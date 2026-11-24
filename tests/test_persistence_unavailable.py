"""Public failure behavior using harmless credentials and an unavailable endpoint."""
import socket

from nilaris.admission import AdmissionProfile, Ingestion


def test_unavailable_persistence_never_claims_acceptance_or_absence():
    # Reserve a non-listening endpoint so no unrelated local service is contacted.
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))
        client = Ingestion(dict(host='127.0.0.1', port=endpoint.getsockname()[1],
                                user='diagnostic_probe', password='harmless_probe',
                                connect_timeout=1, dbname='diagnostic_probe'), AdmissionProfile.l1())
        expected = dict(known=False, category='persistence_unavailable',
                        reason='durable outcome unavailable', next_action='retry_same_key')
        assert client.offer(namespace='probe', command_id='probe',
                            members=[dict(member_key='m1')]) == dict(
            expected, affected=dict(namespace='probe', command_id='probe', member_keys=['m1']),
            coverage='unknown')
        assert client.resolve('probe', 'probe') == dict(
            expected, affected=dict(namespace='probe', command_id='probe'), coverage='unknown')
        assert client.status('probe') == dict(
            known=False, category='persistence_unavailable',
            affected=dict(job_id='probe'), coverage='unknown',
            reason='job status unavailable', next_action='retry_status')


def test_verbose_failed_diagnostics_do_not_expose_connection_credentials(tmp_path):
    """Tooling check, not proof of database durability or arbitrary caller privacy."""
    import os
    from pathlib import Path
    import subprocess
    import uuid

    sentinel = uuid.uuid4().hex
    secret_file = tmp_path / 'sentinel'
    secret_file.write_text(sentinel)
    secret_file.chmod(0o600)
    # Exercise real connection exceptions, including both fixture command paths.
    probe = tmp_path / 'test_probe.py'
    probe.write_text('''
from pathlib import Path
import pytest
from conftest import Database, database_command
from nilaris.admission import AdmissionProfile, Ingestion, migrate

@pytest.fixture
def options():
    return Database(host='127.0.0.1', port=PORT, user='diagnostic_probe',
                    password=Path(SECRET_PATH).read_text(), connect_timeout=1,
                    dbname='diagnostic_probe')

def test_migration(options):
    migrate(options)

@pytest.mark.parametrize('action', ['CREATE', 'DROP'])
def test_fixture_connection(options, action):
    command_options = Database(options)
    del command_options['dbname']
    database_command(command_options, 'nilaris_test_diagnostic', action)

def test_client_diagnostic(options):
    client = Ingestion(options, AdmissionProfile.l1())
    assert client.offer('probe', 'probe', [dict(member_key='probe')])['known'] is True
'''.replace('SECRET_PATH', repr(str(secret_file))))
    fixture_source = Path(__file__).with_name('conftest.py')
    (tmp_path / 'conftest.py').write_text(fixture_source.read_text())
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))
        probe.write_text(probe.read_text().replace('PORT', str(endpoint.getsockname()[1])))
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        result = subprocess.run(
            ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris',
             'python', 'runtime/a1/with_overlay.py', '-m', 'pytest',
             str(probe), '-q', '--tb=long', '--showlocals'],
            env=environment, capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert '4 failed' in output
    assert sentinel not in output
    assert 'migration persistence_unavailable' in output
    assert 'fixture persistence_unavailable' in output
    assert 'psycopg/connection.py' not in output
