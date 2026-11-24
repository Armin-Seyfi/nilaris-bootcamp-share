from contextlib import closing
import hashlib
import json
import os

import pytest

from nilaris.rfq_demo import SourceBundle, reviewer_credential


EXPECTED_SELECTIONS = [
    ('Rated flow rate', 1, 'pass'), ('Normal flow rate', 2, 'pass'),
    ('Flow', 1, 'missing qualifier'), ('Rated flow rate', 2, 'wrong row'),
]


def sample(directory):
    pdf = b'%PDF-1.4\nsynthetic fixture'
    png = b'synthetic raster'
    manifest = dict(
        sha256=hashlib.sha256(pdf).hexdigest(), image_sha256=hashlib.sha256(png).hexdigest(),
        filename='test.pdf', page=1, row=1, section='TEST TABLE',
        passage='1\tFlow-rated\t70\tbar', qualifier_comparison='2\tFlow-normal\t45\tbar',
        attribute='Flow-rated', comparison_attribute='Flow-normal', unit='bar',
        unrelated_passage='Synthetic freight note',
        page_text='TEST TABLE\n1\tFlow-rated\t70\tbar\n2\tFlow-normal\t45\tbar\nSynthetic freight note',
        page_size=[600, 800], bounds=[.1, .2, .6, .04], comparison_bounds=[.1, .25, .6, .04], original='original.pdf', image='page.png')
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (directory / 'original.pdf').write_bytes(pdf)
    (directory / 'page.png').write_bytes(png)
    return manifest


def test_revision_and_render_must_match_before_serving(tmp_path):
    manifest = sample(tmp_path)
    bundle = SourceBundle.load(tmp_path, manifest['sha256'])
    assert bundle.pdf.startswith(b'%PDF')
    (tmp_path / 'page.png').write_bytes(b'wrong page')
    with pytest.raises(ValueError, match='rendered'):
        SourceBundle.load(tmp_path, manifest['sha256'])
    (tmp_path / 'original.pdf').write_bytes(b'another revision')
    with pytest.raises(ValueError, match='revision'):
        SourceBundle.load(tmp_path, manifest['sha256'])


