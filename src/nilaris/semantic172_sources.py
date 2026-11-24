"""Public Semantic139 literal capture, isolated from all historical ledgers."""
import base64
import hashlib
from io import BytesIO
import zipfile
from nilaris.semantic_contract import canonical

def capture_sources(sources):
    """Capture selectable PDF layout and literal workbook cells, without rules."""
    if type(sources) is not list or len(sources) != 2:
        raise ValueError('exactly two family documents required')
    docs = []
    for source in sources:
        if set(source) != {'document_id', 'source_revision', 'content_pin', 'media_type', 'content'}:
            raise ValueError('source disclosure fields')
        raw = base64.b64decode(source['content'], validate=True)
        if len(raw) > 1024**2 or hashlib.sha256(raw).hexdigest() != source['content_pin']:
            raise ValueError('source pin mismatch')
        spans, unread, inspected, layout = [], [], [], {}
        if source['media_type'] == 'application/pdf':
            import pdfplumber
            from pdfminer.pdfpage import PDFPage
            from itertools import islice
            with pdfplumber.open(BytesIO(raw)) as pdf:
                if sum(1 for _ in islice(PDFPage.create_pages(pdf.doc), 3)) > 2:
                    raise ValueError('PDF page cap')
                for number, page in enumerate(pdf.pages, 1):
                    lines = page.extract_text_lines(layout=False, strip=True, return_chars=False)
                    if not lines:
                        unread.append(f'page:{number}:no-selectable-text')
                    inspected.append(f'page:{number}:selectable-lines')
                    for index, line in enumerate(lines, 1):
                        spans.append(dict(locator_id=f'p{number}:s{index}', text=line['text'],
                            locator=dict(page=number, span=index, bbox=[line[k] for k in ('x0','top','x1','bottom')])))
        elif source['media_type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':
            from openpyxl.reader.excel import ExcelReader
            from nilaris.fixture_workbook import BoundedArchive, MAIN, REL
            from xml.etree import ElementTree
            import posixpath
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                if (len(archive.infolist()) > 64 or sum(i.file_size for i in archive.infolist()) > 4*1024**2 or
                        any(i.file_size > max(1, i.compress_size)*100 for i in archive.infolist())):
                    raise ValueError('workbook expansion cap')
            reader = ExcelReader(BytesIO(raw), read_only=False, keep_vba=False,
                                 data_only=False, keep_links=False)
            archive = BoundedArchive(reader.archive)
            reader.archive = archive
            workbook_xml = ElementTree.fromstring(archive.read('xl/workbook.xml'))
            sheets = workbook_xml.find(MAIN+'sheets')
            if sheets is None or len(sheets) > 2:
                raise ValueError('workbook sheet cap')
            relationships = ElementTree.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
            numeric_tokens = {}
            for sheet_info in sheets:
                target, = [r.get('Target') for r in relationships if r.get('Id') == sheet_info.get(REL+'id')]
                member = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl',target))
                if not member.startswith('xl/worksheets/'):
                    raise ValueError('unsupported worksheet relationship')
                sheet_xml = ElementTree.fromstring(archive.read(member))
                if sheet_xml.find('.//'+MAIN+'f') is not None:
                    raise ValueError('formula capture forbidden')
                numeric_tokens[sheet_info.get('name')] = {c.get('r'):c.findtext(MAIN+'v')
                    for c in sheet_xml.findall('.//'+MAIN+'c') if c.get('t','n') in ('n','b')}
            reader.read()
            workbook = reader.wb
            count = 0
            for sheet in workbook.worksheets:
                if sheet.max_row * sheet.max_column > 10000:
                    raise ValueError('workbook extent cap')
                merges = [str(m) for m in sheet.merged_cells.ranges]
                layout[sheet.title] = dict(merges=merges)
                inspected.append(f'{sheet.title}:{sheet.calculate_dimension()}')
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value is not None:
                            count += 1
                        if count > 100 or cell.data_type == 'f':
                            raise ValueError('workbook cell/formula cap')
                        text = numeric_tokens[sheet.title].get(cell.coordinate)
                        if text is None and cell.value is not None:
                            text = str(cell.value)
                        spans.append(dict(locator_id=f'{sheet.title}!{cell.coordinate}', text=text,
                            **({'cell_type':cell.data_type} if cell.data_type in ('n','b') and cell.value is not None else {}),
                            locator=dict(sheet=sheet.title, cell=cell.coordinate)))
            workbook.close()
            archive.close()
        else:
            raise ValueError('unsupported source media')
        docs.append(dict(document_id=source['document_id'], source_revision=source['source_revision'],
            content_pin=source['content_pin'], media_type=source['media_type'], spans=spans,
            layout=layout, coverage=dict(inspected=inspected, unread=unread)))
    if len({d['document_id'] for d in docs}) != 2:
        raise ValueError('duplicate delivery identity')
    result = dict(documents=docs)
    if len(canonical(result)) > 1024**2:
        raise ValueError('capture cap')
    return result
