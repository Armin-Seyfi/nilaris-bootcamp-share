"""Explicit local projection of immutable synthetic semantic artifacts.

This module does not run extraction or import the semantic experiment runtime.
Structural publication validity makes no claim about semantic correctness.
"""
import hashlib
import json

from nilaris.results import canonical
from nilaris.semantic_publication_contract import validate

VERSION = 'semantic-projection-v1'
MAX_BUNDLE_BYTES = 256 * 1024
REF_KEYS = ('document_id', 'source_revision', 'locator_id')


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _ref(value):
    return tuple(value[key] for key in REF_KEYS)


def build_projection(artifact, capture, configuration, expected_pins):
    """Return a detached, bounded projection; refuse forged/inconclusive inputs.

    Pins hash canonical JSON, not the encoding of a file. Callers retain exact
    artifact files separately; the complete supplied artifact is retained here.
    """
    bundle = dict(artifact=artifact, capture=capture, configuration=configuration)
    if len(canonical(bundle)) > MAX_BUNDLE_BYTES:
        raise ValueError('semantic_bundle_limit')
    if expected_pins != {key: digest(value) for key, value in bundle.items()}:
        raise ValueError('semantic_pin_conflict')
    bundle = json.loads(canonical(bundle))
    artifact, capture, configuration = (bundle[key] for key in ('artifact','capture','configuration'))
    if not isinstance(configuration,dict) or configuration.get('source_kind') != 'synthetic':
        raise ValueError('synthetic_only')
    if not isinstance(artifact,dict) or artifact.get('status') != 'completed':
        raise ValueError('inconclusive_semantic_result')
    output = artifact['initial_output']
    target = artifact['target_document_id']
    validate(output, capture, target)
    if output['version'] != 'semantic-experiment-v1':
        raise ValueError('unsupported_semantic_contract')
    documents = capture['documents']
    targets = [d for d in documents if d['document_id'] == target]
    if not targets or len({(d['source_revision'],d['content_pin']) for d in targets}) != 1:
        raise ValueError('target_identity_conflict')
    source = targets[0]
    spans, source_documents, document_pins = {}, {}, {}
    for document in documents:
        coverage = document['coverage']
        if (not isinstance(coverage,dict) or not isinstance(coverage.get('inspected'),list) or
                not isinstance(coverage.get('unread'),list) or coverage['unread']):
            raise ValueError('inconclusive_source_coverage')
        pin = document['content_pin']
        if not isinstance(pin,str) or len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
            raise ValueError('invalid_source_pin')
        identity = (document['document_id'],document['source_revision'])
        if identity in document_pins and document_pins[identity] != pin:
            raise ValueError('source_identity_conflict')
        document_pins[identity] = pin
        for span in document['spans']:
            key = (document['document_id'],document['source_revision'],span['locator_id'])
            if key in spans and spans[key] != span:
                raise ValueError('source_identity_conflict')
            spans[key] = span
            source_documents[key] = document
    observations, candidates, associations, mapping, readonly = [], [], [], [], []
    support_groups = {}
    for row in output['observations']:
        ref = _ref(row)
        document = source_documents[ref]
        support_id = digest([document['content_pin'],spans[ref]['locator']])
        group = support_groups.setdefault(support_id,dict(id=support_id,
            content_pin=document['content_pin'],locator=spans[ref]['locator'],
            observation_ids=[],source_refs=[]))
        group['observation_ids'].append(row['id'])
        source_ref = {k:row[k] for k in REF_KEYS}
        if source_ref not in group['source_refs']:
            group['source_refs'].append(source_ref)
        if _ref(row) not in spans:
            raise ValueError('invalid_observation_reference')
        if row['document_id'] != target:
            readonly.append(dict(kind='observation',record=row,reason='external_context_evidence'))
            mapping.append(dict(kind='observation',id=row['id'],disposition='read_only',
                                reason='external_context_evidence',projected_ids=[]))
            continue
        observations.append(dict(id=row['id'], locator=spans[_ref(row)]['locator'],
            kind=row['label'], original_content=spans[_ref(row)]['text'],
            value=row['original_value'], unit=row['original_unit'], source_type='semantic',
            presence=row['presence'], domain=row['domain'], source_ref={k:row[k] for k in REF_KEYS},
            content_pin=source['content_pin'],support_id=support_id))
        mapping.append(dict(kind='observation',id=row['id'],disposition='represented',reason=None,projected_ids=[row['id']]))
    by_ref = {}
    for row in output['observations']:
        by_ref.setdefault(_ref(row), []).append(row)
    for row in output['candidates']:
        refs = row['evidence_refs']
        if not refs or any(_ref(ref) not in spans for ref in refs):
            raise ValueError('invalid_candidate_reference')
        reason = ('unsupported_value_type' if row['value_type'] not in ('decimal','text') else
                  'unsupported_unit' if ((row['value_type'] == 'decimal' and
                      (row['unit'] is None or not row['unit'].strip())) or
                      (row['value_type'] == 'text' and row['unit'] is not None)) else
                  'unsupported_domain' if row['domain'] != 'technical' else
                  'external_context_evidence' if any(ref['document_id'] != target for ref in refs) else
                  'missing_observation_mapping' if any(_ref(ref) not in by_ref for ref in refs) else
                  'ambiguous_observation_mapping' if any(len(by_ref[_ref(ref)]) != 1 for ref in refs) else
                  'unsupported_evidence' if any(by_ref[_ref(ref)][0]['domain'] != 'technical' or
                       by_ref[_ref(ref)][0]['presence'] != 'observed' for ref in refs) else None)
        ids = [by_ref[_ref(ref)][0]['id'] for ref in refs] if reason is None else []
        if reason is None:
            candidates.append(dict(id=row['id'],label=row['label'],value_type=row['value_type'],
                unit=row['unit'],domain=row['domain'],observation_ids=list(dict.fromkeys(ids)),
                requirements='unspecified'))
        else:
            readonly.append(dict(kind='candidate',record=row,reason=reason))
        mapping.append(dict(kind='candidate',id=row['id'],disposition='represented' if reason is None else 'read_only',
                            reason=reason,projected_ids=[row['id']] if reason is None else []))
    for row in output['associations']:
        refs = [*row['source_refs'], row['target_ref']]
        if not row['source_refs'] or any(_ref(ref) not in spans for ref in refs):
            raise ValueError('invalid_association_reference')
        reason = ('cross_document_relation' if any(ref['document_id'] != target for ref in refs) else
                  'missing_observation_mapping' if any(_ref(ref) not in by_ref for ref in refs) else
                  'ambiguous_observation_mapping' if any(len(by_ref[_ref(ref)]) != 1 for ref in refs) else None)
        if reason is None:
            associations.append(dict(id=row['id'],observation_ids=list(dict.fromkeys(by_ref[_ref(ref)][0]['id'] for ref in refs)),
                method='semantic',rationale=row['relation'],disposition=row['disposition'],
                source_refs=row['source_refs'],target_ref=row['target_ref']))
        else:
            readonly.append(dict(kind='association',record=row,reason=reason))
        mapping.append(dict(kind='association',id=row['id'],disposition='represented' if reason is None else 'read_only',
                            reason=reason,projected_ids=[row['id']] if reason is None else []))
    return dict(contract='proposal-set-v1',stage_contract=VERSION,adapter=VERSION,
        approved=False,outcome='completed',coverage=source['coverage'],
        observations=observations,candidates=candidates,associations=associations,
        semantic_context=dict(mapping_version=VERSION,origin=expected_pins,target={
            key:source[key] for key in ('document_id','source_revision','content_pin')},
            documents=documents,original_status=artifact['status'],original_output=output,
            original_artifact=artifact,configuration=configuration,
            support_groups=list(support_groups.values()),
            record_mapping=mapping,read_only_records=readonly))


