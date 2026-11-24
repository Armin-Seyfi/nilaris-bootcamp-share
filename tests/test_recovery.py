"""Worker-control behavior observed through the agreed ingestion seam."""
from nilaris.admission import AdmissionProfile, Ingestion, migrate
from test_admission import member
import pytest


def accepted(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'offer', [member()])['members'][0]['job_id']
    return client, job_id


def test_queued_cancellation_survives_restart_and_prevents_claim(database):
    client, job_id = accepted(database)
    initial = client.status(job_id)
    cancelled = client.cancel(job_id, expected_revision=initial['revision'])
    assert cancelled['state'] == 'cancelled'
    assert Ingestion(database).status(job_id)['state'] == 'cancelled'
    assert client.claim(job_id, expected_revision=cancelled['revision'])['category'] == 'claim_conflict'


def test_one_current_claim_wins_and_cancel_reports_actual_running_state(database):
    from concurrent.futures import ThreadPoolExecutor
    client, job_id = accepted(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: client.claim(job_id, expected_revision=0), range(2)))
    assert sum(result.get('claimed', False) for result in outcomes) == 1
    status = Ingestion(database).status(job_id)
    assert status['state'] == 'running'
    assert status['current_claim']['stage'] == 'acquisition'
    assert status['current_claim']['generation'] == 1
    assert len(status['attempts']) == 1
    assert 'token' not in str(status)
    assert client.cancel(job_id, expected_revision=status['revision'])['category'] == 'cancel_conflict'
    assert client.status(job_id)['state'] == 'running'


def test_pinned_read_only_capture_queues_processing_and_retains_bytes_identity(database):
    from nilaris.execution import ControlledSource
    import hashlib
    client, job_id = accepted(database)
    claim = client.claim(job_id, expected_revision=0)
    outcome = client.capture(job_id, claim, ControlledSource({'P': ('r1', b'controlled input')}))
    assert outcome['state'] == 'queued'
    fresh = Ingestion(database).status(job_id)
    assert fresh['content_pin'] == hashlib.sha256(b'controlled input').hexdigest()
    assert fresh['coverage'] == dict(acquisition='captured', processing='not_started')
    assert fresh['counters']['captured_input_bytes'] == 16
    assert fresh['attempts'][0]['stopped'] is True
    assert client.capture(job_id, claim, ControlledSource({'P': ('r1', b'changed')}))['category'] == 'stale_commit'
    assert client.status(job_id)['content_pin'] == fresh['content_pin']


def literal_output(label='Flow rate'):
    from nilaris.results import Effect, StageOutput
    return StageOutput(
        outcome='completed',
        coverage=dict(inspected=['page:1'], unread=[], limits=['controlled-stage-only']),
        effects=(
            Effect('observation', 0, dict(id='o1', locator={'page': 1, 'text': 'Flow rate: 12 m3/h'},
                kind='property', original_content='Flow rate: 12 m3/h', value='12', unit='m3/h',
                source_type='text', presence='observed')),
            Effect('candidate', 0, dict(id='c1', label=label, value_type='decimal', unit='m3/h',
                domain='technical', observation_ids=['o1'], requirements='unspecified')),
        ))


def processing(database):
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    claim = client.claim(job_id, client.status(job_id)['revision'])
    return client, job_id, claim


def test_partial_and_reviewed_retry_expose_their_actual_next_actions(database):
    from dataclasses import replace
    client, job_id, claim = processing(database)
    partial = replace(literal_output(), outcome='partial',
                      coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]))
    status = client.publish(job_id, claim, partial)
    assert status['next_action'] == 'reviewed_retry'
    queued = client.retry(job_id, status['revision'], status['generation'], 'operator', 'finish coverage')
    assert queued['state'] == 'queued'
    assert Ingestion(database).status(job_id)['next_action'] == 'await_processing'


@pytest.mark.parametrize('counter,category', [('live_jobs', 'live_capacity'), ('waiting_jobs', 'waiting_capacity')])
def test_partial_retry_respects_waiting_and_live_capacity_without_changing_results(database, counter, category):
    from dataclasses import replace
    from nilaris.execution import ControlledSource
    migrate(database)
    profile = AdmissionProfile.l1().lowered_for_test('retry-one-capacity-v1', **{counter: 1})
    client = Ingestion(database, profile)
    job_id = client.offer('operator', 'first', [member()])['members'][0]['job_id']
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    claim = client.claim(job_id, client.status(job_id)['revision'])
    partial = replace(literal_output(), outcome='partial',
                      coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]))
    status = client.publish(job_id, claim, partial)
    before = client.result(job_id)
    item = dict(member('other'), document_id='other-document')
    other = client.offer('operator', 'other', [item])['members'][0]['job_id']
    refused = client.retry(job_id, status['revision'], status['generation'], 'operator', 'finish coverage')
    assert refused.get('category') == category
    assert client.status(job_id)['state'] == 'partial'
    assert client.result(job_id) == before
    client.cancel(other, 0)
    assert client.retry(job_id, status['revision'], status['generation'], 'operator', 'finish coverage')['state'] == 'queued'


def test_supervised_controlled_graph_publishes_after_process_accounting_and_allows_next_job(database):
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    status = client.run_controlled(job_id, client.status(job_id)['revision'], literal_output())
    assert status['state'] == 'completed'
    attempt = status['attempts'][-1]
    assert attempt['worker']['adapter'] == 'controlled-langgraph-langchain-v1'
    assert attempt['worker']['pid'] == attempt['worker']['pgid']
    assert attempt['accounting_complete'] is True
    assert attempt['completion_evidence'] == 'process_group_reaped'
    assert attempt['heartbeat_at'] is not None
    assert attempt['wall_seconds'] > 0
    assert attempt['cpu_seconds'] > 0
    assert attempt['cpu_seconds'] == 20
    assert 0 < attempt['measured_cpu_seconds'] <= attempt['cpu_seconds']
    assert attempt['cpu_accounting'] == 'conservative_full_allowance'
    assert 20 < status['usage']['cpu_seconds'] <= 60
    assert attempt['peak_rss_bytes'] > 0
    assert Ingestion(database).result(job_id)['publications'][0]['adapter'] == 'controlled-langgraph-langchain-v1'
    other = client.offer('operator', 'next', [dict(member('next'), document_id='next-doc')])['members'][0]['job_id']
    assert client.claim(other, 0)['claimed'] is True


