"""Authenticated expert commands against a fresh, operator-provisioned customer."""
from contextlib import ExitStack
from dataclasses import replace
import json
import time

import psycopg
import pytest

from nilaris import customer_review
from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.execution import ControlledSource
from nilaris.results import Effect
from test_admission import member
from test_onboarding182_pg import customer_and_invitation, http_client, onboarding, ordered_transactions
from test_review import proposal


def publish_fixture(service, customer, *, revision='r1', value='12', with_unlinked=False):
    # Provisioning already migrated this disposable DB. Use its limited runtime role.
    provisioner = service.provisioner
    with service._registry() as connection:
        row = connection.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',
                                 (customer,)).fetchone()
    options = provisioner.customer_runtime(row)
    ingest = Ingestion(options, AdmissionProfile.l1(), monitor_path=provisioner.config['service_monitor_path'])
    offered = ingest.offer('synthetic', 'offer-' + revision, [dict(member(), source_revision=revision)])
    assert offered.get('members'), offered
    job = offered['members'][0]['job_id']
    ingest.capture(job, ingest.claim(job, 0), ControlledSource({'P': (revision, b'Synthetic pump ' + value.encode())}))
    output = proposal(value)
    if with_unlinked:
        note = dict(output.effects[0].payload, id='unlinked-office-note',
                    original_content='Internal test-only freight note', value='unrelated')
        output = replace(output, effects=(*output.effects, Effect('observation', 3, note)))
    ingest.publish(job, ingest.claim(job, ingest.status(job)['revision']), output)
    return job, ingest.result(job)['publications'][0]['id']


def test_reviewer_saves_generated_attribute_and_reloads_original_evidence(onboarding, http_client):
    customer, _, accept = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*accept)
    job, proposal_id = publish_fixture(onboarding, customer)
    connect, _ = http_client
    with connect('bob-login') as post:
        scope = dict(customer_id=customer, job_id=job)

        def command(operation, **fields):
            code, result = post('/expert/command', dict(scope, command=dict(operation=operation, **fields)))
            assert code == 200, result
            return result

        context = post('/expert/context', scope)
        assert context[0] == 200 and context[1]['roles'] == ['Reviewer']
        created = command('review_create', command_id='create', job_id=job, proposal_id=proposal_id,
                          product_type='pump', expected_base=None)
        assert created['saved']
        assert post('/expert/acknowledge', selected(post, scope, 'create'))[0] == 200
        draft = created['draft_id']
        original = command('review_read', draft_id=draft)
        definitions = original['definitions']
        definitions[0]['label'] = 'Rated flow rate'
        saved = command('review_save', command_id='edit', draft_id=draft, expected_revision=0,
                        definitions=definitions, associations=original['associations'], reason='Rated label')
        assert saved['saved'] and saved['revision'] == 1
        fresh = command('review_read', draft_id=draft)
        assert fresh['definitions'][0]['label'] == 'Rated flow rate'
        assert fresh['initial']['candidates'][0]['label'] == 'Flow rate'
        assert fresh['initial']['observations'][0]['original_content'] == 'Flow rate: 12 m3/h'
        assert fresh['actor'] == context[1]['actor']


