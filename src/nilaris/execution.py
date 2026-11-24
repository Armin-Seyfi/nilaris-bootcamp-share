"""Durable worker-control primitives. No worker launch or arbitrary HTTP output API."""
from contextlib import nullcontext
import hashlib
import base64
import json
import uuid
import time

from psycopg.types.json import Jsonb
from nilaris.results import StageOutput, canonical

import psycopg
from psycopg.rows import dict_row

MIGRATION = '''
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 0;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 0;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS reason text NOT NULL DEFAULT 'pending_acquisition';
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS content_pin text;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS captured_input bytea;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS held_active_reservation boolean NOT NULL DEFAULT false;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS processing_key jsonb;
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS canonical_job_id text REFERENCES admission_jobs(id);
ALTER TABLE admission_jobs ADD COLUMN IF NOT EXISTS lineage jsonb;
UPDATE admission_jobs SET processing_key=jsonb_build_array(
    input->'document_id',to_jsonb(content_pin),input->'case_id',input->'membership_revision',
    input->'product_type',input->'processing_contract',input->'reference_set',
    input->'selection_policy',input->'intent_id'),canonical_job_id=id
    WHERE content_pin IS NOT NULL AND processing_key IS NULL AND canonical_job_id IS NULL
    AND reason<>'input_integrity_conflict';
CREATE UNIQUE INDEX IF NOT EXISTS admission_jobs_processing_key ON admission_jobs(processing_key);
CREATE TABLE IF NOT EXISTS job_attempts (
    id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES admission_jobs(id),
    stage text NOT NULL,
    generation bigint NOT NULL,
    token text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT clock_timestamp() + interval '10 seconds',
    stopped boolean NOT NULL DEFAULT false,
    reason text NOT NULL DEFAULT 'running',
    UNIQUE (job_id, generation)
);
CREATE TABLE IF NOT EXISTS job_effects (
    job_id text NOT NULL REFERENCES admission_jobs(id),
    stage_contract text NOT NULL,
    kind text NOT NULL,
    ordinal integer NOT NULL,
    payload jsonb NOT NULL,
    support_id text,
    PRIMARY KEY (job_id, stage_contract, kind, ordinal)
);
CREATE TABLE IF NOT EXISTS job_publications (
    id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES admission_jobs(id),
    attempt_id text NOT NULL UNIQUE REFERENCES job_attempts(id),
    sequence integer NOT NULL,
    request jsonb NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE(job_id, sequence)
);
ALTER TABLE job_publications ADD COLUMN IF NOT EXISTS publication_origin text NOT NULL DEFAULT 'stage';
ALTER TABLE job_publications ALTER COLUMN attempt_id DROP NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='job_publication_origin_guard'
                   AND conrelid='job_publications'::regclass) THEN
        ALTER TABLE job_publications ADD CONSTRAINT job_publication_origin_guard CHECK (
            (publication_origin='stage' AND attempt_id IS NOT NULL) OR
            (publication_origin='semantic-projection-v1' AND attempt_id IS NULL AND
             coalesce(payload->>'adapter'='semantic-projection-v1' AND
                      payload->>'stage_contract'='semantic-projection-v1',false)));
    END IF;
END $$;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS completion_evidence text;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS worker jsonb;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS accounting_complete boolean NOT NULL DEFAULT false;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS wall_seconds double precision NOT NULL DEFAULT 0;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS cpu_seconds double precision NOT NULL DEFAULT 0;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS measured_cpu_seconds double precision NOT NULL DEFAULT 0;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS cpu_accounting text NOT NULL DEFAULT 'not_measured';
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS peak_rss_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS acquisition_requests integer NOT NULL DEFAULT 0 CHECK (acquisition_requests >= 0);
ALTER TABLE job_attempts ADD COLUMN IF NOT EXISTS acquisition_read_token text;
CREATE TABLE IF NOT EXISTS job_recoveries (
    job_id text NOT NULL REFERENCES admission_jobs(id),
    revision bigint NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    generation bigint NOT NULL,
    PRIMARY KEY (job_id, revision)
);
'''


def conflict(job_id, category='claim_conflict'):
    return dict(known=True, category=category, affected=dict(job_id=job_id),
                coverage='unknown', reason='current job state or revision prevents this action',
                next_action='query_job')


def locked_job(connection, ingestion, job_id):
    return connection.execute("""SELECT * FROM admission_jobs WHERE id=%s
        AND input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s FOR UPDATE""",
        (job_id, ingestion.scoped_binding)).fetchone()


def cancel(ingestion, job_id, expected_revision):
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        job = locked_job(connection, ingestion, job_id)
        if not job or job['revision'] != expected_revision or job['state'] not in ('acquiring', 'queued'):
            return conflict(job_id, 'cancel_conflict')
        if job['held_active_reservation']:
            attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s', (job_id,)).fetchall()
            if (not attempts or any(not attempt['stopped'] or attempt['worker'] is not None or
                    attempt['completion_evidence'] != 'finite_controlled_call'
                    for attempt in attempts)):
                return conflict(job_id, 'cancel_conflict')
        connection.execute("UPDATE admission_jobs SET state='cancelled', reason='queued_cancelled', held_active_reservation=false, revision=revision+1 WHERE id=%s", (job_id,))
        connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                           (retained_usage(connection, job_id)['retained_bytes'], job_id))
    return ingestion.status(job_id)


def _stopped_scoped_slot(connection, previous_id, successor_id):
    """Prove one tenant's pre-intent slot is transferable without loading its source.

    The SQL charge bounds retained_usage for finite local attempts with no
    publications, effects or review material. Six ASCII bytes per stored JSON
    byte covers canonical Unicode escapes. Include the next recovery record.
    """
    return connection.execute("""SELECT 1 FROM scoped_jobs old
        JOIN scoped_jobs successor ON successor.customer_id=old.customer_id
        JOIN admission_jobs j ON j.id=old.job_id
        WHERE old.job_id=%s AND successor.job_id=%s AND old.state='held'
          AND old.receipt IS NULL AND j.state='held' AND j.held_active_reservation
          AND j.input->>'scoped_intake_binding'=old.binding
          AND EXISTS (SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.stage='processing'
              AND a.id=old.claim->>'attempt_id' AND a.token=old.claim->>'token'
              AND a.generation=j.generation AND a.generation::text=old.claim->>'generation')
          AND NOT EXISTS (SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND
              (NOT a.stopped OR a.worker IS NOT NULL OR a.acquisition_read_token IS NOT NULL
               OR a.completion_evidence IS DISTINCT FROM 'finite_controlled_call'))
          AND NOT EXISTS (SELECT 1 FROM job_effects WHERE job_id=j.id)
          AND NOT EXISTS (SELECT 1 FROM job_publications WHERE job_id=j.id)
          AND NOT EXISTS (SELECT 1 FROM review_receipts WHERE job_id=j.id)
          AND 16384::bigint + coalesce(octet_length(j.captured_input),0)
              + 6::bigint * octet_length(jsonb_build_array(j.input,j.semantic_key,j.profile,
                    j.processing_key,j.canonical_job_id,j.lineage)::text)
              + (SELECT 4100 * count(*) FROM job_attempts WHERE job_id=j.id)
              + (SELECT coalesce(sum(4096 + 6::bigint * octet_length(to_jsonb(r)::text)),0)
                 FROM job_recoveries r WHERE r.job_id=j.id)
              <= least(j.reservation_bytes,(j.profile->>'job_bytes')::bigint)""",
        (previous_id,successor_id)).fetchone() is not None


