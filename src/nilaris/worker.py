"""Supervised fixture and controlled-test process group with fenced publication."""
from dataclasses import replace
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.execution import claim, conflict, publish, retained_usage
from nilaris.results import Effect, StageOutput, canonical
from nilaris.worker_lifecycle import BOOTSTRAP, command as lifecycle_command, processes

ADAPTER = 'controlled-langgraph-langchain-v1'
PIPE_LIMIT = 2 * 1024**2
WORKER_METADATA_LIMIT = 8192


def persist_worker(connection, job_id, owned, metadata):
    """Charge actual bounded metadata growth under the job's admission lock."""
    size = len(canonical(metadata))
    if size > WORKER_METADATA_LIMIT:
        raise RuntimeError('worker metadata capacity')
    connection.execute('SELECT pg_advisory_xact_lock(714012001)')
    job = connection.execute('SELECT profile FROM admission_jobs WHERE id=%s', (job_id,)).fetchone()
    previous = connection.execute('SELECT worker FROM job_attempts WHERE id=%s AND token=%s',
                                  (owned['attempt_id'], owned['token'])).fetchone()
    if not job or not previous:
        raise RuntimeError('worker metadata capacity')
    growth = max(0, size - len(canonical(previous['worker'])))
    if retained_usage(connection, job_id)['retained_bytes'] + growth > job['profile']['job_bytes']:
        raise RuntimeError('worker metadata capacity')
    connection.execute('UPDATE job_attempts SET worker=%s WHERE id=%s AND token=%s',
                       (Jsonb(metadata), owned['attempt_id'], owned['token']))


def heartbeat(ingestion, job_id, owned):
    with psycopg.connect(**ingestion.database) as connection:
        updated = connection.execute('''UPDATE job_attempts a SET heartbeat_at=clock_timestamp(),
            expires_at=clock_timestamp()+interval '10 seconds' FROM admission_jobs j
            WHERE a.id=%s AND a.token=%s AND a.job_id=j.id AND j.id=%s
            AND a.generation=j.generation AND j.generation=%s AND j.state='running'
            AND NOT a.stopped AND a.expires_at>clock_timestamp() RETURNING a.id''',
            (owned['attempt_id'], owned['token'], job_id, owned['generation'])).fetchone()
    return bool(updated)


def stop_owned(process, identity, observed, reaped):
    """Best-effort bounded cleanup; uncertainty never becomes completion evidence."""
    deadline = time.monotonic() + 4
    term_at = None
    final_usage = None
    while time.monotonic() < deadline:
        group = processes(process.pid)
        if not reaped:
            pid, code, final_usage = os.wait4(process.pid, os.WNOHANG)
            if pid:
                reaped = True
                process.returncode = os.waitstatus_to_exitcode(code)
                # The inventory preceded wait4: it may contain the zombie just
                # reaped here. Require a fresh sample before absence or signals.
                continue
        if reaped and not group:
            return True, final_usage
        leader = next((m for m in group if m['pid'] == process.pid), None)
        safe = (leader and identity and leader['start'] == identity['start']) or (
            reaped and group and all(observed.get(m['pid']) == m['start'] for m in group))
        if not safe:
            return False, None
        try:
            if term_at is None:
                term_at = time.monotonic()
                os.killpg(process.pid, signal.SIGTERM)
            elif time.monotonic() - term_at >= 2:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # An independent stop can race the sample. ESRCH is not proof:
            # retain the deadline and require fresh inventory plus real reaping.
            pass
        time.sleep(0.1)
    return False, None