def test_controlled_worker_deadline_stops_group_holds_and_releases_only_accounted_slot(database):
    import time
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    started = time.monotonic()
    status = client.run_controlled(job_id, client.status(job_id)['revision'], literal_output(),
                                  test_wall_seconds=1, test_delay_seconds=20)
    assert status['state'] == 'held'
    assert status['reason'] == 'attempt_wall_limit'
    assert time.monotonic() - started < 6
    attempt = status['attempts'][-1]
    assert attempt['stopped'] is True
    assert attempt['accounting_complete'] is True
    assert attempt['completion_evidence'] == 'process_group_reaped'
    assert attempt['worker']['limits']['version'] == 'controlled-worker-lower-test-v1'
    assert attempt['worker']['limits']['attempt_wall_seconds'] == 1
    assert attempt['cpu_seconds'] == 20
    assert attempt['measured_cpu_seconds'] <= attempt['cpu_seconds']
    assert attempt['cpu_accounting'] == 'conservative_full_allowance'
    assert client.result(job_id)['publications'] == []
    other = client.offer('operator', 'next', [dict(member('next'), document_id='next-doc')])['members'][0]['job_id']
    assert client.claim(other, 0)['claimed'] is True


def test_typed_initial_publication_and_effects_survive_restart_and_lost_ack_replay(database):
    client, job_id, claim = processing(database)
    published = client.publish(job_id, claim, literal_output())
    assert published['state'] == 'completed'
    fresh = Ingestion(database).result(job_id)
    assert fresh == client.result(job_id)
    initial = fresh['publications'][0]
    assert initial['kind'] == 'initial'
    assert initial['approved'] is False
    assert initial['context']['processing_contract'] == 'fixture-v1'
    assert initial['context']['reference_set'] == 'none'
    assert initial['coverage']['limits'] == ['controlled-stage-only']
    assert [effect['kind'] for effect in fresh['effects']] == ['observation', 'candidate']
    assert initial['candidates'][0]['label'] == 'Flow rate'
    assert initial['observations'][0]['original_content'] == 'Flow rate: 12 m3/h'
    assert client.publish(job_id, claim, literal_output())['state'] == 'completed'
    assert client.result(job_id) == fresh


def test_malformed_candidate_cannot_publish_or_change_initial_results(database):
    client, job_id, claim = processing(database)
    refused = client.publish(job_id, claim, literal_output(label=12))
    assert refused['category'] == 'malformed_output'
    assert client.result(job_id)['publications'] == []
    assert client.result(job_id)['effects'] == []


# Reuse the real subprocess adapter fixture at the same approved ingestion seam.
from test_command_service import command_process


def test_large_publication_is_fully_queryable_through_bounded_text_pages(command_process, database):
    from dataclasses import replace
    import json
    from nilaris.results import Effect
    text, start, _ = command_process
    client, job_id, claim = processing(database)
    output = literal_output()
    observation = dict(output.effects[0].payload, original_content='wide\\"\u2603' * 18000)
    output = replace(output, effects=(Effect('observation', 0, observation), output.effects[1]))
    assert client.publish(job_id, claim, output)['state'] == 'completed'
    start()
    status = text(dict(operation='status', job_id=job_id))
    assert status.get('state') == 'completed', 'large accepted result must not make status unavailable'
    assert len(json.dumps(status).encode()) <= 131072
    pages, cursor = [], None
    while True:
        command = dict(operation='result', job_id=job_id)
        if cursor is not None:
            command['cursor'] = cursor
        page = text(command)
        assert page.get('known') is True and 'fragment' in page, 'large result must offer a reconstructable page'
        assert len(json.dumps(page).encode()) <= 131072
        pages.append(page['fragment'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    result = json.loads(''.join(pages))
    assert result['publications'][0]['observations'][0]['original_content'] == 'wide\\"\u2603' * 18000
    assert result['publications'][0]['id'] == status['results'][0]['id']
    assert result['effects'][0]['payload']['original_content'] == observation['original_content']
    assert text(dict(operation='result', job_id=job_id))['fragment'] == pages[0]


def test_output_id_cannot_name_two_different_committed_effects(database):
    from dataclasses import replace
    from nilaris.results import Effect
    client, job_id, claim = processing(database)
    output = literal_output()
    duplicate = Effect('observation', 1, dict(output.effects[0].payload, value='13'))
    output = replace(output, effects=(*output.effects, duplicate))
    assert client.publish(job_id, claim, output).get('category') == 'malformed_output'
    assert client.result(job_id)['publications'] == []


def test_typed_output_rejects_nontext_unit_without_committing_effects(database):
    from dataclasses import replace
    from nilaris.results import Effect
    client, job_id, claim = processing(database)
    output = literal_output()
    invalid = Effect('observation', 0, dict(output.effects[0].payload, unit=12))
    assert client.publish(job_id, claim, replace(output, effects=(invalid, output.effects[1]))).get('category') == 'malformed_output'
    assert client.result(job_id)['effects'] == []


def test_output_metadata_and_links_have_declared_types(database):
    from dataclasses import replace
    from nilaris.results import Effect
    client, job_id, claim = processing(database)
    output = literal_output()
    for patch in ({'kind': 1}, {'source_type': []}, {'value': 12.0},
                  {'locator': {}}, {'locator': {'page': True}}, {'original_content': object()}):
        invalid = Effect('observation', 0, dict(output.effects[0].payload, **patch))
        assert client.publish(job_id, claim, replace(output, effects=(invalid, output.effects[1]))).get('category') == 'malformed_output'
    invalid = replace(output, coverage=dict(inspected=[1], unread=[], limits=[]))
    assert client.publish(job_id, claim, invalid).get('category') == 'malformed_output'
    candidate = Effect('candidate', 0, dict(output.effects[1].payload, observation_ids=['o1', 'o1']))
    assert client.publish(job_id, claim, replace(output, effects=(output.effects[0], candidate))).get('category') == 'malformed_output'
    assert client.result(job_id)['publications'] == []


def test_partial_retry_adds_missing_effects_without_changing_initial_publication(database):
    from dataclasses import replace
    from nilaris.results import Effect
    client, job_id, claim = processing(database)
    first = replace(literal_output(), outcome='partial',
                    coverage=dict(inspected=['page:1'], unread=['page:2'], limits=['controlled-stage-only']))
    status = client.publish(job_id, claim, first)
    initial = client.result(job_id)
    retried = client.retry(job_id, status['revision'], status['generation'], actor='operator', reason='inspect remaining page')
    assert retried['state'] == 'queued'
    assert retried['generation'] > status['generation']
    next_claim = client.claim(job_id, retried['revision'])
    second = literal_output()
    additional = Effect('observation', 1, dict(second.effects[0].payload, id='o2', locator={'page': 2}, value='13'))
    status = client.publish(job_id, next_claim, replace(second, effects=(*second.effects, additional)))
    assert status['state'] == 'completed'
    result = Ingestion(database).result(job_id)
    assert result['publications'][0] == initial['publications'][0]
    assert result['publications'][1]['kind'] == 'supplement'
    assert result['publications'][1]['initial_publication_id'] == initial['publications'][0]['id']
    assert [item['id'] for item in result['publications'][1]['observations']] == ['o2']
    assert result['publications'][1]['candidates'] == []
    assert result['effects'][0]['support_id'] == initial['effects'][0]['support_id']
    assert len(result['effects']) == 3
    assert status['recovery'][-1]['actor'] == 'operator'
    assert status['recovery'][-1]['reason'] == 'inspect remaining page'
    assert client.publish(job_id, next_claim, replace(second, effects=(*second.effects, additional)))['state'] == 'completed'
    assert client.result(job_id) == result


def test_serialized_result_allowance_is_cumulative_and_preserves_initial_on_refusal(database):
    from dataclasses import replace
    from nilaris.results import Effect
    client, job_id, claim = processing(database)
    output = literal_output()
    first_observation = Effect('observation', 0, dict(output.effects[0].payload, original_content='A' * 1100000))
    partial = replace(output, outcome='partial', coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]),
                      effects=(first_observation, output.effects[1]))
    status = client.publish(job_id, claim, partial)
    initial_page = client.result(job_id)
    queued = client.retry(job_id, status['revision'], status['generation'], 'operator', 'remaining scope')
    claim = client.claim(job_id, queued['revision'])
    extra = Effect('observation', 1, dict(output.effects[0].payload, id='o2', locator={'page': 2}, original_content='B' * 1100000))
    refusal = client.publish(job_id, claim, replace(output, effects=(extra,)))
    assert refusal.get('category') == 'resource_exhausted'
    status = client.status(job_id)
    assert status['state'] == 'held'
    assert status['reason'] == 'serialized_result_limit'
    assert len(status['results']) == 1
    assert len(status['effects']) == 2
    assert client.result(job_id) == initial_page
    assert 1100000 < status['usage']['serialized_result_bytes'] < 1200000
    assert 3300000 < status['usage']['retained_bytes'] <= 8388608
    assert status['reservation_bytes'] == 8388608


