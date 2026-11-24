"""Prepared reader logic at the external Source.read seam; NO network/DB/auth."""
import json

from nilaris.drive import PreparedReadOnlyDrive
from test_fixture_extraction import fixture_input


def test_prepared_reader_downloads_only_the_authorized_exact_pdf_revision():
    item, content = fixture_input('P.pdf')
    item.update(connector='drive-readonly-prepared-v1', source_root='synthetic-root-P',
                expected_sha256='4fb883b2ae49d073b37bb649917050da03c16a4e6e86f1cdb6c4ff3bdb336c34')
    requests = []
    credentials = []

    def synthetic_credentials():
        credentials.append('synthetic-supplier-called')
        return 'synthetic-access-token'

    class SyntheticDriveHTTP:
        """Only this external provider transport is substituted."""
        def get(self, path, *, token, limit):
            assert token == 'synthetic-access-token'
            requests.append((path, limit))
            if path == '/drive/v3/files/a1-source-P?fields=id,parents,trashed,mimeType,capabilities,shortcutDetails&supportsAllDrives=true':
                return json.dumps(dict(id='a1-source-P', parents=['synthetic-root-P'], trashed=False,
                    mimeType='application/pdf', capabilities=dict(canDownload=True))).encode()
            if path == '/drive/v3/files/a1-source-P/revisions/a1-P-r1?alt=media':
                return content
            raise AssertionError('unexpected provider operation')

    source = PreparedReadOnlyDrive(dict(principal='synthetic-principal', allowed_member=item),
        credential_supplier=synthetic_credentials, transport=SyntheticDriveHTTP(),
        authorization=dict(principal='synthetic-principal', source_root='synthetic-root-P',
                           source_ids=['a1-source-P']))
    assert source.read('a1-source-P', 'a1-P-r1', 1048576) == content
    assert requests == [
        ('/drive/v3/files/a1-source-P?fields=id,parents,trashed,mimeType,capabilities,shortcutDetails&supportsAllDrives=true', 65536),
        ('/drive/v3/files/a1-source-P/revisions/a1-P-r1?alt=media', 1505)]
    assert credentials == ['synthetic-supplier-called']


def test_unapproved_reader_never_asks_for_credentials_or_provider_access():
    item, _ = fixture_input('P.pdf')
    item.update(connector='drive-readonly-prepared-v1', source_root='synthetic-root-P',
                expected_sha256='4fb883b2ae49d073b37bb649917050da03c16a4e6e86f1cdb6c4ff3bdb336c34')
    calls = []

    def forbidden_credentials():
        calls.append('credentials')
        raise AssertionError('credentials must not be requested')

    class ForbiddenHTTP:
        def get(self, *args, **kwargs):
            calls.append('transport')
            raise AssertionError('provider must not be called')

    allowed = dict(principal='synthetic-principal', source_root='synthetic-root-P',
                   source_ids=['a1-source-P'])
    for grant, source_id, revision, limit in [
            (None, 'a1-source-P', 'a1-P-r1', 1048576),
            (dict(allowed, source_root='other-root'), 'a1-source-P', 'a1-P-r1', 1048576),
            (dict(allowed, source_ids=['other-file']), 'a1-source-P', 'a1-P-r1', 1048576),
            (dict(allowed, principal='other-principal'), 'a1-source-P', 'a1-P-r1', 1048576),
            (allowed, 'other-file', 'a1-P-r1', 1048576),
            (allowed, 'a1-source-P', 'a1-P-r2', 1048576),
            (allowed, 'a1-source-P', 'a1-P-r1', 1504)]:
        source = PreparedReadOnlyDrive(dict(principal='synthetic-principal', allowed_member=item),
            credential_supplier=forbidden_credentials, transport=ForbiddenHTTP(), authorization=grant)
        assert source.read(source_id, revision, limit) is None
        assert calls == []


def test_provider_scope_or_pin_mismatch_is_unavailable_without_latest_fallback():
    item, content = fixture_input('P.pdf')
    item.update(connector='drive-readonly-prepared-v1', source_root='synthetic-root-P',
                expected_sha256='4fb883b2ae49d073b37bb649917050da03c16a4e6e86f1cdb6c4ff3bdb336c34')
    grant = dict(principal='synthetic-principal', source_root='synthetic-root-P', source_ids=['a1-source-P'])
    metadata_path = '/drive/v3/files/a1-source-P?fields=id,parents,trashed,mimeType,capabilities,shortcutDetails&supportsAllDrives=true'
    revision_path = '/drive/v3/files/a1-source-P/revisions/a1-P-r1?alt=media'
    for fault in ('outside_root', 'wrong_file', 'changed_hash', 'wrong_size', 'missing_revision'):
        requests = []

        class SyntheticDriveHTTP:
            def get(self, path, *, token, limit):
                requests.append(path)
                if path == metadata_path:
                    return json.dumps(dict(id='other-file' if fault == 'wrong_file' else 'a1-source-P',
                        parents=['other-root' if fault == 'outside_root' else 'synthetic-root-P'],
                        trashed=False, mimeType='application/pdf', capabilities=dict(canDownload=True))).encode()
                assert path == revision_path
                assert limit == 1505
                if fault == 'missing_revision':
                    return None
                if fault == 'changed_hash':
                    return fixture_input('P2.pdf')[1]
                if fault == 'wrong_size':
                    return content + b'x'
                raise AssertionError('out-of-scope metadata must stop before download')

        source = PreparedReadOnlyDrive(dict(principal='synthetic-principal', allowed_member=item),
            credential_supplier=lambda: 'synthetic-access-token', transport=SyntheticDriveHTTP(), authorization=grant)
        assert source.read('a1-source-P', 'a1-P-r1', 1048576) is None
        assert requests == ([metadata_path] if fault in ('outside_root', 'wrong_file')
                            else [metadata_path, revision_path])
