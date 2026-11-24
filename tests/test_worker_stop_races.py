"""Lower wall expiry must retain complete accounting through the public seam."""
import os
import subprocess
import time

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_admission import member
from test_recovery import literal_output


def assert_lower_wall_accounted(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'lower-wall', [member()])['members'][0]['job_id']
    captured = client.capture(job_id, client.claim(job_id, 0),
                              ControlledSource({'P': ('r1', b'controlled input')}))
    client.run_controlled(job_id, captured['revision'], literal_output(),
                          test_wall_seconds=1, test_delay_seconds=20)
    held = Ingestion(database).status(job_id)
    attempt = held['attempts'][-1]
    worker = attempt.get('worker') or {}
    evidence = dict(state=held['state'], reason=held.get('reason'),
                    accounting_complete=attempt['accounting_complete'],
                    accounting_unknown=held['usage']['accounting_unknown'],
                    active_capacity_reserved=held['active_capacity_reserved'],
                    failure=worker.get('failure'),
                    readiness=worker.get('bootstrap_readiness'),
                    limits=worker.get('limits'), usage=held['usage'])
    assert held['state'] == 'held', evidence
    assert attempt['accounting_complete'] is True, evidence
    assert held['reason'] == 'attempt_wall_limit', evidence
    assert held['usage']['accounting_unknown'] is False, evidence
    assert client.result(job_id)['publications'] == [], evidence


def test_lower_wall_stop_accounts_real_group(database):
    assert_lower_wall_accounted(database)


def test_lower_wall_stop_accounts_when_caller_pauses_before_reap(database, monkeypatch):
    """A delayed caller must account the actual owned group's deadline stop."""
    actual_popen = subprocess.Popen
    actual_wait4 = os.wait4
    bootstrap_pids = set()
    paused = False

    def launch(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if (isinstance(command, list) and len(command) > 3 and
                command[:2] == ['/bin/sh', '-c'] and
                command[3] == 'nilaris-controlled-bootstrap'):
            bootstrap_pids.add(process.pid)
        return process

    def reap(pid, options):
        nonlocal paused
        if pid in bootstrap_pids and options == os.WNOHANG and not paused:
            paused = True
            time.sleep(1.2)
        return actual_wait4(pid, options)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(os, 'wait4', reap)
    try:
        assert_lower_wall_accounted(database)
    finally:
        assert len(bootstrap_pids) == 1 and paused, 'owned bootstrap scheduling fault must occur'
