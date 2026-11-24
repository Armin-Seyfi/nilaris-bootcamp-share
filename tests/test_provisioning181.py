"""Operator boundary against a fresh root-owned disposable cluster only."""
import json
import hmac
import os
import uuid
from pathlib import Path

import pytest

from nilaris.provisioning181 import OperatorProvisioner, ControlledOperatorVerifier


@pytest.fixture
def provisioner():
    path = os.environ.get('NILARIS_PROVISION181_TEST_CONFIG')
    if not path:
        pytest.skip('fresh isolated provisioning cluster not supplied')
    config = json.loads(Path(path).read_text())
    return OperatorProvisioner(config, ControlledOperatorVerifier({'synthetic-operator-token': 'operator-a'}))


def test_operator_provisions_empty_customer_and_reads_durable_status(provisioner):
    ready = provisioner.provision('synthetic-operator-token', 'empty-customer-a')
    assert ready['state'] == 'ready'
    assert ready['business_rows'] == 0
    assert ready['runtime_restricted'] is True
    assert provisioner.status('synthetic-operator-token', 'empty-customer-a') == ready


def test_customer_mode_refuses_fixtures_but_explicit_fixture_mode_loads(provisioner):
    provisioner.provision('synthetic-operator-token', 'fixture-refusal-customer')
    with pytest.raises(PermissionError, match='fixture mode'):
        provisioner.load_fixtures('synthetic-operator-token', 'fixture-refusal-customer')
    fixture = provisioner.provision('synthetic-operator-token', 'explicit-fixture', mode='fixture')
    loaded = provisioner.load_fixtures('synthetic-operator-token', 'explicit-fixture')
    assert fixture['state'] == 'ready'
    assert loaded['fixture_job']['state'] == 'held'
    assert loaded['fixture_job']['input']['document_id'] == 'synthetic-fixture-document'
    assert provisioner.load_fixtures('synthetic-operator-token', 'explicit-fixture') == loaded


def test_unmonitored_provisioning_refuses_before_allocating(provisioner, tmp_path):
    config = dict(provisioner.config, service_monitor_path=str(tmp_path / 'missing-monitor'))
    unmonitored = OperatorProvisioner(config, provisioner.verifier)
    operation_id = 'unmonitored-' + uuid.uuid4().hex
    with pytest.raises(RuntimeError, match='monitor'):
        unmonitored.provision('synthetic-operator-token', operation_id)
    assert provisioner.status('synthetic-operator-token', operation_id)['state'] == 'absent'


def test_interrupted_secret_allocation_remains_in_inventory_without_regeneration(provisioner, monkeypatch):
    operation_id = 'secret-interruption-' + uuid.uuid4().hex
    original_open = os.open
    allocated_paths = []

    class ControlledInterruption(BaseException):
        pass

    def interrupt_migration_secret(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and str(path).endswith('.json'):
            allocated_paths.append(Path(path))
            if str(path).endswith('.migration.json'):
                raise ControlledInterruption()
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', interrupt_migration_secret)
        with pytest.raises(ControlledInterruption):
            provisioner.provision('synthetic-operator-token', operation_id)

    interrupted = provisioner.status('synthetic-operator-token', operation_id)
    assert interrupted['state'] == 'provisioning'
    assert provisioner.directory / interrupted['secret_ref'] == allocated_paths[0]
    assert provisioner.directory / interrupted['migration_secret_ref'] == allocated_paths[1]
    assert interrupted in provisioner.inventory('synthetic-operator-token')
    runtime_bytes = allocated_paths[0].read_bytes()
    retried = provisioner.provision('synthetic-operator-token', operation_id)
    assert retried['state'] == 'held'
    assert retried['reason'].startswith('secrets_')
    for field in ('customer_id', 'database_name', 'runtime_role', 'migration_role',
                  'secret_ref', 'migration_secret_ref'):
        assert retried[field] == interrupted[field]
    assert allocated_paths[0].read_bytes() == runtime_bytes
    assert not allocated_paths[1].exists()
    assert provisioner.provision('synthetic-operator-token', operation_id) == retried
    assert retried in provisioner.inventory('synthetic-operator-token')


@pytest.mark.parametrize('cold_process', [False, True], ids=['parent-first', 'cold-process'])
def test_crash_after_resources_reconciles_same_customer_and_fresh_process_status(provisioner, tmp_path, cold_process):
    import subprocess
    operation_id = 'crash-' + uuid.uuid4().hex
    class ControlledInterruption(BaseException):
        pass
    def interrupt_after_resources(stage, interrupted_id):
        raise ControlledInterruption()
    interrupted_operator = OperatorProvisioner(provisioner.config, provisioner.verifier,
        fault=interrupt_after_resources)
    if not cold_process:
        with pytest.raises(ControlledInterruption):
            interrupted_operator.provision('synthetic-operator-token', operation_id)
    before_process_crash = provisioner.status('synthetic-operator-token', operation_id)
    assert before_process_crash['state'] == ('absent' if cold_process else 'provisioning')
    config_snapshot = tmp_path / 'crash-config.json'
    config_snapshot.write_text(json.dumps(provisioner.config))
    config_snapshot.chmod(0o600)
    script = tmp_path / 'crash.py'
    script.write_text('''import json, os
from pathlib import Path
from nilaris.provisioning181 import OperatorProvisioner, ControlledOperatorVerifier
config = json.loads(Path(os.environ['NILARIS_PROVISION181_TEST_CONFIG']).read_text())
def crash(stage, operation):
    os._exit(91)
p = OperatorProvisioner(config, ControlledOperatorVerifier({'synthetic-operator-token':'operator-a'}), fault=crash)
print(json.dumps(p.provision('synthetic-operator-token', os.environ['PROVISION181_CRASH_OPERATION'])))
''')
    launcher = ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris',
                'python', 'runtime/a1/with_overlay.py']
    crashed = subprocess.run([*launcher, str(script)], env=dict(os.environ,
        PROVISION181_CRASH_OPERATION=operation_id, NILARIS_PROVISION181_TEST_CONFIG=str(config_snapshot)), capture_output=True, timeout=30)
    assert crashed.returncode == 91, crashed.stdout.decode('utf-8', errors='replace')
    interrupted = provisioner.status('synthetic-operator-token', operation_id)
    assert interrupted['state'] == 'provisioning'
    if not cold_process:
        assert interrupted == before_process_crash
    assert interrupted in provisioner.inventory('synthetic-operator-token')
    ready = provisioner.provision('synthetic-operator-token', operation_id)
    assert ready['state'] == 'ready'
    assert ready['customer_id'] == interrupted['customer_id']
    assert ready['database_name'] == interrupted['database_name']
    assert provisioner.provision('synthetic-operator-token', operation_id) == ready
    config_path = tmp_path / 'cli-config.json'
    config_path.write_text(json.dumps(dict(provisioner.config,
        authentication_mode='controlled-test-only',
        controlled_credentials={'synthetic-operator-token': 'operator-a'})))
    config_path.chmod(0o600)
    fresh = subprocess.run([*launcher, '-m', 'nilaris.provisioning181', '--config', str(config_path)],
        input=json.dumps(dict(action='status', credential='synthetic-operator-token', operation_id=operation_id)),
        text=True, capture_output=True, timeout=30)
    assert fresh.returncode == 0
    assert json.loads(fresh.stdout) == ready


