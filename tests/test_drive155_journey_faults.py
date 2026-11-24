"""ROOT ONLY: actual acquisition timeout and concurrent reviewed revocation."""
import json
import os
import signal
import time

import pytest

from test_drive155_source import synthetic_sa


class FaultTransport:
    """Controlled external transport with file-signalled, bounded media faults."""
    def __init__(self, provider, fault, entered, released):
        self.provider, self.fault = provider, fault
        self.entered, self.released = entered, released

    def get(self, path, **kwargs):
        if 'alt=media' in path:
            self.entered.write_text('entered')
            if self.fault == 'hang':
                time.sleep(10)
            elif self.fault == 'wait-for-revocation':
                deadline = time.monotonic() + 5
                while not self.released.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                if not self.released.exists():
                    raise ValueError('controlled release unavailable')
        return self.provider.get(path, **kwargs)


def controlled(database, synthetic_sa, tmp_path, fault):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    from nilaris.drive155_journey import DriveJourney
    from nilaris.drive155_source import SourceBinding, AuthorizedDriveSource
    from test_drive155_source import configuration, authority, SyntheticProvider, TokenResponse
    config = configuration()
    bound = SourceBinding(config, authority(config))
    entered, released = tmp_path / 'media-entered', tmp_path / 'media-released'
    exchanges = tmp_path / 'exchanges'
    def supplier(binding_id):
        with exchanges.open('a') as output:
            output.write('exchange\n')
        return synthetic_sa
    transport = FaultTransport(SyntheticProvider(bound.allowed_member), fault, entered, released)
    source = AuthorizedDriveSource(bound, credential_supplier=supplier, transport=transport,
                                   token_transport=lambda **kwargs: TokenResponse())
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    operator = DriveJourney(ingestion, {'synthetic-binding': source})
    ingestion.drive_policy = operator
    assert operator.drive_discover('synthetic-binding', 'discover', 'page')['state'] == 'completed'
    receipt = operator.drive_admit('synthetic-binding', 'discover', 'page')
    job_id = receipt['members'][0]['job_id']
    return operator, ingestion, job_id, entered, released, exchanges


def test_root_acquisition_unknown_retains_all_capacity_and_original_identity(database, synthetic_sa, tmp_path):
    from nilaris.drive155_supervision import SupervisedSource
    operator, ingestion, job_id, entered, _, exchanges = controlled(database, synthetic_sa, tmp_path, 'hang')
    operator.runner = SupervisedSource(test_wall_seconds=.4)
    before = ingestion.status(job_id)
    held = operator.drive_acquire('synthetic-binding', 'acquire', job_id, before['revision'])
    assert entered.exists()
    assert held['state'] == 'held', held
    assert held['reserved_requests'] == 5
    assert held['reservation_bytes'] == 1024**2
    assert held['active_capacity_reserved'] is True
    assert held['accounting']['charged_cpu_seconds'] == 20
    current = ingestion.status(job_id)
    assert current['state'] == 'held', current
    assert current['reason'] == 'source_accounting_unknown'
    assert current['generation'] > before['generation']
    assert current['active_capacity_reserved'] is True
    assert current['reservation_bytes'] == 8 * 1024**2
    assert current['counters']['acquisition_requests'] == 5
    assert current['counters']['captured_input_bytes'] == 0
    assert current['admission']['source_retained_bytes'] == 2 * 1024**2
    assert current['admission']['active_capacity_reservations'] == 1
    assert operator.drive_acquire('synthetic-binding', 'acquire', job_id, before['revision']) == held
    assert operator.drive_acquire('synthetic-binding', 'replacement', job_id, current['revision'])['category'] == 'original_acquisition_required'
    assert exchanges.read_text().splitlines() == ['exchange', 'exchange']


