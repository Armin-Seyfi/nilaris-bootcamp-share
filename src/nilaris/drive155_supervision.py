"""Explicit source process boundary; no credentials, provider defaults or DB access.

Only a single-threaded caller may fork. A separate group leader monitors the
provider grandchild while the caller durably records its identity. Failure to
prove full accounting, EOF and reaping never authorizes a captured-input commit.
"""
import base64
import json
import math
import os
import resource
import select
import signal
import threading
import time

from nilaris.worker_lifecycle import processes as lifecycle_processes

PIPE_LIMIT = 131072


def processes(pgid):
    # The fixed inventory utility is synchronously reaped by the helper and
    # runs in its own session. It must not appear as a provider descendant in
    # the inventory it produces. No unknown group member is excluded.
    return lifecycle_processes(pgid, isolate_inventory=True)


def _encode(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _read(fd, data):
    chunk = os.read(fd, min(16384, PIPE_LIMIT + 1 - len(data)))
    data.extend(chunk)
    if len(data) > PIPE_LIMIT:
        raise ValueError('bounded source pipe')
    return not chunk


def _write(fd, value):
    raw = _encode(value)
    if len(raw) > PIPE_LIMIT:
        raise ValueError('bounded source pipe')
    while raw:
        raw = raw[os.write(fd, raw):]


def _kill_group(signum, frame):
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _monitor(provider, gate, output, wall, cpu_limit, rss_limit):
    os.setsid()
    signal.signal(signal.SIGALRM, _kill_group)
    signal.setitimer(signal.ITIMER_REAL, wall)
    resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(cpu_limit), math.ceil(cpu_limit)))
    started = time.monotonic()
    # This clock starts before waiting on the durable identity gate. Parent loss
    # cannot leave an ungated provider or an unbounded monitor behind.
    if not select.select([gate], [], [], wall)[0] or os.read(gate, 1) != b'1':
        os._exit(2)
    os.close(gate)
    reading, writing = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reading)
        os.close(output)
        try:
            value = provider()
            value = ({'bytes': base64.b64encode(value).decode('ascii')}
                     if isinstance(value, bytes) else {'json': value})
            _write(writing, value)
            os.close(writing)
            os._exit(0)
        except BaseException:
            # Provider text, response bodies and credentials never leave worker.
            os._exit(3)
    os.close(writing)
    data = bytearray()
    eof = False
    usage = None
    samples = {}
    peak = 0
    code = None
    while True:
        members = processes(os.getpgrp())
        for member in members:
            key = member['pid'], member['start']
            samples[key] = max(samples.get(key, 0), member['cpu'])
        peak = max(peak, sum(member['rss'] for member in members))
        if (time.monotonic() - started >= wall or sum(samples.values()) >= cpu_limit
                or peak >= rss_limit):
            _kill_group(None, None)
        if not eof and select.select([reading], [], [], .025)[0]:
            eof = _read(reading, data)
        if usage is None:
            pid, code, measured = os.wait4(child, os.WNOHANG)
            if pid:
                usage = measured
        if usage is not None and eof:
            # Grandchildren keeping inherited descriptors/group membership do
            # not become a successful returned-call proof.
            if any(member['pid'] != os.getpid() for member in processes(os.getpgrp())):
                _kill_group(None, None)
            cpu = max(sum(samples.values()), usage.ru_utime + usage.ru_stime + time.process_time())
            value = json.loads(data) if os.waitstatus_to_exitcode(code) == 0 else None
            _write(output, dict(value=value, wall_seconds=time.monotonic() - started,
                               cpu_seconds=cpu, peak_rss_bytes=peak, provider_returned=value is not None))
            os.close(reading)
            os.close(output)
            os._exit(0)


