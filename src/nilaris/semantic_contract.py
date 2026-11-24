"""Strict public artifact contract for the isolated semantic experiment."""
import json

VERSION = 'semantic-experiment-v1'
MODEL = 'gpt-5.4-2026-03-05'
PRESENCE = ['observed', 'blank', 'missing_marker', 'unread', 'ambiguous']
DOMAINS = ['technical', 'logistical', 'commercial', 'other', 'unresolved']


def obj(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def string(values=None):
    return dict(type='string', **({'enum': values} if values else {}))


def array(items):
    return dict(type='array', items=items)


NULLABLE = dict(type=['string', 'null'])
REF = obj({key: string() for key in ('document_id', 'source_revision', 'locator_id')})
SCHEMA = obj(dict(version=string([VERSION]), observations=array(obj(dict(
    id=string(), document_id=string(), source_revision=string(), locator_id=string(),
    label=string(), original_value=NULLABLE, original_unit=NULLABLE,
    presence=string(PRESENCE), domain=string(DOMAINS)))), associations=array(obj(dict(
    id=string(), source_refs=array(REF), target_ref=REF, relation=string(),
    disposition=string(['supported', 'ambiguous', 'unresolved'])))), candidates=array(obj(dict(
    id=string(), label=string(), value_type=string(['decimal', 'text', 'boolean', 'range', 'unresolved']),
    unit=NULLABLE, domain=string(['technical']), evidence_refs=array(REF), uncertainty=string())))))


def _check(value, schema):
    kind = schema['type']
    if isinstance(kind, list):
        if value is None and 'null' in kind:
            return
        kind = 'string'
    if kind == 'object':
        if type(value) is not dict or set(value) != set(schema['properties']):
            raise ValueError('schema object mismatch')
        for key, child in schema['properties'].items():
            _check(value[key], child)
    elif kind == 'array':
        if type(value) is not list or len(value) > 200:
            raise ValueError('schema array mismatch')
        for child in value:
            _check(child, schema['items'])
    elif kind == 'string':
        if type(value) is not str or len(value) > 8192:
            raise ValueError('schema string mismatch')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('schema enum mismatch')


def validate(output, capture, target_document_id):
    """Reject every malformed record or forged identity; never repair answers."""
    _check(output, SCHEMA)
    available = {(d['document_id'], d['source_revision'], s['locator_id'])
                 for d in capture['documents'] for s in d['spans']}
    def reference(ref):
        if tuple(ref[k] for k in ('document_id', 'source_revision', 'locator_id')) not in available:
            raise ValueError('unknown source reference')
    for group in ('observations', 'associations', 'candidates'):
        ids = [r['id'] for r in output[group]]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate record id')
    for row in output['observations']:
        if row['document_id'] != target_document_id:
            raise ValueError('observation target identity')
        reference(row)
    for row in output['associations']:
        if not row['source_refs']:
            raise ValueError('missing source reference')
        for ref in [*row['source_refs'], row['target_ref']]:
            reference(ref)
    for row in output['candidates']:
        if not row['evidence_refs']:
            raise ValueError('missing candidate evidence')
        for ref in row['evidence_refs']:
            reference(ref)
    return output


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
