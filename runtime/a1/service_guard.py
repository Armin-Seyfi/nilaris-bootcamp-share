"""Start only the task Compose project; stop it at deadline or monitored limit.

This does not implement application admission/worker controls. No volume deletion.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from nilaris.service_monitor import publish_service_monitor
from nilaris.storage_coordination import storage_lock

ROOT = Path(__file__).resolve().parent
PRIVATE = Path('/private/tmp/nilaris-a1-setup-02')
COMPOSE = ['docker', 'compose', '--env-file', str(PRIVATE / 'secrets.env'), '-f', str(ROOT / 'compose.yaml')]

def call(args, timeout=10):
    return subprocess.check_output(args, text=True, timeout=timeout, stderr=subprocess.STDOUT).strip()

def timed_probe(timings, label, submitted, operation, *args):
    started = time.monotonic()
    try:
        return operation(*args)
    finally:
        timings[label] = (submitted, started, time.monotonic())

def du_probe(service, path):
    started = time.monotonic()
    try:
        return call([*COMPOSE, 'exec', '-T', service, 'du', '-sk', path])
    except subprocess.CalledProcessError as error:
        ended = time.monotonic()
        output = error.output if isinstance(error.output, str) else ''
        diagnostics = []
        known_parts = {'var', 'lib', 'postgresql', 'docker', 'base', 'global',
                       'pg_wal', 'archive_status', 'pg_tblspc', 'pg_stat_tmp',
                       'pgsql_tmp', 'home', 'node', '.n8n'}
        known_errors = {'No such file or directory': 'missing',
                        'Permission denied': 'permission',
                        'Input/output error': 'io'}
        # Never echo generic Compose output. Only classify exact du lines and
        # retain known directory names/numeric storage identities from paths.
        for line in output[:8192].splitlines():
            match = re.fullmatch(r"du: (?:cannot access |cannot read directory )?['‘]([^'’\n]+)['’]: (.+)", line)
            if match is None:
                match = re.fullmatch(r'du: (/[^\n]+): ([^\n]+)', line)
            if match is None:
                continue
            parts = match[1].split('/')[:16]
            safe_path = '/'.join(part if part in known_parts or re.fullmatch(
                r'[0-9]{1,24}(?:[._][0-9]{1,24}|_(?:fsm|vm|init))?|[0-9A-F]{24}', part)
                else '<redacted>' for part in parts if part)
            leaf_kind = 'other'
            pg_path = re.fullmatch(
                r'/var/lib/postgresql/[0-9]+/docker/(base/[0-9]+|base/pgsql_tmp|global)/([^/]+)', match[1])
            if service == 'postgres' and pg_path is not None:
                context, leaf = pg_path.groups()
                if context != 'base/pgsql_tmp' and leaf == 'pg_internal.init':
                    leaf_kind = 'relcache_init'
                elif context != 'base/pgsql_tmp' and re.fullmatch(r'pg_internal\.init\.[0-9]+', leaf):
                    leaf_kind = 'relcache_init_temp'
                elif context == 'base/pgsql_tmp' and re.fullmatch(r'pgsql_tmp[0-9]+\.[0-9]+', leaf):
                    leaf_kind = 'query_temp'
                elif context.startswith('base/') and context != 'base/pgsql_tmp':
                    if re.fullmatch(r't[0-9]+_[0-9]+(?:_(?:fsm|vm|init))?(?:\.[0-9]+)?', leaf):
                        leaf_kind = 'temporary_relation'
                    elif re.fullmatch(r'[0-9]+(?:_(?:fsm|vm|init))?(?:\.[0-9]+)?', leaf):
                        leaf_kind = 'numeric_relation'
            diagnostics.append({'path': '/' + safe_path,
                                'leaf_kind': leaf_kind,
                                'error': known_errors.get(match[2], 'unclassified')})
            if len(diagnostics) == 4:
                break
        print('Physical storage probe failed: ' + json.dumps(dict(
            phase='du_failed', service=service, exit=error.returncode,
            started=round(started, 6), ended=round(ended, 6),
            elapsed=round(ended - started, 6), output_chars=len(output),
            output_lines=output.count('\n') + bool(output), diagnostics=diagnostics
        ), sort_keys=True), file=sys.stderr, flush=True)
        raise

def select_runtime(mode, compose_owner=None):
    """Only this checkout or the explicitly retained original01 owns this project."""
    owner = Path(compose_owner) if compose_owner else ROOT
    if str(owner) not in (str(ROOT), '/private/tmp/nilaris-a1-01/runtime/a1'):
        raise ValueError('unrecognized Compose source owner')
    if mode not in ('all', 'postgres'):
        raise ValueError('unknown service selection')
    return dict(owner=str(owner), services=['postgres'] if mode == 'postgres' else ['postgres','n8n'],
                ports=[55432] if mode == 'postgres' else [55432,5679])


def retained_n8n_probe():
    """Create a bounded inert reader for the exact retained task volume, never n8n."""
    volume = 'nilaris-synth_n8ndata'
    labels = json.loads(call(['docker', 'volume', 'inspect', '--format', '{{json .Labels}}', volume]))
    if (not isinstance(labels, dict) or labels.get('com.docker.compose.project') != 'nilaris-synth'
            or labels.get('com.docker.compose.volume') != 'n8ndata'):
        raise RuntimeError('retained n8n volume ownership unavailable')
    image = 'postgres@sha256:a8a18afebc8b2b529112c931f193151b712e566b7a49563dc0f328d51f652087'
    return call(['docker', 'create', '--pull', 'never', '--restart', 'no',
                 '--label', 'nilaris.guard=transition167-retained-reader',
                 '--network', 'none', '--read-only', '--memory', '64m',
                 '--cpus', '0.1', '--pids-limit', '16', '--log-driver', 'none',
                 '--mount', 'type=volume,src=' + volume + ',dst=/retained,readonly',
                 '--entrypoint', 'sleep', image, '1800'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--services', choices=('all','postgres'), default='all')
    parser.add_argument('--compose-owner')
    args = parser.parse_args()
    selection = select_runtime(args.services, args.compose_owner)
    global COMPOSE
    COMPOSE = ['docker', 'compose', '--env-file', str(PRIVATE / 'secrets.env'),
               '-f', str(Path(selection['owner']) / 'compose.yaml')]
    selected = selection['services']
    if not 1 <= args.seconds <= 1800:
        raise SystemExit('duration must be 1..1800 seconds')
    # Reject unrelated same-project containers before any mutating Compose call.
    existing = call(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=nilaris-synth']).splitlines()
    for cid in existing:
        owner = call(['docker', 'inspect', '--format', '{{index .Config.Labels "com.docker.compose.project.working_dir"}}', cid])
        if owner != selection['owner']:
            raise SystemExit('same-project container has another source owner; stop')
    if args.services == 'postgres':
        retained_readers = call(['docker', 'ps', '-aq', '--filter',
                                'label=nilaris.guard=transition167-retained-reader'])
        if retained_readers:
            raise SystemExit('prior retained-storage reader requires reconciliation; stop')
        running = call([*COMPOSE, 'ps', '--services', '--status', 'running']).splitlines()
        if any(service != 'postgres' for service in running):
            raise SystemExit('unselected task service is running; stop before renewal')
    for port in selection['ports']:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    if shutil.disk_usage(PRIVATE).free < 2 * 1024**3:
        raise SystemExit('host free space below L1 minimum')
    started = time.monotonic()
    ids = []
    helper = None
    try:
        publish_service_monitor('starting')
        if args.services == 'postgres':
            helper = retained_n8n_probe()
            call(['docker', 'start', helper])
        # Atomic real bind remains Docker's responsibility; failure stops this run.
        call([*COMPOSE, 'up', '-d', '--wait', '--wait-timeout', '90', '--pull', 'never', *selected], timeout=100)
        ids = call([*COMPOSE, 'ps', '-aq']).splitlines()
        if helper:
            ids.append(helper)
        print('Task services started; guarded deadline', args.seconds, 'seconds', flush=True)
        while time.monotonic() - started < args.seconds:
            cycle = time.monotonic()
            services = [('postgres', '/var/lib/postgresql', 2*1024**3), ('n8n', '/home/node/.n8n', 256*1024**2)]
            # Independent read-only probes run together so CLI startup does not
            # serialize the one-second physical sampling window.
            timings = {}
            with storage_lock(exclusive=False, deadline=cycle + 1), ThreadPoolExecutor(max_workers=3) as probes:
                acquired = time.monotonic()
                storage = [probes.submit(timed_probe, timings, service + '_du',
                                        time.monotonic(), call,
                                        ['docker', 'exec', helper, 'du', '-sk', '/retained'])
                           if helper and service == 'n8n' else
                           probes.submit(timed_probe, timings, service + '_du',
                                        time.monotonic(), du_probe, service, path)
                           for service, path, _ in services]
                layers = probes.submit(timed_probe, timings, 'writable_layers',
                                       time.monotonic(), call,
                                       ['docker', 'inspect', '--size', '--format', '{{.SizeRw}}', *ids])
                sizes = [int(probe.result().split()[0]) * 1024 for probe in storage]
                writable = sum(int(size) for size in layers.result().splitlines())
            probed = time.monotonic()
            for (service, _, ceiling), size in zip(services, sizes):
                if size >= ceiling:
                    raise RuntimeError(f'{service} storage ceiling reached')
            logs = sum(p.stat().st_size for p in PRIVATE.glob('*.log'))
            if logs + 32*1024**2 >= 64*1024**2:
                raise RuntimeError('task logs ceiling reached')
            if shutil.disk_usage(PRIVATE).free < 2*1024**3:
                raise RuntimeError('host free space below minimum')
            # Reserve2GiB for images, well above the inspected479145441 bytes.
            if sum(sizes) + writable + logs + 32*1024**2 + sum(p.stat().st_size for p in PRIVATE.rglob('*') if p.is_file()) >= 6*1024**3:
                raise RuntimeError('task allocation guard reached (2GiB reserved for images)')
            completed = time.monotonic()
            if completed - cycle > 1:
                print('Storage cycle missed: '
                      f'lock_wait={acquired - cycle:.6f}s '
                      f'probes={probed - acquired:.6f}s '
                      f'post_probe={completed - probed:.6f}s '
                      f'total={completed - cycle:.6f}s', file=sys.stderr, flush=True)
                print('Storage cycle probe timings: ' + json.dumps(dict(
                    cycle_start=round(cycle, 6),
                    executor_gap=round(probed - max(value[2] for value in timings.values()), 6),
                    probes={label: dict(queue=round(started - submitted, 6),
                                        start=round(started - cycle, 6),
                                        duration=round(ended - started, 6),
                                        end=round(ended - cycle, 6))
                            for label, (submitted, started, ended) in timings.items()}
                ), sort_keys=True), file=sys.stderr, flush=True)
                raise RuntimeError('service storage monitor missed its one-second target; stop task services')
            publish_service_monitor('ready')
            # Start the next sample immediately. Sleeping to the previous cycle's
            # start lets a slower next probe leave the ready signal older than
            # one second even when both probes meet their individual deadline.
        print('Guarded session deadline reached', flush=True)
    finally:
        try:
            publish_service_monitor('stopped')
        finally:
            # No down -v: demonstration and diagnostic evidence survive. Signal
            # publication failure must never prevent scoped service shutdown.
            try:
                subprocess.run([*COMPOSE, 'stop', '-t', '2', *selected], check=True, timeout=20)
            finally:
                if helper:
                    subprocess.run(['docker', 'rm', '-f', helper], check=True, timeout=10)
            print('Task services stopped; volumes retained', flush=True)

if __name__ == '__main__':
    main()
