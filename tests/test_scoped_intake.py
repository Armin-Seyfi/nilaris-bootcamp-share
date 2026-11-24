"""Customer-facing and worker boundaries on the root-owned disposable PostgreSQL."""
import sys
from pathlib import Path
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import uuid

from test_onboarding182_pg import invite_alice, onboarding
from nilaris.scoped_intake import ScopedIntake, SourceBinding


def setup_source(service):
    operation, ready, _, invitation, payload = invite_alice(service)
    service.accept('alice-login', invitation['invitation_id'], payload['secret'], uuid.uuid4().hex)
    principal, revisions = service.authenticate_session('alice-login')
    customer = ready['customer_id']
    binding = SourceBinding(principal.issuer, principal.subject, 'equipment', principal.recipient)
    return ScopedIntake(service, {customer: binding}), customer, revisions, operation


def test_absent_disclosure_policy_holds_original_customer_job_across_restart(onboarding):
    intake, customer, revisions, _ = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        submitted = intake.admit(customer, 'one-item', 'pump-sheet')
        assert submitted['state'] == 'held'
        assert submitted['reason'] == 'disclosure_policy_absent'
    fresh = ScopedIntake(onboarding, intake.bindings)
    with onboarding.customer_request('alice-login', revisions):
        assert fresh.job(customer, submitted['job_id']) == submitted
        assert fresh.admit(customer, 'one-item', 'pump-sheet') == submitted
        assert fresh.jobs(customer)['jobs'] == [submitted]


def allow_synthetic(intake, customer, revision=0):
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    return intake.set_policy(customer, uuid.uuid4().hex, revision, dict(
        active=True, route=CONTROLLED_ROUTE, evidence_ref='synthetic-route-approval'))


def test_active_worker_result_is_the_original_receivers_byte_receipt(onboarding):
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, _ = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake, customer)
        item = intake.admit(customer, 'original', 'pump-sheet')
    with LocalReceiver(onboarding.provisioner.directory, customer) as receiver:
        done = intake.worker(customer).run(item['job_id'])
        assert receiver.deliveries == (item['operation_id'],)
    assert done['state'] == 'completed'
    assert done['receipt']['bytes'] > 1000
    assert len(done['receipt']['sha256']) == 64
    assert all(done[k] == item[k] for k in ('job_id','operation_id','reservation_id'))
    with onboarding.customer_request('alice-login', revisions):
        assert intake.job(customer, item['job_id']) == done


def test_original_admission_and_policy_receipts_are_read_only_after_policy_change(onboarding):
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    intake, customer, revisions, _ = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        assert intake.policy_status(customer,'p1') == {'state':'absent'}
        assert intake.admission_status(customer,'item') == {'state':'absent'}
        policy = dict(active=True, route=CONTROLLED_ROUTE, evidence_ref='local-approval')
        applied = intake.set_policy(customer,'p1',0,policy)
        item = intake.admit(customer,'item','valve-sheet')
        intake.set_policy(customer,'p2',1,dict(policy,active=False))
        assert intake.policy_status(customer,'p1') == applied
        assert intake.admission_status(customer,'item') == item
        assert intake.admit(customer,'item','valve-sheet') == item
        assert intake.policy(customer)['revision'] == 2


import threading
from concurrent.futures import ThreadPoolExecutor
import pytest


@pytest.mark.parametrize('pause', ['queued','claimed','prechecked','intent'])
def test_suspension_first_reaches_no_receiver_even_after_reactivation(onboarding, pause):
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake,customer)
        submitted = intake.admit(customer,'job','pump-sheet')
    paused, resume = threading.Event(), threading.Event()
    def checkpoint(phase):
        if phase == pause:
            paused.set()
            assert resume.wait(4)
    worker = intake.worker(customer)
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver, ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.run,submitted['job_id'],checkpoint=checkpoint)
        try:
            assert paused.wait(4)
            assert onboarding.change_access('operator-login',operation,'suspend','suspended',0)['state'] == 'confirmed'
        finally:
            resume.set()
        held = future.result(5)
        assert held['state'] == 'held' and held['reason'] == 'customer_access_changed'
        assert onboarding.change_access('operator-login',operation,'restore','active',1)['state'] == 'confirmed'
        assert worker.run(submitted['job_id']) == held
    assert receiver.deliveries == ()
    assert held['operation_id'] == submitted['operation_id']
    assert held['reservation_bytes'] == submitted['reservation_bytes']


