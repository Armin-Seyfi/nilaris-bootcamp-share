"""Canonical content boundaries through ingestion, with real persistence."""
import hashlib

import pytest

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_admission import member
from test_recovery import literal_output


def captured(client, command, item, content):
    job_id = client.offer('operator', command, [item])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    assert claim.get('claimed') is True, claim
    return client.capture(job_id, claim,
                          ControlledSource({item['source_id']: (item['source_revision'], content)}))


def published_original(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    original = captured(client, 'original', member(), b'controlled input')
    status = client.publish(original['id'], client.claim(original['id'], original['revision']),
                            literal_output())
    assert status['state'] == 'completed', status
    return client, status, client.result(original['id'])


def assert_original_preserved(client, original, result):
    fresh = Ingestion(client.database)
    current = fresh.status(original['id'])
    for field in ('input', 'content_pin', 'profile', 'attempts', 'results', 'effect_count'):
        assert current[field] == original[field], field
    assert fresh.result(original['id']) == result


def test_changed_bytes_create_distinct_version_preserving_original_history(database):
    client, original, initial = published_original(database)
    changed = captured(client, 'changed-bytes', dict(member('changed'), source_revision='r2'),
                       b'changed controlled input')
    assert changed['state'] == 'queued'
    assert changed['canonical_job_id'] == changed['id'] != original['id']
    assert changed['content_pin'] != original['content_pin']
    assert changed['input']['document_id'] == original['input']['document_id']
    assert changed['input']['source_revision'] == 'r2'
    assert changed['effect_count'] == 0 and changed['results'] == []
    assert_original_preserved(client, original, initial)


@pytest.mark.parametrize('operand,value', [
    ('document_id', 'explicit-separate-document'),
    ('case_id', 'case-B'),
    ('membership_revision', 'manifest-2'),
    ('product_type', 'valve'),
])
def test_equal_bytes_in_distinct_supported_context_remain_separate(database, operand, value):
    client, original, initial = published_original(database)
    item = dict(member('separate-context'), source_revision='r2', **{operand: value})
    distinct = captured(client, 'separate-context', item, b'controlled input')
    assert distinct['state'] == 'queued'
    assert distinct['canonical_job_id'] == distinct['id'] != original['id']
    assert distinct['content_pin'] == original['content_pin']
    assert distinct['input'][operand] == value
    assert distinct['effect_count'] == 0 and distinct['results'] == []
    assert_original_preserved(client, original, initial)


@pytest.mark.parametrize('operand,value', [
    ('processing_contract', 'unknown-contract'),
    ('reference_set', 'unknown-references'),
    ('selection_policy', 'unknown-policy'),
    ('intent_id', 'unreviewed-intent'),
])
def test_unsupported_context_is_refused_without_canonical_adoption(database, operand, value):
    client, original, initial = published_original(database)
    item = dict(member('unsupported'), source_revision='r2', **{operand: value})
    response = client.offer('operator', 'unsupported-context', [item])
    outcome = response['members'][0]
    assert outcome['disposition'] == 'rejected'
    assert outcome['category'] == 'unsupported_context'
    assert 'job_id' not in outcome
    assert response['totals'] == dict(offered=1, accepted=0, deferred=0, rejected=1)
    assert Ingestion(database).resolve('operator', 'unsupported-context') == response
    assert_original_preserved(client, original, initial)


def test_simulated_digest_collision_holds_different_bytes_without_overwriting_original(
        database, monkeypatch):
    """Fault at stdlib hash boundary; this is not a real SHA-256 collision."""
    client, original, initial = published_original(database)
    item = dict(member('hash-boundary-fault'), source_revision='r2')
    job_id = client.offer('operator', 'simulated-collision', [item])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    assert claim.get('claimed') is True, claim
    real_sha256 = hashlib.sha256

    class SimulatedCollision:
        def hexdigest(self):
            return original['content_pin']

    def hash_boundary(content=b'', *args, **kwargs):
        if content == b'different bytes under simulated hash fault':
            return SimulatedCollision()
        return real_sha256(content, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(hashlib, 'sha256', hash_boundary)
        held = client.capture(job_id, claim, ControlledSource({
            'P': ('r2', b'different bytes under simulated hash fault')}))
    assert held['state'] == 'held'
    assert held['reason'] == 'input_integrity_conflict'
    assert held['canonical_job_id'] is None
    assert held['effect_count'] == 0 and held['results'] == []
    assert [attempt['stage'] for attempt in held['attempts']] == ['acquisition']
    assert held['attempts'][0]['stopped'] is True
    assert held['usage']['accounting_unknown'] is False
    assert Ingestion(database).status(job_id)['reason'] == 'input_integrity_conflict'
    assert_original_preserved(client, original, initial)


def test_competing_canonical_adoption_preserves_capacity_and_losing_acquisition_audit(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    first = client.offer('operator', 'first', [member()])['members'][0]['job_id']
    first_claim = client.claim(first, 0)
    assert first_claim.get('claimed') is True, first_claim
    rival_item = dict(member('rival'), source_revision='r2')
    waiting_items = [rival_item] + [dict(member(f'waiting-{index}'),
                                       document_id=f'explicit-doc-{index}') for index in range(3)]
    waiting = client.offer('operator', 'four-waiting', waiting_items)
    assert waiting['totals']['accepted'] == 4
    rival, next_eligible, *_ = [item['job_id'] for item in waiting['members']]
    full = client.status(first)
    assert full['admission']['active_capacity_reservations'] == 1
    assert full['admission']['waiting_jobs'] == 4
    assert full['admission']['live_jobs'] == 5
    original_profile = full['profile']
    rival_reservation = client.status(rival)['reservation_bytes']
    assert client.claim(rival, 0)['category'] == 'active_capacity'
    assert client.status(rival)['attempts'] == []
    overflow = client.offer('operator', 'overflow', [dict(member('overflow'), document_id='overflow')])
    assert overflow['members'][0]['disposition'] == 'deferred'
    canonical = client.capture(first, first_claim,
                               ControlledSource({'P': ('r1', b'controlled input')}))
    assert canonical['state'] == 'queued' and canonical['canonical_job_id'] == first
    rival_claim = client.claim(rival, 0)
    assert rival_claim.get('claimed') is True, rival_claim
    competing = client.status(rival)['admission']
    assert competing['active_capacity_reservations'] == 1
    assert competing['waiting_jobs'] == 4 and competing['live_jobs'] == 5
    loser = client.capture(rival, rival_claim,
                           ControlledSource({'P': ('r2', b'controlled input')}))
    assert loser['state'] == 'completed' and loser['disposition'] == 'duplicate-of'
    assert loser['canonical_job_id'] == first
    assert loser['active_capacity_reserved'] is False
    assert loser['effect_count'] == 0 and loser['results'] == []
    assert [attempt['stage'] for attempt in loser['attempts']] == ['acquisition']
    assert loser['attempts'][0]['stopped'] is True
    assert loser['attempts'][0]['completion_evidence'] == 'finite_controlled_call'
    assert loser['counters']['acquisition_requests'] == 1
    assert 0 < loser['usage']['retained_bytes'] <= loser['reservation_bytes'] < rival_reservation
    assert loser['usage']['accounting_unknown'] is False
    fresh = Ingestion(database, AdmissionProfile.l1().lowered_for_test(
        'canonical-boundary-lower-v1', input_bytes=2048))
    assert fresh.status(rival)['attempts'] == loser['attempts']
    assert fresh.result(rival)['canonical_job_id'] == first
    replay = fresh.offer('operator', 'rival-replay', [rival_item])['members'][0]
    assert replay['disposition'] == 'existing' and replay['job_id'] == first
    assert fresh.status(first)['profile'] == original_profile
    next_claim = client.claim(next_eligible, 0)
    assert next_claim.get('claimed') is True, next_claim
    progressed = client.capture(next_eligible, next_claim,
                                ControlledSource({'P': ('r1', b'next controlled input')}))
    assert progressed['state'] == 'queued'


def test_unresolved_equal_byte_copy_is_deferred_without_document_merge(database):
    client, original, initial = published_original(database)
    copy = dict(member('unresolved-copy'), source_id='copy-P', document_id='unresolved-copy',
                identity='unresolved')
    response = client.offer('operator', 'unresolved-copy', [copy])['members'][0]
    assert response['disposition'] == 'deferred'
    assert response['category'] == 'identity_unresolved'
    assert 'job_id' not in response
    assert_original_preserved(client, original, initial)
