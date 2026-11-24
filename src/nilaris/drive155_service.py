"""Explicit future activation assembly. Never invoked by ordinary listener startup.

This module is not live-readiness evidence. Running it requires separately
confirmed concrete owner authority and live authorization. No credential file is
opened by assembly: key resolution occurs inside a supervised dispatched call.
"""
import argparse
import hmac
from pathlib import Path
import sys

from nilaris.command_service import Configuration, serve
from nilaris.drive155_private import read_private_json
from nilaris.drive155_auth import FixedGoogleTransport, PrivateCredentialRegistry
from nilaris.drive155_journey import DriveJourney
from nilaris.drive155_source import SourceBinding, AuthorizedDriveSource, binding_digest


def configuration(command_config_path, source_path, authority_path, credential_registry_path,
                  *, activation_digest=None, repository_root=None):
    """Assemble one generic private source only after exact authority confirmation.

    The activation digest is SHA256 of canonical authority JSON (same canonical
    encoding as binding_digest), independently supplied as an explicit argument.
    Registry JSON is exactly `{version:1,references:{binding_id:absolute_path}}`.
    Keep archived immutable source/authority/config copies to read older streams.
    """
    try:
        root = Path(repository_root or Path(__file__).resolve().parents[2]).resolve()
        source = read_private_json(source_path, root)
        authority = read_private_json(authority_path, root)
        bound = SourceBinding(source, authority)
        if (not isinstance(activation_digest, str) or
                not hmac.compare_digest(activation_digest, binding_digest(authority))):
            raise ValueError
        registry = read_private_json(credential_registry_path, root)
        if (set(registry) != {'version', 'references'} or type(registry['version']) is not int
                or registry['version'] != 1 or not isinstance(registry['references'], dict)
                or set(registry['references']) != {bound.credential_binding_id}):
            raise ValueError
        supplier = PrivateCredentialRegistry(registry['references'], repository_root=root)
        private = Configuration.load(command_config_path)
        if private.drive is not None or private.profile is None:
            raise ValueError
        transport = FixedGoogleTransport()
        reader = AuthorizedDriveSource(bound, credential_supplier=supplier,
                                       transport=transport, token_transport=transport)
        private.source_operator = DriveJourney(None, {source['binding_id']: reader})
        return private
    except Exception:
        raise ValueError('source activation unavailable') from None


def main():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            print('source activation unavailable', file=sys.stderr)
            raise SystemExit(2)
    parser = Parser(description='Separately authorized explicit source listener only')
    for name in ('command-config', 'source', 'authority', 'credential-registry', 'activate-authority-digest'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        private = configuration(args.command_config, args.source, args.authority,
                                args.credential_registry, activation_digest=args.activate_authority_digest)
        serve(private)
    except Exception:
        print('source activation unavailable', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