def test_reviewer_requires_authority_to_open_original_and_edit_without_evidence(tmp_path):
    import http.client
    import threading
    from nilaris.rfq_demo import record_fixture_runs, reviewer_server
    manifest = sample(tmp_path)
    bundle = SourceBundle.load(tmp_path, manifest['sha256'])
    runs = record_fixture_runs(bundle)
    with reviewer_server(bundle, runs, 'local-key', port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            def send(path, body=None, cookie=None, extra_headers=None):
                headers = {'Content-Type': 'application/json', 'Origin': f'http://{host}:{port}'}
                headers.update(extra_headers or {})
                if cookie:
                    headers['Cookie'] = cookie
                with closing(http.client.HTTPConnection(host, port, timeout=2)) as client:
                    client.request('POST' if body is not None else 'GET', path,
                                   json.dumps(body) if body is not None else None, headers)
                    result = client.getresponse()
                    return result.status, result.read(), result.getheader('Set-Cookie')

            root = '/rfq/'
            assert send(root + 'source/' + manifest['sha256'] + '/original.pdf')[0] == 401
            assert send(root + 'session', {'token': 'local-key'}, extra_headers={'Transfer-Encoding': ''})[0] == 400
            status, _, cookie = send(root + 'session', {'token': 'local-key'})
            assert status == 200
            cookie = cookie.split(';')[0]
            status, choices, _ = send(root + 'runs', cookie=cookie)
            assert status == 200
            choices = json.loads(choices)
            assert [choice['fixture'] for choice in choices] == [
                'Rated selection', 'Normal selection', 'Missing qualifier', 'Wrong row']
            for choice, expected in zip(choices, EXPECTED_SELECTIONS, strict=True):
                run_id = choice['run_id']
                routes = root + 'runs/' + run_id + '/'
                proposal = json.loads(send(routes + 'proposal', cookie=cookie)[1])
                record = proposal['initial_record']
                assert (proposal['name'], record['source_row'], record['verdict']) == expected
                assert 'passage' not in proposal
                inspection = json.loads(send(routes + 'inspection', cookie=cookie)[1])
                assert inspection['run_id'] == record['run_id'] == run_id
                assert inspection['request']['body_sha256'] == record['request_body_sha256']
                evidence = json.loads(send(routes + 'evidence', cookie=cookie)[1])
                row1 = expected[1] == 1
                assert evidence['passage'] == manifest['passage' if row1 else 'qualifier_comparison']
                top_css_px = '213.33' if row1 else '266.67'
                pdf_url, anchor = evidence['pdf_url'].split('#')
                assert anchor == 'page=1&zoom=200,0,' + top_css_px
                assert send(evidence['source_url'].split('#')[0], cookie=cookie)[0] == 200
                assert send(pdf_url, cookie=cookie)[1] == bundle.pdf
                assert send(pdf_url)[0] == 401
                assert send(evidence['prefix'] + '/page.png', cookie=cookie)[1] == bundle.page_image
                wrong_pdf = pdf_url.replace('/row-1/', '/row-2/') if row1 else pdf_url.replace('/row-2/', '/row-1/')
                assert send(wrong_pdf, cookie=cookie)[0] == 404
                correction = 'Corrected Attribute'
                status, saved, _ = send(routes + 'proposal', {'name': correction}, cookie)
                assert status == 200 and 'Reviewer edited' in json.loads(saved)['provenance']
                reopened = json.loads(send(routes + 'proposal', cookie=cookie)[1])
                assert reopened['name'] == correction and reopened['initial_record'] == record
                assert json.loads(send(routes + 'evidence', cookie=cookie)[1]) == evidence
        finally:
            server.shutdown()
            thread.join()


def test_credential_recovery_is_exclusive_and_preserves_replacements(tmp_path):
    keyfile = tmp_path / 'reviewer.key'
    keyfile.write_text('stale key from interrupted session')
    keyfile.chmod(0o600)
    with reviewer_credential(tmp_path) as first:
        assert keyfile.read_text().strip() == first
        with pytest.raises(BlockingIOError), reviewer_credential(tmp_path):
            pytest.fail('a second Reviewer acquired a live bundle')
        assert keyfile.read_text().strip() == first
        replacement = tmp_path / 'replacement'
        replacement.write_text('another owner')
        replacement.chmod(0o644)
        replacement.replace(keyfile)
    assert keyfile.read_text() == 'another owner'
    with pytest.raises(ValueError, match='reconciliation'), reviewer_credential(tmp_path):
        pytest.fail('a non-private replacement was overwritten')


def test_credential_write_failure_releases_ownership(tmp_path, monkeypatch):
    def fail_write(fd, data):
        raise OSError('write failed')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'write', fail_write)
        with pytest.raises(OSError, match='write failed'), reviewer_credential(tmp_path):
            pytest.fail('a session opened without a complete key')
    assert not (tmp_path / 'reviewer.key').exists()
    with reviewer_credential(tmp_path) as key:
        assert (tmp_path / 'reviewer.key').read_text().strip() == key
    assert not (tmp_path / 'reviewer.key').exists()


def test_key_cleanup_follows_the_locked_bundle_if_renamed(tmp_path):
    bundle = tmp_path / 'bundle'
    moved = tmp_path / 'moved'
    bundle.mkdir(mode=0o700)
    with reviewer_credential(bundle):
        bundle.rename(moved)
        bundle.mkdir(mode=0o700)
        (bundle / 'reviewer.key').write_text('separate session')
    assert not (moved / 'reviewer.key').exists()
    assert (bundle / 'reviewer.key').read_text() == 'separate session'


def test_each_local_fixture_has_its_own_recorded_response_and_initial_evidence(tmp_path):
    from nilaris.rfq_demo import record_fixture_runs
    manifest = sample(tmp_path)
    runs = record_fixture_runs(SourceBundle.load(tmp_path, manifest['sha256']))
    assert len(runs) == 4
    observations = []
    for run_id, run in runs.items():
        receipt = run.record
        response = receipt['response']
        assert receipt['run_id'] == run_id
        assert receipt['request_body_sha256'] == run.inspection['request']['body_sha256']
        assert receipt['source_revision'] == manifest['sha256']
        assert response['selected_label'] == run.evidence['passage'].split('\t')[1]
        observations.append((response['name'], run.evidence['row'], receipt['verdict']))
    assert observations == EXPECTED_SELECTIONS


def test_fixture_runs_reject_swapped_source_row_numbers(tmp_path):
    from nilaris.rfq_demo import record_fixture_runs
    manifest = sample(tmp_path)
    manifest['qualifier_comparison'] = manifest['qualifier_comparison'].replace('2\t', '1\t', 1)
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='source row'):
        record_fixture_runs(SourceBundle.load(tmp_path, manifest['sha256']))