def test_held_customer_does_not_block_other_customer_or_change_operation(provisioner):
    held_id = 'held-' + uuid.uuid4().hex
    def fail_after_resources(stage, operation_id):
        raise RuntimeError('controlled synthetic failure')
    failing = OperatorProvisioner(provisioner.config, provisioner.verifier, fault=fail_after_resources)
    held = failing.provision('synthetic-operator-token', held_id)
    assert held['state'] == 'held'
    assert provisioner.provision('synthetic-operator-token', held_id) == held
    other_id = 'independent-' + uuid.uuid4().hex
    other = provisioner.provision('synthetic-operator-token', other_id)
    assert other['state'] == 'ready'
    assert other['business_rows'] == 0
    for field in ('customer_id', 'database_name', 'runtime_role', 'secret_ref', 'storage_namespace', 'worker_namespace'):
        assert other[field] != held[field]
    with pytest.raises(PermissionError, match='alteration'):
        provisioner.provision('synthetic-operator-token', other_id, mode='fixture')
    assert provisioner.status('synthetic-operator-token', other_id) == other


def test_operator_authority_precedes_resource_or_status_lookup(provisioner):
    with pytest.raises(PermissionError, match='authentication'):
        provisioner.provision('forged-role-admin', 'forged-customer')
    with pytest.raises(PermissionError, match='authentication'):
        provisioner.status('forged-role-admin', 'empty-customer-a')
    another = OperatorProvisioner(provisioner.config,
        ControlledOperatorVerifier({'other-token': 'operator-b'}))
    with pytest.raises(PermissionError, match='ownership'):
        another.provision('other-token', 'empty-customer-a')
    with pytest.raises(PermissionError, match='ownership'):
        another.status('other-token', 'empty-customer-a')
    assert another.inventory('other-token') == []


def test_cluster_binding_and_retained_target_are_refused(provisioner):
    wrong_cluster = OperatorProvisioner(dict(provisioner.config, system_identifier='0'), provisioner.verifier)
    with pytest.raises(RuntimeError, match='ownership binding'):
        wrong_cluster.provision('synthetic-operator-token', 'wrong-cluster')
    with pytest.raises(RuntimeError, match='dedicated control'):
        OperatorProvisioner(dict(provisioner.config,
            admin=dict(provisioner.admin, dbname='nilaris_demo')), provisioner.verifier)
    assert provisioner.status('synthetic-operator-token', 'wrong-cluster')['state'] == 'absent'


def test_real_runtime_role_is_isolated_restricted_and_replay_preserves_data(provisioner):
    import psycopg
    from psycopg import sql
    a = provisioner.provision('synthetic-operator-token', 'roles-a-' + uuid.uuid4().hex)
    b = provisioner.provision('synthetic-operator-token', 'roles-b-' + uuid.uuid4().hex)
    assert a['state'] == b['state'] == 'ready'
    secret_a = json.loads((provisioner.directory / a['secret_ref']).read_text())
    secret_b = json.loads((provisioner.directory / b['secret_ref']).read_text())
    password_a = secret_a.get('password', secret_a.get('runtime'))
    password_b = secret_b.get('password', secret_b.get('runtime'))
    credentials_differ = not hmac.compare_digest(password_a, password_b)
    assert credentials_differ
    options = dict(provisioner.admin, user=a['runtime_role'], password=password_a, dbname=a['database_name'])
    with psycopg.connect(**options, autocommit=True) as connection:
        assert connection.execute('SELECT count(*) FROM admission_jobs').fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('CREATE TABLE public.forbidden_runtime_ddl (id text)')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(a['migration_role'])))
        connection.execute("INSERT INTO admission_jobs (id,input,state,reservation_bytes) VALUES ('synthetic-preserved','{}','held',8388608)")
    assert provisioner.provision('synthetic-operator-token', a['operation_id']) == a
    with psycopg.connect(**options) as connection:
        assert connection.execute("SELECT count(*) FROM admission_jobs WHERE id='synthetic-preserved'").fetchone()[0] == 1
    for forbidden in (b['database_name'], provisioner.admin['dbname'], 'postgres', 'template1'):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(**dict(options, dbname=forbidden)).close()
