"""Controlled synthetic intake. Only registry-bound customer databases receive business data."""
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import os
import stat
import re
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.dispatch_gate import dispatch_gate
from nilaris.admission import AdmissionProfile, Ingestion, _admission_usage
from nilaris.intake167 import plan_digest
from nilaris.local_transport import MAX_BYTES
from nilaris.onboarding182 import CustomerAccessDenied, is_command_key


# Logical reservation includes an allowance for row/index overhead and copies
# of the complete policy and original command identity. PostgreSQL computes it
# identically on upgrade of already existing rows and on every future insert.
POLICY_BYTES = """8192::bigint + 3 * (octet_length(issuer)::bigint + octet_length(subject)
    + octet_length(command_id) + octet_length(policy::text))"""

POLICY_MIGRATION = f"""
CREATE TABLE IF NOT EXISTS intake_policies (
    revision bigint PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
    command_id text NOT NULL, policy jsonb NOT NULL, UNIQUE(issuer,subject,command_id));
ALTER TABLE intake_policies ADD COLUMN IF NOT EXISTS reservation_bytes bigint
    GENERATED ALWAYS AS ({POLICY_BYTES}) STORED;
"""

MIGRATION = POLICY_MIGRATION + """
ALTER TABLE admission_receipts ADD COLUMN IF NOT EXISTS initial_source_pin text
    CHECK (initial_source_pin ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX IF NOT EXISTS intake_single_initial_source ON admission_receipts ((true))
    WHERE initial_source_pin IS NOT NULL;
CREATE TABLE IF NOT EXISTS scoped_jobs (
    job_id text PRIMARY KEY REFERENCES admission_jobs(id), customer_id text NOT NULL,
    binding text NOT NULL, source_revision bigint NOT NULL, access_revision bigint NOT NULL, member_revision bigint NOT NULL,
    policy_revision bigint NOT NULL, source_key text NOT NULL, content_pin text NOT NULL,
    operation_id text NOT NULL UNIQUE, state text NOT NULL, reason text NOT NULL,
    claim jsonb, receipt jsonb);
"""

CONTROLLED_ROUTE = dict(provider='controlled-local', model='sha256-byte-count-v1',
                        endpoint='local-receiver-v1', region='local', features=['digest'],
                        purpose='synthetic-intake', content_class='synthetic-technical-pdf',
                        retention='digest-only', training='disabled', subprocessors=[])
SOURCE_KEYS = ('pump-sheet', 'valve-sheet')
CUSTOMER_ID = re.compile('[a-f0-9]{32}')
STATUS_SELECT = """SELECT s.job_id,s.operation_id,s.job_id AS reservation_id,j.reservation_bytes,
    s.state,s.reason,s.source_key,s.policy_revision,s.receipt
    FROM scoped_jobs s JOIN admission_jobs j ON j.id=s.job_id"""


@dataclass(frozen=True)
class SourceBinding:
    issuer: str
    subject: str
    root: str
    recipient: str

    def __post_init__(self):
        if (not all(isinstance(value, str) and 0 < len(value) <= 200 and '\x00' not in value
                    for value in (self.issuer, self.subject, self.recipient)) or not is_command_key(self.root)):
            raise ValueError('invalid source authority')

    def matches(self, principal):
        return (self.issuer, self.subject, self.recipient) == (
            principal.issuer, principal.subject, principal.recipient)

    def digest(self, customer):
        return plan_digest([customer, asdict(self)])


@dataclass(frozen=True)
class SourceAuthority:
    pin: str | None
    revision: int
    needs_operator: bool = False


def current_source(c, row):
    if row['source_binding'] is not None:
        return SourceAuthority(row['source_binding'],row['source_revision'])
    initial = c.execute("""SELECT initial_source_pin FROM admission_receipts
        WHERE initial_source_pin IS NOT NULL""").fetchone()
    if initial:
        return SourceAuthority(initial['initial_source_pin'],1)
    # Older receipts can predate the durable marker. Their command bodies
    # cannot grant a root authority; explicit operator binding is required.
    existing = c.execute("""SELECT 1 FROM admission_receipts
        WHERE namespace LIKE 'scoped-intake:%' LIMIT 1""").fetchone()
    return SourceAuthority(None,0,needs_operator=bool(existing))


