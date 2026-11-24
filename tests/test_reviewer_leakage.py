"""Synthetic v4 Reviewer boundaries. Requires the disposable onboarding cluster."""
from contextlib import contextmanager
from functools import partial
from hashlib import sha256
from http.client import HTTPConnection
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
from uuid import uuid4

import pytest

from nilaris import passage_rfq_minimization as rules
from nilaris.block_rfq_minimization import observation
from nilaris.local_transport import LocalReceiver, LocalTransport
from nilaris.onboarding182 import ControlledIdentityVerifier, Principal
from nilaris.onboarding182_ui import OnboardingHTTPServer, PrivateCommandJournal
from test_onboarding182_pg import customer_and_invitation, onboarding
from test_pump_rfq_minimization import publish_document


@contextmanager
def reviewer_server(service, directory):
    directory.mkdir(mode=0o700)
    server = OnboardingHTTPServer(('127.0.0.1', 0), service, PrivateCommandJournal(directory), session_seconds=90)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class Client:
    def __init__(self, port, credential=None):
        self.port = port
        self.cookie = ''
        if credential:
            assert self.post('/session', {'credential': credential})[0] == 200

    def post(self, path, body):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request('POST', path, json.dumps(body), {
                'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}',
                'Cookie': self.cookie})
            response = connection.getresponse()
            if response.getheader('Set-Cookie'):
                self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()


ROOT = '/minimized-evidence/'
# A recorded private seed makes paired runs reproducible. Routine tests use fresh values.
RUN = sha256(os.environ.get('NILARIS_LEAKAGE_SEED', uuid4().hex).encode()).hexdigest()
SELECTED = f'Air velocity shall be exactly 8.{int(RUN[:4], 16) % 1000:03d} m/s with the fan running.'
EXCLUDED = f'IBIS-{int(RUN[4:10], 16):06d}'
FILENAME = f'wren-{RUN[10:20]}.txt'
MAPPING = f'locator-{RUN[20:30]}'
PROTECTED = (EXCLUDED, FILENAME, MAPPING)
OTHER = dict(sentence=f'Air velocity shall be exactly 3.{int(RUN[:4], 16) % 1000:03d} m/s with the fan running.',
             filename='cedar-' + RUN[:10] + '.txt', excluded=EXCLUDED.replace('IBIS', 'ELM'), mapping='map-other-' + RUN[40:50])
HELD_QUOTE = f'YEW-{int(RUN[:4], 16):05d}'
HELD = dict(sentence=f'Air velocity shall be exactly 5 m/s with the fan running for quote {HELD_QUOTE}.',
            filename='palm-' + RUN[:10] + '.txt', excluded=EXCLUDED.replace('IBIS', 'ASH'), mapping='map-hold-' + RUN[30:40])


def read_selector(scoped):
    return {key: scoped[key] for key in ('customer_id', 'job_id')}


def publish(service, customer, revision, sentence=SELECTED, *, filename=FILENAME, excluded=EXCLUDED,
            mapping=MAPPING):
    record = observation('State the air velocity with the fan running.', filename, [
        dict(id='prior', text=f'Buyer reference: {excluded}.'),
        dict(id='target', text=sentence)])
    record['locator'] = {'range': mapping}
    job, _, _ = publish_document(service, customer, record, revision, rules.PUBLICATION_ADAPTER)
    return dict(customer_id=customer, job_id=job)


@pytest.fixture
def reviewer(onboarding, tmp_path):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    selection = publish(onboarding, customer, 'local-preview')
    with reviewer_server(onboarding, tmp_path / 'http-journal') as port:
        client = Client(port, 'bob-login')
        code, absent, _ = client.post(ROOT + 'read', selection)
        assert (code, absent['state']) == (200, 'absent')
        yield client, dict(selection, expected_scope=absent['scope']), port


