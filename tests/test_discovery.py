"""Durable discovery through the authenticated command seam and real PostgreSQL."""
from test_command_service import command_process, stop
from test_admission import member
import pytest


@pytest.mark.parametrize('adopt_before_admit', [True, False])
def test_adopted_revision_reconciles_original_existing_or_accepted_receipt(database, adopt_before_admit):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    from nilaris.execution import ControlledSource
    from test_recovery import literal_output
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    original = ingestion.offer('operator', 'original', [member()])['members'][0]['job_id']
    ingestion.capture(original, ingestion.claim(original, 0),
                      ControlledSource({'P': ('r1', b'controlled input')}))
    ingestion.publish(original, ingestion.claim(original, ingestion.status(original)['revision']), literal_output())
    initial = ingestion.result(original)
    changed = dict(member('revision-2'), source_revision='r2')
    if adopt_before_admit:
        alias = ingestion.offer('operator', 'revision-2', [changed])['members'][0]['job_id']
    staged = ingestion.discovery_stage('adopted', 'page', None, 'next', [changed])
    assert staged.get('state') == 'staged', staged
    if not adopt_before_admit:
        offered = ingestion.discovery_admit('adopted', 'page')
        assert 'members' in offered, offered
        alias = offered['members'][0]['job_id']
    adopted = ingestion.capture(alias, ingestion.claim(alias, 0),
                                ControlledSource({'P': ('r2', b'controlled input')}))
    assert adopted['canonical_job_id'] == original
    if adopt_before_admit:
        offered = ingestion.discovery_admit('adopted', 'page')
    assert offered['members'][0]['disposition'] == ('existing' if adopt_before_admit else 'accepted')
    assert offered['members'][0]['job_id'] == (original if adopt_before_admit else alias)
    settled = Ingestion(database, AdmissionProfile.l1()).discovery_reconcile('adopted', 'page')
    assert settled.get('state') == 'settled', settled
    assert settled['members'] == [dict(member=changed, disposition=offered['members'][0], pending=False)]
    assert settled['checkpoint'] == dict(cursor='next', exhausted=False)
    assert ingestion.result(original) == initial


def page(page_id='page-1', expected_cursor=None, next_cursor='cursor-2', members=None):
    return dict(operation='discovery_stage', namespace='synthetic-discovery',
                page_id=page_id, expected_cursor=expected_cursor, next_cursor=next_cursor,
                members=[member()] if members is None else members)


