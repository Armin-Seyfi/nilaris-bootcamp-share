"""Offline source supervision and root-only durable journey acceptance."""
import os
import time

from nilaris.drive155_supervision import SupervisedSource
from test_command_service import command_process


def test_supervised_provider_has_durable_identity_gate_and_reaped_exact_result(tmp_path):
    marker = tmp_path / 'dispatched'
    def provider():
        marker.write_text('yes')
        return b'exact captured bytes'
    def gate(identity):
        assert not marker.exists()
        assert identity['pid'] == identity['pgid']
        return True
    result = SupervisedSource().run(provider, gate)
    assert result['proved'] is True
    assert result['value'] == b'exact captured bytes'
    assert result['evidence'] == 'process_group_reaped'
    assert result['cpu_seconds'] >= 0


def test_identity_gate_refusal_never_dispatches(tmp_path):
    marker = tmp_path / 'dispatched'
    def provider():
        marker.write_text('no')
    result = SupervisedSource().run(provider, lambda identity: False)
    assert result['value'] is None
    assert not marker.exists()


def test_independent_monitor_stops_hung_provider_when_parent_gate_stalls(tmp_path):
    marker = tmp_path / 'dispatched'
    def provider():
        marker.write_text('no')
        time.sleep(10)
    def gate(identity):
        time.sleep(.5)
        return True
    result = SupervisedSource(test_wall_seconds=.2).run(provider, gate)
    assert result['proved'] is False
    assert result['value'] is None
    assert not marker.exists()


def test_default_source_commands_refuse_before_loading_authority():
    from nilaris.drive155_journey import disabled
    assert disabled()['category'] == 'live_source_disabled'
    assert disabled()['coverage'] == 'unknown'


def journey(database, synthetic_sa, tmp_path):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate
    from nilaris.drive155_journey import DriveJourney
    from test_drive155_source import source_fixture
    source, provider, _, _ = source_fixture(synthetic_sa)
    marker = tmp_path / 'supplier-dispatches'
    def supplier(binding_id):
        with marker.open('a') as output:
            output.write('exchange\n')
        return synthetic_sa
    source._credential_supplier = supplier  # Controlled external credential boundary.
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    operator = DriveJourney(ingestion, {'synthetic-binding': source})
    ingestion.drive_policy = operator
    return operator, ingestion, source, provider, marker


def test_root_durable_exact_journey_replays_and_processes_without_approval(database, synthetic_sa, tmp_path):
    from nilaris.drive155_journey import DriveJourney
    operator, ingestion, source, _, marker = journey(database, synthetic_sa, tmp_path)
    discovered = operator.drive_discover('synthetic-binding', 'discovery-one', 'page-one')
    assert discovered.get('state') == 'completed', discovered
    assert discovered['reserved_requests'] == 4
    assert discovered['page']['members'][0]['member'] == source.allowed_member
    assert marker.read_text().splitlines() == ['exchange']
    restored = DriveJourney(ingestion, {'synthetic-binding': source})
    assert restored.drive_discover('synthetic-binding', 'discovery-one', 'page-one') == discovered
    assert restored.drive_discover('synthetic-binding', 'discovery-one', 'page-one',
                                   expected_namespace=source.namespace) == discovered
    assert restored.drive_discover('synthetic-binding', 'replacement-id', 'page-two')['category'] == 'original_discovery_required'
    receipt = restored.drive_admit('synthetic-binding', 'discovery-one', 'page-one')
    assert receipt['totals'] == dict(offered=1, accepted=1, rejected=0, deferred=0), receipt
    job_id = receipt['members'][0]['job_id']
    before = ingestion.status(job_id)
    acquired = restored.drive_acquire('synthetic-binding', 'acquisition-one', job_id, before['revision'])
    assert acquired.get('state') == 'completed', acquired
    assert restored.drive_acquire('synthetic-binding', 'acquisition-one', job_id, before['revision'],
                                  expected_namespace=source.namespace) == acquired
    assert acquired['reserved_requests'] == 5
    captured = acquired['job']
    assert captured['state'] == 'queued', captured
    assert captured['counters']['acquisition_requests'] == 5
    assert captured['counters']['captured_input_bytes'] == 1505
    assert captured['usage']['cpu_seconds'] == 20
    assert acquired['reserved_cpu_seconds'] == 20
    assert acquired['accounting']['charged_cpu_seconds'] == 20
    assert captured['content_pin'] == source.allowed_member['expected_sha256']
    assert captured['attempts'][0]['completion_evidence'] == 'process_group_reaped'
    assert captured['attempts'][0]['accounting_complete'] is True
    assert marker.read_text().splitlines() == ['exchange', 'exchange']
    assert restored.drive_acquire('synthetic-binding', 'acquisition-one', job_id, before['revision']) == acquired
    assert restored.drive_acquire('synthetic-binding', 'replacement', job_id, captured['revision'])['category'] == 'original_acquisition_required'
    processed = ingestion.run_fixture(job_id, captured['revision'])
    assert processed.get('state') == 'completed', processed
    publication, = ingestion.result(job_id)['publications']
    assert publication['content_pin'] == source.allowed_member['expected_sha256']
    assert publication['approved'] is False
    assert restored.drive_operation('synthetic-binding', 'acquisition-one') == acquired


