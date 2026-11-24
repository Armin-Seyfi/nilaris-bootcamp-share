"""Content adoption and reviewed intent history through the ingestion seam."""
from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_admission import member
from test_recovery import literal_output
from test_command_service import command_process  # noqa: F401
import pytest


def test_equal_bytes_new_source_revision_adopts_canonical_without_processing(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    original = client.offer('operator', 'original', [member()])['members'][0]['job_id']
    client.capture(original, client.claim(original, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    client.publish(original, client.claim(original, client.status(original)['revision']), literal_output())
    initial = client.result(original)
    changed_revision = dict(member('revision-2'), source_revision='r2')
    lowered = AdmissionProfile.l1().lowered_for_test('adoption-lower-v1', input_bytes=2048)
    other_client = Ingestion(database, lowered)
    acquired = other_client.offer('operator', 'new-revision', [changed_revision])['members'][0]['job_id']
    adopted = other_client.capture(acquired, other_client.claim(acquired, 0),
                                  ControlledSource({'P': ('r2', b'controlled input')}))
    assert adopted['state'] == 'completed'
    assert adopted['disposition'] == 'duplicate-of'
    assert adopted['canonical_job_id'] == original
    assert adopted['content_pin'] == client.status(original)['content_pin']
    assert adopted['input']['source_revision'] == 'r2'
    assert [attempt['stage'] for attempt in adopted['attempts']] == ['acquisition']
    assert adopted['effect_count'] == 0 and adopted['results'] == []
    assert adopted['active_capacity_reserved'] is False
    assert adopted['reservation_bytes'] >= adopted['usage']['retained_bytes'] > 0
    fresh = Ingestion(database, lowered)
    assert fresh.status(acquired)['canonical_job_id'] == original
    assert fresh.result(acquired)['canonical_job_id'] == original
    replay = fresh.offer('operator', 'later-delivery', [changed_revision])['members'][0]
    assert replay['disposition'] == 'existing' and replay['job_id'] == original
    assert fresh.status(original)['profile']['version'] == 'L1-v1'
    assert fresh.result(original) == initial


def test_text_reprocess_recovers_same_linked_job_after_service_restart(command_process, database):
    from test_command_service import stop
    client, start, _ = command_process
    process = start()
    worker = Ingestion(database, AdmissionProfile.l1())
    original = worker.offer('operator', 'original', [member()])['members'][0]['job_id']
    worker.capture(original, worker.claim(original, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    status = worker.publish(original, worker.claim(original, worker.status(original)['revision']), literal_output())
    command = dict(operation='reprocess', namespace='operator', command_id='reviewed-text',
                   job_id=original, expected_revision=status['revision'],
                   expected_generation=status['generation'], actor='reviewer', reason='reviewed same contract')
    outcome = client(command)
    assert outcome.get('disposition') == 'accepted', outcome
    stop(process)
    start()
    assert client(dict(operation='resolve', namespace='operator', command_id='reviewed-text')) == outcome
    assert client(command) == outcome
    assert client(dict(operation='status', job_id=outcome['job_id']))['lineage']['original_job_id'] == original


def test_reviewed_reprocess_replays_linked_intent_and_preserves_initial_support(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    original = client.offer('operator', 'original', [member()])['members'][0]['job_id']
    client.capture(original, client.claim(original, 0),
                   ControlledSource({'P': ('r1', b'controlled input')}))
    status = client.publish(original, client.claim(original, client.status(original)['revision']), literal_output())
    initial = client.result(original)
    arguments = dict(namespace='operator', command_id='reviewed-reprocess', job_id=original,
                     expected_revision=status['revision'], expected_generation=status['generation'],
                     actor='reviewer', reason='verify the same controlled contract')
    outcome = client.reprocess(**arguments)
    new_id = outcome['job_id']
    assert new_id != original
    linked = client.status(new_id)
    assert linked['state'] == 'queued' and linked['attempts'] == []
    assert linked['input']['intent_id'] != 'ordinary'
    assert linked['content_pin'] == status['content_pin']
    assert linked['lineage'] == dict(original_job_id=original,
        original_initial_publication_id=initial['publications'][0]['id'],
        actor='reviewer', reason=arguments['reason'])
    fresh = Ingestion(database, AdmissionProfile.l1())
    assert fresh.reprocess(**arguments) == outcome
    assert fresh.resolve('operator', 'reviewed-reprocess') == outcome
    assert fresh.reprocess(**dict(arguments, reason='changed content'))['category'] == 'command_conflict'
    assert fresh.reprocess(**dict(arguments, command_id='stale', expected_revision=0))['category'] == 'reprocess_conflict'
    fresh.publish(new_id, fresh.claim(new_id, linked['revision']), literal_output())
    newer = fresh.result(new_id)
    assert newer['lineage'] == linked['lineage']
    assert newer['publications'][0]['id'] != initial['publications'][0]['id']
    assert newer['effects'][0]['support_id'] == initial['effects'][0]['support_id']
    assert fresh.result(original) == initial


def test_text_explicit_hold_and_safe_retry_preserve_acquisition_stage_and_allowance(command_process, database):
    client, start, _ = command_process
    start()
    worker = Ingestion(database, AdmissionProfile.l1())
    job_id = worker.offer('operator', 'unavailable', [member()])['members'][0]['job_id']
    held = worker.capture(job_id, worker.claim(job_id, 0), ControlledSource({}))
    command = dict(operation='reconcile', job_id=job_id,
                   expected_revision=held['revision'], expected_generation=held['generation'],
                   actor='operator', reason='review exact offered revision', disposition='hold')
    reviewed = client(command)
    assert reviewed.get('state') == 'held', reviewed
    assert reviewed['generation'] > held['generation']
    assert reviewed['recovery'][-1]['reason'] == command['reason']
    retried = client(dict(command, expected_revision=reviewed['revision'],
                          expected_generation=reviewed['generation'], disposition='safe-retry'))
    assert retried['state'] == 'acquiring'
    assert retried['attempts'] == held['attempts']
    assert retried['counters']['acquisition_requests'] == 1
    assert retried['content_pin'] is None
    captured = worker.capture(job_id, worker.claim(job_id, retried['revision']),
                              ControlledSource({'P': ('r1', b'controlled input')}))
    assert captured['state'] == 'queued'
    assert captured['counters']['acquisition_requests'] == 2
    assert [attempt['stage'] for attempt in captured['attempts']] == ['acquisition', 'acquisition']


@pytest.mark.parametrize('disposition,state', [('fail', 'failed'), ('quarantine', 'quarantined')])
def test_text_terminal_disposition_preserves_partial_publication_after_accounted_stop(command_process, database, disposition, state):
    from dataclasses import replace
    client, start, _ = command_process
    start()
    worker = Ingestion(database, AdmissionProfile.l1())
    job_id = worker.offer('operator', 'partial', [member()])['members'][0]['job_id']
    worker.capture(job_id, worker.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    partial = replace(literal_output(), outcome='partial',
                      coverage=dict(inspected=['page:1'], unread=['page:2'], limits=[]))
    published = worker.publish(job_id, worker.claim(job_id, worker.status(job_id)['revision']), partial)
    initial = worker.result(job_id)
    queued = worker.retry(job_id, published['revision'], published['generation'], 'operator', 'inspect remaining page')
    held = worker.run_controlled(job_id, queued['revision'], literal_output(),
                                 test_wall_seconds=1, test_delay_seconds=20)
    assert held['state'] == 'held' and held['attempts'][-1]['accounting_complete'] is True, {
        'reason': held.get('reason'), 'worker': held['attempts'][-1].get('worker'),
        'usage': held.get('usage')}
    command = dict(operation='reconcile', job_id=job_id, expected_revision=held['revision'],
                   expected_generation=held['generation'], actor='operator',
                   reason='reviewed bounded processing interruption', disposition=disposition)
    terminal = client(command)
    assert terminal.get('state') == state, terminal
    assert terminal['active_capacity_reserved'] is False
    assert terminal['usage']['cpu_seconds'] == held['usage']['cpu_seconds']
    assert terminal['coverage'] == held['coverage']
    assert terminal['reservation_bytes'] >= terminal['usage']['retained_bytes']
    assert worker.result(job_id) == initial
    assert Ingestion(database).status(job_id)['state'] == state


def test_held_finite_acquisition_without_completion_proof_reports_unknown_accounting(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'interrupted', [member()])['members'][0]['job_id']
    client.claim(job_id, 0)
    running = client.status(job_id)
    held = client.reconcile(job_id, running['revision'], running['generation'],
                            'operator', 'in-process acquisition caller disappeared', disposition='hold')
    assert held['state'] == 'held'
    assert held['attempts'][0]['worker'] is None
    assert held['attempts'][0]['completion_evidence'] is None
    assert held['usage']['accounting_unknown'] is True
    assert held['active_capacity_reserved'] is True
    assert client.reconcile(job_id, held['revision'], held['generation'], 'operator',
                            'cannot infer zero usage', disposition='safe-retry')['category'] == 'recovery_held'


def test_finite_acquisition_reports_actual_elapsed_usage_instead_of_known_zero(database):
    import time

    class MeasuredSource(ControlledSource):
        def read(self, source_id, revision, limit):
            time.sleep(0.03)
            sum(value * value for value in range(100000))
            return super().read(source_id, revision, limit)

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'measured', [member()])['members'][0]['job_id']
    captured = client.capture(job_id, client.claim(job_id, 0),
                              MeasuredSource({'P': ('r1', b'controlled input')}))
    assert captured['state'] == 'queued'
    assert captured['usage']['wall_seconds'] >= 0.03
    assert captured['usage']['cpu_seconds'] > 0
    assert captured['usage']['measured_cpu_seconds'] > 0
    assert captured['usage']['accounting_unknown'] is False
    assert captured['counters']['acquisition_requests'] == 1
    fresh = Ingestion(database).status(job_id)
    assert fresh['usage'] == captured['usage']


def test_failed_finite_read_keeps_measured_usage_and_charged_request_on_same_attempt(database):
    import time

    class FailedSource(ControlledSource):
        def read(self, source_id, revision, limit):
            time.sleep(0.03)
            sum(value * value for value in range(100000))
            raise RuntimeError('controlled source read failed')

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'failed-read', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    with pytest.raises(RuntimeError, match='controlled source read failed'):
        client.capture(job_id, claim, FailedSource({}))
    failed = Ingestion(database).status(job_id)
    assert failed['counters']['acquisition_requests'] == 1
    assert failed['usage']['wall_seconds'] >= 0.03
    assert failed['usage']['cpu_seconds'] > 0
    captured = client.capture(job_id, claim, ControlledSource({'P': ('r1', b'controlled input')}))
    assert captured['state'] == 'queued'
    assert captured['counters']['acquisition_requests'] == 2
    assert captured['usage']['wall_seconds'] >= failed['usage']['wall_seconds']
    assert captured['usage']['cpu_seconds'] >= failed['usage']['cpu_seconds']
    assert captured['usage']['accounting_unknown'] is False


def test_expired_finite_capture_retains_usage_and_completion_without_committing_input(database):
    import time

    class LateSource(ControlledSource):
        def read(self, source_id, revision, limit):
            time.sleep(11)
            return super().read(source_id, revision, limit)

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'late-read', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    outcome = client.capture(job_id, claim, LateSource({'P': ('r1', b'controlled input')}))
    assert outcome['category'] == 'stale_commit'
    held = Ingestion(database).status(job_id)
    assert held['content_pin'] is None and held['canonical_job_id'] is None
    assert held['effect_count'] == 0
    assert held['usage']['wall_seconds'] >= 11
    assert held['counters']['acquisition_requests'] == 1
    assert held['state'] == 'held'
    assert held['attempts'][0]['stopped'] is True
    assert held['attempts'][0]['completion_evidence'] == 'finite_controlled_call'
    assert held['usage']['accounting_unknown'] is False
    assert held['active_capacity_reserved'] is True
    retried = client.reconcile(job_id, held['revision'], held['generation'],
                               'operator', 'read completed after expiry; retry exact source pin',
                               disposition='safe-retry')
    assert retried['state'] == 'acquiring'
    captured = client.capture(job_id, client.claim(job_id, retried['revision']),
                              ControlledSource({'P': ('r1', b'controlled input')}))
    assert captured['state'] == 'queued'
    assert captured['usage']['wall_seconds'] >= 11
    assert captured['counters']['acquisition_requests'] == 2


def test_finite_read_outliving_transaction_limit_can_finish_within_claim(database):
    import time

    class SlowSource(ControlledSource):
        def read(self, source_id, revision, limit):
            time.sleep(6)
            return super().read(source_id, revision, limit)

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'slow-read', [member()])['members'][0]['job_id']
    outcome = client.capture(job_id, client.claim(job_id, 0),
                             SlowSource({'P': ('r1', b'controlled input')}))
    fresh = Ingestion(database).status(job_id)
    assert fresh.get('id') == job_id
    assert outcome.get('state') == 'queued', outcome


def test_same_claim_cannot_start_second_read_or_finish_while_first_read_is_active(database):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered, release = threading.Event(), threading.Event()
    overlapping_reads = []

    class PausedSource(ControlledSource):
        def read(self, source_id, revision, limit):
            entered.set()
            assert release.wait(5), 'controlled read release was not signaled'
            return super().read(source_id, revision, limit)

    class OtherSource(ControlledSource):
        def read(self, source_id, revision, limit):
            overlapping_reads.append(True)
            return super().read(source_id, revision, limit)

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'one-read', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.capture, job_id, claim,
                            PausedSource({'P': ('r1', b'controlled input')}))
        try:
            assert entered.wait(3)
            refused = client.capture(job_id, claim, OtherSource({'P': ('r1', b'overlapping bytes')}))
            during = client.status(job_id)
        finally:
            release.set()
        captured = first.result(timeout=5)
    assert refused.get('category') == 'acquisition_in_progress', refused
    assert overlapping_reads == []
    assert during['state'] == 'running' and during['current_claim'] is not None
    assert during['usage']['accounting_unknown'] is True
    assert during['counters']['acquisition_requests'] == 1
    assert captured['state'] == 'queued'
    assert captured['counters']['acquisition_requests'] == 1
    assert captured['usage']['accounting_unknown'] is False


def test_reviewed_revocation_during_read_denies_input_and_waits_for_actual_owner_completion(database):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered, release = threading.Event(), threading.Event()

    class PausedSource(ControlledSource):
        def read(self, source_id, revision, limit):
            entered.set()
            assert release.wait(5), 'controlled read release was not signaled'
            return super().read(source_id, revision, limit)

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'revoked-read', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.capture, job_id, claim,
                              PausedSource({'P': ('r1', b'controlled input')}))
        try:
            assert entered.wait(3)
            running = client.status(job_id)
            held = client.reconcile(job_id, running['revision'], running['generation'],
                                    'operator', 'review in-flight exact source read', disposition='hold')
            assert held['state'] == 'held' and held['active_capacity_reserved'] is True
            assert held['usage']['accounting_unknown'] is True
            assert client.reconcile(job_id, held['revision'], held['generation'], 'operator',
                                    'owner still reading', disposition='safe-retry')['category'] == 'recovery_held'
        finally:
            release.set()
        assert pending.result(timeout=5)['category'] == 'stale_commit'
    finished = Ingestion(database).status(job_id)
    assert finished['state'] == 'held' and finished['generation'] == held['generation']
    assert finished['reason'] == 'reviewed_generation_revoked'
    assert finished['content_pin'] is None and finished['canonical_job_id'] is None
    assert finished['effect_count'] == 0
    assert finished['attempts'][0]['stopped'] is True
    assert finished['usage']['accounting_unknown'] is False
    assert finished['counters']['acquisition_requests'] == 1
    assert finished['active_capacity_reserved'] is True
    retry = client.reconcile(job_id, finished['revision'], finished['generation'],
                             'operator', 'actual finite owner returned', disposition='safe-retry')
    assert retry['state'] == 'acquiring'


def test_text_failed_processing_retry_reserves_headroom_and_keeps_prior_allowance(command_process, database):
    client, start, _ = command_process
    start()
    worker = Ingestion(database, AdmissionProfile.l1())
    job_id = worker.offer('operator', 'failed-processing', [member()])['members'][0]['job_id']
    worker.capture(job_id, worker.claim(job_id, 0), ControlledSource({'P': ('r1', b'controlled input')}))
    held = worker.run_controlled(job_id, worker.status(job_id)['revision'], literal_output(),
                                 test_wall_seconds=1, test_delay_seconds=20)
    assert held['state'] == 'held' and held['attempts'][-1]['accounting_complete'] is True, held['reason']
    failed = client(dict(operation='reconcile', job_id=job_id, expected_revision=held['revision'],
                         expected_generation=held['generation'], actor='operator',
                         reason='diagnosed controlled processing failure', disposition='fail'))
    assert failed['state'] == 'failed'
    assert failed['next_action'] == 'reviewed_retry'
    queued = client(dict(operation='retry', job_id=job_id, expected_revision=failed['revision'],
                         expected_generation=failed['generation'], actor='operator',
                         reason='retry missing processing after diagnosis'))
    assert queued.get('state') == 'queued', queued
    assert queued['reservation_bytes'] == AdmissionProfile.l1().job_bytes
    assert queued['attempts'] == held['attempts']
    assert queued['usage']['cpu_seconds'] == held['usage']['cpu_seconds']
    assert queued['usage']['wall_seconds'] == held['usage']['wall_seconds']
    assert queued['content_pin'] == held['content_pin']
    assert queued['input']['intent_id'] == 'ordinary'
    completed = worker.publish(job_id, worker.claim(job_id, queued['revision']), literal_output())
    assert completed['state'] == 'completed'
    assert [attempt['stage'] for attempt in completed['attempts']] == ['acquisition', 'processing', 'processing']


def test_exceptional_read_return_after_reviewed_revocation_records_actual_completion(database):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered, release = threading.Event(), threading.Event()

    class FailedSource(ControlledSource):
        def read(self, source_id, revision, limit):
            entered.set()
            assert release.wait(5), 'controlled read release was not signaled'
            raise RuntimeError('controlled revoked source failure')

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'failed-revoked-read', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.capture, job_id, claim, FailedSource({}))
        try:
            assert entered.wait(3)
            running = client.status(job_id)
            reviewed = client.reconcile(job_id, running['revision'], running['generation'],
                                        'operator', 'hold actual pending acquisition', disposition='hold')
            assert reviewed['usage']['accounting_unknown'] is True
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='controlled revoked source failure'):
            pending.result(timeout=5)
    finished = Ingestion(database).status(job_id)
    assert finished['state'] == 'held' and finished['generation'] == reviewed['generation']
    assert finished['reason'] == 'reviewed_generation_revoked'
    assert finished['usage']['accounting_unknown'] is False
    assert finished['attempts'][0]['stopped'] is True
    assert finished['attempts'][0]['completion_evidence'] == 'finite_controlled_call'
    assert finished['content_pin'] is None and finished['canonical_job_id'] is None
    assert finished['counters']['acquisition_requests'] == 1
    assert finished['active_capacity_reserved'] is True
    retry = client.reconcile(job_id, finished['revision'], finished['generation'],
                             'operator', 'actual failed read returned', disposition='safe-retry')
    assert retry['state'] == 'acquiring'


def test_exceptional_read_after_real_expiry_records_usage_and_finite_completion(database):
    import time

    class FailedLateSource(ControlledSource):
        def read(self, source_id, revision, limit):
            time.sleep(11)
            raise RuntimeError('controlled late source failure')

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'exceptional-late', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    with pytest.raises(RuntimeError, match='controlled late source failure'):
        client.capture(job_id, claim, FailedLateSource({}))
    held = Ingestion(database).status(job_id)
    assert held['state'] == 'held' and held['reason'] == 'claim_lost'
    assert held['generation'] > claim['generation']
    assert held['attempts'][0]['stopped'] is True
    assert held['attempts'][0]['completion_evidence'] == 'finite_controlled_call'
    assert held['usage']['wall_seconds'] >= 11
    assert held['usage']['accounting_unknown'] is False
    assert held['counters']['acquisition_requests'] == 1
    assert held['content_pin'] is None and held['canonical_job_id'] is None
    assert held['effect_count'] == 0 and held['active_capacity_reserved'] is True
    retry = client.reconcile(job_id, held['revision'], held['generation'],
                             'operator', 'actual failed read returned after expiry', disposition='safe-retry')
    assert retry['state'] == 'acquiring'


@pytest.mark.parametrize('metric,wall,cpu,reason', [
    ('wall', 31, 0.01, 'attempt_wall_limit'),
    ('cpu', 0.01, 21, 'attempt_cpu_limit'),
])
@pytest.mark.parametrize('exceptional', [False, True])
def test_finite_acquisition_enforces_attempt_allowance_after_actual_return_with_simulated_measurement(
        database, monkeypatch, metric, wall, cpu, reason, exceptional):
    """Clock-boundary test; actual controlled I/O returns, native limits tested elsewhere."""
    from types import SimpleNamespace
    import nilaris.execution as execution
    calls = []

    class MeasuredSource(ControlledSource):
        def read(self, source_id, revision, limit):
            calls.append(True)
            if exceptional:
                raise RuntimeError('controlled measured source failure')
            return super().read(source_id, revision, limit)

    class ForbiddenSource(ControlledSource):
        def read(self, source_id, revision, limit):
            pytest.fail('an exhausted or stopped acquisition cannot read again')

    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'attempt-' + metric, [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    wall_clock, cpu_clock = iter((0, wall)), iter((0, cpu))
    monkeypatch.setattr(execution, 'time', SimpleNamespace(
        monotonic=lambda: next(wall_clock), process_time=lambda: next(cpu_clock)))
    source = MeasuredSource({'P': ('r1', b'controlled input')})
    if exceptional:
        with pytest.raises(RuntimeError, match='controlled measured source failure'):
            client.capture(job_id, claim, source)
    else:
        client.capture(job_id, claim, source)
    held = Ingestion(database).status(job_id)
    assert held['state'] == 'held' and held['reason'] == reason
    assert held['attempts'][0]['stopped'] is True
    assert held['attempts'][0]['completion_evidence'] == 'finite_controlled_call'
    assert held['usage']['accounting_unknown'] is False
    assert held['usage']['wall_seconds'] == wall and held['usage']['cpu_seconds'] == cpu
    assert held['content_pin'] is None and held['canonical_job_id'] is None
    assert held['counters']['acquisition_requests'] == 1 and calls == [True]
    assert client.capture(job_id, claim, ForbiddenSource({})).get('category') in ('stale_commit', 'resource_exhausted')
    other = client.offer('operator', 'other', [dict(member('other'), document_id='other-doc')])['members'][0]['job_id']
    assert client.claim(other, 0)['claimed'] is True


@pytest.mark.parametrize('metric,first_wall,first_cpu,second_wall,second_cpu,reason', [
    ('wall', 31, 0.01, 90, 0.01, 'job_wall_limit'),
    ('cpu', 0.01, 21, 0.01, 40, 'job_cpu_limit'),
])
@pytest.mark.parametrize('exceptional', [False, True])
def test_finite_acquisition_preserves_job_allowance_across_retry_with_simulated_measurement(
        database, monkeypatch, metric, first_wall, first_cpu, second_wall, second_cpu, reason, exceptional):
    """Simulated usage-clock boundary; each real finite read actually returns."""
    from types import SimpleNamespace
    import nilaris.execution as execution
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'cumulative-' + metric, [member()])['members'][0]['job_id']

    class MeasuredSource(ControlledSource):
        def read(self, source_id, revision, limit):
            if exceptional:
                raise RuntimeError('controlled cumulative source failure')
            return super().read(source_id, revision, limit)

    def capture_with_measurement(claim, wall, cpu):
        wall_clock, cpu_clock = iter((0, wall)), iter((0, cpu))
        monkeypatch.setattr(execution, 'time', SimpleNamespace(
            monotonic=lambda: next(wall_clock), process_time=lambda: next(cpu_clock)))
        if exceptional:
            with pytest.raises(RuntimeError, match='controlled cumulative source failure'):
                client.capture(job_id, claim, MeasuredSource({'P': ('r1', b'controlled input')}))
        else:
            client.capture(job_id, claim, MeasuredSource({'P': ('r1', b'controlled input')}))
        return client.status(job_id)

    first = capture_with_measurement(client.claim(job_id, 0), first_wall, first_cpu)
    assert first['state'] == 'held'
    retry = client.reconcile(job_id, first['revision'], first['generation'],
                             'operator', 'retry exact pin within cumulative allowance', disposition='safe-retry')
    assert retry['state'] == 'acquiring'
    second_claim = client.claim(job_id, retry['revision'])
    exhausted = capture_with_measurement(second_claim, second_wall, second_cpu)
    assert exhausted['state'] == 'held' and exhausted['reason'] == reason
    assert exhausted['usage']['wall_seconds'] == first_wall + second_wall
    assert exhausted['usage']['cpu_seconds'] == first_cpu + second_cpu
    assert exhausted['usage']['accounting_unknown'] is False
    assert exhausted['content_pin'] is None and exhausted['canonical_job_id'] is None
    assert exhausted['counters']['acquisition_requests'] == 2
    assert [attempt['stage'] for attempt in exhausted['attempts']] == ['acquisition', 'acquisition']
    refused = client.reconcile(job_id, exhausted['revision'], exhausted['generation'],
                               'operator', 'cannot reset cumulative allowance', disposition='safe-retry')
    assert refused.get('category') in ('resource_exhausted', 'recovery_held')
    assert client.status(job_id)['generation'] == exhausted['generation']
    class ForbiddenSource(ControlledSource):
        def read(self, source_id, revision, limit):
            pytest.fail('exhausted Job allowance cannot initiate another read')
    assert client.capture(job_id, second_claim, ForbiddenSource({})).get('category') in (
        'stale_commit', 'resource_exhausted')


def test_exhausted_finite_job_reports_only_reviewed_dispositions_and_no_retry(database, monkeypatch):
    """Simulated CPU accounting boundary after an actual finite source return."""
    from types import SimpleNamespace
    import nilaris.execution as execution
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'exhausted-actions', [member()])['members'][0]['job_id']
    claim = client.claim(job_id, 0)
    wall_clock, cpu_clock = iter((0, 0.01)), iter((0, 61))
    monkeypatch.setattr(execution, 'time', SimpleNamespace(
        monotonic=lambda: next(wall_clock), process_time=lambda: next(cpu_clock)))
    exhausted = client.capture(job_id, claim, ControlledSource({'P': ('r1', b'controlled input')}))
    assert exhausted['reason'] == 'job_cpu_limit'
    assert exhausted['next_action'] == 'reviewed_disposition'
    assert exhausted['recovery_actions'] == ['hold', 'fail', 'quarantine']
    refused = client.reconcile(job_id, exhausted['revision'], exhausted['generation'],
                               'operator', 'cannot reset consumed allowance', disposition='safe-retry')
    assert refused['category'] == 'resource_exhausted'
    failed = client.reconcile(job_id, exhausted['revision'], exhausted['generation'],
                              'operator', 'retain exhausted work as diagnosed failure', disposition='fail')
    assert failed['state'] == 'failed' and failed['next_action'] == 'query_job'


