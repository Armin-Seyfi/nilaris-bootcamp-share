"""Source-free review definitions at the real HTTP adapter/persistence seam."""
from copy import deepcopy
import json

from test_review_manual import manual
from test_review_ui import command_process, review_browser, http, login, command


def test_manual_decimal_and_text_roundtrip_http_source_guards_and_immutable_approval(review_browser):
    ui = review_browser
    ui['start']()
    cookie = login()
    context = json.loads(http('GET', '/api/context', cookie=cookie)[2])
    namespace = context['namespace']
    created = command(cookie, dict(operation='review_create',namespace=namespace,
        command_id='create',job_id=ui['job'],proposal_id=ui['proposal'],
        product_type='Isolated UI test pump',expected_base=None,actor='isolated-manual-expert'))
    draft = created['draft_id']
    view = command(cookie, dict(operation='review_read',draft_id=draft))
    initial = deepcopy(view['initial'])
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
    view['definitions'].extend([manual(label='<expert & rating>'),
        manual('manual-text',label='Expert note',value_type='text',unit=None)])
    body = dict(operation='review_save',namespace=namespace,command_id='manual-save',
        draft_id=draft,expected_revision=0,definitions=view['definitions'],
        associations=view['associations'],actor='isolated-manual-expert',reason='Human additions')
    assert command(cookie,dict(operation='review_resolve',namespace=namespace,
        command_id='manual-save'))['category'] == 'unknown_command'
    first = command(cookie,body)
    assert first['revision'] == 1
    assert command(cookie,body) == first
    assert command(cookie,dict(operation='review_resolve',namespace=namespace,
        command_id='manual-save')) == first
    resumed = command(cookie,dict(operation='review_resume',draft_id=draft))
    assert 'scenario_review' not in resumed
    assert resumed['initial'] == initial
    assert resumed['associations'] == view['associations']
    assert resumed['definitions'][-2]['label'] == '<expert & rating>'
    forged = deepcopy(body)
    forged.update(command_id='forged-links',expected_revision=1)
    forged['definitions'][-1]['observation_ids'] = ['o-flow']
    refused = command(cookie,forged)
    assert refused['category'] == 'validation_error'
    assert refused['errors'][0]['field'] == 'definitions[4].observation_ids'
    assert command(cookie,dict(operation='review_read',draft_id=draft))['revision'] == 1
    resumed['definitions'][-1]['label'] = 'Edited text after reconnect'
    scenario = dict(version='scenario-review-v1',registry=resumed['available_scenario_registry'],
        references=[dict(attribute_id='manual-text',scenario_id='exact_repeat',
                         requirement='conditional',condition='When explicitly nominated.')])
    body.update(command_id='manual-edit',expected_revision=1,
        definitions=resumed['definitions'],scenario_review=scenario)
    assert command(cookie,body)['revision'] == 2
    exact = command(cookie,dict(operation='review_read',draft_id=draft,revision=2))
    assert exact['scenario_review'] == scenario
    for definition in exact['definitions'][-2:]:
        assert definition['definition_kind'] == 'human_manual_v1'
        assert definition['origin']['kind'] == 'human'
        assert definition['origin']['initial_candidate_id'] is None
        assert definition['origin']['source_is_proof_of_edit'] is False
        assert definition['observation_ids'] == []
    approved = command(cookie,dict(operation='review_approve',namespace=namespace,
        command_id='manual-approve',draft_id=draft,expected_revision=2,
        product_type='Isolated UI test pump',actor='isolated-manual-expert',explicit=True))
    assert approved['approved'] is True
    for _ in range(2):
        readback = command(cookie,dict(operation='review_read',draft_id=draft))
        assert readback['approved_schema']['definitions'][-2:] == exact['definitions'][-2:]
        assert readback['approved_schema']['scenario_review'] == scenario
        assert readback['initial'] == initial
    body.update(command_id='manual-locked',expected_revision=2)
    assert command(cookie,body)['category'] == 'revision_conflict'
