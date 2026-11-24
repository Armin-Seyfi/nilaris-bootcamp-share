"""Public subprocess command seam with real task-owned PostgreSQL."""
from dataclasses import asdict
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import select
import socket
import socketserver
import subprocess
import threading
import time

import pytest

from nilaris.admission import AdmissionProfile, migrate
from test_admission import member

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris',
          'python', str(ROOT / 'runtime/a1/with_overlay.py')]


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


@pytest.fixture
def command_process(database, tmp_path):
    migrate(database)
    config = tmp_path / 'command.json'
    config.write_text(json.dumps(dict(database=database, token='synthetic-test-token',
                                     admission_profile=asdict(AdmissionProfile.l1()))))
    config.chmod(0o600)
    processes = []

    def client(command):
        result = subprocess.run([*PYTHON, '-m', 'nilaris.command_service', 'client',
                                 '--config', str(config)], input=json.dumps(command),
                                text=True, capture_output=True, cwd=ROOT, timeout=15)
        assert result.returncode == 0, 'text client failed; diagnostics withheld'
        return json.loads(result.stdout)

    def start():
        process = subprocess.Popen([*PYTHON, '-m', 'nilaris.command_service', 'serve',
                                    '--config', str(config)], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        processes.append(process)
        for _ in range(100):
            if process.poll() is not None:
                break
            status = client(dict(operation='status', job_id='absent'))
            if status.get('category') == 'not_found':
                listener = int(subprocess.check_output(
                    ['lsof', '-t', '-nP', '-iTCP:18765', '-sTCP:LISTEN'], text=True).strip())
                assert os.getpgid(listener) == process.pid, 'listener is not owned by test process group'
                print(f'task transport group={process.pid} listener={listener}', flush=True)
                return process
            time.sleep(0.05)
        raise AssertionError('command service did not become available')

    try:
        yield client, start, config
    finally:
        for process in processes:
            stop(process)


def test_text_client_recovers_acknowledged_job_after_service_restart(command_process):
    client, start, _ = command_process
    process = start()
    offered = client(dict(operation='offer', namespace='operator', command_id='restart', members=[member()]))
    assert offered['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    job_id = offered['members'][0]['job_id']
    stop(process)
    assert client(dict(operation='status', job_id=job_id))['category'] == 'transport_unavailable'
    start()
    assert client(dict(operation='resolve', namespace='operator', command_id='restart')) == offered
    status = client(dict(operation='status', job_id=job_id))
    assert status['input']['source_revision'] == 'r1'
    assert status['coverage'] == dict(acquisition='pending', processing='not_started')
    assert status['counters'] == dict(attempts=0, captured_input_bytes=0, acquisition_requests=0)
    assert status['admission']['job_reservation_bytes'] == 8388608


def test_near_limit_accepted_metadata_remains_queryable_across_restart(command_process):
    client, start, _ = command_process
    process = start()
    item = member()
    item['document_id'] = 'd' * 65000
    command = dict(operation='offer', namespace='operator', command_id='near-limit', members=[item])
    assert 65000 < len(json.dumps(command).encode()) <= 65536
    offered = client(command)
    assert offered['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    job_id = offered['members'][0]['job_id']
    for restarted in (False, True):
        if restarted:
            stop(process)
            process = start()
        assert client(dict(operation='resolve', namespace='operator', command_id='near-limit')) == offered
        status = client(dict(operation='status', job_id=job_id))
        assert status.get('known') is True, 'accepted near-limit job status must remain available'
        assert status['id'] == job_id
        assert status['input'] == item
        assert status['state'] == 'acquiring'
        assert status['profile'] == asdict(AdmissionProfile.l1())
        assert status['coverage'] == dict(acquisition='pending', processing='not_started')
        assert status['counters'] == dict(attempts=0, captured_input_bytes=0, acquisition_requests=0)
        assert status['admission']['waiting_jobs'] == 1
        assert status['admission']['job_reservation_bytes'] == 8388608
        assert 135168 < status['admission']['receipt_reservation_bytes'] <= 204800
        assert status['next_action'] == 'await_pinned_acquisition'
        print('near-limit public status: restart=' + str(restarted) + ' known=true '
              'identity/pin/profile/counters preserved next=await_pinned_acquisition', flush=True)


def test_lost_response_resolves_canonical_receipt_and_replay(command_process):
    client, start, _ = command_process
    start()
    command = dict(operation='offer', namespace='operator', command_id='lost', members=[member()])
    connection = HTTPConnection('127.0.0.1', 18765, timeout=3)
    connection.request('POST', '/command', json.dumps(command),
                       {'Authorization': 'Bearer synthetic-test-token'})
    connection.close()  # Deliberately discard the entire response, including headers.
    resolved = client(dict(operation='resolve', namespace='operator', command_id='lost'))
    assert resolved['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    assert client(command) == resolved
    status = client(dict(operation='status', job_id=resolved['members'][0]['job_id']))
    assert status['admission']['waiting_jobs'] == 1
    assert status['admission']['job_reservation_bytes'] == 8388608
    assert status['admission']['receipt_reservation_bytes'] == 135168
    command['members'][0]['source_revision'] = 'changed'
    assert client(command)['category'] == 'command_conflict'


def test_command_service_authentication_whitelist_and_bounded_body(command_process):
    client, start, _ = command_process
    start()
    for body, authorization, expected in [
            (b'{}', 'Bearer wrong-token', 'unauthorized'),
            (json.dumps(dict(operation='migrate')).encode(), 'Bearer synthetic-test-token', 'invalid_request'),
            (b'x' * 65537, 'Bearer synthetic-test-token', 'invalid_request')]:
        connection = HTTPConnection('127.0.0.1', 18765, timeout=3)
        try:
            connection.request('POST', '/command', body, {'Authorization': authorization})
            result = json.loads(connection.getresponse().read())
            assert result['category'] == expected
            assert result['known'] is True
            assert result['affected'] == {}
            assert result['coverage'] == 'unknown'
            assert result['reason']
            assert result['next_action']
        finally:
            connection.close()
    assert client(dict(operation='status', job_id='absent'))['category'] == 'not_found'
    assert client(dict(operation='resolve', namespace='operator', command_id='absent'))['known'] is False


def test_text_client_invalid_input_has_actionable_error_envelope(command_process):
    _, _, config = command_process
    for raw in ('{', 'x' * 65537):
        response = subprocess.run([*PYTHON, '-m', 'nilaris.command_service', 'client',
                                   '--config', str(config)], input=raw, text=True,
                                  capture_output=True, cwd=ROOT, timeout=15)
        assert response.returncode == 0, 'client input error did not produce a public envelope'
        result = json.loads(response.stdout)
        assert result == dict(known=True, category='invalid_request', affected={}, coverage='unknown',
                             reason='Command syntax, route or bounded metadata is invalid.',
                             next_action='correct_request')


def test_command_startup_configuration_failure_has_safe_error_envelope(tmp_path):
    response = subprocess.run([*PYTHON, '-m', 'nilaris.command_service', 'client',
                               '--config', str(tmp_path / 'absent.json')],
                              input='{}', text=True, capture_output=True, cwd=ROOT, timeout=15)
    assert response.returncode == 1
    # Conda appends its own diagnostic for a nonzero child exit.
    assert json.loads(response.stderr.splitlines()[0]) == dict(
        known=True, category='configuration_unavailable', affected={}, coverage='unknown',
        reason='Private command configuration is unavailable or invalid.',
        next_action='configure_approved_private_configuration')
    assert response.stdout == ''


def test_command_startup_listener_failure_has_safe_error_envelope(command_process):
    _, start, config = command_process
    start()
    response = subprocess.run([*PYTHON, '-m', 'nilaris.command_service', 'serve',
                               '--config', str(config)], text=True, capture_output=True,
                              cwd=ROOT, timeout=15)
    assert response.returncode == 1
    assert json.loads(response.stderr.splitlines()[0]) == dict(
        known=True, category='listener_unavailable', affected={}, coverage='unknown',
        reason='Command listener is unavailable.', next_action='restore_authorized_loopback_listener')


def test_command_invalid_cli_arguments_have_safe_error_envelope():
    response = subprocess.run([*PYTHON, '-m', 'nilaris.command_service'],
                              text=True, capture_output=True, cwd=ROOT, timeout=15)
    assert response.returncode == 2
    assert json.loads(response.stderr.splitlines()[0]) == dict(
        known=True, category='invalid_request', affected={}, coverage='unknown',
        reason='Command syntax, route or bounded metadata is invalid.', next_action='correct_request')


def test_database_network_failure_is_unknown_then_same_key_recovers(command_process):
    """Cut only a task-owned proxy; PostgreSQL and its retained data keep running."""
    client, start, config = command_process
    available = threading.Event()
    available.set()

    class Proxy(socketserver.BaseRequestHandler):
        def handle(self):
            if not available.is_set():
                return
            with socket.create_connection(('127.0.0.1', 55432), timeout=3) as upstream:
                peers = (self.request, upstream)
                while available.is_set():
                    readable, _, _ = select.select(peers, [], [], 0.1)
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.request else self.request
                        destination.sendall(data)

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Proxy) as proxy:
        proxy.daemon_threads = True
        runner = threading.Thread(target=proxy.serve_forever, daemon=True)
        runner.start()
        data = json.loads(config.read_text())
        data['database']['port'] = proxy.server_address[1]
        config.write_text(json.dumps(data))
        del data
        try:
            start()
            original = client(dict(operation='offer', namespace='operator', command_id='before-outage', members=[member()]))
            assert original['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
            job_id = original['members'][0]['job_id']
            new_member = member('outage')
            new_member['document_id'] = 'doc-outage'
            offer = dict(operation='offer', namespace='operator', command_id='during-outage', members=[new_member])
            available.clear()
            for command, affected, action in [
                    (dict(operation='status', job_id=job_id), dict(job_id=job_id), 'retry_status'),
                    (dict(operation='resolve', namespace='operator', command_id='before-outage'),
                     dict(namespace='operator', command_id='before-outage'), 'retry_same_key'),
                    (offer, dict(namespace='operator', command_id='during-outage', member_keys=['outage']), 'retry_same_key')]:
                response = client(command)
                assert response['known'] is False
                assert response['category'] == 'persistence_unavailable'
                assert response['coverage'] == 'unknown'
                assert response['affected'] == affected
                assert response['next_action'] == action
            available.set()
            assert client(dict(operation='resolve', namespace='operator', command_id='before-outage')) == original
            recovered = client(offer)
            assert recovered['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
            assert client(offer) == recovered
            status = client(dict(operation='status', job_id=job_id))
            assert status['input']['source_revision'] == 'r1'
            assert status['admission']['waiting_jobs'] == 2
            assert status['admission']['job_reservation_bytes'] == 16777216
            assert status['admission']['receipt_reservation_bytes'] == 270336
            print('operator transcript: offer accepted=1; DB cut status/resolve/offer known=false '
                  'category=persistence_unavailable coverage=unknown; restored resolve=original; '
                  'same-key retry accepted=1 replay=identical; status waiting=2 job_bytes=16777216 '
                  'receipt_bytes=270336 source_revision=r1', flush=True)
        finally:
            available.set()
            proxy.shutdown()
            runner.join(timeout=5)
