"""Guard mode selection is exercised without Docker or credentials."""
import importlib.util
from pathlib import Path

import pytest


def guard():
    path = Path(__file__).resolve().parents[1] / 'runtime/a1/service_guard.py'
    spec = importlib.util.spec_from_file_location('guard167', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_postgres_selection_preserves_explicit_original_owner():
    module = guard()
    selection = module.select_runtime('postgres', '/private/tmp/nilaris-a1-01/runtime/a1')
    assert selection['services'] == ['postgres']
    assert selection['ports'] == [55432]
    assert selection['owner'] == '/private/tmp/nilaris-a1-01/runtime/a1'


def test_unrelated_compose_owner_is_refused():
    with pytest.raises(ValueError):
        guard().select_runtime('postgres', '/private/tmp/unrelated/runtime/a1')


@pytest.mark.parametrize('fail_start', [False, True])
def test_postgres_cli_starts_and_stops_only_selected_services_and_owned_reader(monkeypatch, fail_start):
    module = guard()
    calls, mutations = [], []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): assert address == ('127.0.0.1', 55432)
    def output(args, **kwargs):
        calls.append(args)
        if args[:3] == ['docker','ps','-aq']:
            return 'old-n8n\n' if args[-1].endswith('=nilaris-synth') else ''
        if args[:3] == ['docker','volume','inspect']:
            return '{"com.docker.compose.project":"nilaris-synth","com.docker.compose.volume":"n8ndata"}'
        if args[:2] == ['docker','inspect']: return '/private/tmp/nilaris-a1-01/runtime/a1'
        if args[:2] == ['docker','create']: return 'owned-reader'
        if 'up' in args and fail_start: raise module.subprocess.CalledProcessError(1, args)
        return ''
    monkeypatch.setattr(module.subprocess, 'check_output', output)
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kwargs: mutations.append(args))
    monkeypatch.setattr(module.socket, 'socket', Socket)
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 4*1024**3})())
    monkeypatch.setattr(module, 'publish_service_monitor', lambda _: None)
    ticks = iter([0, 2])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(module.sys, 'argv', ['guard', '--seconds','1','--services','postgres',
                         '--compose-owner','/private/tmp/nilaris-a1-01/runtime/a1'])
    if fail_start:
        with pytest.raises(module.subprocess.CalledProcessError): module.main()
    else:
        module.main()
    up = next(c for c in calls if 'up' in c)
    assert up[-1] == 'postgres' and 'n8n' not in up
    assert mutations[0][-4:] == ['stop','-t','2','postgres']
    assert mutations[1] == ['docker','rm','-f','owned-reader']
    create = next(c for c in calls if c[:2] == ['docker','create'])
    assert '--read-only' in create and 'none' in create and '--pids-limit' in create
    assert 'type=volume,src=nilaris-synth_n8ndata,dst=/retained,readonly' in create