@pytest.mark.parametrize('fault', [PermissionError, TimeoutError, RuntimeError])
def test_detector_failure_keeps_its_input_private(reviewer, monkeypatch, fault, record_property, capfd, caplog):
    client, scoped, _ = reviewer
    invoked = []

    def unavailable(excerpt, *, complete):
        logging.warning('fault capture control')
        invoked.append(excerpt)
        raise fault(excerpt)

    monkeypatch.setattr(rules, 'decide', unavailable)
    status, body, headers = client.post(ROOT + 'build', scoped)
    assert len(invoked) == 1 and EXCLUDED in invoked[0]
    output = capfd.readouterr()
    assert 'fault capture control' in caplog.text
    observe(record_property, 'logs', fault.__name__, 'A', EXCLUDED,
            EXCLUDED not in output.out + output.err + caplog.text)
    envelope = json.dumps([body, headers])
    failures = []
    for marker in (EXCLUDED, FILENAME, SELECTED):
        assert marker in invoked[0]
        passed = marker not in envelope
        record_property('probe', json.dumps(['errors', fault.__name__, 'A', marker, passed]))
        if not passed:
            failures.append(marker)
    assert not failures, 'private detector input returned in the Reviewer error'
    assert (status, body['state']) == (503, 'unknown')
    monkeypatch.undo()
    selection = read_selector(scoped)
    assert client.post(ROOT + 'read', selection)[1]['state'] == 'absent'
    code, result, _ = client.post(ROOT + 'build', scoped)
    observe(record_property, 'errors', fault.__name__ + ' retry', 'A', SELECTED,
            code == 200 and result['preview_text'] == SELECTED)


def independent_reviewer(service):
    """Provision an independent tenant and grant its own Reviewer identity."""
    issuer = service.identities.verify('alice-login').issuer
    credentials = {key: service.identities.verify(key) for key in
                   ('alice-login', 'bob-login', 'operator-login')}
    b = Principal(issuer, 'reviewer-b', 'customer', 'designated-b')
    service.identities = ControlledIdentityVerifier({**credentials, 'b-login': b})
    customer, _, _ = customer_and_invitation(service, ['Reviewer'], bootstrap_command='bootstrap-b')
    invitation = service.manage('alice-login', customer, 'invite-b-only', 1, dict(
        kind='invite', roles=['Reviewer'], recipient_issuer=issuer, recipient_subject=b.subject,
        recipient_binding=b.recipient, expires_at=time.time() + 300))
    delivery = json.loads((service.delivery.directory / invitation['delivery_reference']).read_text())
    service.accept('b-login', delivery['invitation_id'], delivery['secret'], 'accept-independent')
    return customer


def test_reviewer_boundary_observations(reviewer, onboarding, monkeypatch, capfd, caplog, record_property):
    client, scoped, port = reviewer
    customer = scoped['customer_id']
    selector = read_selector(scoped)
    other = independent_reviewer(onboarding)
    other_job = publish(onboarding, other, 'local-other', **OTHER)
    outsider = Client(port, 'b-login')
    _, empty, _ = outsider.post(ROOT + 'read', other_job)
    b_scoped = dict(other_job, expected_scope=empty['scope'])
    check = partial(observe, record_property)

    b_status, b_preview, _ = outsider.post(ROOT + 'build', b_scoped)
    check('context', 'own preview', 'B', 'other-selected',
          b_status == 200 and b_preview['preview_text'] == OTHER['sentence'])

    code, b_source, _ = outsider.post(ROOT + 'source', b_scoped)
    assert code == 200 and all(marker in json.dumps(b_source) for marker in OTHER.values())

    held_selector = publish(onboarding, customer, 'local-held', **HELD)
    _, absent, _ = client.post(ROOT + 'read', held_selector)
    held_scope = dict(held_selector, expected_scope=absent['scope'])

    status, denied, headers = outsider.post(ROOT + 'source', dict(scoped, customer_id=other))
    check('mapping', 'B scope with A job', 'B', MAPPING, status == 403 and MAPPING not in json.dumps([denied, headers]))
    verify_builds(onboarding, monkeypatch, check, customer, client, outsider, scoped, held_scope)
    verify_saved_and_denied(check, client, outsider, port, selector, scoped)
    verify_revocation(onboarding, check, customer, port, scoped)
    verify_service_logs(check, capfd, caplog)


