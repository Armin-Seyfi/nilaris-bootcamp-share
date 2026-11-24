"""Narrow versioned fixture rules; no expected answers or document-name rules.

Loaded only by the supervised native graph stage. Raw source rows and proposed
interpretations remain distinct effects, with no expert approval or defaults.
"""
import base64
import hashlib
from dataclasses import dataclass
from io import BytesIO
from itertools import islice
import re
from zipfile import BadZipFile

from nilaris.results import Effect, StageOutput


FIELD_CONTRACT = 'fixture-fields-v1'
METHOD = 'declared-ascii-label-and-literal-unit-v1'
ASCII_SPACE = ' \t\n\r\f\v'
ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
DECIMAL = re.compile(r'[0-9]+(?:\.[0-9]+)?\Z', re.ASCII)


@dataclass(frozen=True)
class FieldRule:
    field_key: str
    label: str
    value_type: str
    unit: str | None
    domain: str


RULES = {
    'flow rate': FieldRule(field_key='flow_rate', label='Flow rate',
                          value_type='decimal', unit='m3/h', domain='technical'),
    'pressure': FieldRule(field_key='pressure', label='Pressure',
                         value_type='decimal', unit='bar', domain='technical'),
    'material': FieldRule(field_key='material', label='Material',
                         value_type='text', unit=None, domain='technical'),
    'delivery': FieldRule(field_key='delivery', label='Delivery',
                         value_type='text', unit=None, domain='logistical'),
}


def normalize_label(value):
    return re.sub(r'[ \t\n\r\f\v]+', ' ', value.strip(ASCII_SPACE)).translate(ASCII_LOWER)


def row_effects(line, page, region, source):
    label, separator, raw_value = line.partition(':')
    label = label.strip(ASCII_SPACE)
    value = raw_value.strip(ASCII_SPACE) if separator else line
    rule = RULES.get(normalize_label(label)) if separator else None
    field_key = rule.field_key if rule else 'unresolved'
    observation_id = f'observation:{field_key}:{region}'
    locator = dict(page=page, text=line)
    unit = None
    if rule and rule.value_type == 'decimal':
        value, space, unit = value.rpartition(' ')
        if not space:
            value, unit = raw_value.strip(ASCII_SPACE), None
    observation = dict(id=observation_id, locator=locator, label=label,
        kind='property', original_content=line, value=value, unit=unit,
        source_type='text', presence='observed', content_pin=source['content_pin'],
        document_id=source['document_id'], source_revision=source['source_revision'])
    return proposed_effects(observation, rule, region)


def proposed_effects(observation, rule, region):
    """Apply the same declared field rules to source-preserving parser rows."""
    field_key = rule.field_key if rule else 'unresolved'
    value, unit = observation['value'], observation['unit']
    observation_id = observation['id']
    valid = bool(rule and value and
                 ((rule.value_type == 'text' and observation['source_type'] == 'text' and unit is None) or
                  (rule.value_type == 'decimal' and DECIMAL.fullmatch(value) and unit == rule.unit)))
    if rule and rule.value_type == 'decimal' and isinstance(value, str) and DECIMAL.fullmatch(value):
        observation['decimal_string'] = value
    effects = [Effect('observation', region, observation)]
    association = dict(id=f'association:{field_key}:{region}',
        observation_ids=[observation_id], method=METHOD, field_contract=FIELD_CONTRACT,
        matched_rule=field_key if rule else None, source_operands=dict(locator=observation['locator'],
            label=observation['label'], value=value, unit=unit),
        rationale='Declared ASCII label matching, unsigned decimal grammar and literal units only.',
        disposition='awaiting_expert_decision' if valid else 'unresolved')
    effects.append(Effect('association', region, association))
    if valid:
        candidate = dict(id=f'candidate:{field_key}:{region}', field_key=field_key,
            label=rule.label, value_type=rule.value_type, unit=unit, domain=rule.domain,
            observation_ids=[observation_id], requirements='unspecified',
            optionality='unspecified', conditions='unspecified',
            field_contract=FIELD_CONTRACT, method=METHOD,
            review_disposition='awaiting_expert_decision')
        if rule.domain == 'logistical':
            candidate['value'] = value
        effects.append(Effect('candidate', region, candidate))
    return effects