def test_public_reprocess_cannot_occupy_frozen_discovery_receipt_key(command_process, database):
    from nilaris.admission import AdmissionProfile, Ingestion
    from nilaris.execution import ControlledSource
    from test_recovery import literal_output
    client, start, _ = command_process
    start()
    worker = Ingestion(database, AdmissionProfile.l1())
    original = worker.offer('operator', 'original', [member()])['members'][0]['job_id']
    worker.capture(original, worker.claim(original, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    completed = worker.publish(original, worker.claim(original, worker.status(original)['revision']), literal_output())
    assert client(page())['state'] == 'staged'
    before = client(dict(operation='status', job_id=original))['admission']
    denied = client(dict(operation='reprocess', namespace='discovery:synthetic-discovery',
                         command_id='page-1', job_id=original, expected_revision=completed['revision'],
                         expected_generation=completed['generation'], actor='reviewer', reason='review same contract'))
    assert denied.get('category') == 'reserved_namespace', denied
    assert client(dict(operation='resolve', namespace='discovery:synthetic-discovery',
                       command_id='page-1'))['known'] is False
    assert client(dict(operation='status', job_id=original))['admission'] == before
    key = dict(namespace='synthetic-discovery', page_id='page-1')
    offered = client(dict(operation='discovery_admit', **key))
    assert offered['members'][0]['job_id'] == original
    assert offered['members'][0]['disposition'] == 'existing'
    settled = client(dict(operation='discovery_reconcile', **key))
    assert settled['state'] == 'settled'
    assert settled['members'][0]['disposition'] == offered['members'][0]
    assert settled['checkpoint'] == dict(cursor='cursor-2', exhausted=False)


def test_historical_reprocess_receipt_in_reserved_namespace_keeps_replay_and_conflict(database):
    import psycopg
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    from nilaris.execution import ControlledSource
    from test_recovery import literal_output
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    original = ingestion.offer('operator', 'original', [member()])['members'][0]['job_id']
    ingestion.capture(original, ingestion.claim(original, 0),
                      ControlledSource({'P': ('r1', b'controlled input')}))
    completed = ingestion.publish(original, ingestion.claim(original, ingestion.status(original)['revision']), literal_output())
    arguments = dict(namespace='legacy', command_id='reviewed', job_id=original,
                     expected_revision=completed['revision'], expected_generation=completed['generation'],
                     actor='reviewer', reason='review same contract')
    outcome = ingestion.reprocess(**arguments)
    assert outcome['disposition'] == 'accepted'
    # Install a historical receipt only in the disposable persistence fixture.
    with psycopg.connect(**database) as connection:
        connection.execute('UPDATE admission_receipts SET namespace=%s WHERE namespace=%s AND command_id=%s',
                           ('discovery:legacy', 'legacy', 'reviewed'))
    arguments['namespace'] = 'discovery:legacy'
    fresh = Ingestion(database, AdmissionProfile.l1())
    before = fresh.status(original)['admission']
    assert fresh.reprocess(**arguments) == outcome
    assert fresh.reprocess(**dict(arguments, reason='changed'))['category'] == 'command_conflict'
    assert fresh.resolve('discovery:legacy', 'reviewed') == outcome
    after = fresh.status(original)['admission']
    assert {key: after[key] for key in ('waiting_jobs', 'live_jobs', 'job_reservation_bytes', 'receipt_reservation_bytes')} == {
        key: before[key] for key in ('waiting_jobs', 'live_jobs', 'job_reservation_bytes', 'receipt_reservation_bytes')}


def test_frozen_page_survives_process_restart_without_accepting_jobs(command_process):
    client, start, _ = command_process
    process = start()
    staged = client(page())
    assert staged.get('state') == 'staged'
    assert staged['checkpoint'] == dict(cursor=None, exhausted=False)
    assert staged['members'] == [dict(member=member(), disposition=None, pending=True)]
    kill(process)
    start()
    assert client(dict(operation='discovery_read', namespace='synthetic-discovery',
                       page_id='page-1')) == staged
    changed = page()
    changed['members'][0]['source_revision'] = 'changed'
    assert client(changed)['category'] == 'page_conflict'
    assert client(dict(operation='resolve', namespace='discovery:synthetic-discovery',
                       command_id='page-1'))['known'] is False


def test_explicit_admission_and_receipt_reconciliation_preserve_all_pending_members(command_process):
    client, start, _ = command_process
    start()
    unresolved = member('unresolved')
    unresolved['identity'] = 'ambiguous'
    unresolved['document_id'] = None
    rejected = member('rejected')
    rejected['media_type'] = 'text/plain'
    client(page(members=[member(), unresolved, rejected]))
    key = dict(namespace='synthetic-discovery', page_id='page-1')
    held = client(dict(operation='discovery_reconcile', **key))
    assert held.get('category') == 'receipt_unknown'
    assert client(dict(operation='discovery_read', **key))['checkpoint']['cursor'] is None
    offered = client(dict(operation='discovery_admit', **key))
    assert offered['totals'] == dict(offered=3, accepted=1, deferred=1, rejected=1)
    settled = client(dict(operation='discovery_reconcile', **key))
    assert settled['state'] == 'settled'
    assert settled['checkpoint'] == dict(cursor='cursor-2', exhausted=False)
    assert [item['pending'] for item in settled['members']] == [False, True, True]
    assert [item['disposition']['disposition'] for item in settled['members']] == ['accepted','deferred','rejected']
    assert settled['members'][1]['disposition']['reason'] == 'identity_unresolved'
    assert client(dict(operation='discovery_admit', **key)) == offered
    assert client(dict(operation='discovery_reconcile', **key)) == settled
    assert client(dict(operation='status', job_id=offered['members'][0]['job_id']))['input'] == member()


def test_typed_frozen_content_and_stale_checkpoint_cannot_overwrite_page(command_process):
    client, start, _ = command_process
    start()
    original = member()
    original['input_bytes'] = 1
    client(page(members=[original]))
    changed = page(members=[dict(original, input_bytes=True)])
    assert client(changed)['category'] == 'page_conflict'
    assert client(page('competitor'))['category'] == 'page_in_progress'
    client(dict(operation='discovery_admit', namespace='synthetic-discovery', page_id='page-1'))
    client(dict(operation='discovery_reconcile', namespace='synthetic-discovery', page_id='page-1'))
    assert client(page('stale'))['category'] == 'cursor_conflict'


def test_staging_requires_live_monitor_before_reserving_storage(database, tmp_path):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1(), monitor_path=tmp_path / 'absent-monitor')
    command = page()
    command.pop('operation')
    assert ingestion.discovery_stage(**command)['category'] == 'service_monitor_unavailable'
    assert ingestion.discovery_read('synthetic-discovery','page-1')['known'] is False


def kill(process):
    import os
    import signal
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def test_original_admission_survives_abrupt_process_loss_before_reconciliation(command_process):
    client, start, _ = command_process
    process = start()
    client(page())
    key = dict(namespace='synthetic-discovery', page_id='page-1')
    offered = client(dict(operation='discovery_admit', **key))
    kill(process)
    start()
    assert client(dict(operation='discovery_read', **key))['checkpoint']['cursor'] is None
    reconciled = client(dict(operation='discovery_reconcile', **key))
    assert reconciled['members'][0]['disposition'] == offered['members'][0]
    assert client(dict(operation='status',job_id=offered['members'][0]['job_id']))['admission']['waiting_jobs'] == 1


@pytest.mark.parametrize('boundary', ['before','after'])
def test_atomic_reconciliation_survives_actual_kill_at_commit(command_process, monkeypatch, tmp_path, boundary):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    import time
    import test_command_service
    marker = tmp_path / 'commit-marker'
    monkeypatch.setenv('I10_COMMIT_PAUSE',boundary)
    monkeypatch.setenv('I10_COMMIT_MARKER',str(marker))
    monkeypatch.setattr(test_command_service,'PYTHON', [*test_command_service.PYTHON, str(Path(__file__).with_name('discovery_crash_process.py'))])
    client, start, _ = command_process
    process = start()
    client(page())
    key = dict(namespace='synthetic-discovery',page_id='page-1')
    offered = client(dict(operation='discovery_admit',**key))
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(client,dict(operation='discovery_reconcile',**key))
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), 'task-only transaction pause was not reached'
        kill(process)
        assert response.result(timeout=10)['known'] is False
    monkeypatch.setenv('I10_COMMIT_PAUSE','')
    start()
    recovered = client(dict(operation='discovery_read',**key))
    assert recovered['state'] == ('staged' if boundary == 'before' else 'settled')
    assert recovered['checkpoint']['cursor'] == (None if boundary == 'before' else 'cursor-2')
    settled = client(dict(operation='discovery_reconcile',**key))
    assert settled['checkpoint'] == dict(cursor='cursor-2',exhausted=False)
    assert settled['members'][0]['disposition']['job_id'] == offered['members'][0]['job_id']
    assert client(dict(operation='status',job_id=offered['members'][0]['job_id']))['admission']['waiting_jobs'] == 1