def publication_view(publication):
    """Detached ordinary read; exact retained artifact is explicitly on demand."""
    visible = json.loads(canonical(publication))
    context = visible.get('semantic_context')
    if visible.get('stage_contract') == VERSION and context is not None:
        context.pop('original_artifact', None)
        context['provenance'] = dict(available=True,operation='result',
            job_id=visible['job_id'],proposal_id=visible['id'],provenance=True,
            artifact_pin=context['origin']['artifact'])
    return visible


def provenance_page(publication, cursor=None):
    """Read exact immutable artifact with a cursor scoped to this view and pin."""
    import base64
    context = publication['semantic_context']
    owner = ['semantic-provenance',publication['job_id'],publication['id'],context['origin']['artifact']]
    result = dict(known=True,job_id=publication['job_id'],proposal_id=publication['id'],
                  artifact_pin=context['origin']['artifact'],artifact=context['original_artifact'])
    encoded = canonical(result)
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor,str) or len(cursor) > 1024:
                raise ValueError
            identity,offset = json.loads(base64.b64decode(cursor,validate=True))
            if identity != owner or type(offset) is not int or offset < 0 or offset % 24000 or offset >= len(encoded):
                raise ValueError
        except (ValueError,TypeError):
            return dict(known=True,job_id=publication['job_id'],category='invalid_cursor')
    end = min(offset+24000,len(encoded))
    next_cursor = base64.b64encode(canonical([owner,end]).encode()).decode() if end < len(encoded) else None
    return dict(known=True,job_id=publication['job_id'],proposal_id=publication['id'],
                encoding='json-ascii',offset=offset,fragment=encoded[offset:end],next_cursor=next_cursor)