def controlled_source(source_key):
    if source_key not in SOURCE_KEYS:
        raise ValueError('unknown synthetic source')
    directory = Path(__file__).with_name('scoped_fixtures')
    root = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(source_key + '.pdf', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
                raise ValueError('bounded fixture file required')
            content = stream.read(MAX_BYTES+1)
            if len(content) != info.st_size:
                raise ValueError('fixture changed during read')
            return content
    finally:
        os.close(root)


def policy_reserved_bytes(c):
    # Ordinary callers may still use a pre-Ticket03 database. Account any
    # existing policy rows without installing or altering that database.
    schema = c.execute("""SELECT to_regclass('intake_policies') IS NOT NULL AS present,
        EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('intake_policies')
        AND attname='reservation_bytes' AND NOT attisdropped) AS migrated""").fetchone()
    if not schema['present']:
        return 0
    charge = 'reservation_bytes' if schema['migrated'] else POLICY_BYTES
    return int(c.execute(f'SELECT coalesce(sum({charge}),0) AS bytes FROM intake_policies').fetchone()['bytes'])


def current_policy(c):
    return c.execute('SELECT revision,policy FROM intake_policies ORDER BY revision DESC LIMIT 1'
                     ).fetchone() or dict(revision=0, policy=None)


def policy_hold(policy):
    if policy is None:
        return 'disclosure_policy_absent'
    if not policy['active']:
        return 'disclosure_policy_inactive'
    if policy['route'] != CONTROLLED_ROUTE:
        return 'disclosure_route_incompatible'
    return None


def validate_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {'active','route','evidence_ref'}
            or type(policy['active']) is not bool or not is_command_key(policy['evidence_ref'])
            or not isinstance(policy['route'], dict) or set(policy['route']) != set(CONTROLLED_ROUTE)):
        raise ValueError('explicit policy, evidence reference and complete route required')
    for name, reference in CONTROLLED_ROUTE.items():
        value = policy['route'][name]
        values = value if isinstance(reference, list) and isinstance(value, list) else [value]
        if (type(reference) != type(value) or len(values) > 16 or
                not all(isinstance(v, str) and 0 < len(v) <= 150 and '\x00' not in v for v in values)):
            raise ValueError('unsupported policy setting')


class IntakeReceiptCapacity(Exception):
    """Roll back an attempted admission which cannot retain its original receipt."""