def claim(ingestion, job_id, expected_revision, *, transaction=None):
    from nilaris.service_monitor import service_monitor_ready
    if not service_monitor_ready(ingestion.monitor_path):
        return conflict(job_id, 'service_monitor_unavailable')
    with (nullcontext(transaction) if transaction is not None else
          psycopg.connect(**ingestion.database, row_factory=dict_row)) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        if not service_monitor_ready(ingestion.monitor_path):
            return conflict(job_id, 'service_monitor_unavailable')
        job = locked_job(connection, ingestion, job_id)
        if not job or job['revision'] != expected_revision or job['state'] not in ('acquiring', 'queued'):
            return conflict(job_id)
        if connection.execute('SELECT count(*) AS count FROM job_attempts WHERE NOT stopped').fetchone()['count']:
            return conflict(job_id, 'active_capacity')
        if connection.execute('''SELECT 1 FROM drive155_operations WHERE active
                AND (job_id IS NULL OR job_id<>%s) LIMIT 1''', (job_id,)).fetchone():
            return conflict(job_id, 'active_capacity')
        stage = 'acquisition' if job['state'] == 'acquiring' else 'processing'
        if job['held_active_reservation']:
            own_attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s', (job_id,)).fetchall()
            if (not own_attempts or any(not attempt['stopped'] or
                    (attempt['worker'] is None and attempt['completion_evidence'] != 'finite_controlled_call') or
                    (attempt['worker'] is not None and (not attempt['accounting_complete'] or
                     attempt['completion_evidence'] != 'process_group_reaped'))
                    for attempt in own_attempts)):
                return conflict(job_id, 'active_capacity')
            from nilaris.worker_lifecycle import processes
            try:
                if any(a['worker'] and processes(a['worker']['pgid']) for a in own_attempts):
                    return conflict(job_id, 'active_capacity')
            except Exception:
                return conflict(job_id, 'active_capacity')
        counts = connection.execute(
            '''SELECT count(*) AS total, count(*) FILTER (WHERE stage=%s) AS stage,
               coalesce(sum(wall_seconds),0) AS wall_seconds,coalesce(sum(cpu_seconds),0) AS cpu_seconds
               FROM job_attempts WHERE job_id=%s''',
            (stage, job_id)).fetchone()
        if (counts['total'] >= 4 or counts['stage'] >= 2 or
                counts['wall_seconds'] >= 120 or counts['cpu_seconds'] >= 60):
            return conflict(job_id, 'resource_exhausted')
        if retained_usage(connection, job_id)['retained_bytes'] + 4096 > job['profile']['job_bytes']:
            return conflict(job_id, 'resource_exhausted')
        reserved = connection.execute("""SELECT id,revision,generation,profile,input->>'scoped_intake_binding' AS binding
            FROM admission_jobs WHERE held_active_reservation AND id<>%s FOR UPDATE""",(job_id,)).fetchall()
        for predecessor in reserved:
            if predecessor['binding'] != ingestion.scoped_binding:
                if (predecessor['binding'] and ingestion.scoped_binding and
                        _stopped_scoped_slot(connection, predecessor['id'], job_id)):
                    continue
                return conflict(job_id, 'active_capacity')
            attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s', (predecessor['id'],)).fetchall()
            if not attempts or any(not a['stopped'] or
                    (a['worker'] is not None and (not a['accounting_complete'] or
                     a['completion_evidence'] != 'process_group_reaped')) or
                    (a['worker'] is None and a['completion_evidence'] != 'finite_controlled_call')
                    for a in attempts):
                return conflict(job_id, 'active_capacity')
            from nilaris.worker import processes
            try:
                if any(a['worker'] and processes(a['worker']['pgid']) for a in attempts):
                    return conflict(job_id, 'active_capacity')
            except Exception:
                return conflict(job_id, 'active_capacity')
            if retained_usage(connection, predecessor['id'])['retained_bytes'] + 8192 > predecessor['profile']['job_bytes']:
                return conflict(job_id, 'resource_exhausted')
        # Transfer within the admission lock: each reserved non-running Job
        # becomes waiting at the same commit that this eligible Job becomes running.
        for predecessor in reserved:
            connection.execute('INSERT INTO job_recoveries (job_id,revision,generation,actor,reason) VALUES (%s,%s,%s,%s,%s)',
                               (predecessor['id'], predecessor['revision'] + 1, predecessor['generation'],
                                'worker-supervisor', 'verified_capacity_transfer'))
            connection.execute('UPDATE admission_jobs SET held_active_reservation=false,revision=revision+1 WHERE id=%s',
                               (predecessor['id'],))
        attempt_id, token = str(uuid.uuid4()), uuid.uuid4().hex
        generation = job['generation'] + 1
        connection.execute(
            'INSERT INTO job_attempts (id,job_id,stage,generation,token) VALUES (%s,%s,%s,%s,%s)',
            (attempt_id,job_id,stage,generation,token))
        connection.execute("UPDATE admission_jobs SET state='running', reason='running', held_active_reservation=false, revision=revision+1, generation=%s WHERE id=%s", (generation,job_id))
    return dict(known=True, claimed=True, job_id=job_id, attempt_id=attempt_id,
                generation=generation, token=token, stage=stage)


def project(connection, row):
    state = connection.execute('SELECT revision, generation, reason, content_pin, canonical_job_id, lineage, held_active_reservation, octet_length(captured_input) AS captured_input_bytes FROM admission_jobs WHERE id=%s', (row['id'],)).fetchone()
    row.update(state)
    row['active_capacity_reserved'] = row.pop('held_active_reservation') or row['state'] == 'running'
    attempts = connection.execute(
        'SELECT id, stage, generation, started_at, expires_at, heartbeat_at, stopped, reason, worker, accounting_complete, completion_evidence, wall_seconds, cpu_seconds, measured_cpu_seconds, cpu_accounting, peak_rss_bytes, acquisition_requests FROM job_attempts WHERE job_id=%s ORDER BY generation',
        (row['id'],)).fetchall()
    for attempt in attempts:
        for field in ('started_at', 'expires_at', 'heartbeat_at'):
            attempt[field] = attempt[field].isoformat() if attempt[field] else None
    effects, publications = outputs(connection, row['id'])
    row['effects'] = [{key: effect[key] for key in ('stage_contract', 'kind', 'ordinal', 'support_id')}
                      for effect in effects[:16]]
    row['effect_count'] = len(effects)
    row['effects_truncated'] = len(effects) > 16
    row['results'] = [{key: publication[key] for key in
                      ('id', 'kind', 'sequence', 'initial_publication_id', 'outcome', 'approved')}
                     for publication in publications]
    if publications:
        row['coverage'] = publications[-1]['coverage']
        if len(canonical(row['coverage'])) > 4096:
            row['coverage'] = dict(outcome=publications[-1]['outcome'],
                                   result_reference=publications[-1]['id'])
    row['attempts'] = attempts
    row['counters']['attempts'] = len(attempts)
    row['counters']['acquisition_requests'] = sum(a['acquisition_requests'] for a in attempts)
    row['counters']['captured_input_bytes'] = row.pop('captured_input_bytes') or 0
    if row['content_pin'] and not publications:
        row['coverage'] = dict(acquisition='captured', processing='not_started')
        row['next_action'] = 'await_processing'
    if row['state'] == 'held':
        row['next_action'] = 'reconcile_interruption'
    if row['state'] == 'partial':
        row['next_action'] = 'reviewed_retry'
    if row['state'] == 'queued':
        row['next_action'] = 'await_processing'
    row['current_claim'] = next((a for a in reversed(attempts) if not a['stopped']), None)
    row['recovery'] = connection.execute(
        'SELECT revision,generation,actor,reason FROM job_recoveries WHERE job_id=%s ORDER BY revision',
        (row['id'],)).fetchall()
    row['usage'] = retained_usage(connection, row['id'])
    row['usage'].update(wall_seconds=sum(a['wall_seconds'] for a in attempts),
                        acquisition_requests=row['counters']['acquisition_requests'],
                        cpu_seconds=sum(a['cpu_seconds'] for a in attempts),
                        measured_cpu_seconds=sum(a['measured_cpu_seconds'] for a in attempts),
                        peak_rss_bytes=max((a['peak_rss_bytes'] for a in attempts), default=0),
                        accounting_unknown=any(
                            (not a['stopped'] or a['completion_evidence'] != 'finite_controlled_call')
                            if a['worker'] is None else
                            (not a['accounting_complete'] or a['completion_evidence'] != 'process_group_reaped')
                            for a in attempts))
    last_stage = attempts[-1]['stage'] if attempts else None
    exhausted = (len(attempts) >= 4 or row['usage']['wall_seconds'] >= 120 or
                 row['usage']['cpu_seconds'] >= 60 or
                 (last_stage and sum(a['stage'] == last_stage for a in attempts) >= 2) or
                 (last_stage == 'acquisition' and row['counters']['acquisition_requests'] >= 12))
    if row['state'] == 'held':
        row['recovery_actions'] = ['hold']
        if not row['usage']['accounting_unknown']:
            row['recovery_actions'] += ['fail', 'quarantine']
            if exhausted:
                row['next_action'] = 'reviewed_disposition'
            elif row['reason'] in ('source_version_unavailable', 'attempt_wall_limit', 'attempt_cpu_limit',
                    'worker_rss_limit', 'worker_output_limit', 'controlled_worker_failed',
                    'claim_lost', 'reviewed_generation_revoked'):
                row['recovery_actions'].append('safe-retry')
    if row['state'] == 'running':
        row['next_action'] = 'await_worker_or_reconcile_interruption'
    if row['state'] in ('completed', 'cancelled', 'quarantined'):
        row['next_action'] = 'terminal'
    if row['state'] == 'failed':
        retryable_failure = (not exhausted and not row['usage']['accounting_unknown'] and
            row['reason'] == 'reviewed_failed' and attempts and (
                (last_stage == 'processing' and row['content_pin'] and attempts[-1]['reason'] in (
                    'attempt_wall_limit', 'attempt_cpu_limit', 'worker_rss_limit', 'worker_output_limit',
                    'controlled_worker_failed', 'claim_lost', 'reviewed_generation_revoked')) or
                (last_stage == 'acquisition' and row['content_pin'] is None and attempts[-1]['reason'] in (
                    'source_version_unavailable', 'attempt_wall_limit', 'attempt_cpu_limit', 'claim_lost',
                    'reviewed_generation_revoked'))))
        row['next_action'] = 'reviewed_retry' if retryable_failure else 'query_job'
    if row['canonical_job_id'] and row['canonical_job_id'] != row['id']:
        row['disposition'] = 'duplicate-of'
        row['coverage'] = dict(acquisition='captured', processing='canonical_job')
        row['next_action'] = 'query_canonical_job'
    return row


