"""Loopback-only controlled QA UI. Identity issuance is deliberately absent."""
from dataclasses import dataclass
from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import argparse
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
import re
import secrets
import stat
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from nilaris import customer_review, intake_commands, minimized_evidence
from nilaris.onboarding182 import (
    AcceptanceRefused, AccessBodyInvalid, AccessReceiptMismatch, ControlledIdentityVerifier, CustomerAccessDenied,
    Onboarding, Principal, PrivateInvitationDelivery, is_command_key,
)
from nilaris.provisioning181 import OperatorProvisioner, ControlledOperatorVerifier, ProvisioningRefused, _private_json


@dataclass(frozen=True)
class AcceptanceJournal:
    customer_id: str


@dataclass(frozen=True)
class ManagementJournal:
    customer_id: str
    revocation: bool = False


def same_json(left, right):
    # Python equates false with zero, even inside a command or selector.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


class JournalBusy(Exception):
    pass


class PrivateCommandJournal:
    def __init__(self, directory):
        self.directory = PrivateInvitationDelivery(directory).directory

    def _path(self, principal, channel):
        identity = json.dumps([principal.issuer, principal.subject, principal.principal_class, principal.recipient])
        name = hashlib.sha256(identity.encode()).hexdigest()
        if isinstance(channel, (customer_review.ReviewJournal, intake_commands.IntakeJournal)):
            return self.directory / (name + channel.suffix)
        if isinstance(channel, ManagementJournal):
            target = hashlib.sha256(channel.customer_id.encode()).hexdigest()
            prefix = '-management-revocation-' if channel.revocation else '-management-'
            return self.directory / (name + prefix + target)
        if isinstance(channel, AcceptanceJournal):
            target = hashlib.sha256(channel.customer_id.encode()).hexdigest()
            return self.directory / (name + '-accept-' + target)
        if channel not in ('command', 'revocation', 'access'):
            raise ValueError('invalid pending channel')
        return self.directory / (name if channel == 'command' else name + '-' + channel)

    @contextmanager
    def locked(self, principal, channel, *, catalog=False):
        # The short catalog lock makes an unselected recipient's single-file
        # lookup atomic with file creation/removal. Never perform DB work under it.
        if catalog and principal.principal_class == 'customer':
            channel = 'command'
        path = self._path(principal, channel).with_suffix('.catalog' if catalog else '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not catalog or time.monotonic() >= deadline:
                        raise JournalBusy from None
                    time.sleep(0.005)
            yield
        finally:
            os.close(fd)

    def single_pending(self, principal):
        """Capture one original even if another invitation was assigned since login."""
        legacy = self._path(principal, 'command')
        prefix = legacy.name + '-accept-'
        with self.locked(principal, 'command', catalog=True):
            selected = legacy if legacy.exists() else None
            for path in self.directory.glob(prefix + '*'):
                if not re.fullmatch('[0-9a-f]{64}', path.name[len(prefix):]):
                    continue
                if selected is not None:
                    raise PermissionError('select your invitation for exact recovery')
                selected = path
            if selected is None:
                return False, None
            return selected != legacy, _private_json(selected)

    def exists(self, principal, *, channel):
        return self._path(principal, channel).exists()

    def read(self, principal, *, channel='command'):
        with self.locked(principal, channel, catalog=True):
            return self._read(principal, channel)

    def _read(self, principal, channel):
        path = self._path(principal, channel)
        try:
            if isinstance(channel, (customer_review.ReviewJournal, intake_commands.IntakeJournal)):
                limit = customer_review.MAX_BODY * 2
                try:
                    record = _private_json(path, limit=limit)
                except ProvisioningRefused:
                    if not self._recover_review_link(path, limit):
                        raise
                    record = _private_json(path, limit=limit)
                if not isinstance(record, dict):
                    raise ProvisioningRefused('invalid private review record')
                return record
            return _private_json(path)
        except FileNotFoundError:
            return None

    def _recover_review_link(self, path, limit):
        """Finish an old writer's interrupted link/unlink under its catalog lock."""
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.fstat(descriptor)
            if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
                return False
            target = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISREG(target.st_mode) or target.st_uid != os.getuid()
                    or stat.S_IMODE(target.st_mode) != 0o600 or target.st_nlink != 2
                    or target.st_size > limit):
                return False
            for name in os.listdir(descriptor):
                if not re.fullmatch(r'\.pending-[0-9a-f]{32}', name):
                    continue
                try:
                    companion = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Another scope may publish its own temporary.
                if os.path.samestat(target, companion) and companion.st_nlink == 2:
                    os.unlink(name, dir_fd=descriptor)
                    os.fsync(descriptor)
                    return True
            return False
        finally:
            os.close(descriptor)

    def save(self, principal, request, *, channel='command'):
        with self.locked(principal, channel, catalog=True):
            self._save(principal, request, channel)

    def _save(self, principal, request, channel):
        if channel == 'access' and request.get('action') != 'change_access':
            raise ValueError('access channel requires an access transition')
        if channel == 'revocation' and request.get('action') != 'revoke':
            raise ValueError('revocation channel requires explicit revoke')
        existing = self._read(principal, channel)
        if existing is not None:
            equivalent = customer_review.same_command if isinstance(channel, (customer_review.ReviewJournal, intake_commands.IntakeJournal)) else same_json
            if not equivalent(existing, request):
                raise PermissionError('resolve or acknowledge your exact pending command first')
            return
        path = self._path(principal, channel)
        # Reconcile a crash between the two acknowledgement unlinks before a new
        # request can inherit any previous command's known-outcome marker.
        path.with_suffix('.known').unlink(missing_ok=True)
        self._sync()
        temporary = self.directory / ('.pending-' + secrets.token_hex(16))
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(request, stream, ensure_ascii=not isinstance(channel, (customer_review.ReviewJournal, intake_commands.IntakeJournal)))
                stream.flush()
                os.fsync(stream.fileno())
            if isinstance(channel, (customer_review.ReviewJournal, intake_commands.IntakeJournal)):
                # The catalog lock covers the absence check above and publication.
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self._sync()

    def _sync(self):
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def acknowledge(self, principal, *, channel='command'):
        with self.locked(principal, channel, catalog=True):
            self._acknowledge(principal, channel)

    def _acknowledge(self, principal, channel):
        path = self._path(principal, channel)
        if self._read(principal, channel) is not None and not path.with_suffix('.known').exists():
            raise PermissionError('unknown outcome; resolve your exact pending command first')
        path.unlink(missing_ok=True)
        path.with_suffix('.known').unlink(missing_ok=True)
        self._sync()

    def mark_known(self, principal, *, channel='command'):
        path = self._path(principal, channel).with_suffix('.known')
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync()