def test_received_datagram_with_lost_response_reconciles_during_suspension(onboarding, monkeypatch):
    from nilaris.local_transport import LocalReceiver, LocalTransport
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake,customer)
        submitted = intake.admit(customer,'job','valve-sheet')
    received, lost = threading.Event(), threading.Event()
    original_start = LocalTransport.start
    def delayed_reply(transport, key, payload):
        response = original_start(transport, key, payload)
        class LostResponse:
            def wait(self):
                response.wait()
                received.set()
                assert lost.wait(5)
                raise TimeoutError
        return LostResponse()
    monkeypatch.setattr(LocalTransport,'start',delayed_reply)
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver, ThreadPoolExecutor(1) as pool:
        task = pool.submit(intake.worker(customer).run,submitted['job_id'])
        try:
            assert received.wait(4)
            assert onboarding.change_access('operator-login',operation,'suspend','suspended',0)['state'] == 'confirmed'
        finally:
            lost.set()
        unknown = task.result(5)
        assert unknown['state'] == 'uncertain' and unknown['receipt'] is None
        fresh = ScopedIntake(onboarding,intake.bindings).worker(customer)
        assert fresh.run(submitted['job_id']) == unknown
        result = fresh.reconcile(submitted['job_id'])
        assert result['state'] == 'completed' and result['receipt']['bytes'] > 0
        assert fresh.reconcile(submitted['job_id']) == result
    assert receiver.deliveries == (submitted['operation_id'],)
    for field in ('operation_id','job_id','reservation_id','reservation_bytes'):
        assert result[field] == unknown[field] == submitted[field]


def test_cross_customer_worker_source_and_secrets_do_not_follow_untrusted_selectors(onboarding):
    from nilaris.provisioning181 import ProvisioningRefused
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    first, a, _, _ = setup_source(onboarding)
    second, b, revisions, _ = setup_source(onboarding)
    intake = ScopedIntake(onboarding,{**first.bindings, **second.bindings})
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake,a)
        intact = intake.admit(a,'shared-key','pump-sheet')
        held = intake.admit(b,'shared-key','pump-sheet')
        assert intact['job_id'] != held['job_id']
        assert held['state'] == 'held'
        with pytest.raises(PermissionError):
            intake.job(b,intact['job_id'])
        with pytest.raises(ValueError):
            intake.admit(a,'traversal','../' + b + '.runtime.json')
        with pytest.raises(ValueError):
            intake.set_policy(b,'bogus',0,dict(active=True,route=CONTROLLED_ROUTE,
                                               evidence_ref='synthetic',secret_ref=a+'.runtime.json'))
    with pytest.raises(PermissionError):
        intake.worker(b).run(intact['job_id'])
    with pytest.raises(ValueError):
        SourceBinding('issuer','subject','../../' + a,'recipient')
    with onboarding.provisioner._control(allocator=False) as c:
        row = c.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',(b,)).fetchone()
    with pytest.raises(ProvisioningRefused):
        onboarding.provisioner.customer_runtime(dict(row,secret_ref=a+'.runtime.json'))


def test_policy_revocation_and_source_root_changes_do_not_reinterpret_original(onboarding):
    from dataclasses import replace
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    intake, customer, revisions, _ = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake,customer)
        original = intake.admit(customer,'job','pump-sheet')
        intake.set_policy(customer,'revoke',1,dict(active=False,route=CONTROLLED_ROUTE,evidence_ref='revoked'))
        changed = ScopedIntake(onboarding,{customer:replace(intake.bindings[customer],root='second-root')})
        with pytest.raises(PermissionError):
            changed.admission_status(customer,'job')
        with pytest.raises(PermissionError):
            changed.admit(customer,'job','pump-sheet')
    with pytest.raises(PermissionError):
        changed.worker(customer).run(original['job_id'])
    held = intake.worker(customer).run(original['job_id'])
    assert held['state'] == 'held' and held['reason'] == 'disclosure_policy_changed'
    assert held['policy_revision'] == 1 and held['operation_id'] == original['operation_id']


def test_ordinary_processing_cannot_claim_cancel_or_reconcile_scoped_jobs(onboarding):
    from nilaris.admission import Ingestion, AdmissionProfile
    intake,customer,revisions,_ = setup_source(onboarding)
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(intake,customer)
        item = intake.admit(customer,'local-only','pump-sheet')
    with onboarding.provisioner._control(allocator=False) as c:
        row = c.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',(customer,)).fetchone()
    generic = Ingestion(onboarding.provisioner.customer_runtime(row), AdmissionProfile.l1(),
                        monitor_path=onboarding.provisioner.config['service_monitor_path'])
    job_id = item['job_id']
    assert generic.status(job_id)['category'] == 'not_found'
    assert not generic.claim(job_id,0).get('claimed')
    assert generic.cancel(job_id,0).get('category')
    assert generic.result(job_id)['category'] == 'not_found'
    assert generic.reconcile(job_id,0,0,'other','generic','quarantine').get('category')
    with onboarding.customer_request('alice-login',revisions):
        assert intake.job(customer,job_id) == item