def test_root_unknown_discovery_holds_capacity_and_never_resends(database, synthetic_sa, tmp_path):
    from nilaris.drive155_journey import DriveJourney
    from test_admission import member
    operator, ingestion, source, provider, marker = journey(database, synthetic_sa, tmp_path)
    original = provider.get
    def hang(path, **kwargs):
        if path.startswith('/drive/v3/about'):
            time.sleep(10)
        return original(path, **kwargs)
    provider.get = hang
    operator.runner = SupervisedSource(test_wall_seconds=.3)
    held = operator.drive_discover('synthetic-binding', 'unknown-one', 'page-one')
    assert held['state'] == 'held', held
    assert held['active_capacity_reserved'] is True
    assert held['reserved_requests'] == 4
    assert marker.read_text().splitlines() == ['exchange']
    restored = DriveJourney(ingestion, {'synthetic-binding': source})
    assert restored.drive_discover('synthetic-binding', 'unknown-one', 'page-one') == held
    assert restored.drive_discover('synthetic-binding', 'new-id', 'new-page')['category'] == 'original_discovery_required'
    # Ordinary synthetic admission is allowed to queue, but its claim cannot
    # spend the source operation's still-held process capacity.
    ingestion.drive_policy = None
    receipt = ingestion.offer('ordinary', 'ordinary-one', [member()])
    job_id = receipt['members'][0]['job_id']
    assert ingestion.claim(job_id, 0)['category'] == 'active_capacity'
    assert marker.read_text().splitlines() == ['exchange']


def test_root_authority_replacement_keeps_old_stream_readable_and_denies_old_context(database, synthetic_sa, tmp_path):
    import copy
    from nilaris.drive155_journey import DriveJourney
    from nilaris.drive155_source import SourceBinding, AuthorizedDriveSource
    from test_drive155_source import configuration, authority, SyntheticProvider, TokenResponse
    operator, ingestion, source, _, marker = journey(database, synthetic_sa, tmp_path)
    old = operator.drive_discover('synthetic-binding', 'original', 'original-page')
    receipt = operator.drive_admit('synthetic-binding', 'original', 'original-page')
    job_id = receipt['members'][0]['job_id']
    config = copy.deepcopy(configuration())
    config['allowed_member']['case_id'] = 'replacement-case'
    binding = SourceBinding(config, authority(config))
    replacement = AuthorizedDriveSource(binding, credential_supplier=lambda key: synthetic_sa,
        transport=SyntheticProvider(binding.allowed_member), token_transport=lambda **kwargs: TokenResponse())
    fresh = DriveJourney(ingestion, {'replacement-binding': replacement, 'original-binding': source})
    ingestion.drive_policy = fresh
    assert fresh.drive_acquire('replacement-binding', 'bad-context', job_id, 0)['category'] == 'source_policy_denied'
    assert ingestion.discovery_read(source.namespace, 'original-page')['namespace'] == old['namespace']
    assert fresh.drive_operation('original-binding', 'original') == old
    assert fresh.drive_operation('replacement-binding', 'original')['known'] is False
    new = fresh.drive_discover('replacement-binding', 'original', 'original-page')
    assert new['state'] == 'completed', new
    assert new['namespace'] != old['namespace']
    assert fresh.drive_operation('original-binding', 'original') == old
    assert marker.read_text().splitlines() == ['exchange']


