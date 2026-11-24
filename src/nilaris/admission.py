"""Authoritative offer/resolve/status seam; acknowledgement follows commit."""
from contextlib import nullcontext
from functools import wraps
from inspect import signature
from dataclasses import asdict, dataclass, replace
import json
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from nilaris.service_monitor import STATUS_PATH, service_monitor_ready

RECEIPT_RESERVATION_BYTES = 135168  # bounded request + response + record allowance

MIGRATION = '''
CREATE TABLE IF NOT EXISTS admission_jobs (
    id text PRIMARY KEY,
    input jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'acquiring',
    reservation_bytes bigint NOT NULL
);
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS profile jsonb;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS semantic_key jsonb;
UPDATE admission_jobs SET semantic_key = jsonb_build_array(
    input->'document_id', input->'source_revision', input->'case_id', input->'membership_revision',
    input->'product_type', input->'processing_contract', input->'reference_set',
    input->'selection_policy', input->'intent_id') WHERE semantic_key IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS admission_jobs_semantic_key ON admission_jobs (semantic_key);
CREATE TABLE IF NOT EXISTS admission_receipts (
    namespace text NOT NULL,
    command_id text NOT NULL,
    request jsonb NOT NULL,
    result jsonb NOT NULL,
    PRIMARY KEY (namespace, command_id)
);
ALTER TABLE admission_receipts ADD COLUMN IF NOT EXISTS reservation_bytes bigint NOT NULL DEFAULT 135168;
'''


@dataclass(frozen=True)
class AdmissionProfile:
    """Versioned admission envelope only; execution controls require their own gates."""
    version: str = 'L1-v1'
    active_attempts: int = 1
    waiting_jobs: int = 4
    live_jobs: int = 5
    batch_members: int = 8
    metadata_bytes: int = 65536
    input_bytes: int = 1048576
    job_bytes: int = 8388608
    logical_bytes: int = 67108864
    terminal_headroom_bytes: int = 8388608

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError('explicit profile version required')
        ceilings = (1, 4, 5, 8, 65536, 1048576, 8388608, 67108864)
        names = ('active_attempts', 'waiting_jobs', 'live_jobs', 'batch_members',
                 'metadata_bytes', 'input_bytes', 'job_bytes', 'logical_bytes')
        if any(type(getattr(self, name)) is not int or not 0 < getattr(self, name) <= ceiling
               for name, ceiling in zip(names, ceilings)):
            raise ValueError('test limits must be positive and lower-only')
        if self.version == 'L1-v1' and any(getattr(self, name) != ceiling
                                          for name, ceiling in zip(names, ceilings)):
            raise ValueError('lower limits require a distinct test profile version')
        if self.terminal_headroom_bytes != 8388608:
            raise ValueError('terminal headroom cannot be borrowed by admission')
        if self.job_bytes != 8388608:
            raise ValueError('approved job reservation components require 8 MiB')

    @classmethod
    def l1(cls):
        return cls()

    def lowered_for_test(self, version, **limits):
        if version == self.version:
            raise ValueError('distinct test profile version required')
        if any(name in ('version', 'terminal_headroom_bytes') or
               name not in asdict(self) or type(value) is not int or value > getattr(self, name)
               for name, value in limits.items()):
            raise ValueError('test limits must be lower-only')
        return replace(self, version=version, **limits)


class _ConnectionOptions(dict):
    def __repr__(self):
        return '<PostgreSQL connection options; credentials redacted>'


def migrate(database):
    """Prepare a task database; dependency diagnostics never cross this boundary."""
    database = _ConnectionOptions(database)
    try:
        with psycopg.connect(**database) as connection:
            connection.execute(MIGRATION)
            from nilaris.execution import MIGRATION as EXECUTION_MIGRATION
            connection.execute(EXECUTION_MIGRATION)
            from nilaris.review import MIGRATION as REVIEW_MIGRATION
            connection.execute(REVIEW_MIGRATION)
            from nilaris.discovery import MIGRATION as DISCOVERY_MIGRATION
            connection.execute(DISCOVERY_MIGRATION)
            from nilaris.drive155_journey import MIGRATION as SOURCE_MIGRATION
            connection.execute(SOURCE_MIGRATION)
            from nilaris.scoped_intake import MIGRATION as SCOPED_MIGRATION
            connection.execute(SCOPED_MIGRATION)
            from nilaris.minimized_evidence import MIGRATION as MINIMIZED_MIGRATION
            connection.execute(MINIMIZED_MIGRATION)
        return
    except psycopg.Error:
        pass
    # Raise outside the handler so no dependency exception is retained as context.
    raise RuntimeError('migration persistence_unavailable; retry migration') from None