def test_explicit_config_rotation_fences_already_queued_worker_and_new_stream(onboarding):
    from dataclasses import replace
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake, customer)
        item = intake.admit(customer,'first','pump-sheet')
    stale = intake.worker(customer)
    rotated = ScopedIntake(onboarding,{customer:replace(intake.bindings[customer],root='equipment-two')})
    assert rotated.bind_source('operator-login',operation,1)['revision'] == 2
    with onboarding.customer_request('alice-login', revisions):
        with pytest.raises(PermissionError):
            intake.admit(customer,'stale','pump-sheet')
        with pytest.raises(PermissionError):
            rotated.admission_status(customer,'first')
        new = rotated.admit(customer,'second','pump-sheet')
        assert new['job_id'] != item['job_id']
    assert stale.run(item['job_id'])['reason'] == 'source_authority_changed'


def test_host_gate_orders_handoff_even_if_worker_loses_registry_connection(onboarding, monkeypatch):
    """Terminate only the live worker connection to this disposable owned control database."""
    import psycopg
    from nilaris.local_transport import LocalReceiver, LocalTransport
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake, customer)
        item = intake.admit(customer,'drop-control','pump-sheet')
    at_socket, resume = threading.Event(), threading.Event()
    start = LocalTransport.start
    def start_after_disconnect(transport, operation_id, payload):
        at_socket.set()
        assert resume.wait(6)
        return start(transport, operation_id, payload)
    monkeypatch.setattr(LocalTransport,'start',start_after_disconnect)
    def open_control():
        # An application_name makes only this test worker's sessions eligible.
        return psycopg.connect(**inspector_options, autocommit=True)
    inspector_options = dict(onboarding.provisioner.admin)
    marker = 'scoped-worker-' + uuid.uuid4().hex
    onboarding.provisioner.admin['application_name'] = marker
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver, ThreadPoolExecutor(2) as pool:
        dispatch = pool.submit(intake.worker(customer).run,item['job_id'])
        try:
            assert at_socket.wait(5)
            with open_control() as inspector:
                targets = inspector.execute("""SELECT pid FROM pg_stat_activity WHERE datname=current_database()
                    AND application_name=%s AND state='idle in transaction'""",(marker,)).fetchall()
                assert len(targets) == 1
                assert inspector.execute("SELECT pg_terminate_backend(%s,1000)",targets[0]).fetchone()[0]
            # The public command must fail closed while the real sender is paused.
            # Losing its DB connection cannot free the live host dispatch gate.
            with pytest.raises(TimeoutError):
                onboarding.change_access('operator-login',operation,'s','suspended',0)
            pending = onboarding.access_command_status('operator-login',operation,'s','suspended',0)
            assert pending['state'] == 'in_progress'
        finally:
            resume.set()
        try:
            dispatch.result(4)
        except psycopg.Error:
            pass  # The sender lost its registry transaction; the receiver owns handoff evidence.
        assert onboarding.change_access('operator-login',operation,'s','suspended',0)['state'] == 'confirmed'
    assert receiver.deliveries == (item['operation_id'],)
    assert intake.worker(customer).reconcile(item['job_id'])['state'] == 'completed'


@pytest.mark.parametrize('phase', ['acquisition_claim','processing_claim','claimed','claimed_expired','intent','datagram_accepted','handed_off'])
def test_worker_process_death_preserves_original_and_never_replays_intent(onboarding, phase, tmp_path):
    import subprocess
    import time
    from dataclasses import replace
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake,customer)
        item = intake.admit(customer,'crash-original','valve-sheet')
    args = [onboarding.identities.verify('alice-login').issuer,str(tmp_path),customer,item['job_id'],
            'claimed' if phase == 'claimed_expired' else phase]
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver:
        finished = subprocess.run(['/opt/miniconda3/bin/conda','run','--no-capture-output','-n','nilaris',
            'python','runtime/a1/with_overlay.py',__file__,'crash-worker',*args],
            capture_output=True,timeout=25)
        assert finished.returncode == 73
        if phase == 'claimed_expired':
            time.sleep(10.1)  # Existing processing claim lasts ten seconds.
        if phase in ('intent','datagram_accepted','handed_off'):
            rotated = ScopedIntake(onboarding,{customer:replace(intake.bindings[customer],root='replacement')})
            rotated.bind_source('operator-login',operation,1)
            with pytest.raises(PermissionError):
                rotated.worker(customer).reconcile(item['job_id'])
            with onboarding.customer_request('alice-login',revisions):
                second = rotated.admit(customer,'new-source','pump-sheet')
            assert rotated.worker(customer).run(second['job_id'])['state'] == 'queued'
            original = intake.worker(customer)
            uncertain = original.run(item['job_id'])
            assert uncertain['state'] == 'uncertain'
            assert uncertain['reason'] == ('handoff_response_pending' if phase == 'handed_off' else 'intent_recorded')
            result = original.reconcile(item['job_id'])
            assert result['state'] == ('uncertain' if phase == 'intent' else 'completed')
            expected_successor = 'queued' if phase == 'intent' else 'completed'
            assert rotated.worker(customer).run(second['job_id'])['state'] == expected_successor
        else:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: intake.worker(customer).run(item['job_id']), range(2)))
            assert any(result['state'] == 'completed' for result in results)
            with onboarding.customer_request('alice-login',revisions):
                result = intake.job(customer,item['job_id'])
                second = intake.admit(customer,'other','pump-sheet')
            assert intake.worker(customer).run(second['job_id'])['state'] == 'completed'
    expected = () if phase == 'intent' else (item['operation_id'],)
    if phase != 'intent':
        expected += (second['operation_id'],)
    assert receiver.deliveries == expected
    assert all(result[k] == item[k] for k in ('job_id','operation_id','reservation_id','reservation_bytes'))


