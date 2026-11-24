"""Authenticated review of a selected proposal inside one provisioned customer DB."""
import hashlib
import inspect
import json
import math
import re
import secrets
from decimal import Decimal, InvalidOperation

from nilaris.results import canonical
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from nilaris.admission import Ingestion
from nilaris.review import Review
from nilaris.rfq_stage_decision import IMPORT_CONTRACT


READ_ROLES = frozenset({'Reviewer', 'Approver'})
MUTATING = {'review_create': 'Reviewer', 'review_save': 'Reviewer', 'review_stage_add': 'Reviewer', 'review_stage_save': 'Reviewer', 'review_stage_bulk': 'Reviewer', 'review_approve': 'Approver'}
READING = {'review_schema_preview', 'review_stage_read', 'review_stage_source', 'review_read', 'review_resume', 'result', 'review_incoming'}
ROUTES = {'/expert/dashboard', '/expert/context', '/expert/command', '/expert/prepare', '/expert/resolve',
          '/expert/retry', '/expert/acknowledge', '/expert/unlock', '/expert/reconcile', '/expert/local-status'}
MAX_BODY = 73728
PAGE_SIZE = 24


def representable(value):
    """PostgreSQL text and JSONB require Unicode scalar values excluding U+0000."""
    remaining = [value]
    while remaining:
        item = remaining.pop()
        if isinstance(item, str) and re.search(r'[\x00\ud800-\udfff]', item):
            return False
        if isinstance(item, dict):
            remaining.extend(item.keys())
            remaining.extend(item.values())
        elif isinstance(item, list):
            remaining.extend(item)
    return True


def decode_request(raw):
    """Refuse wire JSON whose intent the existing command representation would lose."""
    def exact_float(token):
        value = float(token)
        if not math.isfinite(value) or Decimal(token) != Decimal(str(value)):
            raise ValueError('unsupported review command number')
        return value

    def reject_constant(token):
        raise ValueError('nonfinite review command number')

    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate review command field')
        return result

    try:
        return json.loads(raw, parse_float=exact_float, parse_constant=reject_constant,
                          object_pairs_hook=unique_object)
    except (InvalidOperation, RecursionError) as error:
        raise ValueError('unsupported review command JSON') from error


@dataclass(frozen=True)
class ReviewJournal:
    customer_id: str
    job_id: str
    role: str = ''

    @property
    def suffix(self):
        return '-review-' + digest([self.customer_id, self.job_id, self.role])