def _persistence_boundary(operation):
    @wraps(operation)
    def guarded(self, *args, **kwargs):
        try:
            return operation(self, *args, **kwargs)
        except psycopg.Error:
            # Preserve caller identities, never infer a durable disposition or coverage.
            supplied = signature(operation).bind(self, *args, **kwargs).arguments
            affected = _affected_identities(supplied)
            # Commit errors are uncertain too: never manufacture a rejection.
            reason, next_action = (
                ('job status unavailable', 'retry_status') if operation.__name__ == 'status'
                else ('durable outcome unavailable', 'retry_same_key'))
            return dict(known=False, category='persistence_unavailable',
                        affected=affected, coverage='unknown',
                        reason=reason, next_action=next_action)
    return guarded


def _affected_identities(supplied):
    # Invalid/unconfigured callers have not passed metadata validation. Omit
    # identities that cannot fit; coverage remains explicitly unknown.
    affected = {key: supplied[key] for key in ('namespace', 'command_id', 'job_id')
                if isinstance(supplied.get(key), str) and len(supplied[key]) <= 65536}
    if isinstance(supplied.get('members'), list):
        affected['member_keys'] = [item['member_key'] for item in supplied['members'][:8]
                                   if isinstance(item, dict) and isinstance(item.get('member_key'), str)
                                   and len(item['member_key']) <= 65536]
    return affected if len(json.dumps(affected, ensure_ascii=True)) <= 65536 else {}


def scoped_namespace_refusal():
    return dict(known=True, category='reserved_namespace', coverage='unknown',
                reason='Customer intake commands require the authenticated scoped service.',
                next_action='use_customer_intake')