def retained_usage(connection, job_id):
    """Conservative logical retained copies; physical storage has its own guard.

    Canonical ASCII counts every serialized copy (including replay requests).
    Fixed per-record allowances cover row/index bookkeeping, in addition to the
    bytes themselves. Scratch and live worker accounting are separate controls.
    """
    job = connection.execute('SELECT input,semantic_key,profile,processing_key,canonical_job_id,lineage,octet_length(captured_input) AS captured_bytes FROM admission_jobs WHERE id=%s',
                             (job_id,)).fetchone()
    retained = 8192 + len(canonical([job['input'], job['semantic_key'], job['profile'],
                                   job['processing_key'], job['canonical_job_id'], job['lineage']])) + (job['captured_bytes'] or 0)
    for effect in connection.execute('SELECT stage_contract,kind,ordinal,payload,support_id FROM job_effects WHERE job_id=%s', (job_id,)):
        retained += 4096 + len(canonical(effect))
    serialized = 0
    for publication in connection.execute('SELECT request,payload FROM job_publications WHERE job_id=%s', (job_id,)):
        size = len(canonical(publication['payload']))
        serialized += size
        retained += 8192 + size + len(canonical(publication['request']))
    for attempt in connection.execute('SELECT worker FROM job_attempts WHERE job_id=%s', (job_id,)):
        worker = attempt['worker']
        retained += 4096 + len(canonical(worker))
        if worker is not None and 'semantic_artifact_bytes' in worker:
            charge = worker['semantic_artifact_bytes']
            if type(charge) is not int or charge < 0:
                raise ValueError('invalid semantic artifact accounting')
            retained += charge
    for recovery in connection.execute('SELECT revision,generation,actor,reason FROM job_recoveries WHERE job_id=%s', (job_id,)):
        retained += 4096 + len(canonical(recovery))
    from nilaris.review import retained_bytes as review_retained_bytes
    retained += review_retained_bytes(connection, job_id)
    # Pre-Ticket05 tenants keep their ordinary read and execution paths until
    # their schema is explicitly migrated for the controlled preview.
    if connection.execute("SELECT to_regclass('public.minimized_evidence') AS table_name").fetchone()['table_name']:
        from nilaris.minimized_evidence import ROW_RESERVATION_BYTES
        preview_count = connection.execute('''
            SELECT count(*) AS count FROM minimized_evidence m
            JOIN job_publications p ON p.id=m.publication_id WHERE p.job_id=%s
            ''', (job_id,)).fetchone()['count']
        retained += preview_count * ROW_RESERVATION_BYTES
    return dict(serialized_result_bytes=serialized, retained_bytes=retained)


def reprocess(ingestion, namespace, command_id, job_id, expected_revision, expected_generation, actor, reason):
    """Create one reviewed intent without changing the original Job or budget."""
    from dataclasses import asdict
    from nilaris.admission import AdmissionProfile, _admission_usage, RECEIPT_RESERVATION_BYTES
    from nilaris.service_monitor import service_monitor_ready
    if (any(not isinstance(value, str) or not value.strip()
            for value in (namespace, command_id, job_id, actor, reason)) or
            type(expected_revision) is not int or type(expected_generation) is not int or
            len(canonical([namespace, command_id, job_id, actor, reason])) > 4096):
        return conflict(job_id, 'invalid_reprocessing')
    if namespace.startswith('scoped-intake:'):
        from nilaris.admission import scoped_namespace_refusal
        return dict(scoped_namespace_refusal(), affected=dict(job_id=job_id))
    if not isinstance(ingestion.profile, AdmissionProfile):
        return conflict(job_id, 'configuration_unavailable')
    request = dict(operation='reprocess', job_id=job_id, expected_revision=expected_revision,
                   expected_generation=expected_generation, actor=actor, reason=reason,
                   profile=asdict(ingestion.profile))
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        prior = connection.execute('SELECT request,result FROM admission_receipts WHERE namespace=%s AND command_id=%s',
                                   (namespace, command_id)).fetchone()
        if prior:
            return prior['result'] if prior['request'] == request else conflict(job_id, 'command_conflict')
        if namespace.startswith('discovery:'):
            return dict(known=True, category='reserved_namespace', affected=dict(job_id=job_id),
                        coverage='unknown', reason='Discovery admission keys are journal owned.',
                        next_action='use_discovery_admit')
        if not service_monitor_ready(ingestion.monitor_path):
            return conflict(job_id, 'service_monitor_unavailable')
        original = locked_job(connection, ingestion, job_id)
        if (not original or original['revision'] != expected_revision or original['generation'] != expected_generation):
            return conflict(job_id, 'reprocess_conflict')
        if (original['state'] not in ('completed', 'partial', 'failed', 'cancelled') or
                original['canonical_job_id'] != job_id or not original['content_pin'] or
                original['captured_input'] is None or
                hashlib.sha256(original['captured_input']).hexdigest() != original['content_pin']):
            return conflict(job_id, 'recovery_held')
        attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s', (job_id,)).fetchall()
        if any(not a['stopped'] or (a['worker'] is None and a['completion_evidence'] != 'finite_controlled_call') or
               (a['worker'] is not None and (not a['accounting_complete'] or
                a['completion_evidence'] != 'process_group_reaped')) for a in attempts):
            return conflict(job_id, 'recovery_held')
        from nilaris.worker import processes
        try:
            if any(a['worker'] and processes(a['worker']['pgid']) for a in attempts):
                return conflict(job_id, 'recovery_held')
        except Exception:
            return conflict(job_id, 'recovery_held')
        if len(original['captured_input']) > ingestion.profile.input_bytes:
            return conflict(job_id, 'input_limit')
        admission = _admission_usage(connection)
        for counter, category in (('waiting_jobs', 'waiting_capacity'), ('live_jobs', 'live_capacity')):
            if admission[counter] >= getattr(ingestion.profile, counter):
                return conflict(job_id, category)
        if (admission['job_reservation_bytes'] + admission['receipt_reservation_bytes'] +
                ingestion.profile.job_bytes + RECEIPT_RESERVATION_BYTES >
                ingestion.profile.logical_bytes - ingestion.profile.terminal_headroom_bytes):
            return conflict(job_id, 'byte_capacity')
        _, publications = outputs(connection, job_id)
        lineage = dict(original_job_id=job_id,
                       original_initial_publication_id=publications[0]['id'] if publications else None,
                       actor=actor, reason=reason)
        new_id = str(uuid.uuid4())
        new_input = dict(original['input'], intent_id=str(uuid.uuid4()))
        semantic = [new_input[key] for key in ('document_id', 'source_revision', 'case_id',
            'membership_revision', 'product_type', 'processing_contract', 'reference_set', 'selection_policy', 'intent_id')]
        processing = [new_input['document_id'], original['content_pin']] + semantic[2:]
        connection.execute('''INSERT INTO admission_jobs
            (id,input,state,reservation_bytes,profile,semantic_key,processing_key,canonical_job_id,
             content_pin,captured_input,lineage,reason)
            VALUES (%s,%s,'queued',%s,%s,%s,%s,%s,%s,%s,%s,'reviewed_reprocessing')''',
            (new_id, Jsonb(new_input), ingestion.profile.job_bytes, Jsonb(asdict(ingestion.profile)),
             Jsonb(semantic), Jsonb(processing), new_id, original['content_pin'],
             original['captured_input'], Jsonb(lineage)))
        result = dict(known=True, job_id=new_id, intent_id=new_input['intent_id'], lineage=lineage,
                      disposition='accepted', next_action='query_job')
        connection.execute('''INSERT INTO admission_receipts(namespace,command_id,request,result,reservation_bytes)
            VALUES (%s,%s,%s,%s,%s)''',
            (namespace, command_id, Jsonb(request), Jsonb(result), RECEIPT_RESERVATION_BYTES))
    return result


