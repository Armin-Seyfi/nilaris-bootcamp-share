"""The customer can open the scoped job status view from the existing landing."""
from contextlib import closing
import threading
from urllib.request import urlopen

from nilaris.onboarding182_ui import OnboardingHTTPServer


def test_customer_landing_offers_scoped_job_status_view():
    with OnboardingHTTPServer(('127.0.0.1', 0), None, None, session_seconds=30) as server:
        listener = threading.Thread(target=server.serve_forever)
        listener.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/', timeout=5) as response:
                page = response.read().decode()
            assert 'View scoped jobs' in page
            assert 'Authorization evidence reference' in page
        finally:
            server.shutdown()
            listener.join()


def test_policy_ambiguity_is_refused_before_session_or_customer_lookup():
    from http.client import HTTPConnection
    with OnboardingHTTPServer(('127.0.0.1', 0), None, None, session_seconds=30) as server:
        listener = threading.Thread(target=server.serve_forever)
        listener.start()
        try:
            with closing(HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as connection:
                connection.request('POST', '/intake/set-policy', '{"policy":{"active":false,"active":true}}',
                                   {'Origin': f'http://127.0.0.1:{server.server_port}',
                                    'Content-Type': 'application/json'})
                assert connection.getresponse().status == 400
        finally:
            server.shutdown()
            listener.join()


def test_worker_cli_reports_unavailable_original_without_exposing_internal_diagnostics(tmp_path):
    import subprocess
    process = subprocess.run(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         'runtime/a1/with_overlay.py', '-m', 'nilaris.scoped_worker_cli',
         '--config', str(tmp_path / 'private.json'), '--customer', 'a' * 32, 'reconcile', 'original-job'],
        capture_output=True, text=True, timeout=20)
    assert process.returncode == 1
    assert process.stdout == '{"state": "unknown", "next_action": "inspect_original_job"}\n'
    assert 'Traceback' not in process.stderr
