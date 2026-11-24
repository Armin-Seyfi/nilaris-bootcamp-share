"""Finite prospective batch; isolated durable records, no import-time effects."""
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time

from nilaris.semantic_contract import canonical, validate
from nilaris.semantic_provider import request_body as original_request, count_request

PROFILE = 'semantic172-v1'
LIVE_CREDENTIAL_PATH = '/private/tmp/nilaris-semantic128-private/credentials/openai-api-key'
RESERVATION = Decimal('0.10144')
HISTORICAL = Decimal('0.30432')
POLICY = ('One technical definition per distinct property and stated applicability/condition. '
          'Do not pool distinct evidence requirements. Qualifier metadata alone is context '
          'unless it independently defines a technical property. Decimal units preserve '
          'explicit nonblank literal source units; unitless text definitions use schema null.')


def request_body(capture, target):
    body = original_request(capture, target)
    body['instructions'] = body['instructions'].replace(
        'Absent units and blank source values use empty\nstrings; retain literal missing markers.',
        'Absent units use schema null; blank source values use empty strings; retain literal missing markers.')
    body['instructions'] += '\n' + POLICY
    return body


class Ledger:
    """New ledger only: unknown reservations remain charged and block continuation."""
    def __init__(self, root, *, mode):
        if mode not in ('live', 'offline'):
            raise ValueError('explicit mode required')
        self.root = Path(root).resolve()
        self.mode = mode
        if self.read('mode.json') != {'mode': mode, 'profile': PROFILE}:
            raise ValueError('ledger mode mismatch')

    @contextmanager
    def locked(self):
        lock = self.root / '.lock'
        if lock.is_symlink():
            raise ValueError('ledger lock alias')
        with lock.open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def write(self, name, value):
        if Path(name).name != name or not name.endswith('.json'):
            raise ValueError('artifact name')
        raw = canonical(value)
        if len(raw) > 2 * 1024**2 or sum(p.stat().st_size for p in self.root.iterdir()) + len(raw) > 32 * 1024**2:
            raise ValueError('artifact capacity')
        fd = os.open(self.root / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def read(self, name):
        if Path(name).name != name or not name.endswith('.json'):
            raise ValueError('artifact name')
        path = self.root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024**2:
            raise ValueError('artifact identity')
        return json.loads(path.read_bytes())

    def status(self):
        attempts = [self.read(p.name) for p in sorted(self.root.glob('attempt-*.json'))]
        completed = 0
        for a in attempts:
            number = a['attempt']
            parent = self.root / f'parent-{number:02}.json'
            result = self.root / f'result-{number:02}.json'
            if parent.exists():
                if (self.read(parent.name) != dict(attempt=number, status='completed') or
                        not result.exists() or self.read(result.name)['status'] != 'completed' or
                        not (self.root / f'dispatch-{number:02}.json').is_file()):
                    raise ValueError('parent/result accounting mismatch')
                validate(self.read(result.name)['initial_output'], self.read('capture.json'), a['document_id'])
                if not (self.root / f'stop-{number:02}.json').exists():
                    completed += 1
        known_usage = []
        for path in sorted(self.root.glob('response-*.json')):
            usage = self.read(path.name).get('usage')
            if (type(usage) is dict and type(usage.get('input_tokens')) is int and
                    type(usage.get('output_tokens')) is int and
                    0 <= usage['input_tokens'] <= 16000 and 0 <= usage['output_tokens'] <= 4096):
                cost = (Decimal(usage['input_tokens'])*Decimal('2.50') +
                        Decimal(usage['output_tokens'])*Decimal('15.00')) / 1000000
                known_usage.append(dict(record=path.name, input_tokens=usage['input_tokens'],
                    output_tokens=usage['output_tokens'], uncached_estimate_usd=str(cost)))
        return dict(attempted=len(attempts), completed=completed, inconclusive=len(attempts)-completed,
            attempts=attempts, new_reserved_usd=str(RESERVATION * len(attempts)),
            historical_reserved_usd=str(HISTORICAL), combined_reserved_usd=str(HISTORICAL + RESERVATION*len(attempts)),
            reported_usage=known_usage, usage_unknown_reservations=len(attempts)-len(known_usage),
            reservation_attempted=len(attempts),
            dispatch_intents=len(list(self.root.glob('dispatch-*.json'))),
            received_records=len(list(self.root.glob('response-*.json'))),
            external_generations_upper_bound=len(attempts) if self.mode == 'live' else 0)

    def verify(self):
        frozen = self.read('freeze.json')
        capture = self.read('capture.json')
        if (frozen['profile'] != PROFILE or frozen['configuration'] != configuration() or
                frozen['code_pins'] != code_pins() or frozen['capture_pin'] != digest(capture) or
                frozen['requests'] != {k: request_body(capture, k) for k in frozen['order']}):
            raise ValueError('frozen evidence mutation')
        if frozen['sources_pin'] is not None:
            from nilaris.semantic172_sources import capture_sources
            sources = self.read('sources.json')
            if digest(sources) != frozen['sources_pin'] or capture_sources(sources) != capture:
                raise ValueError('source evidence mutation')
        for request in frozen['requests'].values():
            if self.mode == 'live':
                count_request(request)
            elif len(canonical(request)) + 2048 > 16000:
                raise ValueError('input cap')
        return frozen

    def reserve(self, target):
        with self.locked():
            frozen = self.verify()
            if self.mode == 'live':
                check_authorization(self)
            state = self.status()
            n = state['attempted']
            if (n >= 2 or target != frozen['order'][n] or state['completed'] != n or
                    any(self.root.glob('stop-*.json')) or
                    (n and batch_remaining(self) <= 0)):
                raise ValueError('batch stopped, wrong order or unknown outcome')
            # The reservation is the charge: fsync before identity gate or HTTP.
            self.write(f'attempt-{n+1:02}.json', dict(attempt=n+1, document_id=target,
                request_pin=digest(frozen['requests'][target]), reserved_at=time.time(),
                reserved_monotonic=time.monotonic(),
                reservation_usd=str(RESERVATION), status='dispatch_reserved_unknown'))
            return n+1


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def configuration():
    return dict(profile=PROFILE, http_seconds=90, worker_wall_seconds=105, batch_seconds=600,
        live_credential_path=LIVE_CREDENTIAL_PATH,
        cpu_seconds=20, rss_bytes=1024**3, stdout_bytes=2*1024**2, lease_seconds=10,
        heartbeat_seconds=2, input_tokens=16000, output_tokens=4096, retries=0,
        max_requests=2, new_reservation_ceiling_usd='0.20288', historical_reserved_usd='0.30432',
        combined_reservation_ceiling_usd='0.50720')


def code_pins():
    directory = Path(__file__).parent
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in
        ('semantic172.py', 'semantic172_sources.py', 'worker.py', 'controlled_stage.py', 'execution.py', 'admission.py',
         'semantic_contract.py', 'semantic_provider.py', 'worker_lifecycle.py')}


