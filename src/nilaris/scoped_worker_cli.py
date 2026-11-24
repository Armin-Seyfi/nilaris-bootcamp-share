"""Run a single controlled customer worker operation or its finite local receiver."""
import argparse
import json
import time

import psycopg

from nilaris.local_transport import LocalReceiver
from nilaris.onboarding182_ui import controlled_services
from nilaris.provisioning181 import _private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='private controlled local service configuration')
    parser.add_argument('--customer', required=True, help='configured customer source binding')
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('run', 'reconcile'):
        commands.add_parser(action).add_argument('job_id')
    commands.add_parser('receive').add_argument('--seconds', required=True, type=int)
    args = parser.parse_args()

    service, intake = controlled_services(_private_json(args.config))
    if intake is None:
        parser.error('configure a controlled intake source first')
    worker = intake.worker(args.customer)
    if args.action == 'receive':
        if not 1 <= args.seconds <= 120:
            parser.error('supply a receiver duration between 1 and 120 seconds')
        service.provisioner._require_monitor()
        deadline = time.monotonic() + args.seconds
        receiver = LocalReceiver(service.provisioner.directory, args.customer)
        try:
            with receiver:
                print('Controlled receiver ready', flush=True)
                while time.monotonic() < deadline:
                    service.provisioner._require_monitor()
                    receiver.wait(min(0.2, max(0, deadline - time.monotonic())))
        finally:
            print(json.dumps({'received_operations': receiver.deliveries}))
    else:
        result = worker.run(args.job_id) if args.action == 'run' else worker.reconcile(args.job_id)
        print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, psycopg.Error):
        # An interrupted handoff can have taken effect before the caller receives an error.
        print(json.dumps({'state': 'unknown', 'next_action': 'inspect_original_job'}))
        raise SystemExit(1) from None
