"""Strict authorized Drive source. Unwired: all provider boundaries are injected.

Controlled positive tests do not establish Viewer compatibility with retained
revision retrieval. Actual dispatch requires separate owner/live authorization.
"""
import copy
import hashlib
import json
import re


PDF_SHA256 = '4fb883b2ae49d073b37bb649917050da03c16a4e6e86f1cdb6c4ff3bdb336c34'
PDF_SIZE = 1505
LABEL = 'drive-readonly-v1'
_MEMBER_KEYS = {'member_key', 'connector', 'source_id', 'source_revision', 'source_root',
    'expected_sha256', 'document_id', 'case_id', 'membership_revision', 'product_type',
    'processing_contract', 'reference_set', 'selection_policy', 'intent_id', 'input_bytes',
    'media_type', 'identity'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def binding_digest(configuration):
    """Canonical nonsecret configuration digest for an independent owner record."""
    return hashlib.sha256(_canonical(configuration)).hexdigest()


def _text(value):
    return (isinstance(value, str) and 1 <= len(value) <= 256 and value == value.strip()
            and all(32 <= ord(c) < 127 for c in value))


class SourceBinding:
    """Immutable snapshot of strict configuration plus independent owner authority."""
    def __init__(self, configuration, authority):
        try:
            config = copy.deepcopy(configuration)
            grant = copy.deepcopy(authority)
            if (not isinstance(config, dict) or set(config) != {'version', 'binding_id',
                    'customer_id', 'credential_binding_id', 'principal', 'source_root', 'allowed_member'}
                    or type(config['version']) is not int or config['version'] != 1
                    or not all(_text(config[k]) for k in ('binding_id', 'customer_id',
                                                        'credential_binding_id', 'source_root'))):
                raise ValueError
            principal = config['principal']
            member = config['allowed_member']
            if (not isinstance(principal, dict) or set(principal) != {'email', 'cloud_unique_id',
                    'drive_permission_id', 'project_id', 'oauth_client_id'} or not all(map(_text, principal.values()))
                    or not isinstance(member, dict) or set(member) != _MEMBER_KEYS
                    or not all(_text(member[k]) for k in _MEMBER_KEYS - {'input_bytes'})
                    or member['connector'] != LABEL or member['source_root'] != config['source_root']
                    or member['media_type'] != 'application/pdf'
                    or type(member['input_bytes']) is not int or member['input_bytes'] != PDF_SIZE
                    or member['expected_sha256'] != PDF_SHA256):
                raise ValueError
            digest = binding_digest(config)
            if (not isinstance(grant, dict) or set(grant) != {'version', 'authority_id', 'binding_digest'}
                    or type(grant['version']) is not int or grant['version'] != 1
                    or not _text(grant['authority_id']) or grant['binding_digest'] != digest):
                raise ValueError
            self._config = config
            self._namespace = hashlib.sha256(_canonical({'binding': config, 'authority': grant})).hexdigest()
            self._digest = digest
        except Exception:
            raise ValueError('source binding unavailable') from None

    @property
    def namespace(self):
        return self._namespace

    @property
    def configuration_digest(self):
        return self._digest

    @property
    def allowed_member(self):
        return copy.deepcopy(self._config['allowed_member'])

    @property
    def credential_binding_id(self):
        return self._config['credential_binding_id']

    @property
    def principal(self):
        return copy.deepcopy(self._config['principal'])


class AuthorizedDriveSource:
    """Bounded source seam, deliberately separate from trusted finite sources.

    The caller must reserve discovery=4/acquisition=5 requests before invoking
    these operations and supervise the process. Failure never grants a retry.
    Transport.get accepts relative fixed-host Drive GET paths, token and read
    bound; it must refuse redirects/retries and honor bounded socket timeouts.
    No production transport or live default is supplied here.
    """
    label = LABEL
    discovery_requests = 4
    acquisition_requests = 5

    def __init__(self, binding, *, credential_supplier, transport, token_transport):
        if not isinstance(binding, SourceBinding):
            raise ValueError('source binding unavailable')
        self._binding = binding
        self._credential_supplier = credential_supplier
        self._transport = transport
        self._token_transport = token_transport

    @property
    def namespace(self):
        return self._binding.namespace

    @property
    def allowed_member(self):
        return self._binding.allowed_member

    def permits(self, member):
        return member == self._binding.allowed_member

    def _json(self, path, token):
        raw = self._transport.get(path, token=token, limit=65536)
        if not isinstance(raw, bytes) or len(raw) > 65536:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value

    def _authenticate(self):
        from nilaris.drive155_auth import mint_token
        token = mint_token(self._credential_supplier(self._binding.credential_binding_id),
                           self._binding.principal, self._token_transport)
        about = self._json('/drive/v3/about?fields=user(me,emailAddress,permissionId)', token)
        user = about.get('user')
        principal = self._binding.principal
        if (not isinstance(user, dict) or user.get('me') is not True
                or user.get('emailAddress') != principal['email']
                or user.get('permissionId') != principal['drive_permission_id']):
            raise ValueError
        return token

    def _valid_file(self, value):
        member = self._binding.allowed_member
        return (isinstance(value, dict) and value.get('id') == member['source_id']
                and value.get('name') == 'P.pdf' and value.get('parents') == [member['source_root']]
                and value.get('trashed') is False and value.get('mimeType') == 'application/pdf'
                and 'shortcutDetails' not in value and 'driveId' not in value
                and isinstance(value.get('capabilities'), dict)
                and value['capabilities'].get('canDownload') is True)

    def discover(self):
        """Return one complete authorized offer, or None; never partial progress."""
        from urllib.parse import quote, urlencode
        try:
            token = self._authenticate()
            member = self._binding.allowed_member
            root_id = member['source_root']
            root = self._json('/drive/v3/files/' + quote(root_id, safe='') +
                              '?fields=id,mimeType,trashed,shortcutDetails,driveId', token)
            if (root.get('id') != root_id or root.get('mimeType') != 'application/vnd.google-apps.folder'
                    or root.get('trashed') is not False or 'shortcutDetails' in root or 'driveId' in root):
                return None
            escaped_root = root_id.replace('\\', '\\\\').replace("'", "\\'")
            query = urlencode(dict(q="'" + escaped_root + "' in parents and trashed = false",
                corpora='user', spaces='drive', includeItemsFromAllDrives='false', pageSize=2,
                fields='nextPageToken,incompleteSearch,files(id,name,parents,trashed,mimeType,capabilities,shortcutDetails,driveId)'))
            page = self._json('/drive/v3/files?' + query, token)
            files = page.get('files')
            if (page.get('incompleteSearch') is not False or page.get('nextPageToken') not in (None, '')
                    or not isinstance(files, list) or len(files) != 1 or not self._valid_file(files[0])):
                return None
            return [member]
        except Exception:
            return None

    def read(self, source_id, revision, limit):
        """Read only the exact retained revision; no latest/head or write fallback."""
        from urllib.parse import quote
        member = self._binding.allowed_member
        if (source_id != member['source_id'] or revision != member['source_revision']
                or type(limit) is not int or not PDF_SIZE <= limit <= 1024**2):
            return None
        try:
            token = self._authenticate()
            path = '/drive/v3/files/' + quote(source_id, safe='')
            file = self._json(path + '?fields=id,name,parents,trashed,mimeType,capabilities,shortcutDetails,driveId', token)
            if not self._valid_file(file):
                return None
            revision_path = path + '/revisions/' + quote(revision, safe='')
            metadata = self._json(revision_path + '?fields=id,mimeType,size,keepForever', token)
            if (metadata.get('id') != revision or metadata.get('mimeType') != 'application/pdf'
                    or metadata.get('size') != str(PDF_SIZE) or metadata.get('keepForever') is not True):
                return None
            content = self._transport.get(revision_path + '?alt=media', token=token, limit=PDF_SIZE)
            if (not isinstance(content, bytes) or len(content) != PDF_SIZE
                    or hashlib.sha256(content).hexdigest() != member['expected_sha256']):
                return None
            return content
        except Exception:
            return None
