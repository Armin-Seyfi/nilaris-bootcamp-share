"""Same-host customer dispatch ordering survives loss of a PostgreSQL session."""
from contextlib import contextmanager
import fcntl
import os
import re
import stat
import time


@contextmanager
def dispatch_gate(directory, customer):
    if not isinstance(customer, str) or not re.fullmatch('[0-9a-f]{32}', customer):
        raise PermissionError('invalid customer dispatch gate')
    path = directory / (customer + '.dispatch.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('customer dispatch ordering unavailable') from None
                time.sleep(0.005)
        info = os.fstat(fd)
        current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                info.st_uid != os.getuid() or info.st_nlink != 1 or
                (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise PermissionError('invalid customer dispatch gate')
        yield
    finally:
        os.close(fd)
