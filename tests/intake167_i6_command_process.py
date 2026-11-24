"""Controlled I6 HTTP listener: real source/journey, synthetic external boundaries."""
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nilaris.command_service import Configuration, serve
from nilaris.drive155_journey import DriveJourney
from nilaris.drive155_source import AuthorizedDriveSource, SourceBinding
from test_drive155_source import (
    PDF, SyntheticProvider, TokenResponse, authority, configuration, synthetic_sa,
)


class ControlledProvider:
    """Faults become visible only after the caller durably admits the offer.

    A file journal crosses the actual supervisor's process boundary. It records
    external calls, never substitutes for source/capture/persistence behavior.
    """
    def __init__(self, member, fault_path, journal):
        self.member = member
        self.provider = SyntheticProvider(member)
        self.fault_path = fault_path
        self.journal = journal
        self.newer_revision = 'synthetic-newer-revision'
        self.changed_media = b'x' * len(PDF)

    def record(self, **event):
        with self.journal.open('a') as output:
            output.write(json.dumps(event) + '\n')

    def get(self, path, *, token, limit):
        self.record(kind='get', path=path, limit=limit)
        fault = self.fault_path.read_text() if self.fault_path.exists() else None
        url = urlsplit(path)
        media = parse_qs(url.query).get('alt') == ['media']
        file_path = '/drive/v3/files/' + self.member['source_id']
        exact_path = file_path + '/revisions/' + self.member['source_revision']
        if fault == 'unavailable-revision':
            # Both metadata and media for the offered revision are unavailable;
            # a valid newer head and its media still exist in this provider.
            if url.path == exact_path:
                self.record(kind='fault', fault=fault, newer_revision=self.newer_revision)
                return None
            if url.path == file_path:
                if media:
                    return self.changed_media
                return json.dumps(dict(self.provider.file, headRevisionId=self.newer_revision)).encode()
            if url.path == file_path + '/revisions/' + self.newer_revision:
                if media:
                    return self.changed_media
                return json.dumps(dict(self.provider.revision, id=self.newer_revision)).encode()
        if fault == 'changed-same-length-media' and url.path == exact_path and media:
            self.record(kind='fault', fault=fault, returned_bytes=len(self.changed_media))
            return self.changed_media
        return self.provider.get(path, token=token, limit=limit)


def main():
    if len(sys.argv) != 5 or sys.argv[1] != '--controlled-offline':
        raise SystemExit(2)
    private = Configuration.load(sys.argv[2])
    source_config = configuration()
    grant = authority(source_config)
    fault_path = Path(sys.argv[3])
    if fault_path.exists() and fault_path.read_text() == 'rebound-authority':
        grant = dict(grant, authority_id='rebound-owner-grant')
    bound = SourceBinding(source_config, grant)
    provider = ControlledProvider(bound.allowed_member, Path(sys.argv[3]), Path(sys.argv[4]))
    synthetic_credential = synthetic_sa.__wrapped__()

    def supplier(binding_id):
        provider.record(kind='credential', binding_id=binding_id)
        return synthetic_credential

    def exchange(**request):
        provider.record(kind='token', method=request['method'], url=request['url'])
        return TokenResponse()

    source = AuthorizedDriveSource(bound, credential_supplier=supplier, transport=provider,
                                   token_transport=exchange)
    private.source_operator = DriveJourney(None, {'synthetic-binding': source})
    serve(private)


if __name__ == '__main__':
    main()