class Ingestion:
    def __init__(self, database, profile=None, *, monitor_path=STATUS_PATH, drive_policy=None, semantic172=False, scoped_binding=None):
        if type(semantic172) is not bool:
            raise ValueError('explicit semantic172 flag')
        self.scoped_binding = scoped_binding
        self.semantic172 = semantic172
        self.database = _ConnectionOptions(database)
        self.profile = profile
        self.monitor_path = monitor_path
        self.drive_policy = drive_policy

    def owns_input(self, member):
        return member.get('scoped_intake_binding') == self.scoped_binding

    @_persistence_boundary
    def offer(self, namespace, command_id, members, *, _discovery=False, _transaction=None):
        affected = _affected_identities(dict(namespace=namespace, command_id=command_id, members=members))
        if not isinstance(self.profile, AdmissionProfile):
            return dict(known=True, category='configuration_unavailable',
                        affected=affected, coverage='unknown',
                        reason='explicit versioned admission profile required',
                        next_action='configure_approved_profile')
        if isinstance(namespace, str) and namespace.startswith('scoped-intake:') and (
                self.scoped_binding is None or _transaction is None):
            return scoped_namespace_refusal()
        if isinstance(namespace, str) and namespace.startswith('discovery:') and not _discovery:
            return dict(known=True, category='reserved_namespace', coverage='unknown',
                        reason='Discovery admission keys are journal owned.', next_action='use_discovery_admit')
        category = None
        if not isinstance(members, list) or not 0 < len(members) <= self.profile.batch_members:
            category = 'batch_limit'
        elif (not isinstance(namespace, str) or not namespace.strip() or
              not isinstance(command_id, str) or not command_id.strip() or
              any(not isinstance(member, dict) for member in members)):
            category = 'invalid_request'
        else:
            try:
                size = 0
                for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(
                        dict(namespace=namespace, command_id=command_id, members=members)):
                    size += len(chunk)
                    if size > self.profile.metadata_bytes:
                        category = 'metadata_limit'
                        break
            except (TypeError, ValueError, RecursionError):
                category = 'invalid_request'
            keys = [member.get('member_key') for member in members]
            if any(not isinstance(key, str) for key in keys):
                category = 'invalid_request'
            elif len(set(keys)) != len(keys):
                category = 'invalid_request'
            elif sum(len(json.dumps(key, ensure_ascii=True)) for key in keys) * 2 + 8192 > 131072:
                # Member identifiers appear in both disposition and affected identities.
                category = 'metadata_limit'
        if category:
            return dict(known=True, category=category, receipt_stored=False,
                        affected=affected, coverage='unknown',
                        reason='offer outside bounded metadata contract',
                        next_action='correct_request_and_retry')
        monitor_unavailable = dict(known=False, category='service_monitor_unavailable',
                        affected=dict(namespace=namespace, command_id=command_id,
                                      member_keys=keys), coverage='unknown',
                        receipt_stored=False, reason='fresh live task service monitor required',
                        next_action='restore_task_monitor_then_resolve_or_retry_same_key')
        if not service_monitor_ready(self.monitor_path):
            return monitor_unavailable
        with (nullcontext(_transaction) if _transaction is not None else
              psycopg.connect(**self.database, row_factory=dict_row)) as connection:
            connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            if not service_monitor_ready(self.monitor_path):
                return monitor_unavailable
            prior = connection.execute(
                'SELECT request, result, request=%s AS same_request FROM admission_receipts WHERE namespace=%s AND command_id=%s',
                (Jsonb(members), namespace, command_id)).fetchone()
            if prior:
                if prior['same_request']:
                    return prior['result']
                return dict(known=True, category='command_conflict',
                            affected=affected, coverage='unknown',
                            reason='command key already has different content',
                            next_action='use_original_content_or_new_command')
            usage = _admission_usage(connection)
            ordinary_bytes = self.profile.logical_bytes - self.profile.terminal_headroom_bytes
            reserved = usage['job_reservation_bytes'] + usage['receipt_reservation_bytes']
            # Request metadata bounds both copies of member keys in expanded errors.
            # Keep the existing minimum; larger metadata must pay its conservative
            # request + response + record allowance before any work is accepted.
            receipt_bytes = max(RECEIPT_RESERVATION_BYTES, 3 * size + 8192)
            store_receipt = reserved + receipt_bytes <= ordinary_bytes
            if self.scoped_binding is not None and not store_receipt:
                # Even a canonical existing Job needs room to bind this new key.
                return dict(known=True, category='receipt_capacity', receipt_stored=False)
            if store_receipt:
                reserved += receipt_bytes
            outcomes = []
            for member in members:
                if not self.owns_input(member):
                    outcomes.append(_nonacceptance(member['member_key'], 'rejected', 'source_policy_denied',
                                                   'correct_input_and_retry_new_command'))
                    continue
                if (self.drive_policy is not None or member.get('connector') in ('synthetic-drive-v1', 'drive-readonly-v1')) and (
                        self.drive_policy is None or not self.drive_policy.permits(member)):
                    outcomes.append(_nonacceptance(member.get('member_key'), 'rejected',
                        'source_policy_denied', 'correct_input_and_retry_new_command'))
                    continue
                invalid = _member_disposition(member, self.profile, semantic172=self.semantic172)
                if invalid:
                    outcomes.append(invalid)
                    continue
                semantic_key = [member[key] for key in (
                    'document_id', 'source_revision', 'case_id', 'membership_revision',
                    'product_type', 'processing_contract', 'reference_set', 'selection_policy', 'intent_id')]
                existing = connection.execute(
                    '''SELECT coalesce(j.canonical_job_id,j.id) AS id, coalesce(c.profile,j.profile) AS profile,
                    CASE WHEN j.input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s
                         THEN j.input END AS original_input
                    FROM admission_jobs j LEFT JOIN admission_jobs c ON c.id=j.canonical_job_id
                    WHERE j.semantic_key=%s''', (self.scoped_binding,Jsonb(semantic_key))).fetchone()
                if existing and existing['original_input'] is None:
                    outcomes.append(_nonacceptance(member['member_key'], 'rejected', 'source_policy_denied',
                                                   'correct_input_and_retry_new_command'))
                    continue
                input_limit = existing['profile']['input_bytes'] if existing else self.profile.input_bytes
                if (type(member.get('input_bytes')) is not int or
                        not 0 < member['input_bytes'] <= input_limit):
                    outcomes.append(_nonacceptance(member['member_key'], 'rejected', 'input_limit',
                                                  'correct_input_and_retry_new_command'))
                    continue
                if existing:
                    if (any(label in (member['connector'], existing['original_input'].get('connector'))
                            for label in ('synthetic-drive-v1', 'drive-readonly-v1'))
                            and any(member.get(key) != existing['original_input'].get(key) for key in (
                                'connector', 'source_root', 'source_id', 'source_revision', 'expected_sha256', 'input_bytes'))):
                        outcomes.append(_nonacceptance(member['member_key'], 'rejected', 'source_pin_conflict',
                                                      'preserve_original_pin_and_review_source'))
                        continue
                    outcomes.append(dict(member_key=member['member_key'], disposition='existing',
                                         job_id=existing['id'], reason='canonical_intent', next_action='query_job'))
                    continue
                reason = None
                if not store_receipt or reserved + self.profile.job_bytes > ordinary_bytes:
                    reason = 'byte_capacity'
                elif usage['waiting_jobs'] >= self.profile.waiting_jobs:
                    reason = 'waiting_capacity'
                elif usage['live_jobs'] >= self.profile.live_jobs:
                    reason = 'live_capacity'
                if reason:
                    outcomes.append(_nonacceptance(member['member_key'], 'deferred', reason,
                                                  'retry_new_command_when_capacity_available'))
                    continue
                job_id = str(uuid.uuid4())
                connection.execute(
                    'INSERT INTO admission_jobs (id, input, reservation_bytes, profile, semantic_key) VALUES (%s,%s,%s,%s,%s)',
                    (job_id, Jsonb(member), self.profile.job_bytes, Jsonb(asdict(self.profile)), Jsonb(semantic_key)))
                outcomes.append(dict(member_key=member['member_key'], disposition='accepted',
                                     job_id=job_id, next_action='query_job'))
                usage['waiting_jobs'] += 1
                usage['live_jobs'] += 1
                reserved += self.profile.job_bytes
            result = dict(known=True, receipt_stored=store_receipt, members=outcomes, totals=dict(
                offered=len(members), accepted=sum(x['disposition'] in ('accepted', 'existing') for x in outcomes),
                deferred=sum(x['disposition'] == 'deferred' for x in outcomes),
                rejected=sum(x['disposition'] == 'rejected' for x in outcomes)))
            if store_receipt:
                connection.execute(
                    'INSERT INTO admission_receipts (namespace, command_id, request, result, reservation_bytes) VALUES (%s,%s,%s,%s,%s)',
                    (namespace, command_id, Jsonb(members), Jsonb(result), receipt_bytes))
        return result

    @_persistence_boundary
    def resolve(self, namespace, command_id):
        if isinstance(namespace, str) and namespace.startswith('scoped-intake:'):
            return scoped_namespace_refusal()
        with psycopg.connect(**self.database) as connection:
            row = connection.execute(
                'SELECT result FROM admission_receipts WHERE namespace=%s AND command_id=%s',
                (namespace, command_id)).fetchone()
        return row[0] if row else dict(
            known=False, category='unknown', affected=dict(namespace=namespace, command_id=command_id),
            coverage='unknown', reason='No durable offer receipt is available.', next_action='retry_same_key')

    @_persistence_boundary
    def status(self, job_id):
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            row = connection.execute('''
                SELECT id, input, created_at, state, reservation_bytes, profile
                FROM admission_jobs WHERE id=%s AND input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s''',
                (job_id,self.scoped_binding)).fetchone()
            if row:
                row['admission'] = _admission_usage(connection)
                row['admission']['terminal_headroom_bytes'] = row['profile']['terminal_headroom_bytes']
                row['created_at'] = row['created_at'].isoformat()
                row['known'] = True
                row['coverage'] = dict(acquisition='pending', processing='not_started')
                row['counters'] = dict(attempts=0, captured_input_bytes=0)
                row['next_action'] = 'await_pinned_acquisition'
                from nilaris.execution import project
                project(connection, row)
                return row
        return dict(known=True, category='not_found', affected=dict(job_id=job_id),
                    coverage='unknown',
                    reason='job not found', next_action='check_job_identity')


    @_persistence_boundary
    def discovery_stage(self, namespace, page_id, expected_cursor, next_cursor, members):
        from nilaris.discovery import stage
        return stage(self, namespace, page_id, expected_cursor, next_cursor, members)

    @_persistence_boundary
    def discovery_admit(self, namespace, page_id):
        from nilaris.discovery import admit
        return admit(self, namespace, page_id)

    @_persistence_boundary
    def discovery_reconcile(self, namespace, page_id):
        from nilaris.discovery import reconcile
        return reconcile(self, namespace, page_id)

    @_persistence_boundary
    def discovery_read(self, namespace, page_id):
        from nilaris.discovery import read
        return read(self, namespace, page_id)

    @_persistence_boundary
    def cancel(self, job_id, expected_revision):
        from nilaris.execution import cancel
        return cancel(self, job_id, expected_revision)

    @_persistence_boundary
    def claim(self, job_id, expected_revision):
        from nilaris.execution import claim
        return claim(self, job_id, expected_revision)


    @_persistence_boundary
    def capture(self, job_id, claim, source):
        from nilaris.execution import capture
        return capture(self, job_id, claim, source)


    @_persistence_boundary
    def publish(self, job_id, claim, output):
        from nilaris.execution import publish
        return publish(self, job_id, claim, output)

    @_persistence_boundary
    def result(self, job_id, cursor=None, proposal_id=None, provenance=False):
        from nilaris.execution import result
        return result(self, job_id, cursor, proposal_id, provenance)

    @_persistence_boundary
    def retry(self, job_id, expected_revision, expected_generation, actor, reason):
        from nilaris.execution import retry
        return retry(self, job_id, expected_revision, expected_generation, actor, reason)

    @_persistence_boundary
    def reconcile(self, job_id, expected_revision, expected_generation, actor, reason, disposition=None):
        from nilaris.execution import reconcile
        return reconcile(self, job_id, expected_revision, expected_generation, actor, reason, disposition)

    @_persistence_boundary
    def reprocess(self, namespace, command_id, job_id, expected_revision, expected_generation, actor, reason):
        from nilaris.execution import reprocess
        return reprocess(self, namespace, command_id, job_id, expected_revision,
                         expected_generation, actor, reason)

    @_persistence_boundary
    def run_fixture(self, job_id, expected_revision):
        """Process the Job's captured bytes through the supervised fixture worker."""
        from nilaris.worker import run_fixture
        return run_fixture(self, job_id, expected_revision)

    @_persistence_boundary
    def run_controlled(self, job_id, expected_revision, output, *, test_wall_seconds=30, test_delay_seconds=0,
                       test_cpu_seconds=20, test_rss_bytes=1024**3, test_workload='literal'):
        """Run labeled controlled test data through the bounded graph; no extraction."""
        from nilaris.worker import run_controlled
        return run_controlled(self, job_id, expected_revision, output,
                              test_wall_seconds=test_wall_seconds, test_delay_seconds=test_delay_seconds,
                              test_cpu_seconds=test_cpu_seconds, test_rss_bytes=test_rss_bytes,
                              test_workload=test_workload)


