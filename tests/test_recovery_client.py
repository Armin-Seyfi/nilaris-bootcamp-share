"""Recovery commands through the public textual client and real task database."""
import pytest

from nilaris.admission import Ingestion
from test_admission import member
from test_command_service import command_process  # noqa: F401


def test_text_reconcile_revokes_generation_and_keeps_unknown_worker_held(command_process, database):
    from nilaris.execution import ControlledSource

    client, start, _ = command_process
    start()
    job_id = client(dict(operation='offer', namespace='operator', command_id='revoke',
                         members=[member()]))['members'][0]['job_id']
    worker = Ingestion(database)
    claim = worker.claim(job_id, 0)
    running = client(dict(operation='status', job_id=job_id))
    command = dict(operation='reconcile', job_id=job_id,
                   expected_revision=running['revision'], expected_generation=running['generation'],
                   actor='operator', reason='review interrupted acquisition')
    held = client(command)
    assert held.get('state') == 'held', held
    assert held['reason'] == 'reviewed_generation_revoked'
    assert held['generation'] == running['generation'] + 1
    assert held['active_capacity_reserved'] is True
    assert held['recovery'] == [dict(revision=held['revision'], generation=held['generation'],
                                     actor='operator', reason='review interrupted acquisition')]
    assert client(command)['category'] == 'reconcile_conflict'
    source = ControlledSource({'P': ('r1', b'controlled input')})
    assert worker.capture(job_id, claim, source)['category'] == 'stale_commit'
    before = client(dict(operation='status', job_id=job_id))
    refused = client(dict(command, expected_revision=before['revision'],
                          expected_generation=before['generation']))
    assert refused['category'] == 'recovery_held'
    assert refused['known'] is True
    assert refused['next_action'] == 'query_job'
    assert client(dict(operation='status', job_id=job_id)) == before


def test_text_reconcile_requeues_only_accounted_interruption_with_review(command_process, database):
    from http.client import HTTPConnection
    import json
    from nilaris.execution import ControlledSource
    from test_recovery import literal_output

    client, start, _ = command_process
    start()
    job_id = client(dict(operation='offer', namespace='operator', command_id='reconcile',
                         members=[member()]))['members'][0]['job_id']
    worker = Ingestion(database)
    worker.capture(job_id, worker.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    worker.run_controlled(job_id, worker.status(job_id)['revision'], literal_output(),
                          test_wall_seconds=1, test_delay_seconds=20)
    held = client(dict(operation='status', job_id=job_id))
    assert held['state'] == 'held'
    assert held['reason'] == 'attempt_wall_limit'
    assert held['attempts'][-1]['stopped'] is True
    assert held['attempts'][-1]['accounting_complete'] is True
    assert held['next_action'] == 'reconcile_interruption'
    before = client(dict(operation='result', job_id=job_id))
    command = dict(operation='reconcile', job_id=job_id, expected_revision=held['revision'],
                   expected_generation=held['generation'], actor='operator',
                   reason='reviewed stopped and accounted processing attempt')
    connection = HTTPConnection('127.0.0.1', 18765, timeout=3)
    try:
        connection.request('POST', '/command', json.dumps(command),
                           {'Authorization': 'Bearer wrong-token'})
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read())['category'] == 'unauthorized'
    finally:
        connection.close()
    for changed in (dict(expected_revision=held['revision'] + 1),
                    dict(expected_generation=held['generation'] + 1)):
        assert client(dict(command, **changed))['category'] == 'reconcile_conflict'
    queued = client(command)
    assert queued['state'] == 'queued'
    assert queued['next_action'] == 'await_processing'
    assert queued['generation'] == held['generation'] + 1
    assert queued['active_capacity_reserved'] is False
    assert queued['content_pin'] == held['content_pin']
    assert queued['attempts'] == held['attempts']
    assert queued['usage']['cpu_seconds'] == held['usage']['cpu_seconds']
    assert queued['usage']['wall_seconds'] == held['usage']['wall_seconds']
    assert queued['recovery'][-1] == dict(revision=queued['revision'], generation=queued['generation'],
                                         actor='operator', reason=command['reason'])
    assert client(dict(operation='result', job_id=job_id)) == before
    assert client(command)['category'] == 'reconcile_conflict'
    completed = worker.run_controlled(job_id, queued['revision'], literal_output())
    assert completed['state'] == 'completed'
    assert client(dict(operation='status', job_id=job_id))['state'] == 'completed'
    assert client(dict(operation='result', job_id=job_id))['publications']


