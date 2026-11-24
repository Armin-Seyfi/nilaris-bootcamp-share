"""Durable, bounded expert review commands over immutable published evidence."""
import base64
import hashlib
import json
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.admission import (_ConnectionOptions, _persistence_boundary,
                               _admission_usage)
from nilaris.rfq_stage_decision import DIAMETER_ID, IMPORT_CONTRACT, STAGES
from nilaris.stage_matrix import matrix, exact_proposal, record_decision, stage_changes
from nilaris.results import canonical
from nilaris.semantic_publication import publication_view
from nilaris.review_scenarios import available_registry, validation_errors as scenario_errors
from nilaris.review_equipment import (validation_errors as equipment_errors,
                                      with_origin as equipment_with_origin)

_SCENARIO_OMITTED = object()
_EQUIPMENT_OMITTED = object()

MIGRATION = '''
CREATE TABLE IF NOT EXISTS review_publication_bases (
    proposal_id text PRIMARY KEY REFERENCES job_publications(id),
    job_id text NOT NULL REFERENCES admission_jobs(id),
    base_version text,
    reservation_bytes bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS review_drafts (
    id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES admission_jobs(id),
    proposal_id text NOT NULL REFERENCES job_publications(id),
    product_type text NOT NULL,
    base_version text,
    actor text NOT NULL,
    revision bigint NOT NULL DEFAULT 0,
    approval_id text,
    reservation_bytes bigint NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS review_active_product_type
    ON review_drafts(product_type) WHERE approval_id IS NULL;
CREATE TABLE IF NOT EXISTS review_revisions (
    draft_id text NOT NULL REFERENCES review_drafts(id),
    revision bigint NOT NULL,
    payload jsonb NOT NULL,
    reservation_bytes bigint NOT NULL,
    PRIMARY KEY(draft_id, revision)
);
CREATE TABLE IF NOT EXISTS review_approvals (
    id text PRIMARY KEY,
    draft_id text NOT NULL UNIQUE REFERENCES review_drafts(id),
    product_type text NOT NULL,
    version bigint NOT NULL,
    payload jsonb NOT NULL,
    reservation_bytes bigint NOT NULL,
    UNIQUE(product_type, version)
);
CREATE TABLE IF NOT EXISTS review_receipts (
    sequence bigserial UNIQUE,
    namespace text NOT NULL,
    command_id text NOT NULL,
    job_id text NOT NULL REFERENCES admission_jobs(id),
    request jsonb NOT NULL,
    result jsonb NOT NULL,
    reservation_bytes bigint NOT NULL,
    PRIMARY KEY(namespace, command_id)
);
'''


def refusal(category, **fields):
    return dict(known=True, saved=False, approved=False, category=category, **fields)


def _receipt(connection, namespace, command_id, expected_request):
    # Apply the same typed JSONB equality for initial replay and later reconciliation.
    prior = connection.execute("""
        SELECT result,request=%s AS same_request FROM review_receipts
        WHERE namespace=%s AND command_id=%s""",
        (Jsonb(expected_request), namespace, command_id)).fetchone()
    if prior is None:
        return None
    if expected_request is not None and not prior['same_request']:
        return refusal('command_conflict')
    return prior['result']


def _bounded(value, limit=65536):
    try:
        size = 0
        for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False,sort_keys=True).iterencode(value):
            size += len(chunk)
            if size > limit:
                return False
        return True
    except (TypeError, ValueError, RecursionError):
        return False


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _without_origin(record):
    return {key:value for key,value in record.items() if key != 'origin'} if record is not None else None


def _manual_errors(definitions, associations, previous, initial):
    """Validate the source-free contract without repairing editable draft content."""
    errors = []

    def error(field, reason):
        if len(errors) < 64:
            errors.append(dict(field=field,reason=reason))

    prior = {d['id']: d for d in previous['definitions']}
    initial_ids = {candidate['id'] for candidate in initial['candidates']}
    identities = [d['id'] for d in definitions]
    manual_ids = {d['id'] for d in definitions if d.get('definition_kind') == 'human_manual_v1'}
    for identity, original in prior.items():
        if original.get('definition_kind') == 'human_manual_v1' and identity not in identities:
            error('definitions.' + identity + '.definition_kind','Retain the manual definition; explicitly exclude instead of deleting it')
    allowed = {'id','definition_kind','label','value_type','unit','requirements','conditions',
               'observation_ids','decision','origin'}
    for index, definition in enumerate(definitions):
        path = f'definitions[{index}]'
        identity = definition['id']
        kind = definition.get('definition_kind')
        original = prior.get(identity)
        if 'definition_kind' in definition and kind != 'human_manual_v1':
            error(path + '.definition_kind','Only human_manual_v1 is recognized')
        if original and original.get('definition_kind') == 'human_manual_v1' and kind != 'human_manual_v1':
            error(path + '.definition_kind','A persisted manual definition must retain its kind')
        if kind != 'human_manual_v1':
            continue
        if identity in initial_ids or (original and original.get('definition_kind') != 'human_manual_v1'):
            error(path + '.definition_kind','Original candidates and legacy definitions cannot become manual definitions')
        if identities.count(identity) != 1:
            error(path + '.id','Manual identity must be unique and must not collide with another definition')
        for field in sorted(set(definition) - allowed):
            error(path + '.' + field,'Unsupported manual definition field; source links and defaults are not permitted')
        if definition.get('observation_ids') != []:
            error(path + '.observation_ids','Manual definitions require an explicit empty observation list')
        for field in ('requirements','conditions'):
            if definition.get(field) != 'unspecified':
                error(path + '.' + field,'Manual definitions leave this unspecified; use scenario references for applicability')
        if definition.get('decision') not in ('accepted','deferred','rejected'):
            error(path + '.decision','Explicitly accept, defer or reject the manual definition')
    for index, association in enumerate(associations):
        targets = association.get('candidate_ids') if isinstance(association,dict) else None
        if isinstance(targets,list) and any(isinstance(target,str) and target in manual_ids for target in targets):
            error(f'associations[{index}].candidate_ids','Manual definitions cannot be association targets or claim documentary support')
    return errors


def _json_equalities(connection, pairs):
    """Compare one bounded batch with the same typed numeric rules as storage."""
    if not pairs:
        return []
    return [row['same'] for row in connection.execute(
        'SELECT pair->0 = pair->1 AS same FROM jsonb_array_elements(%s) '
        'WITH ORDINALITY AS pairs(pair,position) ORDER BY position', (Jsonb(pairs),)).fetchall()]


