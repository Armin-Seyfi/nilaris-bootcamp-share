"""Author only the approved A1 synthetic inputs; never load test expectations.

Run via the NILARIS interpreter and runtime/a1/with_overlay.py. This is fixture
authoring, not a production parser or acceptance-test interface.
"""
import argparse
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook
from reportlab.pdfgen.canvas import Canvas


def pdf(flow):
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(612, 792), invariant=1, pageCompression=0)
    canvas.setTitle('Synthetic A1 fixture')
    canvas.setAuthor('NILARIS synthetic fixture author')
    canvas.setFont('Helvetica', 12)
    for index, line in enumerate((
        f'Flow rate: {flow} m3/h', 'Pressure: 6 bar',
        'Material: Cast iron', 'Delivery: Six weeks',
    )):
        canvas.drawString(72, 720 - index * 24, line)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def workbook():
    book = Workbook()
    book.properties.creator = 'NILARIS synthetic fixture author'
    book.properties.created = datetime(2000, 1, 1)
    sheet = book.active
    sheet.title = 'Properties'
    for row in (
        ('Property', 'Value', 'Unit'),
        ('Flow rate', 12, 'm3/h'),
        ('Pressure', 6, 'bar'),
        ('Material', 'Cast iron', None),
        ('Delivery', 'Six weeks', None),
    ):
        sheet.append(row)
    original = BytesIO()
    book.save(original)
    book.close()
    # Freeze ZIP timestamps and openpyxl's save-time modified property so that
    # rerunning this authoring tool yields identical source pins.
    normalized = BytesIO()
    with ZipFile(original) as source, ZipFile(normalized, 'w') as target:
        for name in sorted(source.namelist()):
            data = source.read(name)
            if name == 'docProps/core.xml':
                core = ElementTree.fromstring(data)
                core.find('{http://purl.org/dc/terms/}modified').text = '2000-01-01T00:00:00Z'
                data = ElementTree.tostring(core, encoding='utf-8')
            member = ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            member.compress_type = ZIP_DEFLATED
            member.external_attr = 0o600 << 16
            target.writestr(member, data, compresslevel=9)
    return normalized.getvalue()


def prepare(destination):
    p = pdf('12')
    artifacts = {
        'P.pdf': p,
        'W.xlsx': workbook(),
        'P2.pdf': pdf('13'),
        'delivery-renamed.pdf': p,
        'ambiguous-copy.pdf': p,
    }
    deliveries = []
    for key, filename, source_id, document, revision, identity in (
        ('P', 'P.pdf', 'a1-source-P', 'a1-doc-P', 'a1-P-r1', 'resolved'),
        ('W', 'W.xlsx', 'a1-source-W', 'a1-doc-W', 'a1-W-r1', 'resolved'),
        ('P2', 'P2.pdf', 'a1-source-P', 'a1-doc-P', 'a1-P-r2', 'resolved'),
        ('renamed-P', 'delivery-renamed.pdf', 'a1-source-P', 'a1-doc-P', 'a1-P-r1', 'resolved'),
        ('ambiguous-P', 'ambiguous-copy.pdf', 'a1-source-ambiguous', None, 'a1-ambiguous-r1', 'ambiguous'),
    ):
        content = artifacts[filename]
        deliveries.append(dict(
            delivery_key=f'a1-delivery-{key}', artifact=filename,
            source_id=source_id, source_revision=revision,
            document_id=document, identity=identity, intent_id='ordinary',
            input_bytes=len(content), sha256=sha256(content).hexdigest(),
            media_type=('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                        if filename.endswith('.xlsx') else 'application/pdf'),
        ))
    manifest = dict(
        manifest_version='a1-exact-fixture-1',
        provenance='New bounded synthetic inputs; no historical/customer/private content.',
        context=dict(case_id='a1-case-fixture', membership_revision='a1-manifest-1',
                     nominated_product_type='Fixture pump', origin='supplied',
                     classification='not_discovered'),
        processing_contract='fixture-v1', reference_set='none', selection_policy='none',
        deliveries=deliveries,
    )
    artifacts['manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    if sum(map(len, artifacts.values())) > 1024 ** 2:
        raise ValueError('synthetic fixture authoring byte budget exceeded before writes')
    destination.mkdir(parents=True, exist_ok=True)
    collisions = set(path.name for path in destination.iterdir()) - artifacts.keys()
    if collisions:
        raise ValueError('destination contains files outside this fixture tool')
    for name, content in artifacts.items():
        (destination / name).write_bytes(content)
        print(f'{sha256(content).hexdigest()}  {len(content):5d}  {name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[2] / 'tests/fixtures/a1')
    prepare(parser.parse_args().output)
