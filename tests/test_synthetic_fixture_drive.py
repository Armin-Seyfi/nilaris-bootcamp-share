"""Offline synthetic source boundary; no service or persistence substitute."""
import base64
import json
from pathlib import Path

import pytest

from nilaris.synthetic_fixture_drive import SyntheticFixtureDrive
from nilaris.execution import ControlledSource


PACKAGE = Path(__file__).resolve().parents[1] / 'deliverables/n8n/synthetic153'


def configuration():
    members = json.loads((PACKAGE / 'members.json').read_text())
    fixtures = PACKAGE.parents[2] / 'tests/fixtures/a1'
    return dict(mode='synthetic-fixtures-v1', entries=[
        dict(allowed_member=member, object=dict(
            source_id=member['source_id'], source_revision=member['source_revision'],
            source_root=member['source_root'],
            content_b64=base64.b64encode((fixtures / name).read_bytes()).decode('ascii')))
        for name, member in members.items() if name != 'delivery-renamed.pdf'
    ], aliases=[members['delivery-renamed.pdf']])


def test_inventory_factory_supplies_a_trusted_finite_capture_source():
    source = SyntheticFixtureDrive(configuration()).controlled_source()
    assert isinstance(source, ControlledSource)


@pytest.mark.parametrize('entry_index', [0, 1, 2], ids=['P', 'W', 'P2'])
def test_public_capture_accepts_inventory_and_pins_both_revisions(database, entry_index):
    from nilaris.admission import AdmissionProfile, Ingestion, migrate

    config = configuration()
    policy = SyntheticFixtureDrive(config)
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1(), drive_policy=policy)
    source = policy.controlled_source()
    member = config['entries'][entry_index]['allowed_member']
    receipt = ingestion.offer('synthetic-capture-regression', member['member_key'], [member])
    assert receipt['totals']['accepted'] == 1, receipt
    job_id = receipt['members'][0]['job_id']
    before = ingestion.status(job_id)
    claim = ingestion.claim(job_id, before['revision'])
    assert claim['claimed'] is True
    captured = ingestion.capture(job_id, claim, source)
    assert captured['state'] == 'queued', captured
    assert captured['reason'] == 'pinned_input_captured'
    assert captured['content_pin'] == member['expected_sha256']
    assert captured['counters']['acquisition_requests'] == 1
    assert captured['counters']['captured_input_bytes'] == member['input_bytes']


def test_exact_pdf_workbook_and_two_pdf_revisions_are_independently_readable():
    policy = SyntheticFixtureDrive(configuration())
    members = json.loads((PACKAGE / 'members.json').read_text())
    source = policy.controlled_source()
    for name, member in members.items():
        assert policy.permits(member)
        content = source.read(member['source_id'], member['source_revision'], member['input_bytes'])
        assert content == (PACKAGE.parents[2] / 'tests/fixtures/a1' / name).read_bytes()
    assert source.read('a1-source-P', 'latest', 1505) is None
    assert source.read('other-source', 'a1-P-r1', 1505) is None


def test_private_configuration_selects_inventory_without_raising_existing_size_cap(tmp_path):
    from nilaris.command_service import Configuration
    path = tmp_path / 'private.json'
    path.write_text(json.dumps(dict(database={}, token='synthetic-offline-token', drive=configuration())))
    path.chmod(0o600)
    assert path.stat().st_size <= 16384
    loaded = Configuration.load(path)
    assert loaded.drive.permits(configuration()['entries'][1]['allowed_member'])


@pytest.mark.parametrize('field,value', [
    ('source_root', 'other-root'), ('source_revision', 'latest'),
    ('document_id', 'other-document'), ('input_bytes', 1),
    ('expected_sha256', '0' * 64), ('member_key', 'invented-alias'),
    ('connector', 'drive'), ('case_id', 'customer-case')])
def test_changed_offer_is_denied_before_any_source_read(field, value):
    config = configuration()
    policy = SyntheticFixtureDrive(config)
    item = dict(config['entries'][0]['allowed_member'], **{field: value})
    assert policy.permits(item) is False


def test_config_mutation_cannot_change_frozen_policy_or_supply_a_new_revision():
    config = configuration()
    policy = SyntheticFixtureDrive(config)
    config['entries'][0]['allowed_member']['source_root'] = 'changed'
    assert policy.permits(config['entries'][0]['allowed_member']) is False
    source = policy.controlled_source()
    assert source.read('a1-source-P', 'a1-P-r1', 1504) is None
    assert source.read('a1-source-P', 'a1-P-r3', 8192) is None


@pytest.mark.parametrize('change', ['live', 'extra', 'duplicate', 'oversize', 'wrong_root', 'alias'])
def test_invalid_inventory_fails_closed(change):
    config = configuration()
    if change == 'live':
        config['mode'] = 'drive-readonly-prepared-v1'
    elif change == 'extra':
        config['credential'] = 'not-used'
    elif change == 'duplicate':
        config['entries'][2] = config['entries'][0]
    elif change == 'oversize':
        config['entries'][0]['object']['content_b64'] = base64.b64encode(b'x' * 8193).decode('ascii')
    elif change == 'wrong_root':
        config['entries'][0]['object']['source_root'] = 'other-root'
    else:
        config['aliases'][0]['expected_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        SyntheticFixtureDrive(config)


def test_private_preparation_preserves_input_and_refuses_existing_destination(tmp_path):
    import subprocess
    import sys
    original = tmp_path / 'original.json'
    original.write_text(json.dumps(dict(database={}, token='synthetic-offline-token')))
    before = original.read_bytes()
    output = tmp_path / 'new-private.json'
    command = [sys.executable, str(PACKAGE / 'prepare_inventory.py'), str(original), str(output)]
    prepared = subprocess.run(command, capture_output=True, text=True)
    assert prepared.returncode == 0
    assert 'synthetic-offline-token' not in prepared.stdout + prepared.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    from nilaris.command_service import Configuration
    assert Configuration.load(output).drive.permits(configuration()['entries'][0]['allowed_member'])
    retained = output.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode == 1
    assert original.read_bytes() == before
    assert output.read_bytes() == retained