def _manual_reused_ids(connection, draft_id, definitions):
    """Retained revisions reserve identities and prevent manual omission/kind switches."""
    manual_ids = [d['id'] for d in definitions if d.get('definition_kind') == 'human_manual_v1']
    omitted = connection.execute('''
        SELECT 1 FROM review_revisions
        CROSS JOIN LATERAL jsonb_array_elements(payload->'definitions') AS items(definition)
        WHERE draft_id=%s AND definition->>'definition_kind'='human_manual_v1'
        AND NOT (definition->>'id'=ANY(%s)) LIMIT 1''',(draft_id,manual_ids)).fetchone()
    if omitted:
        return [dict(field='definitions',reason='Retain every previously saved manual definition with its manual kind; explicitly exclude instead of deleting it')]
    if not manual_ids:
        return []
    reused = {row['id'] for row in connection.execute('''
        SELECT DISTINCT definition->>'id' AS id FROM review_revisions
        CROSS JOIN LATERAL jsonb_array_elements(payload->'definitions') AS items(definition)
        WHERE draft_id=%s AND definition->>'id'=ANY(%s)
        AND definition->>'definition_kind' IS DISTINCT FROM 'human_manual_v1'
        LIMIT 64''', (draft_id,manual_ids))}
    return [dict(field=f'definitions[{index}].definition_kind',
                 reason='A retained legacy identity cannot be reused as a manual definition')
            for index, definition in enumerate(definitions) if definition['id'] in reused][:64]


def approval_evidence(initial, saved):
    """Return only source records needed to inspect the whole saved schema."""
    definitions = [row for row in saved['definitions'] if row.get('decision') == 'accepted']
    links = [row for row in saved['associations'] if isinstance(row, dict) and _text(row.get('id'))
             and row.get('disposition') in ('supported', 'accepted')]
    observations = set()
    for row in definitions + links:
        refs = row.get('observation_ids')
        if isinstance(refs, list) and all(_text(ref) for ref in refs):
            observations.update(refs)
    candidates = {row['id'] for row in definitions}
    associations = {row['id'] for row in links}
    return {
        'id': initial['id'], 'job_id': initial['job_id'], 'kind': initial['kind'],
        'context': {k: initial['context'][k] for k in ('document_id', 'source_revision', 'product_type')},
        'coverage': {}, 'schema_inspection': True,
        'observations': [row for row in initial['observations'] if row['id'] in observations],
        'associations': [row for row in initial['associations'] if row['id'] in associations],
        'candidates': [row for row in initial['candidates'] if row['id'] in candidates],
    }


