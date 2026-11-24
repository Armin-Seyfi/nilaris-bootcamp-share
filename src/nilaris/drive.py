"""One explicitly mapped synthetic Drive object; no live Drive access."""
import base64
import binascii
import hashlib
from http.client import HTTPSConnection
import json
import re
from urllib.parse import quote

from nilaris.execution import ControlledSource


class PinnedDrive:
    """Private one-file policy and labeled finite external-source substitute.

    This adapter supplies fixture bytes, never Google bytes or credentials.
    The policy is independent of the offered HTTP/n8n metadata.
    """
    label = 'synthetic-drive-v1'

    def __init__(self, configuration):
        if not isinstance(configuration, dict) or configuration.get('mode') != self.label:
            raise ValueError('live Drive execution is not authorized')
        allowed = configuration.get('allowed_member')
        required = {'member_key', 'connector', 'source_id', 'source_revision', 'source_root',
                    'expected_sha256', 'document_id', 'case_id', 'membership_revision',
                    'product_type', 'processing_contract', 'reference_set', 'selection_policy',
                    'intent_id', 'input_bytes', 'media_type', 'identity'}
        if (not isinstance(allowed, dict) or set(allowed) != required or
                any(not isinstance(allowed[key], str) or not allowed[key].strip()
                    for key in required - {'input_bytes'}) or
                allowed['connector'] != self.label or allowed['media_type'] != 'application/pdf' or
                not re.fullmatch('[0-9a-f]{64}', allowed['expected_sha256']) or
                type(allowed['input_bytes']) is not int or not 0 < allowed['input_bytes'] <= 1024**2):
            raise ValueError('one-file Drive policy unavailable')
        self.allowed = dict(allowed)
        source = configuration.get('object')
        if not isinstance(source, dict) or set(source) != {
                'source_id', 'source_revision', 'source_root', 'content_b64'}:
            raise ValueError('synthetic Drive fixture unavailable')
        try:
            content = base64.b64decode(source['content_b64'], validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ValueError('synthetic Drive fixture unavailable') from None
        if len(content) > 1024**2:
            raise ValueError('synthetic Drive fixture exceeds bound')
        self._source = dict(source)
        self._content = content

    def permits(self, member):
        return member == self.allowed

    def controlled_source(self):
        """Called only after durable claim/capacity checks; exact ID/revision only."""
        same_root = self._source['source_root'] == self.allowed['source_root']
        objects = ({self._source['source_id']: (self._source['source_revision'], self._content)}
                   if same_root else {})
        return ControlledSource(objects)


class _ReadOnlyHTTPS:
    """Fixed Google host, GET only, bounded reads/timeouts, no redirect/retry."""
    def get(self, path, *, token, limit):
        connection = HTTPSConnection('www.googleapis.com', timeout=5)
        try:
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + token})
            if connection.sock is not None:
                connection.sock.settimeout(10)
            response = connection.getresponse()
            if response.status != 200:
                return None
            return response.read(limit + 1)
        finally:
            connection.close()


class PreparedReadOnlyDrive:
    """Unwired preparation, NOT an approved/supervised live acquisition path.

    The command listener refuses every live mode. A future owner-authorized
    runner must provide exact principal/root/file authorization and enforce its
    OS deadlines, request accounting and credential handling before using this
    reader. Injected provider transport tests prove only read-only reader logic.
    Configured principal is not proof of the token's actual Google principal.
    """
    label = 'drive-readonly-prepared-v1'

    def __init__(self, configuration, *, credential_supplier, transport=None, authorization=None):
        self._configuration = configuration
        self._authorization = authorization
        self._credential_supplier = credential_supplier
        self._transport = transport if transport is not None else _ReadOnlyHTTPS()

    def read(self, source_id, revision, limit):
        # No supplier or transport call precedes complete local scope validation.
        config = self._configuration
        if not isinstance(config, dict) or not isinstance(config.get('allowed_member'), dict):
            return None
        item = config['allowed_member']
        strings = ('source_root', 'source_id', 'source_revision', 'document_id', 'case_id',
                   'membership_revision', 'product_type', 'processing_contract',
                   'reference_set', 'selection_policy', 'intent_id')
        if (not isinstance(config.get('principal'), str) or not config['principal'].strip() or
                any(not isinstance(item.get(key), str) or not item[key].strip() for key in strings) or
                item.get('connector') != self.label or item.get('media_type') != 'application/pdf' or
                not isinstance(item.get('expected_sha256'), str) or
                not re.fullmatch('[0-9a-f]{64}', item['expected_sha256']) or
                type(item.get('input_bytes')) is not int or not 0 < item['input_bytes'] <= 1024**2 or
                type(limit) is not int or not item['input_bytes'] <= limit <= 1024**2 or
                source_id != item['source_id'] or revision != item['source_revision'] or
                self._authorization != dict(principal=config['principal'], source_root=item['source_root'],
                                             source_ids=[item['source_id']])):
            return None
        try:
            token = self._credential_supplier()
            if (not isinstance(token, str) or not 1 <= len(token) <= 8192 or
                    any(not 33 <= ord(char) <= 126 for char in token)):
                return None
            path = '/drive/v3/files/' + quote(source_id, safe='')
            raw = self._transport.get(path + '?fields=id,parents,trashed,mimeType,capabilities,shortcutDetails&supportsAllDrives=true',
                                      token=token, limit=65536)
            if not isinstance(raw, bytes) or len(raw) > 65536:
                return None
            metadata = json.loads(raw)
            if (not isinstance(metadata, dict) or metadata.get('id') != source_id or
                    not isinstance(metadata.get('parents'), list) or
                    item['source_root'] not in metadata['parents'] or metadata.get('trashed') is not False or
                    metadata.get('mimeType') != 'application/pdf' or metadata.get('shortcutDetails') or
                    not isinstance(metadata.get('capabilities'), dict) or
                    metadata['capabilities'].get('canDownload') is not True):
                return None
            content = self._transport.get(path + '/revisions/' + quote(revision, safe='') + '?alt=media',
                                          token=token, limit=item['input_bytes'])
            if (not isinstance(content, bytes) or len(content) != item['input_bytes'] or
                    hashlib.sha256(content).hexdigest() != item['expected_sha256']):
                return None
            return content
        except Exception:
            # No provider body, token or exception text escapes this reader.
            # Missing/restricted/changed pins are unavailable, never latest bytes.
            return None
