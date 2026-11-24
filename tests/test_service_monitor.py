"""Controlled monitor files arrange failures; observations use ingestion only."""
import json
import os
import time

from nilaris.admission import AdmissionProfile, Ingestion


def test_timely_variable_guard_probes_keep_public_admission_available(database, tmp_path, monkeypatch):
    """Control Docker/time boundaries while running the guard's actual schedule."""
    import importlib.util
    from functools import partial
    from pathlib import Path
    from types import SimpleNamespace
    from nilaris.admission import migrate
    from nilaris import service_monitor, storage_coordination
    from test_admission import member

    migrate(database)
    spec = importlib.util.spec_from_file_location(
        'task_guard', Path(__file__).resolve().parents[1] / 'runtime/a1/service_guard.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    path = tmp_path / 'monitor.json'

    class Clock:
        now = 100.0

        def monotonic(self):
            return self.now

        def sleep(self, duration):
            self.now += duration

    class Port:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            pass

    clock = Clock()
    coordination = tmp_path / 'postgres-storage.lock'
    coordination.touch(mode=0o600)
    Path(str(coordination) + '.turnstile').touch(mode=0o600)
    outcomes = []
    samples = 0

    def docker_output(command, **kwargs):
        nonlocal samples
        if command[:3] == ['docker', 'ps', '-aq']:
            return ''
        if command[-2:] == ['ps', '-q']:
            return 'postgres-id\nn8n-id'
        if 'du' in command:
            return '1 data'
        if '{{.SizeRw}}' in command:
            samples += 1
            # Both complete within the unchanged one-second probe deadline.
            clock.now += .05 if samples == 1 else .35
            if samples == 2:
                outcomes.append(Ingestion(database, AdmissionProfile.l1(), monitor_path=path).offer(
                    'operator', 'timely-guard', [member()]))
            return '0\n0'
        return ''

    monkeypatch.setattr(guard, 'time', clock)
    monkeypatch.setattr(service_monitor, 'time', clock)
    monkeypatch.setattr(storage_coordination, 'time', clock)
    monkeypatch.setattr(guard, 'storage_lock', partial(
        storage_coordination.storage_lock, path=coordination))
    monkeypatch.setattr(guard, 'socket', SimpleNamespace(socket=Port))
    monkeypatch.setattr(guard.subprocess, 'check_output', docker_output)
    monkeypatch.setattr(guard.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, 'publish_service_monitor',
                        lambda state: service_monitor.publish_service_monitor(state, path))
    monkeypatch.setattr(guard.sys, 'argv', ['service_guard.py', '--seconds', '2'])
    guard.main()
    assert len(outcomes) == 1
    assert outcomes[0]['known'], {'category': outcomes[0].get('category')}
    assert outcomes[0]['totals']['accepted'] == 1


def test_offer_refuses_missing_failed_stale_or_dead_service_monitor(tmp_path):
    path = tmp_path / 'monitor.json'
    client = Ingestion({}, AdmissionProfile.l1(), monitor_path=path)
    for state in (None,
                  dict(state='failed', pid=os.getpid(), sampled_at=time.monotonic()),
                  dict(state='ready', pid=os.getpid(), sampled_at=time.monotonic()-10),
                  dict(state='ready', pid=2147483647, sampled_at=time.monotonic()),
                  dict(state='ready', pid=os.getpid(), sampled_at=time.monotonic()+10),
                  {}):
        if state is not None:
            path.write_text(json.dumps(state))
        result = client.offer('operator', 'guarded', [dict(member_key='m1')])
        assert result == dict(known=False, category='service_monitor_unavailable',
                             affected=dict(namespace='operator', command_id='guarded', member_keys=['m1']),
                             coverage='unknown', receipt_stored=False,
                             reason='fresh live task service monitor required',
                             next_action='restore_task_monitor_then_resolve_or_retry_same_key')
