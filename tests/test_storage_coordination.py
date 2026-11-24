"""Public storage coordination with real file locks and isolated private files."""
import multiprocessing as mp
from pathlib import Path
import threading
import time

import pytest

from nilaris.storage_coordination import storage_lock


@pytest.fixture
def lock_path(tmp_path):
    path = tmp_path / 'storage.lock'
    path.touch(mode=0o600)
    Path(str(path) + '.turnstile').touch(mode=0o600)
    return path


def continuous_scan(path, active, advance, reacquired, stop):
    try:
        while not stop.is_set():
            with storage_lock(exclusive=False, deadline=time.monotonic() + 2, path=path):
                active.set()
                reacquired.set()
                advance.wait(2)
                advance.clear()
                active.clear()
    except RuntimeError:
        if not stop.is_set():
            raise


def test_admitted_writer_progresses_between_continuous_scans(lock_path, monkeypatch):
    """Pin scan gaps between writer polls, the previously starving schedule."""
    context = mp.get_context('fork')
    active, advance, reacquired, stop = [context.Event() for _ in range(4)]
    reader = context.Process(target=continuous_scan,
                             args=(lock_path, active, advance, reacquired, stop))
    reader.start()
    assert reacquired.wait(1)
    identities = [p.stat().st_ino for p in (lock_path, Path(str(lock_path) + '.turnstile'))]
    sleep = time.sleep

    def adverse_schedule(seconds):
        reacquired.clear()
        advance.set()
        # Before the fix the reader always gets back in before the next poll.
        # An admitted writer now blocks that acquisition, so this wait expires.
        reacquired.wait(.02)
        sleep(seconds)

    monkeypatch.setattr('nilaris.storage_coordination.time.sleep', adverse_schedule)
    try:
        with storage_lock(exclusive=True, deadline=time.monotonic() + .5, path=lock_path):
            assert not active.is_set(), 'scan and lifecycle overlapped'
    finally:
        stop.set()
        advance.set()
        reader.join(3)
        if reader.is_alive():
            reader.kill()
            reader.join()
    assert reader.exitcode == 0
    assert identities == [p.stat().st_ino for p in (lock_path, Path(str(lock_path) + '.turnstile'))]


@pytest.mark.parametrize('exclusive', [False, True])
@pytest.mark.parametrize('blocked_file', ['storage', 'turnstile'])
def test_contention_expires_without_entering_body_and_releases_descriptors(lock_path, exclusive, blocked_file):
    import fcntl
    import os

    blocked = lock_path if blocked_file == 'storage' else Path(str(lock_path) + '.turnstile')
    descriptor = os.open(blocked, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='deadline reached'):
            with storage_lock(exclusive=exclusive, deadline=started + .1, path=lock_path):
                pytest.fail('expired contender entered body')
    finally:
        os.close(descriptor)
    assert .09 <= time.monotonic() - started < .4
    with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
        pass


def test_both_waits_spend_one_absolute_deadline(lock_path):
    import fcntl
    import os

    gate = os.open(str(lock_path) + '.turnstile', os.O_RDWR)
    storage = os.open(lock_path, os.O_RDWR)
    fcntl.flock(gate, fcntl.LOCK_EX)
    fcntl.flock(storage, fcntl.LOCK_EX)
    release = threading.Timer(.12, os.close, args=(gate,))
    release.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='deadline reached'):
            with storage_lock(exclusive=True, deadline=started + .2, path=lock_path):
                pytest.fail('contender entered body')
    finally:
        release.join()
        os.close(storage)
    assert .19 <= time.monotonic() - started < .3
    with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
        pass


@pytest.mark.parametrize('file', ['storage', 'turnstile'])
@pytest.mark.parametrize('fault', ['missing', 'public', 'hardlink', 'symlink', 'directory'])
def test_incompatible_coordination_setup_fails_closed(lock_path, file, fault):
    import os

    target = lock_path if file == 'storage' else Path(str(lock_path) + '.turnstile')
    if fault == 'public':
        target.chmod(0o644)
    elif fault == 'hardlink':
        os.link(target, target.with_name('alias'))
    else:
        target.unlink()
        if fault == 'symlink':
            other = target.with_name('other')
            other.touch(mode=0o600)
            target.symlink_to(other)
        elif fault == 'directory':
            target.mkdir(mode=0o600)
    with pytest.raises((OSError, RuntimeError)):
        with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
            pytest.fail('invalid setup entered body')
    if fault == 'missing':
        assert not target.exists(), 'live acquisition auto-created missing proof'