def test_retained_copy_limit_counts_replayed_request_and_preserves_input_and_history(database):
    from dataclasses import replace
    from nilaris.execution import ControlledSource
    from nilaris.results import Effect
    client, job_id = accepted(database)
    source = ControlledSource({'P': ('r1', b'x' * 1048576)})
    client.capture(job_id, client.claim(job_id, 0), source)
    claim = client.claim(job_id, client.status(job_id)['revision'])
    output = literal_output()
    observation = Effect('observation', 0, dict(output.effects[0].payload, original_content='A' * 1900000))
    output = replace(output, effects=(observation, output.effects[1]))
    partial = replace(output, outcome='partial', coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]))
    status = client.publish(job_id, claim, partial)
    before = client.result(job_id)
    assert status['state'] == 'partial'
    queued = client.retry(job_id, status['revision'], status['generation'], 'operator', 'finish coverage')
    refusal = client.publish(job_id, client.claim(job_id, queued['revision']), output)
    assert refusal.get('category') == 'resource_exhausted'
    assert refusal['reason'] == 'retained_byte_limit'
    after = client.status(job_id)
    assert after['usage']['serialized_result_bytes'] < 2097152
    assert 6700000 < after['usage']['retained_bytes'] <= 8388608
    assert after['counters']['captured_input_bytes'] == 1048576
    assert client.result(job_id) == before


def test_many_committed_effects_keep_text_status_bounded_and_refer_to_complete_results(command_process, database):
    from dataclasses import replace
    from nilaris.results import Effect
    text, start, _ = command_process
    client, job_id, claim = processing(database)
    output = literal_output()
    effects = tuple(Effect('observation', i, dict(output.effects[0].payload, id='o' + str(i), locator={'row': i + 1}))
                    for i in range(900))
    assert client.publish(job_id, claim, replace(output, effects=effects))['state'] == 'completed'
    start()
    status = text(dict(operation='status', job_id=job_id))
    assert status.get('state') == 'completed', 'many valid effects must retain queryable bounded status'
    assert status['effect_count'] == 900
    assert status['effects_truncated'] is True
    assert status['next_action'] == 'terminal'
    assert status['results'][0]['id'] in client.result(job_id)['publication_ids']


def test_claim_identity_must_match_its_current_generation_and_token(database):
    client, job_id, claim = processing(database)
    forged = dict(claim, attempt_id='different-attempt')
    assert client.publish(job_id, forged, literal_output()).get('category') == 'stale_commit'
    assert client.result(job_id)['publications'] == []


def read_all(client, job_id, page=None):
    import json
    page = page or client.result(job_id)
    if 'fragment' not in page:
        return page
    fragments = [page['fragment']]
    while page['next_cursor'] is not None:
        page = client.result(job_id, page['next_cursor'])
        fragments.append(page['fragment'])
    return json.loads(''.join(fragments))