def digest(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


def command_fingerprint(command):
    """Hash JSON types and exact decimal values, independent of Python/JS number spelling."""
    def typed(value):
        if isinstance(value, dict):
            keys = sorted(value, key=lambda k: k.encode('utf-16-be', 'surrogatepass'))
            return ['object', [[key, typed(value[key])] for key in keys]]
        if isinstance(value, list):
            return ['array', [typed(item) for item in value]]
        if value is None or isinstance(value, (str, bool)):
            return value
        number = Decimal(str(value))
        if not number:
            return ['number', '0']
        sign, digits, exponent = number.as_tuple()
        coefficient = ''.join(map(str, digits))
        significant = coefficient.rstrip('0')
        exponent += len(coefficient) - len(significant)
        return ['number', ('-' if sign else '') + significant + 'e' + str(exponent)]
    return hashlib.sha256(canonical(typed(command)).encode('ascii')).hexdigest()


def same_command(left, right):
    return command_fingerprint(left) == command_fingerprint(right)


def journal_tag(command):
    return hashlib.sha256(canonical(command).encode()).hexdigest()


def validate_key(key):
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise ValueError('bounded command identity required')


def landing_actions(bundles):
    actions = ['administrative_metadata'] if 'Administrator' in bundles else []
    actions.extend(operation for operation in sorted(READING | MUTATING.keys())
                   if required_roles(operation).intersection(bundles))
    return actions


def required_roles(operation):
    if operation == 'review_schema_preview':
        return {'Approver'}
    if operation in MUTATING:
        return {MUTATING[operation]}
    if operation in ('result', 'review_incoming', 'review_stage_read'):
        return {'Reviewer'}
    if operation in ('review_read', 'review_resume', 'review_stage_source'):
        return READ_ROLES
    raise ValueError('unsupported customer review operation')


class CustomerReview:
    """A request-local handle, used only while the registry's customer row is locked."""
    def __init__(self, database, principal, customer, roles, check_mutation, *, case_source_allowed=False):
        self.review = Review(database, check_mutation=check_mutation, case_source_allowed=case_source_allowed)
        self.ingestion = Ingestion(database)
        self.actor = json.dumps([principal.issuer, principal.subject], ensure_ascii=False, separators=(',', ':'))
        self.identity = [customer, principal.issuer, principal.subject, principal.recipient]
        self.roles = roles

    def dashboard(self, cursor):
        after = ''
        owner = digest(['dashboard', *self.identity])
        if cursor is not None:
            validate_key(cursor)
            prefix, separator, after = cursor.partition(':')
            if prefix != owner or not separator or not after:
                raise ValueError('invalid dashboard cursor')
        rows = self.proposals(after=after)
        visible = rows[:PAGE_SIZE]
        for row in visible:
            row['product_type'] = row['product_type'][:160]
        more = owner + ':' + visible[-1]['job_id'] if len(rows) > PAGE_SIZE else None
        return {'state': 'available', 'proposals': visible, 'next_cursor': more, 'roles': self.roles}

    def proposals(self, job_id=None, *, after=''):
        with psycopg.connect(**self.review.database, row_factory=dict_row) as connection:
            return connection.execute("""
                SELECT p.job_id, p.id AS proposal_id,
                       CASE WHEN p.payload->>'adapter'=%s THEN 'review-first-b' END AS workspace, p.payload->'context'->>'product_type' AS product_type,
                       d.id AS draft_id, d.revision, b.base_version
                FROM job_publications p
                LEFT JOIN review_publication_bases b ON b.proposal_id=p.id
                LEFT JOIN LATERAL (
                    SELECT r.id, r.revision FROM review_drafts r LEFT JOIN review_approvals a ON a.id=r.approval_id
                    WHERE r.proposal_id=p.id ORDER BY a.version DESC NULLS FIRST LIMIT 1
                ) d ON true
                WHERE p.payload->>'kind'='initial' AND (%s::text IS NULL OR p.job_id=%s)
                    AND (%s OR d.id IS NOT NULL) AND p.job_id>%s
                ORDER BY p.job_id LIMIT %s""", (IMPORT_CONTRACT, job_id, job_id, 'Reviewer' in self.roles, after, PAGE_SIZE + 1)).fetchall()

    def namespace(self, job):
        return 'authenticated-review:' + digest([*self.identity, job])

    def local_receipt(self, job, selector):
        fingerprint = selector.get('fingerprint')
        operation = selector['operation'].removeprefix('review_')
        key = selector['command_id']
        if not representable(key):
            # No value in the PostgreSQL text primary key can equal this identity.
            return {'state': 'absent'}
        # Only an exact, currently authorized operation can supply body or receipt data.
        # Read the stored decimal spelling, without rounding arbitrary JSONB numbers to floats.
        with psycopg.connect(**self.review.database) as connection:
            row = connection.execute("""
                SELECT CASE WHEN permitted THEN request::text END, CASE WHEN permitted THEN result END
                FROM (SELECT request, result,
                    (%s AND request->>'operation'=%s AND request->>'actor'=%s) AS permitted
                    FROM review_receipts WHERE namespace=%s AND command_id=%s AND job_id=%s) receipt
                """, (fingerprint is not None, operation, self.actor, self.namespace(job), key, job)).fetchone()
        if row is None:
            return {'state': 'absent'}
        if fingerprint is None:
            return {'state': 'occupied'}
        if row[0] is not None:
            stored = json.loads(row[0], parse_float=Decimal, parse_int=Decimal)
            stored.pop('actor')
            stored.update(operation=selector['operation'], command_id=key)
            if secrets.compare_digest(fingerprint, command_fingerprint(stored)):
                return {'state': 'matching', 'receipt': row[1]}
        return {'state': 'different'}

    def prepare(self, job, command):
        operation = command['operation']
        if not required_roles(operation).intersection(self.roles):
            raise PermissionError('current expert role required')
        arguments = {k: v for k, v in command.items() if k != 'operation'}
        if {'actor', 'namespace', 'customer_id', 'credential', 'receipt_namespace', 'receipt_operations'} & arguments.keys():
            raise ValueError('server-derived authority required')
        if 'job_id' in arguments and operation != 'review_incoming' and arguments['job_id'] != job:
            raise PermissionError('proposal access refused')
        if operation in MUTATING:
            validate_key(arguments.get('command_id'))
            arguments.update(actor=self.actor, namespace=self.namespace(job))
        if operation in ('review_read', 'review_resume'):
            arguments['receipt_namespace'] = self.namespace(job)
            arguments['receipt_operations'] = sorted(name.removeprefix('review_') for name, role in MUTATING.items()
                                                    if role in self.roles)
        if operation == 'result':
            method = self.ingestion.result
        else:
            name = 'read_incoming' if operation == 'review_incoming' else operation.removeprefix('review_')
            method = getattr(self.review, name)
        inspect.signature(method).bind(**arguments)
        if 'draft_id' in arguments:
            with psycopg.connect(**self.review.database) as connection:
                if not connection.execute('SELECT 1 FROM review_drafts WHERE job_id=%s AND id=%s',
                                          (job, arguments['draft_id'])).fetchone():
                    raise PermissionError('proposal access refused')
        return method, arguments


def handle(path, body, service, principal, journal, locks):
    """Called inside the existing authenticated session and customer_request transaction."""
    # Legacy recovery may compare an old incompatible key in memory, never in SQL.
    recovery = path in ('/expert/local-status', '/expert/resolve', '/expert/retry', '/expert/acknowledge')
    if not representable({key: value for key, value in body.items() if not (recovery and key == 'command_id')}):
        raise ValueError('unsupported review text')
    customer = body['customer_id']
    if path == '/expert/dashboard':
        if set(body) not in ({'customer_id'}, {'customer_id', 'cursor'}):
            raise ValueError('select a customer')
        with service.review_boundary(customer, READ_ROLES) as workspace:
            return workspace.dashboard(body.get('cursor')), None

    expected_scope = None
    if 'expected_scope' in body:
        expected_scope = body['expected_scope']
        validate_key(expected_scope)
        body = {key: value for key, value in body.items() if key != 'expected_scope'}
    job = body['job_id']
    validate_key(job)
    selector = {'customer_id', 'job_id'}
    submitting = path in ('/expert/command', '/expert/prepare', '/expert/reconcile')
    prepared = path == '/expert/command' and body.get('prepared') is True
    if submitting:
        expected = selector | {'command', 'prepared'} if prepared else selector | {'command'}
        if set(body) != expected or not isinstance(body['command'], dict):
            raise ValueError('review command required')
        command = body['command']
        roles = required_roles(command['operation'])
        if (prepared or path != '/expert/command') and command['operation'] not in MUTATING:
            raise ValueError('only review mutations can be staged')
    elif path == '/expert/local-status':
        if 'fingerprint' in body:
            selector.add('fingerprint')
            fingerprint = body['fingerprint']
            if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
                raise ValueError('SHA-256 fingerprint required')
        if set(body) != selector | {'operation', 'command_id'} or body['operation'] not in MUTATING:
            raise ValueError('exact mutation identity required')
        validate_key(body['command_id'])
        roles = required_roles(body['operation'])
    elif path == '/expert/unlock':
        if set(body) != selector | {'role'} or body['role'] not in READ_ROLES:
            raise ValueError('expert journal role required')
        roles = {body['role']}
    else:
        expected = selector if path == '/expert/context' else selector | {'command_id'}
        if path in ('/expert/retry', '/expert/acknowledge') or (path == '/expert/resolve' and 'tag' in body):
            expected = expected | {'tag'}
        if set(body) != expected:
            raise ValueError('invalid review selector')
        roles = READ_ROLES

    with service.review_boundary(customer, roles) as workspace:
        # A different tab may replace the origin-wide cookie after the editor opened.
        # Its original scope constrains the already-authorized request; it grants nothing.
        if expected_scope is not None and expected_scope != workspace.namespace(job):
            raise PermissionError('editor identity changed; reopen the proposal')
        proposals = workspace.proposals(job)
        if not proposals:
            raise PermissionError('proposal access refused')
        if path == '/expert/unlock':
            role = body['role']
            key_channel = ReviewJournal(customer, job, 'browser-key-' + role)
            locks.enter_context(journal.locked(principal, key_channel))
            saved = journal.read(principal, channel=key_channel)
            if saved is None:
                saved = {'key': secrets.token_hex(32)}
                journal.save(principal, saved, channel=key_channel)
            # The browser stores only the opaque slot, IV and authenticated ciphertext.
            return dict(saved, scope=workspace.namespace(job), slot=digest([workspace.namespace(job), role])), None
        channel = ReviewJournal(customer, job)
        if path == '/expert/command' and command['operation'] in READING:
            method, arguments = workspace.prepare(job, command)
            return method(**arguments), None

        # Serialize every pending review command for this person and proposal. Role-specific
        # files let recovery check current permission before opening a private command body.
        locks.enter_context(journal.locked(principal, channel))
        original = None
        for role in sorted(READ_ROLES.intersection(workspace.roles)):
            permitted_channel = ReviewJournal(customer, job, role)
            original = journal.read(principal, channel=permitted_channel)
            if original is not None:
                channel = permitted_channel
                break
        tag = journal_tag(original) if original is not None else None
        if 'tag' in body and body['tag'] != tag:
            raise ValueError('server journal changed; inspect the original again')
        if path == '/expert/local-status':
            # A discard is safe only when there is no pending operation for this user/job,
            # including a currently inaccessible role, and no durable receipt at this key.
            occupied = original is not None or any(journal.exists(principal, channel=ReviewJournal(customer, job, role))
                                                    for role in READ_ROLES.difference(workspace.roles))
            if occupied:
                return {'state': 'occupied'}, None
            return workspace.local_receipt(job, body), None
        if path == '/expert/context':
            pending = None
            if original:
                pending = {'command_id': original['command_id'], 'operation': original['operation']}
                if original['operation'] in ('review_stage_add', 'review_stage_save', 'review_stage_bulk'):
                    pending = dict(original)
            return dict(proposals[0], actor=workspace.actor, roles=workspace.roles, pending=pending,
                        pending_tag=tag, request_limit_bytes=MAX_BODY, scope=workspace.namespace(job)), None

        if submitting:
            method, arguments = workspace.prepare(job, command)
            if path == '/expert/reconcile':
                if original is not None and not same_command(original, command):
                    return {'known': False, 'category': 'other_pending'}, None
                result = resolve(workspace, job, command)
                return dict(result, journal_pending=original is not None, journal_tag=tag), (
                    channel if original is not None and result.get('known') is True else None)
            if original is None and prepared:
                raise PermissionError('original staged command required')
            if original is None:
                for missing_role in READ_ROLES.difference(workspace.roles):
                    if journal.exists(principal, channel=ReviewJournal(customer, job, missing_role)):
                        raise PermissionError('original pending review permission required')
                channel = ReviewJournal(customer, job, MUTATING[command['operation']])
            journal.save(principal, command, channel=channel)
            if path == '/expert/prepare':
                return {'state': 'prepared' if original is None else 'pending', 'journal_tag': tag or journal_tag(command)}, None
        else:
            if not original or original['command_id'] != body['command_id']:
                raise PermissionError('original pending command required')
            if MUTATING.get(original['operation']) != channel.role:
                raise PermissionError('original expert permission required')
            supported = representable(original)
            if supported:
                method, arguments = workspace.prepare(job, original)
            if path == '/expert/acknowledge':
                journal.acknowledge(principal, channel=channel)
                return {'state': 'acknowledged'}, None
            if not supported:
                # Compare the exact prior command with any receipt without binding its body.
                status = workspace.local_receipt(job, dict(operation=original['operation'],
                    command_id=original['command_id'], fingerprint=command_fingerprint(original)))
                if status['state'] == 'matching':
                    return dict(status['receipt'], journal_tag=tag), channel
                if status['state'] not in ('absent', 'different'):
                    raise RuntimeError('unresolved private review command')
                return dict(known=True, saved=False, category='unrepresentable_command', journal_tag=tag), channel
        if original is None:
            result = method(**arguments)
        else:
            result = resolve(workspace, job, original)
            if (prepared or path == '/expert/retry') and result.get('category') == 'unknown_command':
                # Preserve the original private body even when a client resubmits an equivalent number spelling.
                if prepared:
                    method, arguments = workspace.prepare(job, original)
                result = method(**arguments)
        return dict(result, journal_tag=tag or journal_tag(command)), channel if result.get('known') is True else None


def resolve(workspace, job, original):
    operation = original['operation'].removeprefix('review_')
    request = {k: v for k, v in original.items() if k not in ('operation', 'command_id')}
    request.update(operation=operation, actor=workspace.actor)
    return workspace.review.resolve(workspace.namespace(job), original['command_id'], expected_request=request)