def prepare(root, capture, order, *, mode, evidence_pins=None, sources=None):
    if (type(order) is not list or len(order) != 2 or len(set(order)) != 2 or
            {d['document_id'] for d in capture['documents']} != set(order) or len(capture['documents']) != 2):
        raise ValueError('exactly two independent ordered captures required')
    if mode == 'live' and (not evidence_pins or any(type(v) is not str or len(v) != 64 or
            any(c not in '0123456789abcdef' for c in v) for v in evidence_pins.values())):
        raise ValueError('live requires frozen external evidence hashes')
    if mode == 'live' and sources is None:
        raise ValueError('live requires original frozen sources')
    if sources is not None:
        from nilaris.semantic172_sources import capture_sources
        if capture_sources(sources) != capture:
            raise ValueError('source capture mismatch')
    root = Path(root).resolve()
    root.mkdir(mode=0o700)  # New, never resume/repair an existing or historical root.
    sink = object.__new__(Ledger)
    sink.root, sink.mode = root, mode
    if mode not in ('live', 'offline'):
        raise ValueError('mode')
    requests = {target: request_body(capture, target) for target in order}
    counts = {target: count_request(body) if mode == 'live' else
              {'conservative_upper': len(canonical(body))+2048} for target, body in requests.items()}
    if any(c['conservative_upper'] > 16000 for c in counts.values()):
        raise ValueError('input cap')
    sink.write('mode.json', dict(mode=mode, profile=PROFILE))
    sink.write('capture.json', capture)
    if sources is not None:
        sink.write('sources.json', sources)
    sink.write('freeze.json', dict(profile=PROFILE, order=order, requests=requests,
        token_counts=counts, capture_pin=digest(capture), configuration=configuration(),
        code_pins=code_pins(), evidence_pins=evidence_pins or {},
        sources_pin=digest(sources) if sources is not None else None))
    return sink


