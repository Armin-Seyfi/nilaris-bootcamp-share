"""Local explicit publication command, with no HTTP path ingestion route."""
import argparse
import json
from pathlib import Path

from nilaris.command_service import Configuration
from nilaris.review import Review
from nilaris.semantic_publication import MAX_BUNDLE_BYTES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', required=True)
    parser.add_argument('--bundle', required=True, help='Pinned artifact/capture/configuration JSON bundle')
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--command-id', required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--expected-revision', required=True, type=int)
    parser.add_argument('--expected-base', required=True, help='Exact approval ID, or literal null')
    parser.add_argument('--actor', required=True)
    parser.add_argument('--explicit', action='store_true', required=True)
    args = parser.parse_args()
    try:
        configuration = Configuration.load(args.configuration)
        with Path(args.bundle).open('rb') as source:
            raw = source.read(MAX_BUNDLE_BYTES+1)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ValueError
        bundle = json.loads(raw)
        if set(bundle) != {'artifact','capture','configuration','expected_pins'}:
            raise ValueError
        result = Review(configuration.database).promote_semantic(
            namespace=args.namespace,command_id=args.command_id,job_id=args.job_id,
            expected_revision=args.expected_revision,
            expected_base=None if args.expected_base == 'null' else args.expected_base,
            actor=args.actor,explicit=args.explicit,**bundle)
    except (OSError,ValueError,TypeError,KeyError,RecursionError):
        result = dict(known=True,saved=False,approved=False,category='invalid_local_publication_input')
    print(json.dumps(result,allow_nan=False))
    return 0 if result.get('saved') else 1


if __name__ == '__main__':
    raise SystemExit(main())