def test_root_returned_exact_bytes_cannot_cross_reviewed_generation_fence(database, synthetic_sa, tmp_path):
    operator, ingestion, job_id, entered, released, exchanges = controlled(database, synthetic_sa, tmp_path, 'wait-for-revocation')
    before = ingestion.status(job_id)
    review = tmp_path / 'review-outcome'
    controller = os.fork()
    if controller == 0:
        try:
            deadline = time.monotonic() + 8
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            if not entered.exists():
                os._exit(2)
            running = ingestion.status(job_id)
            revoked = ingestion.reconcile(job_id, running['revision'], running['generation'],
                                           'controlled-reviewer', 'revoke before exact media returns')
            review.write_text(json.dumps({k: revoked[k] for k in ('revision','generation','reason')}))
            released.write_text('review committed')
            os._exit(0)
        except BaseException:
            os._exit(3)
    reaped = False
    try:
        held = operator.drive_acquire('synthetic-binding', 'acquire', job_id, before['revision'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found, code, _ = os.wait4(controller, os.WNOHANG)
            if found:
                reaped = True
                assert os.waitstatus_to_exitcode(code) == 0
                break
            time.sleep(.01)
        assert reaped
        expected = json.loads(review.read_text())
        assert held['state'] == 'held', held
        # The provider genuinely returned and was reaped; refusal is the real
        # durable generation fence, rather than a fabricated transport timeout.
        assert held['accounting']['proved'] is True, held
        assert held['accounting']['evidence'] == 'process_group_reaped'
        current = ingestion.status(job_id)
        assert {k: current[k] for k in expected} == expected
        assert current['state'] == 'held'
        assert current['reason'] == 'reviewed_generation_revoked'
        assert current['counters']['captured_input_bytes'] == 0
        assert current['content_pin'] is None
        assert current['counters']['acquisition_requests'] == 5
        assert current['attempts'][0]['accounting_complete'] is True
        assert current['attempts'][0]['stopped'] is True
        assert operator.drive_acquire('synthetic-binding', 'acquire', job_id, before['revision']) == held
        assert exchanges.read_text().splitlines() == ['exchange', 'exchange']
    finally:
        if not reaped:
            os.kill(controller, signal.SIGKILL)
            os.waitpid(controller, 0)


def test_root_cpu_allowance_survives_coordinator_death_before_capture(database, synthetic_sa, tmp_path):
    from nilaris.drive155_journey import DriveJourney
    from nilaris.drive155_supervision import SupervisedSource
    operator, ingestion, job_id, entered, _, exchanges = controlled(database, synthetic_sa, tmp_path, 'hang')
    operator.runner = SupervisedSource(test_wall_seconds=1)
    before = ingestion.status(job_id)
    coordinator = os.fork()
    if coordinator == 0:
        try:
            operator.drive_acquire('synthetic-binding', 'interrupted', job_id, before['revision'])
            os._exit(0)
        except BaseException:
            os._exit(3)
    reaped = False
    try:
        deadline = time.monotonic() + 8
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert entered.exists(), 'controlled provider did not enter media read'
        pending = operator.drive_operation('synthetic-binding', 'interrupted')
        running = ingestion.status(job_id)
        assert pending['state'] == 'pending'
        assert pending['accounting']['charged_cpu_seconds'] == 20
        assert pending['accounting']['proved'] is False
        assert pending['accounting']['reason'] == 'source_accounting_unknown'
        assert running['usage']['cpu_seconds'] == 20
        assert running['attempts'][0]['cpu_seconds'] == 20
        assert running['attempts'][0]['accounting_complete'] is False
        os.kill(coordinator, signal.SIGKILL)
        os.waitpid(coordinator, 0)
        reaped = True
        # Let the independently bounded provider group expire after parent loss.
        time.sleep(1.2)
        restored = DriveJourney(ingestion, operator.sources)
        assert restored.drive_acquire('synthetic-binding', 'interrupted', job_id, before['revision']) == pending
        retained = ingestion.status(job_id)
        assert retained['usage']['cpu_seconds'] == 20
        assert retained['counters']['acquisition_requests'] == 5
        assert retained['counters']['captured_input_bytes'] == 0
        assert retained['reservation_bytes'] == 8 * 1024**2
        assert retained['active_capacity_reserved'] is True
        assert pending['reserved_requests'] == 5
        assert pending['reservation_bytes'] == 1024**2
        assert pending['active_capacity_reserved'] is True
        assert exchanges.read_text().splitlines() == ['exchange', 'exchange']
    finally:
        if not reaped:
            os.kill(coordinator, signal.SIGKILL)
            os.waitpid(coordinator, 0)
        # No provider is left beyond its independently enforced one-second bound.
        time.sleep(1.2)


def test_root_discovery_cpu_allowance_is_visible_before_provider_returns(database, synthetic_sa, tmp_path):
    from nilaris.drive155_supervision import SupervisedSource
    from test_drive155_journey import journey
    operator, _, _, provider, _ = journey(database, synthetic_sa, tmp_path)
    entered = tmp_path / 'discovery-entered'
    original = provider.get
    def blocked(path, **kwargs):
        if path.startswith('/drive/v3/about'):
            entered.write_text('entered')
            time.sleep(10)
        return original(path, **kwargs)
    provider.get = blocked
    operator.runner = SupervisedSource(test_wall_seconds=1)
    coordinator = os.fork()
    if coordinator == 0:
        try:
            operator.drive_discover('synthetic-binding', 'in-flight', 'page')
            os._exit(0)
        except BaseException:
            os._exit(3)
    try:
        deadline = time.monotonic() + 8
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert entered.exists()
        pending = operator.drive_operation('synthetic-binding', 'in-flight')
        assert pending['state'] == 'pending'
        assert pending['reserved_cpu_seconds'] == 20
        assert pending['accounting']['charged_cpu_seconds'] == 20
        assert pending['accounting']['proved'] is False
        assert pending['reserved_requests'] == 4
        assert pending['reservation_bytes'] == 1024**2
        assert pending['active_capacity_reserved'] is True
    finally:
        os.kill(coordinator, signal.SIGKILL)
        os.waitpid(coordinator, 0)
        time.sleep(1.2)


@pytest.mark.parametrize('recorded_accounting', [False, True], ids=['missing-accounting', 'recorded-accounting'])
def test_root_legacy_unknown_operation_retains_allowance_and_migrates_without_doublecharge(database, synthetic_sa, tmp_path, recorded_accounting):
    import psycopg
    from psycopg.types.json import Jsonb
    from nilaris.admission import migrate
    operator, ingestion, job_id, _, _, _ = controlled(database, synthetic_sa, tmp_path, 'hang')
    from nilaris.drive155_supervision import SupervisedSource
    operator.runner = SupervisedSource(test_wall_seconds=.3)
    before = ingestion.status(job_id)
    held = operator.drive_acquire('synthetic-binding', 'legacy', job_id, before['revision'])
    assert held['state'] == 'held'
    # Synthetic historical storage setup: a coordinator could disappear before
    # _run recorded accounting, or after that commit but before _capture charged
    # the Job attempt. Neither window may leave a free CPU allowance on restart.
    with psycopg.connect(**database) as connection:
        connection.execute("""UPDATE drive155_operations SET accounting=%s,state='pending',result=NULL
            WHERE operation_id='legacy'""", (Jsonb(held['accounting']) if recorded_accounting else None,))
        connection.execute('UPDATE job_attempts SET cpu_seconds=0 WHERE job_id=%s', (job_id,))
    unknown = operator.drive_operation('synthetic-binding', 'legacy')
    assert unknown['reserved_cpu_seconds'] == 20
    assert unknown['accounting']['charged_cpu_seconds'] == 20
    assert unknown['accounting']['proved'] is False
    assert unknown['accounting']['reason'] == 'source_accounting_unknown'
    if recorded_accounting:
        assert unknown['accounting'] == held['accounting']
    migrate(database)
    assert ingestion.status(job_id)['usage']['cpu_seconds'] == 20
    migrate(database)
    assert ingestion.status(job_id)['usage']['cpu_seconds'] == 20
    assert operator.drive_acquire('synthetic-binding', 'legacy', job_id, before['revision']) == unknown
