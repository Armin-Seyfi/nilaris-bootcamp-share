"""ROOT ONLY: real durable claims + existing supervised LangGraph + loopback HTTP.

No external model. Only root may run these nodes with the disposable DB fixture.
"""
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import hashlib
from io import BytesIO
import json
import threading
import time

import pytest

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from nilaris.semantic_contract import MODEL, canonical
from nilaris.semantic172 import prepare, digest, run
from nilaris.semantic172_sources import capture_sources


def sources():
    from reportlab.pdfgen.canvas import Canvas
    result = []
    for target in ('A', 'B'):
        output = BytesIO()
        canvas = Canvas(output, invariant=1)
        canvas.drawString(40, 700, f'Equipment {target}: pressure 8 bar')
        canvas.save()
        raw = output.getvalue()
        result.append(dict(document_id=target, source_revision='test-r1',
            media_type='application/pdf', content_pin=hashlib.sha256(raw).hexdigest(),
            content=base64.b64encode(raw).decode()))
    return result


def setup_job(database, source):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1(), semantic172=True)
    member = dict(member_key=source['document_id'], connector='synthetic', source_id=source['document_id'],
        source_revision=source['source_revision'], document_id=source['document_id'], case_id='172-offline',
        membership_revision='1', product_type='test', processing_contract='semantic172-v1',
        reference_set='none', selection_policy='none', intent_id='ordinary',
        input_bytes=len(base64.b64decode(source['content'])), media_type='application/pdf', identity='resolved')
    receipt = client.offer('semantic172-offline', source['document_id'], [member])
    assert receipt['totals']['accepted'] == 1, receipt
    job_id = receipt['members'][0]['job_id']
    queued = client.capture(job_id, client.claim(job_id, 0), ControlledSource(
        {source['document_id']: (source['source_revision'], base64.b64decode(source['content']))}))
    assert queued['state'] == 'queued', queued
    return client, job_id


@pytest.mark.parametrize('delay,status', [(65, 200), (0, 429), (0, 200)])
def test_root_existing_supervisor_105_profile(database, tmp_path, delay, status):
    docs = sources()
    sink = prepare(tmp_path / 'batch', capture_sources(docs), ['A', 'B'], mode='offline', sources=docs)
    client, job = setup_job(database, docs[0])
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            time.sleep(delay)
            output = dict(version='semantic-experiment-v1', observations=[], associations=[], candidates=[])
            response = dict(model=MODEL, status='completed', usage=dict(input_tokens=100, output_tokens=100),
                output=[dict(type='message', content=[dict(type='output_text', text=canonical(output).decode())])])
            payload = canonical(response)
            self.send_response(status)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        before = client.status(job)
        outcome = run(client, job, before['revision'], artifact_root=sink.root, target_document_id='A',
            expected_freeze_sha256=digest(sink.read('freeze.json')), mode='offline',
            offline_endpoint=f'http://127.0.0.1:{server.server_port}/v1/responses')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert len(calls) == 1, outcome
    assert outcome['state'] == ('completed' if status == 200 else 'held'), outcome
    attempt = outcome['attempts'][-1]
    assert attempt['accounting_complete'] is True
    assert attempt['completion_evidence'] == 'process_group_reaped'
    assert attempt['worker']['limits']['attempt_wall_seconds'] == 105
    assert attempt['worker']['limits']['lease_seconds'] == 10
    assert attempt['worker']['limits']['heartbeat_seconds'] == 2
    assert attempt['worker']['limits']['output_bytes'] == 2*1024**2
    assert attempt['worker']['limits']['rss_bytes'] == 1024**3
    assert attempt['cpu_accounting'] == 'conservative_full_allowance'
    if delay:
        assert attempt['wall_seconds'] > 60
    assert sink.status()['new_reserved_usd'] == '0.10144'
    assert sink.status()['completed'] == (1 if status == 200 else 0)
    with pytest.raises((ValueError, FileExistsError)):
        sink.reserve('A')


def test_root_reservation_lock_outlasting_lease_never_opens_dispatch_gate(database, tmp_path, monkeypatch):
    """Real ledger contention must not send after the durable claim expires."""
    import fcntl
    import subprocess
    actual_popen = subprocess.Popen
    input_writes = []
    class ObservedInput:
        def __init__(self, stream):
            self.stream = stream
        def write(self, data):
            input_writes.append(len(data))
            return self.stream.write(data)
        def close(self):
            return self.stream.close()
    def launch(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        command = args[0] if args else kwargs.get('args')
        if (isinstance(command, list) and len(command) > 3 and
                command[3] == 'nilaris-controlled-bootstrap'):
            process.stdin = ObservedInput(process.stdin)
        return process
    monkeypatch.setattr(subprocess, 'Popen', launch)
    docs = sources()
    sink = prepare(tmp_path / 'batch', capture_sources(docs), ['A', 'B'], mode='offline', sources=docs)
    client, job = setup_job(database, docs[0])
    lock = (sink.root / '.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            calls.append(True)
            self.send_response(429)
            self.send_header('Content-Length', '0')
            self.end_headers()
    server = HTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    # No module replacement: a separate descriptor holds the real OS ledger lock.
    release = threading.Timer(13, lambda: fcntl.flock(lock, fcntl.LOCK_UN))
    release.start()
    try:
        before = client.status(job)
        outcome = run(client, job, before['revision'], artifact_root=sink.root, target_document_id='A',
            expected_freeze_sha256=digest(sink.read('freeze.json')), mode='offline',
            offline_endpoint=f'http://127.0.0.1:{server.server_port}/v1/responses')
    finally:
        release.join(15)
        lock.close()
        server.shutdown()
        server.server_close()
        serving.join(2)
    assert input_writes == [], outcome
    assert calls == [], outcome
    assert not (sink.root / 'dispatch-01.json').exists(), outcome
    assert outcome['state'] == 'held' and outcome['reason'] == 'claim_lost', outcome
    assert outcome['attempts'][-1]['accounting_complete'] is True, outcome
    assert sink.status()['attempted'] == 1
    assert sink.status()['new_reserved_usd'] == '0.10144'
    assert sink.status()['completed'] == 0
    with pytest.raises(ValueError):
        sink.reserve('B')