@pytest.mark.parametrize('active,region,reason', [
    (False,'local','disclosure_policy_inactive'), (True,'unpermitted','disclosure_route_incompatible')])
def test_policy_restrictions_hold_before_any_receiver_contact(onboarding, active, region, reason):
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    from nilaris.local_transport import LocalReceiver
    intake,customer,revisions,_ = setup_source(onboarding)
    with onboarding.customer_request('alice-login',revisions):
        intake.set_policy(customer,'only-route',0,dict(active=active,route=dict(CONTROLLED_ROUTE,region=region),
                                                       evidence_ref='synthetic-approval'))
        held = intake.admit(customer,'pinned','pump-sheet')
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver:
        assert held['state'] == 'held' and held['reason'] == reason
        assert intake.worker(customer).run(held['job_id']) == held
    assert receiver.deliveries == ()


if __name__ == '__main__':
    import os
    import sys
    from pathlib import Path
    from test_onboarding182_pg import controlled_service
    if sys.argv[1] == 'crash-receiver':
        from nilaris.local_transport import LocalReceiver
        with LocalReceiver(sys.argv[2], sys.argv[3]):
            os._exit(74)
    if sys.argv[1] == 'crash-first-admit':
        _, _, issuer, directory, customer = sys.argv
        service = controlled_service(os.environ['NILARIS_ONBOARDING182_TEST_CONFIG'], issuer, Path(directory))
        source = SourceBinding(issuer,'alice','equipment','alice-designated')
        _, revisions = service.authenticate_session('alice-login')
        with service.customer_request('alice-login',revisions):
            result = ScopedIntake(service,{customer:source}).admit(customer,'first-command','pump-sheet')
            assert result['state'] == 'queued'
            os._exit(75)  # Tenant committed; outer registry transaction dies here.
    _, _, issuer, directory, customer, job_id, phase = sys.argv
    service = controlled_service(os.environ['NILARIS_ONBOARDING182_TEST_CONFIG'], issuer, Path(directory))
    source = SourceBinding(issuer,'alice','equipment','alice-designated')
    def crash(at):
        if at == phase:
            os._exit(73)
    ScopedIntake(service,{customer:source}).worker(customer).run(job_id,checkpoint=crash)


def test_policy_and_ordinary_admission_compete_for_same_byte_budget(onboarding):
    from nilaris.admission import AdmissionProfile, Ingestion
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    from test_admission import member
    intake, customer, revisions, _ = setup_source(onboarding)
    profile = AdmissionProfile.l1().lowered_for_test(
        'bounded-policy-test', logical_bytes=2*8388608 + 135168 + 16000)
    limited = ScopedIntake(onboarding, intake.bindings, profile=profile)
    with onboarding.provisioner._control(allocator=False) as c:
        row = c.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',(customer,)).fetchone()
    generic = Ingestion(onboarding.provisioner.customer_runtime(row),profile,
                        monitor_path=onboarding.provisioner.config['service_monitor_path'])
    policy = dict(active=True,route=CONTROLLED_ROUTE,evidence_ref='controlled')
    def write(key, expected):
        with onboarding.customer_request('alice-login',revisions):
            return limited.set_policy(customer,key,expected,policy)
    first = write('one',0)
    start = threading.Barrier(2)
    def change():
        start.wait(3)
        try:
            return write('two',1)
        except ValueError as error:
            assert 'capacity' in str(error)
            return None
    def offer():
        start.wait(3)
        return generic.offer('ordinary','item',[member()])
    with ThreadPoolExecutor(2) as pool:
        update = pool.submit(change)
        admission = pool.submit(offer).result(5)['members'][0]
        applied = update.result(5)
    assert bool(applied) != (admission['disposition'] == 'accepted')
    assert write('one',0) == first
    if not applied:
        usage = generic.status(admission['job_id'])['admission']
        assert usage['policy_retained_bytes'] > 8192
        assert usage['receipt_reservation_bytes'] == usage['policy_retained_bytes'] + 135168
        with onboarding.customer_request('alice-login',revisions):
            assert limited.policy_status(customer,'two') == {'state':'absent'}
        with pytest.raises(ValueError,match='capacity'):
            write('three',1)
    else:
        assert admission['reason'] == 'byte_capacity'