def run_controlled(ingestion, job_id, expected_revision, output, *, test_wall_seconds, test_delay_seconds,
                   test_cpu_seconds=20, test_rss_bytes=1024**3, test_workload='literal'):
    if (not isinstance(output, StageOutput) or type(test_wall_seconds) not in (int, float) or
            not 0 < test_wall_seconds <= 30 or type(test_delay_seconds) not in (int, float) or
            not 0 <= test_delay_seconds <= 30 or type(test_cpu_seconds) not in (int, float) or
            not 0 < test_cpu_seconds <= 20 or type(test_rss_bytes) is not int or
            not 0 < test_rss_bytes <= 1024**3 or test_workload not in ('literal', 'cpu', 'rss', 'output')):
        return conflict(job_id, 'invalid_controlled_stage')
    try:
        snapshot = replace(output, adapter=ADAPTER).snapshot()
        request = canonical(dict(output=snapshot, delay=test_delay_seconds,
                                 workload=test_workload)).encode()
    except (ValueError, TypeError, RecursionError):
        return conflict(job_id, 'malformed_output')
    if len(request) > PIPE_LIMIT:
        return conflict(job_id, 'resource_exhausted')
    return _run_request(ingestion, job_id, expected_revision, request,
                        test_wall_seconds=test_wall_seconds, test_cpu_seconds=test_cpu_seconds,
                        test_rss_bytes=test_rss_bytes, test_workload=test_workload, adapter=ADAPTER)


def run_fixture(ingestion, job_id, expected_revision):
    prepared = prepare_fixture(ingestion, job_id, expected_revision)
    if not prepared.get('prepared'):
        return prepared
    return run_prepared_fixture(ingestion, job_id, prepared)


def prepare_fixture(ingestion, job_id, expected_revision):
    """Validate retained input/budgets and durably claim before acknowledging.

    The returned preparation stays within the listener process; claim tokens
    and retained bytes are never included in the command response.
    """
    """Only persisted captured input can enter the native fixture processor."""
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        job = connection.execute('''SELECT input,captured_input,content_pin,profile
            FROM admission_jobs WHERE id=%s AND input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s''',
            (job_id,ingestion.scoped_binding)).fetchone()
    if not job:
        return conflict(job_id, 'not_found')
    if job['input'].get('processing_contract') == 'semantic172-v1':
        return conflict(job_id, 'semantic172_explicit_runner_required')
    content = job['captured_input']
    if (content is None or len(content) > job['profile']['input_bytes'] or
            hashlib.sha256(content).hexdigest() != job['content_pin']):
        return conflict(job_id, 'invalid_captured_input')
    request = canonical(dict(operation='fixture', fixture=dict(
        content=base64.b64encode(content).decode('ascii'), content_pin=job['content_pin'],
        media_type=job['input']['media_type'], source_revision=job['input']['source_revision'],
        document_id=job['input']['document_id']))).encode()
    if len(request) > PIPE_LIMIT:
        return conflict(job_id, 'resource_exhausted')
    prepared = _prepare_request(ingestion, job_id, expected_revision)
    if prepared.get('prepared'):
        prepared['request'] = request
    return prepared


def run_prepared_fixture(ingestion, job_id, prepared):
    """Run the original durable processing claim, without claiming again."""
    return _run_claimed(ingestion, job_id, prepared['request'],
                        prepared['before'], prepared['owned'],
                        test_wall_seconds=30, test_cpu_seconds=20, test_rss_bytes=1024**3,
                        test_workload='fixture', adapter='fixture-native-v1')


def _run_request(ingestion, job_id, expected_revision, request, *, test_wall_seconds,
                 test_cpu_seconds, test_rss_bytes, test_workload, adapter, semantic=None):
    if semantic is None and ingestion.status(job_id).get('input', {}).get('processing_contract') == 'semantic172-v1':
        return conflict(job_id, 'semantic172_explicit_runner_required')
    prepared = _prepare_request(ingestion, job_id, expected_revision)
    if not prepared.get('prepared'):
        return prepared
    return _run_claimed(ingestion, job_id, request, prepared['before'], prepared['owned'],
                        test_wall_seconds=test_wall_seconds, test_cpu_seconds=test_cpu_seconds,
                        test_rss_bytes=test_rss_bytes, test_workload=test_workload, adapter=adapter, semantic=semantic)


