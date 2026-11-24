"""Approved openpyxl parser behind one cumulative expanded-read allowance.

The version-bound ExcelReader integration installs the adapter before its first
expanded read. XML tokens supplement, rather than replace, typed-cell parsing.
"""
from io import BytesIO, RawIOBase
import posixpath
import re
from xml.etree import ElementTree


EXPANDED_LIMIT = 4 * 1024**2
MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


class WorkbookLayoutError(ValueError):
    """Input lies outside the declared narrow workbook layout."""


class WorkbookFormatError(ValueError):
    """Captured workbook XML is not well formed."""


class WorkbookReadLimit(RuntimeError):
    """Operational stop, kept distinct from openpyxl's ValueError format wrapper."""
    def __init__(self, expanded_bytes, limit):
        super().__init__('workbook actual expanded read limit')
        self.expanded_bytes = expanded_bytes
        self.limit = limit


class WorkbookCellLimit(RuntimeError):
    def __init__(self, cells_seen):
        super().__init__('workbook cell limit')
        self.cells_seen = cells_seen


class WorkbookMemberLimit(RuntimeError):
    def __init__(self, members_seen):
        super().__init__('workbook member limit')
        self.members_seen = members_seen


class WorkbookExpansionRatioLimit(RuntimeError):
    def __init__(self, expanded_bytes, compressed_bytes):
        super().__init__('workbook expansion ratio limit')
        self.expanded_bytes = expanded_bytes
        self.compressed_bytes = compressed_bytes


class WorkbookMetadataExpansionLimit(RuntimeError):
    def __init__(self, expanded_bytes):
        super().__init__('workbook metadata expansion limit')
        self.expanded_bytes = expanded_bytes


class ExpandedRead(RawIOBase):
    def __init__(self, archive, member):
        self.archive = archive
        self.member = member

    def readable(self):
        return True

    def read(self, size=-1):
        if size == 0:
            return b''
        chunks = []
        while size < 0 or size > 0:
            remaining = self.archive.limit - self.archive.expanded_bytes
            if remaining <= 0:
                raise WorkbookReadLimit(self.archive.expanded_bytes, self.archive.limit)
            count = min(8192, remaining, size if size >= 0 else 8192)
            data = self.member.read(count)
            self.archive.expanded_bytes += len(data)
            if not data:
                break
            chunks.append(data)
            if size >= 0:
                size -= len(data)
        return b''.join(chunks)

    def readinto(self, target):
        data = self.read(len(target))
        target[:len(data)] = data
        return len(data)

    def close(self):
        self.member.close()
        super().close()


class BoundedArchive:
    def __init__(self, archive):
        self.archive = archive
        self.filename = '<captured-workbook>'
        self.expanded_bytes = 0
        members = archive.infolist()
        if len(members) > 64:
            raise WorkbookMemberLimit(len(members))
        if (len({row.filename for row in members}) != len(members) or
                any(row.flag_bits & 1 for row in members)):
            raise WorkbookLayoutError('workbook archive member limit or unsupported member')
        compressed = sum(row.compress_size for row in members)
        self.limit = min(EXPANDED_LIMIT, 100 * compressed)
        for row in members:
            if row.file_size > 100 * row.compress_size:
                raise WorkbookExpansionRatioLimit(row.file_size, row.compress_size)
        metadata_expanded = sum(row.file_size for row in members)
        if metadata_expanded > EXPANDED_LIMIT:
            raise WorkbookMetadataExpansionLimit(metadata_expanded)
        if not compressed:
            raise WorkbookLayoutError('workbook expansion metadata limit')

    def namelist(self):
        return self.archive.namelist()

    def open(self, name, mode='r', pwd=None):
        if mode != 'r':
            raise ValueError('read-only captured workbook')
        return ExpandedRead(self, self.archive.open(name, mode, pwd))

    def read(self, name, pwd=None):
        with self.open(name, pwd=pwd) as stream:
            return stream.read()

    def close(self):
        self.archive.close()