def test_text_cursor_keeps_initial_snapshot_after_missing_only_supplement(command_process, database):
    from dataclasses import replace
    import json
    from nilaris.results import Effect
    text, start, _ = command_process
    client, job_id, claim = processing(database)
    output = literal_output()
    observation = Effect('observation', 0, dict(output.effects[0].payload, original_content='A' * 140000))
    partial = replace(output, outcome='partial', coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]),
                      effects=(observation, output.effects[1]))
    status = client.publish(job_id, claim, partial)
    initial = read_all(client, job_id)
    start()
    first = text(dict(operation='result', job_id=job_id))
    old_cursor = first['next_cursor']
    queued = client.retry(job_id, status['revision'], status['generation'], 'operator', 'finish page two')
    extra = Effect('observation', 1, dict(output.effects[0].payload, id='o2', locator={'page': 2}))
    assert client.publish(job_id, client.claim(job_id, queued['revision']), replace(output, effects=(extra,)))['state'] == 'completed'
    pieces, cursor = [first['fragment']], old_cursor
    while cursor is not None:
        page = text(dict(operation='result', job_id=job_id, cursor=cursor))
        assert len(json.dumps(page).encode()) <= 131072
        pieces.append(page['fragment'])
        cursor = page['next_cursor']
    assert json.loads(''.join(pieces)) == initial
    latest = text(dict(operation='result', job_id=job_id))
    assert len(latest['publication_ids']) == 2
    assert latest['publication_ids'][0] == first['publication_ids'][0]
    assert text(dict(operation='result', job_id=job_id, cursor='not-a-cursor'))['category'] == 'invalid_cursor'


def test_partial_retry_reference_errors_and_conflicting_replay_preserve_initial(database):
    from dataclasses import replace
    from nilaris.results import Effect
    client, job_id, claim = processing(database)
    output = literal_output()
    partial = replace(output, outcome='partial', coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]))
    status = client.publish(job_id, claim, partial)
    initial = client.result(job_id)
    queued = client.retry(job_id, status['revision'], status['generation'], 'operator', 'finish page two')
    next_claim = client.claim(job_id, queued['revision'])
    reused_id = Effect('observation', 1, dict(output.effects[0].payload, locator={'page': 2}))
    assert client.publish(job_id, next_claim, replace(output, effects=(reused_id,))).get('category') == 'malformed_output'
    broken_link = Effect('candidate', 1, dict(output.effects[1].payload, id='c2', observation_ids=['missing']))
    assert client.publish(job_id, next_claim, replace(output, effects=(broken_link,))).get('category') == 'malformed_output'
    assert client.result(job_id) == initial
    assert client.publish(job_id, next_claim, literal_output(label='different')).get('category') == 'effect_conflict'
    assert client.status(job_id)['state'] == 'held'
    assert client.result(job_id) == initial
    assert client.publish(job_id, next_claim, output).get('category') == 'stale_commit'


def test_public_status_keeps_lifecycle_and_effects_in_one_snapshot_during_publication(database):
    from concurrent.futures import ThreadPoolExecutor
    client, job_id, claim = processing(database)
    def read_snapshots():
        return [Ingestion(database).status(job_id) for _ in range(40)]
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(read_snapshots)
        assert client.publish(job_id, claim, literal_output())['state'] == 'completed'
        snapshots = reader.result()
    for status in snapshots:
        if status['state'] == 'completed':
            assert status['current_claim'] is None
            assert status['effect_count'] == 2
            assert len(status['results']) == 1
            assert status['reason'] == 'published_completed'
        else:
            assert status['state'] == 'running'
            assert status['current_claim']['generation'] == status['generation']
            assert status['effect_count'] == 0
            assert status['results'] == []
            assert status['reason'] == 'running'


