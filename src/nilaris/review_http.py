"""Common loopback request checks and private responses for Reviewer sessions."""
from contextlib import contextmanager
import fcntl
import hmac
import os
import secrets
import stat
import json
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler

MAX_BODY = 65536


class ReviewHTTPHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *args):
        pass

    def send_error(self, *args, **kwargs):
        self.reply(400, {'category': 'invalid_request'})

    def reply(self, code, data, mime='application/json', cookie=None):
        raw = data if isinstance(data, bytes) else json.dumps(data, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def host_valid(self):
        return self.headers.get_all('Host', []) == [self.expected_host]

    def authenticated(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            supplied = cookies[self.cookie_name].value
            return hmac.compare_digest(supplied, self.session_token)
        except (KeyError, ValueError, TypeError, CookieError):
            return False

    def same_origin_json(self):
        if not self.host_valid() or self.headers.get_all('Origin', []) != ['http://' + self.expected_host]:
            self.reply(403, {'category': 'same_origin_required'})
            return None
        try:
            return self.read_json()
        except (OSError, ValueError, TypeError, RecursionError):
            self.reply(400, {'category': 'invalid_request'})
            return None

    def login(self, body, credential):
        token = body.get('token')
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), credential.encode()):
            return self.reply(401, {'category': 'unauthorized'})
        return self.reply(200, {'authenticated': True}, cookie=(
            f'{self.cookie_name}={self.session_token}; HttpOnly; SameSite=Strict; Path=/rfq/'))

    def read_json(self):
        lengths = self.headers.get_all('Content-Length', [])
        if ('Transfer-Encoding' in self.headers or len(lengths) != 1 or
                self.headers.get('Content-Type') != 'application/json'):
            raise ValueError
        size = int(lengths[0])
        if not 0 < size <= MAX_BODY:
            raise ValueError
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
        return body


@contextmanager
def reviewer_credential(directory):
    """Serialize use of the private bundle and rotate any previous session's key."""
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        keyfile = 'reviewer.key'
        owner = os.fstat(directory_fd)
        if owner.st_uid != os.getuid() or owner.st_mode & 0o077:
            raise ValueError('Reviewer credentials require a private, owner-only bundle directory.')
        try:
            old = os.stat(keyfile, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(old.st_mode) or old.st_uid != os.getuid()
                    or stat.S_IMODE(old.st_mode) != 0o600 or old.st_nlink != 1):
                raise ValueError('Existing Reviewer key requires owner reconciliation.')
            os.unlink(keyfile, dir_fd=directory_fd)
        key = secrets.token_urlsafe(32)
        fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        owned = os.fstat(fd)
        try:
            raw = (key + '\n').encode('ascii')
            if os.write(fd, raw) != len(raw):
                raise OSError('Incomplete Reviewer key write.')
            yield key
        finally:
            try:
                current = os.stat(keyfile, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if os.path.samestat(owned, current):
                    os.unlink(keyfile, dir_fd=directory_fd)
            finally:
                os.close(fd)
    finally:
        os.close(directory_fd)
