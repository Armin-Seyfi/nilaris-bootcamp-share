"""Local Reviewer for an authorized RFQ Attribute and its pinned source."""
import argparse
import base64
from dataclasses import dataclass
from hashlib import sha256
from http.server import HTTPServer
import json
import math
import re
from pathlib import Path
import secrets
import psycopg
import subprocess
import time

from nilaris.review_http import ReviewHTTPHandler, reviewer_credential
from nilaris.rfq_approval import PumpSchemaReview
from nilaris.rfq_fixture_runs import record_fixture_runs, source_location
from nilaris.rfq_request import printed_unit

ASSETS = Path(__file__).with_name("review_web")
PUBLIC_FILES = {"/": ("rfq_reviewer.html", "text/html; charset=utf-8"),
                "/rfq/app.js": ("rfq_demo.js", "text/javascript"),
                "/rfq/style.css": ("rfq_demo.css", "text/css")}

NOTE_REVISION = (ASSETS / 'rfq_note_revision.txt').read_text(encoding='ascii').strip()
SELECTED_REVISION = (ASSETS / 'rfq_revision.txt').read_text(encoding='ascii').strip()


@dataclass(frozen=True)
class SourceBundle:
    manifest: dict
    pdf: bytes
    page_image: bytes
    note: "SourceBundle | None" = None

    @classmethod
    def load(cls, directory, expected_revision):
        manifest = json.loads((directory / 'manifest.json').read_bytes())
        pdf = (directory / 'original.pdf').read_bytes()
        if sha256(pdf).hexdigest() != expected_revision or manifest['sha256'] != expected_revision:
            raise ValueError('The original PDF does not match the selected revision.')
        page_image = (directory / 'page.png').read_bytes()
        if sha256(page_image).hexdigest() != manifest['image_sha256']:
            raise ValueError('The rendered source page has changed.')
        if 'comparison_bounds' not in manifest:
            raise ValueError('Prepare a new private bundle with both row locations.')
        for bounds in (manifest['bounds'], manifest['comparison_bounds']):
            x, y, width, height = bounds
            if not (all(math.isfinite(v) for v in bounds)
                    and 0 <= x < x + width <= 1 and 0 <= y < y + height <= 1):
                raise ValueError('Source row is outside the page.')
        if manifest['bounds'][1] >= manifest['comparison_bounds'][1]:
            raise ValueError('Source row order differs from the original page.')
        note = None
        if 'note_original' in manifest or (directory / 'note-original.pdf').exists():
            note_pdf = (directory / 'note-original.pdf').read_bytes()
            if sha256(note_pdf).hexdigest() != NOTE_REVISION:
                raise ValueError('The note PDF does not match the selected revision.')
            observed = extract_pdf('--note', note_pdf)
            image = base64.b64decode(observed.pop('image'), validate=True)
            observed.update(filename='1.pdf', sha256=NOTE_REVISION, page=1, section='REMARKS')
            note = cls(observed, note_pdf, image)
        return cls(manifest, pdf, page_image, note)


@dataclass(frozen=True)
class EvidenceAsset:
    location: dict
    marker: bytes
    source: SourceBundle


def extract_pdf(mode, pdf):
    extractor = Path(__file__).resolve().parents[2] / 'tools/rfq/prepare.swift'
    result = subprocess.run(['/usr/bin/swift', str(extractor), mode], input=pdf,
                            stdout=subprocess.PIPE, check=True, timeout=90)
    return json.loads(result.stdout)