def test_existing_disclosure_rows_are_backfilled_once_with_originals_intact(onboarding):
    import psycopg
    from psycopg.types.json import Jsonb
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    from test_admission import member
    intake, customer, revisions, operation = setup_source(onboarding)
    # Set up only the legacy data shape in this newly provisioned empty tenant.
    # SET ROLE selects the assigned migration/runtime roles without reading a role secret.
    inventory = onboarding.status('operator-login',operation)
    owned = dict(onboarding.provisioner.admin,dbname=inventory['database_name'])
    migration = dict(owned,options='-c role=' + inventory['migration_role'])
    runtime = dict(owned,options='-c role=' + inventory['runtime_role'])
    actor = onboarding.identities.verify('alice-login')
    policy = dict(active=True,route=CONTROLLED_ROUTE,evidence_ref='legacy-evidence')
    with psycopg.connect(**migration) as c:
        c.execute('ALTER TABLE intake_policies DROP COLUMN reservation_bytes')
        c.execute('''INSERT INTO intake_policies (revision,issuer,subject,command_id,policy)
            VALUES (1,%s,%s,'legacy',%s)''',(actor.issuer,actor.subject,Jsonb(policy)))
    profile = AdmissionProfile.l1().lowered_for_test(
        'legacy-policy-test', logical_bytes=2*8388608 + 135168 + 16000)
    limited = ScopedIntake(onboarding,intake.bindings,profile=profile)
    generic = Ingestion(runtime,profile,monitor_path=onboarding.provisioner.config['service_monitor_path'])
    job = generic.offer('ordinary','original-admission',[member()])['members'][0]['job_id']
    before = generic.status(job)['admission']
    assert before['policy_retained_bytes'] > 8192
    for _ in range(2):
        migrate(migration)
        after = generic.status(job)['admission']
        for key in ('policy_retained_bytes','job_reservation_bytes','receipt_reservation_bytes'):
            assert after[key] == before[key]
        with onboarding.customer_request('alice-login',revisions):
            original = {'revision':1,'policy':policy}
            assert limited.policy(customer) == original
            assert limited.policy_status(customer,'legacy') == original
            assert limited.set_policy(customer,'legacy',0,policy) == original
            with pytest.raises(ValueError,match='capacity'):
                limited.set_policy(customer,'another',1,policy)
            assert limited.policy_status(customer,'another') == {'state':'absent'}


def test_duplicate_source_needs_a_durable_receipt_for_each_acknowledged_key(onboarding):
    from nilaris.admission import AdmissionProfile
    from nilaris.scoped_intake import CONTROLLED_ROUTE
    intake, customer, revisions, _ = setup_source(onboarding)
    # One job, two admission receipts, and room for two policy versions.
    profile = AdmissionProfile.l1().lowered_for_test(
        'bounded-alias', logical_bytes=2*8388608 + 2*135168 + 50000)
    limited = ScopedIntake(onboarding,intake.bindings,profile=profile)
    policy = dict(active=True,route=CONTROLLED_ROUTE,evidence_ref='approved')
    with onboarding.customer_request('alice-login',revisions):
        limited.set_policy(customer,'first-policy',0,policy)
        original = limited.admit(customer,'original','pump-sheet')
        assert limited.admit(customer,'acknowledged-alias','pump-sheet') == original
        refused = limited.admit(customer,'unacknowledged','pump-sheet')
        assert refused['state'] == 'refused' and 'receipt' in refused['reason']
    fresh = ScopedIntake(onboarding,intake.bindings,profile=profile)
    with onboarding.customer_request('alice-login',revisions):
        fresh.set_policy(customer,'second-policy',1,dict(policy,active=False))
        for key in ('original','acknowledged-alias'):
            assert fresh.admission_status(customer,key) == original
            assert fresh.admit(customer,key,'pump-sheet') == original
        assert fresh.admission_status(customer,'unacknowledged') == {'state':'absent'}
        assert fresh.admit(customer,'unacknowledged','pump-sheet') == refused
        assert fresh.admission_status(customer,'unacknowledged') == {'state':'absent'}
        assert fresh.jobs(customer)['jobs'] == [original]


