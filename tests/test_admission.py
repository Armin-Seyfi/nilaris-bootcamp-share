from nilaris.admission import AdmissionProfile, Ingestion, migrate


def member(key='m1'):
    return dict(member_key=key, connector='synthetic', source_id='P', source_revision='r1',
                document_id='doc-P', case_id='case-A', membership_revision='manifest-1',
                product_type='pump', processing_contract='fixture-v1', reference_set='none',
                selection_policy='none', intent_id='ordinary', input_bytes=1024,
                media_type='application/pdf', identity='resolved')


def test_accepted_job_and_receipt_survive_new_client(database):
    migrate(database)
    first = Ingestion(database, AdmissionProfile.l1()).offer('operator', 'c1', [member()])
    assert first['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    job_id = first['members'][0]['job_id']
    fresh = Ingestion(database)
    assert fresh.resolve('operator', 'c1') == first
    job = fresh.status(job_id)
    assert job['state'] == 'acquiring'
    assert job['input']['source_revision'] == 'r1'
    assert job['next_action'] == 'await_pinned_acquisition'
    assert job['reservation_bytes'] == 8388608


def test_concurrent_same_command_replays_and_changed_content_conflicts(database):
    from concurrent.futures import ThreadPoolExecutor
    migrate(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: Ingestion(database, AdmissionProfile.l1()).offer('operator', 'c1', [member()]), range(2)))
    assert results[0] == results[1]
    changed = member()
    changed['source_revision'] = 'r2'
    assert Ingestion(database, AdmissionProfile.l1()).offer('operator', 'c1', [changed]) == dict(
        known=True, category='command_conflict', reason='command key already has different content',
        affected=dict(namespace='operator', command_id='c1', member_keys=['m1']), coverage='unknown',
        next_action='use_original_content_or_new_command')
    assert Ingestion(database).resolve('operator', 'c1') == results[0]


def test_missing_profile_refuses_offer_without_persisting_acceptance(database):
    migrate(database)
    result = Ingestion(database).offer('operator', 'no-profile', [member()])
    assert result == dict(known=True, category='configuration_unavailable',
                         affected=dict(namespace='operator', command_id='no-profile', member_keys=['m1']), coverage='unknown',
                         reason='explicit versioned admission profile required',
                         next_action='configure_approved_profile')
    assert Ingestion(database).resolve('operator', 'no-profile')['known'] is False


def test_explicit_versioned_l1_profile_accepts_and_refuses_raised_test_limits(database):
    from nilaris.admission import AdmissionProfile
    import pytest
    migrate(database)
    profile = AdmissionProfile.l1()
    result = Ingestion(database, profile=profile).offer('operator', 'profiled', [member()])
    assert result['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    job = Ingestion(database).status(result['members'][0]['job_id'])
    assert job['profile']['version'] == 'L1-v1'
    assert job['profile']['waiting_jobs'] == 4
    with pytest.raises(ValueError, match='lower-only'):
        profile.lowered_for_test('raised-v1', waiting_jobs=5)
    with pytest.raises(ValueError, match='version'):
        profile.lowered_for_test('', waiting_jobs=1)
    assert Ingestion(database, profile={'version': 'unverified'}).offer(
        'operator', 'invalid-profile', [member()])['category'] == 'configuration_unavailable'


def test_concurrent_offers_respect_waiting_capacity_and_expose_reservations(database):
    from concurrent.futures import ThreadPoolExecutor
    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('one-waiting-v1', waiting_jobs=1)
    def offer(index):
        item = member(str(index))
        item['document_id'] = f'doc-{index}'
        return Ingestion(database, profile).offer('operator', str(index), [item])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(offer, [1, 2]))
    assert sorted(result['totals']['accepted'] for result in results) == [0, 1]
    refused = next(result for result in results if result['totals']['deferred'])
    assert refused['members'][0]['reason'] == 'waiting_capacity'
    assert refused['members'][0]['next_action'] == 'retry_new_command_when_capacity_available'
    accepted = next(result for result in results if result['totals']['accepted'])
    job = Ingestion(database).status(accepted['members'][0]['job_id'])
    assert job['admission']['waiting_jobs'] == 1
    assert job['admission']['live_jobs'] == 1
    assert job['admission']['job_reservation_bytes'] == 8388608
    assert job['admission']['receipt_reservation_bytes'] == 270336
    assert job['admission']['terminal_headroom_bytes'] == 8388608
    assert job['admission']['oldest_waiting_seconds'] >= 0