def test_text_cancel_checks_revision_and_preserves_effective_cancellation(command_process):
    client, start, _ = command_process
    start()
    offered = client(dict(operation='offer', namespace='operator', command_id='cancel', members=[member()]))
    job_id = offered['members'][0]['job_id']
    initial = client(dict(operation='status', job_id=job_id))
    refused = client(dict(operation='cancel', job_id=job_id, expected_revision=initial['revision'] + 1))
    assert refused['category'] == 'cancel_conflict'
    assert refused['known'] is True
    assert refused['affected'] == dict(job_id=job_id)
    assert client(dict(operation='status', job_id=job_id))['state'] == 'acquiring'
    cancelled = client(dict(operation='cancel', job_id=job_id, expected_revision=initial['revision']))
    assert cancelled['state'] == 'cancelled'
    assert cancelled['next_action'] == 'terminal'
    assert client(dict(operation='resolve', namespace='operator', command_id='cancel')) == offered
    assert client(dict(operation='status', job_id=job_id))['state'] == 'cancelled'


def test_text_retry_records_review_and_preserves_initial_results(command_process, database):
    from dataclasses import replace
    from nilaris.execution import ControlledSource
    from test_recovery import literal_output

    client, start, _ = command_process
    start()
    job_id = client(dict(operation='offer', namespace='operator', command_id='retry', members=[member()]))['members'][0]['job_id']
    worker = Ingestion(database)
    worker.capture(job_id, worker.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    partial = replace(literal_output(), outcome='partial',
                      coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]))
    worker.publish(job_id, worker.claim(job_id, worker.status(job_id)['revision']), partial)
    initial = client(dict(operation='result', job_id=job_id))
    status = client(dict(operation='status', job_id=job_id))
    command = dict(operation='retry', job_id=job_id, expected_revision=status['revision'],
                   expected_generation=status['generation'], actor='operator', reason='inspect remaining page')
    stale = client(dict(command, expected_generation=status['generation'] + 1))
    assert stale['category'] == 'retry_conflict'
    assert stale['known'] is True
    assert stale['next_action'] == 'query_job'
    queued = client(command)
    assert queued['state'] == 'queued'
    assert queued['next_action'] == 'await_processing'
    assert queued['generation'] == status['generation'] + 1
    assert queued['recovery'] == [dict(revision=queued['revision'], generation=queued['generation'],
                                       actor='operator', reason='inspect remaining page')]
    assert client(dict(operation='result', job_id=job_id)) == initial
    replay = client(command)
    assert replay['category'] == 'retry_conflict'
    assert replay['known'] is True
    assert client(dict(operation='result', job_id=job_id)) == initial


def test_text_cancel_reports_claim_won_without_false_cancellation(command_process, database):
    client, start, _ = command_process
    start()
    job_id = client(dict(operation='offer', namespace='operator', command_id='claimed', members=[member()]))['members'][0]['job_id']
    worker = Ingestion(database)
    assert worker.claim(job_id, 0)['claimed'] is True
    running = client(dict(operation='status', job_id=job_id))
    outcome = client(dict(operation='cancel', job_id=job_id, expected_revision=running['revision']))
    assert outcome == dict(known=True, category='cancel_conflict', affected=dict(job_id=job_id),
                           coverage='unknown', reason='current job state or revision prevents this action',
                           next_action='query_job')
    current = client(dict(operation='status', job_id=job_id))
    assert current['state'] == 'running'
    assert current['generation'] == running['generation']
    assert current['current_claim']['id'] == running['current_claim']['id']


@pytest.mark.parametrize('operation', ['retry', 'reconcile'])
def test_text_recovery_rejects_malformed_metadata_and_unapproved_routes(command_process, operation):
    client, start, _ = command_process
    start()
    retry = dict(operation=operation, job_id='absent', expected_revision=0,
                 expected_generation=0, actor='operator', reason='reviewed retry')
    malformed = [
        dict(operation='cancel', job_id='absent', expected_revision=True),
        dict(operation='cancel', job_id='absent', expected_revision=-1),
        dict(retry, expected_generation='0'),
        dict(retry, expected_revision=9223372036854775808),
        dict(retry, actor=' '),
        {key: value for key, value in retry.items() if key != 'reason'},
        dict(retry, output={}),
        dict(operation='run_controlled', job_id='absent', output={}),
        dict(operation='reconcile', job_id='absent'),
    ]
    for command in malformed:
        outcome = client(command)
        assert outcome['category'] == 'invalid_request'
        assert outcome['known'] is True
        assert outcome['affected'] == {}
    oversized_review = client(dict(retry, reason='r' * 4097))
    assert oversized_review['category'] == 'invalid_recovery'
    assert oversized_review['known'] is True
    assert oversized_review['next_action'] == 'query_job'
