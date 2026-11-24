"""Task-local, fail-closed admission signal; physical limits remain sampled."""
import json
import os
from pathlib import Path
import time

STATUS_PATH = Path('/private/tmp/nilaris-a1-setup-02/service-monitor.json')


def service_monitor_ready(path=STATUS_PATH):
    try:
        with Path(path).open('rb') as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            return False
        status = json.loads(raw)
        if not isinstance(status, dict) or status.get('state') != 'ready':
            return False
        pid, sampled = status.get('pid'), status.get('sampled_at')
        if type(pid) is not int or pid <= 0 or type(sampled) not in (int, float):
            return False
        if not 0 <= time.monotonic() - sampled <= 1:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError, OverflowError):
        return False


def publish_service_monitor(state, path=STATUS_PATH):
    """Replace a complete private signal atomically; never include configuration."""
    path = Path(path)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        with temporary.open('w') as target:
            os.chmod(temporary, 0o600)
            json.dump(dict(state=state, pid=os.getpid(), sampled_at=time.monotonic()), target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
