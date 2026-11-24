"""Bind each recorded local test response to its capture and original source row."""
from dataclasses import dataclass
from uuid import uuid4

from nilaris.rfq_request import inspect_request


# These are authored test responses, independently of the request and verdict.
TEST_RESPONSES = (
    ('Rated selection', 'Rated flow rate', 'Flow-rated'),
    ('Normal selection', 'Normal flow rate', 'Flow-normal'),
    ('Missing qualifier', 'Flow', 'Flow-rated'),
    ('Wrong row', 'Rated flow rate', 'Flow-normal'),
)
QUALIFIED_LABELS = {'Rated flow rate': 'Flow-rated', 'Normal flow rate': 'Flow-normal'}


@dataclass
class FixtureRun:
    record: dict
    evidence: dict
    marker: bytes
    inspection: dict
    edited_name: str | None = None

    def proposal(self):
        initial_name = self.record['response']['name']
        origin = 'Recorded local test response for this run.'
        display_name = self.edited_name or initial_name
        return {'name': display_name, 'initial_name': initial_name, 'initial_provenance': origin,
                'provenance': origin if display_name == initial_name else
                'The Reviewer edited this Attribute name in the current local session.',
                'initial_record': self.record}


def record_fixture_runs(bundle):
    manifest = bundle.manifest
    rows = {
        manifest['attribute']: (manifest['passage'], manifest['bounds']),
        manifest['comparison_attribute']: (manifest['qualifier_comparison'], manifest['comparison_bounds']),
    }
    if list(rows) != ['Flow-rated', 'Flow-normal']:
        raise ValueError('Verified evidence for the two approved labels is required.')
    for number, (label, (passage, _)) in enumerate(rows.items(), start=1):
        if passage.split('\t')[:2] != [str(number), label]:
            raise ValueError('Response evidence differs from the approved source row.')
    runs = {}
    for title, name, label in TEST_RESPONSES:
        run_id = uuid4().hex
        inspection = inspect_request(manifest)
        retained = inspection['comparison']['retained']['attributes']
        if {item['label'] for item in retained} != rows.keys():
            raise ValueError('The request receipt and the original source rows disagree.')
        evidence, marker = evidence_location(manifest, run_id, rows[label])
        response = {'name': name, 'selected_label': label}
        expected = QUALIFIED_LABELS.get(name)
        verdict = 'missing qualifier' if expected is None else ('pass' if expected == label else 'wrong row')
        record = {'run_id': run_id, 'fixture': title, 'response_kind': 'Local test response',
                  'request_body_sha256': inspection['request']['body_sha256'], 'response': response,
                  'source_revision': manifest['sha256'], 'source_row': evidence['row'], 'verdict': verdict}
        runs[run_id] = FixtureRun(record, evidence, marker, inspection)
    return runs


def evidence_location(manifest, run_id, source_row):
    passage, bounds = source_row
    row = int(passage.split('\t')[0])
    prefix = f"/rfq/runs/{run_id}/source/{manifest['sha256']}/row-{row}"
    source, marker = source_location(manifest, prefix, bounds, row=row)
    other = manifest['qualifier_comparison'] if row == 1 else manifest['passage']
    source.update(passage=passage, qualifier_comparison=other)
    return source, marker


def source_location(manifest, prefix, bounds, *, row=None, note=None):
    x, y, width, height = bounds
    # Chrome's PDF zoom fragment takes offsets in unzoomed CSS pixels.
    # The manifest records the row from the top of a page measured in PDF points.
    top_css_px = y * manifest['page_size'][1] * 96 / 72
    source = {key: manifest[key] for key in ('filename', 'sha256', 'page', 'section')}
    left_css_px = '0'
    if note is None:
        source['row'] = row
    else:
        source['note'] = note
        left_css_px = f"{x * manifest['page_size'][0] * 96 / 72:.2f}"
    source.update(prefix=prefix, source_url=prefix + '/#evidence-row',
                  pdf_url=f"{prefix}/original.pdf#page={manifest['page']}&zoom=200,{left_css_px},{top_css_px:.2f}")
    marker = (f'#evidence-row {{left:{x:.8%};top:{y:.8%};'
              f'width:{width:.8%};height:{height:.8%}}}').encode()
    return source, marker
