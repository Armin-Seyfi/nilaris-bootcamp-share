"""Review regressions through captured workbooks and public ingestion results."""
from io import BytesIO
from zipfile import ZipFile

import pytest

from test_fixture_extraction import capture_content, capture_fixture, fixture_input


@pytest.mark.parametrize(('label', 'coordinate', 'field_key'), [
    ('Material', 'B4', 'material'), ('Delivery', 'B5', 'delivery')])
def test_numeric_workbook_text_field_remains_raw_and_unresolved(
        database, label, coordinate, field_key):
    from openpyxl import load_workbook
    item, content = fixture_input('W.xlsx')
    workbook = load_workbook(BytesIO(content))
    workbook['Properties'][coordinate] = 12
    changed = BytesIO()
    workbook.save(changed)
    workbook.close()
    content = changed.getvalue()
    item.update(member_key=f'W-numeric-{field_key}', source_revision=f'W-number-{field_key}-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'completed'
    publication, = client.result(job_id)['publications']
    observation, = [row for row in publication['observations'] if row['label'] == label]
    assert observation['original_cells'][coordinate] == dict(value=12, type='n')
    assert (observation['source_type'], observation['value'], observation['numeric_xml_token']) == (
        'number', '12', '12')
    association, = [row for row in publication['associations']
                   if row['observation_ids'] == [observation['id']]]
    assert association['disposition'] == 'unresolved'
    assert all(row['field_key'] != field_key for row in publication['candidates'])


@pytest.mark.parametrize('layout', ['header', 'formula', 'merged', 'hidden_row',
                                 'hidden_column', 'nontext_label'])
def test_rejected_workbook_layout_retains_partial_processing_coverage(database, layout):
    from openpyxl import load_workbook
    item, content = fixture_input('W.xlsx')
    workbook = load_workbook(BytesIO(content))
    sheet = workbook['Properties']
    if layout == 'header':
        sheet['A1'] = 'Unsupported header'
    elif layout == 'formula':
        sheet['B2'] = '=6+6'
    elif layout == 'merged':
        sheet.merge_cells('A4:B4')
    elif layout == 'hidden_row':
        sheet.row_dimensions[4].hidden = True
    elif layout == 'hidden_column':
        sheet.column_dimensions['B'].hidden = True
    else:
        sheet['A4'] = 12
    changed = BytesIO()
    workbook.save(changed)
    workbook.close()
    content = changed.getvalue()
    item.update(member_key=f'W-bad-{layout}', source_revision=f'W-bad-{layout}-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['inspected'] == []
    assert publication['coverage']['unread'] == ['input:unsupported-workbook-layout']
    assert publication['observations'] == publication['candidates'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'


def test_malformed_worksheet_xml_retains_partial_processing_coverage(database):
    item, content = fixture_input('W.xlsx')
    changed = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(changed, 'w') as target:
        for entry in original.infolist():
            data = original.read(entry.filename)
            if entry.filename == 'xl/worksheets/sheet1.xml':
                assert b'</worksheet>' in data
                data = data.replace(b'</worksheet>', b'</broken>')
            target.writestr(entry, data)
    content = changed.getvalue()
    item.update(member_key='W-malformed-sheet', source_revision='W-malformed-sheet-r1',
                input_bytes=len(content))
    client, job_id = capture_content(database, item, content)
    assert client.run_fixture(job_id, client.status(job_id)['revision'])['state'] == 'partial'
    publication, = client.result(job_id)['publications']
    assert publication['coverage']['inspected'] == []
    assert publication['coverage']['unread'] == ['input:malformed-workbook']
    assert publication['observations'] == publication['candidates'] == []
    other, other_id = capture_fixture(database, 'P.pdf')
    assert other.run_fixture(other_id, other.status(other_id)['revision'])['state'] == 'completed'