def retry(ingestion, job_id, expected_revision, expected_generation, actor, reason):
    """Reviewed retries keep acquisition and processing at their own stages.

    Finite direct test calls never establish OS absence. A supervised controlled
    attempt additionally needs the supervisor's actual reaping/accounting record.
    Unknown interruptions and general production recovery remain held.
    """
    if any(not isinstance(value, str) or not value.strip() for value in (actor, reason)) or len(canonical([actor, reason])) > 4096:
        return conflict(job_id, 'invalid_recovery')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        job = locked_job(connection, ingestion, job_id)
        if (not job or job['revision'] != expected_revision or
                job['generation'] != expected_generation):
            return conflict(job_id, 'retry_conflict')
        attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s ORDER BY generation', (job_id,)).fetchall()
        failed_processing = (job['state'] == 'failed' and job['reason'] == 'reviewed_failed' and
            attempts and attempts[-1]['stage'] == 'processing' and attempts[-1]['reason'] in (
                'attempt_wall_limit', 'attempt_cpu_limit', 'worker_rss_limit', 'worker_output_limit',
                'controlled_worker_failed', 'claim_lost', 'reviewed_generation_revoked'))
        failed_acquisition = (job['state'] == 'failed' and job['reason'] == 'reviewed_failed' and
            attempts and attempts[-1]['stage'] == 'acquisition' and attempts[-1]['reason'] in (
                'source_version_unavailable', 'attempt_wall_limit', 'attempt_cpu_limit', 'claim_lost',
                'reviewed_generation_revoked', 'acquisition_request_limit'))
        failed_retry = failed_processing or failed_acquisition
        acquisition_retry = (((job['state'] == 'held' and job['reason'] == 'source_version_unavailable') or
                              failed_acquisition) and
                             job['content_pin'] is None and job['captured_input'] is None)
        processing_retry = ((job['state'] == 'partial' or failed_processing) and job['content_pin'] and
                            job['captured_input'] is not None)
        if not acquisition_retry and not processing_retry:
            return conflict(job_id, 'retry_conflict')
        if (not attempts or any(not a['stopped'] or
               (a['worker'] is None and a['completion_evidence'] != 'finite_controlled_call') or
               (a['worker'] is not None and (not a['accounting_complete'] or
                                            a['completion_evidence'] != 'process_group_reaped')) for a in attempts)):
            return conflict(job_id, 'recovery_held')
        retry_stage = 'acquisition' if acquisition_retry else 'processing'
        if (len(attempts) >= 4 or sum(a['stage'] == retry_stage for a in attempts) >= 2 or
                (retry_stage == 'acquisition' and sum(a['acquisition_requests'] for a in attempts) >= 12) or
                sum(a['wall_seconds'] for a in attempts) >= 120 or sum(a['cpu_seconds'] for a in attempts) >= 60):
            return conflict(job_id, 'resource_exhausted')
        from nilaris.admission import _admission_usage
        reserve = job['held_active_reservation']
        if acquisition_retry and not failed_retry:
            if not reserve:
                admission = _admission_usage(connection)
                # This held Job is already live and waiting. Reserving it removes
                # one waiting Job without admitting another live Job.
                if admission['waiting_jobs'] - 1 > job['profile']['waiting_jobs']:
                    return conflict(job_id, 'waiting_capacity')
                if admission['live_jobs'] > job['profile']['live_jobs']:
                    return conflict(job_id, 'live_capacity')
                if admission['active_capacity_reservations'] >= job['profile']['active_attempts']:
                    return conflict(job_id, 'active_capacity')
                reserve = True
        else:
            admission = _admission_usage(connection)
            for counter, category in (('waiting_jobs', 'waiting_capacity'), ('live_jobs', 'live_capacity')):
                if admission[counter] >= job['profile'][counter]:
                    return conflict(job_id, category)
            if failed_retry:
                from nilaris.worker import processes
                try:
                    if any(a['worker'] and processes(a['worker']['pgid']) for a in attempts):
                        return conflict(job_id, 'recovery_held')
                except Exception:
                    return conflict(job_id, 'recovery_held')
            if (admission['job_reservation_bytes'] + admission['receipt_reservation_bytes'] +
                    job['profile']['job_bytes'] - job['reservation_bytes'] >
                    job['profile']['logical_bytes'] - job['profile']['terminal_headroom_bytes']):
                return conflict(job_id, 'byte_capacity')
        usage = retained_usage(connection, job_id)
        recovery = dict(revision=job['revision'] + 1, generation=job['generation'] + 1, actor=actor, reason=reason)
        if (usage['serialized_result_bytes'] >= 2 * 1024**2 or
                usage['retained_bytes'] + 8192 + len(canonical(recovery)) > job['profile']['job_bytes']):
            return conflict(job_id, 'resource_exhausted')
        # The immutable captured bytes are the sole processing-retry input.
        if processing_retry and hashlib.sha256(job['captured_input']).hexdigest() != job['content_pin']:
            return conflict(job_id, 'recovery_held')
        connection.execute('INSERT INTO job_recoveries (job_id,revision,generation,actor,reason) VALUES (%s,%s,%s,%s,%s)',
                           (job_id, job['revision'] + 1, job['generation'] + 1, actor, reason))
        if acquisition_retry:
            connection.execute("UPDATE admission_jobs SET state='acquiring',held_active_reservation=%s,reason=%s,reservation_bytes=%s,revision=revision+1,generation=generation+1 WHERE id=%s",
                               (reserve, 'reviewed_failed_acquisition_retry' if failed_retry else 'reviewed_acquisition_retry',
                                job['profile']['job_bytes'] if failed_retry else job['reservation_bytes'], job_id))
        else:
            connection.execute("UPDATE admission_jobs SET state='queued',held_active_reservation=false,reason=%s,reservation_bytes=%s,revision=revision+1,generation=generation+1 WHERE id=%s",
                               ('reviewed_failed_retry' if failed_processing else 'reviewed_partial_retry',
                                job['profile']['job_bytes'], job_id))
    return ingestion.status(job_id)


