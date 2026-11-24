"""Explicit private credential resolution and bounded google-auth OAuth exchange.

No ambient authentication, HTTP implementation, logging or credential export.
The required injected transport is responsible for honoring timeout, fixed URL,
read bound and redirect refusal. Only an offline controlled transport is approved.
"""
import copy

from google.oauth2 import service_account


TOKEN_URL = 'https://oauth2.googleapis.com/token'
READONLY_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
METADATA_LIMIT = 65536


class SourceUnavailable(ValueError):
    """Deliberately generic: provider errors and credential contents never escape."""


def mint_token(info, principal, token_transport):
    """One scoped OAuth exchange; SDK retries cannot trigger another dispatch."""
    try:
        info = copy.deepcopy(info)
        required = {'type', 'project_id', 'private_key_id', 'private_key', 'client_email',
                    'client_id', 'token_uri'}
        optional = {'auth_uri', 'auth_provider_x509_cert_url', 'client_x509_cert_url', 'universe_domain'}
        if (not isinstance(info, dict) or not required <= set(info) or set(info) - required - optional
                or any(not isinstance(v, str) or not v for v in info.values())
                or info['type'] != 'service_account' or info['token_uri'] != TOKEN_URL
                or info.get('universe_domain', 'googleapis.com') != 'googleapis.com'
                or info['client_email'] != principal['email']
                or info['client_id'] != principal['oauth_client_id']
                or info['project_id'] != principal['project_id']):
            raise SourceUnavailable
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[READONLY_SCOPE], always_use_jwt_access=False)
        dispatched = False

        def request(url, method='GET', body=None, headers=None, **kwargs):
            nonlocal dispatched
            if (dispatched or url != TOKEN_URL or method != 'POST'
                    or not isinstance(body, bytes) or len(body) > 16384):
                raise SourceUnavailable
            dispatched = True
            response = token_transport(url=TOKEN_URL, method='POST', body=body,
                headers=dict(headers or {}), timeout=5, limit=METADATA_LIMIT, allow_redirects=False)
            if (type(response.status) is not int or not isinstance(response.data, bytes)
                    or len(response.data) > METADATA_LIMIT or response.status != 200):
                raise SourceUnavailable
            return response

        credentials.refresh(request)
        token = credentials.token
        if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or any(not 33 <= ord(c) <= 126 for c in token)):
            raise SourceUnavailable
        return token
    except Exception:
        raise SourceUnavailable('source unavailable') from None


class PrivateCredentialRegistry:
    """Explicit private ID-to-file references; never consult environment or ADC.

    References are not part of the exported source configuration. Each credential
    file must be an owner-only regular file outside the repository; symlinks,
    relative paths and oversized/ambiguous JSON are rejected before returning it.
    """
    def __init__(self, references, *, repository_root):
        from pathlib import Path
        try:
            if (not isinstance(references, dict) or not references
                    or any(not isinstance(k, str) or not 1 <= len(k) <= 256
                           or not isinstance(v, str) for k, v in references.items())):
                raise SourceUnavailable
            self._references = dict(references)
            self._repository = Path(repository_root).resolve()
        except Exception:
            raise SourceUnavailable('source unavailable') from None

    def __call__(self, binding_id):
        from nilaris.drive155_private import read_private_json
        try:
            return read_private_json(self._references[binding_id], self._repository)
        except Exception:
            raise SourceUnavailable('source unavailable') from None


class FixedGoogleTransport:
    """Fixed-host HTTPS, single request, bounded read; never automatically wired.

    Connection factory is the external socket seam for offline tests. Production
    dispatch remains subject to separate private activation and OS supervision.
    """
    def __init__(self, *, connection_factory=None):
        from http.client import HTTPSConnection
        self._connection_factory = connection_factory or HTTPSConnection

    def _send(self, host, method, path, *, headers, body, limit):
        connection = self._connection_factory(host, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(5)
            response = connection.getresponse()
            if response.status != 200:
                raise SourceUnavailable
            data = response.read(limit + 1)
            if not isinstance(data, bytes) or len(data) > limit:
                raise SourceUnavailable
            return data
        finally:
            connection.close()

    def get(self, path, *, token, limit):
        try:
            if (not isinstance(path, str) or not path.startswith('/drive/v3/')
                    or len(path) > 8192 or any(not 33 <= ord(c) <= 126 for c in path)
                    or not isinstance(token, str) or not 1 <= len(token) <= 8192
                    or any(not 33 <= ord(c) <= 126 for c in token)
                    or type(limit) is not int or not 1 <= limit <= METADATA_LIMIT):
                return None
            return self._send('www.googleapis.com', 'GET', path,
                              headers={'Authorization': 'Bearer ' + token}, body=None, limit=limit)
        except Exception:
            return None

    def __call__(self, *, url, method, body, headers, timeout, limit, allow_redirects):
        from types import SimpleNamespace
        try:
            if (url != TOKEN_URL or method != 'POST' or allow_redirects is not False
                    or timeout != 5 or type(limit) is not int or limit != METADATA_LIMIT
                    or not isinstance(body, bytes) or len(body) > 16384):
                raise SourceUnavailable
            data = self._send('oauth2.googleapis.com', 'POST', '/token', headers=headers, body=body, limit=limit)
            return SimpleNamespace(status=200, data=data, headers={})
        except Exception:
            raise SourceUnavailable('source unavailable') from None
