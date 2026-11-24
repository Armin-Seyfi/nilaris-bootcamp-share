"""Send the pinned Ticket05 fixture to a loopback receiver with one simulated slot."""
import argparse
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import stat
import subprocess


REQUEST_SHA256 = "4c7899e2143b28e024b5ae473ee54c2ecab50918215de03e6ad38ec5f8e20109"
REHEARSAL_SETTINGS = {
    "PROVIDER": "openai",
    "API_PROJECT": "NILARIS Semantic Experiment",
    "MODEL": "gpt-5.4-2026-03-05",
    "EFFECTIVE_TIER": "standard",
    "MAX_OUTPUT_TOKENS": "256",
    "ATTEMPT_LIMIT": "1",
    "RETRY_LIMIT": "0",
    "USD_CEILING": "0.03",
}


def load_validated_rehearsal_key():
    # Discover the checkout from cwd, using its own index and normal ignore files.
    git_env = {name: os.environ[name] for name in ("HOME", "XDG_CONFIG_HOME") if name in os.environ}
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", ".env"],
                             env=git_env, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if ignored.returncode:
        raise ValueError
    fd = os.open(".env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        values = {}
        for line in stream:
            line = line.removesuffix("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator or name in values:
                raise ValueError
            values[name] = value
    if any(values.get(name) != expected for name, expected in REHEARSAL_SETTINGS.items()):
        raise ValueError
    key = values.get("OPENAI_API_KEY", "")
    if not re.fullmatch(r"dummy-[A-Za-z0-9_-]+", key):
        raise ValueError
    return key


def record(directory, name, **fields):
    with (directory / name).open("x", encoding="ascii") as stream:
        json.dump(fields, stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    # Exclusive creation makes the directory the identity of a single simulation.
    args.evidence.mkdir(mode=0o700)
    try:
        key = load_validated_rehearsal_key()
        body = args.request.read_bytes()
        if hashlib.sha256(body).hexdigest() != REQUEST_SHA256 or not 1 <= args.port <= 65535:
            raise ValueError
    except (OSError, ValueError):
        record(args.evidence, "receipt.json", simulated_only=True, status="refused")
        return 1

    record(args.evidence, "attempt.json", simulated_slots=1)
    connection = HTTPConnection("127.0.0.1", args.port, timeout=5)
    status = "unknown"
    try:
        connection.request("POST", "/v1/responses", body,
                           {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        if connection.getresponse().status == 204:
            status = "received"
    except (HTTPException, OSError):
        pass  # The simulated slot is consumed even if delivery is uncertain.
    finally:
        connection.close()
    record(args.evidence, "receipt.json", simulated_only=True, status=status)
    return 0 if status == "received" else 1


if __name__ == "__main__":
    raise SystemExit(main())
