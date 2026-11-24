"""Root-owned fresh PostgreSQL only. Never start services or use a retained DB."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
import pytest
from nilaris.provisioning181 import OperatorProvisioner, ControlledOperatorVerifier
from nilaris.onboarding182 import Onboarding, ControlledIdentityVerifier, Principal, PrivateInvitationDelivery


@pytest.fixture
def onboarding(tmp_path):
    path = os.environ.get('NILARIS_ONBOARDING182_TEST_CONFIG')
    if not path:
        pytest.skip('root-owned isolated onboarding cluster not supplied')
    tmp_path.chmod(0o700)
    service = controlled_service(path, 'local-qa-' + uuid.uuid4().hex, tmp_path)
    service.install('operator-login')
    return service


def controlled_service(config_path, issuer, directory):
    from nilaris.provisioning181 import _private_json
    config = _private_json(config_path)
    identities = ControlledIdentityVerifier({
        'operator-login': Principal(issuer, 'operator-a', 'operator', ''),
        'alice-login': Principal(issuer, 'alice', 'customer', 'alice-designated'),
        'bob-login': Principal(issuer, 'bob', 'customer', 'bob-designated'),
        'other-operator-login': Principal(issuer, 'operator-b', 'operator', ''),
        'wrong-binding-login': Principal(issuer, 'alice', 'customer', 'different-designation'),
    })
    provisioner = OperatorProvisioner(config, ControlledOperatorVerifier({
        'operator-login': 'operator-a', 'other-operator-login': 'operator-b'}))
    return Onboarding(provisioner, identities, PrivateInvitationDelivery(directory), validity_ceiling_seconds=600)


def invite_alice(service):
    operation = 'onboarding-' + uuid.uuid4().hex
    ready = service.provision('operator-login', operation)
    assert ready['state'] == 'ready'
    command = uuid.uuid4().hex
    invitation = service.invite('operator-login', operation, command, service.identities.verify('alice-login').issuer, 'alice', 'alice-designated', time.time() + 300)
    payload = json.loads((service.delivery.directory / invitation['delivery_reference']).read_text())
    return operation, ready, command, invitation, payload


def test_initial_administrator_accepts_and_fresh_service_reads_metadata_only(onboarding):
    service = onboarding
    operation, ready, command, invitation, payload = invite_alice(service)
    receipt = service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'accept-alice')
    assert receipt['state'] == 'accepted'
    assert receipt['customer_id'] == ready['customer_id']
    fresh = Onboarding(service.provisioner, service.identities, service.delivery, validity_ceiling_seconds=600)
    assert fresh.accept('alice-login', invitation['invitation_id'], payload['secret'], 'accept-alice') == receipt
    assert fresh.landing('alice-login', ready['customer_id']) == {
        'customer_id': ready['customer_id'], 'principal': {'issuer': service.identities.verify('alice-login').issuer, 'subject': 'alice'},
        'bundles': ['Administrator'], 'actions': ['administrative_metadata'], 'state': 'active'}
    assert fresh.invitation_status('operator-login', command)['state'] == 'accepted'
    with pytest.raises(PermissionError):
        fresh.landing('bob-login', ready['customer_id'])
    with pytest.raises(PermissionError):
        fresh.landing('operator-login', ready['customer_id'])


def test_matching_recipient_single_use_and_bootstrap_cannot_replace_admin(onboarding):
    service = onboarding
    operation, ready, command, invitation, payload = invite_alice(service)
    with pytest.raises(PermissionError):
        service.accept('bob-login', invitation['invitation_id'], payload['secret'], 'wrong-recipient')
    assert service.invitation_status('operator-login', command)['state'] == 'pending'
    with pytest.raises(PermissionError):
        service.accept('operator-login', invitation['invitation_id'], payload['secret'], 'staff-conversion')
    with pytest.raises(PermissionError):
        service.accept('alice-login', invitation['invitation_id'], 'x' * 43, 'wrong-secret')
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'first-accept')
    with pytest.raises(PermissionError):
        service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'another-accept')
    with pytest.raises(PermissionError):
        service.invite('operator-login', operation, 'replace-admin', service.identities.verify('alice-login').issuer, 'bob', 'bob-designated', time.time() + 100)
    assert service.landing('alice-login', ready['customer_id'])['bundles'] == ['Administrator']


def test_read_only_receipt_resolution_never_consumes_pending_invitation(onboarding):
    service = onboarding
    _, ready, command, invitation, payload = invite_alice(service)
    assert service.acceptance_status('alice-login', invitation['invitation_id'], payload['secret'], 'lost-request')['state'] == 'absent'
    assert service.invitation_status('operator-login', command)['state'] == 'pending'
    with pytest.raises(PermissionError):
        service.landing('alice-login', ready['customer_id'])
    saved = service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'lost-request')
    assert service.acceptance_status('alice-login', invitation['invitation_id'], payload['secret'], 'lost-request') == saved
    with pytest.raises(PermissionError):
        service.acceptance_status('bob-login', invitation['invitation_id'], payload['secret'], 'lost-request')


def test_expiry_boundary_revocation_and_current_issuer_authority(onboarding, monkeypatch):
    import types
    import nilaris.onboarding182 as module
    service = onboarding
    _, ready, command, invitation, payload = invite_alice(service)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'time', types.SimpleNamespace(time=lambda: invitation['expires_at']))
        assert service.invitation_status('operator-login', command)['state'] == 'expired'
        with pytest.raises(PermissionError):
            service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'at-expiry')
    with pytest.raises(PermissionError):
        service.landing('alice-login', ready['customer_id'])
    service.revoke('operator-login', command)
    assert service.revoke('operator-login', command)['state'] == 'revoked'
    with pytest.raises(PermissionError):
        service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'after-revoke')
    _, _, command2, invitation2, payload2 = invite_alice(service)
    service.identities.revoke('operator-login')
    with pytest.raises(PermissionError):
        service.accept('alice-login', invitation2['invitation_id'], payload2['secret'], 'revoked-issuer')
    with pytest.raises(PermissionError):
        service.invitation_status('operator-login', command2)


def test_concurrent_distinct_redemptions_create_one_administrator(onboarding):
    from concurrent.futures import ThreadPoolExecutor
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    def redeem(key):
        try:
            return service.accept('alice-login', invitation['invitation_id'], payload['secret'], key)['state']
        except PermissionError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(redeem, ('concurrent-one', 'concurrent-two')))
    assert sorted(outcomes) == ['accepted', 'refused']
    assert service.landing('alice-login', ready['customer_id'])['actions'] == ['administrative_metadata']


def test_two_customers_and_operator_receipts_are_isolated(onboarding):
    service = onboarding
    _, a, command_a, invitation_a, payload_a = invite_alice(service)
    service.accept('alice-login', invitation_a['invitation_id'], payload_a['secret'], 'accept-a')
    operation_b = 'onboarding-b-' + uuid.uuid4().hex
    b = service.provision('operator-login', operation_b)
    invitation_b = service.invite('operator-login', operation_b, uuid.uuid4().hex,
        service.identities.verify('alice-login').issuer, 'bob', 'bob-designated', time.time() + 300)
    payload_b = json.loads((service.delivery.directory / invitation_b['delivery_reference']).read_text())
    service.accept('bob-login', invitation_b['invitation_id'], payload_b['secret'], 'accept-b')
    assert service.landing('bob-login', b['customer_id'])['principal']['subject'] == 'bob'
    for credential, customer in [('alice-login', b), ('bob-login', a)]:
        with pytest.raises(PermissionError):
            service.landing(credential, customer['customer_id'])
    for credential in ('alice-login', 'bob-login', 'forged-role'):
        with pytest.raises(PermissionError):
            service.invitation_status(credential, command_a)
    # Same command identity for a different invitation cannot reveal/reuse a receipt.
    with pytest.raises(PermissionError):
        service.acceptance_status('alice-login', invitation_b['invitation_id'], payload_b['secret'], 'accept-a')


def test_unknown_private_delivery_preserves_original_hash_key_and_reference(onboarding):
    service = onboarding
    original_delivery = service.delivery
    class UncertainDelivery:
        directory = original_delivery.directory
        def deliver(self, reference, payload):
            original_delivery.deliver(reference, payload)
            raise OSError('controlled lost acknowledgement')
    service.delivery = UncertainDelivery()
    operation, _, command, invitation, payload = invite_alice(service)
    assert invitation['delivery'] == 'unknown'
    before = (original_delivery.directory / invitation['delivery_reference']).read_bytes()
    fresh = Onboarding(service.provisioner, service.identities, original_delivery, validity_ceiling_seconds=600)
    assert fresh.invite('operator-login', operation, command, service.identities.verify('alice-login').issuer, 'alice', 'alice-designated', invitation['expires_at']) == invitation
    assert (original_delivery.directory / invitation['delivery_reference']).read_bytes() == before
    assert len(list(original_delivery.directory.iterdir())) == 1
    assert 'secret' not in json.dumps(fresh.invitation_status('operator-login', command))
    assert fresh.accept('alice-login', invitation['invitation_id'], payload['secret'], 'accept-unknown-delivery')['state'] == 'accepted'


def test_competing_bootstrap_and_changed_original_command_are_refused(onboarding):
    service = onboarding
    operation, ready, command, invitation, payload = invite_alice(service)
    with pytest.raises(PermissionError):
        service.invite('operator-login', operation, 'competing-live', service.identities.verify('alice-login').issuer, 'bob', 'bob-designated', time.time() + 100)
    with pytest.raises(PermissionError):
        service.invite('operator-login', operation, command, service.identities.verify('alice-login').issuer, 'bob', 'bob-designated', invitation['expires_at'])
    assert service.invitation_status('operator-login', command) == invitation
    assert service.invitation_status('other-operator-login', command)['state'] == 'absent'
    with pytest.raises(PermissionError):
        service.revoke('other-operator-login', command)
    with pytest.raises(PermissionError):
        service.status('other-operator-login', operation)
    with pytest.raises(PermissionError):
        service.accept('wrong-binding-login', invitation['invitation_id'], payload['secret'], 'binding-forgery')
    with pytest.raises(PermissionError):
        service.landing('alice-login', ready['customer_id'])


def cold_readback(directory, *, issuer, **request):
    descriptor = directory / 'restart.json'
    with open(descriptor, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(dict(issuer=issuer, **request), stream)
    completed = subprocess.run(['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris',
        'python', 'runtime/a1/with_overlay.py', __file__, str(descriptor)],
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, 'cold receipt readback failed (private diagnostics withheld)'
    return json.loads(completed.stdout)


def test_cold_process_resolves_committed_receipt_without_redemption_again(onboarding, tmp_path):
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    acceptance_key = 'cold-accept'
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], acceptance_key)
    receipt = cold_readback(tmp_path, issuer=service.identities.verify('alice-login').issuer,
        invitation_id=invitation['invitation_id'], secret=payload['secret'], command_id=acceptance_key)
    assert receipt == dict(state='accepted', invitation_id=invitation['invitation_id'],
        command_id=acceptance_key, customer_id=ready['customer_id'])


@pytest.fixture
def http_client(onboarding, tmp_path):
    """Actual authenticated HTTP → registry → readback; root starts this only."""
    from contextlib import contextmanager
    from http.client import HTTPConnection
    import threading
    from nilaris.onboarding182_ui import OnboardingHTTPServer, PrivateCommandJournal
    directory = tmp_path / 'journal'
    directory.mkdir(mode=0o700)
    journal = PrivateCommandJournal(directory)

    @contextmanager
    def connect(credential='operator-login', *, second_credential=None, intake=None):
        server = OnboardingHTTPServer(('127.0.0.1', 0), onboarding, journal, session_seconds=60, intake=intake)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def client():
            cookie = ''
            def post(path, body, *, raw=False):
                nonlocal cookie
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                try:
                    connection.request('POST', path, body if raw else json.dumps(body), {
                        'Content-Type': 'application/json',
                        'Origin': 'http://127.0.0.1:' + str(server.server_port), 'Cookie': cookie})
                    response = connection.getresponse()
                    if response.getheader('Set-Cookie'):
                        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            return post
        post = client()
        try:
            assert post('/session', {'credential': credential})[0] == 200
            if second_credential:
                second = client()
                assert second('/session', {'credential': second_credential})[0] == 200
                yield post, second
            else:
                yield post
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()
    return connect, journal


def test_http_unknown_delivery_allows_separately_durable_explicit_revoke(onboarding, http_client):
    service = onboarding
    original_delivery = service.delivery
    class UncertainDelivery:
        def deliver(self, reference, payload):
            original_delivery.deliver(reference, payload)
            raise OSError('controlled acknowledgement loss')
    service.delivery = UncertainDelivery()
    operation = 'http-unknown-' + uuid.uuid4().hex
    assert service.provision('operator-login', operation)['state'] == 'ready'
    request = dict(action='invite', operation_id=operation, command_id=uuid.uuid4().hex,
        recipient_issuer=service.identities.verify('alice-login').issuer,
        recipient_subject='alice', recipient_binding='alice-designated', expires_at=time.time() + 300)
    connect, journal = http_client
    operator = service.identities.verify('operator-login')
    with connect() as post:
        code, invitation = post('/command', request)
        assert code == 200 and invitation['delivery'] == 'unknown'
        assert post('/acknowledge', {})[0] == 403
        code, result = post('/command', dict(action='revoke', command_id=request['command_id']))
        assert code == 200 and result['state'] == 'revoked'
        assert journal.read(operator) == request
    # A fresh HTTP process/session view reads both exact commands independently.
    with connect() as post:
        assert post('/pending', {})[1]['command']['action'] == 'invite'
        assert post('/pending', {'channel': 'revocation'})[1]['command'] == {
            'action': 'revoke', 'command_id': request['command_id']}
        assert post('/resolve', {'channel': 'revocation'})[1]['state'] == 'revoked'
        assert post('/acknowledge', {'channel': 'revocation'})[0] == 200
        assert post('/acknowledge', {})[0] == 403
        assert journal.read(operator) == request
    handoff = json.loads((original_delivery.directory / invitation['delivery_reference']).read_text())
    with pytest.raises(PermissionError):
        service.accept('alice-login', invitation['invitation_id'], handoff['secret'], 'after-http-revoke')


@pytest.mark.parametrize('accepted', [False, True])
def test_http_resolve_unperformed_revoke_does_not_acknowledge_invitation_state(onboarding, http_client, accepted):
    service = onboarding
    _, _, command, invitation, payload = invite_alice(service)
    if accepted:
        service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'accept-before-revoke')
    connect, journal = http_client
    operator = service.identities.verify('operator-login')
    request = dict(action='revoke', command_id=command)
    # Original-format journals remain readable after upgrade. Simulate the crash
    # after durable save but before executing revoke, without touching DB internals.
    journal.save(operator, request)
    with connect() as post:
        code, result = post('/resolve', {})
        assert code == 200 and result['state'] == 'unknown'
        assert result['invitation_state'] == ('accepted' if accepted else 'pending')
        assert post('/acknowledge', {})[0] == 403
        assert journal.read(operator) == request
    assert service.invitation_status('operator-login', command)['state'] == ('accepted' if accepted else 'pending')
    if not accepted:
        # The exact durable effect, independently executed, is now read-only resolvable.
        service.revoke('operator-login', command)
        with connect() as post:
            assert post('/resolve', {})[1]['state'] == 'revoked'
            assert post('/acknowledge', {})[0] == 200
        assert journal.read(operator) is None


def test_acceptance_rechecks_current_provisioner_issuer_authority(onboarding):
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    service.provisioner.verifier.credentials.pop('operator-login')
    with pytest.raises(PermissionError):
        service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'issuer-no-longer-authorized')
    with pytest.raises(PermissionError):
        service.acceptance_status('alice-login', invitation['invitation_id'], payload['secret'], 'issuer-no-longer-authorized')
    with pytest.raises(PermissionError):
        service.landing('alice-login', ready['customer_id'])


def test_http_refused_revoke_acknowledges_only_its_own_command(onboarding, http_client):
    service = onboarding
    operation, _, command, invitation, payload = invite_alice(service)
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'accept-before-refused-revoke')
    connect, journal = http_client
    operator = service.identities.verify('operator-login')
    invite = dict(action='invite', operation_id=operation, command_id=command,
        recipient_issuer=service.identities.verify('alice-login').issuer,
        recipient_subject='alice', recipient_binding='alice-designated', expires_at=invitation['expires_at'])
    journal.save(operator, invite)
    with connect() as post:
        code, result = post('/command', dict(action='revoke', command_id=command))
        assert code == 403 and result['state'] == 'refused'
        assert post('/acknowledge', {'channel': 'revocation'})[0] == 200
        assert post('/acknowledge', {})[0] == 403
        assert journal.read(operator) == invite


def test_access_transition_exact_status_restart_and_stale_revision(onboarding):
    service = onboarding
    operation, ready, _, _, _ = invite_alice(service)
    command = dict(operation_id=operation, command_id='suspend-once',
                   access_state='suspended', expected_revision=0)
    assert service.access_status('operator-login', operation) == dict(
        customer_id=ready['customer_id'], operation_id=operation, provisioning_state='ready',
        access_state='active', revision=0)
    with pytest.raises(PermissionError):
        service.change_access('other-operator-login', **command)
    with pytest.raises(PermissionError):
        service.change_access('alice-login', **command)
    receipt = service.change_access('operator-login', **command)
    assert receipt['state'] == 'confirmed' and receipt['revision'] == 1
    fresh = Onboarding(service.provisioner, service.identities, service.delivery, validity_ceiling_seconds=600)
    assert fresh.access_command_status('operator-login', **command) == receipt
    assert fresh.change_access('operator-login', **command) == receipt
    with pytest.raises(PermissionError):
        fresh.change_access('operator-login', **dict(command, access_state='active'))
    stale = dict(command, command_id='stale', access_state='active')
    refused = fresh.change_access('operator-login', **stale)
    assert refused['state'] == 'refused'
    assert fresh.access_command_status('operator-login', **stale) == refused
    assert fresh.access_status('operator-login', operation)['access_state'] == 'suspended'


def test_suspension_denies_existing_cookie_and_keeps_exact_private_journal(onboarding, http_client):
    service = onboarding
    operation, ready, _, invitation, payload = invite_alice(service)
    request = dict(action='accept', invitation_id=invitation['invitation_id'],
                   secret=payload['secret'], command_id='original-accept')
    from nilaris.onboarding182_ui import AcceptanceJournal
    connect, journal = http_client
    alice = service.identities.verify('alice-login')
    land = dict(action='landing', customer_id=ready['customer_id'])
    with connect('alice-login') as post:
        assert post('/command', request)[1]['state'] == 'accepted'
        assert post('/command', land)[0] == 200
        change = dict(operation_id=operation, command_id='suspend', access_state='suspended', expected_revision=0)
        assert service.change_access('operator-login', **change)['state'] == 'confirmed'
        for path, body in [('/command', land), ('/resolve', {}), ('/pending', {}),
                           ('/acknowledge', {}), ('/command', request), ('/session', {'credential': 'alice-login'})]:
            code, denied = post(path, body)
            assert code == 403 and set(denied) == {'state', 'reason'}
        assert journal.read(alice, channel=AcceptanceJournal(ready['customer_id'])) == request
        assert service.change_access('operator-login', **dict(change, command_id='reactivate',
            access_state='active', expected_revision=1))['state'] == 'confirmed'
        assert post('/command', land)[0] == 403
        assert post('/resolve', {})[0] == 403
        assert post('/session', {'credential': 'alice-login'})[0] == 200
        assert post('/resolve', {})[1]['command_id'] == 'original-accept'
        assert post('/command', land)[1]['customer_id'] == ready['customer_id']


@pytest.mark.parametrize('operation_kind', ['landing', 'accept'])
@pytest.mark.parametrize('suspends_first', [True, False])
def test_committed_customer_boundary_orders_racing_requests(onboarding, monkeypatch, operation_kind, suspends_first):
    service = onboarding
    operation, ready, invitation_key, invitation, handoff = invite_alice(service)
    acceptance = ('alice-login', invitation['invitation_id'], handoff['secret'], 'racing-accept')
    if operation_kind == 'landing':
        service.accept(*acceptance)
    _, revisions = service.authenticate_session('alice-login')

    def customer():
        try:
            with service.customer_request('alice-login', revisions):
                if operation_kind == 'accept':
                    return service.accept(*acceptance)['state']
                return service.landing('alice-login', ready['customer_id'])['state']
        except PermissionError:
            return 'refused'

    def suspend():
        return service.change_access('operator-login', operation, 'racing-suspend', 'suspended', 0)['state']

    first, second = (suspend, customer) if suspends_first else (customer, suspend)
    results = ordered_transactions(service, monkeypatch, first, second,
                                   first_commits=2 if suspends_first else 1)
    intended = 'accepted' if operation_kind == 'accept' else 'active'
    assert results == (('confirmed', 'refused') if suspends_first else (intended, 'confirmed'))
    before_accept = suspends_first and operation_kind == 'accept'
    assert service.invitation_status('operator-login', invitation_key)['state'] == ('pending' if before_accept else 'accepted')
    service.change_access('operator-login', operation, 'fresh-access', 'active', 1)
    assert service.acceptance_status(*acceptance)['state'] == ('absent' if before_accept else 'accepted')
    if before_accept:
        with pytest.raises(PermissionError):
            service.landing('alice-login', ready['customer_id'])
    else:
        assert service.landing('alice-login', ready['customer_id'])['state'] == 'active'


def ordered_transactions(service, monkeypatch, first, second, *, first_commits=1):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event, local
    import psycopg

    held, release, connecting = Event(), Event(), Event()
    actor = local()
    pids = {}
    real_connect = psycopg.connect
    real_transaction = psycopg.Connection.transaction

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if getattr(actor, 'order', None) == 'second':
            pids['second'] = conn.info.backend_pid
            connecting.set()
        return conn

    @contextmanager
    def transaction(conn, *args, **kwargs):
        with real_transaction(conn, *args, **kwargs) as txn:
            yield txn
            if getattr(actor, 'order', None) == 'first':
                actor.transactions += 1
                if actor.transactions == first_commits:
                    pids['first'] = conn.info.backend_pid
                    held.set()
                    assert release.wait(6)

    def run(order, action):
        actor.order, actor.transactions = order, 0
        return action()

    with real_connect(**service.provisioner.admin, autocommit=True) as observer:
        monkeypatch.setattr(psycopg, 'connect', connect)
        monkeypatch.setattr(psycopg.Connection, 'transaction', transaction)
        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(run, 'first', first)
            try:
                assert held.wait(5)
                trailing = pool.submit(run, 'second', second)
                assert connecting.wait(3)
                deadline = time.monotonic() + 2
                while pids['first'] not in observer.execute(
                        'SELECT pg_blocking_pids(%s)', (pids['second'],)).fetchone()[0]:
                    assert time.monotonic() < deadline, 'competing backend never reached the shared boundary'
                    time.sleep(0.01)
            finally:
                release.set()
            return leading.result(timeout=6), trailing.result(timeout=6)


@pytest.mark.parametrize('revokes_first', [True, False])
def test_competing_revocation_and_acceptance_preserve_winning_outcome(onboarding, monkeypatch, revokes_first):
    service = onboarding
    _, _, invite_key, invitation, handoff = invite_alice(service)
    args = ('alice-login', invitation['invitation_id'], handoff['secret'], 'accept-race')

    def accept():
        try:
            return service.accept(*args)['state']
        except PermissionError:
            return 'refused'

    def revoke():
        try:
            return service.revoke('operator-login', invite_key)['state']
        except PermissionError:
            return 'refused'

    first, second = (revoke, accept) if revokes_first else (accept, revoke)
    assert ordered_transactions(service, monkeypatch, first, second) == (
        'revoked' if revokes_first else 'accepted', 'refused')
    assert service.invitation_status('operator-login', invite_key)['state'] == ('revoked' if revokes_first else 'accepted')
    assert service.acceptance_status(*args)['state'] == ('absent' if revokes_first else 'accepted')


def test_same_identity_two_customers_preserve_independent_landing_and_private_recovery(onboarding, http_client):
    service = onboarding
    a, target_a, _, invite_a, handoff_a = invite_alice(service)
    _, target_b, _, invite_b, handoff_b = invite_alice(service)
    request = dict(action='accept', invitation_id=invite_a['invitation_id'],
                   secret=handoff_a['secret'], command_id='recover-original')
    accept_b = dict(action='accept', invitation_id=invite_b['invitation_id'],
                    secret=handoff_b['secret'], command_id='accepted-b')
    select_b = dict(invitation_id=invite_b['invitation_id'])
    connect, journal = http_client
    alice = service.identities.verify('alice-login')
    # Crash after the private write, before dispatching the original acceptance.
    journal.save(alice, request)
    with connect('alice-login') as post:
        service.change_access('operator-login', a, 'suspend-a', 'suspended', 0)
        assert post('/command', accept_b)[1]['state'] == 'accepted'
        assert post('/acknowledge', {})[0] == 403
        assert post('/pending', dict(invitation_id=invite_a['invitation_id']))[0] == 403
        assert post('/pending', select_b)[1]['command']['command_id'] == 'accepted-b'
        assert post('/resolve', select_b)[1]['customer_id'] == target_b['customer_id']
        assert post('/acknowledge', select_b)[1]['state'] == 'acknowledged'
        assert post('/pending', select_b)[1]['state'] == 'absent'
        assert post('/command', dict(action='landing', customer_id=target_b['customer_id']))[0] == 200
        assert post('/command', dict(action='landing', customer_id=target_a['customer_id']))[0] == 403
        for path in ('/resolve', '/pending', '/retry', '/acknowledge'):
            assert post(path, {})[0] == 403
        assert journal.read(alice) == request
        service.change_access('operator-login', a, 'reactivate-a', 'active', 1)
        assert post('/command', request)[0] == 403
        assert post('/session', {'credential': 'alice-login'})[0] == 200
        assert post('/resolve', {})[1]['state'] == 'absent'
        assert post('/command', request)[1]['state'] == 'absent'
        assert post('/acknowledge', {})[0] == 403
        assert post('/retry', {})[1]['command_id'] == 'recover-original'
        assert post('/command', dict(action='landing', customer_id=target_a['customer_id']))[0] == 200


@pytest.mark.parametrize('commit_lost', [True, False], ids=['committed-response-lost', 'intent-only'])
def test_exact_access_recovers_with_read_only_resolution_after_uncertain_commit(
        onboarding, http_client, monkeypatch, commit_lost, tmp_path):
    from contextlib import contextmanager
    import psycopg
    service = onboarding
    operation, _, _, _, _ = invite_alice(service)
    connect, journal = http_client
    request = dict(action='change_access', operation_id=operation, command_id='original-suspend',
                   access_state='suspended', expected_revision=0)
    channel = {'channel': 'access'}
    real_transaction = psycopg.Connection.transaction
    transactions = 0

    @contextmanager
    def uncertain(conn, *args, **kwargs):
        nonlocal transactions
        transactions += 1
        transition = transactions == 3  # Owned-target preflight, intent, then transition.
        with real_transaction(conn, *args, **kwargs) as txn:
            yield txn
            if transition and not commit_lost:
                raise psycopg.OperationalError('controlled disconnect before commit')
        if transition and commit_lost:
            raise psycopg.OperationalError('controlled disconnect after commit')

    with connect() as post:
        with monkeypatch.context() as patch:
            patch.setattr(psycopg.Connection, 'transaction', uncertain)
            assert post('/command', request)[0] == 503
        assert post('/acknowledge', channel)[0] == 403
    operator = service.identities.verify('operator-login')
    assert journal.read(operator, channel='access') == request
    if commit_lost:
        receipt, current = cold_readback(tmp_path, issuer=operator.issuer, operation_id=operation)
        assert (receipt['command_id'], receipt['state']) == ('original-suspend', 'confirmed')
        assert (current['access_state'], current['revision']) == ('suspended', 1)
    with connect('other-operator-login') as wrong:
        assert wrong('/pending', channel)[1]['state'] == 'absent'
        assert wrong('/command', dict(request, action='access_command_status'))[0] == 403
    with connect() as fresh:
        receipt = fresh('/resolve', channel)[1]
        assert receipt['command_id'] == 'original-suspend'
        assert receipt['state'] == ('confirmed' if commit_lost else 'in_progress')
        current = fresh('/command', dict(action='access_status', operation_id=operation))[1]
        assert (current['access_state'], current['revision']) == (('suspended', 1) if commit_lost else ('active', 0))
        if not commit_lost:
            assert fresh('/acknowledge', channel)[0] == 403
            assert fresh('/command', request)[1] == receipt
        final = fresh('/retry', channel)[1]
        assert final['state'] == 'confirmed' and final['revision'] == 1
        assert fresh('/resolve', channel)[1] == final
        assert fresh('/acknowledge', channel)[0] == 200
    assert journal.read(operator, channel='access') is None


def test_expired_session_survives_as_exact_unresolved_command_only(onboarding, http_client, monkeypatch):
    from types import SimpleNamespace
    import nilaris.onboarding182_ui as ui
    service = onboarding
    operation, _, _, invite, handoff = invite_alice(service)
    connect, journal = http_client
    principal = service.identities.verify('alice-login')
    request = dict(action='accept', invitation_id=invite['invitation_id'], secret=handoff['secret'], command_id='pending')
    journal.save(principal, request)
    tick = [100.0]
    monkeypatch.setattr(ui, 'time', SimpleNamespace(monotonic=lambda: tick[0]))
    with connect('alice-login') as post:
        tick[0] += 61
        assert post('/pending', {})[0] == 403
        service.change_access('operator-login', operation, 'suspend', 'suspended', 0)
        assert post('/session', {'credential': 'alice-login'})[0] == 403
        service.change_access('operator-login', operation, 'reactivate', 'active', 1)
        assert post('/session', {'credential': 'alice-login'})[0] == 200
        assert post('/resolve', {})[1]['state'] == 'absent'
        assert post('/acknowledge', {})[0] == 403
    assert journal.read(principal) == request


def test_http_independent_customer_completes_while_suspension_is_uncommitted(onboarding, http_client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event
    import psycopg
    service = onboarding
    operation_a, _, _, invite_a, handoff_a = invite_alice(service)
    operation_b, b, invite_key_b, invitation, handoff = invite_alice(service)
    original = dict(action='accept', invitation_id=invite_a['invitation_id'],
                    secret=handoff_a['secret'], command_id='retain-a')
    accept_b = dict(action='accept', invitation_id=invitation['invitation_id'],
                    secret=handoff['secret'], command_id='accepted-b')
    held, release = Event(), Event()
    real_transaction = psycopg.Connection.transaction
    transitions = 0
    suspension_pid = None

    @contextmanager
    def transaction(conn, *args, **kwargs):
        nonlocal transitions, suspension_pid
        with real_transaction(conn, *args, **kwargs) as txn:
            yield txn
            transitions += 1
            if transitions == 3:  # Hold the transition after the preflight and intent.
                suspension_pid = conn.info.backend_pid
                held.set()
                assert release.wait(9)

    connect, journal = http_client
    principal = service.identities.verify('alice-login')
    journal.save(principal, original)
    with connect(second_credential='alice-login') as (operator, alice), psycopg.connect(
            **service.provisioner.admin, autocommit=True) as observer:
        monkeypatch.setattr(psycopg.Connection, 'transaction', transaction)
        with ThreadPoolExecutor(max_workers=2) as pool:
            suspension = pool.submit(operator, '/command', dict(action='change_access',
                operation_id=operation_a, command_id='suspend-a', access_state='suspended', expected_revision=0))
            try:
                assert held.wait(4)
                recovery_a = pool.submit(alice, '/resolve', {})
                deadline = time.monotonic() + 3
                while not observer.execute('SELECT EXISTS (SELECT FROM pg_stat_activity '
                        'WHERE %s = ANY(pg_blocking_pids(pid)))', (suspension_pid,)).fetchone()[0]:
                    assert time.monotonic() < deadline, 'recipient A never reached the suspension boundary'
                    time.sleep(0.01)
                busy = operator('/command', dict(action='change_access', operation_id=operation_b,
                    command_id='different-access', access_state='suspended', expected_revision=0))
                assert (busy[0], busy[1]['state']) == (409, 'busy')
                assert operator('/pending', {'channel': 'access'})[1]['command']['command_id'] == 'suspend-a'
                assert operator('/command', dict(action='invitation_status', command_id=invite_key_b))[1]['state'] == 'pending'
                assert alice('/command', accept_b)[1]['state'] == 'accepted'
                assert alice('/command', dict(action='landing', customer_id=b['customer_id']))[1]['state'] == 'active'
                selected = dict(invitation_id=invitation['invitation_id'])
                assert alice('/pending', selected)[1]['command']['command_id'] == 'accepted-b'
                assert alice('/resolve', selected)[1]['customer_id'] == b['customer_id']
                assert alice('/acknowledge', selected)[1]['state'] == 'acknowledged'
            finally:
                release.set()
            assert suspension.result(timeout=5)[1]['state'] == 'confirmed'
            assert recovery_a.result(timeout=5)[0] == 403
        assert journal.read(principal) == original


def test_operator_can_acknowledge_refused_invitation_and_invite_independent_customer(onboarding, http_client):
    service = onboarding
    a, b = ['http-' + uuid.uuid4().hex for _ in range(2)]
    for operation in (a, b):
        service.provision('operator-login', operation)
    service.change_access('operator-login', a, 'suspend-a', 'suspended', 0)
    connect, _ = http_client
    with connect() as post:
        invite = dict(action='invite', operation_id=a, command_id='invite-a',
            recipient_issuer=service.identities.verify('alice-login').issuer,
            recipient_subject='alice', recipient_binding='alice-designated', expires_at=time.time() + 300)
        assert post('/command', invite)[0] == 403
        assert post('/acknowledge', {})[1]['state'] == 'acknowledged'
        assert post('/command', dict(invite, operation_id=b, command_id='invite-b'))[1]['delivery'] == 'delivered'


def test_deliberate_retry_after_revocation_can_acknowledge_definite_refusal(onboarding, http_client):
    service = onboarding
    _, _, command, invitation, handoff = invite_alice(service)
    connect, journal = http_client
    principal = service.identities.verify('alice-login')
    saved = dict(action='accept', invitation_id=invitation['invitation_id'], secret=handoff['secret'], command_id='redeem')
    journal.save(principal, saved)
    with connect('alice-login') as post:
        assert post('/resolve', {})[1]['state'] == 'absent'
        assert post('/acknowledge', {})[0] == 403
        service.revoke('operator-login', command)
        assert post('/retry', {})[0] == 403
        assert post('/acknowledge', {})[1]['state'] == 'acknowledged'
        assert post('/pending', {})[1]['state'] == 'absent'


@pytest.mark.parametrize('unbound', ['nonexistent', 'wrong-recipient', 'invalid'])
def test_fresh_session_acknowledges_legacy_known_unbound_refusal(onboarding, http_client, unbound):
    service = onboarding
    _, _, _, invitation, handoff = invite_alice(service)
    connect, journal = http_client
    principal = service.identities.verify('bob-login')
    invitation_id = {'nonexistent': 'mistyped', 'wrong-recipient': invitation['invitation_id'],
                     'invalid': None}[unbound]
    saved = dict(action='accept', invitation_id=invitation_id, secret=handoff['secret'], command_id='legacy-refusal')
    # 02A persisted the exact invalid body plus an empty .known marker on refusal.
    journal.save(principal, saved)
    with connect('bob-login') as post:
        assert post('/acknowledge', {})[0] == 403
    journal.mark_known(principal)
    with connect('bob-login') as post:
        assert post('/acknowledge', {})[1]['state'] == 'acknowledged'
        assert post('/pending', {})[1]['state'] == 'absent'


@pytest.mark.parametrize('unbound', ['nonexistent', 'wrong-recipient', 'malformed'])
def test_acknowledge_original_legacy_refusal_with_suspended_scoped_command(onboarding, http_client, unbound):
    from nilaris.onboarding182_ui import AcceptanceJournal
    service = onboarding
    invite_alice(service)  # A separate active customer keeps this recipient session usable.
    operation, customer, _, invitation, handoff = invite_alice(service)
    scoped = dict(action='accept', command_id='uncertain', invitation_id=invitation['invitation_id'],
                  secret=handoff['secret'])
    alice = service.identities.verify('alice-login')
    wrong = service.provision('operator-login', 'wrong-' + uuid.uuid4().hex)
    other = service.invite('operator-login', wrong['operation_id'], 'invite-bob', alice.issuer,
                           'bob', 'bob-designated', time.time() + 300)
    invalid_id = {'nonexistent': 'mistyped', 'wrong-recipient': other['invitation_id'], 'malformed': None}[unbound]
    legacy = dict(scoped, command_id='old-refusal', invitation_id=invalid_id)
    connect, journal = http_client
    journal.save(alice, legacy)
    scoped_channel = AcceptanceJournal(customer['customer_id'])
    journal.save(alice, scoped, channel=scoped_channel)
    service.change_access('operator-login', operation, 'pause', 'suspended', 0)
    selector = dict(command_id='old-refusal') if unbound == 'malformed' else dict(invitation_id=invalid_id)
    with connect('alice-login') as post:
        assert post('/acknowledge', selector)[0] == 403
        if unbound != 'malformed':
            assert post('/resolve', selector)[0] == 403
            assert post('/acknowledge', selector)[0] == 403
        journal.mark_known(alice)
        assert post('/acknowledge', {})[0] == 403
        assert post('/acknowledge', {'command_id': 'different'})[0] == 403
        assert post('/acknowledge', selector) == (200, {'state': 'acknowledged'})
        assert journal.read(alice) is None
        assert journal.read(alice, channel=scoped_channel) == scoped


@pytest.mark.parametrize('invalid', ['mistyped-secret', 'short-secret', 'malformed-command', 'receipt-collision'])
def test_resolve_interrupted_invalid_acceptance_before_deliberate_correction(onboarding, http_client, invalid):
    from nilaris.onboarding182_ui import AcceptanceJournal
    service = onboarding
    _, customer, _, invitation, handoff = invite_alice(service)
    original = dict(action='accept', command_id='interrupted', invitation_id=invitation['invitation_id'],
                    secret=handoff['secret'])
    if invalid == 'receipt-collision':
        _, _, _, other, payload = invite_alice(service)
        service.accept('alice-login', other['invitation_id'], payload['secret'], original['command_id'])
    elif invalid == 'malformed-command':
        original['command_id'] = []
    else:
        original['secret'] = 'wrong' if invalid == 'short-secret' else 'x' * 42
    connect, journal = http_client
    principal = service.identities.verify('alice-login')
    channel = AcceptanceJournal(customer['customer_id'])
    journal.save(principal, original, channel=channel)  # Interrupted before dispatch.
    selected = dict(invitation_id=original['invitation_id'])
    corrected = dict(original, command_id='corrected', secret=handoff['secret'])
    with connect('alice-login') as post:
        assert post('/acknowledge', selected)[0] == 403
        assert post('/command', corrected)[0] == 403
        operator = service.provisioner.verifier.credentials.pop('operator-login')
        try:
            assert post('/resolve', selected)[0] == 403
            assert post('/acknowledge', selected)[0] == 403
        finally:
            service.provisioner.verifier.credentials['operator-login'] = operator
        assert post('/resolve', selected) == (200, {'state': 'refused', 'reason': 'original acceptance refused'})
        assert post('/command', corrected)[0] == 403
        assert post('/acknowledge', selected)[1]['state'] == 'acknowledged'
        assert post('/command', corrected)[1]['state'] == 'accepted'


def test_replacement_invitation_cannot_acknowledge_original_acceptance(onboarding, http_client):
    service = onboarding
    operation, _, issue_key, first, handoff = invite_alice(service)
    original = dict(action='accept', command_id='original', invitation_id=first['invitation_id'],
                    secret=handoff['secret'])
    service.revoke('operator-login', issue_key)
    principal = service.identities.verify('alice-login')
    replacement = service.invite('operator-login', operation, 'replacement', principal.issuer,
                                principal.subject, principal.recipient, time.time() + 300)
    selected = dict(invitation_id=replacement['invitation_id'])
    replacement_handoff = json.loads((service.delivery.directory / replacement['delivery_reference']).read_text())
    connect, _ = http_client
    with connect('alice-login') as post:
        assert post('/command', original)[0] == 403
        assert post('/command', dict(original, **selected, secret=replacement_handoff['secret']))[0] == 403
        assert post('/acknowledge', selected)[0] == 403
        assert post('/pending', dict(invitation_id=first['invitation_id']))[1]['command']['command_id'] == 'original'
        assert post('/acknowledge', dict(invitation_id=first['invitation_id']))[1]['state'] == 'acknowledged'


if __name__ == '__main__':
    from nilaris.onboarding182_ui import PrivateCommandJournal
    from nilaris.provisioning181 import _private_json

    descriptor = Path(sys.argv[1])
    request = _private_json(descriptor)
    service = controlled_service(os.environ['NILARIS_ONBOARDING182_TEST_CONFIG'],
                                 request.pop('issuer'), descriptor.parent)
    if 'operation_id' in request:
        journal = PrivateCommandJournal(descriptor.parent / 'journal')
        saved = journal.read(service.identities.verify('operator-login'), channel='access')
        saved.pop('action')
        result = [service.access_command_status('operator-login', **saved),
                  service.access_status('operator-login', request['operation_id'])]
    else:
        result = service.acceptance_status('alice-login', **request)
    print(json.dumps(result))


@pytest.mark.parametrize('field,value', [('operation_id', None), ('command_id', []), ('access_state', 'hold'),
                                        ('expected_revision', False), ('expected_revision', 2**63)])
def test_invalid_access_body_never_occupies_journal_and_saved_original_recovers(onboarding, http_client, field, value):
    service = onboarding
    operation, _, _, _, _ = invite_alice(service)
    valid = dict(action='change_access', operation_id=operation, command_id='pause',
                 expected_revision=0, access_state='suspended')
    invalid = dict(valid, **{field: value})
    connect, journal = http_client
    selector = {'channel': 'access'}
    operator = service.identities.verify('operator-login')
    with connect() as post:
        assert post('/command', invalid)[0] == 400
        assert post('/pending', selector)[1]['state'] == 'absent'
    journal.save(operator, invalid, channel='access')  # Previously saved before validation or a known marker.
    with connect() as post:
        assert post('/command', valid)[0] == 403
        assert post('/acknowledge', selector)[0] == 403
        assert post('/resolve', selector) == (200, {'state': 'refused', 'reason': 'original access command refused'})
        assert post('/retry', selector)[1]['state'] == 'refused'
        assert post('/acknowledge', selector)[1]['state'] == 'acknowledged'
        assert post('/command', valid)[1]['revision'] == 1


@pytest.mark.parametrize('locator', [None, [], 'omitted'])
def test_resolve_interrupted_legacy_invalid_locator_among_suspended_commands(onboarding, http_client, locator):
    from nilaris.onboarding182_ui import AcceptanceJournal
    service = onboarding
    invite_alice(service)
    op, customer, _, invitation, handoff = invite_alice(service)
    connect, journal = http_client
    alice = service.identities.verify('alice-login')
    scoped = dict(action='accept', invitation_id=invitation['invitation_id'], command_id='scoped', secret=handoff['secret'])
    channel = AcceptanceJournal(customer['customer_id'])
    journal.save(alice, scoped, channel=channel)
    legacy = dict(action='accept', command_id='legacy', secret=handoff['secret'])
    if locator != 'omitted':
        legacy['invitation_id'] = locator
    journal.save(alice, legacy)
    service.change_access('operator-login', op, 'pause', 'suspended', 0)
    selector = {'command_id': 'legacy'}
    with connect('alice-login') as post:
        assert post('/resolve', {})[0] == 403
        assert post('/resolve', {'command_id': 'scoped'})[0] == 403
        assert post('/acknowledge', selector)[0] == 403
        assert post('/resolve', selector) == (200, {'state': 'refused', 'reason': 'original acceptance refused'})
        assert post('/acknowledge', selector)[1]['state'] == 'acknowledged'
        assert post('/pending', {'invitation_id': invitation['invitation_id']})[0] == 403
        assert journal.read(alice, channel=channel) == scoped


def test_access_receipt_collision_resolves_only_the_private_saved_body(onboarding, http_client):
    service = onboarding
    a, _, _, _, _ = invite_alice(service)
    b, _, _, _, _ = invite_alice(service)
    original = dict(operation_id=a, command_id='same', access_state='suspended', expected_revision=0)
    confirmed = service.change_access('operator-login', **original)
    collision = dict(original, action='change_access', operation_id=b)
    connect, journal = http_client
    operator = service.identities.verify('operator-login')
    selector = {'channel': 'access'}
    journal.save(operator, collision, channel='access')
    with connect('other-operator-login') as other:
        assert other('/resolve', selector)[1]['state'] == 'absent'
    with connect() as post:
        assert post('/acknowledge', selector)[0] == 403
        assert post('/resolve', selector) == (200, {'state': 'refused', 'reason': 'original access command refused'})
        assert post('/retry', selector)[1]['state'] == 'refused'
        assert post('/acknowledge', selector)[1]['state'] == 'acknowledged'
        assert post('/command', dict(original, action='access_command_status')) == (200, confirmed)
        assert post('/command', dict(operation_id=b, action='access_status'))[1]['revision'] == 0


def test_administrator_invites_colleague_and_reads_redeemed_roles(onboarding):
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    customer = ready['customer_id']
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'bootstrap')
    issuer = service.identities.verify('bob-login').issuer
    command = dict(kind='invite', roles=['Reviewer'], recipient_issuer=issuer,
                   recipient_subject='bob', recipient_binding='bob-designated',
                   expires_at=time.time() + 300)
    result = service.manage('alice-login', customer, 'invite-bob', 0, command)
    delivery = json.loads((service.delivery.directory / result['delivery_reference']).read_text())
    service.accept('bob-login', delivery['invitation_id'], delivery['secret'], 'join')
    assert service.landing('bob-login', customer)['bundles'] == ['Reviewer']
    roster = service.members('alice-login', customer)
    assert [(m['subject'], m['roles']) for m in roster['users']] == [
        ('alice', ['Administrator']), ('bob', ['Reviewer'])]


def test_membership_role_change_fences_session_and_preserves_final_admin(onboarding):
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    customer = ready['customer_id']
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], 'bootstrap')
    original = service.members('alice-login',customer)
    target = original['users'][0]['membership_id']
    for command in (dict(kind='roles', target=target, roles=['Administrator','Approver']),
                    dict(kind='roles', target=target, roles=['Reviewer']), dict(kind='deactivate', target=target)):
        receipt = service.manage('alice-login',customer,uuid.uuid4().hex,0,command)
        assert receipt['state'] == 'refused'
    assert service.members('alice-login',customer) == original


def test_management_session_reads_roster_and_recovers_exact_command(onboarding, http_client):
    _, ready, _, invitation, payload = invite_alice(onboarding)
    customer = ready['customer_id']
    onboarding.accept('alice-login', invitation['invitation_id'], payload['secret'], 'bootstrap')
    with http_client[0]('alice-login') as post:
        status, roster = post('/command', dict(action='members',customer_id=customer))
        assert status == 200
        command = dict(action='manage',customer_id=customer,command_id='safe',expected_revision=0,
                       command=dict(kind='deactivate',target=roster['users'][0]['membership_id']))
        assert post('/command',command)[1]['state'] == 'refused'
    with http_client[0]('alice-login') as post:
        assert post('/resolve',dict(management_customer=customer))[1]['reason'] == 'last active Administrator required'


def customer_and_invitation(service, roles, *, bootstrap_command="bootstrap"):
    _, ready, _, invitation, payload = invite_alice(service)
    customer = ready['customer_id']
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], bootstrap_command)
    bob = service.identities.verify('bob-login')
    args = ('alice-login',customer,'invite-bob',0,dict(kind='invite',roles=roles,
            recipient_issuer=bob.issuer,recipient_subject=bob.subject,recipient_binding=bob.recipient,
            expires_at=time.time()+300))
    result = service.manage(*args)
    delivery = json.loads((service.delivery.directory / result['delivery_reference']).read_text())
    return customer, args, ('bob-login',delivery['invitation_id'],delivery['secret'],'accept-bob')


@pytest.mark.parametrize('first_kind', ['roles','deactivate'])
def test_competing_administrators_cannot_remove_each_other(onboarding,monkeypatch,first_kind):
    service = onboarding
    customer, _, redeem = customer_and_invitation(service,['Administrator'])
    service.accept(*redeem)
    users = service.members('alice-login',customer)['users']
    def remove(actor,target,kind):
        body = dict(kind=kind,target=target['membership_id'])
        if kind == 'roles':
            body['roles'] = ['Reviewer']
        try:
            return service.manage(actor,customer,'remove-other',2,body)['state']
        except PermissionError:
            return 'refused'
    first = lambda: remove('alice-login',users[1],first_kind)
    second = lambda: remove('bob-login',users[0],'deactivate')
    assert ordered_transactions(service,monkeypatch,first,second) == (
        'updated' if first_kind=='roles' else 'deactivated','refused')
    assert service.members('alice-login',customer)['users'][0] == users[0]


@pytest.mark.parametrize('revokes_first',[True,False])
def test_ordinary_invitation_revoke_redemption_order(onboarding,monkeypatch,revokes_first):
    service = onboarding
    customer, _, redeem = customer_and_invitation(service,['Administrator'])
    def accept():
        try:
            return service.accept(*redeem)['state']
        except PermissionError:
            return 'refused'
    def revoke():
        return service.manage('alice-login',customer,'revoke-bob',1,dict(kind='revoke',target=redeem[1]))['state']
    first,second = (revoke,accept) if revokes_first else (accept,revoke)
    assert ordered_transactions(service,monkeypatch,first,second) == (
        'revoked' if revokes_first else 'accepted','refused')


def test_unknown_membership_delivery_resolves_without_resending(onboarding,monkeypatch):
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    customer = ready['customer_id']
    service.accept('alice-login',invitation['invitation_id'],payload['secret'],'bootstrap')
    def fail(*args):
        raise OSError('controlled transport failure')
    monkeypatch.setattr(service.delivery,'deliver',fail)
    args = ('alice-login',customer,'invite-bob',0,dict(kind='invite',roles=['Reviewer'],
            recipient_issuer=service.identities.verify('bob-login').issuer,recipient_subject='bob',
            recipient_binding='bob-designated',expires_at=time.time()+300))
    first = service.manage(*args)
    assert first['delivery'] == 'unknown'
    assert service.management_status(*args) == first
    assert service.manage(*args) == first


def test_ordinary_invitation_needs_current_issuer_authority(onboarding):
    service = onboarding
    _, _, redeem = customer_and_invitation(service,['Approver'])
    service.identities.revoke('alice-login')
    with pytest.raises(PermissionError):
        service.accept(*redeem)


@pytest.mark.parametrize('removes_first', [True,False])
def test_metadata_mutation_orders_with_existing_session_removal(onboarding,monkeypatch,removes_first):
    service = onboarding
    customer, _, redeem = customer_and_invitation(service,['Administrator'])
    service.accept(*redeem)
    users = service.members('alice-login',customer)['users']
    _, session = service.authenticate_session('bob-login')
    def remove():
        return service.manage('alice-login',customer,'remove',2,
            dict(kind='deactivate',target=users[1]['membership_id']))['state']
    def edit():
        try:
            with service.customer_request('bob-login',session):
                return service.manage('bob-login',customer,'grant',2,
                    dict(kind='roles',target=users[0]['membership_id'],roles=['Administrator','Approver']))['state']
        except PermissionError:
            return 'refused'
    first,second = (remove,edit) if removes_first else (edit,remove)
    assert ordered_transactions(service,monkeypatch,first,second) == (
        'deactivated' if removes_first else 'updated','refused')
    roster = service.members('alice-login',customer)
    assert ('Approver' in roster['users'][0]['roles']) is (not removes_first)
    assert roster['users'][1]['active'] is (not removes_first)


def test_administrator_authority_requires_session_identity_and_own_customer(onboarding,http_client):
    service = onboarding
    customer, original, redeem = customer_and_invitation(service,['Reviewer'])
    service.accept(*redeem)
    _, other, _, _, _ = invite_alice(service)
    with http_client[0]('alice-login',second_credential='bob-login') as (admin,reviewer):
        view = dict(action='members',customer_id=customer)
        assert reviewer('/command',view)[0] == 403
        assert admin('/command',dict(view,customer_id=other['customer_id']))[0] == 403
        assert admin('/command',dict(view,actor='bob'))[0] == 400
        assert reviewer('/resolve',dict(management_customer=customer))[0] == 403
        assert admin('/command',dict(action='landing',customer_id=customer))[1]['actions'] == ['administrative_metadata']
        roles = dict(action='manage',customer_id=customer,command_id='invalid-roles',expected_revision=2,
                     command=dict(kind='roles',target=redeem[1],roles=['Support']))
        assert admin('/command',roles)[0] == 400
        assert admin('/pending',dict(management_customer=customer))[1]['state'] == 'absent'
    for subject in ('alice','operator-a'):
        command = dict(original[-1],recipient_subject=subject,recipient_binding='different-binding',roles=['Approver'])
        assert service.manage('alice-login',customer,'invite-'+subject,2,command)['state'] == 'refused'
    with pytest.raises(PermissionError):
        service.members('operator-login',customer)


def test_explicit_management_retry_restores_exact_uncommitted_body(onboarding,http_client):
    service = onboarding
    customer, _, redeem = customer_and_invitation(service,['Reviewer'])
    service.accept(*redeem)
    connect,journal = http_client
    from nilaris.onboarding182_ui import ManagementJournal
    body = dict(action='manage',customer_id=customer,command_id='saved-command',expected_revision=2,
                command=dict(kind='roles',target=redeem[1],roles=['Approver']))
    principal = service.identities.verify('alice-login')
    journal.save(principal,body,channel=ManagementJournal(customer))
    selector = dict(management_customer=customer)
    with connect('alice-login') as post:
        assert post('/resolve',selector)[1]['state'] == 'absent'
        assert post('/acknowledge',selector)[0] == 403
        assert post('/command',body)[1]['state'] == 'absent'
        assert service.landing('bob-login',customer)['bundles'] == ['Reviewer']
        assert post('/retry',selector)[1]['state'] == 'updated'
    with connect('alice-login') as post:
        assert post('/resolve',selector)[1]['roles'] == ['Approver']
        assert post('/acknowledge',selector)[1]['state'] == 'acknowledged'
        assert service.landing('bob-login',customer)['bundles'] == ['Approver']


def test_replacement_requires_revocation_and_list_contains_only_pending(onboarding):
    service = onboarding
    customer, original, redeem = customer_and_invitation(service,['Administrator'])
    replacement = dict(original[-1],roles=['Reviewer'])
    assert service.manage('alice-login',customer,'replacement',1,replacement)['state'] == 'refused'
    assert service.manage('alice-login',customer,'withdraw',1,dict(kind='revoke',target=redeem[1]))['state'] == 'revoked'
    assert service.members('alice-login',customer)['invitations'] == []
    assert service.management_status(*original)['state'] == 'revoked'
    assert service.manage('alice-login',customer,'narrower',2,replacement)['state'] == 'pending'
    with pytest.raises(PermissionError):
        service.accept(*redeem)


def test_other_administrator_grants_existing_admin_by_independent_redemption(onboarding):
    service = onboarding
    customer, _, redeem = customer_and_invitation(service,['Administrator'])
    service.accept(*redeem)
    actor = service.identities.verify('alice-login')
    command = dict(kind='invite',roles=['Approver'],recipient_issuer=actor.issuer,
                   recipient_subject=actor.subject,recipient_binding=actor.recipient,expires_at=time.time()+300)
    result = service.manage('bob-login',customer,'appoint-alice',2,command)
    assert result['state'] == 'pending'
    payload = json.loads((service.delivery.directory/result['delivery_reference']).read_text())
    args = (payload['invitation_id'],payload['secret'],'accept-appointment')
    with pytest.raises(PermissionError):
        service.accept('bob-login',*args)
    receipt = service.accept('alice-login',*args)
    assert service.landing('alice-login',customer)['bundles'] == ['Administrator','Approver']
    roster = service.members('alice-login',customer)
    bob = next(user for user in roster['users'] if user['subject']=='bob')
    service.manage('alice-login',customer,'demote-bob',roster['revision'],
                   dict(kind='roles',target=bob['membership_id'],roles=['Reviewer']))
    assert service.acceptance_status('alice-login',*args) == receipt
    assert service.accept('alice-login',*args) == receipt


def test_same_cookie_loses_a_membership_while_b_stays_available(onboarding,http_client):
    service = onboarding
    customer_a, _, grant_a = customer_and_invitation(service,['Administrator','Reviewer'])
    service.accept(*grant_a)
    _, ready_b, _, invitation_b, payload_b = invite_alice(service)
    customer_b = ready_b['customer_id']
    service.accept('alice-login', invitation_b['invitation_id'],payload_b['secret'],'bootstrap-b')
    bob = service.identities.verify('bob-login')
    issued = service.manage('alice-login',customer_b,'issue-b',0,dict(kind='invite',roles=['Administrator'],
        recipient_issuer=bob.issuer,recipient_subject=bob.subject,recipient_binding=bob.recipient,
        expires_at=time.time()+300))
    delivery = json.loads((service.delivery.directory / issued['delivery_reference']).read_text())
    service.accept('bob-login',issued['invitation_id'],delivery['secret'],'join-b')
    with http_client[0]('bob-login',second_credential='alice-login') as (colleague,admin):
        request_a = dict(action='members',customer_id=customer_a)
        request_b = dict(action='members',customer_id=customer_b)
        roster_b = colleague('/command',request_b)
        assert roster_b[0] == 200
        assert colleague('/command',request_a)[0] == 200
        result = admin('/command',dict(action='manage',customer_id=customer_a,command_id='deactivate-a',
            expected_revision=2,command=dict(kind='deactivate',target=grant_a[1])))
        assert result[1]['state'] == 'deactivated'
        assert colleague('/command',request_a)[0] == 403
        assert colleague('/command',request_b) == roster_b


def test_stale_monitor_after_issuance_keeps_delivery_unknown(onboarding,monkeypatch):
    from nilaris.provisioning181 import ProvisioningRefused
    service = onboarding
    _, ready, _, invitation, payload = invite_alice(service)
    service.accept('alice-login',invitation['invitation_id'],payload['secret'],'bootstrap')
    actor = service.identities.verify('bob-login')
    args = ('alice-login',ready['customer_id'],'guard-loss',0,dict(kind='invite',roles=['Reviewer'],
            recipient_issuer=actor.issuer,recipient_subject=actor.subject,recipient_binding=actor.recipient,
            expires_at=time.time()+300))
    def stale():
        raise ProvisioningRefused('fresh service monitor required')
    with service.delivery_boundary():
        receipt = service.manage(*args)
        monkeypatch.setattr(service.provisioner,'_require_monitor',stale)
    assert service.management_status(*args)['delivery'] == 'unknown'
    assert not (service.delivery.directory / receipt['delivery_reference']).exists()


def test_redemption_rechecks_current_permitted_bundles(onboarding,monkeypatch):
    from nilaris import membership185
    customer, _, redeem = customer_and_invitation(onboarding,['Reviewer'])
    monkeypatch.setattr(membership185,'BUNDLES',{'Administrator','Approver'})
    with pytest.raises(PermissionError):
        onboarding.accept(*redeem)
    assert onboarding.members('alice-login',customer)['invitations'][0]['state'] == 'pending'


@pytest.mark.parametrize('end_session',['logout','expiry'])
def test_management_waiter_rechecks_cookie_after_lock(onboarding,http_client,monkeypatch,end_session):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import psycopg
    import nilaris.onboarding182_ui as ui

    customer, _, redeem = customer_and_invitation(onboarding,['Reviewer'])
    onboarding.accept(*redeem)
    tick = [time.monotonic()]
    monkeypatch.setattr(ui,'time',SimpleNamespace(monotonic=lambda:tick[0],sleep=time.sleep))
    body = dict(action='manage',customer_id=customer,command_id='blocked-grant',expected_revision=2,
                command=dict(kind='roles',target=redeem[1],roles=['Approver']))
    with http_client[0]('alice-login') as post, ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(**onboarding.provisioner.admin,autocommit=True) as observer, observer.transaction():
            observer.execute('SELECT 1 FROM provision181_operations WHERE customer_id=%s FOR UPDATE',(customer,))
            grant = pool.submit(post,'/command',body)
            deadline = time.monotonic()+2
            while not observer.execute('SELECT EXISTS (SELECT FROM pg_stat_activity WHERE pg_backend_pid() = ANY(pg_blocking_pids(pid)))').fetchone()[0]:
                observer.execute("SELECT pg_stat_clear_snapshot()")
                assert time.monotonic()<deadline
                time.sleep(.01)
            if end_session=='logout':
                assert post('/logout',{})[0] == 200
            else:
                tick[0] += 61
        assert grant.result(timeout=3)[0] == 403
    assert onboarding.landing('bob-login',customer)['bundles'] == ['Reviewer']


@pytest.mark.parametrize('change',['session','membership','customer'])
def test_private_handoff_preserves_originating_authority_after_commit(onboarding,change):
    service = onboarding
    customer, _, redeem = customer_and_invitation(service,['Administrator'])
    service.accept(*redeem)
    principal,revisions = service.authenticate_session('alice-login')
    roster = service.members('alice-login',customer)
    permitted = True
    def check_session():
        if not permitted:
            raise PermissionError('session expired')
    with service.delivery_boundary():
        with service.customer_request('alice-login',revisions,check_session=check_session):
            result = service.manage('alice-login',customer,'handoff',2,dict(kind='invite',
                roles=['Reviewer'],recipient_issuer=principal.issuer,recipient_subject='third',
                recipient_binding='third-designated',expires_at=time.time()+300))
        if change == 'session':
            permitted = False
        elif change == 'membership':
            service.manage('bob-login',customer,'change-originator',3,dict(kind='roles',
                target=roster['users'][0]['membership_id'],roles=['Administrator','Reviewer']))
        else:
            operation = service.provisioner.inventory('operator-login')
            operation_id = next(row['operation_id'] for row in operation if row['customer_id']==customer)
            service.change_access('operator-login',operation_id,'suspend','suspended',0)
            service.change_access('operator-login',operation_id,'reactivate','active',1)
    assert result['delivery'] == 'unknown'
    assert not (service.delivery.directory/result['delivery_reference']).exists()
