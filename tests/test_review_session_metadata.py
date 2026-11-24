"""Offline HTTP seam: known UI deadline, with no listener or backend."""
from io import BytesIO
import json
from types import SimpleNamespace

from nilaris import review_ui


def test_context_reports_only_known_ui_deadline_without_extending_server(monkeypatch):
    responses = []
    requests = []

    class Connection:
        def settimeout(self, timeout):
            pass

        def makefile(self, mode, *args):
            return BytesIO(b'GET /api/context HTTP/1.1\r\n'
                           b'Host: 127.0.0.1:18766\r\n'
                           b'Cookie: nilaris_review_session=isolated-session\r\n\r\n')

        def sendall(self, raw):
            responses.append(raw)

    class OfflineServer:
        def __init__(self, address, handler):
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            requests.append('request')
            self.handler(Connection(), ('127.0.0.1', 1), self)

    clock = iter([100, 100, 102])
    monkeypatch.setattr(review_ui, 'HTTPServer', OfflineServer)
    monkeypatch.setattr(review_ui.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(review_ui.time, 'time', lambda: 1000)
    monkeypatch.setattr(review_ui.secrets, 'token_urlsafe', lambda size: 'isolated-session')
    review_ui.serve(SimpleNamespace(token='unused'), 'isolated-job', seconds=1)
    headers, body = b''.join(responses).split(b'\r\n\r\n', 1)
    assert b'200 OK' in headers
    assert json.loads(body) == dict(job_id='isolated-job', draft_id=None,
        base_version=None, namespace='local-review-ui:isolated-job',
        ui_deadline_unix_ms=1001000)
    assert requests == ['request']
