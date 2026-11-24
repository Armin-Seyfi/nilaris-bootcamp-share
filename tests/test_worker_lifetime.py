"""Independent worker lifetime at the public ingestion/status seam."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.execution import ControlledSource

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admission import member
from test_recovery import accepted, literal_output


ROOT = Path(__file__).resolve().parents[1]
LOWER_WALL_SECONDS = 4


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM is uncertainty, never proof of absence. Native group teardown
        # can expose it transiently; require a fresh real OS inventory instead.
        result = subprocess.run(['/bin/ps', '-axo', 'pgid='], capture_output=True,
                                text=True, timeout=1, check=True)
        return any(int(value.strip()) == pgid for value in result.stdout.splitlines())


def cleanup_owned_group(worker):
    if not worker:
        return
    for member in (worker, worker.get('stage'), worker.get('independent_monitor')):
        if not member:
            continue
        identity = subprocess.run(['/bin/ps', '-p', str(member['pid']), '-o', 'pgid=,lstart='],
                                  capture_output=True, text=True, timeout=1).stdout.split(None, 1)
        if (len(identity) == 2 and int(identity[0]) == worker['pgid'] and
                identity[1].split() == member['start'].split()):
            try:
                os.killpg(worker['pgid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
            return


def group_states(pgid):
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='], capture_output=True,
                            text=True, timeout=1, check=True)
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and int(fields[1]) == pgid:
            members.append(dict(pid=int(fields[0]), pgid=pgid, state=fields[2]))
    return members


def corrupt_control_wire(mode):
    """Fault only the real monitor stderr transport in this external harness."""
    actual_popen = subprocess.Popen
    applied = threading.Event()
    threads = []
    foreign_start = subprocess.run(['/bin/ps', '-p', str(os.getpid()), '-o', 'lstart='],
                                   capture_output=True, text=True, timeout=1, check=True).stdout.strip()

    def launch(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if not (isinstance(command, list) and command[:2] == ['/bin/sh', '-c'] and
                command[3] == 'nilaris-controlled-bootstrap'):
            return process
        actual_stderr = process.stderr
        read_fd, write_fd = os.pipe()
        process.stderr = os.fdopen(read_fd, 'rb')

        def forward():
            try:
                raw = actual_stderr.read(4096)
                if mode == 'open-output':
                    os.write(write_fd, raw)
                    applied.set()
                    time.sleep(3)
                    return
                prefix = b'NILARIS_WORKER_STOP '
                line = raw.split(b'\n', 1)[0]
                if line.startswith(prefix):
                    receipt = json.loads(line[len(prefix):])
                    if receipt.get('reason') == ('worker_rss_limit' if mode.endswith('-rss') else 'attempt_cpu_limit'):
                        if mode in ('malformed', 'malformed-rss'):
                            raw = prefix + b'{FAULT_CONTROL_PAYLOAD}\n'
                        elif mode == 'foreign':
                            receipt.update(pid=os.getpid(), pgid=os.getpgrp(), start=foreign_start,
                                           cpu=1234.5, rss=8 * 1024**3)
                            raw = prefix + json.dumps(receipt).encode() + b'\n'
                        elif mode == 'open-rss':
                            os.write(write_fd, raw)
                            applied.set()
                            time.sleep(3)
                            return
                        else:
                            raise ValueError('unknown wire fault')
                        assert len(raw) <= 4096
                        os.write(write_fd, raw)
                        applied.set()
                        return
                os.write(write_fd, raw)
            finally:
                actual_stderr.close()
                os.close(write_fd)

        thread = threading.Thread(target=forward, daemon=True)
        threads.append(thread)
        thread.start()
        return process

    subprocess.Popen = launch
    return applied, threads


@pytest.mark.parametrize('kill_monitor', [False, True], ids=['caller-only', 'monitor-too'])
def test_killed_caller_cannot_leave_controlled_group_past_lower_deadline(database, kill_monitor):
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(ROOT / 'runtime/a1/with_overlay.py'), str(Path(__file__)), '--supervisor'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker = None
    try:
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
                                               revision=client.status(job_id)['revision'])).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if status['state'] == 'running' and status['attempts'][-1]['worker']:
                candidate = status['attempts'][-1]['worker']
                # The original adapter wrote input immediately. A gated adapter
                # must actually dispatch graph work before this crash, so EOF
                # during startup cannot masquerade as deadline enforcement.
                if candidate.get('bootstrap_readiness', 'input_dispatched') == 'input_dispatched':
                    worker = candidate
                    break
            time.sleep(.02)
        assert worker is not None, 'worker identity must be durable before caller death'
        assert worker['limits']['version'] == 'controlled-worker-lower-test-v1'
        assert worker['limits']['attempt_wall_seconds'] == LOWER_WALL_SECONDS
        os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=5)
        killed_at = time.monotonic()
        if kill_monitor:
            monitor = worker['independent_monitor']
            identity = subprocess.run(['/bin/ps', '-p', str(monitor['pid']), '-o', 'pgid=,lstart='],
                                      capture_output=True, text=True, timeout=1, check=True).stdout.split(None, 1)
            assert int(identity[0]) == worker['pgid']
            assert identity[1].split() == monitor['start'].split()
            os.kill(monitor['pid'], signal.SIGKILL)
            # The bootstrap normally cancels its timers after monitor exit.
            # Prove its leader has gone while the armed actual stage survives;
            # the remaining deadline is then the worker's own kernel timer.
            deadline = time.monotonic() + 1.5
            members = group_states(worker['pgid'])
            while any(m['pid'] == worker['pid'] for m in members) and time.monotonic() < deadline:
                time.sleep(.02)
                members = group_states(worker['pgid'])
            assert not any(m['pid'] == worker['pid'] for m in members), members
            assert any(m['pid'] == worker['stage']['pid'] and not m['state'].startswith('Z')
                       for m in members), members
        # No harness signal reaches the stage or whole worker group before this verdict.
        deadline = killed_at + LOWER_WALL_SECONDS + 3
        while group_exists(worker['pgid']) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not group_exists(worker['pgid']), 'worker outlived its independent deadline'
        assert time.monotonic() - killed_at < LOWER_WALL_SECONDS + 3
        restarted = Ingestion(database, AdmissionProfile.l1())
        current = restarted.status(job_id)
        held = restarted.reconcile(job_id, current['revision'], current['generation'],
                                    'operator', 'caller died before resource accounting')
        assert held['state'] == 'held'
        assert held['usage']['accounting_unknown'] is True
        assert held['active_capacity_reserved'] is True
        assert held['attempts'][-1]['accounting_complete'] is False
        assert held['generation'] > status['generation']
        assert restarted.reconcile(job_id, status['revision'], status['generation'],
                                    'operator', 'stale caller generation')['category'] == 'reconcile_conflict'
        assert restarted.result(job_id)['publications'] == []
        other = restarted.offer('operator', 'other',
            [dict(member('other'), document_id='other', source_id='other')])['members'][0]['job_id']
        assert restarted.claim(other, 0)['category'] == 'active_capacity'
        assert restarted.reconcile(job_id, held['revision'], held['generation'],
                                    'operator', 'group absent accounting still unknown')['category'] == 'recovery_held'
        fresh = restarted.status(job_id)
        assert fresh['attempts'] == held['attempts']
        assert fresh['usage'] == held['usage']
        assert restarted.result(job_id)['publications'] == []
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        cleanup_owned_group(worker)


if __name__ == '__main__' and sys.argv[1:] == ['--supervisor']:
    request = json.load(sys.stdin)
    if request.get('inventory_pause_marker'):
        actual_run = subprocess.run
        inventory_paused = False

        def pause_inventory(*args, **kwargs):
            global inventory_paused
            command = args[0] if args else kwargs.get('args')
            if (inventory_paused and request.get('inventory_cleanup_unavailable')
                    and command == ['/bin/ps', '-axo', 'pid=,pgid=,rss=,time=,lstart=']):
                delayed = ['/bin/sh', '-c', 'kill -STOP $$; exec "$@"',
                           'nilaris-inventory66-unavailable', *command]
                if args:
                    args = (delayed, *args[1:])
                else:
                    kwargs['args'] = delayed
                return actual_run(*args, **kwargs)
            if (not inventory_paused and Path(request['inventory_pause_marker']).exists()
                    and command == ['/bin/ps', '-axo', 'pid=,pgid=,rss=,time=,lstart=']):
                inventory_paused = True
                script = ('/bin/sleep .05; kill -STOP "$PPID"; exec "$@"'
                          if request.get('inventory_pause_stop', True)
                          else '/bin/sleep .05; exec "$@"')
                command = ['/bin/sh', '-c', script,
                           'nilaris-inventory66-pause', *command]
                if args:
                    args = (command, *args[1:])
                else:
                    kwargs['args'] = command
            return actual_run(*args, **kwargs)

        subprocess.run = pause_inventory
    fault = corrupt_control_wire(request['control_fault']) if request.get('control_fault') else None
    options = dict(test_wall_seconds=LOWER_WALL_SECONDS, test_delay_seconds=20)
    options.update(request.get('stage_options', {}))
    response = Ingestion(request['database']).run_controlled(
        request['job_id'], request['revision'], literal_output(),
        **options)
    summary = {key: response.get(key) for key in ('category', 'state', 'reason')}
    if fault:
        applied, threads = fault
        for thread in threads:
            thread.join(timeout=1)
        summary.update(fault_applied=applied.is_set(),
                       fault_threads_stopped=all(not thread.is_alive() for thread in threads))
    sys.stdout.write(json.dumps(summary))


def test_lower_cpu_allowance_holds_and_another_accounted_job_progresses(database):
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    held = client.run_controlled(job_id, client.status(job_id)['revision'], literal_output(),
                                 test_wall_seconds=15, test_cpu_seconds=4,
                                 test_workload='cpu')
    assert held['state'] == 'held'
    assert held['reason'] == 'attempt_cpu_limit', dict(
        reason=held['reason'], usage=held['usage'], attempt=held['attempts'][-1])
    attempt = held['attempts'][-1]
    assert attempt['accounting_complete'] is True
    assert attempt['stopped'] is True
    assert attempt['cpu_seconds'] >= 4
    assert attempt['measured_cpu_seconds'] > 0
    assert attempt['peak_rss_bytes'] > 0
    assert attempt['worker']['limits']['version'] == 'controlled-worker-lower-test-v1'
    assert attempt['worker']['limits']['remaining_cpu_seconds'] == 4
    assert client.result(job_id)['publications'] == []
    other = client.offer('operator', 'after-cpu',
        [dict(member('after-cpu'), document_id='after-cpu', source_id='after-cpu')])['members'][0]['job_id']
    acquired = client.claim(other, 0)
    assert acquired['claimed'] is True
    ready = client.capture(other, acquired,
                           ControlledSource({'after-cpu': ('r1', b'different next input')}))
    assert client.run_controlled(other, ready['revision'], literal_output())['state'] == 'completed'


def test_stalled_caller_observes_independent_cpu_stop_before_wall_deadline(database):
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(ROOT / 'runtime/a1/with_overlay.py'), str(Path(__file__)), '--supervisor'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker = None
    try:
        options = dict(test_wall_seconds=15, test_cpu_seconds=4,
                       test_delay_seconds=0, test_workload='cpu')
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
            revision=client.status(job_id)['revision'], stage_options=options)).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            candidate = status['attempts'][-1]['worker']
            if candidate and candidate.get('bootstrap_readiness') == 'input_dispatched':
                worker = candidate
                break
            time.sleep(.02)
        assert worker is not None, 'controlled CPU work must actually be dispatched'
        os.killpg(supervisor.pid, signal.SIGSTOP)
        stopped_at = time.monotonic()
        deadline = stopped_at + 5
        paused_members = group_states(worker['pgid'])
        while any(not m['state'].startswith('Z') for m in paused_members) and time.monotonic() < deadline:
            time.sleep(.05)
            paused_members = group_states(worker['pgid'])
        elapsed_pause = time.monotonic() - stopped_at
        # A stopped caller cannot reap its dead direct child. Observe live
        # execution separately, then require actual group absence after reaping.
        os.killpg(supervisor.pid, signal.SIGCONT)
        supervisor.wait(timeout=6)
        held = client.status(job_id)
        assert held['state'] == 'held'
        attempt = held['attempts'][-1]
        evidence = dict(reason=held['reason'], usage=held['usage'], attempt=attempt,
                        paused_members=paused_members, elapsed_pause=elapsed_pause)
        control = attempt['worker'].get('control')
        assert control and control['reason'] == 'attempt_cpu_limit', evidence
        assert control['cpu'] >= 4
        assert elapsed_pause < 6, evidence
        assert not group_exists(worker['pgid']), evidence
        assert time.monotonic() - stopped_at < 15, evidence
        assert attempt['measured_cpu_seconds'] >= control['cpu'], evidence
        assert held['reason'] == 'attempt_cpu_limit', dict(
            reason=held['reason'], parent_cpu=attempt['measured_cpu_seconds'],
            monitor_cpu=control['cpu'], monitor_pid=control['pid'],
            accounting_complete=attempt['accounting_complete'], stopped=attempt['stopped'],
            paused_members=paused_members)
        assert held['attempts'][-1]['accounting_complete'] is True
        assert held['attempts'][-1]['stopped'] is True
        assert held['usage']['cpu_seconds'] >= 4
        assert client.result(job_id)['publications'] == []
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        cleanup_owned_group(worker)


@pytest.mark.parametrize('inventory_pause', [False, True, 'no-pause', 'unavailable', 'malformed', 'open-pipe'],
                         ids=['natural', 'in-flight-inventory', 'inventory-no-pause',
                              'cleanup-unavailable', 'control-malformed', 'control-pipe-open'])
def test_stalled_caller_observes_independent_rss_stop_with_lower_versioned_bound(database, tmp_path, inventory_pause):
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(ROOT / 'runtime/a1/with_overlay.py'), str(Path(__file__)), '--supervisor'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker = None
    try:
        options = dict(test_wall_seconds=15, test_rss_bytes=512 * 1024**2,
                       test_delay_seconds=0, test_workload='rss')
        marker = tmp_path / 'inventory-pause'
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
            revision=client.status(job_id)['revision'], stage_options=options,
            inventory_pause_marker=str(marker) if inventory_pause else None,
            inventory_pause_stop=inventory_pause != 'no-pause',
            inventory_cleanup_unavailable=inventory_pause == 'unavailable',
            control_fault={'malformed': 'malformed-rss', 'open-pipe': 'open-rss'}.get(inventory_pause))).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            candidate = status['attempts'][-1]['worker']
            if candidate and candidate.get('bootstrap_readiness') == 'input_dispatched':
                worker = candidate
                break
            time.sleep(.02)
        response = None
        if supervisor.poll() is not None:
            response = json.loads(supervisor.stdout.read(4096) or '{}')
        assert worker is not None, dict(reason=status.get('reason'), state=status.get('state'),
                                        response=response)
        if inventory_pause:
            marker.touch()
        if inventory_pause and inventory_pause != 'no-pause':
            pause_deadline = time.monotonic() + 2
            while not any(m['state'].startswith('T') for m in group_states(supervisor.pid)):
                assert time.monotonic() < pause_deadline, 'real inventory must pause caller'
                time.sleep(.01)
        elif not inventory_pause:
            os.killpg(supervisor.pid, signal.SIGSTOP)
        stopped_at = time.monotonic()
        deadline = stopped_at + 5
        paused_members = group_states(worker['pgid'])
        while any(not m['state'].startswith('Z') for m in paused_members) and time.monotonic() < deadline:
            time.sleep(.05)
            paused_members = group_states(worker['pgid'])
        if inventory_pause and inventory_pause != 'no-pause':
            time.sleep(max(0, stopped_at + 1.2 - time.monotonic()))
        if inventory_pause != 'no-pause':
            os.killpg(supervisor.pid, signal.SIGCONT)
        supervisor.wait(timeout=6)
        held = client.status(job_id)
        attempt = held['attempts'][-1]
        evidence = dict(reason=held['reason'], peak_rss=attempt['peak_rss_bytes'],
                        control=attempt['worker'].get('control'), failure=attempt['worker'].get('failure'),
                        paused_members=paused_members, worker=attempt['worker'],
                        accounting_complete=attempt['accounting_complete'], stopped=attempt['stopped'])
        assert held['state'] == 'held', evidence
        control = attempt['worker'].get('control')
        if inventory_pause == 'malformed':
            assert control is None, evidence
        elif inventory_pause == 'open-pipe':
            pass
        elif inventory_pause != 'no-pause':
            assert control and control['reason'] == 'worker_rss_limit', evidence
            assert control['rss'] >= 512 * 1024**2, evidence
        else:
            assert not attempt['worker'].get('failure'), evidence
        if inventory_pause in ('unavailable', 'malformed', 'open-pipe'):
            if inventory_pause in ('malformed', 'open-pipe'):
                summary = json.loads(supervisor.stdout.read(4096))
                assert summary['fault_applied'] and summary['fault_threads_stopped'], summary
            assert held['reason'] == 'worker_accounting_unknown', evidence
            assert attempt['accounting_complete'] is False and attempt['stopped'] is False, evidence
            assert attempt['completion_evidence'] is None, evidence
            assert held['usage']['accounting_unknown'] is True, evidence
            assert held['active_capacity_reserved'] is True, evidence
            assert attempt['worker']['failure'] == dict(phase='process_inventory', exception_type='TimeoutExpired')
            assert client.result(job_id)['publications'] == [], evidence
            assert client.reconcile(job_id, held['revision'], held['generation'], 'operator',
                'inventory proof incomplete', disposition='safe-retry')['category'] == 'recovery_held', evidence
            assert not group_exists(worker['pgid']), evidence
            return
        if held['reason'] != 'worker_rss_limit':
            pytest.fail(json.dumps(evidence, sort_keys=True))
        assert attempt['peak_rss_bytes'] >= (control['rss'] if control else 512 * 1024**2), evidence
        assert attempt['accounting_complete'] is True
        assert attempt['stopped'] is True
        if inventory_pause is True:
            assert attempt['worker']['failure'] == dict(phase='process_inventory', exception_type='TimeoutExpired')
        assert attempt['worker']['limits']['rss_bytes'] == 512 * 1024**2
        assert attempt['worker']['limits']['version'] == 'controlled-worker-lower-test-v1'
        assert attempt['worker']['limits']['aggregate_native_hard_limit'] is False
        assert not group_exists(worker['pgid'])
        assert time.monotonic() - stopped_at < 15
        assert client.result(job_id)['publications'] == []
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        cleanup_owned_group(worker)


@pytest.mark.parametrize('inventory_pause', [False, True, 'no-pause', 'unavailable', 'below-limit', 'open-pipe'],
                         ids=['natural', 'in-flight-inventory', 'inventory-no-pause',
                              'cleanup-unavailable', 'output-below-limit', 'control-pipe-open'])
def test_stalled_caller_bounds_output_then_accounts_overflow_after_resume(database, tmp_path, inventory_pause):
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(ROOT / 'runtime/a1/with_overlay.py'), str(Path(__file__)), '--supervisor'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker = None
    try:
        options = dict(test_wall_seconds=15, test_delay_seconds=0, test_workload='output')
        if inventory_pause == 'below-limit':
            options.update(test_workload='literal', test_delay_seconds=20)
        marker = tmp_path / 'output-inventory-pause'
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
            revision=client.status(job_id)['revision'], stage_options=options,
            inventory_pause_marker=str(marker) if inventory_pause else None,
            inventory_pause_stop=inventory_pause != 'no-pause',
            inventory_cleanup_unavailable=inventory_pause == 'unavailable',
            control_fault='open-output' if inventory_pause == 'open-pipe' else None)).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            candidate = status['attempts'][-1]['worker']
            if candidate and candidate.get('bootstrap_readiness') == 'input_dispatched':
                worker = candidate
                break
            time.sleep(.02)
        response = None
        if supervisor.poll() is not None:
            response = json.loads(supervisor.stdout.read(4096) or '{}')
        assert worker is not None, dict(state=status.get('state'), response=response)
        if inventory_pause:
            marker.touch()
            if inventory_pause != 'no-pause':
                deadline = time.monotonic() + 2
                while not any(m['state'].startswith('T') for m in group_states(supervisor.pid)):
                    assert time.monotonic() < deadline, 'real inventory must pause caller'
                    time.sleep(.01)
                time.sleep(1.2)
        else:
            os.killpg(supervisor.pid, signal.SIGSTOP)
            time.sleep(5)
        paused = client.status(job_id)
        if inventory_pause != 'no-pause':
            assert paused['state'] == 'running'
            assert paused['attempts'][-1]['accounting_complete'] is False
        assert client.result(job_id)['publications'] == []
        if inventory_pause != 'no-pause':
            os.killpg(supervisor.pid, signal.SIGCONT)
        supervisor.wait(timeout=8)
        held = client.status(job_id)
        attempt = held['attempts'][-1]
        evidence = dict(reason=held['reason'], failure=attempt['worker'].get('failure'),
                        usage=held['usage'], worker=attempt['worker'],
                        accounting_complete=attempt['accounting_complete'], stopped=attempt['stopped'])
        assert held['state'] == 'held', evidence
        if inventory_pause in ('unavailable', 'below-limit', 'open-pipe'):
            assert held['reason'] == 'worker_accounting_unknown', evidence
            assert not attempt['accounting_complete'] and not attempt['stopped'], evidence
            assert attempt['completion_evidence'] is None, evidence
            assert held['usage']['accounting_unknown'] and held['active_capacity_reserved'], evidence
            assert attempt['worker']['failure'] == dict(phase='process_inventory', exception_type='TimeoutExpired'), evidence
            if inventory_pause == 'below-limit':
                assert attempt['worker']['output_bytes_observed'] < 2 * 1024**2, evidence
            elif inventory_pause == 'open-pipe':
                summary = json.loads(supervisor.stdout.read(4096))
                assert summary['fault_applied'] and summary['fault_threads_stopped'], summary
                assert attempt['worker']['output_bytes_observed'] > 2 * 1024**2, evidence
            assert client.reconcile(job_id, held['revision'], held['generation'], 'operator',
                'output proof incomplete', disposition='safe-retry')['category'] == 'recovery_held', evidence
            assert client.result(job_id)['publications'] == [], evidence
            # Unavailable cleanup inventory cannot authorize a signal. The
            # unchanged independent deadline must still empty the actual group.
            deadline = time.monotonic() + 18
            while group_exists(worker['pgid']) and time.monotonic() < deadline:
                time.sleep(.05)
            assert not group_exists(worker['pgid']), evidence
            return
        if held['reason'] != 'worker_output_limit':
            pytest.fail(json.dumps(evidence, sort_keys=True))
        if inventory_pause is True:
            assert attempt['worker']['failure'] == dict(phase='process_inventory', exception_type='TimeoutExpired'), evidence
            assert not attempt['worker'].get('control'), evidence
        if inventory_pause == 'no-pause':
            assert not attempt['worker'].get('failure'), evidence
        assert attempt['accounting_complete'] is True, evidence
        assert attempt['stopped'] is True, evidence
        assert attempt['worker']['limits']['output_bytes'] == 2 * 1024**2
        assert attempt['worker']['output_bytes_observed'] > 2 * 1024**2
        assert attempt['worker']['output_bytes_observed'] <= 2 * 1024**2 + 8192
        assert attempt['cpu_seconds'] >= 20
        assert attempt['peak_rss_bytes'] > 0
        assert not group_exists(worker['pgid'])
        assert client.result(job_id)['publications'] == []
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        cleanup_owned_group(worker)


@pytest.mark.parametrize('wire_fault', ['malformed', 'foreign'])
def test_bad_monitor_receipt_cannot_manufacture_resource_evidence(database, wire_fault):
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(ROOT / 'runtime/a1/with_overlay.py'), str(Path(__file__)), '--supervisor'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker = None
    try:
        options = dict(test_wall_seconds=15, test_cpu_seconds=4,
                       test_delay_seconds=0, test_workload='cpu')
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
            revision=client.status(job_id)['revision'], stage_options=options,
            control_fault=wire_fault)).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            candidate = status['attempts'][-1]['worker']
            if candidate and candidate.get('bootstrap_readiness') == 'input_dispatched':
                worker = candidate
                break
            time.sleep(.02)
        assert worker is not None
        os.killpg(supervisor.pid, signal.SIGSTOP)
        time.sleep(5)
        os.killpg(supervisor.pid, signal.SIGCONT)
        supervisor.wait(timeout=6)
        summary = json.loads(supervisor.stdout.read(4096))
        assert summary['fault_applied'] is True, summary
        assert summary['fault_threads_stopped'] is True, summary
        held = client.status(job_id)
        attempt = held['attempts'][-1]
        evidence = dict(reason=held['reason'], cpu=attempt['measured_cpu_seconds'],
                        failure=attempt['worker'].get('failure'))
        assert held['state'] == 'held', evidence
        assert attempt['worker'].get('control') is None, evidence
        assert attempt['measured_cpu_seconds'] < 4, evidence
        assert held['reason'] != 'attempt_cpu_limit', evidence
        assert attempt['cpu_seconds'] <= 4, evidence
        assert attempt['peak_rss_bytes'] < 8 * 1024**3
        assert 'FAULT_CONTROL_PAYLOAD' not in json.dumps(held)
        assert not group_exists(worker['pgid'])
        if attempt['accounting_complete']:
            assert attempt['stopped'] is True
            assert attempt['completion_evidence'] == 'process_group_reaped'
        else:
            assert held['usage']['accounting_unknown'] is True
            assert held['active_capacity_reserved'] is True
        assert client.result(job_id)['publications'] == []
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        cleanup_owned_group(worker)


# Public fixture calibration only, bounded to six fresh-database trials by plan34.
# Recalibrated for the bounded publication-base record; public pressure band stays fixed.
METADATA_FIXTURE_BODY_BYTES = 1_914_865


def test_metadata_headroom_refuses_controlled_worker_before_claim_or_launch(database, monkeypatch):
    from dataclasses import replace
    from nilaris.results import Effect

    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0),
                   ControlledSource({'P': ('r1', b'x' * 1048576)}))
    base = literal_output()
    effects = tuple(Effect('observation', ordinal,
        dict(base.effects[0].payload, id='o' + str(ordinal),
             locator=dict(page=1, text='controlled part ' + str(ordinal)),
             original_content='A' * METADATA_FIXTURE_BODY_BYTES if ordinal == 0 else 'short literal'))
        for ordinal in range(320))
    partial = replace(base, outcome='partial', effects=effects,
                      coverage=dict(inspected=['page:1'], unread=['page:2'], limits=['controlled-only']))
    published = client.publish(job_id, client.claim(job_id, client.status(job_id)['revision']), partial)
    assert published.get('state') == 'partial', dict(category=published.get('category'),
                                                   reason=published.get('reason'), usage=published.get('usage'))
    queued = client.retry(job_id, published['revision'], published['generation'],
                          'operator', 'finish controlled coverage')
    assert queued.get('state') == 'queued', queued
    retained = queued['usage']['retained_bytes']
    print('[metadata-boundary] ' + json.dumps(dict(body_bytes=METADATA_FIXTURE_BODY_BYTES,
        effects=320, retained_bytes=retained,
        serialized_result_bytes=queued['usage']['serialized_result_bytes'])))
    # Oracle is the approved 8MiB Job envelope; this narrow remaining band is
    # arranged through real public effects/history, not a lowered private cap.
    assert 8388608 - 8192 <= retained <= 8388608 - 4096
    assert queued['usage']['serialized_result_bytes'] < 2097152

    def full_result():
        page = client.result(job_id)
        if 'fragment' not in page:
            return page
        fragments = []
        for _ in range(256):
            fragments.append(page['fragment'])
            if page['next_cursor'] is None:
                return json.loads(''.join(fragments))
            page = client.result(job_id, page['next_cursor'])
        pytest.fail('bounded controlled result did not finish within 256 pages')

    before_result = full_result()
    actual_popen = subprocess.Popen
    launches = []

    def observe_launch(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if (isinstance(command, list) and len(command) > 3 and
                command[:2] == ['/bin/sh', '-c'] and command[3] == 'nilaris-controlled-bootstrap'):
            launches.append(process.pid)
        return process

    monkeypatch.setattr(subprocess, 'Popen', observe_launch)
    completion = replace(base, effects=(
        Effect('observation', 320, dict(base.effects[0].payload, id='o-final')),
        Effect('candidate', 0, dict(base.effects[1].payload, id='c-final', observation_ids=['o-final']))),
        coverage=dict(inspected=['page:1', 'page:2'], unread=[], limits=['controlled-only']))
    refused = client.run_controlled(job_id, queued['revision'], completion)
    assert refused.get('category') == 'resource_exhausted', refused
    assert launches == [], 'metadata refusal must precede actual bootstrap launch'
    after = client.status(job_id)
    for field in ('state', 'revision', 'generation', 'attempts', 'usage', 'counters',
                  'active_capacity_reserved', 'reservation_bytes', 'content_pin'):
        assert after[field] == queued[field], field
    assert full_result() == before_result