def workbook_rows(content, source):
    from openpyxl.reader.excel import ExcelReader
    # Construction opens ZIP metadata only. No openpyxl expanded read occurs
    # until the bounded instance replaces its archive and reader.read() begins.
    reader = ExcelReader(BytesIO(content), read_only=False, keep_vba=False,
                         data_only=False, keep_links=False)
    original = reader.archive
    try:
        archive = BoundedArchive(original)
        reader.archive = archive
        workbook_xml = ElementTree.fromstring(archive.read('xl/workbook.xml'))
        sheets = workbook_xml.find(MAIN + 'sheets')
        if (sheets is None or len(sheets) != 1 or sheets[0].get('name') != 'Properties' or
                sheets[0].get('state', 'visible') != 'visible'):
            raise WorkbookLayoutError('single visible Properties sheet required')
        relationships = ElementTree.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        sheet_id = sheets[0].get(REL + 'id')
        target, = [row.get('Target') for row in relationships if row.get('Id') == sheet_id]
        worksheet_path = (target.lstrip('/') if target.startswith('/') else
                          posixpath.normpath(posixpath.join('xl', target)))
        if not worksheet_path.startswith('xl/worksheets/'):
            raise WorkbookLayoutError('unsupported worksheet relationship')
        sheet_xml = ElementTree.fromstring(archive.read(worksheet_path))
        raw_cells = sheet_xml.findall('.//' + MAIN + 'c')
        coordinates = [row.get('r', '') for row in raw_cells]
        if len(raw_cells) > 100:
            raise WorkbookCellLimit(len(raw_cells))
        if (len(set(coordinates)) != len(coordinates) or
                any(not re.fullmatch(r'[A-C][1-9][0-9]*', cell) for cell in coordinates) or
                any(int(cell[1:]) * 3 > 100 for cell in coordinates) or
                sheet_xml.find(MAIN + 'mergeCells') is not None or
                sheet_xml.find('.//' + MAIN + 'f') is not None or
                any(row.get('hidden') in ('1', 'true')
                    for row in sheet_xml.findall('.//' + MAIN + 'row') +
                    sheet_xml.findall('.//' + MAIN + 'col'))):
            raise WorkbookLayoutError('unsupported workbook cells or layout limit')
        tokens = {row.get('r'): row.findtext(MAIN + 'v') for row in raw_cells}
        reader.read()
        sheet = reader.wb['Properties']
        if (sheet.max_row * sheet.max_column > 100 or sheet.max_column != 3 or
                tuple(cell.value for cell in sheet[1]) != ('Property', 'Value', 'Unit')):
            raise WorkbookLayoutError('three-column fixture header required')
        observations = []
        for row_number, row in enumerate(sheet.iter_rows(min_row=2, max_col=3), 2):
            label, value, unit = row
            if not isinstance(label.value, str):
                raise WorkbookLayoutError('textual property label required')
            cells = {}
            for cell in row:
                original_value = cell.value
                if cell.data_type == 'n' and original_value is not None:
                    token = tokens[cell.coordinate]
                    original_value = int(token) if re.fullmatch(r'[0-9]+', token or '') else token
                cells[cell.coordinate] = dict(value=original_value,
                    type='blank' if original_value is None else cell.data_type)
            numeric = value.data_type == 'n' and value.value is not None
            original_value = cells[value.coordinate]['value']
            raw_value = (tokens[value.coordinate] if numeric else
                         value.value if isinstance(value.value, str) else None)
            # IDs are assigned by field key and source-region order by the
            # shared fixture rules; source coordinates are original operands.
            from nilaris.fixture_processing import RULES, normalize_label
            rule = RULES.get(normalize_label(label.value))
            key = rule.field_key if rule else 'unresolved'
            observation = dict(id=f'observation:{key}:{row_number - 2}',
                label=label.value, kind='property', original_content=cells,
                original_cells=cells, original_value=original_value,
                value=raw_value, unit=unit.value,
                source_type='blank' if value.value is None else 'number' if numeric else 'text',
                presence='blank' if value.value is None else 'observed',
                locator=dict(sheet='Properties', row=row_number, range=f'A{row_number}:C{row_number}'),
                content_pin=source['content_pin'], document_id=source['document_id'],
                source_revision=source['source_revision'])
            if numeric:
                observation['numeric_xml_token'] = tokens[value.coordinate]
            observations.append(observation)
        return observations, [f'Properties:A1:C{sheet.max_row}',
                              f'archive-expanded-bytes:{archive.expanded_bytes}']
    except ElementTree.ParseError as error:
        raise WorkbookFormatError('malformed workbook XML') from error
    finally:
        original.close()