def _prepare_request(ingestion, job_id, expected_revision):
    # Only captured processing is eligible. Do not spend an acquisition claim.
    before = ingestion.status(job_id)
    if before.get('state') != 'queued':
        return conflict(job_id)
    # The whole job already owns its 8MiB admission reservation. Check finite
    # sub-budgets before claiming or launching; later writes recheck under lock.
    if (before['usage']['retained_bytes'] + 4096 + WORKER_METADATA_LIMIT + 8192 >
            before['profile']['job_bytes']):
        return conflict(job_id, 'resource_exhausted')
    owned = claim(ingestion, job_id, expected_revision)
    if not owned.get('claimed'):
        return owned
    return dict(prepared=True, before=before, owned=owned)


def _run_claimed(ingestion, job_id, request, before, owned, *, test_wall_seconds,
                 test_cpu_seconds, test_rss_bytes, test_workload, adapter, semantic=None):
    wall_left = min(test_wall_seconds, 120 - sum(a['wall_seconds'] for a in before['attempts']))
    if semantic is not None:
        from nilaris.semantic172 import Ledger, PROFILE, batch_remaining
        if (semantic.get('profile') != PROFILE or test_wall_seconds != 105 or
                test_cpu_seconds != 20 or test_rss_bytes != 1024**3):
            raise ValueError('invalid semantic172 supervision profile')
        wall_left = min(wall_left, batch_remaining(Ledger(semantic['artifact_root'], mode=semantic['mode'])))
    cpu_left = min(test_cpu_seconds, 60 - sum(a['cpu_seconds'] for a in before['attempts']))
    root = Path(__file__).resolve().parents[2]
    started = time.monotonic()
    command = ['/bin/sh', '-c', BOOTSTRAP, 'nilaris-controlled-bootstrap', str(wall_left),
               str(wall_left + 2),
               *lifecycle_command(root, 'nilaris.worker_lifecycle')]
    environment = dict(os.environ, NILARIS_CONTROLLED_DEADLINE=str(started + wall_left),
                       NILARIS_CONTROLLED_CPU=str(cpu_left),
                       NILARIS_CONTROLLED_RSS=str(test_rss_bytes))
    process = None
    identity = None
    reason = None
    data = bytearray()
    observed_output_bytes = 0
    overflow = threading.Event()
    reaped = False
    usage = None
    peak_rss = 0
    cpu = 0
    observed = {}
    phase = 'launch'
    failure = None
    reader = writer = control_reader = None
    control_record = {}
    ready_header = {}
    header_received = threading.Event()
    ready = threading.Event()
    bootstrap_error = threading.Event()
    input_cancelled = threading.Event()
    input_sent = threading.Event()

    def collect_control():
        if control_reader is not None:
            control_reader.join(timeout=1)
        monitor = worker.get('independent_monitor')
        if (control_record and monitor and control_record['pid'] == monitor['pid'] and
                control_record['pgid'] == monitor['pgid'] and control_record['start'] == monitor['start']):
            # Only this durably verified actual monitor may supply measurements.
            # Reaping/pipe proof and threshold validation remain separate gates.
            worker['control'] = dict(control_record)

    def record_accounting():
        # Both normal and recovered completion require actual absence/reaping.
        # Charge the full effective allowance; cleanup never refunds capacity.
        worker['output_bytes_observed'] = observed_output_bytes
        with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
            persist_worker(connection, job_id, owned, worker)
            connection.execute('''UPDATE job_attempts SET accounting_complete=true,
                completion_evidence='process_group_reaped',wall_seconds=%s,cpu_seconds=%s,
                measured_cpu_seconds=%s,cpu_accounting='conservative_full_allowance',peak_rss_bytes=%s
                WHERE id=%s AND token=%s''', (time.monotonic() - started, max(cpu_left, cpu),
                                            cpu, peak_rss, owned['attempt_id'], owned['token']))

    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   env=environment)
        group = processes(process.pid)
        identity = next(member for member in group if member['pid'] == process.pid)
        observed[identity['pid']] = identity['start']
        worker = dict(pid=process.pid, pgid=process.pid, start=identity['start'], adapter=adapter,
                      controlled_workload=test_workload,
                      output_bytes_observed=0,
                      bootstrap_readiness='pending', members=group[:16], members_truncated=len(group) > 16,
                      limits=dict(version='semantic172-worker-v1' if semantic is not None else 'L1-worker-v1' if (test_wall_seconds == 30 and test_cpu_seconds == 20
                                                            and test_rss_bytes == 1024**3)
                                  else 'controlled-worker-lower-test-v1',
                                  attempt_wall_seconds=test_wall_seconds, remaining_wall_seconds=wall_left,
                                  remaining_cpu_seconds=cpu_left, rss_bytes=test_rss_bytes,
                                  output_bytes=PIPE_LIMIT,
                                  lease_seconds=10, heartbeat_seconds=2, watchdog_poll_ms=100,
                                  termination_grace_seconds=2,
                                  wall_enforcement='independent_bootstrap_and_worker_timers',
                                  resource_enforcement='independent_sampled_group_policy',
                                  aggregate_native_hard_limit=False))
        with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
            persist_worker(connection, job_id, owned, worker)

        def read_output():
            nonlocal observed_output_bytes
            try:
                header = process.stdout.readline(4097)
                observed_output_bytes += len(header)
                if len(header) > 4096 or not header.endswith(b'\n'):
                    raise ValueError('invalid readiness header')
                received = json.loads(header)
                if (not isinstance(received, dict) or set(received) != {'pid', 'pgid', 'monitor_pid'} or
                        any(type(value) is not int or value <= 0 for value in received.values()) or
                        received['pgid'] != process.pid):
                    raise ValueError('invalid readiness identity')
                ready_header.update(received)
                header_received.set()
            except (ValueError, OSError):
                bootstrap_error.set()
                return
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                observed_output_bytes += len(chunk)
                if observed_output_bytes > PIPE_LIMIT:
                    overflow.set()
                    return
                data.extend(chunk)

        def write_input():
            try:
                while not ready.wait(timeout=.05):
                    if (bootstrap_error.is_set() or input_cancelled.is_set() or
                            time.monotonic() >= started + wall_left + 2):
                        return
                process.stdin.write(request)
                process.stdin.close()
                input_sent.set()
            except (BrokenPipeError, OSError):
                pass

        def read_control():
            # Never persist dependency stderr. The entire control buffer is
            # bounded, and only an exact enumerated identity record is accepted.
            try:
                raw = process.stderr.read(4096)
                line = raw.split(b'\n', 1)[0]
                prefix = b'NILARIS_WORKER_STOP '
                if not line.startswith(prefix) or b'\n' not in raw:
                    return
                receipt = json.loads(line[len(prefix):])
                if (not isinstance(receipt, dict) or set(receipt) != {'reason', 'pid', 'pgid', 'start', 'cpu', 'rss'} or
                        receipt['reason'] not in ('attempt_wall_limit', 'attempt_cpu_limit', 'worker_rss_limit') or
                        any(type(receipt[k]) is not int or receipt[k] <= 0 for k in ('pid', 'pgid')) or
                        not isinstance(receipt['start'], str) or len(receipt['start']) > 80 or
                        type(receipt['cpu']) not in (int, float) or not math.isfinite(receipt['cpu']) or receipt['cpu'] < 0 or
                        type(receipt['rss']) is not int or receipt['rss'] < 0):
                    return
                control_record.update(receipt)
            except (ValueError, OSError, OverflowError):
                return

        reader = threading.Thread(target=read_output, daemon=True)
        writer = threading.Thread(target=write_input, daemon=True)
        control_reader = threading.Thread(target=read_control, daemon=True)
        reader.start()
        writer.start()
        control_reader.start()
        next_heartbeat = time.monotonic()
        termination_at = None
        while True:
            now = time.monotonic()
            dispatch_pending = False
            phase = 'process_inventory'
            group = processes(process.pid)
            for member in group:
                if member['pid'] in observed and observed[member['pid']] != member['start']:
                    raise RuntimeError('process identity changed')
                if reaped and member['pid'] not in observed:
                    raise RuntimeError('unrecognized process after leader exit')
                observed[member['pid']] = member['start']
            cpu = max(cpu, sum(member['cpu'] for member in group))
            peak_rss = max(peak_rss, sum(member['rss'] for member in group))
            if (reason is None and header_received.is_set() and not ready.is_set() and
                    now - started < wall_left):
                stage = next((m for m in group if m['pid'] == ready_header['pid']), None)
                monitor = next((m for m in group if m['pid'] == ready_header['monitor_pid']), None)
                if stage is None or monitor is None:
                    raise RuntimeError('bootstrap identity uncertain')
                worker.update(bootstrap_readiness='verified', stage=stage,
                              independent_monitor=monitor, members=group[:16],
                              members_truncated=len(group) > 16,
                              output_bytes_observed=observed_output_bytes)
                with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
                    persist_worker(connection, job_id, owned, worker)
                if semantic is not None:
                    from nilaris.semantic172 import Ledger
                    sink = Ledger(semantic['artifact_root'], mode=semantic['mode'])
                    number = sink.reserve(semantic['target_document_id'])
                    semantic['attempt'] = number
                    request = canonical(dict(operation='semantic172-v1', semantic=semantic)).encode()
                    # Reservation/source verification can block past the lease.
                    # Refresh measurements and force a fenced heartbeat before
                    # allowing any request bytes into the child.
                    batch_clock = time.monotonic()
                    dispatch_deadline = min(started + wall_left, batch_clock + batch_remaining(sink))
                    group = processes(process.pid)
                    for member in group:
                        if member['pid'] not in observed or observed[member['pid']] != member['start']:
                            raise RuntimeError('process identity changed')
                    cpu = max(cpu, sum(member['cpu'] for member in group))
                    peak_rss = max(peak_rss, sum(member['rss'] for member in group))
                    now = time.monotonic()
                    next_heartbeat = 0
                    dispatch_pending = True
                else:
                    ready.set()
            if input_sent.is_set() and worker['bootstrap_readiness'] == 'verified':
                worker['bootstrap_readiness'] = 'input_dispatched'
                worker['output_bytes_observed'] = observed_output_bytes
                with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
                    persist_worker(connection, job_id, owned, worker)
            if not reaped:
                phase = 'reap'
                pid, code, usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    reaped = True
                    process.returncode = os.waitstatus_to_exitcode(code)
                    cpu = max(cpu, usage.ru_utime + usage.ru_stime)
            if reaped and not group:
                input_cancelled.set()
                break
            if reason is None:
                if now - started >= wall_left:
                    reason = 'attempt_wall_limit'
                elif cpu >= cpu_left:
                    reason = 'attempt_cpu_limit'
                elif peak_rss >= test_rss_bytes:
                    reason = 'worker_rss_limit'
                elif overflow.is_set():
                    reason = 'worker_output_limit'
                elif bootstrap_error.is_set():
                    reason = 'controlled_worker_failed'
                elif now >= next_heartbeat:
                    phase = 'heartbeat'
                    heartbeat_started = time.monotonic()
                    if not heartbeat(ingestion, job_id, owned):
                        reason = 'claim_lost'
                    now = time.monotonic()
                    next_heartbeat = now + 2
                    if dispatch_pending and now - heartbeat_started >= 10:
                        reason = 'claim_lost'
            if dispatch_pending and reason is None:
                now = time.monotonic()
                if now >= dispatch_deadline:
                    reason = 'attempt_wall_limit'
                else:
                    ready.set()
            if reason:
                phase = 'termination'
                # A same-numbered unrelated process must never receive a signal.
                leader = next((m for m in group if m['pid'] == process.pid), None)
                safe = (leader and leader['start'] == identity['start']) or (
                    reaped and all(observed.get(m['pid']) == m['start'] for m in group))
                if not safe:
                    raise RuntimeError('process identity uncertain')
                try:
                    if termination_at is None:
                        termination_at = now
                        os.killpg(process.pid, signal.SIGTERM)
                    elif now - termination_at >= 2:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    # An independent deadline can empty the group after this
                    # inventory. ESRCH proves neither identity nor accounting:
                    # repeat fresh inventory and actual wait4, then require the
                    # usual completed pipes before recording any completion.
                    # Keep the termination deadline and normal poll below.
                    pass
                if now - termination_at > 5:
                    raise RuntimeError('termination accounting uncertain')
            time.sleep(max(0, 0.1 - (time.monotonic() - now)))
        phase = 'pipe_completion'
        reader.join(timeout=1)
        writer.join(timeout=1)
        collect_control()
        if reader.is_alive() or writer.is_alive() or control_reader.is_alive():
            raise RuntimeError('pipe accounting uncertain')
        control = worker.get('control')
        if control:
            cpu = max(cpu, control['cpu'])
            peak_rss = max(peak_rss, control['rss'])
            if reason is None:
                if control['reason'] == 'attempt_cpu_limit' and control['cpu'] >= cpu_left:
                    reason = 'attempt_cpu_limit'
                elif control['reason'] == 'worker_rss_limit' and control['rss'] >= test_rss_bytes:
                    reason = 'worker_rss_limit'
                elif (control['reason'] == 'attempt_wall_limit' and
                      time.monotonic() - started >= wall_left):
                    reason = 'attempt_wall_limit'
        wall = time.monotonic() - started
        if reason is None:
            if wall >= wall_left:
                reason = 'attempt_wall_limit'
            elif cpu >= cpu_left:
                reason = 'attempt_cpu_limit'
            elif peak_rss >= test_rss_bytes:
                reason = 'worker_rss_limit'
        if process.returncode != 0 and reason is None:
            reason = 'controlled_worker_failed'
        # wait4(wrapper) and group samples are measurements, not a proof of exact
        # aggregate descendant CPU after a group stop. Charge the full effective
        # allowance on every controlled attempt; never recover it on retry.
        phase = 'accounting_commit'
        record_accounting()
        if reason is None and heartbeat(ingestion, job_id, owned):
            phase = 'publication'
            response = json.loads(data)
            if semantic is not None:
                from nilaris.semantic172 import Ledger, artifact_charge, parent_outcome, digest
                from nilaris.semantic_contract import validate
                receipt = response.get('semantic_receipt')
                sink = Ledger(semantic['artifact_root'], mode=semantic['mode'])
                if digest(sink.verify()) != semantic['freeze_pin']:
                    raise ValueError('frozen experiment mutation')
                result = sink.read(f"result-{semantic['attempt']:02}.json")
                if (set(response) != {'semantic_receipt'} or
                        receipt != dict(attempt=semantic['attempt'], status=result['status']) or
                        result['target_document_id'] != semantic['target_document_id']):
                    raise ValueError('invalid semantic receipt')
                completed = result['status'] == 'completed'
                if completed:
                    validate(result['initial_output'], sink.read('capture.json'),
                             semantic['target_document_id'])
                charge = artifact_charge(sink, semantic['attempt'])
                with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
                    connection.execute('SELECT pg_advisory_xact_lock(714012001)')
                    profile = connection.execute('SELECT profile FROM admission_jobs WHERE id=%s', (job_id,)).fetchone()['profile']
                    proposed = dict(worker, semantic_artifact_bytes=charge['bytes'],
                                    semantic_artifact_inventory_sha256=charge['inventory_sha256'])
                    growth = max(0,len(canonical(proposed))-len(canonical(worker)))
                    if retained_usage(connection, job_id)['retained_bytes'] + charge['bytes'] + growth > profile['job_bytes']:
                        raise RuntimeError('semantic artifact capacity')
                    persist_worker(connection, job_id, owned, proposed)
                    worker = proposed
                    changed = connection.execute('''UPDATE admission_jobs j SET state=%s,
                        reason=%s,revision=revision+1,generation=generation+1,
                        held_active_reservation=%s WHERE j.id=%s AND j.state='running'
                        AND j.generation=%s AND EXISTS(SELECT 1 FROM job_attempts a
                            WHERE a.id=%s AND a.token=%s AND a.job_id=j.id
                            AND a.generation=j.generation AND NOT a.stopped
                            AND a.expires_at>clock_timestamp()) RETURNING j.id''',
                        ('completed' if completed else 'held',
                         'semantic_completed' if completed else 'semantic_'+result['status'], not completed,
                         job_id, owned['generation'], owned['attempt_id'], owned['token'])).fetchone()
                    if changed and completed:
                        connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                            (retained_usage(connection, job_id)['retained_bytes'],job_id))
                    if changed:
                        connection.execute('UPDATE job_attempts SET stopped=true,reason=%s WHERE id=%s AND token=%s',
                            ('semantic_completed' if completed else 'semantic_'+result['status'], owned['attempt_id'], owned['token']))
                if changed:
                    parent_outcome(sink, semantic['attempt'], 'completed' if completed else 'semantic_'+result['status'])
                    return ingestion.status(job_id)
                reason = 'claim_lost'
                raise RuntimeError('semantic claim lost')
            result = StageOutput(response['outcome'], response['coverage'],
                                 tuple(Effect(e['kind'], e['ordinal'], e['payload']) for e in response['effects']),
                                 response['stage_contract'], response['adapter'])
            response = publish(ingestion, job_id, owned, result)
            if response.get('state') in ('completed', 'partial'):
                return response
            reason = response.get('category', 'publication_held')
        reason = reason or 'claim_lost'
    except Exception as error:
        input_cancelled.set()
        # Unknown state remains charged and blocks further claims. No marker is
        # substituted for actual process absence or unavailable resource totals.
        reason = 'worker_accounting_unknown'
        failure = dict(phase=phase, exception_type=type(error).__name__[:80])
        local_errors = {'process identity changed', 'unrecognized process after leader exit',
                        'process identity uncertain', 'termination accounting uncertain',
                        'pipe accounting uncertain', 'bootstrap identity uncertain', 'worker metadata capacity'}
        if type(error) is RuntimeError and error.args and error.args[0] in local_errors:
            failure['control_error'] = error.args[0]
        if process is not None:
            try:
                reaped, cleanup_usage = stop_owned(process, identity, observed, reaped)
                # Preserve observed usage even on cleanup. If the original wait4
                # totals were lost, the claim stays charged for reconciliation.
                if cleanup_usage:
                    cpu = max(cpu, cleanup_usage.ru_utime + cleanup_usage.ru_stime)
                # SIGSTOP can outlast ps's deadline even when ps completed.
                # Its old snapshot is unusable. Only fresh cleanup evidence,
                # measured reaping and completed pipes can recover accounting.
                if (phase == 'process_inventory' and isinstance(error, subprocess.TimeoutExpired)
                        and reaped and (cleanup_usage is not None or usage is not None)
                        and reader is not None and writer is not None):
                    reader.join(timeout=1)
                    writer.join(timeout=1)
                    collect_control()
                    if (not reader.is_alive() and not writer.is_alive()
                            and control_reader is not None and not control_reader.is_alive()):
                        control = worker.get('control')
                        recovered_reason = None
                        if not heartbeat(ingestion, job_id, owned):
                            recovered_reason = 'claim_lost'
                        elif control:
                            if control['reason'] == 'attempt_cpu_limit' and control['cpu'] >= cpu_left:
                                recovered_reason = 'attempt_cpu_limit'
                            elif control['reason'] == 'worker_rss_limit' and control['rss'] >= test_rss_bytes:
                                recovered_reason = 'worker_rss_limit'
                            elif (control['reason'] == 'attempt_wall_limit' and
                                  time.monotonic() - started >= wall_left):
                                recovered_reason = 'attempt_wall_limit'
                        if (recovered_reason is None and overflow.is_set()
                                and observed_output_bytes > PIPE_LIMIT):
                            # The stdout reader owns the byte measurement; a
                            # monitor receipt cannot attest output overflow.
                            # Fresh absence, reaping/rusage, all completed pipes
                            # and the live claim were checked above.
                            recovered_reason = 'worker_output_limit'
                        if recovered_reason:
                            if control:
                                cpu = max(cpu, control['cpu'])
                                peak_rss = max(peak_rss, control['rss'])
                            record_accounting()
                            reason = recovered_reason
            except Exception:
                pass
        if process is not None and identity is not None:
            collect_control()
    finally:
        if process is not None:
            # Pipes close only after normal reaping. An unknown live worker keeps
            # its slot; reconciliation must establish identity before termination.
            if reaped:
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()
    if semantic is not None and 'attempt' in semantic:
        from nilaris.semantic172 import Ledger, parent_outcome, artifact_charge
        sink = Ledger(semantic['artifact_root'], mode=semantic['mode'])
        parent_outcome(sink, semantic['attempt'], reason)
        charge = artifact_charge(sink, semantic['attempt'])
        proposed = dict(worker, semantic_artifact_bytes=charge['bytes'],
                        semantic_artifact_inventory_sha256=charge['inventory_sha256'])
        with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
            connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            profile = connection.execute('SELECT profile FROM admission_jobs WHERE id=%s', (job_id,)).fetchone()['profile']
            growth = max(0, len(canonical(proposed))-len(canonical(worker)))
            extra_charge = max(0, charge['bytes']-worker.get('semantic_artifact_bytes', 0))
            if retained_usage(connection, job_id)['retained_bytes'] + extra_charge + growth > profile['job_bytes']:
                reason = 'semantic_artifact_capacity'
            else:
                persist_worker(connection, job_id, owned, proposed)
                worker = proposed
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        connection.execute('''UPDATE admission_jobs SET state='held',held_active_reservation=true,reason=%s,revision=revision+1,
            generation=generation+1 WHERE id=%s AND generation=%s''', (reason, job_id, owned['generation']))
        connection.execute('''UPDATE job_attempts SET reason=%s,stopped=accounting_complete,
            wall_seconds=greatest(wall_seconds,%s),cpu_seconds=greatest(cpu_seconds,%s),
            measured_cpu_seconds=greatest(measured_cpu_seconds,%s),
            peak_rss_bytes=greatest(peak_rss_bytes,%s) WHERE id=%s AND token=%s''',
            (reason, time.monotonic() - started, cpu, cpu, peak_rss, owned['attempt_id'], owned['token']))
    if failure or control_record:
        if process is not None and identity is not None:
            worker['output_bytes_observed'] = observed_output_bytes
            if failure:
                worker['failure'] = failure
            try:
                with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
                    persist_worker(connection, job_id, owned, worker)
            except RuntimeError as error:
                if error.args != ('worker metadata capacity',):
                    raise
                # Capacity uncertainty is already held with its reservation;
                # never grow durable metadata past the owned job's envelope.
                pass
    return ingestion.status(job_id)