def test_five_open_jobs_retain_held_capacity_until_accounted_atomic_transfer(database):
    from concurrent.futures import ThreadPoolExecutor
    import os
    import time
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    next_id = client.offer('operator', 'next-ready', [dict(member('next'), document_id='next')])['members'][0]['job_id']
    next_ready = client.capture(next_id, client.claim(next_id, 0), ControlledSource({'P': ('r1', b'next input')}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.run_controlled, job_id, client.status(job_id)['revision'],
                              literal_output(), test_wall_seconds=3, test_delay_seconds=20)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if status['state'] == 'running' and status['attempts'][-1]['worker']:
                break
            time.sleep(.02)
        assert status['state'] == 'running'
        transferred = client.status(next_id)
        assert transferred['state'] == next_ready['state'] == 'queued'
        assert transferred['revision'] == next_ready['revision'] + 1
        assert transferred['recovery'][-1]['reason'] == 'verified_capacity_transfer'
        assert transferred['content_pin'] == next_ready['content_pin']
        assert transferred['attempts'] == next_ready['attempts']
        assert client.claim(next_id, next_ready['revision'])['category'] == 'claim_conflict'
        next_ready = transferred
        waiting = client.offer('operator', 'four-waiting',
                               [dict(member(str(i)), document_id='P' + str(i)) for i in range(3)])
        ids = [next_id] + [item['job_id'] for item in waiting['members']]
        assert len(ids) == 4
        assert client.claim(next_id, next_ready['revision'])['category'] == 'active_capacity'
        held = running.result(timeout=10)
    assert held['state'] == 'held'
    assert held.get('active_capacity_reserved') is True
    assert held['admission']['waiting_jobs'] == 4
    assert held['admission']['live_jobs'] == 5
    assert held['admission']['active_capacity_reservations'] == 1
    assert held['attempts'][-1]['stopped'] is True
    assert held['attempts'][-1]['accounting_complete'] is True
    with pytest.raises(ProcessLookupError):
        os.killpg(held['attempts'][-1]['worker']['pgid'], 0)
    sixth = client.offer('operator', 'sixth', [dict(member('sixth'), document_id='sixth')])
    assert sixth['members'][0]['category'] in ('waiting_capacity', 'live_capacity')
    with ThreadPoolExecutor(max_workers=1) as pool:
        next_worker = pool.submit(client.run_controlled, next_id, next_ready['revision'],
                                  literal_output(), test_delay_seconds=1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            fresh = Ingestion(database).status(job_id)
            if not fresh['active_capacity_reserved']:
                break
            time.sleep(.02)
        assert fresh['state'] == 'held'
        assert fresh['active_capacity_reserved'] is False
        assert fresh['attempts'] == held['attempts']
        assert fresh['recovery'][-1]['reason'] == 'verified_capacity_transfer'
        assert fresh['admission']['waiting_jobs'] == 4
        assert fresh['admission']['live_jobs'] == 5
        assert fresh['admission']['active_capacity_reservations'] == 1
        assert client.claim(ids[1], 0)['category'] == 'active_capacity'
        assert client.claim(next_id, next_ready['revision'])['category'] == 'claim_conflict'
        assert client.status(job_id)['recovery'] == fresh['recovery']
        assert next_worker.result(timeout=10)['state'] == 'completed'
    assert client.status(job_id)['state'] == 'held'


def test_reviewed_reconcile_requeues_accounted_interruption_with_pinned_input_and_history(database):
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    held = client.run_controlled(job_id, client.status(job_id)['revision'], literal_output(),
                                 test_wall_seconds=1, test_delay_seconds=20)
    before = client.result(job_id)
    assert client.reconcile(job_id, held['revision'] - 1, held['generation'],
                            'operator', 'verified stopped worker')['category'] == 'reconcile_conflict'
    queued = client.reconcile(job_id, held['revision'], held['generation'],
                              'operator', 'verified stopped worker')
    assert queued['state'] == 'queued'
    assert queued['content_pin'] == held['content_pin']
    assert queued['usage']['cpu_seconds'] == held['usage']['cpu_seconds']
    assert queued['attempts'] == held['attempts']
    assert queued['recovery'][-1]['actor'] == 'operator'
    assert queued['recovery'][-1]['reason'] == 'verified stopped worker'
    assert client.result(job_id) == before
    completed = client.run_controlled(job_id, queued['revision'], literal_output())
    assert completed['state'] == 'completed'
    assert completed['usage']['cpu_seconds'] >= 40


def test_reviewed_revocation_fences_actual_old_worker_before_accounted_manual_recovery(database):
    reviewed_revocation_fences_worker(database)


def test_reviewed_revocation_preserves_worker_accounting_at_two_connection_limit(database, monkeypatch):
    reviewed_revocation_fences_worker(database, monkeypatch=monkeypatch)


def reviewed_revocation_fences_worker(database, *, monkeypatch=None):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    injected = []
    if monkeypatch:
        import threading
        import psycopg
        real_connect = psycopg.connect
        main_thread = threading.get_ident()
        depth = 0
        worker_denied = threading.Event()
        nested_closed = threading.Event()

        class MainConnection:
            def __init__(self, connection):
                self.connection = connection

            def __enter__(self):
                nonlocal depth
                connection = self.connection.__enter__()
                depth += 1
                if depth == 2 and not injected:
                    nested_closed.clear()
                    injected.append('nested_connection')
                    # Keep both real main-thread connections open until the
                    # actual concurrent worker hits the unchanged server cap.
                    if not worker_denied.wait(5):
                        self.__exit__(None, None, None)
                        pytest.fail('real worker did not reach the two-connection boundary')
                return connection

            def __exit__(self, *args):
                nonlocal depth
                depth -= 1
                result = self.connection.__exit__(*args)
                if depth < 2:
                    nested_closed.set()
                return result

        def overlap_connections(*args, **kwargs):
            if threading.get_ident() == main_thread:
                return MainConnection(real_connect(*args, **kwargs))
            try:
                return real_connect(*args, **kwargs)
            except psycopg.Error:
                if depth == 2:
                    worker_denied.set()
                    assert nested_closed.wait(5)
                raise

        monkeypatch.setattr(psycopg, 'connect', overlap_connections)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.run_controlled, job_id, client.status(job_id)['revision'],
                              literal_output(), test_delay_seconds=20)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if (status['state'] == 'running' and status['attempts'][-1]['worker'] and
                    status['attempts'][-1]['worker'].get('bootstrap_readiness') == 'input_dispatched'):
                break
            time.sleep(.02)
        revoked = client.reconcile(job_id, status['revision'], status['generation'],
                                   'operator', 'revoke interrupted generation')
        assert revoked.get('state') == 'held'
        assert revoked['generation'] > status['generation']
        assert revoked['active_capacity_reserved'] is True
        assert client.result(job_id)['publications'] == []
        assert client.reconcile(job_id, revoked['revision'], revoked['generation'],
                                'operator', 'worker still stopping')['category'] == 'recovery_held'
        stopped = running.result(timeout=10)
    assert stopped['state'] == 'held'
    assert stopped['attempts'][-1]['stopped'] is True, stopped['attempts'][-1]['worker'].get('failure')
    assert stopped['attempts'][-1]['worker'].get('failure') is None
    assert stopped['attempts'][-1]['reason'] == 'claim_lost'
    assert stopped['attempts'][-1]['completion_evidence'] == 'process_group_reaped'
    assert client.result(job_id)['publications'] == []
    queued = Ingestion(database).reconcile(job_id, stopped['revision'], stopped['generation'],
                                          'operator', 'verified old generation stopped')
    assert queued['state'] == 'queued'
    assert [r['reason'] for r in queued['recovery']] == [
        'revoke interrupted generation', 'verified old generation stopped']
    assert client.run_controlled(job_id, queued['revision'], literal_output())['state'] == 'completed'


@pytest.mark.parametrize('permission_denied_probe', [False, True], ids=['natural', 'probe_eperm'])
def test_killed_supervisor_restart_never_invents_accounting_or_releases_orphan_capacity(
        database, monkeypatch, permission_denied_probe):
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import time
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    root = Path(__file__).resolve().parents[1]
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(root / 'runtime/a1/with_overlay.py'), str(root / 'tests/recovery_process.py')],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker = None
    try:
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
                                               revision=client.status(job_id)['revision'])).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if (status['state'] == 'running' and status['attempts'][-1]['worker'] and
                    status['attempts'][-1]['worker'].get('bootstrap_readiness') == 'input_dispatched'):
                worker = status['attempts'][-1]['worker']
                break
            time.sleep(.05)
        assert worker is not None, 'controlled worker must be durably identified before crash'
        # Only the freshly created harness group is killed. The controlled worker
        # has its own group, whose identity is exposed by the public status.
        os.killpg(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=5)
        restarted = Ingestion(database, AdmissionProfile.l1())
        current = restarted.status(job_id)
        held = restarted.reconcile(job_id, current['revision'], current['generation'],
                                    'operator', 'supervisor killed before accounting')
        assert held['state'] == 'held'
        assert held['active_capacity_reserved'] is True
        assert held['usage']['accounting_unknown'] is True
        assert restarted.reconcile(job_id, held['revision'], held['generation'],
                                    'operator', 'review orphan')['category'] == 'recovery_held'
        other = restarted.offer('operator', 'other', [dict(member('other'), document_id='other')])['members'][0]['job_id']
        assert restarted.claim(other, 0)['category'] == 'active_capacity'
        assert restarted.result(job_id)['publications'] == []
        # Test harness terminates the exact observed group. Absence alone must
        # still not replace missing supervisor resource/effect accounting.
        identity = subprocess.run(['/bin/ps', '-p', str(worker['pid']), '-o', 'lstart='],
                                  capture_output=True, text=True, check=True).stdout
        assert identity.split() == worker['start'].split()
        os.killpg(worker['pgid'], signal.SIGKILL)
        denied_probes = []
        real_killpg = os.killpg

        def deny_owned_probe(pgid, signum):
            if pgid == worker['pgid'] and signum == 0:
                denied_probes.append(pgid)
                raise PermissionError(1, 'Operation not permitted')
            return real_killpg(pgid, signum)

        with monkeypatch.context() as probe:
            # Fault only the external zero-signal probe after the real,
            # identity-checked SIGKILL. All termination remains real.
            if permission_denied_probe:
                probe.setattr(os, 'killpg', deny_owned_probe)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.killpg(worker['pgid'], 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # EPERM is uncertainty, not evidence that this group is
                    # present or absent. Only a fresh successful inventory
                    # may independently prove absence; failures stay failures.
                    inventory = subprocess.run(
                        ['/bin/ps', '-axo', 'pid=,pgid='], capture_output=True,
                        text=True, timeout=1, check=True)
                    groups = []
                    for row in inventory.stdout.splitlines():
                        pid, pgid = map(int, row.split())
                        assert pid > 0 and pgid >= 0, 'invalid process inventory'
                        groups.append(pgid)
                    assert os.getpgrp() in groups, 'process inventory omitted the test harness'
                    assert time.monotonic() < deadline, 'process inventory exceeded absence deadline'
                    if worker['pgid'] not in groups:
                        break
                time.sleep(.05)
            else:
                pytest.fail('test-owned orphan group did not disappear')
        assert bool(denied_probes) is permission_denied_probe
        worker = None
        assert restarted.reconcile(job_id, held['revision'], held['generation'],
                                    'operator', 'old group absent accounting unknown')['category'] == 'recovery_held'
        after_absence = restarted.status(job_id)
        assert after_absence['active_capacity_reserved'] is True
        assert after_absence['usage']['accounting_unknown'] is True
        assert restarted.claim(other, 0)['category'] == 'active_capacity'
        assert restarted.result(job_id)['publications'] == []
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait(timeout=5)
        if worker:
            try:
                identity = subprocess.run(['/bin/ps', '-p', str(worker['pid']), '-o', 'lstart='],
                                          capture_output=True, text=True).stdout
                if identity.split() == worker['start'].split():
                    os.killpg(worker['pgid'], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_expired_supervisor_resumes_without_reviving_lease_or_publishing_old_output(database):
    expired_supervisor_resumes(database)


def test_expired_supervisor_accounts_completed_inventory_after_pause(database, tmp_path):
    expired_supervisor_resumes(database, pause_inventory_marker=tmp_path / 'inventory-paused')


def test_expired_supervisor_retains_unknown_without_fresh_inventory(database, tmp_path):
    expired_supervisor_resumes(database, pause_inventory_marker=tmp_path / 'inventory-paused',
                               inventory_unavailable=True)


def expired_supervisor_resumes(database, *, pause_inventory_marker=None, inventory_unavailable=False):
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import time
    from nilaris.execution import ControlledSource
    client, job_id = accepted(database)
    client.capture(job_id, client.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    root = Path(__file__).resolve().parents[1]
    supervisor = subprocess.Popen(
        ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris', 'python',
         str(root / 'runtime/a1/with_overlay.py'), str(root / 'tests/recovery_process.py')],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    worker_to_cleanup = None
    try:
        supervisor.stdin.write(json.dumps(dict(database=dict(database), job_id=job_id,
                                               revision=client.status(job_id)['revision'],
                                               pause_inventory_marker=str(pause_inventory_marker)
                                               if pause_inventory_marker else None,
                                               inventory_unavailable=inventory_unavailable)).encode())
        supervisor.stdin.close()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = client.status(job_id)
            if (status['state'] == 'running' and status['attempts'][-1]['worker'] and
                    status['attempts'][-1]['worker'].get('bootstrap_readiness') == 'input_dispatched'):
                break
            time.sleep(.05)
        assert status['state'] == 'running' and status['attempts'][-1]['worker']
        if inventory_unavailable:
            worker_to_cleanup = status['attempts'][-1]['worker']
        if pause_inventory_marker:
            # Arm only after public readiness, with no extra database query
            # nested inside the supervisor's process-command boundary.
            pause_inventory_marker.with_name(pause_inventory_marker.name + '.armed').touch()
            deadline = time.monotonic() + 5
            while not pause_inventory_marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert pause_inventory_marker.exists()
        else:
            os.killpg(supervisor.pid, signal.SIGSTOP)
        time.sleep(10.5)
        assert client.result(job_id)['publications'] == []
        os.killpg(supervisor.pid, signal.SIGCONT)
        supervisor.wait(timeout=10)
        held = Ingestion(database).status(job_id)
        assert held['state'] == 'held'
        if pause_inventory_marker:
            assert held['attempts'][-1]['worker']['failure'] == dict(
                phase='process_inventory', exception_type='TimeoutExpired')
        if inventory_unavailable:
            assert held['reason'] == 'worker_accounting_unknown'
            assert held['attempts'][-1]['accounting_complete'] is False
            assert held['attempts'][-1]['stopped'] is False
            assert held['usage']['accounting_unknown'] is True
            assert held['active_capacity_reserved'] is True
            assert client.result(job_id)['publications'] == []
            return
        assert held['reason'] == 'claim_lost', held['attempts'][-1]['worker'].get('failure')
        assert held['active_capacity_reserved'] is True
        assert held['attempts'][-1]['completion_evidence'] == 'process_group_reaped'
        assert held['attempts'][-1]['accounting_complete'] is True
        assert held['attempts'][-1]['stopped'] is True
        assert held['attempts'][-1]['wall_seconds'] >= 10.5
        assert held['attempts'][-1]['cpu_seconds'] >= 20
        assert held['generation'] > status['generation']
        assert client.result(job_id)['publications'] == []
        with pytest.raises(ProcessLookupError):
            os.killpg(held['attempts'][-1]['worker']['pgid'], 0)
    finally:
        if supervisor.poll() is None:
            os.killpg(supervisor.pid, signal.SIGCONT)
            os.killpg(supervisor.pid, signal.SIGTERM)
            supervisor.wait(timeout=5)
        if worker_to_cleanup:
            identity = subprocess.run(['/bin/ps', '-p', str(worker_to_cleanup['pid']), '-o', 'lstart='],
                                      capture_output=True, text=True).stdout
            if identity.split() == worker_to_cleanup['start'].split():
                os.killpg(worker_to_cleanup['pgid'], signal.SIGKILL)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(worker_to_cleanup['pgid'], 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.05)
                else:
                    pytest.fail('test-owned unaccounted group did not disappear')


def test_finite_acquisition_retains_capacity_across_queued_held_and_transfer(database):
    from nilaris.execution import ControlledSource

    client, acquired_id = accepted(database)
    acquisition_claim = client.claim(acquired_id, 0)
    waiting = client.offer('operator', 'four-waiting', [
        dict(member(str(index)), document_id=f'waiting-{index}') for index in range(4)
    ])
    assert waiting['totals'] == dict(offered=4, accepted=4, deferred=0, rejected=0)
    waiting_ids = [item['job_id'] for item in waiting['members']]

    running = client.status(acquired_id)
    assert running['state'] == 'running'
    assert running['current_claim']['stage'] == 'acquisition'
    assert running['admission']['waiting_jobs'] == 4
    assert running['admission']['live_jobs'] == 5
    assert running['admission']['active_capacity_reservations'] == 1

    queued = client.capture(acquired_id, acquisition_claim,
                            ControlledSource({'P': ('r1', b'captured input')}))
    assert queued['state'] == 'queued'
    assert queued['next_action'] == 'await_processing'
    assert queued['active_capacity_reserved'] is True
    assert queued['attempts'][-1]['stopped'] is True
    assert queued['attempts'][-1]['completion_evidence'] == 'finite_controlled_call'
    assert queued['admission']['waiting_jobs'] == 4
    assert queued['admission']['live_jobs'] == 5
    assert queued['admission']['active_capacity_reservations'] == 1
    assert queued['admission']['job_reservation_bytes'] == 5 * 8388608

    next_status = client.status(waiting_ids[0])
    next_claim = client.claim(waiting_ids[0], next_status['revision'])
    assert next_claim['claimed'] is True
    transferred = client.status(acquired_id)
    assert transferred['state'] == 'queued'
    assert transferred['active_capacity_reserved'] is False
    assert transferred['recovery'][-1]['reason'] == 'verified_capacity_transfer'
    assert transferred['admission']['waiting_jobs'] == 4
    assert transferred['admission']['live_jobs'] == 5
    assert transferred['admission']['active_capacity_reservations'] == 1
    assert client.claim(waiting_ids[1], client.status(waiting_ids[1])['revision'])['category'] == 'active_capacity'

    held = client.capture(waiting_ids[0], next_claim, ControlledSource({}))
    assert held['state'] == 'held'
    assert held['reason'] == 'source_version_unavailable'
    assert held['active_capacity_reserved'] is True
    assert held['attempts'][-1]['stopped'] is True
    assert held['attempts'][-1]['completion_evidence'] == 'finite_controlled_call'
    assert held['admission']['waiting_jobs'] == 4
    assert held['admission']['live_jobs'] == 5
    assert held['admission']['active_capacity_reservations'] == 1
    sixth_member = member('sixth')
    sixth_member['document_id'] = 'unique-sixth'
    sixth = client.offer('operator', 'sixth', [sixth_member])
    assert sixth['totals'] == dict(offered=1, accepted=0, deferred=1, rejected=0)
    assert sixth['members'][0]['category'] in ('waiting_capacity', 'live_capacity')

    last_status = client.status(waiting_ids[1])
    last_claim = client.claim(waiting_ids[1], last_status['revision'])
    assert last_claim['claimed'] is True
    transferred_held = client.status(waiting_ids[0])
    assert transferred_held['state'] == 'held'
    assert transferred_held['reason'] == 'source_version_unavailable'
    assert transferred_held['active_capacity_reserved'] is False
    assert transferred_held['recovery'][-1]['reason'] == 'verified_capacity_transfer'
    assert transferred_held['admission']['waiting_jobs'] == 4
    assert transferred_held['admission']['live_jobs'] == 5
    assert transferred_held['admission']['active_capacity_reservations'] == 1


def test_cancelled_finite_acquisition_releases_queued_reservation(database):
    from nilaris.execution import ControlledSource

    client, job_id = accepted(database)
    claim = client.claim(job_id, 0)
    queued = client.capture(job_id, claim,
                            ControlledSource({'P': ('r1', b'captured input')}))
    assert queued['state'] == 'queued'
    assert queued['active_capacity_reserved'] is True
    assert queued['attempts'][-1]['stopped'] is True
    assert queued['attempts'][-1]['completion_evidence'] == 'finite_controlled_call'

    cancelled = client.cancel(job_id, queued['revision'])
    assert cancelled['state'] == 'cancelled'
    assert cancelled['active_capacity_reserved'] is False
    assert cancelled['admission']['active_capacity_reservations'] == 0
    assert cancelled['admission']['waiting_jobs'] == 0
    assert cancelled['admission']['live_jobs'] == 0


def test_reviewed_acquisition_retry_returns_to_acquiring_with_same_source_version(database):
    from nilaris.execution import ControlledSource
    import hashlib

    client, job_id = accepted(database)
    first_claim = client.claim(job_id, 0)
    unavailable = client.capture(job_id, first_claim,
                                ControlledSource({'P': ('r2', b'newer bytes')}))
    assert unavailable['state'] == 'held'
    assert unavailable['reason'] == 'source_version_unavailable'
    assert unavailable['active_capacity_reserved'] is True
    assert unavailable['content_pin'] is None
    assert unavailable['counters']['captured_input_bytes'] == 0
    assert unavailable['attempts'][-1]['acquisition_requests'] == 1
    assert unavailable['counters']['acquisition_requests'] == 1
    assert unavailable['usage']['acquisition_requests'] == 1

    retried = client.retry(job_id, unavailable['revision'], unavailable['generation'],
                           'operator', 'source version is available')
    assert retried['state'] == 'acquiring'
    assert retried['reason'] == 'reviewed_acquisition_retry'
    assert retried['next_action'] == 'await_pinned_acquisition'
    assert retried['active_capacity_reserved'] is True
    assert retried['content_pin'] is None
    assert len(retried['attempts']) == 1
    assert retried['recovery'][-1]['actor'] == 'operator'
    assert retried['recovery'][-1]['reason'] == 'source version is available'

    second_claim = client.claim(job_id, retried['revision'])
    assert second_claim['claimed'] is True
    assert second_claim['stage'] == 'acquisition'
    assert client.status(job_id)['recovery'] == retried['recovery']
    captured = client.capture(job_id, second_claim,
                             ControlledSource({'P': ('r1', b'captured input')}))
    assert captured['state'] == 'queued'
    assert captured['input']['source_revision'] == unavailable['input']['source_revision']
    assert captured['content_pin'] == hashlib.sha256(b'captured input').hexdigest()
    assert captured['active_capacity_reserved'] is True
    assert len(captured['attempts']) == 2
    assert all(attempt['stage'] == 'acquisition' for attempt in captured['attempts'])
    assert [attempt['acquisition_requests'] for attempt in captured['attempts']] == [1, 1]
    assert captured['counters']['acquisition_requests'] == 2
    assert captured['usage']['acquisition_requests'] == 2
    assert captured['recovery'] == retried['recovery']


def test_acquisition_request_ceiling_survives_failed_reads_and_reviewed_retry(database):
    from nilaris.execution import ControlledSource

    class FailedRead(ControlledSource):
        def read(self, source_id, revision, limit):
            raise RuntimeError('controlled source read failed')

    class ForbiddenRead(ControlledSource):
        def read(self, source_id, revision, limit):
            pytest.fail('acquisition budget must refuse before another source read')

    client, job_id = accepted(database)
    first_claim = client.claim(job_id, 0)
    for _ in range(6):
        with pytest.raises(RuntimeError, match='^controlled source read failed$'):
            client.capture(job_id, first_claim, FailedRead({}))
    fresh = Ingestion(database).status(job_id)
    assert fresh['counters']['acquisition_requests'] == 6
    assert fresh['attempts'][0]['acquisition_requests'] == 6

    unavailable = client.capture(job_id, first_claim, ControlledSource({}))
    assert unavailable['state'] == 'held'
    assert unavailable['usage']['acquisition_requests'] == 7
    retried = client.retry(job_id, unavailable['revision'], unavailable['generation'],
                           'operator', 'retry exact offered version')
    second_claim = client.claim(job_id, retried['revision'])
    assert second_claim['claimed'] is True
    for _ in range(5):
        with pytest.raises(RuntimeError, match='^controlled source read failed$'):
            client.capture(job_id, second_claim, FailedRead({}))

    fresh = Ingestion(database).status(job_id)
    assert len(fresh['attempts']) == 2
    assert [attempt['acquisition_requests'] for attempt in fresh['attempts']] == [7, 5]
    assert fresh['counters']['acquisition_requests'] == 12
    assert fresh['usage']['acquisition_requests'] == 12
    assert fresh['state'] == 'held'
    assert fresh['reason'] == 'acquisition_request_limit'
    assert fresh['current_claim'] is None
    assert fresh['active_capacity_reserved'] is True
    assert fresh['attempts'][-1]['stopped'] is True
    assert fresh['attempts'][-1]['worker'] is None
    assert fresh['attempts'][-1]['completion_evidence'] == 'finite_controlled_call'
    assert fresh['usage']['accounting_unknown'] is False
    refused = client.capture(job_id, second_claim, ForbiddenRead({}))
    assert refused['category'] == 'resource_exhausted'
    after = Ingestion(database).status(job_id)
    assert after['counters']['acquisition_requests'] == 12
    assert after['usage']['acquisition_requests'] == 12
    assert after['content_pin'] is None
    assert after['counters']['captured_input_bytes'] == 0
    assert after['state'] == 'held'
    assert after['reason'] == 'acquisition_request_limit'
    assert after['active_capacity_reserved'] is True
    assert client.retry(job_id, after['revision'], after['generation'],
                        'operator', 'cannot reset exhausted budget')['category'] == 'retry_conflict'
    unrelated_claim = dict(second_claim, token='not-the-exhausted-claim')
    assert client.capture(job_id, unrelated_claim, ForbiddenRead({}))['category'] == 'stale_commit'


def test_reviewed_acquisition_retry_reserves_capacity_from_four_waiting_jobs(database):
    from nilaris.execution import ControlledSource

    client, job_id = accepted(database)
    held = client.capture(job_id, client.claim(job_id, 0), ControlledSource({}))
    assert held['state'] == 'held'
    assert held['active_capacity_reserved'] is True
    waiting_ids = []
    for index in range(4):
        item = dict(member(str(index)), document_id=f'retry-waiting-{index}')
        offered = client.offer('operator', f'retry-waiting-{index}', [item])
        assert offered['totals'] == dict(offered=1, accepted=1, deferred=0, rejected=0)
        waiting_ids.append(offered['members'][0]['job_id'])

    successor = waiting_ids[0]
    successor_claim = client.claim(successor, client.status(successor)['revision'])
    assert successor_claim['claimed'] is True
    transferred = client.status(job_id)
    assert transferred['state'] == 'held'
    assert transferred['active_capacity_reserved'] is False
    assert transferred['recovery'][-1]['reason'] == 'verified_capacity_transfer'
    queued = client.capture(successor, successor_claim,
                            ControlledSource({'P': ('r1', b'successor input')}))
    assert queued['state'] == 'queued'
    cancelled = client.cancel(successor, queued['revision'])
    assert cancelled['state'] == 'cancelled'
    before = Ingestion(database).status(job_id)
    assert before['admission']['waiting_jobs'] == 4
    assert before['admission']['live_jobs'] == 4
    assert before['admission']['active_capacity_reservations'] == 0

    retried = client.retry(job_id, before['revision'], before['generation'],
                           'operator', 'retry offered version after finite transfer')
    assert retried['state'] == 'acquiring'
    assert retried['active_capacity_reserved'] is True
    assert retried['admission']['waiting_jobs'] == 3
    assert retried['admission']['live_jobs'] == 4
    assert retried['admission']['active_capacity_reservations'] == 1
    assert retried['input']['source_revision'] == held['input']['source_revision']
    assert retried['counters']['acquisition_requests'] == 1
    assert client.claim(job_id, retried['revision'])['claimed'] is True
