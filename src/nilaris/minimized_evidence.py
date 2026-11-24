"""Persist and read minimized evidence from supported immutable publications."""
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris import passage_rfq_minimization, block_rfq_minimization, pump_rfq_minimization, quantity_rfq_minimization, qualified_rfq_minimization

CONTRACT = 'minimized-evidence-v1'
DETECTOR = 'synthetic-span-detector-v1'
FIXTURE_ADAPTER = 'ticket05-synthetic-evidence-v1'
OBSERVATION = 'technical-excerpt'
TRACED_RULES = (qualified_rfq_minimization, passage_rfq_minimization)
RFQ_RULES = {module.PUBLICATION_ADAPTER: module for module in (pump_rfq_minimization, block_rfq_minimization, quantity_rfq_minimization, *TRACED_RULES)}
OBSERVATION_BY_ADAPTER = {FIXTURE_ADAPTER: OBSERVATION,
                          **{adapter: rules.OBSERVATION_ID for adapter, rules in RFQ_RULES.items()}}
ROW_RESERVATION_BYTES = 8192
TECHNICAL_TEXT = 'At 20 °C, flow is at least 12.5 m³/h when inlet pressure is no more than 3.2 bar.'
SAFE_PREFIX = 'Contact: Avery Example; quote: Q-731 for Example Pump Works.\n'
CONFLICT_PREFIX = 'Confidential specification includes the upper pressure limit.\n'
PREVIEW_ROLES = frozenset({'Reviewer'})
ROUTES = frozenset({'/minimized-evidence/read', '/minimized-evidence/build', '/minimized-evidence/source'})

MIGRATION = """
CREATE TABLE IF NOT EXISTS minimized_evidence (
    publication_id text NOT NULL REFERENCES job_publications(id),
    observation_id text NOT NULL,
    contract text NOT NULL,
    detector text NOT NULL,
    preview_text text,
    hold_reason text,
    PRIMARY KEY (publication_id, observation_id, contract),
    CHECK ((preview_text IS NULL) <> (hold_reason IS NULL))
);
ALTER TABLE minimized_evidence ADD COLUMN IF NOT EXISTS private_trace jsonb;
"""


def detect_synthetic_spans(excerpt):
    """Return sensitive ranges for two scripted cases, or unresolved."""
    if excerpt == SAFE_PREFIX + TECHNICAL_TEXT:
        return ((0, len(SAFE_PREFIX)),)
    if excerpt == CONFLICT_PREFIX + TECHNICAL_TEXT:
        start = excerpt.index('no more than')
        return ((0, len(CONFLICT_PREFIX)), (start, len(excerpt)))
    return None


def transform(excerpt):
    spans = detect_synthetic_spans(excerpt)
    if spans is None:
        return None, 'sensitivity_unresolved'
    candidate = excerpt
    for start, end in reversed(spans):
        candidate = candidate[:start] + candidate[end:]
    # The synthetic contract declares the entire technical sentence verbatim.
    if candidate != TECHNICAL_TEXT:
        return None, 'technical_meaning_at_risk'
    return candidate, None


def _build_preview(connection, workspace, job, key, detector, source, rules):
    from nilaris.admission import _admission_usage
    from nilaris.execution import retained_usage

    job_row = connection.execute('SELECT profile, reservation_bytes FROM admission_jobs WHERE id=%s',
                                 (job,)).fetchone()
    profile = job_row['profile']
    retained_after = retained_usage(connection, job)['retained_bytes'] + ROW_RESERVATION_BYTES
    usage = _admission_usage(connection)
    occupied = usage['job_reservation_bytes'] + usage['receipt_reservation_bytes']
    if (retained_after > profile['job_bytes'] or
            occupied + max(0, retained_after - job_row['reservation_bytes']) >
            profile['logical_bytes'] - profile['terminal_headroom_bytes']):
        raise RuntimeError('minimized preview capacity unavailable')

    try:
        trace = None
        if rules in TRACED_RULES:
            preview, reason, trace = rules.decide(source['excerpt'], complete=source['complete'])
        elif rules is not None:
            preview, reason = rules.transform(source['excerpt'], complete=source['complete'])
        else:
            preview, reason = transform(source['excerpt'])
    except Exception:
        # A detector can include private source text in any exception, including
        # PermissionError. Only the access boundary may issue a Reviewer denial.
        raise RuntimeError('local evidence transform unavailable') from None
    workspace.review.check_mutation()
    connection.execute("""
        INSERT INTO minimized_evidence
          (publication_id, observation_id, contract, detector, preview_text, hold_reason)
        VALUES (%s, %s, %s, %s, %s, %s)
        """, (*key, detector, preview, reason))
    if trace is not None:
        connection.execute('''UPDATE minimized_evidence SET private_trace=%s
            WHERE publication_id=%s AND observation_id=%s AND contract=%s''', (Jsonb(trace), *key))
    # Terminal jobs have released the original job allowance. Charge this row in
    # the same transaction so the next admission observes the retained bytes.
    connection.execute('''
        UPDATE admission_jobs SET reservation_bytes=GREATEST(reservation_bytes,%s)
        WHERE id=%s AND state IN ('completed','partial','cancelled','quarantined','failed')
            AND NOT held_active_reservation
        ''', (retained_after, job))
    return dict(contract=key[2], detector=detector, preview_text=preview, hold_reason=reason)