EXPLICIT_RETRY = {'accept', 'change_access', 'manage'}
MUTATIONS = {'manage', 'provision', 'invite', 'accept', 'revoke', 'change_access'}
ACTIONS = MUTATIONS | {'members', 'status', 'invitation_status', 'landing', 'access_status', 'access_command_status'}


class OnboardingHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, service, journal, *, session_seconds, intake=None):
        if address[0] != '127.0.0.1':
            raise ValueError('loopback binding required')
        if (not isinstance(session_seconds, (int, float)) or not math.isfinite(session_seconds)
                or session_seconds <= 0):
            raise ValueError('explicit finite session validity required')
        self.intake = intake
        self.service = service
        self.journal = journal
        self.session_seconds = session_seconds
        self.sessions = {}
        self.session_lock = threading.Condition()
        self.active_requests = {}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'ControlledOnboarding'

    def log_message(self, *args):
        pass  # Neither request bodies, identity credentials nor invitation secrets.

    def _reply(self, code, body, cookie=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get('Cookie', ''))
        token = cookie.get('onboarding_session')
        session = self.server.sessions.get(token.value if token else '')
        if not session or time.monotonic() >= session[1]:
            raise PermissionError('reauthentication required; pending commands retained')
        credential = session[0]
        principal = self.server.service.identities.verify(credential)
        return credential, principal, session[2], token.value

    @contextmanager
    def _session_boundary(self, token):
        """After acquiring a customer row, order the protected request with sign-out."""
        server = self.server
        admitted = False

        def check():
            nonlocal admitted
            with server.session_lock:
                self._session()
                if not admitted:
                    server.active_requests[token] = server.active_requests.get(token, 0) + 1
                    admitted = True

        try:
            yield check
        finally:
            if admitted:
                with server.session_lock:
                    server.active_requests[token] -= 1
                    if not server.active_requests[token]:
                        del server.active_requests[token]
                        server.session_lock.notify_all()

    def do_GET(self):
        from nilaris.review_ui import ASSETS, FILES
        path = urlsplit(self.path).path
        mime = 'text/html; charset=utf-8'
        if path == '/':
            body = Path(__file__).with_name('onboarding182.html').read_bytes()
        elif path in ('/schema-preview', '/schema-preview.mjs', '/review-first-b', '/review-first-b.css', '/review-first-b.mjs', '/stage-save.mjs'):
            asset, mime = {
                '/schema-preview': ('schema_preview.html', mime),
                '/schema-preview.mjs': ('schema_preview.mjs', 'text/javascript'),
                '/review-first-b': ('review_first_b.html', mime),
                '/review-first-b.css': ('review_first_b.css', 'text/css'),
                '/review-first-b.mjs': ('review_first_b.mjs', 'text/javascript'),
                '/stage-save.mjs': ('stage_save.mjs', 'text/javascript'),
            }[path]
            body = (ASSETS / asset).read_bytes()
        elif path == '/minimized-evidence':
            body = (ASSETS / 'minimized_evidence.html').read_bytes()
        elif path in ('/customer-intent.js', '/intake-intent.js', '/minimized-evidence.js'):
            mime = 'text/javascript; charset=utf-8'
            body = (ASSETS / path[1:].replace('-', '_')).read_bytes()
        elif path == '/expert' or path in FILES:
            filename, mime = FILES['/' if path == '/expert' else path]
            body = (ASSETS / filename).read_bytes()
        else:
            return self._reply(404, {'state': 'refused'})
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        assets = "'self' 'unsafe-inline'" if path == '/' else "'self'"
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', f"default-src 'self'; script-src {assets}; style-src {assets}; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        expected = 'http://127.0.0.1:' + str(self.server.server_port)
        if self.headers.get('Origin') != expected or self.headers.get('Host') != expected[7:]:
            return self._reply(403, {'state': 'refused', 'reason': 'same-origin request required'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = customer_review.MAX_BODY if self.path in customer_review.ROUTES else 12000
            if not 0 < length <= limit or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('bounded JSON request required')
            raw = self.rfile.read(length)
            exact_json = (self.path in customer_review.ROUTES or self.path in minimized_evidence.ROUTES
                          or self.path.startswith('/intake/'))
            request = customer_review.decode_request(raw) if exact_json else json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError('object required')
            if self.path == '/session':
                if set(request) != {'credential'}:
                    raise ValueError('credential required')
                credential = request['credential']
                principal, revisions = self.server.service.authenticate_session(credential)
                with self.server.session_lock:
                    if len(self.server.sessions) >= 128:
                        self.server.sessions = {k:v for k,v in self.server.sessions.items() if v[1] > time.monotonic()}
                    if len(self.server.sessions) >= 128:
                        raise PermissionError('session capacity reached')
                    token = secrets.token_hex(32)
                    self.server.sessions[token] = (credential, time.monotonic() + self.server.session_seconds, revisions)
                return self._reply(200, {'state': 'authenticated', 'principal_class': principal.principal_class},
                    'onboarding_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=' + str(int(self.server.session_seconds)))
            with self.server.session_lock:
                credential, principal, revisions, token = self._session()
            if self.path == '/logout':
                with self.server.session_lock:
                    self.server.sessions.pop(token, None)
                    self.server.session_lock.wait_for(lambda: token not in self.server.active_requests)
                return self._reply(200, {'state': 'signed_out', 'pending': 'retained'},
                    'onboarding_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            service = self.server.service
            customer = principal.principal_class == 'customer'
            with self._session_boundary(token) as check_session, ExitStack() as command_locks:
                boundary = service.customer_request(credential, revisions, check_session=check_session) if customer else nullcontext()
                if not customer:
                    service._operator(credential)
                with boundary:
                    result, known_channel = self._action(credential, principal, request, command_locks)
                # Make completion acknowledgeable only after the DB commit.
                if known_channel is not None and self._known(result):
                    self.server.journal.mark_known(principal, channel=known_channel)
            return self._reply(200, result)
        except JournalBusy:
            return self._reply(409, {'state': 'busy', 'reason': 'another request is using this pending command; try again'})
        except PermissionError as error:
            return self._reply(403, {'state': 'refused', 'reason': str(error)})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return self._reply(400, {'state': 'refused', 'reason': 'invalid bounded command; pending command retained'})
        except (OSError, RuntimeError, psycopg.Error):
            return self._reply(503, {'state': 'unknown', 'reason': 'outcome unresolved; retain original command and resolve after reauthentication'})

    @staticmethod
    def _arguments(request):
        return {key: value for key, value in request.items() if key != 'action'}

    def _resolve(self, credential, request):
        service = self.server.service
        action = request['action']
        arguments = self._arguments(request)
        if action == 'manage':
            return service.management_status(credential, **arguments)
        if action == 'provision':
            return service.status(credential, arguments['operation_id'])
        if action == 'change_access':
            try:
                return service.access_command_status(credential, **arguments)
            except (AccessBodyInvalid, AccessReceiptMismatch):
                return {'state': 'refused', 'reason': 'original access command refused'}
        if action == 'accept':
            try:
                return service.acceptance_status(credential, **arguments)
            except AcceptanceRefused:
                return {'state': 'refused', 'reason': 'original acceptance refused'}
        if action in ('invite', 'revoke'):
            result = service.invitation_status(credential, arguments['command_id'])
            if action == 'revoke' and result['state'] != 'revoked':
                return dict(result, invitation_state=result['state'], state='unknown',
                            reason='revocation outcome unresolved; original command retained')
            return result
        raise ValueError('invalid saved action')

    def _acceptance_channel(self, credential, principal, invitation_id):
        service = self.server.service
        target = service.acceptance_customer(credential, invitation_id)
        legacy = self.server.journal.read(principal)
        # Keep historical bodies and known markers together, in their original place.
        if legacy and service.acceptance_customer(
                credential, legacy.get('invitation_id'), require_active=False) == target:
            return 'command'
        return AcceptanceJournal(target)

    def _customer_recovery_channel(self, credential, principal, request):
        if self.path in ('/resolve', '/acknowledge') and set(request) in ({'command_id'}, {'invitation_id'}):
            # 02A could persist a refusal before the invitation was bound. Match the
            # recipient's original body directly, even alongside other scoped files.
            legacy = self.server.journal.read(principal)
            field, value = next(iter(request.items()))
            if legacy and legacy.get('action') == 'accept' and field in legacy and same_json(legacy[field], value):
                return 'command', legacy
            if field == 'command_id':
                raise PermissionError('original acceptance command required')
        if 'invitation_id' in request:
            if set(request) != {'invitation_id'}:
                raise ValueError('invalid recovery selector')
            channel = self._acceptance_channel(credential, principal, request['invitation_id'])
            return channel, self.server.journal.read(principal, channel=channel)
        if request not in ({}, {'channel': 'command'}):
            raise ValueError('invalid customer recovery channel')
        scoped, saved = self.server.journal.single_pending(principal)
        if scoped:
            target = self.server.service.acceptance_customer(credential, saved['invitation_id'])
            return AcceptanceJournal(target), saved
        return 'command', saved

    def _intake_action(self, request, principal, locks):
        intake = self.server.intake
        customer = request.get('customer_id')
        if not isinstance(customer, str) or not 0 < len(customer) <= 100:
            raise ValueError('customer selector required')
        args = {key: value for key, value in request.items() if key != 'customer_id'}
        if self.path == '/intake/capabilities' and not args:
            capabilities = intake.capabilities(customer)
            for kind, permitted in (('admit',capabilities['intake']),('set-policy',capabilities['policy_admin'])):
                if permitted:
                    unlocked = intake_commands.handle('unlock',{'kind':kind},intake,customer,
                                                      principal,self.server.journal,locks)
                    capabilities[kind + '_slot'] = unlocked['slot']
            return capabilities
        if self.path == '/intake/jobs' and not args:
            return intake.jobs(customer)
        if self.path == '/intake/admission-status' and set(args) == {'command_id'}:
            return intake.admission_status(customer, **args)
        if self.path == '/intake/job' and set(args) == {'job_id'}:
            return intake.job(customer, **args)
        path = self.path.removeprefix('/intake/')
        if path in ('unlock','admit','set-policy','prepare','pending','retry','ack'):
            return intake_commands.handle(path, args, intake, customer, principal, self.server.journal, locks)
        if self.path == '/intake/policy' and not args:
            from nilaris.scoped_intake import CONTROLLED_ROUTE
            return {**intake.policy(customer), 'controlled_route': CONTROLLED_ROUTE}
        if self.path == '/intake/policy-status' and set(args) == {'command_id'}:
            return intake.policy_status(customer, **args)
        raise ValueError('invalid intake request')

    def _action(self, credential, principal, request, command_locks):
        service, journal = self.server.service, self.server.journal
        if self.path in minimized_evidence.ROUTES:
            if principal.principal_class != 'customer':
                raise PermissionError('customer authority required')
            return minimized_evidence.handle(self.path, request, service), None
        if self.path.startswith('/intake/'):
            if principal.principal_class != 'customer' or self.server.intake is None:
                raise PermissionError('customer intake unavailable')
            return self._intake_action(request, principal, command_locks), None
        if self.path in customer_review.ROUTES:
            if principal.principal_class != 'customer':
                raise PermissionError('customer authority required')
            return customer_review.handle(self.path, request, service, principal, journal, command_locks)
        if self.path in ('/pending', '/resolve', '/acknowledge', '/retry'):
            channel = request.get('channel', 'command')
            customer = principal.principal_class == 'customer'
            management = customer and set(request) in ({'management_customer'}, {'management_revocation'})
            if management:
                target = next(iter(request.values()))
                service.require_management(credential, target)
                channel = ManagementJournal(target, 'management_revocation' in request)
                command_locks.enter_context(journal.locked(principal, channel))
                saved = journal.read(principal, channel=channel)
            elif customer:
                channel, saved = self._customer_recovery_channel(credential, principal, request)
            else:
                if 'invitation_id' in request:
                    raise PermissionError('customer recovery requires recipient authority')
                if self.path != '/pending':
                    command_locks.enter_context(journal.locked(principal, channel))
                saved = journal.read(principal, channel=channel)
            if saved and customer and not management:
                if 'invitation_id' in request and not same_json(request['invitation_id'], saved.get('invitation_id')):
                    raise PermissionError('select the original invitation for exact recovery')
                invitation_id = saved.get('invitation_id')
                legacy = channel == 'command' and saved.get('action') == 'accept'
                target = service.acceptance_customer(credential, invitation_id, require_active=False)
                if 'command_id' in request and target is not None:
                    raise PermissionError('select the original invitation for exact recovery')
                # A malformed legacy locator could never reach a mutation. A well-formed
                # unbound locator may only be acknowledged with its original known marker.
                legacy_refusal = legacy and target is None and (
                    self.path == '/acknowledge' or (self.path == '/resolve' and not is_command_key(invitation_id)))
                if not legacy_refusal:
                    service.acceptance_customer(credential, invitation_id)
                if self.path != '/pending':
                    command_locks.enter_context(journal.locked(principal, channel))
                if not same_json(journal.read(principal, channel=channel), saved):
                    raise PermissionError('pending command changed; select your original invitation')
            if self.path == '/acknowledge':
                if saved:
                    journal.acknowledge(principal, channel=channel)
                return {'state': 'acknowledged'}, None
            if self.path == '/pending':
                fields = ('action', 'command_id', 'operation_id', 'customer_id', 'invitation_id',
                          'access_state', 'expected_revision')
                return {'state': 'pending' if saved else 'absent',
                        'command': {k: saved[k] for k in fields if saved and k in saved}}, None
            if not saved:
                return {'state': 'absent'}, None
            if customer and not management and legacy_refusal:
                return {'state': 'refused', 'reason': 'original acceptance refused'}, channel
            result = self._resolve(credential, saved)
            if self.path == '/retry':
                if saved['action'] not in EXPLICIT_RETRY:
                    raise PermissionError('explicit retry applies only to access or acceptance')
                if result['state'] in ('absent', 'in_progress'):
                    args = self._arguments(saved)
                    try:
                        result = getattr(service, saved['action'])(credential, **args)
                    except AcceptanceRefused:
                        # Absence and the deliberate retry share the customer lock.
                        journal.mark_known(principal, channel=channel)
                        raise
            return result, channel if self._known(result) else None
        if self.path != '/command':
            raise ValueError('unknown route')
        action = request.get('action')
        if action not in ACTIONS:
            raise ValueError('unknown action')
        args = self._arguments(request)
        if 'credential' in args:
            raise PermissionError('session identity is server derived')
        method = getattr(service, action)
        inspect.signature(method).bind(credential, **args)
        service._principal(credential, 'customer' if action in ('accept', 'landing', 'manage', 'members') else 'operator')
        if action not in MUTATIONS:
            return method(credential, **args), None
        channel = {'revoke': 'revocation', 'change_access': 'access'}.get(action, 'command')
        if action == 'manage':
            service.management_status(credential, **args)
            channel = ManagementJournal(args['customer_id'], args['command']['kind'] == 'revoke')
        if action == 'accept':
            channel = self._acceptance_channel(credential, principal, request['invitation_id'])
        command_locks.enter_context(journal.locked(principal, channel))
        previous = journal.read(principal, channel=channel)
        if action == 'change_access' and previous is None:
            # Reject invalid input and unowned targets before reserving a private journal.
            service.access_command_status(credential, **args)
        journal.save(principal, request, channel=channel)
        if previous and action in EXPLICIT_RETRY:
            result = self._resolve(credential, previous)
        else:
            try:
                result = method(credential, **args)
            except CustomerAccessDenied:
                # A new invite refused before issuance has no uncertain prior effect.
                if action == 'invite' and previous is None:
                    journal.mark_known(principal, channel=channel)
                raise
            except (PermissionError, ValueError) as error:
                if not previous and (action not in EXPLICIT_RETRY or
                        isinstance(error, (AccessReceiptMismatch, AcceptanceRefused)) or
                        (action == 'accept' and isinstance(error, ValueError))):
                    journal.mark_known(principal, channel=channel)
                raise
        return result, channel

    @staticmethod
    def _known(result):
        return (result.get('state') not in ('absent', 'provisioning', 'in_progress', 'unknown') and
                (result.get('delivery') != 'unknown' or result.get('state') in ('revoked', 'expired', 'accepted')))


def controlled_services(config):
    if config.get('authentication_mode') != 'controlled-test-only':
        raise PermissionError('production authentication is not configured')
    identities = ControlledIdentityVerifier({credential: Principal(**value)
        for credential, value in config['controlled_identities'].items()})
    operator = OperatorProvisioner(config, ControlledOperatorVerifier(config['controlled_credentials']))
    service = Onboarding(operator, identities, PrivateInvitationDelivery(config['invitation_directory']),
        validity_ceiling_seconds=config['invitation_validity_ceiling_seconds'])
    intake = None
    if 'controlled_intake_sources' in config:
        from nilaris.scoped_intake import ScopedIntake, SourceBinding
        bindings = {customer: SourceBinding(**binding)
                    for customer, binding in config['controlled_intake_sources'].items()}
        intake = ScopedIntake(service, bindings)
    return service, intake


def build_server(config, *, port):
    service, intake = controlled_services(config)
    return OnboardingHTTPServer(('127.0.0.1', port), service,
        PrivateCommandJournal(config['journal_directory']), session_seconds=config['session_validity_seconds'], intake=intake)


def main():
    parser = argparse.ArgumentParser(description='Explicit controlled local onboarding QA only')
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=18768)
    parser.add_argument('--install', action='store_true', help='initialize registry once before QA')
    args = parser.parse_args()
    config = _private_json(args.config)
    server = build_server(config, port=args.port)
    if args.install:
        # Setup uses the configured operator identity; no credential is printed.
        for credential, principal in server.service.identities._credentials.items():
            if principal.principal_class == 'operator':
                server.service.install(credential)
                break
        else:
            raise PermissionError('controlled operator required')
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
