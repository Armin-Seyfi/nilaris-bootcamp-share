"""Public guard CLI, external Docker transport, real du and OS file locks.

Only absolute task paths are relocated into an isolated copy. Docker transport
is controlled; guard control flow, subprocess results and du are not mocked.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python']


def wait_for(path, process):
    deadline = time.monotonic() + 8
    while not path.exists():
        assert process.poll() is None, 'external process exited before handshake'
        assert time.monotonic() < deadline, 'external process handshake expired'
        time.sleep(.005)


def isolated_guard(tmp_path):
    runtime = tmp_path / 'runtime/a1'
    package = tmp_path / 'src/nilaris'
    runtime.mkdir(parents=True)
    package.mkdir(parents=True)
    private = tmp_path / 'private'
    private.mkdir()
    for name in ('service_monitor.py', 'storage_coordination.py'):
        source = (ROOT / 'src/nilaris' / name).read_text()
        (package / name).write_text(source.replace('/private/tmp/nilaris-a1-setup-02', str(private)))
    source = Path(os.environ.get('A1_GUARD_SOURCE', ROOT / 'runtime/a1/service_guard.py')).read_text()
    # Relocate only the literal preflight ports, so this isolated transport test
    # also runs while the task's real services use their unchanged ports.
    assert source.count('ports=[55432] if mode == \'postgres\' else [55432,5679]') == 1
    with socket.socket() as first, socket.socket() as second:
        first.bind(('127.0.0.1', 0))
        second.bind(('127.0.0.1', 0))
        ports = first.getsockname()[1], second.getsockname()[1]
    source = source.replace("ports=[55432] if mode == 'postgres' else [55432,5679]",
                            f"ports={[ports[0]]} if mode == 'postgres' else {list(ports)}")
    guard = runtime / 'service_guard.py'
    guard.write_text(source.replace('/private/tmp/nilaris-a1-setup-02', str(private)))
    data = private / 'data'
    data.mkdir()
    (data / 'owned-database').mkdir()
    (data / 'owned-database/payload').write_bytes(b'x' * 4096)
    lock = private / 'postgres-storage.lock'
    lock.touch(mode=0o600)
    Path(str(lock) + '.turnstile').touch(mode=0o600)
    transport = tmp_path / 'bin'
    transport.mkdir()
    docker = transport / 'docker'
    docker.write_text('''#!/bin/sh
case "$*" in
  "ps -aq"*) exit 0;;
  *"up -d"*) exit 0;;
  *"ps -aq"*) printf 'postgres-id\\nn8n-id\\n';;
  *"inspect --size"*)
    if [ "$PROBE_MODE" = slow-inspect ]; then sleep 1.05; fi
    printf '0\\n0\\n';;
  *"exec -T postgres du"*)
    if [ "$PROBE_MODE" = du-relcache-error ]; then
      du -sk /var/lib/postgresql/18/docker/base/12345/pg_internal.init.6789
      exit "$?"
    fi
    if [ "$PROBE_MODE" = du-error ]; then
      printf 'generic-compose-secret-sentinel\\n'
      du -sk "$PROBE_PRIVATE/unknown-secret-path/12345"
      exit "$?"
    fi
    if [ "$PROBE_MODE" = slow ]; then sleep 1.05; fi
    scan_path="$PROBE_PRIVATE/data/owned-database"
    if [ -f "$PROBE_PRIVATE/removed" ]; then scan_path="$PROBE_PRIVATE/data"; fi
    touch "$PROBE_PRIVATE/scanning"
    printf 'scan\\n' >> "$PROBE_PRIVATE/scans"
    if [ "$PROBE_MODE" = coordinate ]; then
      while [ ! -f "$PROBE_PRIVATE/attempting" ]; do sleep .005; done
      sleep .1
    fi
    du -sk "$scan_path";;
  *"exec -T n8n du"*) du -sk "$PROBE_PRIVATE/data";;
  *"stop -t"*) touch "$PROBE_PRIVATE/stopped";;
  *) exit 2;;
esac
''')
    docker.chmod(0o700)
    env = dict(os.environ, PATH=str(transport) + os.pathsep + os.environ['PATH'],
               PROBE_PRIVATE=str(private), PYTHONPATH=str(tmp_path / 'src'))
    return guard, private, env


def launch(guard, env):
    return subprocess.Popen([*PYTHON, str(guard), '--seconds', '1'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_continuous_scans_admit_external_lifecycle_without_overlap(tmp_path):
    guard, private, env = isolated_guard(tmp_path)
    env['PROBE_MODE'] = 'coordinate'
    # A separate process requests the same exclusive lifecycle lock used by the
    # real fixture, then removes its exact owned directory using the OS.
    actor = subprocess.Popen([*PYTHON, '-c', '''
import os, pathlib, shutil, time
from nilaris.storage_coordination import storage_lock
p = pathlib.Path(os.environ['PROBE_PRIVATE'])
(p / 'actor-ready').touch()
while not (p / 'scanning').exists(): time.sleep(.005)
(p / 'attempting').touch()
with storage_lock(exclusive=True, deadline=time.monotonic()+.5):
    shutil.rmtree(p / 'data/owned-database')
(p / 'removed').touch()
'''], env=env, start_new_session=True)
    try:
        wait_for(private / 'actor-ready', actor)
        process = launch(guard, env)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, (stdout, stderr)
        actor.wait(timeout=5)
        assert actor.returncode == 0
        assert (private / 'removed').exists()
        assert len((private / 'scans').read_text().splitlines()) > 1
        assert (private / 'stopped').exists()
    finally:
        if actor.poll() is None:
            os.killpg(actor.pid, signal.SIGKILL)
            actor.wait()


@pytest.mark.parametrize('proof', ['missing', 'held', 'public', 'missing-turnstile', 'public-turnstile'])
def test_unavailable_coordination_proof_stops_without_scan(tmp_path, proof):
    guard, private, env = isolated_guard(tmp_path)
    env['PROBE_MODE'] = 'negative'
    lock = private / 'postgres-storage.lock'
    holder = None
    if proof == 'missing-turnstile':
        Path(str(lock) + '.turnstile').unlink()
    elif proof == 'public-turnstile':
        Path(str(lock) + '.turnstile').chmod(0o644)
    elif proof == 'missing':
        lock.unlink()
    elif proof == 'public':
        lock.chmod(0o644)
    else:
        holder = subprocess.Popen([*PYTHON, '-c', '''
import os, pathlib, time
from nilaris.storage_coordination import storage_lock
p = pathlib.Path(os.environ['PROBE_PRIVATE'])
with storage_lock(exclusive=True, deadline=time.monotonic()+1):
    (p / 'held').touch()
    time.sleep(5)
'''], env=env, start_new_session=True)
        wait_for(private / 'held', holder)
    try:
        started = time.monotonic()
        process = launch(guard, env)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode != 0, (stdout, stderr)
        assert time.monotonic() - started < 3
        assert (private / 'stopped').exists()
        assert not (private / 'scanning').exists()
        assert json.loads((private / 'service-monitor.json').read_text())['state'] == 'stopped'
    finally:
        if holder is not None:
            os.killpg(holder.pid, signal.SIGTERM)
            holder.wait()


@pytest.mark.parametrize('mode,slow_probe', [('slow', 'postgres_du'), ('slow-inspect', 'writable_layers')])
def test_slow_physical_probe_stops_with_bounded_phase_diagnostic(tmp_path, mode, slow_probe):
    guard, private, env = isolated_guard(tmp_path)
    env['PROBE_MODE'] = mode
    process = launch(guard, env)
    observed = set()
    deadline = time.monotonic() + 12
    while process.poll() is None:
        monitor = private / 'service-monitor.json'
        if monitor.exists():
            observed.add(json.loads(monitor.read_text())['state'])
        assert time.monotonic() < deadline
        time.sleep(.005)
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode != 0, (stdout, stderr)
    assert (private / 'stopped').exists()
    assert 'service storage monitor missed its one-second target' in stderr
    diagnostic = next(line for line in stderr.splitlines() if line.startswith('Storage cycle missed: '))
    assert len(diagnostic) < 160
    for field in ('lock_wait=', 'probes=', 'post_probe=', 'total='):
        assert field in diagnostic
    timings = [line for line in stderr.splitlines() if line.startswith('Storage cycle probe timings: ')]
    assert len(timings) == 1
    assert len(timings[0]) < 1600
    record = json.loads(timings[0].removeprefix('Storage cycle probe timings: '))
    assert record['cycle_start'] > 0
    assert record['executor_gap'] >= 0
    assert set(record['probes']) == {'postgres_du', 'n8n_du', 'writable_layers'}
    for probe in record['probes'].values():
        assert probe['queue'] >= 0
        assert probe['start'] >= 0
        assert probe['duration'] >= 0
        assert probe['end'] >= probe['start']
    assert record['probes'][slow_probe]['duration'] >= 1
    assert json.loads((private / 'service-monitor.json').read_text())['state'] == 'stopped'
    assert 'starting' in observed
    assert 'ready' not in observed


@pytest.mark.parametrize('mode,leaf_kind', [('du-error', 'other'), ('du-relcache-error', 'relcache_init_temp')])
def test_failed_physical_probe_reports_bounded_metadata_and_stops(tmp_path, mode, leaf_kind):
    guard, private, env = isolated_guard(tmp_path)
    env['PROBE_MODE'] = mode
    process = launch(guard, env)
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode != 0
    assert (private / 'stopped').exists()
    assert json.loads((private / 'service-monitor.json').read_text())['state'] == 'stopped'
    assert 'CalledProcessError' in stderr
    assert 'generic-compose-secret-sentinel' not in stdout + stderr
    assert 'unknown-secret-path' not in stdout + stderr
    records = [line for line in stderr.splitlines() if line.startswith('Physical storage probe failed: ')]
    assert len(records) == 1
    assert len(records[0]) < 3000
    record = json.loads(records[0].removeprefix('Physical storage probe failed: '))
    assert record['phase'] == 'du_failed'
    assert record['service'] == 'postgres' and record['exit'] != 0
    assert 0 <= record['elapsed'] <= 10
    assert record['started'] <= record['ended']
    assert record['output_chars'] > 0 and record['output_lines'] >= 1
    assert len(record['diagnostics']) == 1
    assert record['diagnostics'][0]['error'] == 'missing'
    assert record['diagnostics'][0]['leaf_kind'] == leaf_kind
    if mode == 'du-error':
        assert record['diagnostics'][0]['path'].endswith('/<redacted>/12345')
    else:
        assert record['diagnostics'][0]['path'] == '/var/lib/postgresql/18/docker/base/12345/<redacted>'
        assert 'pg_internal.init.6789' not in stdout + stderr


def test_unavailable_control_connection_does_not_block_physical_scan(tmp_path):
    """A real pending control connection cannot consume the scan lock budget."""
    guard, private, env = isolated_guard(tmp_path)
    env['PROBE_MODE'] = 'negative'
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))
        endpoint.listen(1)
        endpoint.settimeout(8)
        # Load native wheels only after Conda's interpreter starts. The copied
        # coordination package stays first, so the fixture uses the guard's lock.
        actor = subprocess.Popen([*PYTHON, '-c', '''
import os, pathlib, sys
sys.path.insert(0, '/private/tmp/nilaris-a1-setup-02/overlay')
sys.path.insert(0, os.environ['PROBE_SOURCE'])
sys.path.insert(0, os.environ['PYTHONPATH'])
from conftest import Database, database_command
try:
    database_command(Database(host='127.0.0.1', port=int(os.environ['PROBE_PORT']),
                              user='diagnostic_probe', password='harmless_probe',
                              connect_timeout=5), 'nilaris_test_diagnostic', 'CREATE')
except BaseException:
    pass
pathlib.Path(os.environ['PROBE_PRIVATE'], 'connection-failed').touch()
'''], env=dict(env, PROBE_SOURCE=str(ROOT / 'tests'),
              PROBE_PORT=str(endpoint.getsockname()[1])),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            connection, _ = endpoint.accept()
            with connection:
                process = launch(guard, env)
                stdout, stderr = process.communicate(timeout=12)
                assert 'storage coordination deadline reached' not in stderr, (
                    'pending control connection exhausted physical scan lock budget')
                assert process.returncode == 0, (stdout, stderr)
                assert (private / 'scanning').exists()
                assert actor.poll() is None, 'control connection must still be pending'
                assert (private / 'stopped').exists()
            actor.wait(timeout=8)
            assert actor.returncode == 0
            assert (private / 'connection-failed').exists()
        finally:
            if actor.poll() is None:
                os.killpg(actor.pid, signal.SIGKILL)
                actor.wait()