def reviewed_disposition(ingestion, job_id, expected_revision, expected_generation, actor, reason, disposition):
    if (disposition not in ('hold', 'safe-retry', 'fail', 'quarantine') or
            any(not isinstance(value, str) or not value.strip() for value in (actor, reason)) or
            len(canonical([actor, reason])) > 4096):
        return conflict(job_id, 'invalid_recovery')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        job = locked_job(connection, ingestion, job_id)
        if (not job or job['revision'] != expected_revision or job['generation'] != expected_generation or
                job['state'] not in ('held', 'running')):
            return conflict(job_id, 'reconcile_conflict')
        state, reserve, outcome = 'held', job['held_active_reservation'], job['reason']
        if job['state'] == 'running':
            if disposition != 'hold':
                return conflict(job_id, 'recovery_held')
            reserve, outcome = True, 'reviewed_generation_revoked'
        if disposition != 'hold':
            if disposition == 'safe-retry':
                allowance = connection.execute('''SELECT coalesce(sum(wall_seconds),0) AS wall,
                    coalesce(sum(cpu_seconds),0) AS cpu FROM job_attempts WHERE job_id=%s''', (job_id,)).fetchone()
                if allowance['wall'] >= 120 or allowance['cpu'] >= 60:
                    return conflict(job_id, 'resource_exhausted')
            if disposition == 'safe-retry' and job['reason'] not in (
                    'source_version_unavailable', 'attempt_wall_limit', 'attempt_cpu_limit',
                    'worker_rss_limit', 'worker_output_limit', 'controlled_worker_failed',
                    'claim_lost', 'reviewed_generation_revoked'):
                return conflict(job_id, 'recovery_held')
            attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s ORDER BY generation', (job_id,)).fetchall()
            if not attempts or any(not a['stopped'] or
                    (a['worker'] is None and a['completion_evidence'] != 'finite_controlled_call') or
                    (a['worker'] is not None and (not a['accounting_complete'] or
                     a['completion_evidence'] != 'process_group_reaped')) for a in attempts):
                return conflict(job_id, 'recovery_held')
            from nilaris.worker import processes
            try:
                if any(a['worker'] and processes(a['worker']['pgid']) for a in attempts):
                    return conflict(job_id, 'recovery_held')
            except Exception:
                return conflict(job_id, 'recovery_held')
        if disposition in ('fail', 'quarantine'):
            state = 'failed' if disposition == 'fail' else 'quarantined'
            outcome, reserve = 'reviewed_' + state, False
        if disposition == 'safe-retry':
            stage = attempts[-1]['stage']
            if stage == 'acquisition':
                if job['content_pin'] is not None or job['captured_input'] is not None:
                    return conflict(job_id, 'recovery_held')
                state, outcome = 'acquiring', 'reviewed_acquisition_retry'
            else:
                if (not job['content_pin'] or job['captured_input'] is None or
                        hashlib.sha256(job['captured_input']).hexdigest() != job['content_pin']):
                    return conflict(job_id, 'recovery_held')
                state, outcome = 'queued', 'reviewed_interruption_retry'
            if (len(attempts) >= 4 or sum(a['stage'] == stage for a in attempts) >= 2 or
                    sum(a['wall_seconds'] for a in attempts) >= 120 or sum(a['cpu_seconds'] for a in attempts) >= 60):
                return conflict(job_id, 'resource_exhausted')
            # Preserve an existing active reservation through acquisition handoff;
            # processing joins waiting only when that exact capacity is available.
            if stage == 'processing':
                from nilaris.admission import _admission_usage
                if reserve and _admission_usage(connection)['waiting_jobs'] >= job['profile']['waiting_jobs']:
                    return conflict(job_id, 'waiting_capacity')
                reserve = False
        recovery = dict(revision=job['revision'] + 1, generation=job['generation'] + 1, actor=actor, reason=reason)
        if retained_usage(connection, job_id)['retained_bytes'] + 8192 + len(canonical(recovery)) > job['profile']['job_bytes']:
            return conflict(job_id, 'resource_exhausted')
        connection.execute('INSERT INTO job_recoveries(job_id,revision,generation,actor,reason) VALUES (%s,%s,%s,%s,%s)',
                           (job_id, recovery['revision'], recovery['generation'], actor, reason))
        connection.execute('UPDATE admission_jobs SET state=%s,reason=%s,held_active_reservation=%s,revision=revision+1,generation=generation+1 WHERE id=%s',
                           (state, outcome, reserve, job_id))
        if disposition in ('fail', 'quarantine'):
            connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                               (retained_usage(connection, job_id)['retained_bytes'], job_id))
    return ingestion.status(job_id)


def reconcile(ingestion, job_id, expected_revision, expected_generation, actor, reason, disposition=None):
    """Reviewed recovery; uncertain effects, worker absence or accounting hold."""
    if disposition is not None:
        return reviewed_disposition(ingestion, job_id, expected_revision, expected_generation, actor, reason, disposition)
    if any(not isinstance(value, str) or not value.strip() for value in (actor, reason)) or len(canonical([actor, reason])) > 4096:
        return conflict(job_id, 'invalid_recovery')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        job = locked_job(connection, ingestion, job_id)
        if (not job or job['revision'] != expected_revision or job['generation'] != expected_generation or
                job['state'] not in ('held', 'running')):
            return conflict(job_id, 'reconcile_conflict')
        if job['state'] == 'running':
            recovery = dict(revision=job['revision'] + 1, generation=job['generation'] + 1, actor=actor, reason=reason)
            if retained_usage(connection, job_id)['retained_bytes'] + 8192 + len(canonical(recovery)) > job['profile']['job_bytes']:
                return conflict(job_id, 'resource_exhausted')
            # Commit fencing precedes any late result or supervisor cleanup.
            # The original supervisor alone accounts its actual owned group;
            # missing supervisors/accounting are never replaced by an assertion.
            connection.execute('INSERT INTO job_recoveries (job_id,revision,generation,actor,reason) VALUES (%s,%s,%s,%s,%s)',
                               (job_id, recovery['revision'], recovery['generation'], actor, reason))
            connection.execute("UPDATE admission_jobs SET state='held',held_active_reservation=true,reason='reviewed_generation_revoked',revision=revision+1,generation=generation+1 WHERE id=%s", (job_id,))
        else:
            attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s ORDER BY generation', (job_id,)).fetchall()
            if job['reason'] not in ('attempt_wall_limit', 'attempt_cpu_limit', 'worker_rss_limit',
                                      'worker_output_limit', 'controlled_worker_failed', 'claim_lost',
                                      'reviewed_generation_revoked'):
                return conflict(job_id, 'recovery_held')
            if not attempts or any(not a['stopped'] or
                    (a['worker'] is None and a['completion_evidence'] != 'finite_controlled_call') or
                    (a['worker'] is not None and (not a['accounting_complete'] or
                     a['completion_evidence'] != 'process_group_reaped')) for a in attempts):
                return conflict(job_id, 'recovery_held')
            from nilaris.worker import processes
            try:
                if any(a['worker'] and processes(a['worker']['pgid']) for a in attempts):
                    return conflict(job_id, 'recovery_held')
            except Exception:
                return conflict(job_id, 'recovery_held')
            if (not job['content_pin'] or job['captured_input'] is None or
                    hashlib.sha256(job['captured_input']).hexdigest() != job['content_pin']):
                return conflict(job_id, 'recovery_held')
            if (len(attempts) >= 4 or sum(a['stage'] == 'processing' for a in attempts) >= 2 or
                    sum(a['wall_seconds'] for a in attempts) >= 120 or sum(a['cpu_seconds'] for a in attempts) >= 60):
                return conflict(job_id, 'resource_exhausted')
            from nilaris.admission import _admission_usage
            if job['held_active_reservation'] and _admission_usage(connection)['waiting_jobs'] >= job['profile']['waiting_jobs']:
                return conflict(job_id, 'waiting_capacity')
            usage = retained_usage(connection, job_id)
            recovery = dict(revision=job['revision'] + 1, generation=job['generation'] + 1, actor=actor, reason=reason)
            if (usage['serialized_result_bytes'] >= 2 * 1024**2 or
                    usage['retained_bytes'] + 8192 + len(canonical(recovery)) > job['profile']['job_bytes']):
                return conflict(job_id, 'resource_exhausted')
            connection.execute('INSERT INTO job_recoveries (job_id,revision,generation,actor,reason) VALUES (%s,%s,%s,%s,%s)',
                               (job_id, recovery['revision'], recovery['generation'], actor, reason))
            connection.execute("UPDATE admission_jobs SET state='queued',held_active_reservation=false,reason='reviewed_interruption_retry',revision=revision+1,generation=generation+1 WHERE id=%s", (job_id,))
    # Release the transaction connection before the public status read opens its
    # own; the independently supervised worker must retain its connection slot.
    return ingestion.status(job_id)


