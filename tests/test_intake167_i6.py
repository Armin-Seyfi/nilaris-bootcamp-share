"""Root-only, serial real PostgreSQL + authenticated HTTP I6 composition proof.

Only provider transports and generated credentials are controlled. The delivered
reader, process supervisor, acquisition journey and persistence all run normally.
No real Google faults or full114 replay are covered here.
"""
import json
import math
import os
import subprocess
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from test_command_service import PYTHON, ROOT, command_process, stop
from test_drive155_source import configuration


def start_controlled_listener(client, config, fault_path, journal):
    process = subprocess.Popen(
        [*PYTHON, str(ROOT / 'tests/intake167_i6_command_process.py'),
         '--controlled-offline', str(config), str(fault_path), str(journal)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            if client(dict(operation='status', job_id='absent')).get('category') == 'not_found':
                listener = int(subprocess.check_output(
                    ['lsof', '-t', '-nP', '-iTCP:18765', '-sTCP:LISTEN'], text=True).strip())
                assert os.getpgid(listener) == process.pid, 'foreign listener refused'
                return process
            time.sleep(.05)
        raise AssertionError('controlled I6 listener unavailable')
    except BaseException:
        stop(process)
        raise


@pytest.mark.parametrize('fault,acquisition_gets', [
    ('unavailable-revision', 3),
    ('changed-same-length-media', 4),
])
def test_root_exact_acquisition_refusal_is_durable_over_http(
        command_process, tmp_path, fault, acquisition_gets):
    client, _, config = command_process
    fault_path = tmp_path / 'provider-fault'
    journal = tmp_path / 'external-calls.jsonl'
    expected = configuration()['allowed_member']

    def events():
        return [json.loads(line) for line in journal.read_text().splitlines()]

    def start():
        return start_controlled_listener(client, config, fault_path, journal)

    process = start()
    try:
        discovery = dict(operation='drive_discover', binding_id='synthetic-binding',
                         operation_id='i6-discover', page_id='i6-page')
        discovered = client(discovery)
        assert discovered.get('state') == 'completed', discovered
        assert discovered['page']['members'][0]['member'] == expected
        offered = client(dict(discovery, operation='drive_admit'))
        assert offered['totals'] == dict(offered=1, accepted=1, rejected=0, deferred=0)
        job_id = offered['members'][0]['job_id']
        before = client(dict(operation='status', job_id=job_id))
        assert before['state'] == 'acquiring'
        assert before['input'] == expected
        assert before['counters'] == dict(attempts=0, acquisition_requests=0, captured_input_bytes=0)
        discovery_events = events()
        assert len([e for e in discovery_events if e['kind'] == 'get']) == 3
        assert len([e for e in discovery_events if e['kind'] == 'token']) == 1

        # Introduce only an external provider fault after the original offer is durable.
        fault_path.write_text(fault)
        acquire = dict(operation='drive_acquire', binding_id='synthetic-binding',
                       operation_id='i6-acquire', job_id=job_id,
                       expected_revision=before['revision'])
        held = client(acquire)
        assert held.get('state') == 'held', held
        assert held['namespace'] == discovered['namespace']
        assert held['operation_id'] == 'i6-acquire'
        assert held['job_id'] == job_id
        assert held['accounting']['proved'] is True, held
        assert held['accounting']['evidence'] == 'process_group_reaped'
        assert held['accounting']['reason'] == 'source_returned_and_reaped'
        assert held['accounting']['charged_cpu_seconds'] == 20
        # Source operation and Job reservations are distinct, retained charges.
        assert held['reserved_requests'] == 5
        assert held['reservation_bytes'] == 1024**2
        assert held['reserved_cpu_seconds'] == 20
        assert held['active_capacity_reserved'] is True
        current = client(dict(operation='status', job_id=job_id))
        assert held['job'] == current
        assert current['input'] == expected
        assert current['id'] == job_id
        assert current['state'] == 'held'
        assert current['reason'] == 'source_version_unavailable'
        assert current['content_pin'] is None
        assert current['counters'] == dict(attempts=1, acquisition_requests=5, captured_input_bytes=0)
        assert current['coverage'] == dict(acquisition='pending', processing='not_started')
        assert current['reservation_bytes'] == 8 * 1024**2
        assert current['active_capacity_reserved'] is True
        assert current['admission']['source_retained_bytes'] == 2 * 1024**2
        assert current['admission']['active_capacity_reservations'] == 1
        assert current['usage']['cpu_seconds'] == 20
        attempt, = current['attempts']
        assert attempt['stage'] == 'acquisition'
        assert attempt['reason'] == 'source_version_unavailable'
        assert attempt['stopped'] is True
        assert attempt['accounting_complete'] is True
        assert attempt['completion_evidence'] == 'process_group_reaped'
        assert current['effects'] == current['results'] == []
        assert current['effect_count'] == 0
        result = client(dict(operation='result', job_id=job_id))
        assert result['effects'] == result['publications'] == []
        assert result['approved'] is False

        calls = events()
        acquisition_events = calls[len(discovery_events):]
        gets = [e for e in acquisition_events if e['kind'] == 'get']
        assert len(gets) == acquisition_gets
        assert len([e for e in calls if e['kind'] == 'credential']) == 2
        assert len([e for e in calls if e['kind'] == 'token']) == 2
        assert all(e['method'] == 'POST' and e['url'] == 'https://oauth2.googleapis.com/token'
                   for e in calls if e['kind'] == 'token')
        paths = [urlsplit(e['path']) for e in gets]
        file_path = '/drive/v3/files/' + expected['source_id']
        exact_path = file_path + '/revisions/' + expected['source_revision']
        assert [p.path for p in paths] == ['/drive/v3/about', file_path, exact_path] + (
            [exact_path] if fault == 'changed-same-length-media' else [])
        media = [p for p in paths if parse_qs(p.query).get('alt') == ['media']]
        assert len(media) == (1 if fault == 'changed-same-length-media' else 0)
        injected, = [e for e in acquisition_events if e['kind'] == 'fault']
        assert injected['fault'] == fault
        if fault == 'unavailable-revision':
            assert injected['newer_revision'] != expected['source_revision']
        else:
            assert injected['returned_bytes'] == expected['input_bytes']

        # Reconstruct the entire listener/journey/client path against the same DB.
        stop(process)
        process = None
        process = start()
        assert client(dict(operation='drive_operation', binding_id='synthetic-binding',
                           operation_id='i6-acquire')) == held
        assert client(dict(operation='status', job_id=job_id)) == current
        assert client(dict(operation='result', job_id=job_id)) == result
        assert client(acquire) == held
        assert client(dict(acquire, operation_id='replacement-acquisition',
                           expected_revision=current['revision']))['category'] == 'original_acquisition_required'
        assert client(dict(operation='status', job_id=job_id)) == current
        assert client(dict(operation='result', job_id=job_id)) == result
        assert events() == calls, 'restart/readback/replay/replacement must spend no external calls'
        print(json.dumps(dict(case=fault, operation_id=held['operation_id'], job_id=job_id,
            source_requests=held['reserved_requests'], source_reservation_bytes=held['reservation_bytes'],
            job_requests=current['counters']['acquisition_requests'], job_reservation_bytes=current['reservation_bytes'],
            external_gets=len([e for e in calls if e['kind'] == 'get']), token_exchanges=2,
            accounting_evidence=held['accounting']['evidence'], transport_paths=[e['path'] for e in calls if e['kind'] == 'get'])), flush=True)
    finally:
        if process is not None:
            stop(process)


def test_root_http_namespace_fence_refuses_listener_rebound_after_attestation(command_process, tmp_path):
    from nilaris.intake167 import DriveAdapter
    from test_drive155_source import authority
    from test_intake167 import plan
    client, _, config_path = command_process
    config = configuration()
    adapter = DriveAdapter(config, authority(config))
    frozen = plan(adapter)
    fault_path = tmp_path / 'binding-change'
    journal = tmp_path / 'external-calls.jsonl'
    process = start_controlled_listener(client, config_path, fault_path, journal)
    try:
        for operation_id in (frozen['discovery_operation'], frozen['acquisition_operation']):
            attested = client(dict(operation='drive_operation', binding_id=config['binding_id'],
                                   operation_id=operation_id))
            assert attested['category'] == 'source_operation_unknown'
            assert attested['namespace'] == adapter.snapshot['namespace']
        stop(process)
        process = None
        fault_path.write_text('rebound-authority')
        process = start_controlled_listener(client, config_path, fault_path, journal)
        # The binding ID and member stay identical; only the listener's owner
        # grant changes after the client has attested and frozen its authority.
        offered = client(dict(operation='offer', namespace='namespace-fence',
                              command_id='offer', members=[adapter.member]))
        assert offered['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
        job_id = offered['members'][0]['job_id']
        before = client(dict(operation='status', job_id=job_id))
        for mutation in (adapter.discover(frozen), adapter.acquire(frozen, before)):
            refused = client(mutation)
            assert refused['category'] == 'source_namespace_mismatch', refused
            unknown = client(dict(operation='drive_operation', binding_id=config['binding_id'],
                                  operation_id=mutation['operation_id']))
            assert unknown['category'] == 'source_operation_unknown'
            assert unknown['namespace'] != adapter.snapshot['namespace']
        after = client(dict(operation='status', job_id=job_id))
        assert {key: value for key, value in after.items() if key != 'admission'} == {
            key: value for key, value in before.items() if key != 'admission'}
        assert {key: value for key, value in after['admission'].items()
                if key != 'oldest_waiting_seconds'} == {
            key: value for key, value in before['admission'].items()
            if key != 'oldest_waiting_seconds'}
        before_age = before['admission']['oldest_waiting_seconds']
        after_age = after['admission']['oldest_waiting_seconds']
        assert math.isfinite(before_age) and math.isfinite(after_age)
        assert 0 <= before_age <= after_age
        assert after['counters'] == dict(attempts=0, acquisition_requests=0, captured_input_bytes=0)
        assert after['admission']['source_retained_bytes'] == 0
        assert not journal.exists(), 'stale namespace must not dispatch credentials/token/provider'
        assert client(dict(operation='discovery_read', namespace=adapter.snapshot['namespace'],
                           page_id=frozen['page_id']))['category'] == 'unknown'
    finally:
        if process is not None:
            stop(process)
