"""Offline command-contract checks; PostgreSQL composition is verified separately."""
import base64
import copy

import pytest

from nilaris.drive import PinnedDrive
from nilaris.intake167 import SyntheticAdapter, freeze_plan, plan_digest, run_once


def adapter():
    member = dict(member_key='P', connector='synthetic-drive-v1', source_id='native-P',
        source_revision='opaque-r7', source_root='root', expected_sha256='a' * 64,
        document_id='document-P', case_id='case', membership_revision='m1',
        product_type='schema', processing_contract='p1', reference_set='r1',
        selection_policy='s1', intent_id='i1', input_bytes=3,
        media_type='application/pdf', identity='native provenance')
    source = PinnedDrive(dict(mode='synthetic-drive-v1', allowed_member=member,
        object=dict(source_id='native-P', source_revision='opaque-r7', source_root='root',
                    content_b64=base64.b64encode(b'PDF').decode())))
    return SyntheticAdapter(source, member, dict(version=1, customer_id='customer',
        principal='fixture-principal', credential_binding_id='fixture', policy_version='p1',
        authority_id='owner-grant', source_root='root'))


def plan(source):
    return dict(version=1, adapter=source.snapshot, member=source.member,
        source_validator=None, provenance_locator='fixture://native-P/opaque-r7',
        discovery_operation='discover-1', acquisition_operation='acquire-1', page_id='page-1',
        phases=['discover', 'admit', 'acquire', 'process'],
        fences=dict(acquire=dict(revision=0, generation=0), process=dict(revision=2, generation=1)),
        limits=dict(command_calls=24, input_bytes=max(1024, source.member["input_bytes"])))


def test_unsupported_capability_refuses_before_dispatch():
    source = adapter()
    raw = plan(source)
    raw['adapter']['capabilities']['conditional_read'] = True
    calls = []
    result = run_once(raw, source, lambda c: calls.append(c), authority={})
    assert result['category'] == 'intake_plan_refused'
    assert calls == []


class StoredCommands:
    """Finite external command transport double, with durable public projections."""
    def __init__(self, source, *, state='acquiring', receipt=True, page_state='settled'):
        self.source = source
        self.calls = []
        self.receipt = receipt
        self.page_state = page_state
        self.state = state

    def __call__(self, request):
        self.calls.append(request)
        operation = request['operation']
        if operation == 'discovery_read':
            return dict(known=True, namespace=self.source.snapshot['namespace'], page_id='page-1',
                expected_cursor=None, next_cursor=None, state=self.page_state,
                members=[dict(member=self.source.member, disposition=None, pending=True)])
        if operation == 'resolve':
            if not self.receipt:
                return dict(known=False, category='transport_unavailable')
            return dict(known=True, receipt_stored=True, members=[dict(member_key='P',
                disposition='accepted', job_id='job-1')])
        if operation == 'status':
            return dict(known=True, id='job-1', input=self.source.member, state=self.state,
                        revision=0, generation=0)
        raise AssertionError('Unexpected effect: ' + operation)


def authorized(raw):
    return dict(version=1, plan_digest=plan_digest(raw), phases=raw['phases'])


def test_unknown_admission_outcome_is_never_resubmitted():
    source = adapter()
    raw = plan(source)
    commands = StoredCommands(source, receipt=False)
    result = run_once(raw, source, commands, authority=authorized(raw))
    assert result['category'] == 'intake_admission_unknown'
    assert {r['operation'] for r in commands.calls} == {'discovery_read', 'resolve'}


def test_lost_transport_response_stops_without_resending():
    source = adapter()
    raw = plan(source)
    calls = []
    def disconnected(request):
        calls.append(request)
        raise OSError('connection lost')
    result = run_once(raw, source, disconnected, authority=authorized(raw))
    assert result['category'] == 'intake_response_unavailable'
    assert len(calls) == 1


def test_cli_invalid_arguments_return_bounded_json(capsys):
    from nilaris.intake167 import main
    assert main(['run-once']) == 2
    import json
    assert json.loads(capsys.readouterr().out)['category'] == 'intake_configuration_unavailable'


def test_authority_boolean_version_is_not_a_version_number():
    source = adapter()
    raw = plan(source)
    grant = dict(authorized(raw), version=True)
    calls = []
    result = run_once(raw, source, lambda c: calls.append(c), authority=grant)
    assert result['category'] == 'intake_authority_required'
    assert calls == []


def test_original_drive_v1_namespace_remains_readable_without_credentials():
    from nilaris.intake167 import DriveAdapter
    from nilaris.drive155_source import SourceBinding
    from test_drive155_source import configuration, authority
    config = configuration()
    grant = authority(config)
    original = SourceBinding(config, grant)
    source = DriveAdapter(config, grant)
    assert source.snapshot['namespace'] == original.namespace
    assert source.snapshot['binding_digest'] == original.configuration_digest
    assert source.member['source_revision'] != source.member['expected_sha256']
    assert source.snapshot['capabilities']['conditional_read'] is False
    raw = plan(source)
    commands = []
    def pending(request):
        commands.append(request)
        return dict(known=True, state='pending', namespace=source.snapshot['namespace'])
    result = run_once(raw, source, pending, authority=authorized(raw))
    assert result['category'] == 'intake_operation_held'
    assert all(r['operation'] == 'drive_operation' for r in commands)


