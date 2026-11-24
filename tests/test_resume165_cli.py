"""Public resume CLI failures with synthetic private files; no listener or DB."""
import json
from pathlib import Path
import subprocess

import pytest

from test_drive155_source import configuration, authority
from test_resume165 import resume


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris',
          'python', str(ROOT / 'runtime/a1/with_overlay.py')]


@pytest.mark.parametrize('arguments', [[], ['--manifest', 'private-argument-marker', '--unknown']])
def test_invalid_resume_cli_arguments_have_safe_error_envelope(arguments):
    response = subprocess.run([*PYTHON, '-m', 'nilaris.resume165', *arguments],
                              capture_output=True, text=True, cwd=ROOT, timeout=15)
    assert response.returncode == 2
    # Conda adds a diagnostic after the child's stderr on nonzero exit.
    assert json.loads(response.stderr.splitlines()[0]) == dict(
        known=True, category='invalid_request', affected={}, coverage='unknown',
        reason='Command syntax, route or bounded metadata is invalid.', next_action='correct_request')
    assert response.stdout == ''
    assert 'usage:' not in response.stderr


def private_configuration(tmp_path, *, valid):
    source = configuration()
    key = tmp_path / 'private-google-key-marker.json'
    values = dict(source=source, authority=authority(source), manifest=resume().manifest,
                  command=dict(database={} if valid else 'private-config-body-marker',
                               token='synthetic-resume-token', google_key_path=str(key)))
    arguments = []
    for name, value in values.items():
        path = tmp_path / (name + '-private-path-marker.json')
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        arguments.extend(['--command-config' if name == 'command' else '--' + name, str(path)])
    return arguments, key


def run_startup_probe(tmp_path, arguments, key):
    """Audit only OS boundaries: refuse key opens and prevent any socket bind."""
    key_opened = tmp_path / 'key-opened'
    bind_attempted = tmp_path / 'bind-attempted'
    launcher = tmp_path / 'cli_probe.py'
    launcher.write_text('''
import runpy, sys
key, key_opened, bind_attempted, arguments = %r
def audit(event, args):
    if event == 'open' and args[0] == key:
        with open(key_opened, 'w') as marker:
            marker.write('credential open attempted')
        raise AssertionError('private-key-error-marker')
    if event == 'socket.bind':
        with open(bind_attempted, 'w') as marker:
            marker.write('listener refused before bind')
        raise OSError('private-listener-error-marker')
sys.addaudithook(audit)
sys.argv = ['nilaris.resume165', *arguments]
runpy.run_module('nilaris.resume165', run_name='__main__')
''' % ((str(key), str(key_opened), str(bind_attempted), arguments),))
    response = subprocess.run([*PYTHON, str(launcher)], capture_output=True, text=True,
                              cwd=ROOT, timeout=15)
    assert not key_opened.exists(), 'resume CLI attempted to open the Google key'
    assert response.returncode == 1
    assert response.stdout == ''
    for private in (str(key), *arguments[1::2], 'private-config-body-marker',
                    'private-key-error-marker', 'private-listener-error-marker'):
        assert private not in response.stderr
    return response, bind_attempted


def test_resume_startup_configuration_failure_has_safe_error_without_key_access(tmp_path):
    arguments, key = private_configuration(tmp_path, valid=False)
    response, bind_attempted = run_startup_probe(tmp_path, arguments, key)
    assert not bind_attempted.exists()
    assert json.loads(response.stderr.splitlines()[0]) == dict(
        known=True, category='configuration_unavailable', affected={}, coverage='unknown',
        reason='Private command configuration is unavailable or invalid.',
        next_action='configure_approved_private_configuration')


def test_resume_startup_listener_failure_has_safe_error_without_binding(tmp_path):
    arguments, key = private_configuration(tmp_path, valid=True)
    response, bind_attempted = run_startup_probe(tmp_path, arguments, key)
    assert bind_attempted.exists()
    assert json.loads(response.stderr.splitlines()[0]) == dict(
        known=True, category='listener_unavailable', affected={}, coverage='unknown',
        reason='Command listener is unavailable.', next_action='restore_authorized_loopback_listener')
