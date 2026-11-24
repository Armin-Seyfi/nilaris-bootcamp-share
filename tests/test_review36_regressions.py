"""Review regressions observed through the approved public ingestion boundary."""
import hashlib
import time
from datetime import datetime, timezone

import pytest

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_admission import member
from test_recovery import accepted, literal_output, processing


def test_final_capture_fence_preserves_returned_read_accounting(database, monkeypatch):
    client, job_id = accepted(database)
    claim = client.claim(job_id, 0)
    expires_at = datetime.fromisoformat(client.status(job_id)['attempts'][-1]['expires_at'])
    waiting_id = client.offer('operator', 'waiting', [
        dict(member('waiting'), document_id='waiting-document')
    ])['members'][0]['job_id']
    content = b'final-capture-fence-input'

    class MeasuredSource(ControlledSource):
        def read(self, source_id, source_revision, limit):
            # Spend most of the lease outside PostgreSQL, leaving the later
            # hash pause within the unchanged five-second transaction limit.
            time.sleep(7)
            started = time.process_time()
            while time.process_time() - started < .01:
                pass
            return super().read(source_id, source_revision, limit)

    original_hash = hashlib.sha256

    def delayed_hash(data=b'', *args, **kwargs):
        if data == content:
            # The source has returned and capture has passed its first fence.
            # Let the real PostgreSQL lease expire at this external hash boundary.
            remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
            assert 0 < remaining < 4
            time.sleep(remaining + .05)
        return original_hash(data, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(hashlib, 'sha256', delayed_hash)
        rejected = client.capture(job_id, claim, MeasuredSource({'P': ('r1', content)}))
    assert rejected['category'] == 'stale_commit'
    fresh = Ingestion(database).status(job_id)
    assert fresh['state'] == 'held'
    assert fresh['reason'] == 'claim_lost'
    assert fresh['content_pin'] is None
    assert fresh['counters']['captured_input_bytes'] == 0
    assert fresh['counters']['acquisition_requests'] == 1
    assert fresh['usage']['wall_seconds'] >= 7
    assert fresh['usage']['measured_cpu_seconds'] >= .01
    assert fresh['usage']['accounting_unknown'] is False
    assert fresh['attempts'][-1]['stopped'] is True
    assert fresh['attempts'][-1]['completion_evidence'] == 'finite_controlled_call'
    assert client.capture(job_id, claim, ControlledSource({'P': ('r1', content)}))['category'] == 'stale_commit'
    next_claim = client.claim(waiting_id, client.status(waiting_id)['revision'])
    assert next_claim['claimed'] is True
    assert client.status(job_id)['active_capacity_reserved'] is False


def test_completed_job_charges_retained_history_instead_of_execution_headroom(database):
    client, job_id, claim = processing(database)
    completed = client.publish(job_id, claim, literal_output())
    assert completed['state'] == 'completed'
    assert completed['usage']['accounting_unknown'] is False
    assert 0 < completed['reservation_bytes'] < 1024**2
    assert completed['reservation_bytes'] == completed['usage']['retained_bytes']
    result = client.result(job_id)
    restarted = Ingestion(database).status(job_id)
    assert restarted['reservation_bytes'] == completed['reservation_bytes']
    assert restarted['attempts'] == completed['attempts']
    assert restarted['counters']['captured_input_bytes'] == 16
    assert Ingestion(database).result(job_id) == result


@pytest.mark.parametrize('captured', [False, True])
def test_cancelled_job_charges_retained_history_after_finite_stop(database, captured):
    client, job_id = accepted(database)
    if captured:
        client.capture(job_id, client.claim(job_id, 0),
                       ControlledSource({'P': ('r1', b'controlled input')}))
    before = client.status(job_id)
    cancelled = client.cancel(job_id, before['revision'])
    assert cancelled['state'] == 'cancelled'
    assert cancelled['usage']['accounting_unknown'] is False
    assert 0 < cancelled['reservation_bytes'] < 1024**2
    assert cancelled['reservation_bytes'] == cancelled['usage']['retained_bytes']
    assert cancelled['attempts'] == before['attempts']
    assert cancelled['counters'] == before['counters']
    assert Ingestion(database).status(job_id)['reservation_bytes'] == cancelled['reservation_bytes']


def test_partial_publication_charges_retained_history_after_finite_stop(database):
    from dataclasses import replace

    client, job_id, claim = processing(database)
    partial = client.publish(job_id, claim, replace(literal_output(), outcome='partial',
        coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[])))
    assert partial['state'] == 'partial'
    assert partial['usage']['accounting_unknown'] is False
    assert 0 < partial['reservation_bytes'] < 1024**2
    assert partial['reservation_bytes'] == partial['usage']['retained_bytes']
    assert partial['admission']['live_jobs'] == 0
    assert len(Ingestion(database).result(job_id)['publications']) == 1


@pytest.mark.parametrize('capacity_available', [True, False])
def test_partial_retry_reserves_full_headroom_before_requeue(database, capacity_available):
    from dataclasses import replace

    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('partial-retry-bytes-v1',
                                                    logical_bytes=17 * 1024**2)
    client = Ingestion(database, profile)
    job_id = client.offer('operator', 'partial', [member()])['members'][0]['job_id']
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    claim = client.claim(job_id, client.status(job_id)['revision'])
    client.publish(job_id, claim, replace(literal_output(), outcome='partial',
        coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[])))
    if not capacity_available:
        other = client.offer('operator', 'uses-headroom', [
            dict(member('other'), document_id='other-document')])
        assert other['totals']['accepted'] == 1
    before = client.status(job_id)
    result = client.result(job_id)
    retried = client.retry(job_id, before['revision'], before['generation'],
                           'operator', 'finish missing coverage')
    if capacity_available:
        assert retried['state'] == 'queued'
        assert retried['reservation_bytes'] == 8388608
        assert retried['recovery'][-1]['reason'] == 'finish missing coverage'
    else:
        assert retried.get('category') == 'byte_capacity'
        after = client.status(job_id)
        assert after['state'] == 'partial'
        assert after['revision'] == before['revision']
        assert after['generation'] == before['generation']
        assert after['reservation_bytes'] == before['reservation_bytes']
        assert after['recovery'] == before['recovery']
    after = Ingestion(database).status(job_id)
    assert after['attempts'] == before['attempts']
    assert after['counters'] == before['counters']
    assert client.result(job_id) == result
