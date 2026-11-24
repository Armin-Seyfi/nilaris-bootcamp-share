"""Controlled local initial-administrator onboarding. No production identity adapter.

The registry shares the provisioner's ownership-bound control connection;
customer business databases and the review service are never opened here.
"""
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

from psycopg import Connection

from nilaris import membership185
from nilaris.dispatch_gate import dispatch_gate
from nilaris.provisioning181 import ProvisioningRefused


def is_command_key(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) is not None


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    principal_class: str
    recipient: str


@dataclass(frozen=True)
class CustomerAuthority:
    revisions: dict
    principal: Principal
    check_session: Callable[[], None] | None


@dataclass(frozen=True)
class CustomerRequest(CustomerAuthority):
    connection: Connection


@dataclass(frozen=True)
class AccessCommand:
    issuer: str
    subject: str
    command_id: str
    operation_id: str
    requested_state: str
    expected_revision: int


class CustomerAccessDenied(PermissionError):
    def __init__(self, reason='customer access refused'):
        super().__init__(reason)


class AccessBodyInvalid(ValueError):
    """The exact access request cannot pass input validation."""


class AccessReceiptMismatch(PermissionError):
    """This key already identifies a different immutable access command."""


class AcceptanceRefused(PermissionError):
    """This exact acceptance cannot have succeeded."""


class ControlledIdentityVerifier:
    """Explicit test-only, server-held credentials; never accepts caller identity claims."""
    def __init__(self, credentials):
        self._credentials = dict(credentials)

    def verify(self, credential):
        if isinstance(credential, str):
            for token, principal in self._credentials.items():
                if hmac.compare_digest(token, credential):
                    return principal
        raise PermissionError('reauthentication required')

    def revoke(self, credential):
        self._credentials.pop(credential, None)

    def current_customer(self, issuer, subject, binding):
        expected = Principal(issuer, subject, 'customer', binding)
        return expected in self._credentials.values()

    def staff_subject(self, issuer, subject):
        return any(p.issuer == issuer and p.subject == subject and p.principal_class == 'operator'
                   for p in self._credentials.values())

    def current_operator(self, issuer, subject, operator_verifier):
        for credential, principal in self._credentials.items():
            if (principal.issuer == issuer and principal.subject == subject
                    and principal.principal_class == 'operator'):
                try:
                    if operator_verifier.verify(credential) == subject:
                        return True
                except PermissionError:
                    continue
        return False