def test_running_hold_preserves_full_waiting_queue_and_transfers_only_to_eligible_successor(onboarding):
    from nilaris.admission import AdmissionProfile, Ingestion
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, operation = setup_source(onboarding)
    profile = AdmissionProfile.l1().lowered_for_test('one-waiting',waiting_jobs=1)
    limited = ScopedIntake(onboarding,intake.bindings,profile=profile)
    metadata = onboarding.status('operator-login',operation)
    runtime = dict(onboarding.provisioner.admin,dbname=metadata['database_name'],options='-c role='+metadata['runtime_role'])
    core = Ingestion(runtime,scoped_binding=intake.bindings[customer].digest(customer))
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(limited,customer)
        first = limited.admit(customer,'running','pump-sheet')
    claimed, resume = threading.Event(), threading.Event()
    def pause(phase):
        if phase == 'claimed':
            claimed.set()
            assert resume.wait(5)
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver, ThreadPoolExecutor(1) as pool:
        running = pool.submit(limited.worker(customer).run,first['job_id'],checkpoint=pause)
        try:
            assert claimed.wait(4)
            with onboarding.customer_request('alice-login',revisions):
                allow_synthetic(limited,customer,1)
                second = limited.admit(customer,'waiting','valve-sheet')
        finally:
            resume.set()
        held = running.result(5)
        assert held['state'] == 'held' and held['reason'] == 'disclosure_policy_changed'
        reserved = core.status(first['job_id'])
        assert reserved['active_capacity_reserved']
        assert reserved['admission']['waiting_jobs'] == 1
        assert reserved['admission']['active_capacity_reservations'] == 1
        assert receiver.deliveries == ()
        assert limited.worker(customer).run(second['job_id'])['state'] == 'completed'
        after = core.status(first['job_id'])
        assert not after['active_capacity_reserved']
        assert after['admission']['waiting_jobs'] == 1
        assert after['admission']['active_capacity_reservations'] == 0
        assert after['recovery'][-1]['reason'] == 'verified_capacity_transfer'
        with onboarding.customer_request('alice-login',revisions):
            assert limited.job(customer,first['job_id']) == held
    assert receiver.deliveries == (second['operation_id'],)
    assert all(held[k] == first[k] for k in ('job_id','operation_id','reservation_id','reservation_bytes'))


@pytest.mark.parametrize('category,limits', [
    ('waiting_capacity', {'waiting_jobs':1}),
    ('live_capacity', {'live_jobs':1}),
    ('byte_capacity', {'logical_bytes':2*8388608 + 3*135168 + 50000})])
def test_known_deferral_is_bound_to_original_command_after_capacity_changes(onboarding, category, limits):
    from nilaris.admission import AdmissionProfile
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, _ = setup_source(onboarding)
    limited = ScopedIntake(onboarding,intake.bindings,profile=AdmissionProfile.l1().lowered_for_test(category,**limits))
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(limited,customer)
        first = limited.admit(customer,'first','pump-sheet')
        deferred = limited.admit(customer,'original-deferral','valve-sheet')
        assert deferred['state'] == 'deferred' and deferred['category'] == category
        assert deferred['command_id'] == 'original-deferral' and deferred['receipt_stored'] is True
        assert 'job_id' not in deferred
    with LocalReceiver(onboarding.provisioner.directory,customer):
        assert limited.worker(customer).run(first['job_id'])['state'] == 'completed'
    fresh = ScopedIntake(onboarding,intake.bindings)
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(fresh,customer,1)
        assert fresh.admission_status(customer,'original-deferral') == deferred
        assert fresh.admit(customer,'original-deferral','valve-sheet') == deferred
        with pytest.raises(ValueError,match='original'):
            fresh.admit(customer,'original-deferral','pump-sheet')
        assert len(fresh.jobs(customer)['jobs']) == 1
        assert fresh.admit(customer,'new-command','valve-sheet')['state'] == 'queued'


def test_rotated_source_can_take_only_a_proven_stopped_same_customer_slot(onboarding):
    from dataclasses import replace
    from nilaris.local_transport import LocalReceiver
    original, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(original,customer)
        first = original.admit(customer,'before-rotation','pump-sheet')
    replacement = ScopedIntake(onboarding,{customer:replace(original.bindings[customer],root='new-root')})
    claimed, resume = threading.Event(), threading.Event()
    def pause(phase):
        if phase == 'claimed':
            claimed.set()
            assert resume.wait(5)
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver, ThreadPoolExecutor(1) as pool:
        running = pool.submit(original.worker(customer).run,first['job_id'],checkpoint=pause)
        try:
            assert claimed.wait(4)
            replacement.bind_source('operator-login',operation,1)
            with onboarding.customer_request('alice-login',revisions):
                second = replacement.admit(customer,'after-rotation','valve-sheet')
                with pytest.raises(PermissionError):
                    replacement.job(customer,first['job_id'])
            assert replacement.worker(customer).run(second['job_id'])['state'] == 'queued'
            assert not receiver.deliveries
        finally:
            resume.set()
        held = running.result(5)
        assert held['state'] == 'held' and held['reason'] == 'source_authority_changed'
        with pytest.raises(PermissionError):
            replacement.worker(customer).reconcile(first['job_id'])
        assert replacement.worker(customer).run(second['job_id'])['state'] == 'completed'
        assert original.worker(customer).reconcile(first['job_id']) == held
    assert receiver.deliveries == (second['operation_id'],)
    assert all(held[k] == first[k] for k in ('job_id','operation_id','reservation_id','reservation_bytes'))