class ScopedIntake:
    def __init__(self, service, bindings, *, profile=None):
        self.profile = AdmissionProfile.l1() if profile is None else profile
        if not isinstance(self.profile, AdmissionProfile):
            raise ValueError('versioned intake profile required')
        self.service = service
        self.bindings = dict(bindings)
        if not all(isinstance(k, str) and CUSTOMER_ID.fullmatch(k) and isinstance(v, SourceBinding)
                   for k, v in self.bindings.items()):
            raise ValueError('invalid customer source configuration')

    @contextmanager
    def _scope(self, customer, action, *, mutate=False):
        with self.service.intake_membership_boundary(customer, mutate=mutate) as boundary:
            registry, actor, membership, check_mutation = boundary
            binding = self.bindings.get(customer)
            row = registry.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',
                                   (customer,)).fetchone()
            pin = binding.digest(customer) if binding else None
            administrator = 'Administrator' in membership['roles']
            source_actor = bool(binding and binding.matches(actor))
            if (action == 'intake' and not source_actor) or (action == 'policy_admin' and not administrator):
                raise CustomerAccessDenied()
            if action == 'capabilities' and not source_actor:
                yield dict(policy_admin=administrator,intake=False,source_keys=[])
                return
            gate = dispatch_gate(self.service.provisioner.directory, customer) if mutate else nullcontext()
            with gate, self.database(row) as c:
                revision = None
                if action in ('capabilities','intake'):
                    source = current_source(c,row)
                    permitted = not source.needs_operator and source.pin in (None,pin)
                    if action == 'capabilities':
                        yield dict(policy_admin=administrator,intake=permitted,
                                   source_keys=list(SOURCE_KEYS) if permitted else [])
                        return
                    if not permitted:
                        raise CustomerAccessDenied()
                    revision = source.revision
                yield c, actor, row['access_revision'], membership['revision'],revision
                if mutate:
                    check_mutation()

    def intent_authority(self, customer, kind):
        action = 'intake' if kind == 'admit' else 'policy_admin'
        with self._scope(customer, action) as (c, _, _, _, revision):
            if kind == 'admit':
                return self.bindings[customer].digest(customer) + '-' + str(max(revision, 1)), current_policy(c)['revision']
            return 'policy', None

    def database(self, row):
        return psycopg.connect(**self.service.provisioner.customer_runtime(row), row_factory=dict_row,
                               options='-c lock_timeout=3000 -c statement_timeout=10000')

    def bind_source(self, credential, operation_id, expected_revision):
        """Activate only this host's configured source; never accept an actor/root from a request."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('source revision required')
        with self.service.operator_source_boundary(credential, operation_id) as (c, customer):
            if customer not in self.bindings:
                raise CustomerAccessDenied()
            pin = self.bindings[customer].digest(customer)
            row = c.execute('SELECT * FROM provision181_operations WHERE customer_id=%s', (customer,)).fetchone()
            with self.database(row) as tenant:
                source = current_source(tenant,row)
            if source.revision != expected_revision or source.pin == pin:
                raise ValueError('stale or unchanged source binding')
            return c.execute('''UPDATE provision181_operations SET source_binding=%s,source_revision=%s
                WHERE customer_id=%s RETURNING source_revision AS revision''',
                (pin,expected_revision+1,customer)).fetchone()

    def capabilities(self, customer):
        with self._scope(customer, 'capabilities') as capabilities:
            return capabilities

    def policy(self, customer):
        with self._scope(customer, 'policy_admin') as (c, *_):
            return current_policy(c)

    @staticmethod
    def _policy_receipt(c, actor, key):
        if not is_command_key(key):
            raise ValueError('original policy command required')
        return c.execute('SELECT revision,policy FROM intake_policies WHERE issuer=%s AND subject=%s AND command_id=%s',
                         (actor.issuer, actor.subject, key)).fetchone()

    def policy_status(self, customer, command_id):
        with self._scope(customer, 'policy_admin') as (c, actor, *_):
            return self._policy_receipt(c, actor, command_id) or dict(state='absent')

    def set_policy(self, customer, command_id, expected_revision, policy):
        with self._scope(customer, 'policy_admin', mutate=True) as (c, actor, *_):
            validate_policy(policy)
            if not is_command_key(command_id) or type(expected_revision) is not int or expected_revision < 0:
                raise ValueError('versioned policy command required')
            c.execute('SELECT pg_advisory_xact_lock(714012001)')
            prior = self._policy_receipt(c, actor, command_id)
            if prior:
                if prior['revision'] != expected_revision + 1 or prior['policy'] != policy:
                    raise ValueError('original policy command required')
                return prior
            if current_policy(c)['revision'] != expected_revision:
                raise ValueError('stale disclosure policy revision')
            self.service.provisioner._require_monitor()
            result = c.execute('''INSERT INTO intake_policies (revision,issuer,subject,command_id,policy)
                VALUES (%s,%s,%s,%s,%s) RETURNING revision,policy''',
                (expected_revision+1,actor.issuer,actor.subject,command_id,Jsonb(policy))).fetchone()
            usage = _admission_usage(c)
            if usage['job_reservation_bytes'] + usage['receipt_reservation_bytes'] > (
                    self.profile.logical_bytes - self.profile.terminal_headroom_bytes):
                # Raising inside the tenant transaction discards the proposed row.
                raise ValueError('disclosure policy byte capacity exceeded')
            return result

    def _admission_receipt(self, c, actor, customer, command_id):
        if not is_command_key(command_id):
            raise ValueError('intake command identity required')
        namespace = 'scoped-intake:' + plan_digest([customer, actor.issuer, actor.subject, actor.recipient])
        binding = self.bindings[customer].digest(customer)
        prior = c.execute("""SELECT CASE WHEN request->0->>'scoped_intake_binding'=%s
            THEN jsonb_build_object('source_key',request->0->>'source_id',
                                    'original_revision',request->0->'scoped_policy_revision','member',result->'members'->0)
            END AS original FROM admission_receipts WHERE namespace=%s AND command_id=%s""",
            (binding,namespace,command_id)).fetchone()
        if prior and prior['original'] is None:
            raise CustomerAccessDenied('scoped admission unavailable')
        return namespace, prior['original'] if prior else None

    def _original_outcome(self, c, customer, key, source_key, member, original_revision):
        if member['disposition'] in ('deferred','rejected'):
            return dict(state=member['disposition'],category=member['category'],reason=member['message'],
                        command_id=key,source_key=source_key,policy_revision=original_revision,receipt_stored=True,next_action=member['next_action'])
        return self._status(c,customer,member['job_id'],self.bindings[customer].digest(customer))

    def admission_status(self, customer, command_id):
        with self._scope(customer, 'intake') as (c, actor, *_):
            _, prior = self._admission_receipt(c, actor, customer, command_id)
            if not prior:
                return dict(state='absent')
            return self._original_outcome(c,customer,command_id,**prior)

    def admit(self, customer, command_id, source_key, *, expected_policy_revision=None):
        try:
            return self._admit(customer, command_id, source_key, expected_policy_revision)
        except IntakeReceiptCapacity:
            return dict(state='refused',command_id=command_id,receipt_stored=False,
                        reason='Insufficient storage capacity for the original intake receipt.')

    def _admit(self, customer, command_id, source_key, expected_policy_revision):
        with self._scope(customer, 'intake', mutate=True) as (c, actor, access, member_revision, source_revision):
            binding = self.bindings[customer].digest(customer)
            namespace, prior = self._admission_receipt(c, actor, customer, command_id)
            if prior:
                if prior['source_key'] != source_key or (expected_policy_revision is not None and
                        prior['original_revision'] != expected_policy_revision):
                    raise ValueError('original intake command and source binding required')
                return self._original_outcome(c,customer,command_id,**prior)
            content = controlled_source(source_key)
            policy = current_policy(c)
            if expected_policy_revision is not None and expected_policy_revision != policy['revision']:
                raise ValueError('disclosure policy changed before admission')
            pin = hashlib.sha256(content).hexdigest()
            first_source = source_revision == 0
            if first_source:
                source_revision = 1
            stream = plan_digest([binding, source_revision, policy['revision'], access, member_revision])
            item = dict(member_key=source_key, connector='synthetic', source_id=source_key, scoped_intake_binding=binding,
                        scoped_policy_revision=policy['revision'], source_revision=pin, document_id=source_key, case_id=stream,
                        membership_revision=str(member_revision), product_type='synthetic equipment',
                        processing_contract='fixture-v1', reference_set='none', selection_policy='none',
                        intent_id='ordinary', media_type='application/pdf', identity='resolved',
                        input_bytes=len(content))
            ingestion = Ingestion(c.info.get_parameters(), self.profile, scoped_binding=binding,
                                  monitor_path=self.service.provisioner.config['service_monitor_path'])
            result = ingestion.offer(namespace, command_id, [item], _transaction=c)
            if result.get('category') == 'receipt_capacity':
                raise IntakeReceiptCapacity
            member = result.get('members', [{}])[0]
            if result.get('receipt_stored') is not True:
                raise RuntimeError('original intake receipt was not established')
            if first_source:
                # The server's verified source pin shares the first original
                # receipt's transaction and fixed record/index allowance.
                marked = c.execute("""UPDATE admission_receipts SET initial_source_pin=%s
                    WHERE namespace=%s AND command_id=%s""",(binding,namespace,command_id)).rowcount
                if marked != 1:
                    raise RuntimeError('original source receipt unavailable')
            if member['disposition'] == 'accepted':
                job = member['job_id']
                reason = policy_hold(policy['policy'])
                c.execute("""INSERT INTO scoped_jobs
                    (job_id,customer_id,binding,access_revision,member_revision,policy_revision,source_key,
                     content_pin,operation_id,state,reason,source_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (job,customer,binding,access,member_revision,policy['revision'],source_key,pin,
                     uuid.uuid4().hex,'held' if reason else 'queued',reason or 'awaiting_worker',source_revision))
                if reason:
                    c.execute("UPDATE admission_jobs SET state='held',reason=%s,revision=revision+1 WHERE id=%s",
                              (reason,job))
            return self._original_outcome(c,customer,command_id,source_key,member,policy['revision'])

    @staticmethod
    def _status(c, customer, job_id, binding):
        result = c.execute(STATUS_SELECT + ' WHERE s.customer_id=%s AND s.job_id=%s AND s.binding=%s',
                           (customer, job_id, binding)).fetchone()
        if not result:
            raise CustomerAccessDenied('scoped job unavailable')
        return result

    def jobs(self, customer):
        with self._scope(customer, 'intake') as (c, *_):
            rows = c.execute(STATUS_SELECT + ' WHERE s.customer_id=%s AND s.binding=%s ORDER BY j.created_at DESC LIMIT 50',
                             (customer, self.bindings[customer].digest(customer))).fetchall()
            return dict(jobs=rows)

    def job(self, customer, job_id):
        with self._scope(customer, 'intake') as (c, *_):
            return self._status(c, customer, job_id, self.bindings[customer].digest(customer))

    def worker(self, customer):
        from nilaris.scoped_worker import ScopedWorker
        if customer not in self.bindings:
            raise CustomerAccessDenied('scoped worker unavailable')
        return ScopedWorker(self, customer, self.bindings[customer])
