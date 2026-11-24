"""One PDF through real HTTP/DB/OS; only Drive is a synthetic adapter."""
import base64
import json
import time

import pytest

from test_command_service import command_process
from test_fixture_extraction import EXPECTED, fixture_input


def pinned_pdf_configuration(config):
    item, content = fixture_input('P.pdf')
    item.update(connector='synthetic-drive-v1', source_root='synthetic-root-P',
                expected_sha256='4fb883b2ae49d073b37bb649917050da03c16a4e6e86f1cdb6c4ff3bdb336c34')
    data = json.loads(config.read_text())
    data['drive'] = dict(mode='synthetic-drive-v1', allowed_member=item,
        object=dict(source_id=item['source_id'], source_revision=item['source_revision'],
                    source_root=item['source_root'], content_b64=base64.b64encode(content).decode('ascii')))
    config.write_text(json.dumps(data))
    return item


def test_pinned_pdf_offer_execute_and_result_survive_http_restart(command_process):
    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    process = start()
    command = dict(operation='offer', namespace='ticket05', command_id='one-P', members=[item])
    receipt = client(command)
    assert receipt['totals'] == dict(offered=1, accepted=1, rejected=0, deferred=0)
    job_id = receipt['members'][0]['job_id']
    assert client(dict(operation='resolve', namespace='ticket05', command_id='one-P')) == receipt
    queued = client(dict(operation='status', job_id=job_id))
    assert queued['input'] == item
    acknowledged = client(dict(operation='execute_fixture_v1', job_id=job_id,
                               expected_revision=queued['revision']))
    assert acknowledged['known'] is True
    assert acknowledged['category'] == 'execution_acknowledged'
    assert acknowledged['next_action'] == 'query_job'
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = client(dict(operation='status', job_id=job_id))
        if status.get('state') in ('completed', 'held', 'failed'):
            break
        time.sleep(0.1)
    assert status['state'] == 'completed'
    assert status['counters']['acquisition_requests'] == 1
    assert status['counters']['captured_input_bytes'] == 1505
    result = client(dict(operation='result', job_id=job_id))
    publication, = result['publications']
    assert publication['content_pin'] == item['expected_sha256']
    assert publication['approved'] is False
    for actual, expected in zip(publication['observations'], EXPECTED['P']['observations'], strict=True):
        assert {key: actual[key] for key in expected} == expected
    from test_command_service import stop
    stop(process)
    start()
    assert client(command) == receipt
    assert client(dict(operation='result', job_id=job_id)) == result


def test_captured_pdf_can_be_manually_executed_after_listener_restart(command_process, database):
    from nilaris.admission import Ingestion
    from nilaris.execution import ControlledSource
    from test_command_service import stop

    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    process = start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='queued-P', members=[item]))
    job_id = receipt['members'][0]['job_id']
    # Arrange the durable handoff through public ingestion, with no native worker.
    ingestion = Ingestion(database)
    captured = ingestion.capture(job_id, ingestion.claim(job_id, 0),
        ControlledSource({item['source_id']: (item['source_revision'], fixture_input('P.pdf')[1])}))
    assert captured['state'] == 'queued'
    assert all(attempt['worker'] is None and attempt['stopped'] for attempt in captured['attempts'])
    stop(process)
    start()
    queued = client(dict(operation='status', job_id=job_id))
    response = client(dict(operation='execute_fixture_v1', job_id=job_id,
                           expected_revision=queued['revision']))
    assert response['category'] == 'processing_acknowledged'
    assert response['state'] == 'running'
    assert response['content_pin'] == item['expected_sha256']
    assert response['next_action'] == 'query_job'
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = client(dict(operation='status', job_id=job_id))
        if status.get('state') in ('completed', 'held', 'failed'):
            break
        time.sleep(0.1)
    assert status['state'] == 'completed'
    assert [attempt['stage'] for attempt in status['attempts']] == ['acquisition', 'processing']
    assert status['counters']['acquisition_requests'] == 1
    assert status['content_pin'] == item['expected_sha256']