def test_failed_acquisition_retry_restores_working_headroom_and_same_source_stage(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    job_id = client.offer('operator', 'failed-acquisition', [member()])['members'][0]['job_id']
    held = client.capture(job_id, client.claim(job_id, 0), ControlledSource({}))
    failed = client.reconcile(job_id, held['revision'], held['generation'],
                              'operator', 'diagnosed offered-version acquisition failure', disposition='fail')
    assert failed['state'] == 'failed'
    retried = client.retry(job_id, failed['revision'], failed['generation'],
                           'operator', 'retry only the same offered revision')
    assert retried.get('state') == 'acquiring', retried
    assert retried['reservation_bytes'] == AdmissionProfile.l1().job_bytes
    assert retried['input'] == held['input']
    assert retried['input']['intent_id'] == 'ordinary'
    assert retried['content_pin'] is None
    assert retried['attempts'] == held['attempts']
    assert retried['usage']['cpu_seconds'] == held['usage']['cpu_seconds']
    claim = client.claim(job_id, retried['revision'])
    assert claim['stage'] == 'acquisition'
    captured = client.capture(job_id, claim, ControlledSource({'P': ('r1', b'controlled input')}))
    assert captured['state'] == 'queued'
    assert captured['counters']['acquisition_requests'] == 2
    assert [attempt['stage'] for attempt in captured['attempts']] == ['acquisition', 'acquisition']