def process(source):
    content = base64.b64decode(source['content'], validate=True)
    if (len(content) > 1024**2 or
            hashlib.sha256(content).hexdigest() != source['content_pin']):
        raise ValueError('invalid captured input')
    coverage = dict(inspected=[], unread=[], limits=[
        'selectable-single-line-fixture-layout-only',
        'generic-discovery-quality-unproven', FIELD_CONTRACT])
    effects = []
    outcome = 'completed'
    if source['media_type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
        from nilaris.fixture_workbook import (
            WorkbookCellLimit, WorkbookExpansionRatioLimit, WorkbookFormatError, WorkbookLayoutError,
            WorkbookMemberLimit, WorkbookMetadataExpansionLimit, WorkbookReadLimit, workbook_rows)
        coverage['limits'][0] = 'three-column-Properties-fixture-layout-only'
        try:
            observations, inspected = workbook_rows(content, source)
        except WorkbookLayoutError:
            coverage['unread'].append('input:unsupported-workbook-layout')
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        except WorkbookReadLimit as error:
            coverage['unread'].append('input:workbook-actual-expanded-read-limit')
            coverage['counters'] = dict(expanded_bytes_read=error.expanded_bytes,
                                        expanded_read_limit=error.limit)
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        except (BadZipFile, WorkbookFormatError):
            coverage['unread'].append('input:malformed-workbook')
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        except WorkbookCellLimit as error:
            coverage['unread'].append('input:workbook-cell-limit')
            coverage['counters'] = dict(workbook_cell_limit=100, workbook_cells_seen=error.cells_seen)
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        except WorkbookMemberLimit as error:
            coverage['unread'].append('input:workbook-member-limit')
            coverage['counters'] = dict(workbook_member_limit=64, workbook_members_seen=error.members_seen)
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        except WorkbookExpansionRatioLimit as error:
            coverage['unread'].append('input:workbook-expansion-ratio-limit')
            coverage['counters'] = dict(expansion_ratio_limit=100,
                member_expanded_bytes=error.expanded_bytes,
                member_compressed_bytes=error.compressed_bytes, expanded_bytes_read=0)
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        except WorkbookMetadataExpansionLimit as error:
            coverage['unread'].append('input:workbook-expanded-metadata-limit')
            coverage['counters'] = dict(expanded_read_limit=4 * 1024**2,
                archive_metadata_expanded_bytes=error.expanded_bytes, expanded_bytes_read=0)
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        coverage['inspected'] = inspected
        for region, observation in enumerate(observations):
            effects.extend(proposed_effects(observation, RULES.get(normalize_label(observation['label'])), region))
        return StageOutput(outcome, coverage, tuple(effects), adapter='fixture-native-v1')
    if source['media_type'] != 'application/pdf':
        raise ValueError('unsupported native fixture media type')
    import pdfplumber
    from pdfminer.pdfpage import PDFPage
    from pdfplumber.utils.exceptions import PdfminerException
    try:
        document = pdfplumber.open(BytesIO(content))
    except PdfminerException:
        coverage['unread'].append('input:malformed-pdf')
        return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
    with document:
        # Inspect at most the first disallowed page's metadata before constructing
        # pdfplumber's full page list or extracting any text.
        if sum(1 for _ in islice(PDFPage.create_pages(document.doc), 3)) > 2:
            coverage['unread'].append('input:pdf-page-limit')
            coverage['counters'] = dict(pdf_page_limit=2, pdf_page_count_lower_bound=3)
            return StageOutput('partial', coverage, (), adapter='fixture-native-v1')
        region = 0
        for page_number, page in enumerate(document.pages, 1):
            text = page.extract_text() or ''
            if len(text.encode('utf-8')) > 1024**2:
                raise ValueError('extracted text limit')
            coverage['inspected'].append(f'page:{page_number}:selectable-text')
            if not text:
                coverage['unread'].append(f'page:{page_number}:no-selectable-text')
                outcome = 'partial'
            for line in text.splitlines():
                if region >= 100:
                    raise ValueError('fixture row limit')
                effects.extend(row_effects(line, page_number, region, source))
                region += 1
    return StageOutput(outcome, coverage, tuple(effects), adapter='fixture-native-v1')