# Only generated offline key material. Root owns execution of DB/process tests.
from test_drive155_source import synthetic_sa


def test_live_factory_refuses_missing_or_wrong_activation_before_registry_read(tmp_path):
    import json
    import pytest
    from nilaris.drive155_service import configuration
    from test_drive155_source import configuration as source_configuration, authority
    source = source_configuration()
    paths = []
    for name, value in [('source', source), ('authority', authority(source))]:
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        paths.append(path)
    for digest in (None, '0' * 64):
        with pytest.raises(ValueError, match='source activation unavailable'):
            configuration(tmp_path / 'absent-command', *paths, tmp_path / 'absent-registry',
                          activation_digest=digest, repository_root=tmp_path / 'repo')


def test_factory_matching_authority_assembles_without_loading_key(tmp_path):
    import json
    from dataclasses import asdict
    from nilaris.admission import AdmissionProfile
    from nilaris.drive155_service import configuration
    from nilaris.drive155_source import binding_digest
    from test_drive155_source import configuration as source_configuration, authority
    source = source_configuration()
    grant = authority(source)
    paths = []
    values = [('command', dict(database={}, token='local-only-token-157',
                               admission_profile=asdict(AdmissionProfile.l1()))),
              ('source', source), ('authority', grant),
              ('registry', dict(version=1, references={source['credential_binding_id']: str(tmp_path / 'absent-key')}))]
    for name, value in values:
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        paths.append(path)
    private = configuration(*paths, activation_digest=binding_digest(grant), repository_root=tmp_path / 'repo')
    assert private.source_operator.permits(source['allowed_member'])
    assert private.drive is None
    assert not (tmp_path / 'absent-key').exists()


def test_root_http_source_capture_processes_and_refuses_finite_acquisition(command_process):
    import json
    from pathlib import Path
    import subprocess
    from test_command_service import PYTHON, ROOT, stop
    client, start, config = command_process
    # Ordinary listener startup exposes no provider activation path.
    ordinary = start()
    assert client(dict(operation='drive_discover', binding_id='synthetic-binding',
                       operation_id='disabled', page_id='disabled'))['category'] == 'live_source_disabled'
    stop(ordinary)
    process = subprocess.Popen([*PYTHON, str(ROOT / 'tests/drive155_command_process.py'),
                                '--controlled-offline', str(config)], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client(dict(operation='status', job_id='absent')).get('category') == 'not_found':
                break
            time.sleep(.05)
        else:
            raise AssertionError('controlled listener unavailable')
        listener = int(subprocess.check_output(['lsof', '-t', '-nP', '-iTCP:18765', '-sTCP:LISTEN'], text=True).strip())
        assert os.getpgid(listener) == process.pid
        discover = dict(operation='drive_discover', binding_id='synthetic-binding', operation_id='http-discover', page_id='http-page')
        assert client(discover).get('state') == 'completed'
        offered = client(dict(operation='drive_admit', binding_id='synthetic-binding', operation_id='http-discover', page_id='http-page'))
        job_id = offered['members'][0]['job_id']
        before = client(dict(operation='status', job_id=job_id))
        # Fixture execution must refuse live acquisition before spending a claim.
        assert client(dict(operation='execute_fixture_v1', job_id=job_id, expected_revision=before['revision']))['category'] == 'claim_conflict'
        assert client(dict(operation='status', job_id=job_id))['revision'] == before['revision']
        acquired = client(dict(operation='drive_acquire', binding_id='synthetic-binding', operation_id='http-acquire', job_id=job_id, expected_revision=before['revision']))
        assert acquired.get('state') == 'completed', acquired
        captured = acquired['job']
        acknowledgment = client(dict(operation='execute_fixture_v1', job_id=job_id, expected_revision=captured['revision']))
        assert acknowledgment['category'] == 'processing_acknowledged', acknowledgment
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status = client(dict(operation='status', job_id=job_id))
            if status.get('state') in ('completed', 'held', 'failed'):
                break
            time.sleep(.1)
        assert status.get('state') == 'completed', status
        assert client(dict(operation='result', job_id=job_id))['publications'][0]['approved'] is False
        assert client(dict(operation='drive_operation', binding_id='synthetic-binding', operation_id='http-acquire')) == acquired
    finally:
        stop(process)
