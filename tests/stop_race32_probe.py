"""Diagnostic OS-boundary ledger; run only under an explicit serial test lease.

All OS operations remain real. Extra identity-only ps samples may affect timing;
this probe supplements, rather than replaces, the uninstrumented reproduction.
"""
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pytest


events = deque(maxlen=64)
groups = set()
last_fields = {}
real_popen = subprocess.Popen
real_run = subprocess.run
real_wait4 = os.wait4
started = time.monotonic()
pause_before_first_reap = False
pause_used = False
trace_path = None


def record(kind, **fields):
    if kind != 'production_inventory' and last_fields.get(kind) == fields:
        return
    last_fields[kind] = fields
    events.append(dict(kind=kind, at=round(time.monotonic() - started, 6), **fields))


def popen(*args, **kwargs):
    command = args[0] if args else kwargs.get('args')
    controlled = (isinstance(command, list) and len(command) > 3 and
                  command[:2] == ['/bin/sh', '-c'] and
                  command[3] == 'nilaris-controlled-bootstrap')
    if controlled and trace_path is not None:
        copied = list(command)
        bootstrap = copied[2]
        substitutions = (
            ('    wait "$soft_sleeper"\n',
             '    wait "$soft_sleeper"\n'
             '    printf \'soft_wait %s %s\\n\' "$soft_sleeper" "$?" >> "$NILARIS_STOP_RACE32_TRACE"\n'),
            ('    wait "$hard_sleeper"\n',
             '    wait "$hard_sleeper"\n'
             '    nilaris_trace_hard_status=$?\n'
             '    printf \'hard_wait %s %s\\n\' "$hard_sleeper" "$nilaris_trace_hard_status" >> "$NILARIS_STOP_RACE32_TRACE"\n'),
            ('    trap \'if [ -n "$soft_sleeper" ];',
             '    trap \'printf "cancel %s %s\\n" "$$" "0" >> "$NILARIS_STOP_RACE32_TRACE"; if [ -n "$soft_sleeper" ];'),
        )
        for old, new in substitutions:
            if bootstrap.count(old) != 1:
                raise RuntimeError('diagnostic bootstrap version mismatch')
            bootstrap = bootstrap.replace(old, new, 1)
        copied[2] = bootstrap
        if args:
            args = (copied, *args[1:])
        else:
            kwargs['args'] = copied
        kwargs['env'] = dict(kwargs.get('env') or os.environ,
                             NILARIS_STOP_RACE32_TRACE=str(trace_path))
    process = real_popen(*args, **kwargs)
    if controlled:
        groups.add(process.pid)
        record('controlled_launch', pgid=process.pid)
    return process


def run(*args, **kwargs):
    result = real_run(*args, **kwargs)
    command = args[0] if args else kwargs.get('args')
    if command != ['/bin/ps', '-axo', 'pid=,pgid=,rss=,time=,lstart='] or not groups:
        return result
    members = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and int(fields[1]) in groups:
            members.append(dict(pid=int(fields[0]), pgid=int(fields[1]), start=fields[4][:80]))
            if len(members) == 16:
                break
    record('production_inventory', members=members)
    # Record executable identity only, never command arguments or environment.
    try:
        identities = real_run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,stat=,comm='],
                              capture_output=True, text=True, timeout=1, check=True)
        related = []
        for line in identities.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5 and int(fields[2]) in groups:
                related.append(dict(pid=int(fields[0]), ppid=int(fields[1]),
                                    pgid=int(fields[2]), state=fields[3][:8],
                                    executable=Path(fields[4]).name[:80]))
                if len(related) == 16:
                    break
        record('diagnostic_identity', members=related)
    except (OSError, subprocess.SubprocessError):
        record('diagnostic_inventory_unavailable')
    return result


def wait4(pid, options):
    global pause_used
    if pid in groups and pause_before_first_reap and not pause_used:
        pause_used = True
        record('scheduling_pause_begin', pid=pid, seconds=1.2)
        time.sleep(1.2)
        record('scheduling_pause_end', pid=pid)
    result = real_wait4(pid, options)
    if pid in groups and result[0]:
        record('controlled_reaped', pid=pid, status=result[1])
    return result


if __name__ == '__main__':
    arguments = sys.argv[1:]
    trace_directory = None
    if '--trace-bootstrap-waits' in arguments:
        arguments.remove('--trace-bootstrap-waits')
        trace_directory = tempfile.TemporaryDirectory(prefix='nilaris-stop-race32-')
        trace_path = Path(trace_directory.name) / 'bootstrap.trace'
        descriptor = os.open(trace_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    if '--pause-before-first-reap' in arguments:
        arguments.remove('--pause-before-first-reap')
        pause_before_first_reap = True
    subprocess.Popen = popen
    subprocess.run = run
    os.wait4 = wait4
    try:
        outcome = pytest.main(['-q', '-s', '--tb=short', *arguments])
    finally:
        subprocess.Popen = real_popen
        subprocess.run = real_run
        os.wait4 = real_wait4
        for event in events:
            print('[stop-race32-os] ' + json.dumps(event, sort_keys=True), flush=True)
        if trace_path is not None:
            with trace_path.open('rb') as captured:
                raw = captured.read(1025)
            parsed = []
            for line in raw.splitlines():
                fields = line.split()
                if (len(raw) > 1024 or len(fields) != 3 or
                        fields[0] not in (b'soft_wait', b'hard_wait', b'cancel') or
                        not all(field.isdigit() and len(field) <= 10 for field in fields[1:])):
                    parsed = [dict(event='invalid_bounded_trace')]
                    break
                parsed.append(dict(event=fields[0].decode('ascii'), pid=int(fields[1]), status=int(fields[2])))
            for event in parsed:
                print('[stop-race32-shell] ' + json.dumps(event, sort_keys=True), flush=True)
            trace_directory.cleanup()
    raise SystemExit(outcome)