class PrivateInvitationDelivery:
    """Private local handoff, with exclusive writes and no implicit resend/read API."""
    def __init__(self, directory):
        self.directory = Path(directory)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError('private delivery directory required')

    def deliver(self, reference, payload):
        if not re.fullmatch(r'[a-f0-9]{32}', reference):
            raise ValueError('invalid private reference')
        fd = os.open(self.directory / reference, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Onboarding:
    def __init__(self, provisioner, identities, delivery, *, validity_ceiling_seconds):
        if (isinstance(validity_ceiling_seconds, bool) or not isinstance(validity_ceiling_seconds, (int, float))
                or not math.isfinite(validity_ceiling_seconds) or validity_ceiling_seconds <= 0):
            raise ValueError('explicit finite validity ceiling required')
        self.provisioner = provisioner
        self.identities = identities
        self.delivery = delivery
        self.validity_ceiling = validity_ceiling_seconds
        self._deliveries = ContextVar('membership_deliveries', default=None)
        self._request = ContextVar('customer_request', default=None)

    def _principal(self, credential, kind):
        principal = self.identities.verify(credential)
        if (not isinstance(principal, Principal) or principal.principal_class != kind
                or not principal.issuer or not principal.subject
                or (kind == 'customer' and not principal.recipient)):
            raise PermissionError('principal authority refused')
        return principal

    def _operator(self, credential):
        principal = self._principal(credential, 'operator')
        # The adapter's operator identity must agree with the existing provisioner.
        if self.provisioner.verifier.verify(credential) != principal.subject:
            raise PermissionError('operator identity binding refused')
        return principal

    @contextmanager
    def _registry(self, *, mutate=False, install=False):
        request = self._request.get()
        if request is not None:
            if mutate:
                self.provisioner._require_monitor()
            yield request.connection
            return
        # Only schema setup needs the allocator lock. Runtime operations lock customer rows.
        # No independent allocator, default DB routing, or runtime credentials.
        with self.provisioner._control(allocator=install) as connection:
            if mutate:
                self.provisioner._require_monitor()
            with connection.transaction():
                yield connection

    def install(self, credential):
        self._operator(credential)
        with self._registry(mutate=True, install=True) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS onboarding182_invitations (
                invitation_id text PRIMARY KEY, command_id text NOT NULL,
                issuer text NOT NULL, subject text NOT NULL,
                customer_id text NOT NULL, provision_operation text NOT NULL,
                recipient_issuer text NOT NULL, recipient_subject text NOT NULL,
                recipient_binding text NOT NULL, expires_at double precision NOT NULL,
                issued_at double precision NOT NULL, verifier text NOT NULL,
                delivery_reference text NOT NULL UNIQUE, delivery text NOT NULL,
                state text NOT NULL, UNIQUE(issuer,subject,command_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS onboarding182_memberships (
                customer_id text PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
                recipient_binding text NOT NULL, invitation_id text NOT NULL UNIQUE)''')
            c.execute('''CREATE TABLE IF NOT EXISTS onboarding182_receipts (
                issuer text NOT NULL, subject text NOT NULL, command_id text NOT NULL,
                invitation_id text NOT NULL, verifier text NOT NULL, customer_id text NOT NULL,
                PRIMARY KEY(issuer,subject,command_id))''')
            c.execute("""ALTER TABLE provision181_operations
                ADD COLUMN IF NOT EXISTS access_state text NOT NULL DEFAULT 'active'
                    CHECK (access_state IN ('active','suspended')),
                ADD COLUMN IF NOT EXISTS access_revision bigint NOT NULL DEFAULT 0""")
            c.execute('''CREATE TABLE IF NOT EXISTS access184_commands (
                issuer text NOT NULL, subject text NOT NULL, command_id text NOT NULL,
                operation_id text NOT NULL REFERENCES provision181_operations(operation_id),
                requested_state text NOT NULL, expected_revision bigint NOT NULL,
                outcome text NOT NULL, result_revision bigint,
                PRIMARY KEY (issuer, subject, command_id))''')

            membership185.install(c)
            c.execute("""ALTER TABLE provision181_operations ADD COLUMN IF NOT EXISTS source_binding text,
                ADD COLUMN IF NOT EXISTS source_revision bigint NOT NULL DEFAULT 0""")

    def _owned_customer(self, c, principal, operation_id):
        row = c.execute("""SELECT operation_id, customer_id, state AS provisioning_state,
                access_state, access_revision AS revision FROM provision181_operations
                WHERE operation_id=%s AND subject=%s AND mode='customer' FOR UPDATE""",
                (operation_id, principal.subject)).fetchone()
        if not row:
            raise PermissionError('own customer operation required')
        return row

    @contextmanager
    def operator_source_boundary(self, credential, operation_id):
        """Order a trusted source configuration change on the operator's own active customer."""
        principal = self._operator(credential)
        self._key(operation_id)
        with self._registry(mutate=True) as c:
            owned = self._owned_customer(c, principal, operation_id)
            if owned['access_state'] != 'active':
                raise CustomerAccessDenied()
            with dispatch_gate(self.provisioner.directory, owned['customer_id']):
                yield c, owned['customer_id']

    def access_status(self, credential, operation_id):
        principal = self._operator(credential)
        self._key(operation_id)
        with self._registry() as c:
            return self._owned_customer(c, principal, operation_id)

    def _access_command(self, credential, operation_id, command_id, access_state, expected_revision):
        principal = self._operator(credential)
        if not is_command_key(operation_id) or not is_command_key(command_id):
            raise AccessBodyInvalid('bounded command identity required')
        if (access_state not in ('active', 'suspended') or type(expected_revision) is not int
                or not 0 <= expected_revision < 2**63):
            raise AccessBodyInvalid('access state and bounded current revision required')
        return AccessCommand(issuer=principal.issuer, subject=principal.subject, command_id=command_id,
                             operation_id=operation_id, requested_state=access_state, expected_revision=expected_revision)

    @staticmethod
    def _access_receipt(c, command):
        row = c.execute('''SELECT * FROM access184_commands
            WHERE issuer=%(issuer)s AND subject=%(subject)s AND command_id=%(command_id)s''', asdict(command)).fetchone()
        if row and (row['operation_id'] != command.operation_id or row['requested_state'] != command.requested_state
                    or row['expected_revision'] != command.expected_revision):
            raise AccessReceiptMismatch('original access command required')
        receipt = dict(command_id=command.command_id, operation_id=command.operation_id,
                       requested_state=command.requested_state, expected_revision=command.expected_revision,
                       state=row['outcome'] if row else 'absent', revision=row['result_revision'] if row else None)
        if receipt['state'] == 'refused':
            receipt['reason'] = ('access revision changed' if receipt['revision'] != command.expected_revision
                                 else 'customer already has the requested access state')
        return receipt

    def access_command_status(self, credential, operation_id, command_id, access_state, expected_revision):
        command = self._access_command(credential, operation_id, command_id, access_state, expected_revision)
        with self._registry() as c:
            self._owned_customer(c, self._operator(credential), operation_id)
            return self._access_receipt(c, command)

    def change_access(self, credential, operation_id, command_id, access_state, expected_revision):
        command = self._access_command(credential, operation_id, command_id, access_state, expected_revision)
        with self._registry(mutate=True) as c:
            self._owned_customer(c, self._operator(credential), operation_id)
            receipt = self._access_receipt(c, command)
            if receipt['state'] == 'absent':
                c.execute("""INSERT INTO access184_commands VALUES
                    (%(issuer)s,%(subject)s,%(command_id)s,%(operation_id)s,%(requested_state)s,%(expected_revision)s,
                     'in_progress',NULL) ON CONFLICT (issuer,subject,command_id) DO NOTHING""", asdict(command))
            elif receipt['state'] != 'in_progress':
                return receipt
        # The host gate outlives the registry COMMIT, including a lost DB session.
        # A crash between these transactions leaves an exact recoverable intent.
        with ExitStack() as ordering, self._registry(mutate=True) as c:
            customer = self._owned_customer(c, self._operator(credential), operation_id)
            receipt = self._access_receipt(c, command)
            if receipt['state'] != 'in_progress':
                return receipt
            ordering.enter_context(dispatch_gate(self.provisioner.directory, customer['customer_id']))
            revision = customer['revision']
            outcome = 'refused'
            if expected_revision == revision and customer['access_state'] != access_state:
                revision += 1
                c.execute('''UPDATE provision181_operations SET access_state=%s, access_revision=%s
                    WHERE operation_id=%s''', (access_state, revision, operation_id))
                outcome = 'confirmed'
            c.execute('''UPDATE access184_commands SET outcome=%(outcome)s, result_revision=%(revision)s
                WHERE issuer=%(issuer)s AND subject=%(subject)s AND command_id=%(command_id)s''',
                dict(asdict(command), outcome=outcome, revision=revision))
            return self._access_receipt(c, command)

    @staticmethod
    def _targets(c, principal):
        # Identity routing only. A recipient may belong to multiple customers.
        identity = (principal.issuer, principal.subject, principal.recipient)
        return c.execute('''SELECT customer_id, access_state, access_revision FROM provision181_operations
            WHERE customer_id IN (
                SELECT customer_id FROM onboarding182_memberships WHERE issuer=%s AND subject=%s AND recipient_binding=%s
                UNION SELECT customer_id FROM onboarding182_invitations
                WHERE recipient_issuer=%s AND recipient_subject=%s AND recipient_binding=%s
            )''', (*identity, *identity)).fetchall()

    def authenticate_session(self, credential):
        principal = self.identities.verify(credential)
        if principal.principal_class == 'operator':
            self._operator(credential)
            return principal, {}
        principal = self._principal(credential, 'customer')
        with self._registry() as c:
            targets = self._targets(c, principal)
            if targets and not any(row['access_state'] == 'active' for row in targets):
                raise CustomerAccessDenied('reauthentication required or customer access refused')
            revisions = {row['customer_id']: row['access_revision'] for row in targets}
            for row in targets:
                m = membership185.member(c,row['customer_id'],principal.issuer,principal.subject,principal.recipient)
                if m:
                    revisions['member:' + row['customer_id']] = m['revision']
            return principal, revisions

    @contextmanager
    def customer_request(self, credential, revisions, *, check_session=None):
        """Order customer journal actions and nested protected operations with suspension."""
        principal = self._principal(credential, 'customer')
        with self.delivery_boundary(), self._registry() as c:
            targets = self._targets(c, principal)
            if targets and not any(row['access_state'] == 'active' and
                    revisions.get(row['customer_id'], 0) == row['access_revision'] for row in targets):
                raise CustomerAccessDenied('reauthentication required or customer access refused')
            token = self._request.set(CustomerRequest(revisions, principal, check_session, c))
            try:
                yield
            finally:
                self._request.reset(token)

    @contextmanager
    def customer_access_boundary(self, customer_id):
        """Hold current active authority through a local effect, never issue a reusable permit.

        Future transport integration must initiate the handoff inside an equivalent
        live boundary. No queue, worker or transport calls this method in Ticket02B.
        """
        with self._registry() as c:
            self._require_active(c, customer_id)
            yield c

    def _invitation_target(self, c, principal, invitation_id):
        target = c.execute('''SELECT customer_id FROM onboarding182_invitations
            WHERE invitation_id=%s AND recipient_issuer=%s AND recipient_subject=%s AND recipient_binding=%s''',
            (invitation_id, principal.issuer, principal.subject, principal.recipient)).fetchone()
        return target['customer_id'] if target else None

    def acceptance_customer(self, credential, invitation_id, *, require_active=True):
        """Route only invitations belonging to the authenticated recipient."""
        principal = self._principal(credential, 'customer')
        try:
            self._key(invitation_id)
        except ValueError:
            # Historical journals may contain a rejected malformed invitation.
            if require_active:
                raise CustomerAccessDenied() from None
            return None
        with self._registry() as c:
            target = self._invitation_target(c, principal, invitation_id)
            if require_active:
                self._require_active(c, target)
            return target

    def _require_active(self, c, customer_id, *, authority=None):
        row = c.execute("""SELECT access_state, access_revision FROM provision181_operations
            WHERE customer_id=%s AND mode='customer' AND state='ready' FOR UPDATE""", (customer_id,)).fetchone()
        request = authority or self._request.get()
        if (not row or row['access_state'] != 'active' or
                (request is not None and request.revisions.get(customer_id, 0) != row['access_revision'])):
            raise CustomerAccessDenied()

        if request:
            if request.check_session:
                request.check_session()
            p = request.principal
            m = membership185.member(c,customer_id,p.issuer,p.subject,p.recipient)
            if m and (not m['active'] or request.revisions.get('member:' + customer_id, 0) != m['revision']):
                raise CustomerAccessDenied('membership changed; authenticate again')
            return m

    def provision(self, credential, operation_id):
        self._operator(credential)
        return self.provisioner.provision(credential, operation_id, mode='customer')

    def status(self, credential, operation_id):
        self._operator(credential)
        return self.provisioner.status(credential, operation_id)

    @staticmethod
    def _key(key):
        if not is_command_key(key):
            raise ValueError('bounded command identity required')

    @staticmethod
    def _view(row):
        state = row['state']
        if state == 'pending' and time.time() >= row['expires_at']:
            state = 'expired'
        return dict(invitation_id=row['invitation_id'], command_id=row['command_id'],
                    customer_id=row['customer_id'], state=state, delivery=row['delivery'],
                    delivery_reference=row['delivery_reference'], expires_at=row['expires_at'],
                    recipient={'issuer': row['recipient_issuer'], 'subject': row['recipient_subject']},
                    bundles=['Administrator'])

    def invite(self, credential, operation_id, command_id, recipient_issuer,
               recipient_subject, recipient_binding, expires_at):
        principal = self._operator(credential)
        self._key(command_id)
        if any(not isinstance(v, str) or not v or len(v) > 200
               for v in (recipient_issuer, recipient_subject, recipient_binding)):
            raise ValueError('customer-designated recipient required')
        if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at)):
            raise ValueError('explicit finite expiry required')
        self._key(operation_id)
        with self._registry(mutate=True) as c:
            customer = self._owned_customer(c, principal, operation_id)
            if customer['provisioning_state'] != 'ready':
                raise PermissionError('ready customer required')
            request = (customer['customer_id'], operation_id, recipient_issuer,
                       recipient_subject, recipient_binding, expires_at)
            row = c.execute('''SELECT * FROM onboarding182_invitations
                WHERE administrator_binding IS NULL AND issuer=%s AND subject=%s AND command_id=%s''',
                (principal.issuer, principal.subject, command_id)).fetchone()
            if row:
                original = tuple(row[k] for k in ('customer_id', 'provision_operation',
                    'recipient_issuer', 'recipient_subject', 'recipient_binding', 'expires_at'))
                if original != request:
                    raise PermissionError('original invitation command required')
                return self._view(row)
            now = time.time()
            self._require_active(c, customer['customer_id'])
            if not now < expires_at <= now + self.validity_ceiling:
                raise ValueError('expiry outside explicit finite validity ceiling')
            established = c.execute('SELECT 1 FROM onboarding182_memberships WHERE customer_id=%s', (customer['customer_id'],)).fetchone()
            live = c.execute('''SELECT 1 FROM onboarding182_invitations WHERE customer_id=%s
                AND state='pending' AND expires_at>%s''', (customer['customer_id'], now)).fetchone()
            if established or live:
                raise PermissionError('bootstrap already established or pending')
            secret = secrets.token_urlsafe(32)
            invitation_id = uuid.uuid4().hex
            reference = uuid.uuid4().hex
            inserted = c.execute('''INSERT INTO onboarding182_invitations VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'unknown','pending')
                ON CONFLICT (issuer,subject,command_id) DO NOTHING RETURNING invitation_id''',
                (invitation_id, command_id, principal.issuer, principal.subject,
                 *request[:-1], expires_at, now, hashlib.sha256(secret.encode()).hexdigest(), reference)).fetchone()
            if not inserted:
                raise PermissionError('original invitation command required')
        # Persist the exact identity/hash before the external edge. Uncertainty never
        # automatically sends again or regenerates a secret, including after restart.
        try:
            with self.customer_access_boundary(customer['customer_id']):
                self.delivery.deliver(reference, dict(invitation_id=invitation_id, secret=secret,
                    customer_id=customer['customer_id']))
        except Exception:
            return self.invitation_status(credential, command_id)
        with self._registry(mutate=True) as c:
            c.execute("UPDATE onboarding182_invitations SET delivery='delivered' WHERE invitation_id=%s", (invitation_id,))
        return self.invitation_status(credential, command_id)

    def invitation_status(self, credential, command_id):
        principal = self._operator(credential)
        self._key(command_id)
        with self._registry() as c:
            row = c.execute('''SELECT * FROM onboarding182_invitations
                WHERE administrator_binding IS NULL AND issuer=%s AND subject=%s AND command_id=%s''',
                (principal.issuer, principal.subject, command_id)).fetchone()
            return self._view(row) if row else dict(command_id=command_id, state='absent')

    def revoke(self, credential, command_id):
        principal = self._operator(credential)
        self._key(command_id)
        with self._registry(mutate=True) as c:
            row = c.execute('''SELECT * FROM onboarding182_invitations
                WHERE administrator_binding IS NULL AND issuer=%s AND subject=%s AND command_id=%s FOR UPDATE''',
                (principal.issuer, principal.subject, command_id)).fetchone()
            if not row or row['state'] == 'accepted':
                raise PermissionError('revocable own invitation required')
            c.execute("UPDATE onboarding182_invitations SET state='revoked' WHERE invitation_id=%s", (row['invitation_id'],))
            row['state'] = 'revoked'
            return self._view(row)

    @staticmethod
    def _acceptance_verifier(secret):
        if not isinstance(secret, str) or not 32 <= len(secret) <= 200:
            raise AcceptanceRefused('invitation refused')
        try:
            return hashlib.sha256(secret.encode()).hexdigest()
        except UnicodeEncodeError:
            raise AcceptanceRefused('invitation refused') from None

    @staticmethod
    def _acceptance_receipt(c, principal, command_id, invitation, verifier):
        receipt = c.execute('''SELECT * FROM onboarding182_receipts
            WHERE issuer=%s AND subject=%s AND command_id=%s''',
            (principal.issuer, principal.subject, command_id)).fetchone()
        if receipt:
            if receipt['invitation_id'] != invitation['invitation_id'] or receipt['verifier'] != verifier:
                raise AcceptanceRefused('original acceptance command required')
            if receipt['customer_id'] != invitation['customer_id']:
                raise RuntimeError('acceptance receipt binding unresolved')
        return receipt

    def accept(self, credential, invitation_id, secret, command_id):
        principal = self._principal(credential, 'customer')
        self._key(command_id)
        self._key(invitation_id)
        verifier = self._acceptance_verifier(secret)
        with self._registry(mutate=True) as c:
            self._require_active(c, self._invitation_target(c, principal, invitation_id))
            row = c.execute('''SELECT * FROM onboarding182_invitations WHERE invitation_id=%s
                AND recipient_issuer=%s AND recipient_subject=%s AND recipient_binding=%s FOR UPDATE''',
                (invitation_id, principal.issuer, principal.subject, principal.recipient)).fetchone()
            if not row:
                raise CustomerAccessDenied()
            if not hmac.compare_digest(row['verifier'], verifier):
                raise AcceptanceRefused('invitation refused')
            customer = c.execute('''SELECT state,mode FROM provision181_operations
                WHERE customer_id=%s AND operation_id=%s''',
                (row['customer_id'], row['provision_operation'])).fetchone()
            if not customer or customer['state'] != 'ready' or customer['mode'] != 'customer':
                raise PermissionError('ready customer required')
            receipt = self._acceptance_receipt(c, principal, command_id, row, verifier)
            self._authorize_acceptance(c, row, principal, receipt)
            if receipt:
                return dict(state='accepted', invitation_id=invitation_id, command_id=command_id,
                            customer_id=receipt['customer_id'])
            if row['state'] != 'pending' or time.time() >= row['expires_at']:
                raise AcceptanceRefused('invitation expired, revoked or consumed')
            if not row['administrator_binding'] and c.execute('SELECT 1 FROM onboarding182_memberships WHERE customer_id=%s', (row['customer_id'],)).fetchone():
                raise AcceptanceRefused('initial administrator already established')
            if not membership185.permitted_roles(row['roles']):
                raise AcceptanceRefused('invitation roles are no longer permitted')
            if row['recipient_revision'] is None:
                inserted = c.execute('''INSERT INTO onboarding182_memberships
                    (customer_id,issuer,subject,recipient_binding,invitation_id,roles)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING subject''',
                    (row['customer_id'], principal.issuer, principal.subject, principal.recipient,
                     invitation_id, row['roles'])).fetchone()
                if not inserted:
                    raise AcceptanceRefused('membership already exists')
            else:
                existing = membership185.member(c, row['customer_id'], principal.issuer,
                                                principal.subject, principal.recipient)
                if not existing or not existing['active'] or existing['revision'] != row['recipient_revision']:
                    raise AcceptanceRefused('invited membership has changed')
                roles = sorted(set(existing['roles']) | set(row['roles']))
                with dispatch_gate(self.provisioner.directory, row['customer_id']):
                    c.execute('''UPDATE onboarding182_memberships SET roles=%s, revision=revision+1
                        WHERE customer_id=%s AND issuer=%s AND subject=%s''',
                        (roles, row['customer_id'], principal.issuer, principal.subject))
            if row['administrator_binding']:
                c.execute('UPDATE provision181_operations SET roster_revision=roster_revision+1 WHERE customer_id=%s',(row['customer_id'],))
            c.execute("UPDATE onboarding182_invitations SET state='accepted' WHERE invitation_id=%s", (invitation_id,))
            inserted = c.execute('''INSERT INTO onboarding182_receipts VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (issuer,subject,command_id) DO NOTHING RETURNING command_id''',
                (principal.issuer, principal.subject, command_id, invitation_id, verifier, row['customer_id'])).fetchone()
            if not inserted:
                raise AcceptanceRefused('original acceptance command required')
            return dict(state='accepted', invitation_id=invitation_id, command_id=command_id,
                        customer_id=row['customer_id'])

    def acceptance_status(self, credential, invitation_id, secret, command_id):
        """Read the original receipt without replaying or consuming an invitation."""
        principal = self._principal(credential, 'customer')
        self._key(invitation_id)
        with self._registry() as c:
            self._require_active(c, self._invitation_target(c, principal, invitation_id))
            invitation = c.execute('''SELECT * FROM onboarding182_invitations
                WHERE invitation_id=%s AND recipient_issuer=%s
                AND recipient_subject=%s AND recipient_binding=%s''',
                (invitation_id, principal.issuer, principal.subject, principal.recipient)).fetchone()
            if not invitation:
                raise CustomerAccessDenied()
            if invitation['state'] != 'accepted' or not invitation['administrator_binding']:
                self._issuer_authority(c, invitation, principal)
            # Only immutable input failures under current authority settle the journal.
            try:
                self._key(command_id)
            except ValueError:
                raise AcceptanceRefused('invalid acceptance command') from None
            verifier = self._acceptance_verifier(secret)
            if not hmac.compare_digest(invitation['verifier'], verifier):
                raise AcceptanceRefused('invitation refused')
            receipt = self._acceptance_receipt(c, principal, command_id, invitation, verifier)
            self._authorize_acceptance(c, invitation, principal, receipt)
            return dict(state='accepted' if receipt else 'absent', invitation_id=invitation_id,
                        command_id=command_id, customer_id=invitation['customer_id'])

    @contextmanager
    def worker_customer_boundary(self, customer_id, *, sending=False):
        """Order a separately invoked worker on its ready tenant, including recovery after suspension."""
        if self._request.get() is not None:
            raise PermissionError('worker requires its own authority boundary')
        with self._registry(mutate=True) as registry, ExitStack() as ordering:
            select = """SELECT * FROM provision181_operations WHERE customer_id=%s
                AND mode='customer' AND state='ready' FOR UPDATE"""
            row = registry.execute(select, (customer_id,)).fetchone()
            if not row:
                raise CustomerAccessDenied('worker binding unavailable')
            if sending:
                ordering.enter_context(dispatch_gate(self.provisioner.directory, customer_id))
                row = registry.execute(select, (customer_id,)).fetchone()
                if not row:
                    raise CustomerAccessDenied('worker binding unavailable')
            yield registry, row

    @contextmanager
    def intake_membership_boundary(self, customer_id, *, mutate=False):
        """Give intake the current member and a session check for its tenant commit."""
        request = self._request.get()
        if request is None or customer_id not in request.revisions:
            raise CustomerAccessDenied()
        with self._registry() as c, (c.transaction() if mutate else nullcontext()):
            membership = self._require_active(c, customer_id)
            if not membership or not membership['active']:
                raise CustomerAccessDenied()
            def check_mutation():
                self._require_active(c, customer_id)
                self.provisioner._require_monitor()
            yield c, request.principal, membership, check_mutation

    @contextmanager
    def review_boundary(self, customer_id, roles):
        from nilaris.customer_review import CustomerReview
        request = self._request.get()
        self._key(customer_id)
        if request is None or customer_id not in request.revisions:
            raise CustomerAccessDenied()
        p = request.principal
        with self._registry() as c:
            membership = self._require_active(c, customer_id)
            if not membership or not membership['active'] or not roles.intersection(membership['roles']):
                raise CustomerAccessDenied('current expert role required')
            row = c.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',
                            (customer_id,)).fetchone()
            def check_mutation():
                self._require_active(c, customer_id)
                self.provisioner._require_monitor()
            yield CustomerReview(self.provisioner.customer_runtime(row), p, customer_id,
                                 membership['roles'], check_mutation,
                                 case_source_allowed=customer_id in self.provisioner.config.get('case001_test_customers', []))

    def landing(self, credential, customer_id):
        from nilaris.customer_review import landing_actions
        principal = self._principal(credential, 'customer')
        self._key(customer_id)
        with self.customer_access_boundary(customer_id) as c:
            row = c.execute('''SELECT m.customer_id,m.roles FROM onboarding182_memberships m
                JOIN provision181_operations p USING(customer_id)
                WHERE m.customer_id=%s AND m.issuer=%s AND m.subject=%s
                AND m.active AND m.recipient_binding=%s AND p.state='ready' AND p.mode='customer' ''',
                (customer_id, principal.issuer, principal.subject, principal.recipient)).fetchone()
            if not row:
                raise CustomerAccessDenied()
            return dict(customer_id=customer_id, principal=dict(issuer=principal.issuer, subject=principal.subject),
                        bundles=row['roles'], actions=landing_actions(row['roles']), state='active')

    def _authorize_acceptance(self, c, invitation, principal, receipt):
        if not receipt or not invitation['administrator_binding']:
            self._issuer_authority(c, invitation, principal)
        if not receipt:
            return
        member = membership185.member(c, invitation['customer_id'], principal.issuer,
                                      principal.subject, principal.recipient)
        if not member or not member['active']:
            raise CustomerAccessDenied()

    def _issuer_authority(self, c, invitation, recipient):
        if invitation['administrator_binding'] is None:
            if not self.identities.current_operator(invitation['issuer'], invitation['subject'], self.provisioner.verifier):
                raise PermissionError('issuer authority revoked')
        else:
            if not self.identities.current_customer(invitation['issuer'], invitation['subject'],
                                                    invitation['administrator_binding']):
                raise PermissionError('issuer authority revoked')
            membership185.require_admin(c, invitation['customer_id'], invitation['issuer'],
                                        invitation['subject'], invitation['administrator_binding'])
            if (recipient.issuer, recipient.subject) == (invitation['issuer'], invitation['subject']):
                raise AcceptanceRefused('another Administrator must grant personal roles')

    @contextmanager
    def management_boundary(self, credential, customer_id, *, mutate=False):
        self._key(customer_id)
        principal = self._principal(credential, 'customer')
        identity = (customer_id, principal.issuer, principal.subject, principal.recipient)
        with self._registry(mutate=mutate) as c:
            membership185.require_admin(c, *identity)
            self._require_active(c, customer_id)
            membership185.require_admin(c, *identity)
            yield c, principal

    def require_management(self, credential, customer_id):
        with self.management_boundary(credential, customer_id):
            pass

    def members(self, credential, customer_id):
        with self.management_boundary(credential, customer_id) as (c, _):
            return membership185.roster(c, customer_id)

    def management_status(self, credential, customer_id, command_id, expected_revision, command):
        self._key(command_id)
        with self.management_boundary(credential, customer_id) as (c, actor):
            return membership185.execute(self,c,actor,customer_id,command_id,expected_revision,command,inspect_only=True)

    @contextmanager
    def delivery_boundary(self):
        if self._deliveries.get() is not None:
            yield
            return
        pending = []
        token = self._deliveries.set(pending)
        try:
            yield
        finally:
            self._deliveries.reset(token)
        for customer, authority, secret, result in pending:
            invitation, reference = result['invitation_id'], result['delivery_reference']
            try:
                with self._registry(mutate=True) as c:
                    self._require_active(c, customer, authority=authority)
                    row = c.execute('SELECT * FROM onboarding182_invitations WHERE invitation_id=%s',(invitation,)).fetchone()
                    if row['state'] != 'pending' or row['expires_at'] <= time.time():
                        continue
                    if not self.identities.current_customer(row['issuer'], row['subject'], row['administrator_binding']):
                        continue
                    membership185.require_admin(c,customer,row['issuer'],row['subject'],row['administrator_binding'])
                    self.provisioner._require_monitor()
                    self.delivery.deliver(reference, dict(customer_id=customer, invitation_id=invitation, secret=secret))
                    c.execute("UPDATE onboarding182_invitations SET delivery='delivered' WHERE invitation_id=%s",(invitation,))
                result['delivery'] = 'delivered'
            except (OSError, PermissionError, ProvisioningRefused):
                continue

    def defer_delivery(self, c, actor, customer, secret, result):
        # Only identity and revisions cross the transaction boundary. Delivery opens its own connection.
        request = self._request.get()
        if request:
            authority = CustomerAuthority(request.revisions, actor, request.check_session)
        else:
            access = c.execute('SELECT access_revision FROM provision181_operations WHERE customer_id=%s',
                               (customer,)).fetchone()
            member = membership185.member(c, customer, actor.issuer, actor.subject, actor.recipient)
            authority = CustomerAuthority({customer: access['access_revision'],
                                          'member:' + customer: member['revision']}, actor, None)
        self._deliveries.get().append((customer, authority, secret, result))

    def manage(self, credential, customer_id, command_id, expected_revision, command):
        self._key(command_id)
        with self.delivery_boundary(), self.management_boundary(credential, customer_id, mutate=True) as (c, actor):
            with dispatch_gate(self.provisioner.directory, customer_id):
                return membership185.execute(self,c,actor,customer_id,command_id,expected_revision,command,inspect_only=False)