def handle(path, body, service):
    building = path == '/minimized-evidence/build'
    opening_original = path == '/minimized-evidence/source'
    fields = {'customer_id', 'job_id'}
    if building or opening_original:
        fields.add('expected_scope')
    if set(body) != fields or any(not isinstance(value, str) or len(value) > 128
                                  for value in body.values()):
        raise ValueError('select the controlled publication')
    job = body['job_id']
    if str(UUID(job)) != job:
        raise ValueError('select the controlled publication')

    # The registry locks the named customer and supplies only that tenant's database.
    with service.review_boundary(body['customer_id'], PREVIEW_ROLES) as workspace:
        scope = workspace.namespace(job)
        if (building or opening_original) and scope != body['expected_scope']:
            raise PermissionError('evidence access refused')
        with psycopg.connect(**workspace.ingestion.database, row_factory=dict_row) as connection:
            if building:
                connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            source = connection.execute("""
                SELECT p.id AS publication_id, supported.adapter, supported.observation_id,
                       p.payload->>'outcome'='completed'
                           AND o.value->>'presence'='observed' AS complete,
                       CASE WHEN %(original)s THEN p.payload->'context'->>'document_id' END AS document_id,
                       CASE WHEN %(original)s THEN p.payload->'context'->>'source_revision' END AS source_revision,
                       CASE WHEN %(original)s THEN p.payload->'content_pin' END AS content_pin,
                       CASE WHEN %(original)s THEN o.value->'locator' END AS locator,
                       CASE WHEN %(excerpt)s THEN o.value->>'original_content' END AS excerpt
                FROM job_publications p
                JOIN unnest(%(adapters)s::text[], %(observations)s::text[]) supported(adapter, observation_id)
                    ON p.payload->>'adapter'=supported.adapter
                JOIN LATERAL jsonb_array_elements(p.payload->'observations') o
                    ON o.value->>'id'=supported.observation_id
                WHERE p.job_id=%(job)s AND p.sequence=0 AND p.publication_origin='stage'
                """, dict(job=job, adapters=list(OBSERVATION_BY_ADAPTER), observations=list(OBSERVATION_BY_ADAPTER.values()),
                          original=opening_original, excerpt=building or opening_original)).fetchone()
            if source is None:
                raise PermissionError('evidence access refused')
            rules = RFQ_RULES.get(source.pop('adapter'))
            contract = rules.CONTRACT if rules else CONTRACT
            detector = rules.DETECTOR if rules else DETECTOR
            # The row keys pin the immutable observation. Its locator and provenance stay with the original.
            key = (source['publication_id'], source['observation_id'], contract)
            saved = connection.execute("""
                SELECT contract, detector, preview_text, hold_reason FROM minimized_evidence
                WHERE publication_id=%s AND observation_id=%s AND contract=%s
                """, key).fetchone()
            if building and saved is None:
                saved = _build_preview(connection, workspace, job, key, detector, source, rules)
            if opening_original:
                if saved is None:
                    raise PermissionError('evidence access refused')
                source.pop('complete')
                if rules in TRACED_RULES:
                    trace = connection.execute('''SELECT private_trace FROM minimized_evidence
                        WHERE publication_id=%s AND observation_id=%s AND contract=%s''', key).fetchone()['private_trace']
                    source['trace'] = trace
                    if 'selected' in trace:
                        source['selected_blocks'] = [trace['selected']]
                if rules in (block_rfq_minimization, quantity_rfq_minimization) and saved['preview_text'] is not None:
                    source['selected_blocks'] = rules.selected_blocks(source['excerpt'], saved['preview_text'])
                if rules is pump_rfq_minimization and saved['preview_text'] is not None:
                    source['selected_blocks'] = pump_rfq_minimization.SELECTED_BLOCKS
                return dict(state='original', **source, contract=saved['contract'], detector=saved['detector'])
            if saved is None:
                return dict(state='absent', scope=scope)
            return dict(state='held' if saved['hold_reason'] else 'preview', scope=scope, **saved)