@pytest.mark.parametrize('file', ['storage', 'turnstile'])
@pytest.mark.parametrize('fault', ['replace', 'permissions', 'hardlink'])
def test_proof_changed_while_waiting_is_refused(lock_path, monkeypatch, file, fault):
    import fcntl
    import os

    target = lock_path if file == 'storage' else Path(str(lock_path) + '.turnstile')
    # Force a genuine wait on the original inode; the gate is already admitted.
    descriptor = os.open(lock_path, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_SH)
    changed = False

    def change_proof(seconds):
        nonlocal changed
        if not changed:
            changed = True
            if fault == 'replace':
                target.rename(target.with_name(target.name + '.old'))
                target.touch(mode=0o600)
            elif fault == 'permissions':
                target.chmod(0o644)
            else:
                os.link(target, target.with_name('alias'))
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    monkeypatch.setattr('nilaris.storage_coordination.time.sleep', change_proof)
    try:
        with pytest.raises(RuntimeError, match='invalid|replaced'):
            with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
                pytest.fail('changed proof entered body')
        assert changed
    finally:
        os.close(descriptor)


def test_thread_failure_releases_lock_and_other_threads_are_excluded(lock_path):
    entered = threading.Event()
    release = threading.Event()

    def failing_holder():
        try:
            with storage_lock(exclusive=False, deadline=time.monotonic() + 1, path=lock_path):
                entered.set()
                release.wait(1)
                raise ValueError('controlled thread failure')
        except ValueError:
            pass

    thread = threading.Thread(target=failing_holder)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(RuntimeError, match='deadline reached'):
            with storage_lock(exclusive=True, deadline=time.monotonic() + .05, path=lock_path):
                pytest.fail('thread holder overlapped writer')
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
        pass


def crash_holder(path, entered):
    import os

    with storage_lock(exclusive=True, deadline=time.monotonic() + 1, path=path):
        entered.set()
        os._exit(91)


def test_process_crash_releases_storage_and_gate(lock_path):
    context = mp.get_context('fork')
    entered = context.Event()
    process = context.Process(target=crash_holder, args=(lock_path, entered))
    process.start()
    assert entered.wait(1)
    process.join(1)
    assert process.exitcode == 91
    with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
        pass


def waiting_writer(path, started):
    started.set()
    with storage_lock(exclusive=True, deadline=time.monotonic() + 5, path=path):
        pass


def test_killed_writer_releases_gate_while_waiting_for_existing_scan(lock_path):
    import fcntl
    import os

    context = mp.get_context('spawn')
    started = context.Event()
    # Spawn prevents inheritance of the parent's existing scan descriptor.
    process = context.Process(target=waiting_writer, args=(lock_path, started))
    with storage_lock(exclusive=False, deadline=time.monotonic() + 1, path=lock_path):
        process.start()
        assert started.wait(1)
        gate = os.open(str(lock_path) + '.turnstile', os.O_RDWR)
        try:
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(gate, fcntl.LOCK_UN)
                except BlockingIOError:
                    break
                assert time.monotonic() < deadline, 'writer never entered turnstile'
                time.sleep(.005)
            process.kill()
            process.join(1)
            assert process.exitcode != 0
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(gate)
            if process.is_alive():
                process.kill()
                process.join()
    with storage_lock(exclusive=True, deadline=time.monotonic() + .2, path=lock_path):
        pass


def test_postacquisition_validation_cannot_admit_body_after_deadline(lock_path, monkeypatch):
    import os

    original_stat = os.stat
    reads = 0

    def delayed_stat(path, *args, **kwargs):
        nonlocal reads
        result = original_stat(path, *args, **kwargs)
        if Path(path) == lock_path:
            reads += 1
            if reads == 2:
                time.sleep(.04)
        return result

    monkeypatch.setattr(os, 'stat', delayed_stat)
    with pytest.raises(RuntimeError, match='deadline reached'):
        with storage_lock(exclusive=True, deadline=time.monotonic() + .02, path=lock_path):
            pytest.fail('postacquisition validation exhausted budget before body')