def test_concurrent_empty_pages_guard_checkpoint_and_replay_never_rewinds(database):
    from concurrent.futures import ThreadPoolExecutor
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    migrate(database)
    def stage_empty(page_id):
        return Ingestion(database,AdmissionProfile.l1()).discovery_stage('empty',page_id,None,'next',[])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(stage_empty,['a','b']))
    assert sorted(result.get('state',result.get('category')) for result in results) == ['page_in_progress','staged']
    winning = next(result['page_id'] for result in results if result.get('state') == 'staged')
    ingestion = Ingestion(database,AdmissionProfile.l1())
    assert ingestion.discovery_admit('empty',winning)['category'] == 'no_offer_needed'
    assert ingestion.discovery_reconcile('empty',winning)['checkpoint'] == dict(cursor='next',exhausted=False)
    assert ingestion.discovery_stage('empty','loop','next','next',[])['category'] == 'cursor_conflict'
    assert ingestion.discovery_stage('empty','end','next',None,[])['state'] == 'staged'
    assert ingestion.discovery_reconcile('empty','end')['checkpoint'] == dict(cursor=None,exhausted=True)
    assert ingestion.discovery_reconcile('empty',winning)['checkpoint'] == dict(cursor=None,exhausted=True)
    assert ingestion.discovery_stage('empty','extra',None,'later',[])['category'] == 'cursor_conflict'
    migrate(database)
    assert Ingestion(database).discovery_read('empty',winning)['state'] == 'settled'