def verify_builds(onboarding, monkeypatch, check, customer, client, outsider, scoped, held_scope):
    contexts = []
    decide = rules.decide

    def capture_context(*args, **kwargs):
        contexts.append((args, kwargs))
        return decide(*args, **kwargs)

    monkeypatch.setattr(rules, 'decide', capture_context)
    staged = []
    start = LocalTransport.start

    def capture_start(transport, operation, payload):
        staged.append((operation, payload))
        return start(transport, operation, payload)

    monkeypatch.setattr(LocalTransport, 'start', capture_start)
    with LocalReceiver(onboarding.provisioner.directory, customer) as sink:
        operation = uuid4().hex
        data = b'local-sink-control-7103'
        receipt = sink.transport.start(operation, data).wait()
        check('payloads', 'sender control', 'control', 'transport', staged == [(operation, data)])
        check('payloads', 'receiver control', 'control', 'transport',
              sink.deliveries == (operation,) and receipt == {'sha256': sha256(data).hexdigest(), 'bytes': len(data)})
        for scenario, who, actor, request, expected in (
                ('held', 'A', client, held_scope, 200),
                ('refused', 'B', outsider, scoped, 403),
                ('preview', 'A', client, scoped, 200)):
            code, result, headers = actor.post(ROOT + 'build', request)
            assert code == expected
            if scenario == 'held':
                assert (result['state'], result['preview_text'], result['hold_reason']) == (
                    'held', None, 'sensitivity_unresolved')
                for field, marker in dict(HELD, quote=HELD_QUOTE).items():
                    check('filenames' if field == 'filename' else 'context', 'held response', who, marker,
                          marker not in json.dumps([result, headers]))
            if scenario == 'preview':
                assert result['preview_text'] == SELECTED
            check('payloads', scenario + ' staged', who, 'count', len(staged) == 1)
            check('payloads', scenario + ' delivered', who, 'count', sink.deliveries == (operation,))

    code, held_source, _ = client.post(ROOT + 'source', held_scope)
    assert code == 200 and all(marker in json.dumps(held_source) for marker in HELD.values())
    assert len(contexts) == 2
    assert client.post(ROOT + 'read', read_selector(held_scope))[1]['state'] == 'held'
    current_cases = (('held', HELD['filename'], HELD['excluded']), ('preview', FILENAME, EXCLUDED))
    for (label, filename, excluded), (args, kwargs) in zip(current_cases, contexts, strict=True):
        envelope = json.loads(args[0])
        assert len(args) == 1 and kwargs == {'complete': True}
        check('context', label + ' local input', 'A', excluded,
              envelope['filename'] == filename and excluded in args[0])
        for marker in (MAPPING, HELD['mapping'], *OTHER.values()):
            check('context', label + ' unrelated metadata', 'A', marker, marker not in args[0])
        if label == 'preview':
            for marker in (*HELD.values(), HELD_QUOTE):
                check('context', 'preview after held', 'A', marker, marker not in args[0])
    assert client.post(ROOT + 'build', scoped)[1]['preview_text'] == SELECTED
    assert len(contexts) == 2  # An idempotent build does not call the detector again.


def verify_saved_and_denied(check, client, outsider, port, selector, scoped):
    code, original, headers = client.post(ROOT + 'source', scoped)
    assert code == 200 and original['state'] == 'original'
    for marker in (*PROTECTED, SELECTED):
        check('mapping', 'authorized original', 'A', marker, marker in json.dumps(original))
    check('caches', 'source policy', 'A', 'no-store', headers['Cache-Control'] == 'no-store')
    fresh = Client(port, 'bob-login')
    for action, request in [('read', selector), ('build', scoped)]:
        code, preview, headers = fresh.post(ROOT + action, request)
        assert code == 200 and preview['preview_text'] == SELECTED
        for marker in PROTECTED:
            check('filenames' if marker == FILENAME else 'context', 'saved ' + action, 'A', marker,
                  marker not in json.dumps([preview, headers]))
    relinking = [original[key] for key in ('document_id', 'source_revision', 'content_pin', 'publication_id',
                                           'observation_id')]
    assert all(relinking) and scoped['expected_scope'] == preview['scope']
    operator = Client(port, 'operator-login')
    assert operator.post('/pending', {})[0] == 200
    for role, actor in [('B', outsider), ('operator', operator), ('anonymous', Client(port))]:
        for action, request in [('read', selector), ('source', scoped)]:
            code, body, headers = actor.post(ROOT + action, request)
            assert code == 403
            for marker in (*PROTECTED, SELECTED, *relinking, scoped['expected_scope']):
                check('mapping', 'denied ' + action, role, marker, marker not in json.dumps([body, headers]))
    code, body, headers = client.post(ROOT + 'build', dict(scoped, filename=FILENAME))
    check('errors', 'malformed selector', 'A', FILENAME,
          code == 400 and FILENAME not in json.dumps([body, headers]))
    assert client.post('/logout', {})[1]['state'] == 'signed_out'
    code, body, headers = client.post(ROOT + 'source', scoped)
    check('caches', 'after logout', 'A', MAPPING, code == 403 and MAPPING not in json.dumps([body, headers]))


