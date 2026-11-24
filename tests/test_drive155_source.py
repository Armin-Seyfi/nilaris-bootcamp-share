"""Approved source/auth boundary, synthetic keys and transports, no network/DB."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from nilaris.drive155_source import SourceBinding, binding_digest


FIXTURES = Path(__file__).parent / 'fixtures/a1'
PDF = (FIXTURES / 'P.pdf').read_bytes()


def configuration():
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    delivery = next(d for d in manifest['deliveries'] if d['artifact'] == 'P.pdf')
    context = manifest['context']
    item = {key: delivery[key] for key in ('source_id', 'source_revision', 'document_id',
             'identity', 'media_type', 'input_bytes', 'intent_id')}
    item.update(member_key=delivery['delivery_key'], connector='drive-readonly-v1',
                source_root='synthetic-root', expected_sha256=hashlib.sha256(PDF).hexdigest(),
                case_id=context['case_id'], membership_revision=context['membership_revision'],
                product_type=context['nominated_product_type'],
                processing_contract=manifest['processing_contract'], reference_set=manifest['reference_set'],
                selection_policy=manifest['selection_policy'])
    return dict(version=1, binding_id='synthetic-binding', customer_id='synthetic-customer',
                credential_binding_id='synthetic-credential', source_root='synthetic-root',
                principal=dict(email='synthetic@example.invalid', cloud_unique_id='987654', oauth_client_id='123456',
                    drive_permission_id='opaque-permission', project_id='synthetic-project'),
                allowed_member=item)


def authority(config):
    return dict(version=1, authority_id='synthetic-owner-grant', binding_digest=binding_digest(config))


def test_binding_requires_independent_authority_and_preserves_immutable_namespace():
    config = configuration()
    grant = authority(config)
    bound = SourceBinding(config, grant)
    assert bound.namespace == SourceBinding(copy.deepcopy(config), dict(grant)).namespace
    for change in ('customer_id', 'credential_binding_id', 'source_root', 'principal', 'allowed_member'):
        altered = copy.deepcopy(config)
        if change == 'principal':
            altered[change]['drive_permission_id'] = 'other-permission'
        elif change == 'allowed_member':
            altered[change]['case_id'] = 'other-case'
        elif change == 'source_root':
            altered[change] = altered['allowed_member']['source_root'] = 'other-root'
        else:
            altered[change] = 'other-binding'
        with pytest.raises(ValueError, match='source binding unavailable'):
            SourceBinding(altered, grant)
        assert SourceBinding(altered, authority(altered)).namespace != bound.namespace
    changed_grant = dict(grant, authority_id='replacement-grant')
    assert SourceBinding(config, changed_grant).namespace != bound.namespace
    member = bound.allowed_member
    member['case_id'] = 'changed'
    config['allowed_member']['case_id'] = 'changed'
    grant['authority_id'] = 'changed'
    assert bound.allowed_member['case_id'] != 'changed'


def test_strict_binding_refuses_unapproved_or_ambiguous_configuration():
    config = configuration()
    for bad in (dict(config, version=True), dict(config, version=2),
                dict(config, access_token='synthetic-secret'),
                dict(config, allowed_member=dict(config['allowed_member'], input_bytes=1504)),
                dict(config, allowed_member=dict(config['allowed_member'], expected_sha256='0' * 64)),
                dict(config, principal=dict(config['principal'], subject='human@example.invalid'))):
        with pytest.raises(ValueError, match='source binding unavailable'):
            SourceBinding(bad, authority(bad))
    for bad in (None, {}, dict(authority(config), confirmed=True)):
        with pytest.raises(ValueError, match='source binding unavailable'):
            SourceBinding(config, bad)


@pytest.fixture(scope='module')
def synthetic_sa():
    # Generated solely for offline JWT signing; never a Google/owner credential.
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return dict(type='service_account', project_id='synthetic-project', private_key_id='synthetic-key',
        private_key=key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(),
        client_email='synthetic@example.invalid', client_id='123456',
        token_uri='https://oauth2.googleapis.com/token')


class TokenResponse:
    status = 200
    headers = {}
    data = b'{"access_token":"synthetic-token","expires_in":3600,"token_type":"Bearer"}'


def test_authentication_signs_exact_readonly_nonimpersonating_claims(synthetic_sa):
    from urllib.parse import parse_qs
    from google.auth import jwt
    from nilaris.drive155_auth import mint_token
    config = configuration()
    requests = []

    def exchange(**request):
        requests.append(request)
        return TokenResponse()

    assert mint_token(synthetic_sa, config['principal'], exchange) == 'synthetic-token'
    assert len(requests) == 1
    request = requests[0]
    assert request['url'] == 'https://oauth2.googleapis.com/token'
    assert request['method'] == 'POST'
    assert request['limit'] == 65536 and request['allow_redirects'] is False
    form = parse_qs(request['body'].decode())
    assert form['grant_type'] == ['urn:ietf:params:oauth:grant-type:jwt-bearer']
    claims = jwt.decode(form['assertion'][0], verify=False)
    assert claims['scope'] == 'https://www.googleapis.com/auth/drive.readonly'
    assert claims['aud'] == 'https://oauth2.googleapis.com/token'
    assert claims['iss'] == 'synthetic@example.invalid'
    assert 'sub' not in claims
    assert 0 < claims['exp'] - claims['iat'] <= 3600


def test_invalid_credential_or_failed_exchange_never_retries_or_exposes_details(synthetic_sa):
    from nilaris.drive155_auth import mint_token, SourceUnavailable
    principal = configuration()['principal']
    calls = []

    def forbidden(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('synthetic-private-provider-error')

    for bad in (dict(synthetic_sa, subject='human@example.invalid'),
                dict(synthetic_sa, type='authorized_user'),
                dict(synthetic_sa, token_uri='https://example.invalid/token'),
                dict(synthetic_sa, client_id='other-id'), dict(synthetic_sa, project_id='other-project'),
                dict(synthetic_sa, client_email='other@example.invalid')):
        with pytest.raises(SourceUnavailable, match='^source unavailable$'):
            mint_token(bad, principal, forbidden)
        assert calls == []
    for status, body in ((500, b'synthetic-private-provider-error'), (302, b''),
                         (200, b'x' * 65537), (200, b'{}'),
                         (200, b'{"access_token":"has whitespace","expires_in":3600}')):
        def failed(**request):
            calls.append('exchange')
            return type('Response', (), dict(status=status, data=body, headers={}))()
        before = len(calls)
        with pytest.raises(SourceUnavailable, match='^source unavailable$'):
            mint_token(synthetic_sa, principal, failed)
        assert len(calls) == before + 1


class SyntheticProvider:
    def __init__(self, member):
        self.member = member
        self.requests = []
        self.about = dict(user=dict(me=True, emailAddress='synthetic@example.invalid',
                                   permissionId='opaque-permission'))
        self.root = dict(id=member['source_root'], mimeType='application/vnd.google-apps.folder', trashed=False)
        self.file = dict(id=member['source_id'], name='P.pdf', parents=[member['source_root']],
            trashed=False, mimeType='application/pdf', capabilities=dict(canDownload=True),
            size='1505', headRevisionId=member['source_revision'])
        self.page = dict(files=[self.file], incompleteSearch=False)
        self.revision = dict(id=member['source_revision'], mimeType='application/pdf',
                            size='1505', keepForever=True)
        self.content = PDF

    def get(self, path, *, token, limit):
        from urllib.parse import urlsplit, parse_qs
        self.requests.append((path, token, limit))
        assert token == 'synthetic-token'
        url = urlsplit(path)
        assert not url.scheme and not url.netloc
        query = parse_qs(url.query)
        if url.path == '/drive/v3/about':
            value = self.about
        elif url.path == '/drive/v3/files':
            assert query['corpora'] == ['user'] and query['spaces'] == ['drive']
            assert query['includeItemsFromAllDrives'] == ['false']
            assert query['q'] == ["'synthetic-root' in parents and trashed = false"]
            value = self.page
        elif url.path == '/drive/v3/files/' + self.member['source_root']:
            value = self.root
        elif url.path == '/drive/v3/files/' + self.member['source_id']:
            value = self.file
        elif url.path == '/drive/v3/files/' + self.member['source_id'] + '/revisions/' + self.member['source_revision']:
            if query.get('alt') == ['media']:
                assert limit == 1505
                return self.content
            value = self.revision
        else:
            raise AssertionError('unexpected provider endpoint')
        assert limit == 65536
        return json.dumps(value).encode()


def source_fixture(synthetic_sa):
    from nilaris.drive155_source import AuthorizedDriveSource
    config = configuration()
    bound = SourceBinding(config, authority(config))
    provider = SyntheticProvider(bound.allowed_member)
    supplier_calls, exchanges = [], []

    def supplier(binding_id):
        supplier_calls.append(binding_id)
        return synthetic_sa

    def token_transport(**request):
        exchanges.append(request['url'])
        return TokenResponse()

    source = AuthorizedDriveSource(bound, credential_supplier=supplier, transport=provider,
                                   token_transport=token_transport)
    return source, provider, supplier_calls, exchanges


def test_discovery_validates_identity_root_and_complete_exact_direct_child(synthetic_sa):
    source, provider, suppliers, exchanges = source_fixture(synthetic_sa)
    assert source.discover() == [configuration()['allowed_member']]
    assert len(exchanges) + len(provider.requests) == 4
    assert suppliers == ['synthetic-credential']
    assert source.permits(configuration()['allowed_member'])
    assert not source.permits(dict(configuration()['allowed_member'], case_id='other-case'))


@pytest.mark.parametrize('fault', ['email', 'permission', 'me', 'missing-user', 'root-id',
    'root-mime', 'root-trash', 'root-shared', 'continuation', 'incomplete', 'missing-completeness',
    'extra-file', 'no-file', 'outside-parent', 'multiple-parents', 'file-id', 'file-mime',
    'file-trash', 'shortcut', 'shared-drive', 'download-denied', 'file-name'])
def test_discovery_refuses_identity_scope_or_incomplete_inventory(synthetic_sa, fault):
    source, provider, _, exchanges = source_fixture(synthetic_sa)
    if fault == 'email': provider.about['user']['emailAddress'] = 'other@example.invalid'
    if fault == 'permission': provider.about['user']['permissionId'] = 'other-permission'
    if fault == 'me': provider.about['user']['me'] = False
    if fault == 'missing-user': provider.about = {}
    if fault == 'root-id': provider.root['id'] = 'other-root'
    if fault == 'root-mime': provider.root['mimeType'] = 'application/pdf'
    if fault == 'root-trash': provider.root['trashed'] = True
    if fault == 'root-shared': provider.root['driveId'] = 'shared-drive'
    if fault == 'continuation': provider.page['nextPageToken'] = 'more'
    if fault == 'incomplete': provider.page['incompleteSearch'] = True
    if fault == 'missing-completeness': provider.page.pop('incompleteSearch')
    if fault == 'extra-file': provider.page['files'].append(dict(provider.file, id='other-file'))
    if fault == 'no-file': provider.page['files'] = []
    if fault == 'outside-parent': provider.file['parents'] = ['other-root']
    if fault == 'multiple-parents': provider.file['parents'].append('other-root')
    if fault == 'file-id': provider.file['id'] = 'other-file'
    if fault == 'file-mime': provider.file['mimeType'] = 'application/vnd.google-apps.document'
    if fault == 'file-trash': provider.file['trashed'] = True
    if fault == 'shortcut': provider.file['shortcutDetails'] = {}
    if fault == 'shared-drive': provider.file['driveId'] = 'shared-drive'
    if fault == 'download-denied': provider.file['capabilities']['canDownload'] = False
    if fault == 'file-name': provider.file['name'] = 'other.pdf'
    assert source.discover() is None
    assert len(exchanges) == 1
    assert len(provider.requests) <= 3
    if fault in ('email', 'permission', 'me', 'missing-user'):
        assert len(provider.requests) == 1


def test_exact_retained_revision_is_acquired_with_one_token_and_five_call_envelope(synthetic_sa):
    source, provider, suppliers, exchanges = source_fixture(synthetic_sa)
    member = source.allowed_member
    assert source.read(member['source_id'], member['source_revision'], 1505) == PDF
    assert len(exchanges) + len(provider.requests) == 5
    assert len(suppliers) == 1
    assert provider.requests[-1][0].endswith('/revisions/' + member['source_revision'] + '?alt=media')


@pytest.mark.parametrize('fault', ['revision-id', 'revision-mime', 'revision-size', 'unretained',
                                  'retention-missing', 'hash', 'size', 'missing-content'])
def test_acquisition_never_falls_back_when_exact_retained_revision_is_unavailable(synthetic_sa, fault):
    source, provider, _, exchanges = source_fixture(synthetic_sa)
    if fault == 'revision-id': provider.revision['id'] = 'other-revision'
    if fault == 'revision-mime': provider.revision['mimeType'] = 'text/plain'
    if fault == 'revision-size': provider.revision['size'] = '1504'
    if fault == 'unretained': provider.revision['keepForever'] = False
    if fault == 'retention-missing': provider.revision.pop('keepForever')
    if fault == 'hash': provider.content = b'x' * 1505
    if fault == 'size': provider.content += b'x'
    if fault == 'missing-content': provider.content = None
    member = source.allowed_member
    assert source.read(member['source_id'], member['source_revision'], 1505) is None
    assert len(exchanges) == 1
    assert len(provider.requests) == (4 if fault in ('hash', 'size', 'missing-content') else 3)
    assert all('?alt=media' not in p or '/revisions/' in p for p, _, _ in provider.requests)


def test_bad_local_pin_is_denied_before_credentials_or_transport(synthetic_sa):
    source, provider, suppliers, exchanges = source_fixture(synthetic_sa)
    member = source.allowed_member
    for source_id, revision, limit in (('other-file', member['source_revision'], 1505),
            (member['source_id'], 'latest', 1505), (member['source_id'], member['source_revision'], 1504),
            (member['source_id'], member['source_revision'], 1024**2 + 1),
            (member['source_id'], member['source_revision'], True)):
        assert source.read(source_id, revision, limit) is None
    assert suppliers == exchanges == provider.requests == []


def test_provider_payloads_are_bounded_and_errors_are_redacted(synthetic_sa):
    for raw in (b'x' * 65537, b'[]', b'{', None):
        source, provider, _, exchanges = source_fixture(synthetic_sa)
        provider.get = lambda *args, **kwargs: raw
        assert source.discover() is None
        assert len(exchanges) == 1
    source, provider, _, _ = source_fixture(synthetic_sa)
    def error(*args, **kwargs):
        raise RuntimeError('synthetic-sensitive-body')
    provider.get = error
    assert source.discover() is None


def test_explicit_private_registry_refuses_ambient_unknown_symlink_or_public_paths(tmp_path, synthetic_sa):
    from nilaris.drive155_auth import PrivateCredentialRegistry, SourceUnavailable
    private = tmp_path / 'private.json'
    private.write_text(json.dumps(synthetic_sa))
    private.chmod(0o600)
    registry = PrivateCredentialRegistry({'synthetic-credential': str(private)},
                                         repository_root=tmp_path / 'repository')
    assert registry('synthetic-credential') == synthetic_sa
    with pytest.raises(SourceUnavailable): registry('unknown')
    private.chmod(0o644)
    with pytest.raises(SourceUnavailable): registry('synthetic-credential')
    private.chmod(0o600)
    link = tmp_path / 'symlink'
    link.symlink_to(private)
    for references in ({'synthetic-credential': str(link)},
                       {'synthetic-credential': 'relative.json'},
                       {'synthetic-credential': str(tmp_path / 'repository' / 'inside.json')}):
        with pytest.raises(SourceUnavailable):
            PrivateCredentialRegistry(references, repository_root=tmp_path / 'repository')('synthetic-credential')


def test_fixed_transport_bounds_reads_hosts_and_refuses_redirects_without_retry():
    from nilaris.drive155_auth import FixedGoogleTransport, SourceUnavailable
    requests, reads, hosts, closed = [], [], [], []
    status = 200
    content = b'{}'

    class Connection:
        sock = None
        def __init__(self, host, *, timeout):
            hosts.append((host, timeout))
        def request(self, method, path, body=None, headers=None):
            requests.append((method, path, body, headers))
        def getresponse(self):
            return self
        @property
        def status(self):
            return status
        def read(self, size):
            reads.append(size)
            return content[:size]
        def close(self):
            closed.append(True)

    transport = FixedGoogleTransport(connection_factory=Connection)
    assert transport.get('/drive/v3/about?fields=user', token='synthetic-token', limit=65536) == b'{}'
    token_kwargs = dict(url='https://oauth2.googleapis.com/token', method='POST', body=b'grant=synthetic',
                        headers={}, timeout=5, limit=65536, allow_redirects=False)
    assert transport(**token_kwargs).data == b'{}'
    assert hosts == [('www.googleapis.com', 5), ('oauth2.googleapis.com', 5)]
    assert reads == [65537, 65537]
    assert requests[0][0:2] == ('GET', '/drive/v3/about?fields=user')
    assert requests[1][0:2] == ('POST', '/token')
    assert len(closed) == 2
    for path in ('https://example.invalid/drive/v3/about', '//example.invalid', '/drive/v3/about\r\nX: y'):
        assert transport.get(path, token='synthetic-token', limit=65536) is None
    assert len(requests) == 2
    for wrong in (dict(token_kwargs, url='https://example.invalid/token'),
                  dict(token_kwargs, method='GET'), dict(token_kwargs, allow_redirects=True)):
        with pytest.raises(SourceUnavailable): transport(**wrong)
    assert len(requests) == 2
    status = 302
    assert transport.get('/drive/v3/about', token='synthetic-token', limit=65536) is None
    with pytest.raises(SourceUnavailable): transport(**token_kwargs)
    assert len(requests) == 4
    status = 200
    content = b'x' * 65537
    assert transport.get('/drive/v3/about', token='synthetic-token', limit=65536) is None
    with pytest.raises(SourceUnavailable): transport(**token_kwargs)
    assert len(requests) == 6 and len(closed) == 6


def test_cloud_unique_id_and_oauth_client_id_are_independent(synthetic_sa):
    from nilaris.drive155_auth import mint_token, SourceUnavailable
    config = configuration()
    config['principal'].update(cloud_unique_id='987654', oauth_client_id='123456')
    bound = SourceBinding(config, authority(config))
    assert mint_token(synthetic_sa, bound.principal, lambda **kwargs: TokenResponse()) == 'synthetic-token'
    with pytest.raises(SourceUnavailable):
        mint_token(dict(synthetic_sa, client_id='987654'), bound.principal,
                   lambda **kwargs: TokenResponse())


def test_account_root_file_and_context_replacement_are_configuration_only(synthetic_sa):
    from nilaris.drive155_source import AuthorizedDriveSource
    original = configuration()
    replacement = copy.deepcopy(original)
    replacement.update(binding_id='replacement-binding', customer_id='replacement-customer',
                       credential_binding_id='replacement-private-ref', source_root='replacement-root')
    replacement['principal'].update(email='replacement@example.invalid', project_id='replacement-project',
                                   cloud_unique_id='cloud-replacement', oauth_client_id='oauth-replacement',
                                   drive_permission_id='permission-replacement')
    replacement['allowed_member'].update(source_id='replacement-file', source_revision='retained-replacement',
                                        source_root='replacement-root', case_id='replacement-case')
    bound = SourceBinding(replacement, authority(replacement))
    assert bound.namespace != SourceBinding(original, authority(original)).namespace
    provider = SyntheticProvider(bound.allowed_member)
    provider.about['user'].update(emailAddress='replacement@example.invalid', permissionId='permission-replacement')
    credential = dict(synthetic_sa, client_email='replacement@example.invalid',
                      project_id='replacement-project', client_id='oauth-replacement')
    calls = []
    def supplier(binding_id):
        calls.append(binding_id)
        return credential
    source = AuthorizedDriveSource(bound, credential_supplier=supplier, transport=provider,
                                   token_transport=lambda **kwargs: TokenResponse())
    member = bound.allowed_member
    assert source.read(member['source_id'], member['source_revision'], 1505) == PDF
    assert calls == ['replacement-private-ref']
    assert not source.permits(original['allowed_member'])


@pytest.mark.parametrize('payload', [b'x' * 65537, b'[]', b'{"type":"service_account","type":"authorized_user"}'])
def test_private_registry_refuses_oversized_or_ambiguous_json(tmp_path, payload):
    from nilaris.drive155_auth import PrivateCredentialRegistry, SourceUnavailable
    private = tmp_path / 'synthetic-private.json'
    private.write_bytes(payload)
    private.chmod(0o600)
    registry = PrivateCredentialRegistry({'synthetic': str(private)}, repository_root=tmp_path / 'repo')
    with pytest.raises(SourceUnavailable, match='^source unavailable$'):
        registry('synthetic')