def test_changed_bytes_under_offered_revision_are_held_without_processing(command_process):
    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    data = json.loads(config.read_text())
    data['drive']['object']['content_b64'] = base64.b64encode(fixture_input('P2.pdf')[1]).decode('ascii')
    config.write_text(json.dumps(data))
    start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='changed-P', members=[item]))
    job_id = receipt['members'][0]['job_id']
    assert client(dict(operation='execute_fixture_v1', job_id=job_id, expected_revision=0))['category'] == 'execution_acknowledged'
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client(dict(operation='status', job_id=job_id))
        if status.get('state') == 'held':
            break
        time.sleep(0.1)
    assert status['state'] == 'held'
    assert status['reason'] == 'source_pin_mismatch'
    assert status['input'] == item
    assert status['content_pin'] is None
    assert status['counters'] == dict(attempts=1, acquisition_requests=1, captured_input_bytes=0)
    assert all(attempt['worker'] is None and attempt['stopped'] for attempt in status['attempts'])
    assert client(dict(operation='result', job_id=job_id))['publications'] == []


def test_backend_policy_denies_out_of_scope_offers_before_canonical_reuse(command_process):
    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='allowed-P', members=[item]))
    job_id = receipt['members'][0]['job_id']
    for field, value in [('source_root', 'other-root'), ('source_id', 'other-file'),
                         ('connector', 'synthetic'), ('membership_revision', 'other-membership')]:
        offered = dict(operation='offer', namespace='ticket05', command_id='denied-' + field,
                       members=[dict(item, **{field: value})])
        denied = client(offered)
        assert 'totals' in denied, denied
        assert denied['totals'] == dict(offered=1, accepted=0, rejected=1, deferred=0)
        disposition, = denied['members']
        assert disposition['reason'] == 'source_policy_denied'
        assert 'job_id' not in disposition
        assert client(dict(operation='resolve', namespace=offered['namespace'],
                           command_id=offered['command_id'])) == denied
    status = client(dict(operation='status', job_id=job_id))
    assert status['input'] == item
    assert status['counters']['attempts'] == status['counters']['acquisition_requests'] == 0
    assert status['admission']['live_jobs'] == 1


def test_lost_execution_ack_and_restart_replay_create_only_one_worker(command_process):
    from http.client import HTTPConnection
    from test_command_service import stop

    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    process = start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='lost-ack-P', members=[item]))
    job_id = receipt['members'][0]['job_id']
    command = dict(operation='execute_fixture_v1', job_id=job_id, expected_revision=0)
    # Send the full request but deliberately discard its response. No completion
    # is inferred from transport delivery, and no native group is killed.
    connection = HTTPConnection('127.0.0.1', 18765, timeout=3)
    connection.request('POST', '/command', json.dumps(command),
        {'Authorization': 'Bearer synthetic-test-token', 'Content-Type': 'application/json'})
    connection.close()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = client(dict(operation='status', job_id=job_id))
        if status.get('state') in ('completed', 'held', 'failed'):
            break
        time.sleep(0.1)
    assert status['state'] == 'completed'
    assert status['counters']['attempts'] == 2
    assert status['counters']['acquisition_requests'] == 1
    processing, = [attempt for attempt in status['attempts'] if attempt['stage'] == 'processing']
    assert processing['stopped'] and processing['accounting_complete']
    assert processing['completion_evidence'] == 'process_group_reaped'
    result = client(dict(operation='result', job_id=job_id))
    assert client(command)['category'] == 'claim_conflict'
    stop(process)
    start()
    assert client(command)['category'] == 'claim_conflict'
    replay = client(dict(operation='status', job_id=job_id))
    assert replay['attempts'] == status['attempts']
    assert client(dict(operation='result', job_id=job_id)) == result


@pytest.mark.parametrize('changed_pin', [
    pytest.param(dict(expected_sha256='be9827a4efc8401cdfb5a76a8b6b17d3a5d8df67e80e43274ccb74b5fd09d9de'), id='hash'),
    pytest.param(dict(input_bytes=1506), id='size')])