@pytest.mark.parametrize('endpoint', ['missing_directory', 'missing_socket', 'crashed_receiver'])
def test_missing_receiver_holds_before_handoff_and_allows_successor(onboarding, endpoint):
    from nilaris.local_transport import LocalReceiver, LocalTransport
    intake, customer, revisions, _ = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake, customer)
        first = intake.admit(customer, 'before-receiver', 'valve-sheet')
        next_item = intake.admit(customer, 'after-receiver', 'pump-sheet')
    transport = LocalTransport(onboarding.provisioner.directory, customer)
    if endpoint == 'crashed_receiver':
        import subprocess
        process = subprocess.run(['/opt/miniconda3/bin/conda','run','--no-capture-output','-n','nilaris',
            'python','runtime/a1/with_overlay.py',__file__,'crash-receiver',
            str(onboarding.provisioner.directory),customer],capture_output=True,timeout=15)
        assert process.returncode == 74
        assert transport.endpoint.is_socket()
    elif endpoint == 'missing_socket':
        transport.endpoint.parent.mkdir(mode=0o700)
    try:
        held = intake.worker(customer).run(first['job_id'])
    finally:
        if endpoint == 'missing_socket':
            transport.endpoint.parent.rmdir()
    assert held['state'] == 'held' and held['reason'] == 'local_receiver_unavailable_before_handoff'
    assert held['receipt'] is None
    assert all(held[k] == first[k] for k in ('job_id','operation_id','reservation_id','reservation_bytes'))
    worker = ScopedIntake(onboarding,intake.bindings).worker(customer)
    with LocalReceiver(onboarding.provisioner.directory, customer) as receiver:
        assert worker.reconcile(first['job_id']) == held
        assert worker.run(first['job_id']) == held
        assert worker.run(next_item['job_id'])['state'] == 'completed'
    assert receiver.deliveries == (next_item['operation_id'],)


def test_full_datagram_queue_holds_original_and_allows_successor(onboarding):
    from contextlib import ExitStack
    import errno
    import socket
    from nilaris.local_transport import LocalReceiver, LocalTransport
    intake, customer, revisions, _ = setup_source(onboarding)
    with onboarding.customer_request('alice-login', revisions):
        allow_synthetic(intake, customer)
        first = intake.admit(customer, 'backpressure', 'pump-sheet')
        second = intake.admit(customer, 'after-backpressure', 'valve-sheet')
    endpoint = LocalTransport(onboarding.provisioner.directory, customer).endpoint
    with ExitStack() as cleanup:
        endpoint.parent.mkdir(mode=0o700)
        cleanup.callback(endpoint.parent.rmdir)
        sink = cleanup.enter_context(socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM))
        sink.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096)
        sink.bind(str(endpoint))
        cleanup.callback(endpoint.unlink)
        filler = cleanup.enter_context(socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM))
        filler.setblocking(False)
        for accepted in range(4096):
            try:
                filler.sendto(b'x',str(endpoint))
            except OSError as error:
                assert error.errno in (errno.EAGAIN,errno.EWOULDBLOCK,errno.ENOBUFS)
                break
        else:
            pytest.fail('controlled datagram queue did not fill')
        held = intake.worker(customer).run(first['job_id'])
        sink.setblocking(False)
        for _ in range(accepted):
            assert sink.recv(8192) == b'x'
        with pytest.raises(BlockingIOError):
            sink.recv(8192)
    assert held['state'] == 'held' and held['receipt'] is None
    assert all(held[k] == first[k] for k in ('job_id','operation_id','reservation_id','reservation_bytes'))
    fresh = ScopedIntake(onboarding,intake.bindings).worker(customer)
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver:
        assert fresh.run(first['job_id']) == fresh.reconcile(first['job_id']) == held
        assert fresh.run(second['job_id'])['state'] == 'completed'
    assert receiver.deliveries == (second['operation_id'],)