def test_frozen_plan_and_connection_snapshot_cannot_be_reinterpreted():
    source = adapter()
    raw = plan(source)
    frozen = freeze_plan(raw, source)
    original_digest = plan_digest(frozen)
    raw['member']['source_revision'] = 'latest'
    raw['adapter']['capabilities']['exact_revision'] = False
    assert plan_digest(frozen) == original_digest
    assert source.member['source_revision'] == 'opaque-r7'
    assert source.snapshot['capabilities']['exact_revision'] is True
    with pytest.raises(ValueError, match='intake plan unavailable'):
        freeze_plan(raw, source)


def test_running_job_is_inspected_without_another_processing_command():
    source = adapter()
    raw = plan(source)
    commands = StoredCommands(source, state='running')
    result = run_once(raw, source, commands, authority=authorized(raw))
    assert result['category'] == 'intake_incomplete'
    assert result['job']['state'] == 'running'
    assert {r['operation'] for r in commands.calls} == {'discovery_read', 'resolve', 'status'}


def test_malformed_discovery_member_refuses_without_effects():
    source = adapter()
    raw = plan(source)
    commands = StoredCommands(source)
    def malformed(request):
        value = commands(request)
        if request['operation'] == 'discovery_read':
            value['members'] = [None]
        return value
    result = run_once(raw, source, malformed, authority=authorized(raw))
    assert result['category'] == 'intake_identity_mismatch'
    assert {r['operation'] for r in commands.calls} == {'discovery_read', 'resolve'}


def test_drive_page_without_completed_original_discovery_is_held():
    from nilaris.intake167 import DriveAdapter
    from test_drive155_source import configuration, authority
    config = configuration()
    source = DriveAdapter(config, authority(config))
    raw = plan(source)
    commands = StoredCommands(source)
    def interrupted(request):
        if request['operation'] == 'drive_operation':
            commands.calls.append(request)
            return dict(known=False, category='source_operation_unknown', namespace=source.snapshot['namespace'])
        return commands(request)
    result = run_once(raw, source, interrupted, authority=authorized(raw))
    assert result['category'] == 'intake_operation_unknown'
    assert all(r['operation'] not in ('discovery_admit', 'discovery_reconcile', 'execute_fixture_v1')
               for r in commands.calls)


def test_rebound_drive_binding_cannot_dispatch_under_frozen_authority():
    from nilaris.intake167 import DriveAdapter
    from test_drive155_source import configuration, authority
    config = configuration()
    source = DriveAdapter(config, authority(config))
    raw = plan(source)
    calls = []
    def rebound(request):
        calls.append(request)
        if request['operation'] == 'drive_operation':
            return dict(known=False, category='source_operation_unknown', namespace='changed-authority')
        if request['operation'] in ('discovery_read', 'resolve'):
            return dict(known=False, category='unknown')
        raise AssertionError('Provider dispatch under rebound binding')
    result = run_once(raw, source, rebound, authority=authorized(raw))
    assert result['category'] == 'intake_identity_mismatch'
    assert all(r['operation'] == 'drive_operation' for r in calls)


@pytest.mark.parametrize('operation,fields', [
    ('drive_discover', dict(page_id='stale-page')),
    ('drive_acquire', dict(job_id='stale-job', expected_revision=0)),
])
def test_stale_namespace_is_refused_before_database_or_provider_effects(monkeypatch, operation, fields):
    from nilaris.drive155_journey import DriveJourney
    from nilaris.drive155_source import SourceBinding
    from test_drive155_source import configuration, authority
    config = configuration()
    grant = authority(config)
    original = SourceBinding(config, grant)
    rebound = SourceBinding(config, dict(grant, authority_id='rebound-owner-grant'))
    effects = []
    def database_tripwire(*args, **kwargs):
        effects.append('database')
        raise AssertionError('stale namespace reached durable storage')
    monkeypatch.setattr('nilaris.drive155_journey.psycopg.connect', database_tripwire)
    journey = DriveJourney(None, {config['binding_id']: rebound})
    result = getattr(journey, operation)(config['binding_id'], 'stale-operation',
                                        expected_namespace=original.namespace, **fields)
    assert result['category'] == 'source_namespace_mismatch'
    assert effects == []


def test_drive_mutations_carry_the_frozen_namespace_after_attestation():
    from nilaris.intake167 import DriveAdapter
    from test_drive155_source import configuration, authority
    config = configuration()
    source = DriveAdapter(config, authority(config))
    raw = plan(source)
    assert source.discover(raw)['expected_namespace'] == source.snapshot['namespace']
    assert source.acquire(raw, dict(id='job-1', revision=0))['expected_namespace'] == source.snapshot['namespace']
