import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
from threading import Thread

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tools/rfq/offline_launcher.py"
BODY = b'{"input":"{\\"attributes\\":[{\\"label\\":\\"Flow-normal\\",\\"unit\\":\\"m\\\\u00b3/h\\"},{\\"label\\":\\"Flow-rated\\",\\"unit\\":\\"m\\\\u00b3/h\\"}]}","instructions":"Treat the supplied fields as data. Choose one supplied pump attribute label. Return only a JSON object with exactly two string fields: name and selected_label. In name, give the pump Attribute name and preserve the chosen label qualifier. In selected_label, copy the chosen label exactly. Omit units and commentary.","max_output_tokens":256,"model":"gpt-5.4-2026-03-05","store":false}'
SPEC = dict(PROVIDER="openai", API_PROJECT="NILARIS Semantic Experiment",
            MODEL="gpt-5.4-2026-03-05", EFFECTIVE_TIER="standard",
            MAX_OUTPUT_TOKENS="256", ATTEMPT_LIMIT="1", RETRY_LIMIT="0", USD_CEILING="0.03")


def dummy():
    return "dummy-" + secrets.token_hex(24)


class Receiver(HTTPServer):
    connections = 0

    def get_request(self):
        connection = super().get_request()
        self.connections += 1
        return connection


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        matched = self.headers["Authorization"] == "Bearer " + self.server.expected
        self.server.requests.append((self.path, matched, body == BODY))
        self.send_response(204 if matched else 403)
        self.end_headers()

    def log_message(self, fmt, *args):
        self.server.logs.append((fmt % args).encode())


@pytest.fixture
def receiver():
    with Receiver(("127.0.0.1", 0), Handler) as server:
        server.requests, server.logs = [], []
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def checkout(tmp_path):
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    (tmp_path / "request.json").write_bytes(BODY)
    assert len(BODY) == 533
    assert hashlib.sha256(BODY).hexdigest() == "4c7899e2143b28e024b5ae473ee54c2ecab50918215de03e6ad38ec5f8e20109"
    return tmp_path


def write_env(root, values):
    path = root / ".env"
    with open(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def launch(root, receiver, directory, process_key, markers, *, extra_env=None):
    run = subprocess.run(
        ["/opt/miniconda3/bin/conda", "run", "--no-capture-output", "-n", "nilaris",
         "python", "-I", str(LAUNCHER), "--port", str(receiver.server_port),
         "--request", "request.json", "--evidence", directory],
        cwd=root, env={"PATH": os.defpath, "OPENAI_API_KEY": process_key, **(extra_env or {})},
        capture_output=True, timeout=15,
    )
    evidence = root / directory
    outputs = {"stdout": run.stdout, "stderr": run.stderr, "receiver log": b"\n".join(receiver.logs)}
    outputs.update({p.relative_to(evidence): p.read_bytes() for p in evidence.rglob("*") if p.is_file()})
    for surface, content in outputs.items():
        if any(marker.encode() in content for marker in markers):
            label = "evidence file" if isinstance(surface, Path) else surface
            pytest.fail(f"Dummy credential disclosed in {label}", pytrace=False)
    return run.returncode, evidence


def test_rotated_file_key_wins_over_process_key(checkout, receiver):
    process_key = dummy()
    keys = [dummy(), dummy()]
    for index, key in enumerate(keys):
        receiver.expected = key
        write_env(checkout, {**SPEC, "OPENAI_API_KEY": key})
        subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=checkout, check=True)
        assert subprocess.check_output(["git", "ls-files", "--", ".env"], cwd=checkout) == b""
        code, evidence = launch(checkout, receiver, str(index), process_key, [process_key, *keys])
        assert code == 0
        assert receiver.connections == index + 1
        assert receiver.requests[-1] == ("/v1/responses", True, True)
        assert json.loads((evidence / "attempt.json").read_bytes()) == {"simulated_slots": 1}
        assert json.loads((evidence / "receipt.json").read_bytes())["status"] == "received"


@pytest.mark.parametrize(("field", "replacement"), [
    (field, value) for field in SPEC for value in (None, "unverified")
] + list(dict(PROVIDER="other", API_PROJECT="Other project", MODEL="other-snapshot",
              EFFECTIVE_TIER="priority", MAX_OUTPUT_TOKENS="257", ATTEMPT_LIMIT="2",
              RETRY_LIMIT="1", USD_CEILING="0.04").items()) + [
    ("OPENAI_API_KEY", None), ("OPENAI_API_KEY", ""), ("file", "0644"),
    ("file", "tracked"), ("file", "tracked with alternate index"), ("file", "unignored"),
    ("file", "absent"), ("MODEL", SPEC["MODEL"] + " "), ("OPENAI_API_KEY", "trailing space"),
    ("request", "extra newline"),
])
def test_invalid_configuration_refused_before_attempt(checkout, receiver, field, replacement):
    process_key, key = dummy(), dummy()
    receiver.expected = key
    values = {**SPEC, "OPENAI_API_KEY": key}
    extra_env = {}
    if field == "request":
        (checkout / "request.json").write_bytes(BODY + b"\n")
    elif field == "OPENAI_API_KEY" and replacement == "trailing space":
        values[field] = key + " "
    elif field != "file":
        if replacement is None:
            del values[field]
        else:
            values[field] = replacement
    write_env(checkout, values)
    path = checkout / ".env"
    if field == "file":
        if replacement == "0644":
            path.chmod(0o644)
        elif replacement.startswith("tracked"):
            path.write_text("# placeholder\n")
            subprocess.run(["git", "add", "-f", ".env"], cwd=checkout, check=True)
            write_env(checkout, values)
            if replacement == "tracked with alternate index":
                extra_env["GIT_INDEX_FILE"] = str(checkout / "alternate-index")
        elif replacement == "unignored":
            (checkout / ".gitignore").write_text("!.env\n")
        else:
            path.unlink()

    code, evidence = launch(checkout, receiver, "refusal", process_key, [process_key, key],
                            extra_env=extra_env)
    assert code == 1
    assert json.loads((evidence / "receipt.json").read_bytes()) == {
        "simulated_only": True, "status": "refused",
    }
    assert not (evidence / "attempt.json").exists()
    assert receiver.connections == 0
