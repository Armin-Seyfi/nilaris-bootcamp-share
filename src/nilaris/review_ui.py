"""Loopback browser adapter over the existing authenticated command HTTP seam.

No processing route, credential in browser assets, or new review data contract.
"""
import argparse
import hmac
from http.server import HTTPServer
from pathlib import Path
import secrets
import time

from nilaris.command_service import Configuration, request
from nilaris.review_http import ReviewHTTPHandler

ASSETS = Path(__file__).with_name('review_web')
ADDRESS = ('127.0.0.1', 18766)
FILES = {'/': ('index.html', 'text/html; charset=utf-8'),
         '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
         '/style.css': ('style.css', 'text/css; charset=utf-8'),
         '/reference-base.css': ('reference-base.css', 'text/css; charset=utf-8'),
         '/reference-refined-b.css': ('reference-refined-b.css', 'text/css; charset=utf-8')}
REVIEW_OPERATIONS = {'review_create', 'review_read', 'review_resume',
                     'review_save', 'review_approve', 'review_resolve'}


def serve(configuration, job_id, draft_id=None, base_version=None, seconds=1800):
    session = secrets.token_urlsafe(32)
    namespace = 'local-review-ui:' + job_id
    drafts = {draft_id} if draft_id else set()
    origin = 'http://127.0.0.1:18766'
    deadline = time.monotonic() + seconds
    ui_deadline_unix_ms = int((time.time() + seconds) * 1000)

    class Handler(ReviewHTTPHandler):
        expected_host = '127.0.0.1:18766'
        cookie_name = 'nilaris_review_session'
        session_token = session

        def do_GET(self):
            if not self.host_valid():
                self.reply(403, {'category': 'invalid_host'})
            elif self.path in FILES:
                name, mime = FILES[self.path]
                self.reply(200, (ASSETS / name).read_bytes(), mime)
            elif self.path == '/api/context':
                if not self.authenticated():
                    self.reply(401, {'category': 'unauthorized'})
                    return
                self.reply(200, dict(job_id=job_id, draft_id=draft_id,
                                     base_version=base_version, namespace=namespace,
                                     ui_deadline_unix_ms=ui_deadline_unix_ms))
            else:
                self.reply(404, {'category': 'not_found'})

        def do_POST(self):
            if not self.host_valid() or self.headers.get_all('Origin', []) != [origin]:
                self.reply(403, {'category': 'same_origin_required'})
                return
            try:
                body = self.read_json()
            except (OSError, ValueError, TypeError, RecursionError):
                self.reply(400, {'category': 'invalid_request'})
                return
            if self.path == '/api/session':
                token = body.get('token')
                if (set(body) != {'token'} or not isinstance(token, str) or
                        not hmac.compare_digest(token.encode(), configuration.token.encode())):
                    self.reply(401, {'category': 'unauthorized'})
                    return
                self.reply(200, {'authenticated': True}, cookie=(
                    'nilaris_review_session=' + session + '; HttpOnly; SameSite=Strict; Path=/'))
                return
            if not self.authenticated():
                self.reply(401, {'category': 'unauthorized'})
                return
            if self.path != '/api/command' or not self.in_scope(body):
                self.reply(400, {'category': 'unsupported_ui_command'})
                return
            result = request(configuration, body)
            if body['operation'] in ('review_create', 'review_resolve') and result.get('saved') and result.get('draft_id'):
                drafts.add(result['draft_id'])
            self.reply(200, result)

        @staticmethod
        def in_scope(body):
            operation = body.get('operation')
            if not isinstance(operation, str):
                return False
            if operation in ('status', 'result'):
                return body.get('job_id') == job_id
            if operation not in REVIEW_OPERATIONS:
                return False
            if 'namespace' in body and body['namespace'] != namespace:
                return False
            if operation == 'review_create':
                return body.get('job_id') == job_id and body.get('command_id') == 'create'
            if operation == 'review_resolve':
                return body.get('namespace') == namespace
            return isinstance(body.get('draft_id'), str) and body['draft_id'] in drafts

    class Server(HTTPServer):
        request_queue_size = 1

        def handle_error(self, *args):
            pass  # Never log request/configuration/credential exception details.

    with Server(ADDRESS, Handler) as server:
        server.timeout = .2
        print('Local review UI ready at http://127.0.0.1:18766; bounded session', flush=True)
        while time.monotonic() < deadline:
            server.handle_request()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--draft-id')
    parser.add_argument('--base-version')
    parser.add_argument('--seconds', type=int, default=1800)
    args = parser.parse_args()
    try:
        if not 1 <= args.seconds <= 1800 or not args.job_id.strip():
            raise ValueError
        configuration = Configuration.load(args.config)
        serve(configuration, args.job_id, args.draft_id, args.base_version, args.seconds)
    except (OSError, ValueError, TypeError):
        print('Local review UI unavailable; inspect authorized configuration, listener and runtime.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
