"""Interpret a single quantity and condition before selecting its private evidence."""
import json
import re

from nilaris.block_rfq_minimization import OBSERVATION_ID
from nilaris.quantity_rfq_minimization import (
    MEANING, SENSITIVITY, _PRIVATE, _QUANTITY, _REFERENCE, _REQUIREMENT, _UNITS, _sentences,
)

PUBLICATION_ADAPTER = 'rfq-text-blocks-v3'
CONTRACT = 'rfq-block-minimization-v3'
DETECTOR = 'qualified-quantity-rules-v1'

_CONDITIONS = {
    'during startup': ('during startup', 'at startup', 'during start-up'),
    'during steady operation': ('during steady operation', 'in steady operation'),
    'with the guard closed': ('with the guard closed', 'when the guard is closed'),
    'with the fan running': ('with the fan running', 'when the fan is running'),
}
_REQUEST = re.compile(
    rf'(?:(?:please )?(?:provide|report|specify|state|give us|confirm)(?: the)?|'
    rf'what (?:is|should)(?: the)?|how much) (?:required )?(?P<quantity>{_QUANTITY})'
    r'(?: requirement| limit)?(?: is required| is needed| be| do we need)?'
)
_SOURCE_QUANTITY = re.compile(rf'(?P<quantity>{_QUANTITY})\b', re.I | re.ASCII)


_PRIVATE_LIMIT = re.compile(
    r' (?:shall|must) be (?:at least|at most|exactly) [+-]?[0-9]{1,4}'
    r'(?:\.[0-9]{1,3})? (?P<unit>N|m/s|°C)(?=\W|$)'
)


def interpret(task):
    if not isinstance(task, str):
        return None
    words = ' '.join(task.lower().split()).rstrip('.?')
    condition = None
    for canonical, alternatives in _CONDITIONS.items():
        for phrase in alternatives:
            if words.endswith(' ' + phrase):
                words = words[:-len(phrase)].rstrip()
            elif words.startswith(phrase + ', '):
                words = words[len(phrase) + 2:]
            else:
                continue
            if condition is not None:
                return None
            condition = canonical
    match = _REQUEST.fullmatch(words)
    if match:
        return dict(quantity=match['quantity'], condition=condition)
    return None


def _read_request(excerpt, *, complete):
    trace = dict(stage='interpretation', understood=False)
    if not complete or not isinstance(excerpt, str) or len(excerpt) > 8192:
        return None, trace
    try:
        record = json.loads(excerpt)
    except (ValueError, RecursionError):
        return None, trace
    if (not isinstance(record, dict) or set(record) != {'task', 'filename', 'blocks'}
            or not isinstance(record['filename'], str) or not record['filename'].strip()):
        return None, trace
    request = interpret(record['task'])
    if request is None:
        return None, trace
    trace.update(stage='selection', understood=True, request=request)
    blocks = record['blocks']
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 16:
        return None, trace
    return blocks, trace


def valid_block(block, seen):
    return (isinstance(block, dict) and set(block) == {'id', 'text'}
            and isinstance(block['id'], str) and bool(block['id'].strip())
            and len(block['id']) <= 128 and block['id'] not in seen and isinstance(block['text'], str))


def decide(excerpt, *, complete):
    blocks, trace = _read_request(excerpt, complete=complete)
    if blocks is None:
        return None, MEANING, trace
    request = trace['request']
    ids = set()
    matches = []
    for block in blocks:
        if not valid_block(block, ids):
            return None, MEANING, trace
        ids.add(block['id'])
        for start, sentence in _sentences(block['text']):
            if _REFERENCE.fullmatch(sentence):
                continue
            subject = _SOURCE_QUANTITY.match(sentence)
            if not subject:
                return None, MEANING, trace
            if subject['quantity'].lower() != request['quantity']:
                if not _REQUIREMENT.fullmatch(sentence):
                    return None, MEANING, trace
                continue
            technical = _REQUIREMENT.fullmatch(sentence)
            if technical and technical['unit'] != _UNITS[request['quantity']]:
                return None, MEANING, trace
            if not technical:
                # A private marker never makes an arbitrary quantity mention an answer.
                if not (_PRIVATE.search(sentence) and any(
                        limit['unit'] == _UNITS[request['quantity']] for limit in _PRIVATE_LIMIT.finditer(sentence))):
                    return None, MEANING, trace
            # Only a fully understood clause can be excluded on condition alone.
            wanted = request['condition']
            if wanted and technical and not sentence.endswith(' ' + wanted + '.'):
                continue
            if wanted and not technical and wanted not in sentence:
                return None, MEANING, trace
            matches.append((sentence, dict(id=block['id'], start=start, end=start + len(sentence))))
    if len(matches) != 1:
        return None, MEANING, trace
    sentence, span = matches[0]
    trace.update(stage='sensitivity', selected=span)
    if _PRIVATE.search(sentence):
        return None, SENSITIVITY, trace
    return sentence, None, trace