def _admission_usage(connection):
    usage = connection.execute('''
        SELECT count(*) FILTER (WHERE state IN ('acquiring', 'queued', 'held') AND NOT held_active_reservation) AS waiting_jobs,
               count(*) FILTER (WHERE state='running' OR held_active_reservation) AS active_capacity_reservations,
               count(*) FILTER (WHERE state NOT IN ('completed', 'cancelled', 'quarantined', 'failed', 'partial')) AS live_jobs,
               coalesce(sum(reservation_bytes), 0)::bigint AS job_reservation_bytes,
               coalesce(extract(epoch FROM clock_timestamp() - min(created_at)
                   FILTER (WHERE state IN ('acquiring', 'queued', 'held') AND NOT held_active_reservation)), 0)::float8 AS oldest_waiting_seconds
        FROM admission_jobs''').fetchone()
    usage['receipt_reservation_bytes'] = connection.execute(
        'SELECT coalesce(sum(reservation_bytes),0)::bigint AS bytes FROM admission_receipts').fetchone()['bytes']
    review_bytes = connection.execute("""SELECT
        coalesce((SELECT sum(reservation_bytes) FROM review_publication_bases),0) +
        coalesce((SELECT sum(reservation_bytes) FROM review_drafts),0) +
        coalesce((SELECT sum(reservation_bytes) FROM review_revisions),0) +
        coalesce((SELECT sum(reservation_bytes) FROM review_approvals),0) +
        coalesce((SELECT sum(reservation_bytes) FROM review_receipts),0) AS bytes""").fetchone()['bytes']
    usage['review_retained_bytes'] = int(review_bytes)
    journal_bytes = connection.execute('''SELECT
        coalesce((SELECT sum(reservation_bytes) FROM discovery_pages),0) +
        coalesce((SELECT sum(reservation_bytes) FROM discovery_streams),0) AS bytes''').fetchone()['bytes']
    usage['discovery_retained_bytes'] = int(journal_bytes)
    usage['receipt_reservation_bytes'] += int(journal_bytes)
    source = connection.execute('''SELECT coalesce(sum(reservation_bytes),0) AS bytes,
        count(*) FILTER (WHERE active AND (job_id IS NULL OR NOT EXISTS
            (SELECT 1 FROM admission_jobs j WHERE j.id=drive155_operations.job_id
             AND (j.state='running' OR j.held_active_reservation)))) AS active
        FROM drive155_operations''').fetchone()
    usage['source_retained_bytes'] = int(source['bytes'])
    usage['receipt_reservation_bytes'] += int(source['bytes'])
    from nilaris.scoped_intake import policy_reserved_bytes
    usage['policy_retained_bytes'] = policy_reserved_bytes(connection)
    usage['receipt_reservation_bytes'] += usage['policy_retained_bytes']
    usage['active_capacity_reservations'] += source['active']
    return usage


