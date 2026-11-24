"""Exact synthetic artifacts through public ingestion input/status/results."""
import json
from io import BytesIO
from pathlib import Path
import random
import time
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_command_service import command_process


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/a1'
EXPECTED = json.loads((ROOT / 'tests/expectations/a1/exact_fixture.json').read_text())


def fixture_input(artifact):
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    delivery = next(row for row in manifest['deliveries'] if row['artifact'] == artifact)
    context = manifest['context']
    item = {key: delivery[key] for key in ('source_id', 'source_revision', 'document_id',
             'identity', 'media_type', 'input_bytes', 'intent_id')}
    item.update(member_key=delivery['delivery_key'], connector='synthetic',
                case_id=context['case_id'], membership_revision=context['membership_revision'],
                product_type=context['nominated_product_type'],
                processing_contract=manifest['processing_contract'],
                reference_set=manifest['reference_set'], selection_policy=manifest['selection_policy'])
    return item, (FIXTURES / artifact).read_bytes()


def capture_fixture(database, artifact):
    item, content = fixture_input(artifact)
    return capture_content(database, item, content)


def capture_content(database, item, content):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    receipt = client.offer('fixture-operator', item['member_key'], [item])
    assert receipt['totals']['accepted'] == 1
    job_id = receipt['members'][0]['job_id']
    captured = client.capture(job_id, client.claim(job_id, 0),
        ControlledSource({item['source_id']: (item['source_revision'], content)}))
    assert captured['state'] == 'queued'
    return client, job_id


def selectable_pdf(pages):
    """Author only controlled selectable-line variants, independently of parsing."""
    from reportlab.pdfgen.canvas import Canvas
    output = BytesIO()
    canvas = Canvas(output, pagesize=(612, 792), invariant=1)
    for lines in pages:
        for index, line in enumerate(lines):
            canvas.drawString(72, 720 - index * 24, line)
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def test_exact_pdf_yields_source_observations_and_unapproved_candidates(database):
    client, job_id = capture_fixture(database, 'P.pdf')
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    result = Ingestion(database).result(job_id)
    publication, = result['publications']
    observations = publication['observations']
    assert len(observations) == 4
    for actual, expected in zip(observations, EXPECTED['P']['observations']):
        assert {key: actual[key] for key in expected} == expected
    technical = [row for row in publication['candidates'] if row['domain'] == 'technical']
    assert [(row['field_key'], row['label'], row['value_type'], row['unit']) for row in technical] == [
        ('flow_rate', 'Flow rate', 'decimal', 'm3/h'),
        ('pressure', 'Pressure', 'decimal', 'bar'),
        ('material', 'Material', 'text', None)]
    logistical, = [row for row in publication['candidates'] if row['domain'] == 'logistical']
    assert (logistical['label'], logistical['value']) == ('Delivery', 'Six weeks')
    assert result['approved'] is publication['approved'] is False
    assert publication['content_pin'] == finished['content_pin']
    assert publication['context']['document_id'] == EXPECTED['P']['document_id']
    assert publication['context']['reference_set'] == 'none'
    assert all(row['requirements'] == 'unspecified' for row in publication['candidates'])
    assert publication['coverage']['unread'] == []


def test_exact_workbook_preserves_typed_cells_and_exact_numeric_tokens(database):
    client, job_id = capture_fixture(database, 'W.xlsx')
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    publication, = Ingestion(database).result(job_id)['publications']
    observations = publication['observations']
    assert len(observations) == 4
    for actual, expected in zip(observations, EXPECTED['W']['observations']):
        assert {key: actual[key] for key in expected} == expected
    assert [(row['numeric_xml_token'], row['decimal_string']) for row in observations[:2]] == [
        ('12', '12'), ('6', '6')]
    assert [(row['field_key'], row['value_type'], row['unit'])
            for row in publication['candidates'] if row['domain'] == 'technical'] == [
        ('flow_rate', 'decimal', 'm3/h'), ('pressure', 'decimal', 'bar'),
        ('material', 'text', None)]
    delivery, = [row for row in publication['candidates'] if row['domain'] == 'logistical']
    assert (delivery['label'], delivery['value']) == ('Delivery', 'Six weeks')
    assert publication['context']['product_type'] == 'Fixture pump'
    assert publication['approved'] is False


