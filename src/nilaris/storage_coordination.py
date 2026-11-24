"""Task-local exclusion between physical storage scans and test DB lifecycle."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time


STORAGE_LOCK = Path('/private/tmp/nilaris-a1-setup-02/postgres-storage.lock')


def _validate(descriptor, path):
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise RuntimeError('invalid storage coordination lock')
    current = os.stat(path, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise RuntimeError('storage coordination lock replaced')


def _acquire(descriptor, operation, deadline):
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError('storage coordination deadline reached')
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(min(0.005, max(0, deadline - time.monotonic())))


@contextmanager
def storage_lock(*, exclusive, deadline, path=STORAGE_LOCK):
    """Acquire the existing private turnstile, then storage, in one budget.

    The operator creates both files once while all participants are stopped.
    Never replace either inode during a session. An admitted writer holds the
    turnstile while existing scans finish, preventing the next scan bypassing it.
    This is precedence after admission, not FIFO scheduling fairness.
    """
    turnstile = Path(str(path) + '.turnstile')
    gate = os.open(turnstile, os.O_RDWR | os.O_NOFOLLOW)
    try:
        _validate(gate, turnstile)
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            _validate(descriptor, path)
            _acquire(gate, fcntl.LOCK_EX, deadline)
            _validate(gate, turnstile)
            _acquire(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH, deadline)
            # Both identities/metadata must still be valid after waiting. Missing,
            # unlink/replacement and incompatible setup fail closed, never fallback.
            _validate(descriptor, path)
            _validate(gate, turnstile)
            if time.monotonic() >= deadline:
                raise RuntimeError('storage coordination deadline reached')
            os.close(gate)
            gate = None
            yield
        finally:
            os.close(descriptor)
    finally:
        if gate is not None:
            os.close(gate)