def schema_draft(bundle):
    # PDFKit reads the same verified bytes that the source URLs serve.
    proposals = []
    locations = {}
    for row in extract_pdf('--schema', bundle.pdf):
        number = row['number']
        prefix = f"/rfq/schema/source/{bundle.manifest['sha256']}/row-{number}"
        location, marker = source_location(bundle.manifest, prefix, row['bounds'], row=number)
        location['passage'] = row['text']
        locations[prefix] = EvidenceAsset(location, marker, bundle)
        if row['multiline']:
            reason = 'Multiple lines or nested columns require interpretation.'
        elif not row['label']:
            reason = 'No readable original label.'
        elif not row['unit']:
            reason = 'A distinct measurement unit could not be identified.'
        else:
            reason = None
        proposals.append(dict(row=number, attribute=None if reason else row['label'],
                              value=None if row['multiline'] else row['value'],
                              unit=None if row['multiline'] else printed_unit(row['unit']),
                              unresolved_reason=reason, source_url=location['source_url']))
        if number == 5 and bundle.note is not None:
            note = bundle.note.manifest
            note_prefix = f"/rfq/schema/source/{note['sha256']}/note-7"
            note_location, note_marker = source_location(note, note_prefix, note['bounds'], note=7)
            note_location.update(passage=note['passage'], context=note['context'])
            locations[note_prefix] = EvidenceAsset(note_location, note_marker, bundle.note)
            initial = dict(attribute=row['label'], expression=note['expression'],
                           evidence=dict(table=location, note=note_location))
            proposals[-1]['alias'] = dict(initial=initial, decision=None)
    return proposals, locations