def test_conflict_receipts_stay_private_to_the_reviewing_identity(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer', 'Administrator'])
    onboarding.accept(*redeem)
    users = onboarding.members('alice-login', customer)
    alice = next(row for row in users['users'] if row['subject'] == 'alice')
    onboarding.manage('bob-login', customer, 'grant-alice', users['revision'],
                      dict(kind='roles', target=alice['membership_id'], roles=['Administrator', 'Approver']))
    job, proposal_id = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login', second_credential='alice-login') as (reviewer, approver):
        def send(post, operation, **arguments):
            code, value = post('/expert/command', dict(scope, command=dict(operation=operation, **arguments)))
            assert code == 200, value
            return value
        created = send(reviewer, 'review_create', command_id='create', job_id=job,
                       proposal_id=proposal_id, product_type='pump', expected_base=None)
        assert reviewer('/expert/acknowledge', selected(reviewer, scope, 'create'))[0] == 200
        draft = created['draft_id']
        saved = send(reviewer, 'review_read', draft_id=draft)
        stale = send(reviewer, 'review_save', command_id='bob-private-stale', draft_id=draft,
                     expected_revision=9, definitions=saved['definitions'], associations=saved['associations'],
                     reason='Concurrent edit')
        assert stale['category'] == 'revision_conflict'
        own = send(reviewer, 'review_resume', draft_id=draft)
        assert [row['command_id'] for row in own['pending_conflicts']] == ['bob-private-stale']
        assert send(approver, 'review_resume', draft_id=draft)['pending_conflicts'] == []


def review_command(post, scope, operation, **arguments):
    return post('/expert/command', dict(scope, command=dict(operation=operation, **arguments)))


def selected(post, scope, command_id):
    code, context = post('/expert/context', scope)
    assert code == 200 and context['pending']['command_id'] == command_id
    return dict(scope, command_id=command_id, tag=context['pending_tag'])


def create_draft(post, scope, proposal):
    code, result = review_command(post, scope, 'review_create', command_id='same-create',
        job_id=scope['job_id'], proposal_id=proposal, product_type='pump', expected_base=None)
    assert code == 200 and result['saved'], result
    assert post('/expert/acknowledge', selected(post, scope, 'same-create'))[0] == 200
    return result['draft_id']


def full_decision(post, scope, draft):
    code, view = review_command(post, scope, 'review_read', draft_id=draft)
    assert code == 200
    for attribute in view['definitions']:
        attribute['decision'] = 'accepted'
    view['definitions'][0]['label'] = 'Rated capacity'
    view['associations'][0]['disposition'] = 'rejected'
    return dict(operation='review_save', command_id='same-save', draft_id=draft,
                expected_revision=0, definitions=view['definitions'], associations=view['associations'],
                reason='Synthetic source review')


def test_two_customer_sessions_and_independent_explicit_approval(onboarding, http_client):
    a, _, redeem_a = customer_and_invitation(onboarding, ['Administrator', 'Reviewer'])
    b, _, redeem_b = customer_and_invitation(onboarding, ['Administrator', 'Reviewer', 'Approver'], bootstrap_command='bootstrap-b')
    onboarding.accept(*redeem_a)
    onboarding.accept(*redeem_b[:3], 'accept-b-identity')
    job_a, proposal_a = publish_fixture(onboarding, a)
    job_b, proposal_b = publish_fixture(onboarding, b)
    a_scope = dict(customer_id=a, job_id=job_a)
    b_scope = dict(customer_id=b, job_id=job_b)
    join = http_client[0]
    with join('bob-login', second_credential='alice-login') as (bob, alice):
        # Administrator alone cannot enumerate either customer's technical content.
        for scope in (a_scope, b_scope):
            assert alice('/expert/dashboard', {'customer_id': scope['customer_id']})[0] == 403
            assert alice('/expert/context', scope)[0] == 403
            assert review_command(alice, scope, 'result', job_id=scope['job_id'])[0] == 403
        draft_a = create_draft(bob, a_scope, proposal_a)
        draft_b = create_draft(bob, b_scope, proposal_b)
        assert draft_a != draft_b
        assert review_command(bob, b_scope, 'review_read', draft_id=draft_a)[0] == 403
        assert review_command(bob, b_scope, 'result', job_id=job_a, cursor='untrusted')[0] == 403
        assert bob('/expert/context', dict(customer_id=b, job_id=job_a))[0] == 403
        a_save = full_decision(bob, a_scope, draft_a)
        b_save = full_decision(bob, b_scope, draft_b)
        assert review_command(bob, a_scope, **dict(a_save, actor='unverified'))[0] == 400
        assert review_command(bob, a_scope, **dict(a_save, roles=['Approver']))[0] == 400
        assert review_command(bob, a_scope, **a_save)[1]['saved']
        assert bob('/expert/resolve', dict(b_scope, command_id='same-save'))[0] == 403
        assert review_command(bob, b_scope, **b_save)[1]['saved']
        for scope in (a_scope, b_scope):
            assert bob('/expert/acknowledge', selected(bob, scope, 'same-save'))[0] == 200
        approve = dict(operation='review_approve', command_id='approve', expected_revision=1,
                       product_type='pump', explicit=True)
        assert review_command(bob, a_scope, **approve, draft_id=draft_a)[0] == 403
        schema = review_command(bob, b_scope, **approve, draft_id=draft_b)[1]
        assert schema['approved']
        users = onboarding.members('bob-login', a)
        alice_id = next(u['membership_id'] for u in users['users'] if u['subject'] == 'alice')
        onboarding.manage('bob-login', a, 'grant-approver', users['revision'],
                          dict(kind='roles', target=alice_id, roles=['Administrator', 'Approver']))
        assert alice('/expert/context', a_scope)[0] == 403
        assert alice('/session', {'credential': 'alice-login'})[0] == 200
        assert review_command(alice, a_scope, **a_save)[0] == 403
        assert review_command(alice, a_scope, 'result', job_id=job_a, provenance=True, proposal_id=proposal_a)[0] == 403
        assert alice('/expert/resolve', dict(a_scope, command_id='same-save'))[0] == 403
        refused = review_command(alice, a_scope, **dict(approve, explicit=False), draft_id=draft_a)[1]
        assert not refused['saved']
        assert alice('/expert/acknowledge', selected(alice, a_scope, 'approve'))[0] == 200
        receipt = review_command(alice, a_scope, **approve, draft_id=draft_a)[1]
        assert receipt['approved'] and receipt['revision'] == 1
        first = review_command(alice, a_scope, 'review_read', draft_id=draft_a)[1]
        second = review_command(bob, a_scope, 'review_read', draft_id=draft_a)[1]
        assert first['approved_schema'] == second['approved_schema']
        assert first['approved_schema']['id'] == receipt['approved_version']
        assert first['approved_schema']['actor'] != first['actor']
        assert len(first['approved_schema']['definitions']) == 3
        assert len(first['approved_schema']['associations']) == 2
        assert first['initial']['associations'][0]['disposition'] == 'supported'


@pytest.mark.parametrize('change', ['remove_role', 'suspend'])
@pytest.mark.parametrize('revocation_first', [True, False])
def test_customer_boundary_orders_review_save(onboarding, http_client, monkeypatch, change, revocation_first):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    join, journal = http_client
    with join('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        save = full_decision(post, scope, draft)
    principal, revisions = onboarding.authenticate_session('bob-login')
    target = next(row['operation_id'] for row in onboarding.provisioner.inventory('operator-login')
                  if row['customer_id'] == customer)

    def revoke():
        if change == 'suspend':
            return onboarding.change_access('operator-login', target, 'suspend', 'suspended', 0)['state']
        return onboarding.manage('alice-login', customer, 'remove-reviewer', 2,
                                 dict(kind='roles', target=redeem[1], roles=['Approver']))['state']

    def edit():
        try:
            with onboarding.customer_request('bob-login', revisions), ExitStack() as locks:
                value, _ = customer_review.handle('/expert/command', dict(scope, command=save),
                                                  onboarding, principal, journal, locks)
                return 'saved' if value['saved'] else value['category']
        except PermissionError:
            return 'refused'

    before, after = (revoke, edit) if revocation_first else (edit, revoke)
    changed = 'confirmed' if change == 'suspend' else 'updated'
    assert ordered_transactions(onboarding, monkeypatch, before, after,
        first_commits=2 if revocation_first and change == 'suspend' else 1) == (
        (changed, 'refused') if revocation_first else ('saved', changed))
    if change == 'suspend':
        onboarding.change_access('operator-login', target, 'reactivate', 'active', 1)
    with pytest.raises(PermissionError), onboarding.customer_request('bob-login', revisions):
        with onboarding.review_boundary(customer, customer_review.READ_ROLES):
            pytest.fail('old session admitted')
    with join('bob-login') as fresh:
        code, view = review_command(fresh, scope, 'review_read', draft_id=draft)
        assert code == 200 and view['revision'] == (0 if revocation_first else 1)


def test_waiting_review_write_rechecks_cookie_after_customer_database_lock(onboarding, http_client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import nilaris.onboarding182_ui as ui

    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    join, _ = http_client
    tick = [time.monotonic()]
    monkeypatch.setattr(ui, 'time', SimpleNamespace(monotonic=lambda:tick[0], sleep=time.sleep))
    with join('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        save = full_decision(post, scope, draft)
        with onboarding._registry() as registry:
            row = registry.execute('SELECT * FROM provision181_operations WHERE customer_id=%s', (customer,)).fetchone()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with psycopg.connect(**onboarding.provisioner.customer_runtime(row)) as holder:
                holder.execute('SELECT pg_advisory_xact_lock(714012001)')
                waiting = pool.submit(review_command, post, scope, **save)
                with psycopg.connect(**onboarding.provisioner.admin, autocommit=True) as observer:
                    deadline = time.monotonic() + 3
                    while not observer.execute('SELECT EXISTS (SELECT FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid)))',
                                               (holder.info.backend_pid,)).fetchone()[0]:
                        assert time.monotonic() < deadline, 'review never reached the customer mutation boundary'
                        time.sleep(.01)
                tick[0] += 61
            assert waiting.result(timeout=4)[0] == 403
        assert post('/session', {'credential': 'bob-login'})[0] == 200
        pending = post('/expert/context', scope)[1]['pending']
        assert pending == dict(operation='review_save', command_id='same-save')
        assert post('/expert/resolve', dict(scope, command_id='same-save'))[1]['category'] == 'unknown_command'
        assert review_command(post, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        # Re-posting the original only reconciles it. Explicit retry applies the journal body.
        assert review_command(post, scope, **save)[1]['category'] == 'unknown_command'
        assert post('/expert/retry', selected(post, scope, 'same-save'))[1]['revision'] == 1
        fresh = review_command(post, scope, 'review_read', draft_id=draft)[1]
        assert fresh['definitions'][0]['label'] == 'Rated capacity' and fresh['reason'] == save['reason']


def test_customer_incoming_comparison_and_dashboard_pages_preserve_approved_version(onboarding, http_client, monkeypatch):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer', 'Approver'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        save = full_decision(post, scope, draft)
        assert review_command(post, scope, **save)[1]['saved']
        assert post('/expert/acknowledge', selected(post, scope, 'same-save'))[0] == 200
        receipt = review_command(post, scope, 'review_approve', command_id='approve', expected_revision=1,
                                 draft_id=draft, product_type='pump', explicit=True)[1]
        assert receipt['approved']
        first = review_command(post, scope, 'review_read', draft_id=draft)[1]
        new_job, new_proposal = publish_fixture(onboarding, customer, revision='r2', value='17')
        compared = review_command(post, scope, 'review_incoming', draft_id=draft, job_id=new_job, proposal_id=new_proposal)[1]
        assert compared['read_only'] and compared['incoming']['observations'][0]['value'] == '17'
        assert review_command(post, scope, 'review_read', draft_id=draft)[1] == first
        monkeypatch.setattr(customer_review, 'PAGE_SIZE', 1, raising=False)
        page = post('/expert/dashboard', dict(customer_id=customer))[1]
        assert len(page['proposals']) == 1 and page['next_cursor']
        next_page = post('/expert/dashboard', dict(customer_id=customer, cursor=page['next_cursor']))[1]
        assert {page['proposals'][0]['job_id'], next_page['proposals'][0]['job_id']} == {job, new_job}
        assert next_page['next_cursor'] is None


def test_role_removal_hides_unresolved_identity_until_restored(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer', 'Approver'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        save = full_decision(post, scope, draft)
        assert review_command(post, scope, **save)[1]['saved']
        onboarding.manage('alice-login', customer, 'remove-review', 2,
                          dict(kind='roles', target=redeem[1], roles=['Approver']))
        assert post('/session', {'credential': 'bob-login'})[0] == 200
        assert post('/expert/context', scope)[1]['pending'] is None
        assert post('/expert/resolve', dict(scope, command_id='same-save'))[0] == 403
        onboarding.manage('alice-login', customer, 'restore-review', 3,
                          dict(kind='roles', target=redeem[1], roles=['Reviewer']))
        assert post('/session', {'credential': 'bob-login'})[0] == 200
        assert post('/expert/resolve', dict(scope, command_id='same-save'))[1]['revision'] == 1
        assert post('/expert/acknowledge', selected(post, scope, 'same-save'))[0] == 200
        # The same key and a new body can never resolve against the earlier receipt.
        altered = dict(save, reason='Different request')
        assert review_command(post, scope, **altered)[1]['category'] == 'command_conflict'
        assert post('/expert/resolve', dict(scope, command_id='same-save'))[1]['category'] == 'command_conflict'


def test_reauthentication_recovers_staged_body_when_mutation_request_never_arrived(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    join, _ = http_client
    with join('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        save = full_decision(post, scope, draft)
        code, prepared = post('/expert/prepare', dict(scope, command=save))
        assert code == 200 and prepared['state'] == 'prepared'
    with join('bob-login') as fresh:
        assert fresh('/expert/context', scope)[1]['pending']['command_id'] == save['command_id']
        resolved = fresh('/expert/resolve', dict(scope, command_id=save['command_id']))[1]
        assert resolved['category'] == 'unknown_command'
        assert review_command(fresh, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        assert fresh('/expert/retry', selected(fresh, scope, save['command_id']))[1]['revision'] == 1
        view = review_command(fresh, scope, 'review_read', draft_id=draft)[1]
        assert view['definitions'][0]['label'] == 'Rated capacity' and view['reason'] == save['reason']


def test_demotion_filters_unlinked_evidence_and_own_prior_edit_receipts(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer', 'Approver'])
    onboarding.accept(*redeem)
    job, proposal_id = publish_fixture(onboarding, customer, with_unlinked=True)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal_id)
        command = full_decision(post, scope, draft)
        assert review_command(post, scope, **command)[1]['saved']
        assert post('/expert/acknowledge', selected(post, scope, command['command_id']))[0] == 200
        stale = dict(command, command_id='former-reviewer-private-key')
        assert review_command(post, scope, **stale)[1]['category'] == 'revision_conflict'
        before = review_command(post, scope, 'review_read', draft_id=draft)[1]
        assert 'Internal test-only freight note' in json.dumps(before)
        assert 'former-reviewer-private-key' in json.dumps(before)
        onboarding.manage('alice-login', customer, 'approver-only', 2,
                          dict(kind='roles', target=redeem[1], roles=['Approver']))
        assert post('/session', {'credential': 'bob-login'})[0] == 200
        for operation in ('review_read', 'review_resume'):
            code, after = review_command(post, scope, operation, draft_id=draft)
            assert code == 200
            assert not after['pending_conflicts']
            assert 'Internal test-only freight note' not in json.dumps(after)
            assert after['definitions'] == before['definitions'] and after['associations'] == before['associations']
            assert 'Flow rate: 12 m3/h' in json.dumps(after['initial']['observations'])


def test_authorized_local_intent_reconciles_without_private_server_journal(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer', 'Approver'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login', second_credential='alice-login') as (post, admin):
        draft = create_draft(post, scope, proposal)
        command = full_decision(post, scope, draft)
        read = dict(scope, command=command)
        key_request = dict(scope, role='Reviewer')
        assert admin('/expert/unlock', key_request)[0] == 403
        code, envelope = post('/expert/unlock', key_request)
        assert code == 200
        assert len(bytes.fromhex(envelope['key'])) == 32
        assert post('/expert/reconcile', read)[1]['category'] == 'unknown_command'
        assert post('/expert/context', scope)[1]['pending'] is None
        assert review_command(post, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        onboarding.manage('alice-login', customer, 'approver-only', 2,
                          dict(kind='roles', target=redeem[1], roles=['Approver']))
        assert post('/session', {'credential': 'bob-login'})[0] == 200
        assert post('/expert/unlock', key_request)[0] == 403
        assert post('/expert/reconcile', read)[0] == 403
        allowed = post('/expert/unlock', dict(scope, role='Approver'))[1]
        assert allowed['key'] != envelope['key']


def test_editor_scope_survives_another_authorized_login_on_the_same_origin(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Administrator', 'Reviewer'])
    onboarding.accept(*redeem)
    users = onboarding.members('bob-login', customer)
    alice = next(row for row in users['users'] if row['subject'] == 'alice')
    onboarding.manage('bob-login', customer, 'grant-reviewer', users['revision'],
                      dict(kind='roles', target=alice['membership_id'], roles=['Administrator', 'Reviewer']))
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as tab:
        draft = create_draft(tab, scope, proposal)
        save = full_decision(tab, scope, draft)
        expected = tab('/expert/unlock', dict(scope, role='Reviewer'))[1]['scope']
        request = dict(scope, expected_scope=expected, command=save)
        assert tab('/expert/prepare', request)[0] == 200
        assert tab('/session', {'credential': 'alice-login'})[0] == 200
        assert tab('/expert/prepare', request)[0] == 403
        assert tab('/expert/command', dict(request, prepared=True))[0] == 403
        assert tab('/expert/unlock', dict(scope, expected_scope=expected, role='Reviewer'))[0] == 403
        assert tab('/expert/context', scope)[1]['pending'] is None
        assert review_command(tab, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        assert tab('/session', {'credential': 'bob-login'})[0] == 200
        persisted = tab('/expert/context', scope)[1]
        assert persisted['scope'] == expected and persisted['pending']['command_id'] == save['command_id']
        assert tab('/expert/reconcile', request)[1]['category'] == 'unknown_command'
        result = tab('/expert/retry', dict(selected(tab, scope, save['command_id']), expected_scope=expected))[1]
        assert result['saved'] and result['revision'] == 1


def test_incomplete_saved_draft_is_inspectable_without_general_evidence(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer', 'Approver'])
    onboarding.accept(*redeem)
    job, proposal_id = publish_fixture(onboarding, customer, with_unlinked=True)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal_id)
        save = full_decision(post, scope, draft)
        del save['definitions'][0]['decision']
        save['definitions'][1]['observation_ids'] = [None, {'bad': 'reference'}]
        save['associations'] = [None, {'id': 'no-disposition'}]
        assert review_command(post, scope, **save)[1]['saved']
        assert post('/expert/acknowledge', selected(post, scope, save['command_id']))[0] == 200
        onboarding.manage('alice-login', customer, 'approver-only', 2,
                          dict(kind='roles', target=redeem[1], roles=['Approver']))
        assert post('/session', {'credential': 'bob-login'})[0] == 200
        code, view = review_command(post, scope, 'review_read', draft_id=draft)
        assert code == 200 and view['revision'] == 1
        assert 'decision' not in view['definitions'][0] and view['associations'][0] is None
        assert 'Internal test-only freight note' not in json.dumps(view)
        denied = review_command(post, scope, 'review_approve', command_id='incomplete',
                                draft_id=draft, expected_revision=1, product_type='pump', explicit=True)[1]
        assert denied['category'] == 'validation_error' and not denied['approved']


def test_bounded_local_recovery_proves_absence_without_exposing_another_command(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal_id = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal_id)
        command = full_decision(post, scope, draft)
        selector = dict(scope, operation=command['operation'], command_id=command['command_id'])
        code, before = post('/expert/local-status', selector)
        assert code == 200 and before == {'state': 'absent'}
        server = dict(command, reason='Other device wrote this original')
        assert post('/expert/prepare', dict(scope, command=server))[0] == 200
        assert post('/expert/local-status', selector)[1] == {'state': 'occupied'}
        assert post('/expert/reconcile', dict(scope, command=command))[1]['category'] == 'other_pending'
        meta = post('/expert/context', scope)[1]
        receipt = post('/expert/retry', dict(scope, command_id=command['command_id'], tag=meta['pending_tag']))[1]
        assert receipt['saved']
        assert post('/expert/acknowledge', dict(scope, command_id=command['command_id'], tag='old'))[0] != 200
        assert post('/expert/acknowledge', dict(scope, command_id=command['command_id'], tag=meta['pending_tag']))[0] == 200
        assert post('/expert/local-status', selector)[1] == {'state': 'occupied'}
        exact = post('/expert/reconcile', dict(scope, command=command))[1]
        assert exact['category'] == 'command_conflict' and exact['journal_pending'] is False
        assert review_command(post, scope, 'review_read', draft_id=draft)[1]['reason'] == server['reason']


def test_cold_oversized_intent_can_distinguish_a_different_prior_receipt(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal_id = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal_id)
        command = full_decision(post, scope, draft)
        actual = review_command(post, scope, **command)[1]
        assert actual['saved']
        assert post('/expert/acknowledge', dict(scope, command_id=command['command_id'],
                                                tag=actual['journal_tag']))[0] == 200
    with http_client[0]('bob-login') as fresh:
        assert fresh('/expert/context', scope)[1]['pending'] is None
        # A bounded fingerprint of a different synthetic command. Its full body exceeds the wire limit.
        oversized = dict(command, reason='Many synthetic observations 🌊' * 7000)
        assert len(json.dumps(oversized).encode()) > customer_review.MAX_BODY
        selector = dict(scope, operation=command['operation'], command_id=command['command_id'])
        wrong = fresh('/expert/local-status', dict(selector, fingerprint='0' * 64))
        assert wrong == (200, {'state': 'different'})
        exact = fresh('/expert/local-status', dict(selector, fingerprint=customer_review.command_fingerprint(command)))[1]
        assert exact['state'] == 'matching' and exact['receipt']['draft_id'] == draft
        assert exact['receipt']['revision'] == 1
        assert fresh('/expert/local-status', dict(selector, fingerprint=customer_review.command_fingerprint(oversized)))[1] == {'state':'different'}
        assert review_command(fresh, scope, 'review_read', draft_id=draft)[1]['reason'] == command['reason']


def test_retry_and_acknowledge_require_the_original_server_body_tag(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal_id = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal_id)
        command = full_decision(post, scope, draft)
        selector = dict(scope, command_id=command['command_id'])
        refused = review_command(post, scope, **dict(command, expected_revision=-1))[1]
        assert refused['category'] == 'invalid_request'
        old = dict(selector, tag=refused['journal_tag'])
        assert post('/expert/acknowledge', old)[0] == 200
        replacement = post('/expert/prepare', dict(scope, command=command))[1]
        assert replacement['journal_tag'] != old['tag']
        for path in ('/expert/retry', '/expert/acknowledge'):
            for stale in (selector, old):
                assert post(path, stale)[0] == 400
        assert review_command(post, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        current = dict(selector, tag=replacement['journal_tag'])
        assert post('/expert/retry', current)[1]['revision'] == 1
        assert post('/expert/acknowledge', current)[0] == 200


def test_comparison_fingerprint_keeps_exact_persisted_decimal_and_string_types():
    import hashlib
    from decimal import Decimal
    canonical = '["object",[["z",["array",[true,["number","0"],null,["number","125e-9"]]]],["\\u00e9","\\ud83c\\udf0a"]]]'
    expected = hashlib.sha256(canonical.encode('ascii')).hexdigest()
    assert customer_review.command_fingerprint({'é':'🌊','z':[True,Decimal('0.000'),None,Decimal('0.0000001250')]}) == expected
    assert customer_review.command_fingerprint({'value':Decimal('9007199254740993')}) != customer_review.command_fingerprint({'value':Decimal('9007199254740992')})


def test_cold_normal_receipt_recognizes_jsonb_numbers_and_keeps_the_original_tag(onboarding, http_client):
    from copy import deepcopy
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal_id = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal_id)
        original = full_decision(post, scope, draft)
        original['definitions'][0]['conditions'] = [dict(operand_ids=['o-flow'], threshold=1e21, sample_count=1)]
        saved = review_command(post, scope, **original)[1]
        assert saved['saved'] and saved['revision'] == 1
        key = dict(scope, command_id=original['command_id'], tag=saved['journal_tag'])

    with http_client[0]('bob-login') as fresh:
        recovered = fresh('/expert/resolve', key)[1]
        assert recovered == saved
        integer_form = deepcopy(original)
        integer_form['definitions'][0]['conditions'][0]['threshold'] = 10**21
        request = dict(scope, command=integer_form)
        assert fresh('/expert/reconcile', request)[1]['revision'] == 1
        assert fresh('/expert/prepare', request)[1] == {'state':'pending','journal_tag':saved['journal_tag']}
        assert fresh('/expert/acknowledge', key)[0] == 200
        for field, value in (('threshold',10**21+1), ('sample_count',True)):
            changed = deepcopy(integer_form)
            changed['definitions'][0]['conditions'][0][field] = value
            assert fresh('/expert/reconcile', dict(scope, command=changed))[1]['category'] == 'command_conflict'
        view = review_command(fresh, scope, 'review_read', draft_id=draft)[1]
        assert view['revision'] == 1 and view['definitions'][0]['conditions'][0]['threshold'] == 10**21


def test_expert_wire_refuses_lossy_numbers_and_ambiguous_json_before_any_effect(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        original = full_decision(post, scope, draft)
        original['definitions'][0]['conditions'] = [dict(operand_ids=['o-flow'], threshold='WIRE-NUMBER', exact=0.1)]
        template = json.dumps(dict(scope,command=original))
        def wire(token):
            return template.replace('"WIRE-NUMBER"', token).encode()
        for value in ('1.0000000000000001', '9e9999', '1e-999', 'NaN', 'Infinity', '-Infinity'):
            for path in ('/expert/prepare', '/expert/command'):
                assert post(path, wire(value), raw=True)[0] == 400
        repeated_key = wire('1').replace(b'"threshold": 1', b'"threshold": 2, "threshold": 1')
        assert post('/expert/prepare', repeated_key, raw=True)[0] == 400
        assert post('/expert/context', scope)[1]['pending'] is None
        assert review_command(post, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        committed = post('/expert/command', wire('1.0'), raw=True)[1]
        assert committed['saved'] and committed['revision'] == 1
        assert post('/expert/reconcile', wire('1.0000000000000001'), raw=True)[0] == 400
        assert post('/expert/reconcile', wire('1'), raw=True)[1]['saved']


def test_incompatible_text_refused_and_legacy_originals_have_exact_disposition(onboarding, http_client):
    from nilaris import customer_review
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, proposal = publish_fixture(onboarding, customer)
    scope = dict(customer_id=customer, job_id=job)
    journal = http_client[1]
    actor = onboarding.identities.verify('bob-login')
    channel = customer_review.ReviewJournal(customer, job, 'Reviewer')
    with http_client[0]('bob-login') as post:
        draft = create_draft(post, scope, proposal)
        save = full_decision(post, scope, draft)
        for invalid in ('\0', '\ud800', '\udfff'):
            for command in (dict(save,reason=invalid), dict(save,command_id=invalid),
                            dict(save,definitions=[{invalid:'nested key'}])):
                raw = json.dumps(dict(scope, command=command)).encode('ascii')
                for path in ('/expert/prepare','/expert/command','/expert/reconcile'):
                    assert post(path, raw, raw=True)[0] == 400
                identity = dict(scope, command_id=command['command_id'], operation='review_save',
                                fingerprint=customer_review.command_fingerprint(command))
                assert post('/expert/local-status', identity)[1] == {'state':'absent'}
                # Simulate the private file format of a previously staged exact original.
                path = journal._path(actor, channel)
                path.write_text(json.dumps(command))
                path.chmod(0o600)
                tag = customer_review.journal_tag(command)
                selector = dict(scope, command_id=command['command_id'], tag=tag)
                assert post('/expert/local-status', identity)[1] == {'state':'occupied'}
                assert post('/expert/acknowledge', selector)[0] == 403
                result = post('/expert/resolve', selector)[1]
                assert result.get('known') is True and result.get('category') == 'unrepresentable_command'
                assert journal.read(actor, channel=channel) == command
                assert post('/expert/acknowledge', dict(selector, tag='different'))[0] == 400
                assert post('/expert/acknowledge', selector)[1]['state'] == 'acknowledged'
                assert post('/expert/local-status', identity)[1] == {'state':'absent'}
            assert post('/expert/context', dict(scope,job_id=invalid))[0] == 400
            assert review_command(post, scope,'review_read',draft_id=invalid)[0] == 400
        assert review_command(post, scope, 'review_read', draft_id=draft)[1]['revision'] == 0
        assert post('/expert/command', dict(scope,command=dict(save,reason='Valid 🐋')))[1]['revision'] == 1