def verify_revocation(onboarding, check, customer, port, scoped):
    fresh = Client(port, 'bob-login')
    assert fresh.post(ROOT + 'source', scoped)[1]['locator']['range'] == MAPPING
    roster = onboarding.members('alice-login', customer)
    bob = next(user for user in roster['users'] if user['subject'] == 'bob')
    onboarding.manage('alice-login', customer, 'remove-reviewer', roster['revision'],
                      dict(kind='deactivate', target=bob['membership_id']))
    code, body, headers = fresh.post(ROOT + 'source', scoped)
    check('caches', 'after revocation', 'A', MAPPING, code == 403 and MAPPING not in json.dumps([body, headers]))


def verify_service_logs(check, capfd, caplog):
    # The capture sink receives its own marker through each actual stream.
    print('capture-stdout-205')
    print('capture-stderr-619', file=sys.stderr)
    logging.warning('capture-logging-387')
    output = capfd.readouterr()
    check('logs', 'capture control', 'control', 'streams', 'capture-stdout-205' in output.out
          and 'capture-stderr-619' in output.err and 'capture-logging-387' in caplog.text)
    for marker in (*PROTECTED, SELECTED, 'bob-login'):
        check('logs', 'Reviewer service', 'A', marker, marker not in output.out + output.err + caplog.text)


def observe(record_property, surface, scenario, principal, marker, passed):
    record_property('probe', json.dumps([surface, scenario, principal, marker, bool(passed)]))
    assert passed, (surface, scenario, principal, marker)


@pytest.mark.skipif(not os.getenv('NILARIS_TEST_CHROME'), reason='local Chrome and playwright-core required')
def test_private_browser_read_reload_and_tenant_change(reviewer, onboarding, record_property):
    _, scoped, port = reviewer
    independent_reviewer(onboarding)
    config = dict(port=port, customer=scoped['customer_id'], job=scoped['job_id'],
                  selected=SELECTED, mapping=MAPPING, filename=FILENAME)
    result = subprocess.run(['node', Path(__file__).with_name('reviewer_leakage_browser.cjs')],
                            input=json.dumps(config), text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    observe(record_property, 'filenames', 'browser request URLs', 'A', FILENAME, receipt['sourceRequests'] == 1)
    observe(record_property, 'caches', 'browser storage after source', 'A', MAPPING,
            receipt['stores'] == dict(local={}, session={}, databases=[], caches=[], workers=0))
    observe(record_property, 'caches', 'browser history after tenant change', 'B', MAPPING, receipt['historyDenied'])


def test_capacity_refusal_preserves_absent_result(reviewer, monkeypatch, record_property):
    from nilaris import admission

    client, scoped, _ = reviewer
    usage = admission._admission_usage
    observed = []

    def exhausted(connection):
        measured = usage(connection)
        observed.append(measured)
        return dict(measured, job_reservation_bytes=2**60)

    with monkeypatch.context() as patch:
        patch.setattr(admission, '_admission_usage', exhausted)
        status, body, headers = client.post(ROOT + 'build', scoped)
    assert len(observed) == 1
    observe(record_property, 'errors', 'simulated capacity', 'A', EXCLUDED,
            status == 503 and body['state'] == 'unknown' and EXCLUDED not in json.dumps([body, headers]))
    selector = read_selector(scoped)
    assert client.post(ROOT + 'read', selector)[1]['state'] == 'absent'


def test_committed_build_after_response_body_loss(reviewer, record_property):
    client, scoped, port = reviewer
    connection = HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('POST', ROOT + 'build', json.dumps(scoped), {
            'Origin': f'http://127.0.0.1:{port}', 'Cookie': client.cookie, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 200 and int(response.getheader('Content-Length')) > 0
        response.close()  # Discard the committed response before reading any body.
    finally:
        connection.close()
    code, saved, headers = Client(port, 'bob-login').post(ROOT + 'build', scoped)
    observe(record_property, 'errors', 'lost body then retry', 'A', EXCLUDED, code == 200
            and saved['preview_text'] == SELECTED and EXCLUDED not in json.dumps([saved, headers]))