class SupervisedSource:
    """Run one explicitly supplied source call with a durable pre-dispatch gate."""
    def __init__(self, *, test_wall_seconds=30, test_cpu_seconds=20,
                 test_rss_bytes=1024**3):
        if (type(test_wall_seconds) not in (int, float) or not 0 < test_wall_seconds <= 30
                or type(test_cpu_seconds) not in (int, float) or not 0 < test_cpu_seconds <= 20
                or type(test_rss_bytes) is not int or not 0 < test_rss_bytes <= 1024**3):
            raise ValueError('invalid source supervision allowance')
        self.wall, self.cpu, self.rss = test_wall_seconds, test_cpu_seconds, test_rss_bytes

    def run(self, provider, identity_gate, heartbeat=lambda: True):
        failed = dict(proved=False, value=None, evidence='source_accounting_unknown',
                      wall_seconds=self.wall, cpu_seconds=self.cpu, peak_rss_bytes=self.rss)
        if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
            return failed
        reading, writing = os.pipe()
        gate_read, gate_write = os.pipe()
        started = time.monotonic()
        pid = os.fork()
        if pid == 0:
            os.close(reading)
            os.close(gate_write)
            try:
                _monitor(provider, gate_read, writing, self.wall, self.cpu, self.rss)
            except BaseException:
                os._exit(4)
        os.close(writing)
        os.close(gate_read)
        identity = None
        observed = {}
        data = bytearray()
        eof = False
        reaped = False
        code = None
        gated = False
        result = None
        try:
            while time.monotonic() - started < self.wall + 1:
                members = processes(pid)
                observed.update({m['pid']: m['start'] for m in members})
                if identity is None:
                    identity = next((m for m in members if m['pid'] == pid), None)
                    if identity is not None:
                        if not identity_gate(dict(identity)):
                            break
                        os.write(gate_write, b'1')
                        os.close(gate_write)
                        gate_write = None
                        gated = True
                if gated and not heartbeat():
                    break
                if not eof and select.select([reading], [], [], .025)[0]:
                    eof = _read(reading, data)
                if not reaped:
                    found, code, usage = os.wait4(pid, os.WNOHANG)
                    reaped = bool(found)
                if reaped and eof:
                    if code == 0 and not processes(pid):
                        result = json.loads(data)
                    break
        except Exception:
            result = None
        finally:
            if gate_write is not None:
                os.close(gate_write)
            # Signal only a current matched leader, or individually observed
            # survivors after reaping. A recycled PID/group is never authority.
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                try:
                    members = processes(pid)
                    if not reaped:
                        found, code, usage = os.wait4(pid, os.WNOHANG)
                        if found:
                            reaped = True
                            continue
                    if reaped and not members:
                        break
                    leader = next((m for m in members if m['pid'] == pid), None)
                    safe = (leader and identity and leader['start'] == identity['start']) or (
                        reaped and members and all(observed.get(m['pid']) == m['start'] for m in members))
                    if not safe:
                        break
                    os.killpg(pid, signal.SIGKILL)
                    time.sleep(.025)
                except Exception:
                    break
            os.close(reading)
        try:
            empty = not processes(pid)
        except Exception:
            empty = False
        if not result or not reaped or not empty:
            return failed
        try:
            wrapped = result['value']
            value = (base64.b64decode(wrapped['bytes'], validate=True) if wrapped and 'bytes' in wrapped
                     else wrapped['json'] if wrapped else None)
            wall = max(result['wall_seconds'], time.monotonic() - started)
            cpu = max(result['cpu_seconds'], usage.ru_utime + usage.ru_stime)
            rss = result['peak_rss_bytes']
            if (not all(type(x) in (int, float) and math.isfinite(x) for x in (wall, cpu, rss))
                    or not 0 <= wall < self.wall or not 0 <= cpu < self.cpu or not 0 <= rss < self.rss):
                return failed
            return dict(proved=True, value=value, evidence='process_group_reaped', identity=identity,
                        wall_seconds=wall, cpu_seconds=cpu, peak_rss_bytes=rss)
        except (KeyError, ValueError, TypeError):
            return failed