def test_exact_byte_boundary_preserves_headroom_and_bounded_receipts(database):
    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('exact-byte-v1', logical_bytes=16912384)
    client = Ingestion(database, profile)
    first = client.offer('operator', 'first', [member()])
    assert first['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    other = member('m2')
    other['document_id'] = 'doc-Q'
    refused = client.offer('operator', 'second', [other])
    assert refused['totals'] == dict(offered=1, accepted=0, deferred=1, rejected=0)
    assert refused['members'][0]['reason'] == 'byte_capacity'
    assert refused['receipt_stored'] is False
    assert client.resolve('operator', 'second')['known'] is False
    assert client.resolve('operator', 'first') == first
    status = client.status(first['members'][0]['job_id'])
    assert status['admission']['job_reservation_bytes'] == 8388608
    assert status['admission']['receipt_reservation_bytes'] == 135168


def test_racing_offers_cannot_borrow_terminal_headroom_at_byte_boundary(database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('racing-byte-v1', logical_bytes=16912384)
    ready = Barrier(2)
    def offer(index):
        item = member(str(index))
        item['document_id'] = f'doc-{index}'
        ready.wait(timeout=5)
        return Ingestion(database, profile).offer('operator', str(index), [item])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(offer, [1, 2]))
    assert sorted(result['totals']['accepted'] for result in results) == [0, 1]
    refused = next(result for result in results if result['totals']['deferred'])
    assert refused['members'][0]['reason'] == 'byte_capacity'
    assert refused['receipt_stored'] is False
    accepted = next(result for result in results if result['totals']['accepted'])
    status = Ingestion(database).status(accepted['members'][0]['job_id'])
    assert status['admission']['waiting_jobs'] == 1
    assert status['admission']['job_reservation_bytes'] == 8388608
    assert status['admission']['receipt_reservation_bytes'] == 135168
    assert status['admission']['terminal_headroom_bytes'] == 8388608


def test_invalid_or_unresolved_members_are_not_accepted(database):
    migrate(database)
    unresolved = member('unresolved')
    unresolved['identity'] = 'unresolved'
    oversized = member('oversized')
    oversized['input_bytes'] = 1048577
    unsupported = member('unsupported')
    unsupported['media_type'] = 'application/zip'
    missing_pin = member('missing-pin')
    del missing_pin['source_revision']
    result = Ingestion(database, AdmissionProfile.l1()).offer(
        'operator', 'mixed', [member(), unresolved, oversized, unsupported, missing_pin])
    assert result['known'], result
    assert result['totals'] == dict(offered=5, accepted=1, deferred=1, rejected=3)
    assert [entry.get('reason') for entry in result['members'][1:]] == [
        'identity_unresolved', 'input_limit', 'unsupported_input', 'invalid_identity']
    assert all(entry['next_action'] for entry in result['members'])


def test_batch_metadata_is_bounded_before_any_acceptance(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    too_many = client.offer('operator', 'too-many', [member(str(n)) for n in range(9)])
    assert too_many['category'] == 'batch_limit'
    assert too_many['known'] is True
    large = member()
    large['path'] = 'x' * 65536
    assert client.offer('operator', 'too-large', [large])['category'] == 'metadata_limit'
    assert client.resolve('operator', 'too-many')['known'] is False
    assert client.resolve('operator', 'too-large')['known'] is False
    assert client.offer('operator', 'valid', [member()])['totals']['accepted'] == 1


def test_semantic_duplicate_precedes_full_queue_and_retains_original_profile(database):
    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('dedup-one-v1', waiting_jobs=1)
    first = Ingestion(database, profile).offer('operator', 'first', [member()])
    duplicate = member('redelivered')
    duplicate.update(path='renamed.pdf', source_id='mapped-copy', transport_timestamp='later')
    newer = profile.lowered_for_test('dedup-smaller-v2', metadata_bytes=32768)
    repeated = Ingestion(database, newer).offer('other-operator', 'new-command', [duplicate])
    job_id = first['members'][0]['job_id']
    assert repeated['members'] == [dict(member_key='redelivered', disposition='existing',
                                      job_id=job_id, reason='canonical_intent', next_action='query_job')]
    assert repeated['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
    status = Ingestion(database).status(job_id)
    assert status['profile']['version'] == 'dedup-one-v1'
    assert status['input']['source_id'] == 'P'
    assert status['admission']['waiting_jobs'] == 1
    assert status['admission']['job_reservation_bytes'] == 8388608
    changed = member('changed-pin')
    changed['source_revision'] = 'r2'
    assert Ingestion(database, profile).offer('operator', 'changed-pin', [changed])[
        'members'][0]['reason'] == 'waiting_capacity'


def test_status_distinguishes_not_found_and_pending_acquisition_coverage(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    assert client.status('absent') == dict(
        known=True, category='not_found', affected=dict(job_id='absent'),
        coverage='unknown',
        reason='job not found', next_action='check_job_identity')
    receipt = client.offer('operator', 'pending', [member()])
    status = client.status(receipt['members'][0]['job_id'])
    assert status['known'] is True
    assert status['reason'] == 'pending_acquisition'
    assert status['coverage'] == dict(acquisition='pending', processing='not_started')
    assert status['attempts'] == []
    assert status['current_claim'] is None
    assert status['results'] == []
    assert status['effects'] == []
    assert status['counters'] == dict(attempts=0, captured_input_bytes=0, acquisition_requests=0)


def test_admission_profile_cannot_underreserve_approved_job_components():
    import pytest
    with pytest.raises(ValueError, match='job reservation'):
        AdmissionProfile.l1().lowered_for_test('underreserved-v1', job_bytes=1048576)


def test_existing_job_uses_original_input_limit_under_smaller_profile(database):
    migrate(database)
    original = AdmissionProfile.l1()
    first = Ingestion(database, original).offer('operator', 'original', [member()])
    assert first['known'], {'category': first.get('category')}
    smaller = original.lowered_for_test('input-512-v1', input_bytes=512)
    repeated = Ingestion(database, smaller).offer('operator', 'smaller', [member()])
    assert repeated['known'], {'category': repeated.get('category')}
    assert repeated['members'][0]['disposition'] == 'existing'
    assert repeated['members'][0]['job_id'] == first['members'][0]['job_id']
    changed = member('new')
    changed['document_id'] = 'new-document'
    refused = Ingestion(database, smaller).offer('operator', 'new', [changed])
    assert refused['known'], {'category': refused.get('category')}
    assert refused['members'][0]['reason'] == 'input_limit'


def test_direct_profile_constructor_keeps_l1_version_tied_to_exact_settings():
    import pytest
    with pytest.raises(ValueError, match='distinct test profile version'):
        AdmissionProfile(version='L1-v1', waiting_jobs=1)
    assert AdmissionProfile(version='direct-one-v1', waiting_jobs=1).waiting_jobs == 1


def test_unapproved_reference_context_cannot_be_admitted(database):
    migrate(database)
    reference = member('reference')
    reference['reference_set'] = 'unapproved-reference'
    selection = member('selection')
    selection['selection_policy'] = 'unapproved-policy'
    result = Ingestion(database, AdmissionProfile.l1()).offer(
        'operator', 'unapproved-context', [reference, selection])
    assert result['totals'] == dict(offered=2, accepted=0, deferred=0, rejected=2)
    assert [item['reason'] for item in result['members']] == ['unsupported_context', 'unsupported_context']


def test_unapproved_processing_and_unlinked_intent_cannot_create_jobs(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    original = client.offer('operator', 'original', [member()])
    processing = member('processing')
    processing['processing_contract'] = 'unknown-version'
    intent = member('intent')
    intent['intent_id'] = 'unlinked-reprocessing'
    refused = client.offer('operator', 'unsupported', [processing, intent])
    assert refused['totals'] == dict(offered=2, accepted=0, deferred=0, rejected=2)
    assert [item['reason'] for item in refused['members']] == ['unsupported_context', 'unsupported_context']
    status = client.status(original['members'][0]['job_id'])
    assert status['admission']['waiting_jobs'] == 1
    assert status['admission']['job_reservation_bytes'] == 8388608


def test_known_offer_errors_explain_affected_identity_and_coverage(database):
    migrate(database)
    unconfigured = Ingestion(database).offer('operator', 'missing', [member()])
    assert unconfigured['affected'] == dict(namespace='operator', command_id='missing', member_keys=['m1'])
    assert unconfigured['coverage'] == 'unknown'
    client = Ingestion(database, AdmissionProfile.l1())
    invalid = member()
    invalid['processing_contract'] = 'unknown'
    refused = client.offer('operator', 'invalid', [invalid])['members'][0]
    assert refused['category'] == 'unsupported_context'
    assert refused['affected'] == dict(member_key='m1')
    assert refused['coverage'] == dict(acquisition='not_started', processing='not_started')
    assert refused['message'] == 'Only the approved ordinary synthetic processing context is supported.'


def test_absent_resolution_preserves_unknown_coverage_and_identity(database):
    migrate(database)
    client = Ingestion(database)
    assert client.resolve('operator', 'absent') == dict(
        known=False, category='unknown', affected=dict(namespace='operator', command_id='absent'),
        coverage='unknown', reason='No durable offer receipt is available.', next_action='retry_same_key')
    assert client.status('absent')['coverage'] == 'unknown'


def test_expanded_errors_remain_bounded_and_receipts_conservatively_charged(database):
    import json
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    accepted = client.offer('operator', 'accepted', [member()])
    invalid = member('k' * 50000)
    invalid['processing_contract'] = 'unsupported'
    result = client.offer('operator', 'large', [invalid])
    assert result['members'][0]['category'] == 'unsupported_context'
    assert len(json.dumps(result).encode()) <= 131072
    assert client.resolve('operator', 'large') == result
    usage = client.status(accepted['members'][0]['job_id'])['admission']
    retained_bytes = len(json.dumps([invalid]).encode()) + len(json.dumps(result).encode())
    assert usage['receipt_reservation_bytes'] >= 135168 + retained_bytes + 4096
    too_large = member('k' * 62000)
    too_large['processing_contract'] = 'unsupported'
    refused = client.offer('operator', 'too-large-reply', [too_large])
    assert refused['category'] == 'metadata_limit'
    assert len(json.dumps(refused).encode()) <= 131072
    assert client.resolve('operator', 'too-large-reply')['known'] is False


def test_unvalidated_error_identities_cannot_exceed_metadata_envelope():
    import json
    result = Ingestion({}).offer('operator', 'unconfigured', [member('k' * 200000)])
    assert result['category'] == 'configuration_unavailable'
    assert result['coverage'] == 'unknown'
    assert len(json.dumps(result).encode()) <= 131072
