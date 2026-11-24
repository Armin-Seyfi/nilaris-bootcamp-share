"""Select one complete, explicitly bounded requirement from an ordered RFQ."""
import json
import re

from nilaris.block_rfq_minimization import OBSERVATION_ID

PUBLICATION_ADAPTER = 'rfq-text-blocks-v2'
CONTRACT = 'rfq-block-minimization-v2'
DETECTOR = 'explicit-quantity-rules-v1'
MEANING = 'technical_meaning_at_risk'
SENSITIVITY = 'sensitivity_unresolved'

_UNITS = {'belt tension': 'N', 'air velocity': 'm/s', 'surface temperature': '°C'}
_QUANTITY = '|'.join(map(re.escape, _UNITS))
_TASK = re.compile(
    rf'(?:Report the (?P<report>{_QUANTITY}) requirement\.|'
    rf'What is the required (?P<ask>{_QUANTITY})\?)', re.I | re.ASCII)
_REQUIREMENT = re.compile(
    rf'(?P<quantity>(?i:{_QUANTITY})) (?:shall|must) be '
    r'(?:at least|at most|exactly) [+-]?[0-9]{1,4}(?:\.[0-9]{1,3})? (?P<unit>N|m/s|°C)'
    r'(?: (?:during (?:startup|steady operation)|with the (?:guard closed|fan running)))?\.',
    re.ASCII)
_REFERENCE = re.compile(r'Buyer reference: [A-Z]{2,8}-[0-9]{3,8}\.')
_PRIVATE = re.compile(r'\b[A-Z]{2,8}-[0-9]{3,8}\b|@|\b(?:buyer|contact|quote|account)\b', re.I | re.ASCII)
_BREAK = re.compile(r'(?<=\.)[ \t\r\n]+')


def _sentences(text):
    start = 0
    for boundary in _BREAK.finditer(text):
        yield start, text[start:boundary.start()]
        start = boundary.end()
    yield start, text[start:]


def transform(excerpt, *, complete):
    if not complete:
        return None, MEANING
    if not isinstance(excerpt, str) or len(excerpt) > 8192:
        return None, SENSITIVITY
    try:
        record = json.loads(excerpt)
    except (ValueError, RecursionError):
        return None, SENSITIVITY
    if (not isinstance(record, dict) or set(record) != {'task', 'filename', 'blocks'}
            or not isinstance(record['filename'], str) or not record['filename'].strip()
            or not isinstance(record['blocks'], list) or not 1 <= len(record['blocks']) <= 16):
        return None, SENSITIVITY
    task = record['task']
    match = _TASK.fullmatch(task) if isinstance(task, str) else None
    if not match:
        return None, MEANING
    wanted = (match['report'] or match['ask']).lower()
    identifiers = set()
    selected = None
    for block in record['blocks']:
        if (not isinstance(block, dict) or set(block) != {'id', 'text'}
                or not isinstance(block['id'], str) or not block['id'].strip()
                or block['id'] in identifiers or not isinstance(block['text'], str)):
            return None, SENSITIVITY
        identifiers.add(block['id'])
        for _, sentence in _sentences(block['text']):
            if _REFERENCE.fullmatch(sentence):
                continue
            if _PRIVATE.search(sentence):
                return None, SENSITIVITY
            requirement = _REQUIREMENT.fullmatch(sentence)
            if not requirement or requirement['unit'] != _UNITS[requirement['quantity'].lower()]:
                return None, MEANING
            if requirement['quantity'].lower() == wanted:
                if selected is not None:
                    return None, MEANING
                selected = sentence
    return (selected, None) if selected else (None, MEANING)


def selected_blocks(excerpt, preview):
    return [dict(id=block['id'], start=start, end=start + len(sentence))
            for block in json.loads(excerpt)['blocks']
            for start, sentence in _sentences(block['text']) if sentence == preview]