def test_discovery_transport_authentication_and_cursor_validation(command_process):
    from http.client import HTTPConnection
    import json
    client,start,_ = command_process
    start()
    connection = HTTPConnection('127.0.0.1',18765,timeout=3)
    connection.request('POST','/command',json.dumps(page()),{'Authorization':'Bearer denied'})
    assert json.loads(connection.getresponse().read())['category'] == 'unauthorized'
    connection.close()
    for value in (False,0,[],{},'', ' '):
        assert client(dict(page(),expected_cursor=value))['category'] == 'invalid_request'
    assert client(dict(page(),next_cursor='x'*1025))['category'] == 'invalid_request'
    for operation,extra in [('result',dict(job_id='absent')),('review_read',dict(draft_id='absent'))]:
        assert client(dict(operation=operation,cursor=None,**extra))['category'] == 'invalid_request'
    assert client(dict(operation='offer',namespace='discovery:synthetic-discovery',command_id='page-1',members=[member()]))['category'] == 'reserved_namespace'
    assert client(dict(page(),next_cursor=None,members=[]))['state'] == 'staged'


def test_journal_reservation_is_retained_once_and_forces_original_offer_deferral(database):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('journal-budget',logical_bytes=8620000)
    ingestion = Ingestion(database,profile)
    staged = ingestion.discovery_stage('budget','one',None,'next',[member()])
    reservation = staged['reservation_bytes'] + 8192
    assert 65536 < reservation < 100000
    offered = ingestion.discovery_admit('budget','one')
    assert offered['totals'] == dict(offered=1,accepted=0,deferred=1,rejected=0)
    assert offered['members'][0]['reason'] == 'byte_capacity'
    settled = ingestion.discovery_reconcile('budget','one')
    assert settled['members'][0]['pending'] is True
    assert settled['checkpoint']['cursor'] == 'next'
    assert ingestion.discovery_stage('budget','two','next','last',[])['category'] == 'journal_capacity'
    assert ingestion.discovery_admit('budget','one') == offered
    # A separately accepted existing Job exposes the shared aggregate through status.
    full = Ingestion(database,AdmissionProfile.l1())
    job = full.offer('ordinary','probe',[member()])['members'][0]['job_id']
    usage = full.status(job)['admission']
    assert usage['discovery_retained_bytes'] == reservation
    assert usage['receipt_reservation_bytes'] == reservation + 270336
    assert usage['terminal_headroom_bytes'] == 8388608
    migrate(database)
    assert Ingestion(database).status(job)['admission']['receipt_reservation_bytes'] == reservation + 270336


def test_original_offer_retains_existing_waiting_deferred_and_policy_denied_members(database):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    migrate(database)
    ingestion = Ingestion(database,AdmissionProfile.l1().lowered_for_test('one-waiting-discovery',waiting_jobs=1))
    members = [member('first'),member('same'),dict(member('waiting'),document_id='other'),
               dict(member('denied'),connector='synthetic-drive-v1')]
    ingestion.discovery_stage('mixed','page',None,None,members)
    offered = ingestion.discovery_admit('mixed','page')
    assert [item['disposition'] for item in offered['members']] == ['accepted','existing','deferred','rejected']
    assert offered['members'][0]['job_id'] == offered['members'][1]['job_id']
    assert [item.get('reason') for item in offered['members'][2:]] == ['waiting_capacity','source_policy_denied']
    settled = ingestion.discovery_reconcile('mixed','page')
    assert [item['pending'] for item in settled['members']] == [False,False,True,True]
    assert settled['checkpoint'] == dict(cursor=None,exhausted=True)
    assert Ingestion(database).discovery_read('mixed','page') == settled


