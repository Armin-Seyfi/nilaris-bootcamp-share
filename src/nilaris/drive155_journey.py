"""Durable authority-bound source operations. Ordinary listeners stay disabled."""
import hashlib
import time

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.drive155_supervision import SupervisedSource
from nilaris.execution import current
from nilaris.service_monitor import service_monitor_ready

MIGRATION = '''
CREATE TABLE IF NOT EXISTS drive155_operations (
    namespace text NOT NULL, operation_id text NOT NULL, kind text NOT NULL,
    request jsonb NOT NULL, state text NOT NULL DEFAULT 'pending',
    result jsonb, identity jsonb, claim jsonb,
    job_id text REFERENCES admission_jobs(id), generation bigint,
    reserved_requests integer NOT NULL, reservation_bytes bigint NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY(namespace,operation_id)
);
ALTER TABLE drive155_operations ADD COLUMN IF NOT EXISTS accounting jsonb;
ALTER TABLE drive155_operations ADD COLUMN IF NOT EXISTS reserved_cpu_seconds integer NOT NULL DEFAULT 20;
-- Earlier interrupted calls could record operation accounting before charging
-- the Job attempt. Retain the allowance across either historical crash window
-- without inventing a measurement or refunding any prior charge.
UPDATE job_attempts a SET cpu_seconds=greatest(a.cpu_seconds,20),
    cpu_accounting='conservative_source_group_allowance'
    FROM drive155_operations o WHERE o.kind='acquire'
    AND a.id=o.claim->>'attempt_id' AND a.job_id=o.job_id;
CREATE UNIQUE INDEX IF NOT EXISTS drive155_one_discovery
    ON drive155_operations(namespace) WHERE kind='discover';
CREATE UNIQUE INDEX IF NOT EXISTS drive155_one_acquisition
    ON drive155_operations(job_id,generation) WHERE kind='acquire';
'''
OPERATION_BYTES = 1024**2
SOURCE_CPU_SECONDS = 20


def _unknown_accounting(cpu_seconds=SOURCE_CPU_SECONDS):
    return dict(proved=False, evidence='source_accounting_unknown', wall_seconds=30,
                measured_cpu_seconds=0, charged_cpu_seconds=cpu_seconds,
                peak_rss_bytes=1024**3, reason='source_accounting_unknown')


def disabled():
    return dict(known=True, category='live_source_disabled', coverage='unknown',
                reason='Source dispatch requires separately confirmed private activation.',
                next_action='retain_offline_mode')


def refusal(category, known=True):
    return dict(known=known, category=category, coverage='unknown',
                reason='Source operation cannot advance: ' + category,
                next_action='read_original_operation_and_job')


def _valid(*values):
    return all(isinstance(value, str) and value.strip() and len(value) <= 256 for value in values)


def _load(connection, namespace, operation_id):
    return connection.execute('SELECT * FROM drive155_operations WHERE namespace=%s AND operation_id=%s',
                              (namespace, operation_id)).fetchone()


def _project(row):
    result = dict(known=True, category=('source_operation_completed' if row['state'] == 'completed'
                  else 'source_operation_held'), state=row['state'], namespace=row['namespace'],
                  operation_id=row['operation_id'], reserved_requests=row['reserved_requests'],
                  reservation_bytes=row['reservation_bytes'], active_capacity_reserved=row['active'],
                  reserved_cpu_seconds=row['reserved_cpu_seconds'],
                  coverage='complete' if row['state'] == 'completed' else 'unknown',
                  next_action='read_original_page_or_job')
    result.update({k: v for k, v in row['request'].items() if k in ('page_id', 'job_id')})
    if row['result']:
        result.update(row['result'])
    result['accounting'] = row.get('accounting') or _unknown_accounting(row['reserved_cpu_seconds'])
    return result


