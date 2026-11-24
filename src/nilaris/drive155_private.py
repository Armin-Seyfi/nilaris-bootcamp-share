"""Shared private JSON custody boundary; callers supply their generic errors."""
import json
import os
from pathlib import Path
import stat

PRIVATE_JSON_LIMIT = 65536


def read_private_json(path, repository_root):
    """Read one owner0600 nonsymlink dictionary outside the repository, bounded."""
    path = Path(path)
    if (not path.is_absolute() or path != path.resolve(strict=True)
            or path.is_relative_to(repository_root)):
        raise ValueError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_size > PRIVATE_JSON_LIMIT):
            raise ValueError
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(PRIVATE_JSON_LIMIT + 1)
    finally:
        os.close(fd)
    if len(raw) > PRIVATE_JSON_LIMIT:
        raise ValueError

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    result = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError
    return result