class Review:
    """Public command seam. All mutations serialize with admission capacity checks."""
    def __init__(self, database, *, check_mutation=None, case_source_allowed=False):
        self.database = _ConnectionOptions(database)
        self.check_mutation = check_mutation
        self.case_source_allowed = case_source_allowed

    @_persistence_boundary
    def promote_semantic(self, namespace, command_id, job_id, expected_revision,
                         expected_base, actor, artifact, capture, configuration,
                         expected_pins, explicit):
        """Local-only explicit import; never dispatch generation or fixture work."""
        if (explicit is not True or type(expected_revision) is not int or expected_revision < 0 or
                not all(_text(v) for v in (namespace,command_id,job_id,actor)) or
                (expected_base is not None and not _text(expected_base))):
            return refusal('invalid_request')
        from nilaris.semantic_publication import build_projection
        try:
            projection = build_projection(artifact,capture,configuration,expected_pins)
        except (ValueError,KeyError,TypeError,RecursionError):
            return refusal('invalid_semantic_projection')
        request = dict(operation='promote_semantic',job_id=job_id,
            expected_revision=expected_revision,expected_base=expected_base,actor=actor,
            projection=projection,explicit=True)
        return self._command(namespace,command_id,request)

    @_persistence_boundary
    def create(self, namespace, command_id, job_id, proposal_id, product_type,
               expected_base, actor):
        request = dict(operation='create', job_id=job_id, proposal_id=proposal_id,
                       product_type=product_type, expected_base=expected_base, actor=actor)
        if (not all(_text(value) for value in (namespace, command_id, job_id, proposal_id,
                                               product_type, actor)) or
                (expected_base is not None and not _text(expected_base))):
            return refusal('invalid_request')
        return self._command(namespace, command_id, request)

    def _command(self, namespace, command_id, request):
        limit = 2 * 1024**2 if request['operation'] == 'promote_semantic' else 65536
        if not _bounded([namespace, command_id, request],limit):
            return refusal('metadata_limit')
        request = json.loads(canonical(request))
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            connection.execute('SELECT pg_advisory_xact_lock(714012001)')
            prior = _receipt(connection, namespace, command_id, request)
            if prior is not None:
                return prior
            operation = request['operation']
            if operation in ('create','promote_semantic'):
                job = connection.execute('SELECT id,profile,state,held_active_reservation,reservation_bytes FROM admission_jobs WHERE id=%s',
                                         (request['job_id'],)).fetchone()
                draft = None
            else:
                draft = connection.execute('SELECT * FROM review_drafts WHERE id=%s',
                                           (request['draft_id'],)).fetchone()
                job = connection.execute('SELECT id,profile,state,held_active_reservation,reservation_bytes FROM admission_jobs WHERE id=%s',
                                         (draft['job_id'],)).fetchone() if draft else None
            if not job:
                return refusal('not_found')
            # Prepare without writes; capacity refusal leaves every record unchanged.
            if draft and operation in ('save', 'approve') and connection.execute(
                    "SELECT 1 FROM job_publications WHERE id=%s AND payload->>'adapter'=%s",
                    (draft['proposal_id'], IMPORT_CONTRACT)).fetchone():
                result, writes = refusal('stage_review_required'), []
            elif operation in ('stage_add', 'stage_save', 'stage_bulk'):
                result, writes = self._prepare_stage_revision(connection, request, draft)
            else:
                result, writes = getattr(self, '_prepare_' + operation)(connection, request, draft)
            receipt_bytes = 8192 + len(canonical([namespace, command_id, request, result]))
            growth = receipt_bytes + sum(write[-1] for write in writes)
            from nilaris.execution import retained_usage
            usage = _admission_usage(connection)
            retained = retained_usage(connection, job['id'])['retained_bytes']
            if (retained + growth > job['profile']['job_bytes'] or
                    usage['job_reservation_bytes'] + usage['receipt_reservation_bytes'] + max(0,retained + growth - job['reservation_bytes']) >
                    job['profile']['logical_bytes'] - job['profile']['terminal_headroom_bytes']):
                return refusal('byte_capacity', receipt_stored=False)
            if self.check_mutation is not None:
                # Contended admission/receipt work may outlive the interactive session.
                self.check_mutation()
            for statement, params, _ in writes:
                connection.execute(statement, params)
            connection.execute('INSERT INTO review_receipts(namespace,command_id,job_id,request,result,reservation_bytes) VALUES(%s,%s,%s,%s,%s,%s)',
                               (namespace, command_id, job['id'], Jsonb(request), Jsonb(result), receipt_bytes))
            # A successful promotion can release a captured handoff reservation.
            # Account from the state written in this transaction, not its old flag.
            current = connection.execute('SELECT state,held_active_reservation FROM admission_jobs WHERE id=%s',
                                         (job['id'],)).fetchone()
            if current['state'] in ('completed','partial','cancelled','quarantined','failed') and not current['held_active_reservation']:
                connection.execute('UPDATE admission_jobs SET reservation_bytes=%s WHERE id=%s',
                                   (retained + growth,job['id']))
            return result

    def _prepare_promote_semantic(self, connection, request, draft):
        from nilaris.semantic_publication import VERSION
        job = connection.execute('SELECT * FROM admission_jobs WHERE id=%s FOR UPDATE',
                                 (request['job_id'],)).fetchone()
        if job['revision'] != request['expected_revision']:
            return refusal('revision_conflict'), []
        attempts = connection.execute('SELECT * FROM job_attempts WHERE job_id=%s',
                                      (job['id'],)).fetchall()
        latest = max(attempts, key=lambda attempt: attempt['generation']) if attempts else None
        # The semantic172 supervisor atomically completes the Job, revokes its
        # owned generation once, and stops that processing attempt. Publication
        # must recognize that exact terminal fence, while queued captures and
        # other completed paths retain the same-generation requirement.
        semantic_completed = (latest is not None and job['state'] == 'completed' and
            job['reason'] == 'semantic_completed' and
            latest['generation'] + 1 == job['generation'] and
            latest['stage'] == 'processing' and latest['reason'] == 'semantic_completed' and
            latest['worker'] is not None and latest['worker'].get('adapter') == 'semantic172-v1')
        if (job['state'] not in ('queued','completed') or
                (job['canonical_job_id'] and job['canonical_job_id'] != job['id']) or
                latest is None or (latest['generation'] != job['generation'] and not semantic_completed) or
                any(not a['stopped'] or a['stage'] not in ('acquisition','processing') or
                    a['acquisition_read_token'] is not None or
                    (a['worker'] is None and a['completion_evidence'] != 'finite_controlled_call') or
                    (a['worker'] is not None and (not a['accounting_complete'] or
                     a['completion_evidence'] != 'process_group_reaped')) for a in attempts)):
            return refusal('active_job_conflict'), []
        from nilaris.worker_lifecycle import processes
        try:
            if any(a['worker'] is not None and
                   (type(a['worker'].get('pgid')) is not int or a['worker']['pgid'] <= 0 or
                    processes(a['worker']['pgid']))
                   for a in attempts):
                return refusal('active_job_conflict'), []
        except Exception:
            return refusal('active_job_conflict'), []
        initial = json.loads(canonical(request['projection']))
        target = initial['semantic_context']['target']
        if (job['input']['connector'] not in ('synthetic','synthetic-drive-v1') or
                target['document_id'] != job['input']['document_id'] or
                target['source_revision'] != job['input']['source_revision'] or
                not job['content_pin'] or target['content_pin'] != job['content_pin'] or
                job['captured_input'] is None or
                hashlib.sha256(job['captured_input']).hexdigest() != job['content_pin'] or
                job['canonical_job_id'] != job['id'] or
                job['semantic_key'] != [job['input'][key] for key in (
                    'document_id','source_revision','case_id','membership_revision','product_type',
                    'processing_contract','reference_set','selection_policy','intent_id')] or
                job['processing_key'] != [job['input'][key] if key != 'content_pin' else job['content_pin']
                    for key in ('document_id','content_pin','case_id','membership_revision','product_type',
                                'processing_contract','reference_set','selection_policy','intent_id')] or
                (job['input']['connector'] == 'synthetic-drive-v1' and (
                    job['input']['expected_sha256'] != job['content_pin'] or
                    job['input']['input_bytes'] != len(job['captured_input'])))):
            return refusal('source_identity_conflict'), []
        product_type = job['input']['product_type']
        latest = connection.execute('SELECT id FROM review_approvals WHERE product_type=%s ORDER BY version DESC LIMIT 1',
                                    (product_type,)).fetchone()
        if (latest['id'] if latest else None) != request['expected_base']:
            return refusal('base_conflict'), []
        if connection.execute('SELECT 1 FROM review_drafts WHERE product_type=%s AND approval_id IS NULL LIMIT 1',
                              (product_type,)).fetchone():
            return refusal('active_draft_conflict'), []
        if connection.execute('SELECT 1 FROM job_publications WHERE job_id=%s LIMIT 1',
                              (job['id'],)).fetchone():
            return refusal('publication_conflict'), []
        publication_id = str(uuid.uuid4())
        initial.update(id=publication_id,job_id=job['id'],kind='initial',sequence=0,
            initial_publication_id=publication_id,content_pin=job['content_pin'],
            context={key:job['input'][key] for key in ('document_id','source_revision','case_id',
                'membership_revision','product_type','processing_contract','reference_set','selection_policy','intent_id')},
            context_origin='supplied',profile=job['profile'])
        if len(canonical(initial)) > 2 * 1024**2:
            return refusal('serialized_result_limit'), []
        base = prepare_publication_base(connection,initial)
        writes = [('INSERT INTO job_publications(id,job_id,attempt_id,sequence,request,payload,publication_origin) VALUES(%s,%s,NULL,0,%s,%s,%s)',
            (publication_id,job['id'],Jsonb(request),Jsonb(initial),VERSION),
            8192+len(canonical(request))+len(canonical(initial))),
            ('INSERT INTO review_publication_bases(proposal_id,job_id,base_version,reservation_bytes) VALUES(%s,%s,%s,%s)',
             (publication_id,job['id'],base['base_version'],base['reservation_bytes']),base['reservation_bytes'])]
        for kind,field in (('observation','observations'),('association','associations'),('candidate','candidates')):
            for ordinal,payload in enumerate(initial[field]):
                support = payload['support_id'] if kind == 'observation' else None
                effect = dict(stage_contract=VERSION,kind=kind,ordinal=ordinal,payload=payload,support_id=support)
                writes.append(('INSERT INTO job_effects(job_id,stage_contract,kind,ordinal,payload,support_id) VALUES(%s,%s,%s,%s,%s,%s)',
                    (job['id'],VERSION,kind,ordinal,Jsonb(payload),support),4096+len(canonical(effect))))
        writes.append(("UPDATE admission_jobs SET state='completed',reason='semantic_projection_published',held_active_reservation=false,revision=revision+1 WHERE id=%s",
                       (job['id'],),0))
        return dict(known=True,saved=True,approved=False,proposal_id=publication_id,
                    job_id=job['id'],revision=job['revision']+1), writes

    @staticmethod
    def _job_bytes(connection, job_id):
        return int(connection.execute('''SELECT
            coalesce((SELECT sum(reservation_bytes) FROM review_publication_bases WHERE job_id=%s),0) +
            coalesce((SELECT sum(reservation_bytes) FROM review_drafts WHERE job_id=%s),0) +
            coalesce((SELECT sum(r.reservation_bytes) FROM review_revisions r JOIN review_drafts d ON d.id=r.draft_id WHERE d.job_id=%s),0) +
            coalesce((SELECT sum(a.reservation_bytes) FROM review_approvals a JOIN review_drafts d ON d.id=a.draft_id WHERE d.job_id=%s),0) +
            coalesce((SELECT sum(reservation_bytes) FROM review_receipts WHERE job_id=%s),0) AS bytes''',
            (job_id, job_id, job_id, job_id, job_id)).fetchone()['bytes'])

    def _prepare_create(self, connection, request, draft):
        publication = connection.execute('SELECT payload FROM job_publications WHERE id=%s AND job_id=%s',
                                        (request['proposal_id'], request['job_id'])).fetchone()
        if not publication or publication['payload']['kind'] != 'initial':
            return refusal('invalid_proposal_set'), []
        initial = publication['payload']
        if initial['context']['product_type'] != request['product_type']:
            return refusal('product_type_conflict'), []
        if initial.get('stage_contract') == 'semantic-projection-v1':
            base = _read_base(connection,initial['id'])
            if not base['known'] or base['version'] != request['expected_base']:
                return refusal('base_conflict'), []
        latest = connection.execute('SELECT id FROM review_approvals WHERE product_type=%s ORDER BY version DESC LIMIT 1',
                                    (request['product_type'],)).fetchone()
        if (latest['id'] if latest else None) != request['expected_base']:
            return refusal('base_conflict'), []
        if connection.execute('SELECT id FROM review_drafts WHERE product_type=%s AND approval_id IS NULL LIMIT 1',
                              (request['product_type'],)).fetchone():
            return refusal('active_draft_conflict'), []
        draft_id = str(uuid.uuid4())
        definitions = [dict(id=c['id'], label=c['label'], value_type=c['value_type'], unit=c['unit'],
                            requirements='unspecified', observation_ids=c['observation_ids'],
                            decision='awaiting', conditions='unspecified', origin=dict(kind='initial_proposal',
                            proposal_id=initial['id'], candidate_id=c['id']))
                       for c in initial['candidates'] if c['domain'] == 'technical']
        payload = dict(definitions=definitions, associations=initial['associations'], actor=request['actor'],
                       reason='Create draft', revision=0)
        if not _bounded(payload):
            return refusal('review_metadata_limit'), []
        draft_bytes = 8192 + len(canonical(request))
        revision_bytes = 8192 + len(canonical(payload))
        result = dict(known=True, saved=True, approved=False, draft_id=draft_id, revision=0)
        return result, [
            ('INSERT INTO review_drafts(id,job_id,proposal_id,product_type,base_version,actor,reservation_bytes) VALUES(%s,%s,%s,%s,%s,%s,%s)',
             (draft_id,request['job_id'],request['proposal_id'],request['product_type'],request['expected_base'],request['actor'],draft_bytes), draft_bytes),
            ('INSERT INTO review_revisions(draft_id,revision,payload,reservation_bytes) VALUES(%s,0,%s,%s)',
             (draft_id,Jsonb(payload),revision_bytes), revision_bytes)]

    @staticmethod
    def _stage_record(connection, draft_id):
        return connection.execute("""
            SELECT d.revision, r.payload, p.payload->'stage_observation' AS observation
            FROM review_drafts d JOIN review_revisions r ON (r.draft_id,r.revision)=(d.id,d.revision)
            JOIN job_publications p ON p.id=d.proposal_id WHERE d.id=%s AND p.payload->>'adapter'=%s
            """, (draft_id, IMPORT_CONTRACT)).fetchone()

    @staticmethod
    def _diameter_definition(record):
        if record:
            return next((item for item in record['payload']['definitions'] if item['id'] == DIAMETER_ID), None)

    @_persistence_boundary
    def stage_read(self, draft_id):
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            row = self._stage_record(connection, draft_id)
        definition = self._diameter_definition(row)
        if definition is None:
            return refusal('not_found')
        decision = row['payload'].get('stage_decision') or dict(stages=None, decided_by=None)
        return dict(known=True, draft_id=draft_id, revision=row['revision'], **row['observation'], **matrix(row),
                    membership=definition['decision'], membership_origin=definition.get('membership_origin'), **decision)

    @_persistence_boundary
    def schema_preview(self, draft_id, revision, history_through=None):
        """Read one immutable B revision and a bounded page of its correction history."""
        through = revision if history_through is None else history_through
        if (type(revision) is not int or type(through) is not int or
                not 0 <= through <= revision <= 2**63 - 1):
            return refusal('invalid_revision')
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            row = connection.execute("""
                SELECT r.payload, p.payload->'stage_observation' AS observation,
                       p.payload->'observations' AS observations, d.proposal_id
                FROM review_drafts d JOIN review_revisions r ON r.draft_id=d.id
                JOIN job_publications p ON p.id=d.proposal_id
                WHERE d.id=%s AND r.revision=%s AND p.payload->>'adapter'=%s
                """, (draft_id, revision, IMPORT_CONTRACT)).fetchone()
            if row is None:
                return refusal('revision_not_found')
            revisions = connection.execute("""
                SELECT payload FROM review_revisions WHERE draft_id=%s AND revision<=%s
                ORDER BY revision DESC LIMIT 9""", (draft_id, through)).fetchall()
        history = []
        for index, item in enumerate(revisions[:8]):
            saved = item['payload']
            previous = revisions[index + 1]['payload'] if index + 1 < len(revisions) else None
            history.append(dict(revision=saved['revision'], actor=saved['actor'], reason=saved['reason'],
                                changes=stage_changes(previous, saved, row['observation'])))
        saved = row['payload']
        return dict(known=True, draft_id=draft_id, revision=revision, proposal_id=row['proposal_id'],
                    evidence=row['observation'], observation_ids=[o['id'] for o in row['observations']],
                    definitions=saved['definitions'], associations=saved['associations'],
                    history=history, older=revisions[8]['payload']['revision'] if len(revisions)>8 else None,
                    **matrix(row))

    @_persistence_boundary
    def stage_source(self, draft_id):
        if not self.case_source_allowed:
            return refusal('source_unavailable')
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            row = connection.execute("""
                SELECT j.captured_input, p.payload->'stage_observation'->>'source_sha256' AS pin
                FROM review_drafts d JOIN job_publications p ON p.id=d.proposal_id
                JOIN admission_jobs j ON j.id=d.job_id WHERE d.id=%s AND p.payload->>'adapter'=%s
                """, (draft_id, IMPORT_CONTRACT)).fetchone()
        if not row or row['captured_input'] is None:
            return refusal('source_unavailable')
        original = row['captured_input']
        if hashlib.sha256(original).hexdigest() != row['pin']:
            return refusal('source_unavailable')
        return dict(known=True, pdf_base64=base64.b64encode(original).decode('ascii'))

    @_persistence_boundary
    def stage_save(self, namespace, command_id, draft_id, expected_revision, stages, actor, *,
                   attribute_id=_SCENARIO_OMITTED, scenario_id=_SCENARIO_OMITTED):
        fields = dict(stages=stages)
        if attribute_id is not _SCENARIO_OMITTED:
            fields['attribute_id'] = attribute_id
        if scenario_id is not _SCENARIO_OMITTED:
            fields['scenario_id'] = scenario_id
        return self._stage_command('stage_save', namespace, command_id, draft_id, expected_revision, actor, **fields)

    @_persistence_boundary
    def stage_add(self, namespace, command_id, draft_id, expected_revision, actor, *, attribute_id=_SCENARIO_OMITTED):
        fields = {} if attribute_id is _SCENARIO_OMITTED else dict(attribute_id=attribute_id)
        return self._stage_command('stage_add', namespace, command_id, draft_id, expected_revision, actor, **fields)

    @_persistence_boundary
    def stage_bulk(self, namespace, command_id, draft_id, expected_revision, cells, actor):
        return self._stage_command('stage_bulk', namespace, command_id, draft_id, expected_revision, actor, cells=cells)

    def _stage_command(self, operation, namespace, command_id, draft_id, expected_revision, actor, **fields):
        if not all(_text(v) for v in (namespace, command_id, draft_id, actor)):
            return refusal('invalid_request')
        return self._command(namespace, command_id, dict(operation=operation, draft_id=draft_id,
                             expected_revision=expected_revision, actor=actor, **fields))

    def _prepare_stage_revision(self, connection, request, draft):
        record = self._stage_record(connection, draft['id'])
        if not record:
            return refusal('not_found'), []
        if type(request['expected_revision']) is not int:
            return refusal('invalid_stage_decision' if request['operation'] == 'stage_save' else 'invalid_request'), []
        if draft['approval_id'] or draft['revision'] != request['expected_revision']:
            return refusal('revision_conflict', current_revision=draft['revision']), []
        revision = draft['revision'] + 1
        payload = dict(record['payload'], revision=revision, actor=request['actor'])
        view = matrix(record)
        operation = request['operation']
        attribute = request.get('attribute_id', DIAMETER_ID)
        definition = next((item for item in payload['definitions'] if item['id'] == attribute), None)
        if operation == 'stage_bulk':
            if not exact_proposal(request['cells'], view['eligible_cells']):
                return refusal('preview_mismatch'), []
            for pair in view['eligible_cells']:
                record_decision(payload, pair['attribute_id'], pair['scenario_id'], [], request['actor'])
            payload['reason'] = 'Set previewed stage obligations to optional'
        elif definition is None:
            return refusal('not_found'), []
        elif operation == 'stage_add':
            if definition['decision'] != 'awaiting':
                return refusal('suggested_attribute_required'), []
            definition.update(decision='accepted', membership_origin=dict(kind='reviewer', actor=request['actor']))
            payload['reason'] = f"Add {definition['label']} to Schema"
        else:
            if definition['decision'] != 'accepted':
                return refusal('schema_membership_required'), []
            stages = request['stages']
            if (not isinstance(stages, list) or any(not isinstance(code, str) or code not in STAGES for code in stages)
                    or len(set(stages)) != len(stages)):
                return refusal('invalid_stage_decision'), []
            scenario = request.get('scenario_id', 'case001')
            if (('scenarios' in payload and 'attribute_id' not in request)
                    or not any(item['id'] == scenario for item in view['scenarios'])):
                return refusal('invalid_stage_decision'), []
            record_decision(payload, attribute, scenario, [code for code in STAGES if code in stages], request['actor'])
            payload['reason'] = 'Set procurement stage obligations'
        size = 8192 + len(canonical(payload))
        return dict(known=True, saved=True, approved=False, draft_id=draft['id'], revision=revision), [
            ('INSERT INTO review_revisions(draft_id,revision,payload,reservation_bytes) VALUES(%s,%s,%s,%s)',
             (draft['id'], revision, Jsonb(payload), size), size),
            ('UPDATE review_drafts SET revision=%s WHERE id=%s', (revision, draft['id']), 0)]

    @_persistence_boundary
    def read(self, draft_id, revision=None, cursor=None, *, receipt_namespace=None, receipt_operations=None):
        if not _text(draft_id) or not _bounded(draft_id) or (revision is not None and (type(revision) is not int or not 0 <= revision <= 9223372036854775807)):
            return refusal('invalid_request')
        owner = ['read', draft_id]
        if receipt_namespace is not None:
            owner.extend([receipt_namespace, receipt_operations])
        try:
            pin, offset = _cursor(cursor, owner)
        except ValueError:
            return refusal('invalid_cursor')
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            draft = connection.execute('SELECT * FROM review_drafts WHERE id=%s', (draft_id,)).fetchone()
            if not draft:
                return refusal('not_found')
            if cursor is not None:
                if (set(pin) != {'revision','approval_id','conflict_cutoff'} or type(pin['revision']) is not int or pin['revision'] < 0 or
                        type(pin['conflict_cutoff']) is not int or pin['conflict_cutoff'] < 0):
                    return refusal('invalid_cursor')
                if pin['approval_id'] is not None and (not isinstance(pin['approval_id'],str) or pin['approval_id'] != draft['approval_id']):
                    return refusal('invalid_cursor')
                revision = pin['revision']
            else:
                revision = draft['revision'] if revision is None else revision
                cutoff = connection.execute('''SELECT coalesce(max(sequence),0) AS cutoff FROM review_receipts
                    WHERE (%s::text IS NULL OR namespace=%s)
                    AND (%s::text[] IS NULL OR request->>'operation'=ANY(%s::text[]))''',
                    (receipt_namespace, receipt_namespace, receipt_operations, receipt_operations)).fetchone()['cutoff']
                pin = dict(revision=revision,approval_id=draft['approval_id'],conflict_cutoff=cutoff)
            saved = connection.execute('SELECT payload FROM review_revisions WHERE draft_id=%s AND revision=%s',
                                       (draft_id,revision)).fetchone()
            if not saved:
                return refusal('revision_not_found')
            initial = connection.execute('SELECT payload FROM job_publications WHERE id=%s',
                                         (draft['proposal_id'],)).fetchone()['payload']
            approval = connection.execute('SELECT payload FROM review_approvals WHERE id=%s',
                                          (pin['approval_id'],)).fetchone()
            conflicts = connection.execute("""SELECT namespace,command_id,result FROM review_receipts
                WHERE job_id=%s AND request->>'draft_id'=%s AND sequence<=%s
                AND (%s::text IS NULL OR namespace=%s)
                AND (%s::text[] IS NULL OR request->>'operation'=ANY(%s::text[]))
                AND result->>'category' IN ('revision_conflict','base_conflict')
                ORDER BY sequence DESC LIMIT 17""",(draft['job_id'],draft_id,pin['conflict_cutoff'],
                                                receipt_namespace,receipt_namespace,receipt_operations,receipt_operations)).fetchall()
            base = _read_base(connection,draft['proposal_id'])
            evidence = (approval_evidence(initial, saved['payload']) if receipt_operations == ['approve']
                        else publication_view(initial))
            result = dict(available_scenario_registry=available_registry(),pending_conflicts=conflicts[:16],more_conflicts=len(conflicts)>16,initial_base_known=base['known'],initial_base_version=base['version'],known=True,saved=True,approved_schema=approval['payload'] if approval else None,draft_id=draft_id,product_type=draft['product_type'],
                        base_version=draft['base_version'],approved_version=pin['approval_id'],
                        initial=evidence,**saved['payload'])
            return _page(result,owner,pin,offset,cursor)

    def resume(self, draft_id, cursor=None, *, receipt_namespace=None, receipt_operations=None):
        return self.read(draft_id, cursor=cursor, receipt_namespace=receipt_namespace, receipt_operations=receipt_operations)

    @_persistence_boundary
    def save(self, namespace, command_id, draft_id, expected_revision, definitions,
             associations, actor, reason, scenario_review=_SCENARIO_OMITTED,
             equipment_review=_EQUIPMENT_OMITTED):
        if (not all(_text(v) for v in (namespace,command_id,draft_id,actor,reason)) or
                type(expected_revision) is not int or not 0 <= expected_revision <= 9223372036854775807 or
                not isinstance(definitions,list) or not isinstance(associations,list)):
            return refusal('invalid_request')
        request = dict(operation='save',draft_id=draft_id,
            expected_revision=expected_revision,definitions=definitions,associations=associations,
            actor=actor,reason=reason)
        if scenario_review is not _SCENARIO_OMITTED:
            request['scenario_review'] = scenario_review
        if equipment_review is not _EQUIPMENT_OMITTED:
            request['equipment_review'] = equipment_review
        return self._command(namespace,command_id,request)

    def _prepare_save(self, connection, request, draft):
        if draft['approval_id'] or draft['revision'] != request['expected_revision']:
            return refusal('revision_conflict',current_revision=draft['revision']), []
        previous = connection.execute('SELECT payload FROM review_revisions WHERE draft_id=%s AND revision=%s',
                                      (draft['id'],draft['revision'])).fetchone()['payload']
        definitions = json.loads(canonical(request['definitions']))
        prior = {d['id']: d for d in previous['definitions']}
        if any(not isinstance(d,dict) or not _text(d.get('id')) for d in definitions):
            return refusal('invalid_request'), []
        initial = connection.execute('SELECT payload FROM job_publications WHERE id=%s',
                                     (draft['proposal_id'],)).fetchone()['payload']
        errors = _manual_errors(definitions, request['associations'], previous, initial)
        if not errors:
            errors = _manual_reused_ids(connection, draft['id'], definitions)
        if errors:
            return refusal('validation_error',errors=errors), []
        initial_ids = {candidate['id'] for candidate in initial['candidates']}
        same_definitions = _json_equalities(connection,
            [[_without_origin(d),_without_origin(prior.get(d['id']))] for d in definitions])
        for definition, same in zip(definitions,same_definitions):
            original = prior.get(definition['id'])
            definition['origin'] = (original['origin'] if same else
                dict(kind='human',actor=request['actor'],reason=request['reason'],
                     source_is_proof_of_edit=False,initial_candidate_id=definition['id'] if definition['id'] in initial_ids else None))
        revision = draft['revision'] + 1
        associations = json.loads(canonical(request['associations']))
        prior_associations = {a['id']:a for a in previous['associations'] if isinstance(a,dict) and isinstance(a.get('id'),str)}
        records = [a for a in associations if isinstance(a,dict)]
        originals = [prior_associations.get(a.get('id')) if isinstance(a.get('id'),str) else None for a in records]
        same_associations = _json_equalities(connection,
            [[_without_origin(a),_without_origin(original)] for a,original in zip(records,originals)])
        for association,original,same in zip(records,originals,same_associations):
            if not same:
                association['origin'] = dict(kind='human',actor=request['actor'],reason=request['reason'],source_is_proof_of_edit=False)
            elif original and 'origin' in original:
                association['origin'] = original['origin']
            else:
                association.pop('origin',None)
        payload = dict(definitions=definitions,associations=associations,actor=request['actor'],
                       reason=request['reason'],revision=revision)
        if 'scenario_review' in request or 'scenario_review' in previous:
            scenario = request.get('scenario_review', previous.get('scenario_review'))
            errors = scenario_errors(scenario, definitions)
            if errors:
                return refusal('validation_error',errors=errors), []
            payload['scenario_review'] = scenario
        if 'equipment_review' in request or 'equipment_review' in previous:
            equipment = request.get('equipment_review', previous.get('equipment_review'))
            errors = equipment_errors(equipment)
            if errors:
                return refusal('validation_error',errors=errors), []
            payload['equipment_review'] = equipment_with_origin(
                equipment, previous.get('equipment_review'), request['actor'], request['reason'])
        if not _bounded(payload):
            return refusal('review_metadata_limit'), []
        size = 8192 + len(canonical(payload))
        return dict(known=True,saved=True,approved=False,draft_id=draft['id'],revision=revision), [
            ('INSERT INTO review_revisions(draft_id,revision,payload,reservation_bytes) VALUES(%s,%s,%s,%s)',
             (draft['id'],revision,Jsonb(payload),size),size),
            ('UPDATE review_drafts SET revision=%s WHERE id=%s',(revision,draft['id']),0)]

    @_persistence_boundary
    def resolve(self, namespace, command_id, *, expected_request=None):
        if not all(_text(v) for v in (namespace,command_id)) or not _bounded([namespace,command_id]):
            return refusal('invalid_request')
        with psycopg.connect(**self.database,row_factory=dict_row) as connection:
            prior = _receipt(connection, namespace, command_id, expected_request)
            if prior is not None:
                return prior
            return dict(known=False,saved=False,category='unknown_command',next_action='retry_same_key')

    @_persistence_boundary
    def approve(self, namespace, command_id, draft_id, expected_revision, product_type, actor, explicit):
        if (not all(_text(v) for v in (namespace,command_id,draft_id,product_type,actor)) or
                type(expected_revision) is not int or not 0 <= expected_revision <= 9223372036854775807 or explicit is not True):
            return refusal('explicit_whole_schema_approval_required')
        return self._command(namespace,command_id,dict(operation='approve',draft_id=draft_id,
            expected_revision=expected_revision,product_type=product_type,actor=actor,explicit=explicit))

    def _prepare_approve(self, connection, request, draft):
        if draft['revision'] != request['expected_revision']:
            return refusal('revision_conflict',current_revision=draft['revision']), []
        if draft['product_type'] != request['product_type']:
            return refusal('product_type_conflict'), []
        if draft['approval_id']:
            approved = connection.execute('SELECT payload FROM review_approvals WHERE id=%s',
                                          (draft['approval_id'],)).fetchone()['payload']
            return dict(known=True,saved=True,approved=True,draft_id=draft['id'],revision=approved['revision'],
                        approved_version=approved['id'],version=approved['version']), []
        latest = connection.execute('SELECT id,version FROM review_approvals WHERE product_type=%s ORDER BY version DESC LIMIT 1',
                                    (draft['product_type'],)).fetchone()
        if (latest['id'] if latest else None) != draft['base_version']:
            return refusal('base_conflict'), []
        saved = connection.execute('SELECT payload FROM review_revisions WHERE draft_id=%s AND revision=%s',
                                   (draft['id'],draft['revision'])).fetchone()['payload']
        initial = connection.execute('SELECT payload FROM job_publications WHERE id=%s',
                                     (draft['proposal_id'],)).fetchone()['payload']
        errors = _manual_errors(saved['definitions'],saved['associations'],saved,initial)
        if not errors:
            errors = _manual_reused_ids(connection,draft['id'],saved['definitions'])
        if not errors:
            errors = validation_errors(saved,initial)
        if errors:
            return refusal('validation_error',errors=errors), []
        approval_id = str(uuid.uuid4())
        version = latest['version'] + 1 if latest else 1
        schema = dict(id=approval_id,version=version,draft_id=draft['id'],revision=draft['revision'],
                      product_type=draft['product_type'],base_version=draft['base_version'],
                      definitions=[d for d in saved['definitions'] if d['decision'] == 'accepted'],
                      associations=[a for a in saved['associations'] if a['disposition'] in ('supported','accepted')],actor=request['actor'],explicit=True)
        if 'scenario_review' in saved:
            schema['scenario_review'] = saved['scenario_review']
        if 'equipment_review' in saved:
            schema['equipment_review'] = saved['equipment_review']
        size = 8192 + len(canonical(schema))
        return dict(known=True,saved=True,approved=True,draft_id=draft['id'],revision=draft['revision'],
                    approved_version=approval_id,version=version), [
            ('INSERT INTO review_approvals(id,draft_id,product_type,version,payload,reservation_bytes) VALUES(%s,%s,%s,%s,%s,%s)',
             (approval_id,draft['id'],draft['product_type'],version,Jsonb(schema),size),size),
            ('UPDATE review_drafts SET approval_id=%s WHERE id=%s',(approval_id,draft['id']),0)]

    @_persistence_boundary
    def read_incoming(self, draft_id, job_id, proposal_id, cursor=None):
        if not all(_text(v) for v in (draft_id,job_id,proposal_id)) or not _bounded([draft_id,job_id,proposal_id]):
            return refusal('invalid_request')
        owner = ['incoming',draft_id,job_id,proposal_id]
        try:
            pin, offset = _cursor(cursor,owner)
            if pin:
                raise ValueError
        except ValueError:
            return refusal('invalid_cursor')
        with psycopg.connect(**self.database,row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            draft = connection.execute('SELECT * FROM review_drafts WHERE id=%s',(draft_id,)).fetchone()
            incoming = connection.execute('SELECT payload FROM job_publications WHERE id=%s AND job_id=%s',
                                          (proposal_id,job_id)).fetchone()
            if not draft or not incoming:
                return refusal('invalid_proposal_set')
            incoming = incoming['payload']
            if incoming['context']['product_type'] != draft['product_type']:
                return refusal('product_type_conflict')
            initial = connection.execute('SELECT payload FROM job_publications WHERE id=%s',
                                         (draft['proposal_id'],)).fetchone()['payload']
            # Supplements contain only newly committed effects. Compare against
            # their immutable accumulated context, pinned to this publication.
            context = connection.execute(
                'SELECT payload FROM job_publications WHERE job_id=%s AND sequence<=%s ORDER BY sequence',
                (job_id,incoming['sequence'])).fetchall() if incoming['kind'] == 'supplement' else [dict(payload=incoming)]
            differences = {}
            for field in ('observations','associations','candidates'):
                before = {item['id']:item for item in initial[field]}
                after = {item['id']:item for publication in context for item in publication['payload'][field]}
                identities = sorted(before.keys() | after.keys())
                same_records = _json_equalities(connection,
                    [[before.get(key),after.get(key)] for key in identities])
                differences[field] = [dict(id=key,before=before.get(key),after=after.get(key))
                                      for key,same in zip(identities,same_records) if not same]
            base = _read_base(connection,proposal_id)
            result = dict(known=True,read_only=True,draft_id=draft_id,base_version=base['version'],
                        incoming_base_known=base['known'],draft_base_version=draft['base_version'],
                        draft_proposal_id=draft['proposal_id'],incoming_initial_proposal_id=incoming['initial_publication_id'],
                        incoming=publication_view(incoming),differences=differences,next_action='read_compare_only')
            return _page(result,owner,{},offset,cursor)


def retained_bytes(connection, job_id):
    """Review-owned retained copies, charged once through the Job reservation."""
    return Review._job_bytes(connection,job_id)


def validation_errors(saved, initial):
    """Report bounded field paths; never repair or partially approve a draft."""
    errors = []

    def error(field, reason):
        if len(errors) < 64:
            errors.append(dict(field=field,reason=reason))

    definitions = saved['definitions']
    identities = [d.get('id') for d in definitions]
    included = {d['id'] for d in definitions if d.get('decision') == 'accepted'}
    observations = {o['id'] for o in initial['observations']}
    for candidate in initial['candidates']:
        if candidate['domain'] == 'technical' and candidate['id'] not in identities:
            error('definitions.' + candidate['id'] + '.decision','Explicitly accept, defer or reject the original candidate')
    for index, definition in enumerate(definitions):
        path = f'definitions[{index}]'
        if identities.count(definition.get('id')) != 1:
            error(path + '.id','Attribute identity must be unique')
        if definition.get('decision') not in ('accepted','deferred','rejected'):
            error(path + '.decision','Explicit review decision required')
        if definition.get('decision') != 'accepted':
            continue
        if not _text(definition.get('label')):
            error(path + '.label','Nonempty display label required')
        if definition.get('value_type') not in ('decimal','text'):
            error(path + '.value_type','Declare decimal or text type')
        unit = definition.get('unit')
        if ((definition.get('value_type') == 'decimal' and not _text(unit)) or
                (definition.get('value_type') == 'text' and (unit is not None or 'unit' not in definition))):
            error(path + '.unit','Decimal requires a unit token; text requires explicit absent unit')
        if definition.get('requirements') != 'unspecified':
            error(path + '.requirements','Fixture requirements remain explicitly unspecified')
        allowed = {'id','label','value_type','unit','requirements','observation_ids','decision','conditions','origin','definition_kind'}
        if set(definition) - allowed - {'default','value'}:
            error(path,'Unsupported definition fields; explicitly declared definition contract required')
        if 'default' in definition or 'value' in definition:
            error(path + '.default','Observed equipment values are not definition defaults')
        links = definition.get('observation_ids')
        origin = definition.get('origin')
        manual = (definition.get('definition_kind') == 'human_manual_v1' and
                  isinstance(origin,dict) and origin.get('kind') == 'human' and
                  origin.get('source_is_proof_of_edit') is False and
                  origin.get('initial_candidate_id') is None and
                  definition['id'] not in {candidate['id'] for candidate in initial['candidates']})
        if not (manual and links == []) and (not isinstance(links,list) or not links or any(not isinstance(o,str) or o not in observations for o in links)):
            error(path + '.observation_ids','Use retained source observations; edited meaning has human origin')
        conditions = definition.get('conditions')
        if conditions == 'unspecified':
            pass
        elif not isinstance(conditions,list):
            error(path + '.conditions','Declare conditions or explicitly leave unspecified')
        else:
            for ci,condition in enumerate(conditions):
                operands = condition.get('operand_ids') if isinstance(condition,dict) else None
                if (not isinstance(operands,list) or not operands or
                        any(not isinstance(o,str) or o not in included | observations for o in operands)):
                    error(path + f'.conditions[{ci}].operand_ids','Condition operands must reference included Attributes or source observations')
    associations = saved['associations']
    association_ids = [a.get('id') for a in associations if isinstance(a,dict)]
    for original in initial['associations']:
        if original['id'] not in association_ids:
            error('associations.' + original['id'] + '.disposition','Explicitly resolve, defer or reject the original association')
    for index, association in enumerate(associations):
        path = f'associations[{index}]'
        if not isinstance(association,dict):
            error(path,'Association record required')
            continue
        if not _text(association.get('id')) or association_ids.count(association.get('id')) != 1:
            error(path + '.id','Unique association identity required')
        disposition = association.get('disposition')
        if disposition not in ('supported','accepted','deferred','rejected'):
            error(path + '.disposition','Resolve, defer or reject included association')
        if disposition in ('deferred','rejected'):
            continue
        links = association.get('observation_ids')
        if not isinstance(links,list) or not links or any(not isinstance(o,str) or o not in observations for o in links):
            error(path + '.observation_ids','Association operands must reference retained observations')
        targets = association.get('candidate_ids',[])
        if not isinstance(targets,list) or any(not isinstance(o,str) or o not in included for o in targets):
            error(path + '.candidate_ids','Association target must be an included Attribute')
    if 'scenario_review' in saved:
        errors.extend(scenario_errors(saved['scenario_review'],definitions)[:max(0,64-len(errors))])
    if 'equipment_review' in saved:
        errors.extend(equipment_errors(saved['equipment_review'])[:max(0,64-len(errors))])
    return errors


def _cursor(cursor, owner):
    if cursor is None:
        return {},0
    try:
        if not isinstance(cursor,str) or len(cursor) > 1024:
            raise ValueError
        identity,pin,offset = json.loads(base64.b64decode(cursor,validate=True))
        if identity != owner or not isinstance(pin,dict) or type(offset) is not int or offset < 0 or offset % 24000:
            raise ValueError
        return pin,offset
    except (ValueError,TypeError,RecursionError):
        raise ValueError('invalid cursor') from None


def _page(result, owner, pin, offset, cursor):
    encoded = canonical(result)
    if cursor is None and len(json.dumps(result).encode()) <= 120000:
        return result
    if offset >= len(encoded):
        return refusal('invalid_cursor')
    end = min(offset + 24000,len(encoded))
    next_cursor = base64.b64encode(canonical([owner,pin,end]).encode()).decode() if end < len(encoded) else None
    return dict(known=True,encoding='json-ascii',offset=offset,fragment=encoded[offset:end],next_cursor=next_cursor)


def prepare_publication_base(connection, publication):
    """Called inside publication's admission lock, before its retained-byte fence."""
    latest = connection.execute('SELECT id FROM review_approvals WHERE product_type=%s ORDER BY version DESC LIMIT 1',
                                (publication['context']['product_type'],)).fetchone()
    record = dict(proposal_id=publication['id'],job_id=publication['job_id'],
                  base_version=latest['id'] if latest else None)
    record['reservation_bytes'] = 4096 + len(canonical(record))
    return record


def record_publication_base(connection, record):
    connection.execute('INSERT INTO review_publication_bases(proposal_id,job_id,base_version,reservation_bytes) VALUES(%s,%s,%s,%s)',
                       (record['proposal_id'],record['job_id'],record['base_version'],record['reservation_bytes']))


def _read_base(connection, proposal_id):
    record = connection.execute('SELECT base_version FROM review_publication_bases WHERE proposal_id=%s',
                                (proposal_id,)).fetchone()
    # Absence is legacy/unknown. Never invent a historical base during migration.
    return dict(known=record is not None,version=record['base_version'] if record else None)
