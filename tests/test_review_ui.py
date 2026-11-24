"""Local browser HTTP seam over existing real command HTTP and isolated review records."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from test_command_service import PYTHON, ROOT, command_process, stop
from test_review import published

UI_ORIGIN = 'http://127.0.0.1:18766'


@pytest.fixture
def review_browser(command_process, database):
    client, start_backend, config = command_process
    job, proposal = published(database, product_type='Isolated UI test pump')
    start_backend()
    processes = []

    def start(draft_id=None):
        args = [*PYTHON, '-m', 'nilaris.review_ui', '--config', str(config),
                '--job-id', job, '--seconds', '120']
        if draft_id:
            args += ['--draft-id', draft_id]
        process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        processes.append(process)
        for _ in range(100):
            if process.poll() is not None:
                break
            try:
                code, _, _ = http('GET', '/')
                if code == 200:
                    return process
            except OSError:
                pass
            time.sleep(.05)
        pytest.fail('local review UI did not become available; diagnostics withheld')

    try:
        yield dict(start=start, job=job, proposal=proposal, backend=client)
    finally:
        for process in processes:
            stop(process)


def http(method, path, body=None, cookie=None, origin=UI_ORIGIN):
    connection = HTTPConnection('127.0.0.1', 18766, timeout=5)
    headers = {'Origin': origin, 'Content-Type': 'application/json'}
    if cookie:
        headers['Cookie'] = cookie
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read(200000)
        return response.status, dict(response.getheaders()), raw
    finally:
        connection.close()


def login():
    code, headers, _ = http('POST', '/api/session', {'token': 'synthetic-test-token'})
    assert code == 200
    assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    return headers['Set-Cookie'].split(';')[0]


def command(cookie, body):
    code, _, raw = http('POST', '/api/command', body, cookie)
    assert code == 200
    return json.loads(raw)


def test_expert_can_save_reload_and_explicitly_approve_actual_proposal(review_browser):
    ui = review_browser
    process = ui['start']()
    assert http('GET', '/api/context')[0] == 401
    cookie = login()
    code, _, raw = http('GET', '/api/context', cookie=cookie)
    context = json.loads(raw)
    assert code == 200 and context['job_id'] == ui['job']
    assert b'synthetic-test-token' not in raw
    result = command(cookie, dict(operation='result', job_id=ui['job']))
    initial, = result['publications']
    assert initial['observations'][0]['original_content'] == 'Flow rate: 12 m3/h'
    created = command(cookie, dict(operation='review_create', namespace=context['namespace'],
        command_id='create', job_id=ui['job'], proposal_id=ui['proposal'],
        product_type='Isolated UI test pump', expected_base=None, actor='isolated-ui-expert'))
    draft = created['draft_id']
    view = command(cookie, dict(operation='review_read', draft_id=draft))
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
    view['definitions'][0]['label'] = 'Rated flow rate'
    saved = command(cookie, dict(operation='review_save', namespace=context['namespace'],
        command_id='save-1', draft_id=draft, expected_revision=0, definitions=view['definitions'],
        associations=view['associations'], actor='isolated-ui-expert', reason='Expert correction'))
    assert saved['saved'] and saved['revision'] == 1
    stop(process)
    ui['start'](draft)
    cookie = login()
    reloaded = command(cookie, dict(operation='review_resume', draft_id=draft))
    assert reloaded['definitions'][0]['label'] == 'Rated flow rate'
    assert reloaded['initial']['candidates'][0]['label'] == 'Flow rate'
    assert reloaded['definitions'][0]['origin']['kind'] == 'human'
    assert reloaded['approved_schema'] is None
    approved = command(cookie, dict(operation='review_approve', namespace=context['namespace'],
        command_id='approve-1', draft_id=draft, expected_revision=1,
        product_type='Isolated UI test pump', actor='isolated-ui-expert', explicit=True))
    assert approved['approved'] is True
    final = command(cookie, dict(operation='review_read', draft_id=draft))
    assert final['approved_schema']['id'] == approved['approved_version']
    assert final['approved_schema']['definitions'][0]['label'] == 'Rated flow rate'
    rejected = command(cookie, dict(operation='review_save', namespace=context['namespace'],
        command_id='after-approval', draft_id=draft, expected_revision=1, definitions=[], associations=[],
        actor='isolated-ui-expert', reason='Attempt immutable edit'))
    assert rejected['category'] == 'revision_conflict'
    assert command(cookie, dict(operation='review_read', draft_id=draft))['approved_schema'] == final['approved_schema']


def test_browser_boundary_rejects_cross_origin_scope_and_processing(review_browser):
    ui=review_browser
    ui['start']()
    assert http('POST','/api/session',{'token':'wrong-token'})[0]==401
    assert http('POST','/api/session',{'token':'synthetic-test-token'},origin='http://evil.invalid')[0]==403
    cookie=login()
    context=json.loads(http('GET','/api/context',cookie=cookie)[2])
    for body in [dict(operation='execute_fixture_v1',job_id=ui['job'],expected_revision=0),
                 dict(operation='result',job_id='another-job'),
                 dict(operation='review_read',draft_id='another-draft'),
                 dict(operation='review_resolve',namespace='another-namespace',command_id='create')]:
        assert http('POST','/api/command',body,cookie)[0]==400
    body=dict(operation='review_create',namespace=context['namespace'],command_id='create',
              job_id=ui['job'],proposal_id=ui['proposal'],product_type='Isolated UI test pump',
              expected_base=None,actor='isolated-ui-expert')
    assert http('POST','/api/command',body,cookie,origin='http://evil.invalid')[0]==403
    assert command(cookie,dict(operation='review_resolve',namespace=context['namespace'],command_id='create'))['known'] is False
    for path in ('/','/app.js','/style.css','/reference-base.css','/reference-refined-b.css'):
        code,headers,raw=http('GET',path)
        assert code==200 and headers['Cache-Control']=='no-store'
        assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
        assert b'synthetic-test-token' not in raw
    assert http('GET','/../../command.json')[0]==404
    assert ui['backend'](dict(operation='status',job_id=ui['job']))['state']=='completed'


def test_lost_create_response_can_be_resolved_without_duplicate_draft(review_browser):
    ui=review_browser
    process=ui['start']()
    cookie=login()
    context=json.loads(http('GET','/api/context',cookie=cookie)[2])
    body=dict(operation='review_create',namespace=context['namespace'],command_id='create',
              job_id=ui['job'],proposal_id=ui['proposal'],product_type='Isolated UI test pump',
              expected_base=None,actor='isolated-ui-expert')
    # Discard the response after a real HTTP request; resolve the same durable key.
    http('POST','/api/command',body,cookie)
    stop(process)
    ui['start']()
    cookie=login()
    resolved=command(cookie,dict(operation='review_resolve',namespace=context['namespace'],command_id='create'))
    assert resolved['saved'] and resolved['revision']==0
    read=command(cookie,dict(operation='review_read',draft_id=resolved['draft_id']))
    assert read['initial']['id']==ui['proposal'] and read['approved_schema'] is None
    assert command(cookie,body)==resolved


def test_malformed_scope_fields_fail_closed_without_dropping_response(review_browser):
    review_browser['start']()
    cookie=login()
    for body in ({'operation': []}, {'operation':'review_read','draft_id': []}):
        code,_,raw=http('POST','/api/command',body,cookie)
        assert code==400
        assert json.loads(raw)['category']=='unsupported_ui_command'
