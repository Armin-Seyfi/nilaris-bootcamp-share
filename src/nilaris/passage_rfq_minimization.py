"""Select verbatim RFQ evidence while allowing bounded administrative prose."""
import re

from nilaris.block_rfq_minimization import OBSERVATION_ID
from nilaris.qualified_rfq_minimization import (
    _CONDITIONS, _PRIVATE_LIMIT, _SOURCE_QUANTITY, _read_request, valid_block,
)
from nilaris.quantity_rfq_minimization import (
    MEANING, SENSITIVITY, _PRIVATE, _REFERENCE, _REQUIREMENT, _UNITS, _sentences,
)

PUBLICATION_ADAPTER = 'rfq-text-blocks-v4'
CONTRACT = 'rfq-block-minimization-v4'
DETECTOR = 'passage-quantity-rules-v1'

# Match a complete administrative statement. A logistical subject can still
# introduce a technical exception, so recognizing its first words is insufficient.
_DAY = r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
_ADMIN = re.compile(
    rf'(?:(?:The )?(?:delivery|shipment|site visit|meeting|bid opening) '
    rf'(?:is scheduled for|is on|will take place on) {_DAY}|'
    rf'(?:The )?(?:office|warehouse|reception desk) (?:is closed|opens|closes) on {_DAY}s?|'
    rf'(?:Bids|Quotations|Proposals|Submissions) are due (?:on |by )?{_DAY}|'
    r'(?:Please )?(?:send|submit|return) (?:the |your )?(?:bid|quotation|proposal) '
    r'(?:by email|in duplicate)|'
    r'(?:Invoices|Quotations|Bids|Packing lists|Shipping labels) are (?:sent|delivered|submitted) '
    r'(?:electronically|by email|by post|in duplicate)|'
    r'(?:The )?(?:packing list|invoice|delivery note|cover sheet|shipping label) '
    r'(?:is (?:enclosed|attached|included)|(?:shall|must|will) be '
    r'(?:enclosed|attached|included|sent separately))|'
    r'(?:The )?(?:cartons|crates|pallets|shipping labels) (?:are|shall be|must be) '
    r'(?:recyclable|stackable|numbered|labeled|labelled)|Thank you for (?:your interest|your quotation))\.',
    re.I | re.ASCII,
)
_EXCLUSIVE_PHASES = {'during startup', 'during steady operation'}


def decide(excerpt, *, complete):
    blocks, trace = _read_request(excerpt, complete=complete)
    if blocks is None:
        trace['cause'] = 'invalid_blocks' if trace['understood'] else 'uninterpreted_request'
        return None, MEANING, trace

    request = trace['request']
    wanted = request['condition']
    seen = set()
    matches = []
    unresolved = None
    for block in blocks:
        if not valid_block(block, seen):
            trace['cause'] = 'invalid_blocks'
            return None, MEANING, trace
        seen.add(block['id'])
        for start, raw in _sentences(block['text']):
            start += len(raw) - len(raw.lstrip())
            sentence = raw.strip()
            if not sentence or _REFERENCE.fullmatch(sentence) or _ADMIN.fullmatch(sentence):
                continue
            span = dict(id=block['id'], start=start, end=start + len(sentence))
            technical = _REQUIREMENT.fullmatch(sentence)
            if technical and technical['unit'] == _UNITS[technical['quantity'].lower()]:
                if technical['quantity'].lower() != request['quantity']:
                    continue
                condition = next((c for c in _CONDITIONS if sentence.endswith(' ' + c + '.')), None)
                if {wanted, condition} == _EXCLUSIVE_PHASES:
                    continue
            else:
                subject = _SOURCE_QUANTITY.match(sentence)
                limit = _PRIVATE_LIMIT.search(sentence)
                if not (_PRIVATE.search(sentence) and subject and limit
                        and subject['quantity'].lower() == request['quantity']
                        and limit['unit'] == _UNITS[request['quantity']]
                        and (not wanted or wanted in sentence)):
                    unresolved = unresolved or span
                    continue
                condition = wanted
            matches.append((sentence, span, condition))

    if len(matches) > 1:
        trace.update(cause='competing_passages', candidates=[match[1] for match in matches[:2]])
        return None, MEANING, trace
    if unresolved is not None:
        trace.update(cause='unresolved_passage', unresolved=unresolved)
        return None, MEANING, trace
    if not matches or (wanted and matches[0][2] != wanted):
        trace['cause'] = 'no_applicable_passage'
        return None, MEANING, trace
    sentence, span, _ = matches[0]
    trace.update(stage='sensitivity', selected=span)
    if _PRIVATE.search(sentence):
        trace['cause'] = 'private_marker'
        return None, SENSITIVITY, trace
    return sentence, None, trace