def test_workbook_material_with_unit_remains_raw_and_unresolved(database):
    from openpyxl import load_workbook
    item, content = fixture_input('W.xlsx')
    workbook = load_workbook(BytesIO(content))
    workbook['Properties']['C4'] = 'kg'
    changed = BytesIO()
    workbook.save(changed)
    workbook.close()
    content = changed.getvalue()
    item.update(member_key='W-material-with-unit', source_revision='W-unit-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    material, = [row for row in publication['observations'] if row['label'] == 'Material']
    assert material['original_cells']['C4'] == dict(value='kg', type='s')
    assert (material['value'], material['unit']) == ('Cast iron', 'kg')
    association, = [row for row in publication['associations']
                   if row['observation_ids'] == [material['id']]]
    assert association['disposition'] == 'unresolved'
    assert all(row['field_key'] != 'material' for row in publication['candidates'])


def test_workbook_decimal_xml_token_survives_without_binary_float_rounding(database):
    item, content = fixture_input('W.xlsx')
    exact = '12.12345678901234567890123456789'
    changed = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(changed, 'w') as target:
        for entry in original.infolist():
            data = original.read(entry.filename)
            if entry.filename == 'xl/worksheets/sheet1.xml':
                old = b'<c r="B2" t="n"><v>12</v></c>'
                assert old in data
                data = data.replace(old, f'<c r="B2" t="n"><v>{exact}</v></c>'.encode())
            target.writestr(entry, data)
    content = changed.getvalue()
    item.update(member_key='W-exact-decimal', source_revision='W-exact-decimal-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    flow, = [row for row in publication['observations'] if row['label'] == 'Flow rate']
    assert flow['source_type'] == 'number'
    assert (flow['numeric_xml_token'], flow['decimal_string'], flow['value']) == (exact, exact, exact)
    assert flow['original_cells']['B2'] == dict(type='n', value=exact)
    candidate, = [row for row in publication['candidates'] if row['field_key'] == 'flow_rate']
    assert (candidate['value_type'], candidate['unit']) == ('decimal', 'm3/h')


def test_renamed_pdf_reuses_job_and_changed_revision_preserves_initial_publication(database):
    client, job_id = capture_fixture(database, 'P.pdf')
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'completed'
    initial = client.result(job_id)
    mapped, _ = fixture_input('delivery-renamed.pdf')
    receipt = client.offer('fixture-operator', 'renamed-delivery', [mapped])
    renamed, = receipt['members']
    assert (renamed['disposition'], renamed['job_id']) == ('existing', job_id)
    assert client.result(renamed['job_id']) == initial
    changed_client, changed_id = capture_fixture(database, 'P2.pdf')
    assert changed_id != job_id
    assert changed_client.run_fixture(changed_id, changed_client.status(changed_id)['revision'])['state'] == 'completed'
    changed, = changed_client.result(changed_id)['publications']
    original, = initial['publications']
    assert changed['content_pin'] != original['content_pin']
    assert changed['context']['document_id'] == original['context']['document_id']
    assert changed['context']['source_revision'] == 'a1-P-r2'
    flow, = [row for row in changed['observations'] if row['label'] == 'Flow rate']
    for key, expected in EXPECTED['P2']['changed_observation'].items():
        assert flow[key] == expected
    assert Ingestion(database).result(job_id) == initial


def test_ambiguous_copy_without_document_mapping_is_deferred_for_identity(database):
    client, job_id = capture_fixture(database, 'P.pdf')
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'completed'
    initial = client.result(job_id)
    ambiguous, copy = fixture_input('ambiguous-copy.pdf')
    assert ambiguous['document_id'] is None
    assert copy == (FIXTURES / 'P.pdf').read_bytes()
    receipt = client.offer('fixture-operator', 'ambiguous-copy', [ambiguous])
    disposition, = receipt['members']
    assert disposition['reason'] == 'identity_unresolved'
    assert disposition['disposition'] == 'deferred'
    assert receipt['totals'] == dict(offered=1, accepted=0, deferred=1, rejected=0)
    assert 'job_id' not in disposition
    assert Ingestion(database).result(job_id) == initial


def test_workbook_blank_value_remains_blank_raw_with_unresolved_interpretation(database):
    from openpyxl import load_workbook
    item, content = fixture_input('W.xlsx')
    workbook = load_workbook(BytesIO(content))
    workbook['Properties']['B2'] = None
    changed = BytesIO()
    workbook.save(changed)
    workbook.close()
    content = changed.getvalue()
    item.update(member_key='W-blank-flow', source_revision='W-blank-flow-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    assert len(publication['observations']) == 4
    blank, = [row for row in publication['observations'] if row['label'] == 'Flow rate']
    assert (blank['presence'], blank['value'], blank['source_type']) == ('blank', None, 'blank')
    assert blank['original_cells']['B2'] == dict(value=None, type='blank')
    assert blank['original_cells']['C2'] == dict(value='m3/h', type='s')
    association, = [row for row in publication['associations']
                   if row['observation_ids'] == [blank['id']]]
    assert association['disposition'] == 'unresolved'
    assert all(row['field_key'] != 'flow_rate' for row in publication['candidates'])
    assert all(row['presence'] != 'absent_in_inspected_scope' for row in publication['observations'])


@pytest.mark.parametrize(('raw', 'value', 'unit'), [
    ('Flow rate: unknown m3/h', 'unknown', 'm3/h'),
    ('Flow rate: 12 gpm', '12', 'gpm')])
def test_pdf_unsupported_flow_preserves_raw_and_unresolved_association(database, raw, value, unit):
    item, _ = fixture_input('P.pdf')
    content = selectable_pdf([[raw, 'Pressure: 6 bar', 'Material: Cast iron', 'Delivery: Six weeks']])
    item.update(member_key='P-unsupported-flow', source_revision='P-unsupported-flow-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    assert len(publication['observations']) == 4
    observation, = [row for row in publication['observations'] if row['label'] == 'Flow rate']
    assert (observation['original_content'], observation['value'], observation['unit']) == (raw, value, unit)
    assert observation['locator'] == dict(page=1, text=raw)
    assert observation['content_pin'] == publication['content_pin']
    association, = [row for row in publication['associations']
                   if row['observation_ids'] == [observation['id']]]
    assert association['disposition'] == 'unresolved'
    assert association['matched_rule'] == 'flow_rate'
    assert association['source_operands'] == dict(locator=observation['locator'],
                                                 label='Flow rate', value=value, unit=unit)
    assert all(row['field_key'] != 'flow_rate' for row in publication['candidates'])


def test_repeated_pdf_labels_keep_separate_conflicting_source_values(database):
    item, _ = fixture_input('P.pdf')
    content = selectable_pdf([['Flow rate: 12 m3/h', 'Flow rate: 13 m3/h',
                              'Pressure: 6 bar', 'Material: Cast iron', 'Delivery: Six weeks']])
    item.update(member_key='P-conflicting-flow', source_revision='P-conflicting-flow-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    observations = [row for row in publication['observations'] if row['label'] == 'Flow rate']
    assert [(row['value'], row['original_content']) for row in observations] == [
        ('12', 'Flow rate: 12 m3/h'), ('13', 'Flow rate: 13 m3/h')]
    assert len({row['id'] for row in observations}) == 2
    candidates = [row for row in publication['candidates'] if row['field_key'] == 'flow_rate']
    assert len({row['id'] for row in candidates}) == 2
    assert [row['observation_ids'] for row in candidates] == [[row['id']] for row in observations]
    associations = [row for row in publication['associations'] if row['matched_rule'] == 'flow_rate']
    assert [row['source_operands']['value'] for row in associations] == ['12', '13']
    assert all(row['review_disposition'] == 'awaiting_expert_decision' for row in candidates)


def test_unread_pdf_page_publishes_partial_without_claiming_absence(database):
    item, _ = fixture_input('P.pdf')
    content = selectable_pdf([['Flow rate: 12 m3/h', 'Pressure: 6 bar',
                              'Material: Cast iron', 'Delivery: Six weeks'], []])
    item.update(member_key='P-unread-page', source_revision='P-unread-page-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['outcome'] == 'partial'
    assert publication['coverage']['unread'] == ['page:2:no-selectable-text']
    assert len(publication['observations']) == 4
    assert all(row['locator']['page'] == 1 for row in publication['observations'])
    assert all(row['presence'] == 'observed' for row in publication['observations'])
    assert publication['approved'] is False


def test_malformed_pdf_has_bounded_partial_coverage_and_next_job_progresses(database):
    item, _ = fixture_input('P.pdf')
    content = b'%PDF-1.7\ncontrolled malformed fixture\n%%EOF\n'
    item.update(member_key='P-malformed', source_revision='P-malformed-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['inspected'] == []
    assert publication['coverage']['unread'] == ['input:malformed-pdf']
    assert publication['observations'] == []
    assert publication['candidates'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    started = time.monotonic()
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'
    assert time.monotonic() - started < 35
    assert client.result(job_id)['publications'] == [publication]


def test_unsupported_workbook_layout_is_partial_and_next_job_progresses(database):
    from openpyxl import load_workbook
    item, content = fixture_input('W.xlsx')
    workbook = load_workbook(BytesIO(content))
    workbook.create_sheet('Other')
    changed = BytesIO()
    workbook.save(changed)
    workbook.close()
    content = changed.getvalue()
    item.update(member_key='W-unsupported-layout', source_revision='W-unsupported-layout-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:unsupported-workbook-layout']
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'W.xlsx')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_actual_workbook_expanded_reads_are_capped_and_next_job_progresses(database):
    item, content = fixture_input('W.xlsx')
    # A small compressed source with modest ratio. Its single sheet is read for
    # exact source operands and again by openpyxl, so metadata alone is insufficient.
    payload = ''.join(random.Random(47).choices('ABCD', k=2_200_000)).encode()
    changed = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(changed, 'w') as target:
        for entry in original.infolist():
            data = original.read(entry.filename)
            if entry.filename == 'xl/worksheets/sheet1.xml':
                assert b'<t>Cast iron</t>' in data
                data = data.replace(b'<t>Cast iron</t>', b'<t>' + payload + b'</t>')
            target.writestr(entry, data)
    content = changed.getvalue()
    assert len(content) < 1024**2
    with ZipFile(BytesIO(content)) as package:
        metadata_expanded = sum(entry.file_size for entry in package.infolist())
        assert metadata_expanded < 4 * 1024**2
        assert all(entry.file_size < 100 * entry.compress_size for entry in package.infolist())
    item.update(member_key='W-actual-read-limit', source_revision='W-actual-read-limit-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:workbook-actual-expanded-read-limit']
    counters = publication['coverage']['counters']
    assert metadata_expanded < counters['expanded_bytes_read'] <= 4 * 1024**2
    assert counters['expanded_read_limit'] == 4 * 1024**2
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_malformed_workbook_has_partial_coverage_and_next_job_progresses(database):
    item, _ = fixture_input('W.xlsx')
    content = b'PK\x03\x04controlled malformed workbook'
    item.update(member_key='W-malformed', source_revision='W-malformed-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:malformed-workbook']
    assert publication['observations'] == []
    assert publication['candidates'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_pdf_page_limit_is_partial_with_unread_scope_and_next_job_progresses(database):
    item, _ = fixture_input('P.pdf')
    content = selectable_pdf([['Flow rate: 12 m3/h'], ['Pressure: 6 bar'], ['Material: Cast iron']])
    item.update(member_key='P-page-limit', source_revision='P-page-limit-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:pdf-page-limit']
    assert publication['coverage']['inspected'] == []
    assert publication['coverage']['counters'] == dict(pdf_page_limit=2, pdf_page_count_lower_bound=3)
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_workbook_cell_limit_is_partial_and_next_job_progresses(database):
    from openpyxl import load_workbook
    item, content = fixture_input('W.xlsx')
    workbook = load_workbook(BytesIO(content))
    sheet = workbook['Properties']
    for _ in range(29):
        sheet.append(('Pressure', 6, 'bar'))
    sheet['A35'] = 'Material'
    assert sum(cell.value is not None for row in sheet for cell in row) == 101
    changed = BytesIO()
    workbook.save(changed)
    workbook.close()
    content = changed.getvalue()
    item.update(member_key='W-cell-limit', source_revision='W-cell-limit-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:workbook-cell-limit']
    assert publication['coverage']['counters'] == dict(workbook_cell_limit=100, workbook_cells_seen=101)
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_workbook_zip_member_limit_is_partial_and_next_job_progresses(database):
    item, content = fixture_input('W.xlsx')
    changed = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(changed, 'w') as target:
        for entry in original.infolist():
            target.writestr(entry, original.read(entry.filename))
        for index in range(56):
            target.writestr(f'controlled-extra-{index}.txt', b'x')
    content = changed.getvalue()
    with ZipFile(BytesIO(content)) as package:
        assert len(package.infolist()) == 65
    item.update(member_key='W-member-limit', source_revision='W-member-limit-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:workbook-member-limit']
    assert publication['coverage']['counters'] == dict(workbook_member_limit=64, workbook_members_seen=65)
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_workbook_expansion_ratio_limit_is_partial_and_next_job_progresses(database):
    item, content = fixture_input('W.xlsx')
    changed = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(changed, 'w') as target:
        for entry in original.infolist():
            target.writestr(entry, original.read(entry.filename))
        target.writestr('controlled-compressed.txt', b'A' * 100_000, compress_type=ZIP_DEFLATED)
    content = changed.getvalue()
    with ZipFile(BytesIO(content)) as package:
        compressed = package.getinfo('controlled-compressed.txt').compress_size
        assert 100_000 > 100 * compressed
    item.update(member_key='W-ratio-limit', source_revision='W-ratio-limit-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:workbook-expansion-ratio-limit']
    assert publication['coverage']['counters'] == dict(expansion_ratio_limit=100,
        member_expanded_bytes=100_000, member_compressed_bytes=compressed, expanded_bytes_read=0)
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_workbook_metadata_expansion_limit_is_partial_and_next_job_progresses(database):
    item, content = fixture_input('W.xlsx')
    payload = ''.join(random.Random(48).choices('AB', k=4_200_000)).encode()
    changed = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(changed, 'w') as target:
        for entry in original.infolist():
            target.writestr(entry, original.read(entry.filename))
        target.writestr('controlled-expanded.txt', payload, compress_type=ZIP_DEFLATED)
    content = changed.getvalue()
    assert len(content) < 1024**2
    with ZipFile(BytesIO(content)) as package:
        metadata_bytes = sum(entry.file_size for entry in package.infolist())
        assert metadata_bytes > 4 * 1024**2
        assert all(entry.file_size <= 100 * entry.compress_size for entry in package.infolist())
    item.update(member_key='W-metadata-limit', source_revision='W-metadata-limit-r1', input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['unread'] == ['input:workbook-expanded-metadata-limit']
    assert publication['coverage']['counters'] == dict(expanded_read_limit=4 * 1024**2,
        archive_metadata_expanded_bytes=metadata_bytes, expanded_bytes_read=0)
    assert publication['observations'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_native_candidate_metadata_is_explicit_without_observed_defaults(database):
    client, job_id = capture_fixture(database, 'P.pdf')
    finished = client.run_fixture(job_id, client.status(job_id)['revision'])
    assert finished['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    technical = [row for row in publication['candidates'] if row['domain'] == 'technical']
    assert len(technical) == 3
    for candidate in technical:
        assert (candidate['requirements'], candidate['optionality'], candidate['conditions']) == (
            'unspecified', 'unspecified', 'unspecified')
        assert candidate['field_contract'] == 'fixture-fields-v1'
        assert candidate['method'] == 'declared-ascii-label-and-literal-unit-v1'
        assert candidate['review_disposition'] == 'awaiting_expert_decision'
        assert 'default' not in candidate and 'value' not in candidate
    assert publication['context']['case_id'] == 'a1-case-fixture'
    assert publication['context']['product_type'] == 'Fixture pump'
    assert publication['context']['source_revision'] == 'a1-P-r1'
    assert publication['context']['processing_contract'] == 'fixture-v1'
    assert publication['context']['reference_set'] == 'none'
    assert publication['profile']['version'] == 'L1-v1'
    assert publication['content_pin'] == finished['content_pin']
    assert publication['approved'] is False


def test_native_publication_marks_case_and_product_type_context_as_supplied(database):
    client, job_id = capture_fixture(database, 'P.pdf')
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    assert publication['context_origin'] == 'supplied'
    assert publication['context']['case_id'] == 'a1-case-fixture'
    assert publication['context']['membership_revision'] == 'a1-manifest-1'
    assert publication['context']['product_type'] == 'Fixture pump'
    assert publication['context']['reference_set'] == 'none'
    assert publication['approved'] is False


def test_actual_native_proposals_support_textual_review_and_preserve_approved_state(database, command_process):
    """Synthetic expert commands demonstrate integration, not human acceptance."""
    ingestion, job_id = capture_fixture(database, 'P.pdf')
    assert ingestion.run_fixture(job_id, ingestion.status(job_id)['revision'])['state'] == 'completed'
    initial_result = ingestion.result(job_id)
    initial, = initial_result['publications']
    client, start, _ = command_process
    start()
    created = client(dict(operation='review_create', namespace='fixture-expert', command_id='create',
        job_id=job_id, proposal_id=initial['id'], product_type='Fixture pump',
        expected_base=None, actor='synthetic-expert-fixture'))
    assert created['saved'] is True
    draft_id = created['draft_id']
    view = client(dict(operation='review_read', draft_id=draft_id))
    assert len(view['definitions']) == 3
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
        if definition['label'] == 'Flow rate':
            definition['label'] = 'Rated flow rate'
    for association in view['associations']:
        association['disposition'] = 'deferred' if association['matched_rule'] == 'delivery' else 'accepted'
    saved = client(dict(operation='review_save', namespace='fixture-expert', command_id='save',
        draft_id=draft_id, expected_revision=0, definitions=view['definitions'],
        associations=view['associations'], actor='synthetic-expert-fixture',
        reason='Synthetic explicit decisions and reviewed display label'))
    assert saved['saved'] is True
    resumed = client(dict(operation='review_resume', draft_id=draft_id))
    flow, = [row for row in resumed['definitions'] if row['label'] == 'Rated flow rate']
    assert flow['origin']['kind'] == 'human'
    assert resumed['initial'] == initial
    approved = client(dict(operation='review_approve', namespace='fixture-expert', command_id='approve',
        draft_id=draft_id, expected_revision=saved['revision'], product_type='Fixture pump',
        actor='synthetic-expert-fixture', explicit=True))
    assert approved['approved'] is True
    before = client(dict(operation='review_read', draft_id=draft_id))
    schema = before['approved_schema']
    assert [row['label'] for row in schema['definitions']] == ['Rated flow rate', 'Pressure', 'Material']
    assert all(row['requirements'] == row['conditions'] == 'unspecified' for row in schema['definitions'])
    assert all('default' not in row and 'value' not in row for row in schema['definitions'])
    mapped, _ = fixture_input('delivery-renamed.pdf')
    renamed = ingestion.offer('fixture-operator', 'renamed-after-approval', [mapped])
    assert renamed['members'][0]['job_id'] == job_id
    changed_client, changed_id = capture_fixture(database, 'P2.pdf')
    assert changed_client.run_fixture(changed_id, changed_client.status(changed_id)['revision'])['state'] == 'completed'
    changed, = changed_client.result(changed_id)['publications']
    incoming = client(dict(operation='review_incoming', draft_id=draft_id,
                          job_id=changed_id, proposal_id=changed['id']))
    assert incoming['read_only'] is True
    assert incoming['base_version'] == approved['approved_version']
    assert any(row['label'] == 'Flow rate' and row['value'] == '13'
               for row in incoming['incoming']['observations'])
    assert client(dict(operation='review_read', draft_id=draft_id)) == before
    assert ingestion.result(job_id) == initial_result
