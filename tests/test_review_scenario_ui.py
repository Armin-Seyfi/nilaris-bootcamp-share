"""Scenario package at the actual browser HTTP→review persistence seam."""
from copy import deepcopy
import json

from test_review_ui import command_process, review_browser, http, login, command


def test_scenario_snapshot_roundtrips_http_and_atomic_approved_readback(review_browser):
    ui = review_browser
    ui['start']()
    cookie = login()
    context = json.loads(http('GET', '/api/context', cookie=cookie)[2])
    created = command(cookie, dict(operation='review_create',
        namespace=context['namespace'], command_id='create', job_id=ui['job'],
        proposal_id=ui['proposal'], product_type='Isolated UI test pump',
        expected_base=None, actor='isolated-scenario-expert'))
    draft = created['draft_id']
    view = command(cookie, dict(operation='review_read', draft_id=draft))
    assert 'scenario_review' not in view
    assert [s['id'] for s in view['available_scenario_registry']['scenarios']] == [
        'new_project', 'exact_repeat', 'equivalent_replacement']
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
    scenario = dict(version='scenario-review-v1', registry=deepcopy(view['available_scenario_registry']),
        references=[dict(attribute_id='flow', scenario_id='exact_repeat',
                         requirement='conditional', condition='When the nominated specification includes it.')])
    body = dict(operation='review_save', namespace=context['namespace'], command_id='scenario-save',
        draft_id=draft, expected_revision=0, definitions=view['definitions'],
        associations=view['associations'], actor='isolated-scenario-expert',
        reason='Explicit scenario review', scenario_review=scenario)
    saved = command(cookie, body)
    assert saved['saved'] is True
    assert command(cookie, body) == saved
    fresh = command(cookie, dict(operation='review_resume', draft_id=draft))
    assert fresh['scenario_review'] == scenario
    assert command(cookie, dict(operation='review_read', draft_id=draft, revision=0)).get('scenario_review') is None
    changed = deepcopy(body)
    changed.update(command_id='blank-conditional', expected_revision=1)
    changed['scenario_review']['references'][0]['condition'] = ' '
    assert command(cookie, changed)['saved'] is False
    assert command(cookie, dict(operation='review_read', draft_id=draft))['revision'] == 1
    approved = command(cookie, dict(operation='review_approve', namespace=context['namespace'],
        command_id='scenario-approve', draft_id=draft, expected_revision=1,
        product_type='Isolated UI test pump', actor='isolated-scenario-expert', explicit=True))
    assert approved['approved'] is True
    final = command(cookie, dict(operation='review_read', draft_id=draft))['approved_schema']
    assert final['scenario_review'] == scenario
    assert command(cookie, dict(operation='review_read', draft_id=draft))['approved_schema'] == final
    changed.update(command_id='locked-save', scenario_review=scenario)
    assert command(cookie, changed)['category'] == 'revision_conflict'
    assert command(cookie, dict(operation='review_read', draft_id=draft))['approved_schema'] == final