def test_discovery_replay_preserves_literal_saved_approved_schema_and_effect_ids(database):
    from nilaris.admission import AdmissionProfile, Ingestion
    from nilaris.review import Review
    from test_review import published, decided
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('expert','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = decided(review,draft)
    assert review.save('expert','save',draft,0,view['definitions'],view['associations'],'synthetic-expert','Keep rated label')['saved'] is True
    assert review.approve('expert','approve',draft,1,'Fixture pump','synthetic-expert',True)['approved'] is True
    before = review.read(draft)
    assert [item['label'] for item in before['approved_schema']['definitions']] == ['Rated flow rate','Pressure','Material']
    ingestion = Ingestion(database,AdmissionProfile.l1())
    effects = ingestion.result(job)
    ingestion.discovery_stage('approved','page',None,None,[dict(member(),product_type='Fixture pump')])
    offered = ingestion.discovery_admit('approved','page')
    assert offered['members'][0]['disposition'] == 'existing'
    assert offered['members'][0]['job_id'] == job
    ingestion.discovery_reconcile('approved','page')
    assert Ingestion(database,AdmissionProfile.l1()).discovery_admit('approved','page') == offered
    assert Review(database).read(draft) == before
    assert Ingestion(database).result(job) == effects


def test_malformed_incomplete_and_wrong_provenance_receipts_hold_every_member(database):
    import copy
    import psycopg
    from psycopg.types.json import Jsonb
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    migrate(database)
    ingestion = Ingestion(database,AdmissionProfile.l1())
    ingestion.discovery_stage('receipt','page',None,'next',[member()])
    original = ingestion.discovery_admit('receipt','page')
    malformed = [[],dict(known=False,category=None)]
    missing = copy.deepcopy(original); missing['members'] = []; malformed.append(missing)
    wrong = copy.deepcopy(original); wrong['members'][0]['member_key'] = 'unrelated'; malformed.append(wrong)
    wrong = copy.deepcopy(original); wrong['members'][0]['job_id'] = 'absent'; malformed.append(wrong)
    unrelated = ingestion.offer('ordinary', 'unrelated', [dict(member(), source_revision='unrelated')])
    wrong = copy.deepcopy(original); wrong['members'][0]['job_id'] = unrelated['members'][0]['job_id']; malformed.append(wrong)
    wrong = copy.deepcopy(original); wrong['totals']['accepted'] = True; malformed.append(wrong)
    wrong = copy.deepcopy(original); wrong['receipt_stored'] = False; malformed.append(wrong)
    wrong = copy.deepcopy(original); wrong['members'][0]['next_action'] = 'x'*200000; malformed.append(wrong)
    # Corrupt only a disposable persistence-boundary fixture, then observe via public APIs.
    for receipt in malformed:
        with psycopg.connect(**database) as connection:
            connection.execute('UPDATE admission_receipts SET result=%s WHERE namespace=%s AND command_id=%s',
                               (Jsonb(receipt),'discovery:receipt','page'))
        assert ingestion.discovery_reconcile('receipt','page')['category'] == 'receipt_malformed'
        readback = Ingestion(database).discovery_read('receipt','page')
        assert readback['checkpoint'] == dict(cursor=None,exhausted=False)
        assert readback['members'] == [dict(member=member(),disposition=None,pending=True)]
    with psycopg.connect(**database) as connection:
        connection.execute('UPDATE admission_receipts SET result=%s,request=%s WHERE namespace=%s AND command_id=%s',
                           (Jsonb(original),Jsonb([dict(member(),source_revision='wrong')]),'discovery:receipt','page'))
    assert ingestion.discovery_reconcile('receipt','page')['category'] == 'receipt_malformed'


def test_unavailable_discovery_persistence_never_claims_nonacceptance(database):
    import socket
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    migrate(database)
    good = Ingestion(database,AdmissionProfile.l1())
    good.discovery_stage('unavailable','page',None,None,[member()])
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1',0))
        bad = Ingestion(dict(host='127.0.0.1',port=endpoint.getsockname()[1],user='probe',
                             password='synthetic',dbname='probe',connect_timeout=1),AdmissionProfile.l1())
        for operation in (bad.discovery_admit,bad.discovery_reconcile,bad.discovery_read):
            result = operation('unavailable','page')
            assert result['known'] is False
            assert result['category'] == 'persistence_unavailable'
            assert result['coverage'] == 'unknown'
    readback = Ingestion(database).discovery_read('unavailable','page')
    assert readback['checkpoint'] == dict(cursor=None,exhausted=False)
    assert readback['members'][0]['pending'] is True


def test_configured_synthetic_drive_policy_preserves_original_pin_on_discovery(database):
    import base64
    import hashlib
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    from nilaris.drive import PinnedDrive
    migrate(database)
    content = b'independent literal synthetic content'
    pinned = dict(member(),connector='synthetic-drive-v1',source_root='synthetic-root',
                  expected_sha256=hashlib.sha256(content).hexdigest(),input_bytes=len(content))
    def policy(item):
        return PinnedDrive(dict(mode='synthetic-drive-v1',allowed_member=item,
            object=dict(source_id='P',source_revision='r1',source_root='synthetic-root',
                        content_b64=base64.b64encode(content).decode())))
    first = Ingestion(database,AdmissionProfile.l1(),drive_policy=policy(pinned))
    first.discovery_stage('pins','original',None,'next',[pinned])
    accepted = first.discovery_admit('pins','original')
    assert accepted['totals']['accepted'] == 1
    first.discovery_reconcile('pins','original')
    changed = dict(pinned,expected_sha256='0'*64)
    second = Ingestion(database,AdmissionProfile.l1(),drive_policy=policy(changed))
    second.discovery_stage('pins','changed','next',None,[changed])
    refused = second.discovery_admit('pins','changed')
    assert refused['members'][0]['reason'] == 'source_pin_conflict'
    settled = second.discovery_reconcile('pins','changed')
    assert settled['members'][0]['pending'] is True
    assert Ingestion(database).status(accepted['members'][0]['job_id'])['input'] == pinned


def test_retained_journal_is_seen_by_review_growth_and_execution_reprocess(database):
    from nilaris.admission import AdmissionProfile, Ingestion
    from nilaris.review import Review
    from test_review import published
    profile = AdmissionProfile.l1().lowered_for_test('shared-journal-ledger',logical_bytes=17100000)
    job,proposal_id = published(database,profile=profile)
    ingestion = Ingestion(database,profile)
    review = Review(database)
    draft = review.create('ledger','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    large = dict(member(),document_id='x'*60000)
    staged = ingestion.discovery_stage('ledger','retained',None,'next',[large])
    assert staged['state'] == 'staged'
    status = ingestion.status(job)
    assert status['admission']['discovery_retained_bytes'] == staged['reservation_bytes'] + 8192
    denied = ingestion.reprocess('ledger','reprocess',job,status['revision'],status['generation'],'synthetic-expert','Retained journal counts')
    assert denied['category'] == 'byte_capacity'
    # Review growth uses the same ledger: force finite retained history to its limit.
    view = review.read(draft)
    revision = 0
    for index in range(100):
        before = review.read(draft)
        result = review.save('ledger',f'save-{index}',draft,revision,view['definitions'],view['associations'],
                             'synthetic-expert','Retained history ' + 'x'*55000)
        if result.get('category') == 'byte_capacity':
            break
        revision = result['revision']
    else:
        raise AssertionError('Review retained history exceeded its finite aggregate budget')
    assert result['saved'] is False
    assert Review(database).read(draft) == before
    assert Ingestion(database).discovery_read('ledger','retained')['members'][0]['pending'] is True
    usage = ingestion.status(job)['admission']
    assert usage['job_reservation_bytes'] + usage['receipt_reservation_bytes'] <= 8711392