def test_reloaded_policy_cannot_rebind_an_admitted_revision_pin(command_process, changed_pin):
    from test_command_service import stop

    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    process = start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='original-pin', members=[item]))
    job_id = receipt['members'][0]['job_id']
    stop(process)
    changed = dict(item, **changed_pin)
    data = json.loads(config.read_text())
    data['drive']['allowed_member'] = changed
    if 'expected_sha256' in changed_pin:
        data['drive']['object']['content_b64'] = base64.b64encode(fixture_input('P2.pdf')[1]).decode('ascii')
    config.write_text(json.dumps(data))
    start()
    refused = client(dict(operation='offer', namespace='ticket05', command_id='rebound-pin', members=[changed]))
    assert refused['totals'] == dict(offered=1, accepted=0, rejected=1, deferred=0)
    assert refused['members'][0]['reason'] == 'source_pin_conflict'
    original = client(dict(operation='status', job_id=job_id))
    assert original['input'] == item
    assert original['counters']['attempts'] == original['counters']['acquisition_requests'] == 0
    assert client(dict(operation='execute_fixture_v1', job_id=job_id,
                       expected_revision=original['revision']))['category'] == 'claim_conflict'


def test_missing_offered_revision_is_held_without_latest_fallback(command_process):
    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    data = json.loads(config.read_text())
    data['drive']['object']['source_revision'] = 'a1-P-r2'
    data['drive']['object']['content_b64'] = base64.b64encode(fixture_input('P2.pdf')[1]).decode('ascii')
    config.write_text(json.dumps(data))
    start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='missing-P', members=[item]))
    job_id = receipt['members'][0]['job_id']
    assert client(dict(operation='execute_fixture_v1', job_id=job_id, expected_revision=0))['category'] == 'execution_acknowledged'
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client(dict(operation='status', job_id=job_id))
        if status.get('state') == 'held':
            break
        time.sleep(0.1)
    assert status['state'] == 'held'
    assert status['reason'] == 'source_version_unavailable'
    assert status['input'] == item and status['content_pin'] is None
    assert status['counters'] == dict(attempts=1, acquisition_requests=1, captured_input_bytes=0)
    assert all(attempt['worker'] is None and attempt['stopped'] for attempt in status['attempts'])
    assert client(dict(operation='result', job_id=job_id))['publications'] == []


def test_live_configuration_is_refused_before_http_listener_starts(command_process):
    import subprocess
    from test_command_service import PYTHON, ROOT

    _, _, config = command_process
    pinned_pdf_configuration(config)
    data = json.loads(config.read_text())
    data['drive']['mode'] = 'drive-readonly-live-v1'
    config.write_text(json.dumps(data))
    response = subprocess.run([*PYTHON, '-m', 'nilaris.command_service', 'serve',
                               '--config', str(config)], cwd=ROOT, text=True,
                              capture_output=True, timeout=15)
    assert response.returncode == 1
    envelope = json.loads(response.stderr.splitlines()[0])
    assert envelope['category'] == 'configuration_unavailable'
    assert response.stdout == ''


def test_actual_capture_size_must_match_the_durable_offer(command_process):
    client, start, config = command_process
    item = pinned_pdf_configuration(config)
    item = dict(item, input_bytes=1506)
    data = json.loads(config.read_text())
    data['drive']['allowed_member'] = item
    config.write_text(json.dumps(data))
    start()
    receipt = client(dict(operation='offer', namespace='ticket05', command_id='wrong-size-P', members=[item]))
    job_id = receipt['members'][0]['job_id']
    assert client(dict(operation='execute_fixture_v1', job_id=job_id, expected_revision=0))['category'] == 'execution_acknowledged'
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = client(dict(operation='status', job_id=job_id))
        if status.get('state') == 'held':
            break
        time.sleep(0.1)
    assert status['state'] == 'held' and status['reason'] == 'source_pin_mismatch'
    assert status['input']['input_bytes'] == 1506
    assert status['counters'] == dict(attempts=1, acquisition_requests=1, captured_input_bytes=0)
    assert all(attempt['worker'] is None and attempt['stopped'] for attempt in status['attempts'])
    assert client(dict(operation='result', job_id=job_id))['publications'] == []
