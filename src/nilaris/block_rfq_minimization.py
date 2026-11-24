"""Ordered-block RFQ observation and the enclosure sound-limit rule, version 1."""
import json
import re

PUBLICATION_ADAPTER = 'rfq-text-blocks-v1'
OBSERVATION_ID = 'rfq-text-blocks'
CONTRACT = 'rfq-block-minimization-v1'
DETECTOR = 'enclosure-sound-rules-v1'
TASK = 'Report the enclosure sound limit and its test condition.'
_SOUND = re.compile(
    r'Enclosure sound level shall be at most [0-9]{1,3}(?:\.[0-9]{1,2})? dB\(A\) '
    r'at [1-9][0-9]? m with the access panel (?:closed|latched)\.'
)
_REFERENCE = re.compile(r'Buyer reference: ([A-Z]{2,8}-[0-9]{3,8})\.')


def observation(task, filename, blocks):
    """Wrap a source record without selecting or rewriting any passage."""
    excerpt = json.dumps(dict(task=task, filename=filename, blocks=blocks), ensure_ascii=False)
    return dict(id=OBSERVATION_ID, kind='excerpt', source_type='text', presence='observed',
                locator={'range': 'ordered source blocks'}, original_content=excerpt,
                value=None, unit=None)


def transform(excerpt, *, complete):
    if not complete:
        return None, 'technical_meaning_at_risk'
    if not isinstance(excerpt, str) or len(excerpt) > 8192:
        return None, 'sensitivity_unresolved'
    try:
        record = json.loads(excerpt)
    except (ValueError, RecursionError):
        return None, 'sensitivity_unresolved'
    if (not isinstance(record, dict) or set(record) != {'filename', 'blocks', 'task'}
            or not isinstance(record['filename'], str) or not record['filename'].strip()):
        return None, 'sensitivity_unresolved'
    if record['task'] != TASK:
        return None, 'technical_meaning_at_risk'
    blocks = record['blocks']
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 16:
        return None, 'sensitivity_unresolved'
    identifiers = set()
    passage = None
    for block in blocks:
        if (not isinstance(block, dict) or set(block) != {'id', 'text'}
                or not isinstance(block['id'], str) or not block['id'].strip()
                or block['id'] in identifiers or not isinstance(block['text'], str)):
            return None, 'sensitivity_unresolved'
        identifiers.add(block['id'])
        text = block['text']
        if _REFERENCE.fullmatch(text):
            continue
        if not _SOUND.fullmatch(text) or passage is not None:
            return None, 'technical_meaning_at_risk'
        passage = text
    if passage is None:
        return None, 'technical_meaning_at_risk'
    return passage, None


def selected_blocks(excerpt, preview):
    # A saved preview under this contract is exactly one unique, complete block.
    return [{'id': block['id']} for block in json.loads(excerpt)['blocks'] if block['text'] == preview]
