"""Deterministic minimization for the normalized Pump RFQ v1 shape."""
import re

PUBLICATION_ADAPTER = 'pump-rfq-document-v1'
OBSERVATION_ID = 'pump-rfq-document'
CONTRACT = 'pump-rfq-minimization-v1'
DETECTOR = 'pump-rfq-field-rules-v1'

_DUTY, _LIMITS = 7, 9
_TECHNICAL_HEADINGS = {_DUTY: 'Duty', _LIMITS: 'Operating limits'}
_FIXED_LINES = {0: 'PUMP RFQ V1', 1: '[Contact]', 4: '[Commercial]',
                **{index: f'[{heading}]' for index, heading in _TECHNICAL_HEADINGS.items()}}
# Report one-based positions in the immutable observation, including the form title.
SELECTED_BLOCKS = tuple({'heading': heading, 'lines': [index + 1, index + 2]}
                        for index, heading in _TECHNICAL_HEADINGS.items())

_UNSIGNED_DECIMAL = r'(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,3})?'
_COMPARISON = r'(?:at least|at most|exactly)'
_FLOW = re.compile(rf'Flow: {_COMPARISON} {_UNSIGNED_DECIMAL} L/s '
                   rf'at differential head {_UNSIGNED_DECIMAL} m\.')
_SUCTION = re.compile(rf'Inlet pressure: {_COMPARISON} {_UNSIGNED_DECIMAL} kPa\((?:abs|g)\) '
                      rf'when liquid temperature is (?:above|below|at least|at most) '
                      rf'[+−-]?{_UNSIGNED_DECIMAL} °C\.')
_NAME = re.compile(r"[A-Za-z][A-Za-z'-]{0,23}(?: [A-Za-z][A-Za-z'-]{0,23}){1,4}")
_EMAIL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,39}@[A-Za-z0-9][A-Za-z0-9-]{0,31}'
                    r'(?:\.[A-Za-z0-9][A-Za-z0-9-]{0,15}){1,3}')
_QUOTE = re.compile(r'RFQ-[A-Z0-9]{2,10}(?:-[A-Z0-9]{2,10}){1,3}')
_METADATA = {'Contact name': _NAME, 'Contact email': _EMAIL, 'Buyer': _NAME, 'Quote ID': _QUOTE}


def _metadata_value(field, expected):
    label, _, value = field.partition(': ')
    if label == expected and _METADATA[label].fullmatch(value):
        return value
    return None


def _technical_block(line, grammar):
    # Only a structural delimiter can separate metadata from the complete technical clause.
    technical, delimiter, suffix = line.partition(' | ')
    metadata = None
    if delimiter:
        label = suffix.partition(': ')[0]
        if label not in ('Contact email', 'Quote ID') or not (metadata := _metadata_value(suffix, label)):
            return None, None, 'sensitivity_unresolved'
    if not grammar.fullmatch(technical):
        return None, None, 'technical_meaning_at_risk'
    return technical, metadata, None


def transform(excerpt, *, complete):
    if not complete:
        return None, 'technical_meaning_at_risk'
    if not isinstance(excerpt, str) or len(excerpt) > 2048:
        return None, 'sensitivity_unresolved'
    lines = excerpt.split('\n')
    if len(lines) != 11 or any(lines[index] != heading for index, heading in _FIXED_LINES.items()):
        return None, 'sensitivity_unresolved'

    protected = [_metadata_value(lines[i], label) for i, label in
                 ((2, 'Contact name'), (3, 'Contact email'), (5, 'Buyer'), (6, 'Quote ID'))]
    if not all(protected):
        return None, 'sensitivity_unresolved'

    flow, flow_metadata, reason = _technical_block(lines[_DUTY + 1], _FLOW)
    if reason:
        return None, reason
    suction, suction_metadata, reason = _technical_block(lines[_LIMITS + 1], _SUCTION)
    if reason:
        return None, reason
    preview = '\n'.join((flow, suction))
    protected.extend(value for value in (flow_metadata, suction_metadata) if value is not None)
    # A protected value reused inside the technical core has an ambiguous role.
    if any(value.casefold() in preview.casefold() for value in protected):
        return None, 'sensitivity_unresolved'
    return preview, None