def reviewer_server(bundle, runs, credential, *, port, schema=None, review_config=None):
    """Open one loopback Reviewer workspace over an immutable local source snapshot."""
    def fixture_runs():
        nonlocal runs
        if runs is None:
            runs = record_fixture_runs(bundle)
        return runs

    rows, locations = schema if schema is not None else ([], {})

    alias_row = next((row for row in rows if 'alias' in row), None)
    review = None
    if review_config:
        from nilaris.rfq_publication import seed_pump_review
        scope = f"pump-schema-v1/{bundle.manifest['sha256']}/{bundle.note.manifest['sha256']}"
        job_id, proposal_id = seed_pump_review(review_config, bundle, (rows, locations), scope=scope)
        review = PumpSchemaReview(review_config, job_id, proposal_id, alias_row['row'])

    class Handler(ReviewHTTPHandler):
        cookie_name = 'nilaris_rfq_reviewer'
        session_token = secrets.token_urlsafe(32)

        @property
        def expected_host(self):
            return f'127.0.0.1:{self.server.server_port}'

        def run_route(self):
            if self.path.startswith('/rfq/runs/'):
                run_id, _, route = self.path[len('/rfq/runs/'):].partition('/')
                return fixture_runs().get(run_id), route
            return None, ''

        def do_GET(self):
            if not self.host_valid():
                return self.reply(403, {'category': 'invalid_host'})
            path = self.path.partition('?')[0]
            if path in PUBLIC_FILES:
                filename, mime = PUBLIC_FILES[path]
                return self.reply(200, (ASSETS / filename).read_bytes(), mime)
            if not self.authenticated():
                return self.reply(401, {'category': 'unauthorized'})
            if self.path == '/rfq/runs':
                return self.reply(200, [{'run_id': key, 'fixture': run.record['fixture']}
                                        for key, run in fixture_runs().items()])
            try:
                if self.path in ('/rfq/schema', '/rfq/schema/review'):
                    view = review.state() if review else dict(
                        draft_id=None, revision=0, rows=rows, definitions=[], corrections=[],
                        versions=[], approved_version=None)
                    return self.reply(200, view['rows'] if self.path == '/rfq/schema' else view)
                if review:
                    history = re.fullmatch(r'/rfq/schema/history/([0-9a-f-]{36})/([0-9]{1,18})', self.path)
                    if history:
                        draft, through = history.groups()
                        return self.reply(200, review.corrections(draft, int(through)))
                    older = re.fullmatch(r'/rfq/schema/versions-before/([0-9]{1,18})', self.path)
                    if older:
                        return self.reply(200, review.versions(int(older[1])))
                if self.path.startswith('/rfq/schema/versions/'):
                    version = review.reopen(self.path.rsplit('/', 1)[1]) if review else None
                    return self.reply(200, version) if version else self.reply(404, {'category': 'unknown_version'})
            except (OSError, psycopg.Error):
                return self.reply(503, {'category': 'version_storage_unavailable'})
            prefix, _, asset = self.path.rpartition('/')
            if prefix in locations:
                return self.source_asset(asset, locations[prefix])
            run, route = self.run_route()
            if run is None:
                return self.reply(404, {'category': 'not_found'})
            if route == 'proposal':
                return self.reply(200, run.proposal())
            if route == 'evidence':
                return self.reply(200, run.evidence)
            if route == 'inspection':
                return self.reply(200, dict(run.inspection, run_id=run.record['run_id']))
            prefix = run.evidence['prefix']
            if self.path.startswith(prefix + '/'):
                return self.source_asset(self.path[len(prefix) + 1:], EvidenceAsset(run.evidence, run.marker, bundle))
            self.reply(404, {'category': 'not_found'})

        def source_asset(self, asset, evidence):
            if asset == '':
                return self.reply(200, (ASSETS / 'rfq_source.html').read_bytes(), 'text/html; charset=utf-8')
            if asset == 'original.pdf':
                return self.reply(200, evidence.source.pdf, 'application/pdf')
            if asset == 'page.png':
                return self.reply(200, evidence.source.page_image, 'image/png')
            if asset == 'location.json':
                return self.reply(200, evidence.location)
            if asset == 'mark.css':
                return self.reply(200, evidence.marker, 'text/css')
            self.reply(404, {'category': 'not_found'})

        def reply_to_review_command(self, command, *args):
            try:
                receipt = command(*args)
            except (OSError, psycopg.Error):
                return self.reply(503, {'category': 'version_storage_unavailable'})
            return self.reply(200 if receipt['saved'] else 409, receipt)

        def do_POST(self):
            body = self.same_origin_json()
            if body is None:
                return
            if self.path == '/rfq/session':
                return self.login(body, credential)
            if not self.authenticated():
                return self.reply(401, {'category': 'unauthorized'})
            if self.path == '/rfq/schema/alias' and alias_row is not None:
                name = body.get('name')
                decision = body.get('decision')
                fields = {'name', 'decision', 'draft_id', 'revision'} if review else {'name', 'decision'}
                if (set(body) != fields or decision not in ('confirm', 'separate')
                        or (review and (not isinstance(body['draft_id'], str) or type(body['revision']) is not int))
                        or not isinstance(name, str) or not 1 <= len(name.strip()) <= 160):
                    return self.reply(400, {'category': 'invalid_alias_review'})
                if review:
                    return self.reply_to_review_command(
                        review.correct, name.strip(), decision, body['draft_id'], body['revision'])
                alias_row['attribute'] = name.strip()
                alias_row['alias']['decision'] = decision
                return self.reply(200, alias_row)
            if self.path == '/rfq/schema/approve':
                if not review:
                    return self.reply(404, {'category': 'approval_not_configured'})
                if (set(body) != {'draft_id', 'revision', 'explicit'} or type(body['revision']) is not int
                        or not isinstance(body['draft_id'], str) or body['explicit'] is not True):
                    return self.reply(400, {'category': 'explicit_whole_schema_approval_required'})
                return self.reply_to_review_command(review.approve, body)
            run, route = self.run_route()
            if run is None or route != 'proposal':
                return self.reply(404, {'category': 'not_found'})
            name = body.get('name')
            if set(body) != {'name'} or not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
                return self.reply(400, {'category': 'invalid_attribute_name'})
            run.edited_name = name.strip()
            self.reply(200, run.proposal())

    return HTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18768)
    parser.add_argument('--seconds', type=int, default=1800, choices=range(1, 7201), metavar='1..7200')
    parser.add_argument('--review-config', type=Path)
    args = parser.parse_args()
    bundle = SourceBundle.load(args.bundle, SELECTED_REVISION)
    schema = schema_draft(bundle)
    config = None
    if args.review_config:
        from nilaris.rfq_runtime import PumpReviewConfig
        config = PumpReviewConfig.load(args.review_config)
        if bundle.note is None:
            parser.error('Whole-Schema approval requires both selected PDFs.')
    with reviewer_credential(args.bundle) as key:
        with reviewer_server(bundle, None, key, port=args.port, schema=schema, review_config=config) as server:
            deadline = time.monotonic() + args.seconds
            server.timeout = .25
            print(f'Reviewer ready at http://127.0.0.1:{server.server_port}/', flush=True)
            while time.monotonic() < deadline:
                server.handle_request()


if __name__ == '__main__':
    main()
