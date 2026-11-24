"""ROOT ONLY: actual command -> PostgreSQL -> supervised fixture composition."""
import base64
import copy
import json
import time

import pytest

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.command_service import Configuration, request
from nilaris.drive import PinnedDrive
from nilaris.fixture_execution import FixtureExecution
from nilaris.intake167 import SyntheticAdapter, plan_digest, run_once
from test_command_service import command_process
from test_synthetic_fixture_drive import configuration
from test_intake167 import plan, authorized


def configured():
    entry = configuration()['entries'][0]
    member = entry['allowed_member']
    raw = dict(mode='synthetic-drive-v1', **entry)
    source = PinnedDrive(raw)
    adapter = SyntheticAdapter(source, member, dict(version=1, customer_id='fixture-customer',
        principal='fixture-principal', credential_binding_id='fixture-credential',
        policy_version='fixture-policy-v1', authority_id='fixture-owner-grant',
        source_root=member['source_root']))
    frozen = plan(adapter)
    frozen['limits']['input_bytes'] = 8192
    frozen['provenance_locator'] = 'fixture://P.pdf'
    return raw, source, adapter, frozen


def test_root_queued_processing_claim_is_durable_before_thread_dispatch(database, monkeypatch):
    """Lost ACK/start failure cannot leave an apparently unstarted queued Job."""
    _, source, adapter, _ = configured()
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1(), drive_policy=source)
    receipt = ingestion.offer('intake167-start-fault', 'offer', [adapter.member])
    job_id = receipt['members'][0]['job_id']
    before = ingestion.status(job_id)
    owned = ingestion.claim(job_id, before['revision'])
    queued = ingestion.capture(job_id, owned, source.controlled_source())
    assert queued['state'] == 'queued'
    def scheduling_failed(thread):
        raise RuntimeError('controlled thread start failure')
    monkeypatch.setattr('nilaris.fixture_execution.threading.Thread.start', scheduling_failed)
    runner = FixtureExecution(ingestion, source)
    with pytest.raises(RuntimeError, match='controlled thread start failure'):
        runner.execute_fixture_v1(job_id, queued['revision'])
    after = ingestion.status(job_id)
    assert after['state'] == 'running'
    assert after['generation'] == queued['generation'] + 1
    assert after['counters']['attempts'] == 2
    # A new listener/client sees durable pending work and cannot dispatch again.
    assert FixtureExecution(ingestion, source).execute_fixture_v1(job_id, queued['revision']).get('claimed') is not True
    assert ingestion.status(job_id)['counters'] == after['counters']


def test_root_http_intake_completed_restart_never_reexecutes(command_process):
    client, start, config_path = command_process
    raw, _, adapter, frozen = configured()
    config = json.loads(config_path.read_text())
    config['drive'] = raw
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    start()
    first = run_once(frozen, adapter, client, authority=authorized(frozen))
    assert first['category'] in ('intake_incomplete', 'intake_review_handoff'), first
    receipt = client(dict(operation='resolve', namespace='discovery:' + adapter.snapshot['namespace'],
                          command_id=frozen['page_id']))
    job_id = receipt['members'][0]['job_id']
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        before = client(dict(operation='status', job_id=job_id))
        if before.get('state') != 'running':
            break
        time.sleep(.1)
    assert before['state'] == 'completed', before
    # No process/acquire phases are needed on restart of an already complete Job.
    read_plan = copy.deepcopy(frozen)
    read_plan['phases'] = []
    second = run_once(read_plan, adapter, client, authority=authorized(read_plan))
    assert second['category'] == 'intake_review_handoff', second
    assert second['result_pages'] >= 1
    assert client(dict(operation='status', job_id=job_id))['counters'] == before['counters']
    assert second['job']['results'] == before['results']


def test_root_lost_admission_response_reconciles_original_page(command_process):
    client, start, config_path = command_process
    raw, _, adapter, frozen = configured()
    config = json.loads(config_path.read_text())
    config['drive'] = raw
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    start()
    namespace = adapter.snapshot['namespace']
    assert client(adapter.discover(frozen))['state'] == 'staged'
    original = client(dict(operation='discovery_admit', namespace=namespace, page_id=frozen['page_id']))
    # The coordinator starts after admission committed but cursor reconciliation did not.
    read_plan = copy.deepcopy(frozen)
    read_plan['phases'] = ['admit']
    result = run_once(read_plan, adapter, client, authority=authorized(read_plan))
    assert result['category'] == 'intake_phase_not_authorized', result
    page = client(dict(operation='discovery_read', namespace=namespace, page_id=frozen['page_id']))
    assert page['state'] == 'settled'
    assert page['members'][0]['disposition'] == original['members'][0]
    assert client(dict(operation='resolve', namespace='discovery:' + namespace,
                       command_id=frozen['page_id'])) == original


def test_root_unknown_drive_operation_attests_rebound_authority_without_dispatch(database):
    from nilaris.drive155_source import SourceBinding, AuthorizedDriveSource
    from nilaris.drive155_journey import DriveJourney
    from nilaris.intake167 import DriveAdapter
    from test_drive155_source import configuration as drive_configuration, authority
    config = drive_configuration()
    original_grant = authority(config)
    original = DriveAdapter(config, original_grant)
    changed_grant = dict(original_grant, authority_id='new-scoped-owner-grant')
    current = SourceBinding(config, changed_grant)
    calls = []
    def tripwire(*args, **kwargs):
        calls.append('provider')
        raise AssertionError('provider must remain unreachable')
    provider = AuthorizedDriveSource(current, credential_supplier=tripwire,
                                    transport=None, token_transport=tripwire)
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    journey = DriveJourney(ingestion, {config['binding_id']: provider})
    unknown = journey.drive_operation(config['binding_id'], 'discover-1')
    assert unknown['category'] == 'source_operation_unknown'
    assert unknown['namespace'] == current.namespace
    assert unknown['namespace'] != original.snapshot['namespace']
    def command(value):
        request = dict(value)
        operation = request.pop('operation')
        return getattr(journey, operation)(**request)
    frozen = plan(original)
    result = run_once(frozen, original, command, authority=authorized(frozen))
    assert result['category'] == 'intake_identity_mismatch'
    assert calls == []