def test_first_admission_survives_outer_request_death_with_its_verified_source(onboarding, tmp_path):
    from dataclasses import replace
    import subprocess
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(intake,customer)
    alternate = ScopedIntake(onboarding,{customer:replace(intake.bindings[customer],root='other-equipment')})
    issuer = intake.bindings[customer].issuer
    proc = subprocess.run(['/opt/miniconda3/bin/conda','run','--no-capture-output','-n','nilaris',
        'python','runtime/a1/with_overlay.py',__file__,'crash-first-admit',issuer,str(tmp_path),customer],
        capture_output=True,timeout=25)
    assert proc.returncode == 75
    with onboarding.customer_request('alice-login',revisions):
        first = intake.admission_status(customer,'first-command')
        assert first['state'] == 'queued'
        assert not alternate.capabilities(customer)['intake']
        with pytest.raises(PermissionError):
            alternate.admit(customer,'other-command','pump-sheet')
    with pytest.raises(ValueError, match='stale'):
        alternate.bind_source('operator-login',operation,0)
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver:
        completed = intake.worker(customer).run(first['job_id'])
        assert completed['state'] == 'completed'
    assert receiver.deliveries == (first['operation_id'],)
    assert all(completed[k] == first[k] for k in ('job_id','operation_id','reservation_id','reservation_bytes'))
    assert alternate.bind_source('operator-login',operation,1) == {'revision':2}
    with onboarding.customer_request('alice-login',revisions):
        assert alternate.capabilities(customer)['intake'] and not intake.capabilities(customer)['intake']


def test_reads_and_unreserved_first_attempt_leave_initial_source_unbound(onboarding):
    from dataclasses import replace
    from nilaris.admission import AdmissionProfile
    intake, customer, revisions, operation = setup_source(onboarding)
    alternate = ScopedIntake(onboarding,{customer:replace(intake.bindings[customer],root='another-source')})
    scarce = ScopedIntake(onboarding,intake.bindings,profile=AdmissionProfile.l1().lowered_for_test(
        'no-original-receipt',logical_bytes=8388608+1024))
    with onboarding.customer_request('alice-login',revisions):
        assert scarce.admission_status(customer,'first') == {'state':'absent'}
        assert scarce.admit(customer,'first','pump-sheet')['receipt_stored'] is False
        assert scarce.admission_status(customer,'first') == {'state':'absent'}
        assert alternate.capabilities(customer)['intake'] and not scarce.jobs(customer)['jobs']
    assert alternate.bind_source('operator-login',operation,0) == {'revision':1}


def test_older_unmarked_admission_requires_explicit_source_binding(onboarding):
    import psycopg
    from nilaris.admission import migrate
    from nilaris.local_transport import LocalReceiver
    intake, customer, revisions, operation = setup_source(onboarding)
    with onboarding.customer_request('alice-login',revisions):
        allow_synthetic(intake,customer)
        first = intake.admit(customer,'legacy','pump-sheet')
    # Restore just the historical schema shape in this disposable tenant.
    inventory = onboarding.status('operator-login',operation)
    migration = dict(onboarding.provisioner.admin,dbname=inventory['database_name'],
                     options='-c role=' + inventory['migration_role'])
    with psycopg.connect(**migration) as c:
        c.execute('ALTER TABLE admission_receipts DROP COLUMN initial_source_pin')
    migrate(migration)
    with onboarding.customer_request('alice-login',revisions):
        assert not intake.capabilities(customer)['intake']
        with pytest.raises(PermissionError):
            intake.admit(customer,'replacement','valve-sheet')
    with LocalReceiver(onboarding.provisioner.directory,customer) as receiver:
        assert intake.worker(customer).run(first['job_id']) == first
        assert not receiver.deliveries
        assert intake.bind_source('operator-login',operation,0) == {'revision':1}
        assert intake.worker(customer).run(first['job_id'])['state'] == 'completed'
    assert receiver.deliveries == (first['operation_id'],)


def test_first_admission_races_operator_at_original_source_revision(onboarding):
    from dataclasses import replace
    intake, customer, revisions, operation = setup_source(onboarding)
    alternate = ScopedIntake(onboarding,{customer:replace(intake.bindings[customer],root='rotated-source')})
    start = threading.Barrier(2)
    def submit():
        start.wait(3)
        with onboarding.customer_request('alice-login',revisions):
            try:
                return intake.admit(customer,'racing-first','pump-sheet')
            except PermissionError:
                return None
    def rotate():
        start.wait(3)
        try:
            return alternate.bind_source('operator-login',operation,0)
        except ValueError as error:
            assert 'stale' in str(error)
            return None
    with ThreadPoolExecutor(2) as executor:
        admission = executor.submit(submit)
        rotation = executor.submit(rotate).result(5)
        original = admission.result(5)
    assert bool(original) != bool(rotation)
    with onboarding.customer_request('alice-login',revisions):
        if original:
            assert intake.admission_status(customer,'racing-first') == original
            assert not alternate.capabilities(customer)['intake']
        else:
            assert rotation == {'revision':1}
            assert not intake.capabilities(customer)['intake']
            assert alternate.jobs(customer) == {'jobs':[]}
