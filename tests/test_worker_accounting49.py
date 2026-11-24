"""Worker accounting through public ingestion and real OS scheduling faults."""
import os
import signal
import subprocess
import time

import pytest

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_admission import member
from test_recovery import literal_output


@pytest.mark.parametrize('fresh_inventory', [True, False], ids=['available', 'unavailable'])
def test_deadline_stop_accounts_when_group_exits_before_caller_signal(database, monkeypatch, fresh_inventory):
    """Independent termination racing a caller signal still needs full proof."""
    actual_popen = subprocess.Popen
    actual_killpg = os.killpg
    actual_run = subprocess.run
    groups = {}
    signal_errors = []
    paused = False
    inventory_timeouts = []

    def launch(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if (isinstance(command, list) and len(command) > 3 and
                command[:2] == ['/bin/sh', '-c'] and
                command[3] == 'nilaris-controlled-bootstrap'):
            groups[process.pid] = time.monotonic()
        return process

    def send(group, signum):
        nonlocal paused
        if group in groups and signum == signal.SIGTERM and not paused:
            paused = True
            time.sleep(max(0, groups[group] + 3.3 - time.monotonic()))
        try:
            return actual_killpg(group, signum)
        except ProcessLookupError:
            if group in groups:
                signal_errors.append(dict(pgid=group, signal=int(signum),
                    elapsed=round(time.monotonic() - groups[group], 6)))
            raise

    def inventory(*args, **kwargs):
        command = args[0] if args else kwargs.get('args')
        if (not fresh_inventory and signal_errors and
                command == ['/bin/ps', '-axo', 'pid=,pgid=,rss=,time=,lstart=']):
            # The real OS child stops before exec; the unchanged ps timeout
            # must expire. subprocess.run kills/reaps this exact child.
            delayed = ['/bin/sh', '-c', 'kill -STOP $$; exec "$@"',
                       'nilaris-accounting49-inventory', *command]
            if args:
                args = (delayed, *args[1:])
            else:
                kwargs['args'] = delayed
            try:
                return actual_run(*args, **kwargs)
            except subprocess.TimeoutExpired:
                inventory_timeouts.append(True)
                raise
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(os, 'killpg', send)
    monkeypatch.setattr(subprocess, 'run', inventory)
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'signal-race', [member()])['members'][0]['job_id']
    captured = client.capture(job_id, client.claim(job_id, 0),
                              ControlledSource({'P': ('r1', b'controlled input')}))
    client.run_controlled(job_id, captured['revision'], literal_output(),
                          test_wall_seconds=1, test_delay_seconds=20)
    held = Ingestion(database).status(job_id)
    attempt = held['attempts'][-1]
    evidence = dict(state=held['state'], reason=held.get('reason'),
                    accounting_complete=attempt['accounting_complete'],
                    completion_evidence=attempt['completion_evidence'],
                    worker=attempt['worker'], usage=held['usage'], signal_errors=signal_errors)
    assert paused and len(groups) == 1 and signal_errors, evidence
    if not fresh_inventory:
        assert inventory_timeouts, evidence
        assert held['state'] == 'held' and held['reason'] == 'worker_accounting_unknown', evidence
        assert attempt['accounting_complete'] is False and attempt['stopped'] is False, evidence
        assert attempt['completion_evidence'] is None, evidence
        assert held['usage']['accounting_unknown'] is True, evidence
        assert held['active_capacity_reserved'] is True, evidence
        assert client.result(job_id)['publications'] == [], evidence
        assert client.reconcile(job_id, held['revision'], held['generation'], 'operator',
            'fresh inventory unavailable', disposition='safe-retry')['category'] == 'recovery_held', evidence
        return
    assert held['state'] == 'held' and attempt['accounting_complete'] is True, evidence
    assert held['reason'] == 'attempt_wall_limit' and attempt['stopped'] is True, evidence
    assert attempt['completion_evidence'] == 'process_group_reaped', evidence
    assert held['usage']['accounting_unknown'] is False, evidence
    assert attempt['cpu_seconds'] >= 20, evidence
    assert client.result(job_id)['publications'] == [], evidence