def check_authorization(sink, expected_pin=None):
    receipt = sink.read('authorization.json')
    if (receipt != dict(profile=PROFILE, freeze_pin=digest(sink.read('freeze.json')),
            input_usd_per_million='2.50', output_usd_per_million='15.00',
            new_ceiling_usd='0.20288', historical_reserved_usd='0.30432', combined_ceiling_usd='0.50720',
            checks={k: True for k in ('owner_dispatch', 'independent_rubric_frozen', 'checker_verified',
                'standards_review', 'spec_review', 'model_available', 'official_terms', 'account_data_sharing_disabled')},
            evidence_pins=sink.read('freeze.json')['evidence_pins'], verified_at=receipt.get('verified_at')) or
            type(receipt.get('verified_at')) not in (int, float) or
            not math.isfinite(receipt['verified_at']) or not 0 <= time.time()-receipt['verified_at'] <= 3600 or
            not receipt['evidence_pins'] or
            (expected_pin is not None and digest(receipt) != expected_pin)):
        raise ValueError('live authorization mismatch')
    return digest(receipt)


def artifact_charge(sink, attempt):
    inventory = []
    for path in sorted(sink.root.iterdir()):
        if path.name == '.lock':
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('artifact identity')
        raw = path.read_bytes()
        inventory.append(dict(name=path.name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    return dict(bytes=sum(row['bytes']+4096 for row in inventory)+8192,
                inventory_sha256=digest(inventory))


def parent_outcome(sink, attempt, status):
    with sink.locked():
        sink.read(f'attempt-{attempt:02}.json')
        sink.write(f'{"parent" if status == "completed" else "stop"}-{attempt:02}.json',
                   dict(attempt=attempt, status=status))


def node_event(sink, descriptor, node, phase):
    with sink.locked():
        sink.write(f'node-{descriptor["attempt"]:02}-{node}-{phase}.json',
                   dict(node=node, phase=phase, at=time.time(), monotonic=time.monotonic(),
                        request_pin=descriptor['request_pin']))


def execute_capture(sink, descriptor, *, deadline):
    from nilaris.semantic_provider import interpret, credential_file
    if sink.mode == 'live' and str(descriptor.get('credential_path')) != LIVE_CREDENTIAL_PATH:
        raise ValueError('designated live credential required')
    frozen = sink.verify()
    number, target = descriptor['attempt'], descriptor['target_document_id']
    if digest(frozen) != descriptor['freeze_pin']:
        raise ValueError('freeze mismatch')
    if sink.mode == 'live':
        check_authorization(sink, descriptor['authorization_pin'])
    reservation = sink.read(f'attempt-{number:02}.json')
    body = frozen['requests'][target]
    if reservation['document_id'] != target or reservation['request_pin'] != digest(body):
        raise ValueError('reservation mismatch')
    with sink.locked():
        sink.write(f'dispatch-{number:02}.json', dict(attempt=number, at=time.time()))
    batch_deadline = time.monotonic() + batch_remaining(sink)
    response = interpret(body, credential=credential_file(descriptor['credential_path'])
        if sink.mode == 'live' else 'synthetic-offline', mode=sink.mode,
        deadline=min(deadline, batch_deadline), offline_endpoint=descriptor.get('offline_endpoint'),
        timing_profile=PROFILE)
    response['target_document_id'] = target
    with sink.locked():
        sink.write(f'response-{number:02}.json', response)
    return response


def finalize_capture(sink, descriptor, response):
    result = dict(response)
    if result['status'] == 'received':
        try:
            validate(result['initial_output'], sink.read('capture.json'), descriptor['target_document_id'])
            result['status'] = 'completed'
        except ValueError:
            result['status'] = 'invalid_output'
    with sink.locked():
        sink.write(f'result-{descriptor["attempt"]:02}.json', result)
    return result


def run(ingestion, job_id, expected_revision, *, artifact_root, target_document_id,
        expected_freeze_sha256, mode, credential_path=None, offline_endpoint=None):
    """One explicit dispatch via the unchanged current durable-claim supervisor."""
    import psycopg
    from psycopg.rows import dict_row
    from nilaris.execution import conflict
    from nilaris.worker import _run_request
    if not getattr(ingestion, 'semantic172', False):
        return conflict(job_id, 'semantic172_not_enabled')
    if mode == 'live' and str(credential_path) != LIVE_CREDENTIAL_PATH:
        raise ValueError('designated live credential required')
    if mode == 'live' and offline_endpoint is not None:
        raise ValueError('live endpoint override')
    sink = Ledger(artifact_root, mode=mode)
    frozen = sink.verify()
    state = sink.status()
    n = state['attempted']
    if (n >= 2 or state['completed'] != n or any(sink.root.glob('stop-*.json')) or
            target_document_id != frozen['order'][n] or batch_remaining(sink) <= 0):
        raise ValueError('batch stopped, wrong order or unknown outcome')
    if digest(frozen) != expected_freeze_sha256 or target_document_id not in frozen['order']:
        raise ValueError('frozen configuration mismatch')
    capture = sink.read('capture.json')
    target, = [d for d in capture['documents'] if d['document_id'] == target_document_id]
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        job = connection.execute('SELECT input,captured_input,content_pin FROM admission_jobs WHERE id=%s',
                                 (job_id,)).fetchone()
    if (not job or job['input'].get('processing_contract') != PROFILE or
            job['input'].get('connector') != 'synthetic' or
            any(job['input'].get(key) != target[key] for key in ('document_id', 'source_revision', 'media_type')) or
            job['content_pin'] != target['content_pin'] or job['captured_input'] is None or
            hashlib.sha256(job['captured_input']).hexdigest() != target['content_pin']):
        return conflict(job_id, 'semantic172_source_identity_mismatch')
    # Conservative retained artifact headroom before claiming a DB slot.
    if (ingestion.status(job_id)['usage']['retained_bytes'] + artifact_charge(sink, 1)['bytes'] +
            3*1024**2 > ingestion.status(job_id)['profile']['job_bytes']):
        return conflict(job_id, 'resource_exhausted')
    authorization = check_authorization(sink) if mode == 'live' else None
    descriptor = dict(profile=PROFILE, artifact_root=str(sink.root), target_document_id=target_document_id,
        request_pin=digest(frozen['requests'][target_document_id]), freeze_pin=expected_freeze_sha256,
        mode=mode, authorization_pin=authorization, offline_endpoint=offline_endpoint,
        credential_path=str(Path(credential_path).absolute()) if credential_path is not None else None)
    request = canonical(dict(operation=PROFILE, semantic=descriptor))
    return _run_request(ingestion, job_id, expected_revision, request, test_wall_seconds=105,
        test_cpu_seconds=20, test_rss_bytes=1024**3, test_workload=PROFILE, adapter=PROFILE, semantic=descriptor)


def batch_remaining(sink):
    attempts = sink.status()['attempts']
    if not attempts:
        return 600
    first = attempts[0]
    elapsed = (time.time()-first['reserved_at'], time.monotonic()-first['reserved_monotonic'])
    if any(not math.isfinite(value) or value < 0 for value in elapsed):
        return 0
    return max(0, 600-max(elapsed))


def read_result(root, attempt, *, mode):
    sink = Ledger(root, mode=mode)
    sink.verify()
    result = sink.read(f'result-{attempt:02}.json')
    stop = sink.root / f'stop-{attempt:02}.json'
    if stop.exists():
        return dict(result, child_status=result['status'], status=sink.read(stop.name)['status'])
    parent = sink.root / f'parent-{attempt:02}.json'
    if not parent.exists() or sink.read(parent.name) != dict(attempt=attempt, status='completed'):
        return dict(result, child_status=result['status'], status='parent_outcome_unknown')
    sink.status()  # Verify reservation/result/parent accounting before publication.
    return result


def main():
    """Operator commands are explicit; preparation/preflight cannot dispatch."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    prepare_parser = sub.add_parser('prepare')
    prepare_parser.add_argument('--root', required=True)
    prepare_parser.add_argument('--sources', required=True)
    prepare_parser.add_argument('--evidence-pins')
    prepare_parser.add_argument('--mode', required=True, choices=('offline', 'live'))
    for name in ('preflight', 'status'):
        command = sub.add_parser(name)
        command.add_argument('--root', required=True)
        command.add_argument('--mode', required=True, choices=('offline', 'live'))
    for name in ('dispatch-offline', 'dispatch-live'):
        command = sub.add_parser(name)
        command.add_argument('--root', required=True)
        command.add_argument('--database-config', required=True)
        command.add_argument('--job-id', required=True)
        command.add_argument('--revision', required=True, type=int)
        command.add_argument('--target', required=True)
        command.add_argument('--freeze-pin', required=True)
        if name == 'dispatch-live':
            command.add_argument('--credential-path', required=True)
        else:
            command.add_argument('--offline-endpoint', required=True)
    args = parser.parse_args()
    if args.operation == 'prepare':
        from nilaris.semantic172_sources import capture_sources
        sources = json.loads(Path(args.sources).read_bytes())
        evidence = json.loads(Path(args.evidence_pins).read_bytes()) if args.evidence_pins else None
        sink = prepare(args.root, capture_sources(sources), [s['document_id'] for s in sources],
                       mode=args.mode, evidence_pins=evidence, sources=sources)
        print(canonical(dict(freeze_pin=digest(sink.read('freeze.json')), configuration=configuration())).decode())
    elif args.operation in ('status', 'preflight'):
        sink = Ledger(args.root, mode=args.mode)
        sink.verify()
        if args.operation == 'preflight' and args.mode == 'live':
            check_authorization(sink)
        print(canonical(dict(freeze_pin=digest(sink.read('freeze.json')), **sink.status())).decode())
    else:
        import stat
        from nilaris.admission import Ingestion, AdmissionProfile
        path = Path(args.database_config)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError('private explicit database configuration required')
        database = json.loads(path.read_bytes())
        ingestion = Ingestion(database, AdmissionProfile.l1(), semantic172=True)
        result = run(ingestion, args.job_id, args.revision, artifact_root=args.root,
            target_document_id=args.target, expected_freeze_sha256=args.freeze_pin,
            mode='live' if args.operation == 'dispatch-live' else 'offline',
            credential_path=getattr(args, 'credential_path', None),
            offline_endpoint=getattr(args, 'offline_endpoint', None))
        print(canonical(result).decode())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('semantic172 operation failed; no retry or repair performed') from None