class ControlledSource:
    """Labeled finite read-only synthetic adapter, not a live Drive adapter."""
    label = 'controlled-read-only-source-v1'

    def __init__(self, objects):
        self._objects = dict(objects)

    def read(self, source_id, revision, limit):
        entry = self._objects.get(source_id)
        if entry is None or entry[0] != revision:
            return None
        content = entry[1]
        if not isinstance(content, bytes) or len(content) > limit:
            return None
        return content


def current(connection, job_id, claim, stage, *, scoped_binding=None):
    if (not isinstance(claim, dict) or claim.get('job_id') != job_id or
            type(claim.get('generation')) is not int or
            not isinstance(claim.get('token'), str) or not isinstance(claim.get('attempt_id'), str)):
        return None
    return connection.execute(
        """SELECT j.*, a.id AS attempt_id FROM admission_jobs j JOIN job_attempts a
        ON a.job_id=j.id AND a.generation=j.generation
        WHERE j.id=%s AND j.state='running' AND j.generation=%s
        AND a.id=%s AND a.token=%s AND a.stage=%s AND NOT a.stopped AND a.expires_at>clock_timestamp()
        AND (a.worker IS NULL OR a.accounting_complete)
        AND j.input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s
        FOR UPDATE OF j,a""",
        (job_id, claim['generation'], claim['attempt_id'], claim['token'], stage, scoped_binding)).fetchone()


def finite_acquisition_limit(connection, job_id, attempt_id):
    total = connection.execute('''SELECT coalesce(sum(wall_seconds),0) AS wall,
        coalesce(sum(cpu_seconds),0) AS cpu FROM job_attempts WHERE job_id=%s''', (job_id,)).fetchone()
    if total['wall'] >= 120:
        return 'job_wall_limit'
    if total['cpu'] >= 60:
        return 'job_cpu_limit'
    usage = connection.execute('SELECT wall_seconds,cpu_seconds FROM job_attempts WHERE id=%s',
                               (attempt_id,)).fetchone()
    if usage['wall_seconds'] >= 30:
        return 'attempt_wall_limit'
    if usage['cpu_seconds'] >= 20:
        return 'attempt_cpu_limit'
    return None


def capture(ingestion, job_id, claim, source):
    if not isinstance(source, ControlledSource):
        return conflict(job_id, 'unapproved_source')
    # Only this bounded controlled source runs here. General I/O/process supervision
    # is not supplied by these primitives and must precede live adapter execution.
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        job = current(connection, job_id, claim, 'acquisition', scoped_binding=ingestion.scoped_binding)
        if not job:
            if (isinstance(claim, dict) and claim.get('job_id') == job_id and
                    type(claim.get('generation')) is int and isinstance(claim.get('attempt_id'), str) and
                    isinstance(claim.get('token'), str)):
                exhausted = connection.execute(
                    """SELECT a.id FROM admission_jobs j JOIN job_attempts a
                    ON a.job_id=j.id AND a.generation=j.generation
                    WHERE j.id=%s AND j.state='held' AND j.reason='acquisition_request_limit'
                    AND a.id=%s AND a.generation=%s AND a.token=%s AND a.stage='acquisition'
                    AND a.stopped AND a.worker IS NULL AND a.completion_evidence='finite_controlled_call'
                    AND (SELECT sum(acquisition_requests) FROM job_attempts WHERE job_id=j.id)>=12""",
                    (job_id, claim['attempt_id'], claim['generation'], claim['token'])).fetchone()
                if exhausted:
                    return conflict(job_id, 'resource_exhausted')
            return conflict(job_id, 'stale_commit')
        requests = connection.execute(
            'SELECT coalesce(sum(acquisition_requests),0) AS total FROM job_attempts WHERE job_id=%s',
            (job_id,)).fetchone()['total']
        reading = connection.execute('SELECT acquisition_read_token FROM job_attempts WHERE id=%s',
                                     (job['attempt_id'],)).fetchone()
        if reading['acquisition_read_token'] is not None:
            return conflict(job_id, 'acquisition_in_progress')
        limit_reason = finite_acquisition_limit(connection, job_id, job['attempt_id'])
        if limit_reason:
            connection.execute("UPDATE admission_jobs SET state='held',reason=%s,held_active_reservation=true,revision=revision+1 WHERE id=%s",
                               (limit_reason, job_id))
            connection.execute("UPDATE job_attempts SET stopped=true,completion_evidence='finite_controlled_call',reason=%s WHERE id=%s AND worker IS NULL",
                               (limit_reason, job['attempt_id']))
            return conflict(job_id, 'resource_exhausted')
        if requests >= 12:
            return conflict(job_id, 'resource_exhausted')
        # Charge before the external read so a failed call or rejected late result
        # cannot roll back consumed acquisition budget.
        read_token = uuid.uuid4().hex
        connection.execute('UPDATE job_attempts SET acquisition_requests=acquisition_requests+1,acquisition_read_token=%s WHERE id=%s',
                           (read_token, job['attempt_id']))
        # The finite read is external to PostgreSQL. Keep its charged request,
        # but hold no transaction/row lock while waiting for the adapter.
        connection.commit()
        started_wall, started_cpu = time.monotonic(), time.process_time()
        try:
            content = source.read(job['input']['source_id'], job['input']['source_revision'],
                                  job['profile']['input_bytes'])
        except Exception:
            wall_seconds, cpu_seconds = time.monotonic() - started_wall, time.process_time() - started_cpu
            connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            finished_read = connection.execute('''UPDATE job_attempts SET wall_seconds=wall_seconds+%s,
                cpu_seconds=cpu_seconds+%s,measured_cpu_seconds=measured_cpu_seconds+%s,
                cpu_accounting='measured_finite_source_call',acquisition_read_token=NULL
                WHERE id=%s AND acquisition_read_token=%s RETURNING id''',
                (wall_seconds, cpu_seconds, cpu_seconds, job['attempt_id'], read_token)).fetchone()
            if not finished_read:
                connection.rollback()
                raise
            # The synchronous controlled call has returned. Only a direct call
            # with no OS worker identity establishes finite termination here.
            attempt = connection.execute('SELECT worker FROM job_attempts WHERE id=%s',
                                         (job['attempt_id'],)).fetchone()
            still_current = current(connection, job_id, claim, 'acquisition', scoped_binding=ingestion.scoped_binding)
            limit_reason = finite_acquisition_limit(connection, job_id, job['attempt_id'])
            if not still_current and attempt['worker'] is None:
                connection.execute("""UPDATE job_attempts SET stopped=true,
                    completion_evidence='finite_controlled_call',reason='claim_lost' WHERE id=%s""",
                    (job['attempt_id'],))
                connection.execute("""UPDATE admission_jobs SET state='held',reason='claim_lost',
                    held_active_reservation=true,revision=revision+1,generation=generation+1
                    WHERE id=%s AND state='running' AND generation=%s""", (job_id, claim['generation']))
            elif (limit_reason or requests + 1 >= 12) and attempt['worker'] is None and still_current:
                reason = limit_reason or 'acquisition_request_limit'
                connection.execute("UPDATE admission_jobs SET state='held',reason=%s,held_active_reservation=true,revision=revision+1 WHERE id=%s",
                                   (reason, job_id))
                connection.execute("UPDATE job_attempts SET stopped=true,completion_evidence='finite_controlled_call',reason=%s WHERE id=%s",
                                   (reason, job['attempt_id']))
            # A failed read consumed resources too; keep its measurement even
            # though raising rolls back any subsequent capture transaction.
            connection.commit()
            raise
        wall_seconds, cpu_seconds = time.monotonic() - started_wall, time.process_time() - started_cpu
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        finished_read = connection.execute('''UPDATE job_attempts SET wall_seconds=wall_seconds+%s,
            cpu_seconds=cpu_seconds+%s,measured_cpu_seconds=measured_cpu_seconds+%s,
            cpu_accounting='measured_finite_source_call',acquisition_read_token=NULL
            WHERE id=%s AND acquisition_read_token=%s RETURNING id''',
            (wall_seconds, cpu_seconds, cpu_seconds, job['attempt_id'], read_token)).fetchone()
        if not finished_read:
            return conflict(job_id, 'stale_commit')
        if not current(connection, job_id, claim, 'acquisition', scoped_binding=ingestion.scoped_binding):
            # The finite source call actually returned. This records completion
            # and measured consumption, never its stale input result. A reviewed
            # revocation/newer generation keeps its own state and reason.
            finished = connection.execute("""UPDATE job_attempts SET stopped=true,
                completion_evidence='finite_controlled_call',reason='claim_lost'
                WHERE id=%s AND worker IS NULL RETURNING id""", (job['attempt_id'],)).fetchone()
            if finished:
                connection.execute("""UPDATE admission_jobs SET state='held',reason='claim_lost',
                    held_active_reservation=true,revision=revision+1,generation=generation+1
                    WHERE id=%s AND state='running' AND generation=%s""", (job_id, claim['generation']))
            connection.commit()
            return conflict(job_id, 'stale_commit')
        refusal = record_captured_input(ingestion, connection, job_id, claim, job, content, requests)
        if refusal is not None:
            return refusal
    return ingestion.status(job_id)