class DriveJourney:
    """Explicitly injected sources only; operation replay never dispatches again.

    `sources` maps nonsecret binding IDs to AuthorizedDriveSource instances.
    Credential suppliers run only in the supervised provider process. This
    object has no ambient credential lookup or default network activation.
    """
    def __init__(self, ingestion, sources, *, runner=None):
        self.ingestion = ingestion
        self.sources = dict(sources)
        self.runner = runner or SupervisedSource()

    def permits(self, member):
        return any(source.permits(member) for source in self.sources.values())

    def drive_operation(self, binding_id, operation_id):
        source = self.sources.get(binding_id)
        if source is None or not _valid(operation_id):
            return refusal('source_binding_unavailable')
        with psycopg.connect(**self.ingestion.database, row_factory=dict_row) as connection:
            row = _load(connection, source.namespace, operation_id)
        # Attest the listener's current binding even before an operation exists.
        # The namespace hashes the complete v1 configuration and owner grant.
        return (_project(row) if row else
                dict(refusal('source_operation_unknown', False), namespace=source.namespace))

    def _reserve(self, source, operation_id, kind, request, claim=None):
        from nilaris.admission import _admission_usage
        profile = self.ingestion.profile
        if profile is None or not service_monitor_ready(self.ingestion.monitor_path):
            return refusal('service_monitor_unavailable', False), False
        with psycopg.connect(**self.ingestion.database, row_factory=dict_row) as connection:
            connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            prior = _load(connection, source.namespace, operation_id)
            if prior:
                return (_project(prior) if prior['kind'] == kind and prior['request'] == request
                        else refusal('source_operation_conflict')), False
            if connection.execute("SELECT 1 FROM drive155_operations WHERE namespace=%s AND kind='discover'", (source.namespace,)).fetchone() and kind == 'discover':
                return refusal('original_discovery_required'), False
            usage = _admission_usage(connection)
            # An acquisition already owns its Job's active slot. The source
            # operation adds durable bytes, but never a second active slot.
            own_slot = 1 if claim else 0
            if usage['active_capacity_reservations'] - own_slot >= profile.active_attempts:
                return refusal('active_capacity'), False
            if usage['job_reservation_bytes'] + usage['receipt_reservation_bytes'] + OPERATION_BYTES > profile.logical_bytes - profile.terminal_headroom_bytes:
                return refusal('journal_capacity'), False
            if not service_monitor_ready(self.ingestion.monitor_path):
                return refusal('service_monitor_unavailable', False), False
            if claim:
                job = current(connection, claim['job_id'], claim, 'acquisition')
                if not job or not source.permits(job['input']):
                    return refusal('stale_commit'), False
                total = connection.execute('SELECT coalesce(sum(acquisition_requests),0) AS n FROM job_attempts WHERE job_id=%s', (job['id'],)).fetchone()['n']
                if total + 5 > 12:
                    return refusal('resource_exhausted'), False
                connection.execute('''UPDATE job_attempts SET acquisition_requests=acquisition_requests+5,
                    acquisition_read_token=%s,cpu_seconds=cpu_seconds+%s,
                    cpu_accounting='conservative_source_group_allowance' WHERE id=%s''',
                    (operation_id, SOURCE_CPU_SECONDS, claim['attempt_id']))
            connection.execute('''INSERT INTO drive155_operations(namespace,operation_id,kind,request,
                claim,job_id,generation,reserved_requests,reservation_bytes,reserved_cpu_seconds,accounting)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (source.namespace, operation_id, kind, Jsonb(request), Jsonb(claim) if claim else None,
                 claim['job_id'] if claim else None, claim['generation'] if claim else None,
                 5 if claim else 4, OPERATION_BYTES, SOURCE_CPU_SECONDS, Jsonb(_unknown_accounting())))
        return None, True

    def _run(self, source, operation_id, provider, claim=None):
        def gate(identity):
            with psycopg.connect(**self.ingestion.database, row_factory=dict_row) as connection:
                connection.execute('SELECT pg_advisory_xact_lock(714012001)')
                row = _load(connection, source.namespace, operation_id)
                if not row or row['state'] != 'pending' or row['identity'] is not None:
                    return False
                if claim and not current(connection, claim['job_id'], claim, 'acquisition'):
                    return False
                connection.execute('UPDATE drive155_operations SET identity=%s WHERE namespace=%s AND operation_id=%s',
                                   (Jsonb(identity), source.namespace, operation_id))
                if claim:
                    connection.execute('UPDATE job_attempts SET worker=%s WHERE id=%s', (Jsonb(identity), claim['attempt_id']))
            return True
        last_heartbeat = 0
        def heartbeat():
            nonlocal last_heartbeat
            if not service_monitor_ready(self.ingestion.monitor_path):
                return False
            if not claim or time.monotonic() - last_heartbeat < 1:
                return True
            from nilaris.worker import heartbeat as beat
            last_heartbeat = time.monotonic()
            return beat(self.ingestion, claim['job_id'], claim)
        try:
            outcome = self.runner.run(provider, gate, heartbeat)
        except Exception:
            outcome = dict(proved=False, value=None, wall_seconds=30, cpu_seconds=20,
                           peak_rss_bytes=1024**3, evidence='source_accounting_unknown')
        accounting = dict(proved=outcome['proved'], evidence=outcome['evidence'],
                          wall_seconds=outcome['wall_seconds'],
                          measured_cpu_seconds=outcome['cpu_seconds'] if outcome['proved'] else 0,
                          charged_cpu_seconds=20, peak_rss_bytes=outcome['peak_rss_bytes'],
                          reason='source_returned_and_reaped' if outcome['proved'] else 'source_accounting_unknown')
        with psycopg.connect(**self.ingestion.database) as connection:
            connection.execute('''UPDATE drive155_operations SET accounting=%s
                WHERE namespace=%s AND operation_id=%s AND state='pending' ''',
                (Jsonb(accounting), source.namespace, operation_id))
        return outcome

    def _finish(self, source, operation_id, state, result=None):
        with psycopg.connect(**self.ingestion.database, row_factory=dict_row) as connection:
            connection.execute('''UPDATE drive155_operations SET state=%s,result=%s,active=%s
                WHERE namespace=%s AND operation_id=%s AND state='pending' ''',
                (state, Jsonb(result) if result else None, state != 'completed', source.namespace, operation_id))
        return self.drive_operation(next(k for k, v in self.sources.items() if v is source), operation_id)

    def drive_discover(self, binding_id, operation_id, page_id, expected_namespace=None):
        source = self.sources.get(binding_id)
        if source is None or not _valid(operation_id, page_id):
            return refusal('source_binding_unavailable')
        # Resolve once: both the namespace fence and every effect use this
        # source, even if the listener's binding map is subsequently replaced.
        if expected_namespace is not None and expected_namespace != source.namespace:
            return refusal('source_namespace_mismatch')
        request = dict(page_id=page_id)
        prior, reserved = self._reserve(source, operation_id, 'discover', request)
        if not reserved:
            return prior
        outcome = self._run(source, operation_id, source.discover)
        members = outcome['value']
        if (not outcome['proved'] or not isinstance(members, list) or len(members) != 1
                or not all(source.permits(m) for m in members)):
            return self._finish(source, operation_id, 'held')
        page = self.ingestion.discovery_stage(source.namespace, page_id, None, None, members)
        if page.get('state') not in ('staged', 'settled'):
            return self._finish(source, operation_id, 'held')
        return self._finish(source, operation_id, 'completed', dict(page=page))

    def drive_admit(self, binding_id, operation_id, page_id):
        operation = self.drive_operation(binding_id, operation_id)
        if operation.get('state') != 'completed' or operation.get('page_id') != page_id or 'page' not in operation:
            return refusal('original_completed_page_required')
        receipt = self.ingestion.discovery_admit(operation['namespace'], page_id)
        if receipt.get('receipt_stored') is True:
            self.ingestion.discovery_reconcile(operation['namespace'], page_id)
        return receipt

    def drive_acquire(self, binding_id, operation_id, job_id, expected_revision, expected_namespace=None):
        source = self.sources.get(binding_id)
        if source is None or not _valid(operation_id, job_id) or type(expected_revision) is not int:
            return refusal('source_binding_unavailable')
        if expected_namespace is not None and expected_namespace != source.namespace:
            return refusal('source_namespace_mismatch')
        # Resolve the original identity before claiming a newer Job generation.
        with psycopg.connect(**self.ingestion.database, row_factory=dict_row) as connection:
            prior = _load(connection, source.namespace, operation_id)
            if prior:
                return _project(prior) if prior['request'] == dict(job_id=job_id, expected_revision=expected_revision) else refusal('source_operation_conflict')
            if connection.execute("SELECT 1 FROM drive155_operations WHERE job_id=%s AND kind='acquire'", (job_id,)).fetchone():
                return refusal('original_acquisition_required')
        before = self.ingestion.status(job_id)
        if before.get('state') != 'acquiring' or not source.permits(before.get('input')):
            return refusal('source_policy_denied')
        owned = self.ingestion.claim(job_id, expected_revision)
        if not owned.get('claimed'):
            return owned
        prior, reserved = self._reserve(source, operation_id, 'acquire',
                                       dict(job_id=job_id, expected_revision=expected_revision), owned)
        if not reserved:
            # Claim has been durably spent; leave it for explicit reconciliation.
            return prior
        member = before['input']
        outcome = self._run(source, operation_id,
            lambda: source.read(member['source_id'], member['source_revision'], member['input_bytes']), owned)
        job = self._capture(source, operation_id, owned, member, outcome)
        if job.get('state') in ('queued', 'completed'):
            return self._finish(source, operation_id, 'completed', dict(job=job))
        return self._finish(source, operation_id, 'held', dict(job=job))

    def _capture(self, source, operation_id, claim, member, outcome):
        from nilaris.execution import conflict, retained_usage
        job_id = claim['job_id']
        with psycopg.connect(**self.ingestion.database, row_factory=dict_row) as connection:
            connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            row = _load(connection, source.namespace, operation_id)
            if not row or row['claim'] != claim or row['state'] != 'pending':
                return conflict(job_id, 'stale_commit')
            proved = outcome['proved'] and row['identity'] == outcome.get('identity')
            connection.execute('''UPDATE job_attempts SET wall_seconds=wall_seconds+%s,
                measured_cpu_seconds=measured_cpu_seconds+%s,
                peak_rss_bytes=greatest(peak_rss_bytes,%s),accounting_complete=%s,
                cpu_accounting=%s,completion_evidence=%s WHERE id=%s AND token=%s''',
                (outcome['wall_seconds'], outcome['cpu_seconds'] if proved else 0,
                 outcome['peak_rss_bytes'], proved, 'conservative_source_group_allowance',
                 'process_group_reaped' if proved else None, claim['attempt_id'], claim['token']))
            job = current(connection, job_id, claim, 'acquisition')
            if not job:
                connection.execute('UPDATE job_attempts SET stopped=%s WHERE id=%s', (proved, claim['attempt_id']))
                # The ordinary current() fence deliberately rejects unknown
                # accounting. Revoke only this exact still-owned generation;
                # a reviewed/newer generation keeps its own state untouched.
                reason = 'source_claim_lost' if proved else 'source_accounting_unknown'
                connection.execute('''UPDATE admission_jobs j SET state='held',reason=%s,
                    held_active_reservation=true,revision=revision+1,generation=generation+1
                    WHERE j.id=%s AND j.state='running' AND j.generation=%s AND EXISTS
                    (SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.id=%s
                     AND a.generation=%s AND a.token=%s)''',
                    (reason, job_id, claim['generation'], claim['attempt_id'], claim['generation'], claim['token']))
                return conflict(job_id, 'stale_commit')
            content = outcome['value']
            valid = (proved and source.permits(job['input']) and job['input'] == member
                     and isinstance(content, bytes) and len(content) == member['input_bytes']
                     and hashlib.sha256(content).hexdigest() == member['expected_sha256'])
            connection.execute('SAVEPOINT source_capture')
            state, reason = 'held', 'source_accounting_unknown' if not proved else 'source_version_unavailable'
            if valid:
                pin = member['expected_sha256']
                key = [pin if k == 'content_pin' else member[k] for k in (
                    'document_id','content_pin','case_id','membership_revision','product_type',
                    'processing_contract','reference_set','selection_policy','intent_id')]
                versions = connection.execute("SELECT captured_input FROM admission_jobs WHERE input->>'document_id'=%s AND content_pin=%s", (member['document_id'], pin)).fetchall()
                winner = connection.execute('SELECT id FROM admission_jobs WHERE processing_key=%s', (Jsonb(key),)).fetchone()
                if any(v['captured_input'] != content for v in versions):
                    reason = 'input_integrity_conflict'
                else:
                    state, reason = ('completed', 'duplicate-of') if winner else ('queued', 'pinned_input_captured')
                    connection.execute('''UPDATE admission_jobs SET content_pin=%s,captured_input=%s,
                        processing_key=%s,canonical_job_id=%s WHERE id=%s''',
                        (pin, content, None if winner else Jsonb(key), winner['id'] if winner else job_id, job_id))
            if not current(connection, job_id, claim, 'acquisition'):
                connection.execute('ROLLBACK TO SAVEPOINT source_capture')
                return conflict(job_id, 'stale_commit')
            connection.execute('''UPDATE admission_jobs SET state=%s,reason=%s,
                held_active_reservation=%s,revision=revision+1 WHERE id=%s''',
                (state, reason, state != 'completed', job_id))
            connection.execute('UPDATE job_attempts SET stopped=%s,reason=%s WHERE id=%s',
                               (proved, reason, claim['attempt_id']))
            if state == 'completed':
                connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                                   (retained_usage(connection, job_id)['retained_bytes'], job_id))
        return self.ingestion.status(job_id)