def _nonacceptance(member_key, disposition, reason, next_action):
    messages = {
        'source_pin_conflict': 'The admitted source revision already has a different immutable source pin.',
        'source_policy_denied': 'Source root, file, revision or explicit mapping is outside the configured policy.',
        'invalid_identity': 'Required source, membership or processing identity is missing or invalid.',
        'identity_unresolved': 'Source identity must be resolved before a Job can be accepted.',
        'unsupported_input': 'Only supported synthetic PDF or XLSX input is eligible.',
        'unsupported_context': 'Only the approved ordinary synthetic processing context is supported.',
        'input_limit': 'Declared input size is invalid or exceeds the approved input limit.',
        'byte_capacity': 'Available storage reservation capacity is insufficient.',
        'waiting_capacity': 'Accepted waiting work has reached the configured count limit.',
        'live_capacity': 'Accepted live work has reached the configured count limit.',
    }
    return dict(member_key=member_key, disposition=disposition, known=True,
                category=reason, reason=reason, message=messages[reason],
                affected=dict(member_key=member_key),
                coverage=dict(acquisition='not_started', processing='not_started'),
                next_action=next_action)


def _member_disposition(member, profile, *, semantic172=False):
    identities = ('member_key', 'connector', 'source_id', 'source_revision', 'document_id',
                  'case_id', 'membership_revision', 'product_type', 'processing_contract',
                  'reference_set', 'selection_policy', 'intent_id')
    reason = None
    disposition = 'rejected'
    unmapped_ambiguous = (member.get('identity') == 'ambiguous' and
                          'document_id' in member and member['document_id'] is None)
    if any(not isinstance(member.get(key), str) or not member[key].strip()
           for key in identities if not (key == 'document_id' and unmapped_ambiguous)):
        reason = 'invalid_identity'
    elif member.get('identity') != 'resolved':
        reason, disposition = 'identity_unresolved', 'deferred'
    elif member['connector'] not in ('synthetic', 'synthetic-drive-v1', 'drive-readonly-v1') or member.get('media_type') not in (
            'application/pdf', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
        reason = 'unsupported_input'
    elif (member['reference_set'] != 'none' or member['selection_policy'] != 'none'
          or not (member['processing_contract'] == 'fixture-v1' or
              (semantic172 and member['connector'] == 'synthetic' and
               member['processing_contract'] == 'semantic172-v1')) or member['intent_id'] != 'ordinary'):
        reason = 'unsupported_context'
    if reason:
        return _nonacceptance(member.get('member_key'), disposition, reason,
                              'correct_input_and_retry_new_command')