def record_captured_input(ingestion, connection, job_id, claim, job, content, requests):
    """Publish returned finite source bytes and finish the acquisition under its original fence."""
    # Returned-call accounting precedes speculative input writes. A lease
    # lost at the final fence must discard only those input writes.
    connection.execute('SAVEPOINT capture_input')
    limit_reason = finite_acquisition_limit(connection, job_id, job['attempt_id'])
    if limit_reason:
        state, reason = 'held', limit_reason
    elif content is None:
        state, reason = 'held', ('acquisition_request_limit' if requests + 1 >= 12 else 'source_version_unavailable')
    elif job['input'].get('connector') == 'synthetic-drive-v1' and (
            len(content) != job['input']['input_bytes'] or
            hashlib.sha256(content).hexdigest() != job['input']['expected_sha256']):
        state, reason = 'held', 'source_pin_mismatch'
    else:
        state, reason = 'queued', 'pinned_input_captured'
        pin = hashlib.sha256(content).hexdigest()
        processing_key = [job['input'][key] if key != 'content_pin' else pin for key in (
            'document_id', 'content_pin', 'case_id', 'membership_revision', 'product_type',
            'processing_contract', 'reference_set', 'selection_policy', 'intent_id')]
        versions = connection.execute(
            """SELECT captured_input FROM admission_jobs WHERE input->>'document_id'=%s AND content_pin=%s
                AND input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s""",
            (job['input']['document_id'], pin, ingestion.scoped_binding)).fetchall()
        winner = connection.execute('SELECT id FROM admission_jobs WHERE processing_key=%s',
                                    (Jsonb(processing_key),)).fetchone()
        if any(version['captured_input'] != content for version in versions):
            state, reason = 'held', 'input_integrity_conflict'
        elif winner:
            state, reason = 'completed', 'duplicate-of'
            connection.execute('UPDATE admission_jobs SET canonical_job_id=%s WHERE id=%s',
                               (winner['id'], job_id))
        else:
            connection.execute('UPDATE admission_jobs SET processing_key=%s,canonical_job_id=id WHERE id=%s',
                               (Jsonb(processing_key), job_id))
        connection.execute('UPDATE admission_jobs SET content_pin=%s, captured_input=%s WHERE id=%s',
                           (pin, content, job_id))
    # Recheck DB time at the actual write boundary even for the finite adapter.
    if not current(connection, job_id, claim, 'acquisition', scoped_binding=ingestion.scoped_binding):
        connection.execute('ROLLBACK TO SAVEPOINT capture_input')
        finished = connection.execute("""UPDATE job_attempts SET stopped=true,
            completion_evidence='finite_controlled_call',reason='claim_lost'
            WHERE id=%s AND worker IS NULL RETURNING id""", (job['attempt_id'],)).fetchone()
        if finished:
            connection.execute("""UPDATE admission_jobs SET state='held',reason='claim_lost',
                held_active_reservation=true,revision=revision+1,generation=generation+1
                WHERE id=%s AND state='running' AND generation=%s""", (job_id, claim['generation']))
        return conflict(job_id, 'stale_commit')
    # Acquisition is finite and fully accounted at this transaction boundary.
    # Keep its active-capacity reservation across the nonterminal queued/held
    # handoff; a later eligible claim transfers it under the admission lock.
    connection.execute('UPDATE admission_jobs SET state=%s, reason=%s, held_active_reservation=%s, revision=revision+1 WHERE id=%s',
                       (state, reason, state != 'completed', job_id))
    connection.execute("UPDATE job_attempts SET stopped=true, completion_evidence='finite_controlled_call', reason=%s WHERE id=%s",
                       (reason, job['attempt_id']))
    if state == 'completed':
        connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                           (retained_usage(connection, job_id)['retained_bytes'], job_id))



def result(ingestion, job_id, cursor=None, proposal_id=None, provenance=False):
    from nilaris.semantic_publication import VERSION, publication_view, provenance_page
    if (type(provenance) is not bool or
            (provenance and (not isinstance(proposal_id,str) or not proposal_id.strip() or len(proposal_id) > 256)) or
            (not provenance and proposal_id is not None)):
        return conflict(job_id, 'invalid_request')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        exists = connection.execute("""SELECT id,canonical_job_id,lineage FROM admission_jobs WHERE id=%s
            AND input->>'scoped_intake_binding' IS NOT DISTINCT FROM %s""", (job_id,ingestion.scoped_binding)).fetchone()
        if not exists:
            return conflict(job_id, 'not_found')
        if exists['canonical_job_id'] and exists['canonical_job_id'] != job_id:
            return dict(known=True, job_id=job_id, disposition='duplicate-of',
                        canonical_job_id=exists['canonical_job_id'], effects=[], publications=[],
                        approved=False, next_action='query_canonical_job')
        _, publications = outputs(connection, job_id)
        if provenance:
            selected = next((p for p in publications if p['id'] == proposal_id),None)
            if selected is None or selected.get('stage_contract') != VERSION:
                return conflict(job_id, 'not_found')
            return provenance_page(selected,cursor)
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:
                    raise ValueError
                owner, cutoff, offset = json.loads(base64.b64decode(cursor, validate=True))
                if (owner != job_id or type(offset) is not int or offset < 0 or
                        not isinstance(cutoff, str)):
                    raise ValueError
                index = next(i for i, publication in enumerate(publications) if publication['id'] == cutoff)
                publications = publications[:index + 1]
            except (ValueError, TypeError, StopIteration):
                return conflict(job_id, 'invalid_cursor')
        # Reconstruct exclusively from the pinned immutable publications. A later
        # supplement cannot change either the contents or offsets of this stream.
        wanted = {(kind, payload['id']) for publication in publications
                  for kind, field in (('observation', 'observations'), ('association', 'associations'),
                                      ('candidate', 'candidates')) for payload in publication[field]}
        effects, _ = outputs(connection, job_id)
        effects = [effect for effect in effects if (effect['kind'], effect['payload']['id']) in wanted]
    publications = [publication_view(p) for p in publications]
    result = dict(known=True, job_id=job_id, effects=effects, publications=publications,
                  approved=False, next_action='review_published_proposal' if publications else 'query_job')
    if exists['lineage'] is not None:
        result['lineage'] = exists['lineage']
    encoded = canonical(result)
    if cursor is None and len(json.dumps(result).encode()) <= 120000:
        return result
    if not publications or offset >= len(encoded) or offset % 24000:
        return conflict(job_id, 'invalid_cursor')
    end = min(offset + 24000, len(encoded))
    next_cursor = (base64.b64encode(canonical([job_id, publications[-1]['id'], end]).encode('ascii')).decode('ascii')
                   if end < len(encoded) else None)
    return dict(known=True, job_id=job_id, encoding='json-ascii',
                publication_ids=[publication['id'] for publication in publications],
                offset=offset, fragment=encoded[offset:end], next_cursor=next_cursor)


def outputs(connection, job_id):
    effects = connection.execute(
        'SELECT stage_contract,kind,ordinal,payload,support_id FROM job_effects WHERE job_id=%s ORDER BY kind DESC,ordinal',
        (job_id,)).fetchall()
    publications = [row['payload'] for row in connection.execute(
        'SELECT payload FROM job_publications WHERE job_id=%s ORDER BY sequence', (job_id,)).fetchall()]
    return effects, publications


def publish(ingestion, job_id, claim, output):
    try:
        if not isinstance(output, StageOutput):
            raise ValueError('typed output required')
        snapshot = output.snapshot()
    except (ValueError, TypeError, RecursionError):
        return conflict(job_id, 'malformed_output')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        prior = connection.execute('SELECT request=%s AS same_request FROM job_publications WHERE job_id=%s AND attempt_id=%s',
                                   (Jsonb(snapshot),job_id, claim.get('attempt_id'))).fetchone() if isinstance(claim, dict) else None
        job = current(connection, job_id, claim, 'processing')
        if not job:
            # An identical lost-ack replay only reads the already committed outcome;
            # no expired/revoked claim can write, change state or publish new output.
            if prior and prior['same_request']:
                return ingestion.status(job_id)
            return conflict(job_id, 'stale_commit')
        existing, publications = outputs(connection, job_id)
        by_key = {(e['stage_contract'], e['kind'], e['ordinal']): e for e in existing}
        observations = {e['payload']['id'] for e in existing if e['kind'] == 'observation'}
        observations.update(e['payload']['id'] for e in snapshot['effects'] if e['kind'] == 'observation')
        new_effects = []
        by_id = {e['payload']['id']: (e['stage_contract'], e['kind'], e['ordinal']) for e in existing}
        for effect in snapshot['effects']:
            payload = effect['payload']
            if effect['kind'] != 'observation' and not set(payload['observation_ids']) <= observations:
                return conflict(job_id, 'malformed_output')
            old = by_key.get((snapshot['stage_contract'], effect['kind'], effect['ordinal']))
            same_payload = (connection.execute(
                'SELECT payload=%s AS same_payload FROM job_effects WHERE job_id=%s AND stage_contract=%s AND kind=%s AND ordinal=%s',
                (Jsonb(payload),job_id,snapshot['stage_contract'],effect['kind'],effect['ordinal'])).fetchone()['same_payload']
                if old else True)
            if not same_payload:
                connection.execute("UPDATE admission_jobs SET state='held',reason='effect_conflict',revision=revision+1,generation=generation+1 WHERE id=%s", (job_id,))
                connection.execute("UPDATE job_attempts SET stopped=true,reason='effect_conflict' WHERE id=%s", (job['attempt_id'],))
                return dict(known=True, category='effect_conflict', affected=dict(job_id=job_id),
                            coverage='previous_publications_preserved', reason='effect key already has different payload',
                            next_action='reconcile_interruption')
            if payload['id'] in by_id and by_id[payload['id']] != (snapshot['stage_contract'], effect['kind'], effect['ordinal']):
                return conflict(job_id, 'malformed_output')
            if not old:
                new_effects.append(effect)
        pending_effects = []
        for effect in new_effects:
            support_id = None
            if effect['kind'] == 'observation':
                support_id = hashlib.sha256(canonical([job['input']['document_id'], job['content_pin'],
                    effect['payload']['locator'], effect['payload']['kind']]).encode()).hexdigest()
            pending_effects.append(dict(stage_contract=snapshot['stage_contract'], kind=effect['kind'],
                                        ordinal=effect['ordinal'], payload=effect['payload'], support_id=support_id))
        publication_id = str(uuid.uuid4())
        sequence = len(publications)
        publication = dict(id=publication_id, contract=snapshot['contract'], job_id=job_id,
            kind='initial' if not publications else 'supplement', sequence=sequence,
            initial_publication_id=publications[0]['id'] if publications else publication_id,
            approved=False, adapter=snapshot['adapter'], outcome=snapshot['outcome'],
            coverage=snapshot['coverage'], content_pin=job['content_pin'],
            context={key: job['input'][key] for key in ('document_id','source_revision','case_id',
                'membership_revision','product_type','processing_contract','reference_set','selection_policy','intent_id')},
            profile=job['profile'],
            observations=[e['payload'] for e in new_effects if e['kind']=='observation'],
            associations=[e['payload'] for e in new_effects if e['kind']=='association'],
            candidates=[e['payload'] for e in new_effects if e['kind']=='candidate'])
        if snapshot['adapter'] == 'fixture-native-v1':
            publication['context_origin'] = 'supplied'
        from nilaris.review import prepare_publication_base, record_publication_base
        review_base = prepare_publication_base(connection,publication)
        usage = retained_usage(connection, job_id)
        serialized = usage['serialized_result_bytes'] + len(canonical(publication))
        retained = (usage['retained_bytes'] + review_base['reservation_bytes'] + len(canonical(snapshot)) + len(canonical(publication)) + 8192 +
                    sum(4096 + len(canonical(effect)) for effect in pending_effects))
        if serialized > 2 * 1024**2 or retained > job['profile']['job_bytes']:
            reason = 'serialized_result_limit' if serialized > 2 * 1024**2 else 'retained_byte_limit'
            connection.execute("UPDATE admission_jobs SET state='held',reason=%s,revision=revision+1,generation=generation+1 WHERE id=%s", (reason,job_id))
            connection.execute("UPDATE job_attempts SET stopped=true,reason=%s,completion_evidence=coalesce(completion_evidence,'finite_controlled_call') WHERE id=%s", (reason,job['attempt_id']))
            return dict(known=True, category='resource_exhausted', affected=dict(job_id=job_id),
                        coverage='previous_publications_preserved', reason=reason, next_action='query_job')
        for effect in pending_effects:
            connection.execute('INSERT INTO job_effects (job_id,stage_contract,kind,ordinal,payload,support_id) VALUES (%s,%s,%s,%s,%s,%s)',
                (job_id,effect['stage_contract'],effect['kind'],effect['ordinal'],Jsonb(effect['payload']),effect['support_id']))
        connection.execute('INSERT INTO job_publications (id,job_id,attempt_id,sequence,request,payload) VALUES (%s,%s,%s,%s,%s,%s)',
                           (publication_id,job_id,job['attempt_id'],sequence,Jsonb(snapshot),Jsonb(publication)))
        record_publication_base(connection,review_base)
        # The guarded row remains locked; expiration is checked at the final write.
        if not current(connection, job_id, claim, 'processing'):
            connection.rollback()
            return conflict(job_id, 'stale_commit')
        connection.execute('UPDATE admission_jobs SET state=%s,reason=%s,revision=revision+1 WHERE id=%s',
                           (snapshot['outcome'], 'published_' + snapshot['outcome'],job_id))
        connection.execute("UPDATE job_attempts SET stopped=true,reason='published',completion_evidence=coalesce(completion_evidence,'finite_controlled_call') WHERE id=%s", (job['attempt_id'],))
        if snapshot['outcome'] in ('completed', 'partial'):
            connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                               (retained_usage(connection, job_id)['retained_bytes'], job_id))
    return ingestion.status(job_id)
