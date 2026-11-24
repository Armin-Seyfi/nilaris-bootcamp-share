"""Scenario references exercised through isolated public Review commands."""
from copy import deepcopy

from nilaris.review import Review
from test_review import decided, published


REGISTRY = dict(version='review-scenarios-v1', scenarios=[
    dict(id='new_project', label='New project', meaning='Specify equipment for a new project without assuming an existing equipment specification as its baseline.'),
    dict(id='exact_repeat', label='Exact repeat', meaning='Repeat a nominated equipment specification; this is not duplicate-document or duplicate-RFQ identity.'),
    dict(id='equivalent_replacement', label='Equivalent replacement', meaning='Consider replacement equipment that may differ from the original while meeting the stated need; this label does not establish engineering equivalence.'),
])


def scenario_review(references=None):
    return dict(version='scenario-review-v1', registry=deepcopy(REGISTRY),
                references=[] if references is None else references)


def draft_review(database):
    job, proposal = published(database)
    review = Review(database)
    draft = review.create('scenario-test', 'create', job, proposal,
                          'Fixture pump', None, 'synthetic-expert')['draft_id']
    return review, draft, decided(review, draft)


def save(review, draft, view, scenario, key='save', revision=0):
    return review.save('scenario-test', key, draft, revision, view['definitions'],
                       view['associations'], 'synthetic-expert', 'Configure scenario references',
                       scenario_review=scenario)


def test_explicit_scenario_references_survive_save_reload_and_whole_approval(database):
    review, draft, view = draft_review(database)
    assert view['available_scenario_registry'] == REGISTRY
    assert 'scenario_review' not in view
    scenario = scenario_review([
        dict(attribute_id='flow', scenario_id='new_project', requirement='required', condition=''),
        dict(attribute_id='pressure', scenario_id='exact_repeat', requirement='conditional',
             condition='When the nominated specification requires it.'),
    ])
    result = save(review, draft, view, scenario)
    assert result['saved'] is True and result['revision'] == 1
    assert Review(database).resume(draft)['scenario_review'] == scenario
    assert 'scenario_review' not in review.read(draft, revision=0)
    approved = review.approve('scenario-test', 'approve', draft, 1, 'Fixture pump',
                              'synthetic-expert', True)
    assert approved['approved'] is True
    for _ in range(2):
        assert Review(database).read(draft)['approved_schema']['scenario_review'] == scenario
    assert review.approve('scenario-test', 'approve-again', draft, 1, 'Fixture pump',
                           'synthetic-expert', True) == approved


def test_references_can_change_and_remove_without_losing_omitted_scenario_data(database):
    review, draft, view = draft_review(database)
    original = scenario_review([
        dict(attribute_id='flow', scenario_id='new_project', requirement='required', condition=''),
        dict(attribute_id='pressure', scenario_id='exact_repeat', requirement='required', condition=''),
    ])
    assert save(review, draft, view, original)['saved'] is True
    changed = scenario_review([
        dict(attribute_id='pressure', scenario_id='exact_repeat', requirement='conditional',
             condition='When pressure is nominated; <script>plain text only</script>'),
    ])
    assert save(review, draft, view, changed, 'change', 1)['revision'] == 2
    view = review.resume(draft)
    view['definitions'][0]['label'] = 'Expert edited flow'
    assert review.save('scenario-test', 'omit', draft, 2, view['definitions'],
                        view['associations'], 'synthetic-expert', 'Keep references')['revision'] == 3
    resumed = Review(database).resume(draft)
    assert resumed['scenario_review'] == changed
    assert resumed['definitions'][0]['label'] == 'Expert edited flow'
    assert resumed['initial']['candidates'][0]['label'] == 'Flow rate'
    resumed['definitions'][1]['decision'] = 'deferred'
    refused = review.save('scenario-test', 'omit-invalid', draft, 3, resumed['definitions'],
                          resumed['associations'], 'synthetic-expert', 'Defer referenced definition')
    assert refused['category'] == 'validation_error'
    assert refused['errors'][0]['field'] == 'scenario_review.references[0].attribute_id'
    assert Review(database).resume(draft)['revision'] == 3
    assert save(review, draft, resumed, scenario_review(), 'remove-last', 3)['revision'] == 4
    assert Review(database).resume(draft)['scenario_review']['references'] == []


def test_legacy_save_and_approval_remain_unconfigured_until_explicit_empty_save(database):
    review, draft, view = draft_review(database)
    assert review.save('scenario-test', 'legacy', draft, 0, view['definitions'],
                        view['associations'], 'synthetic-expert', 'Legacy save')['saved'] is True
    assert 'scenario_review' not in Review(database).resume(draft)
    approved = review.approve('scenario-test', 'legacy-approve', draft, 1, 'Fixture pump',
                              'synthetic-expert', True)
    assert approved['approved'] is True
    before = review.read(draft)['approved_schema']
    assert 'scenario_review' not in before
    assert Review(database).read(draft)['approved_schema'] == before
    # A different isolated Product Type can explicitly record an empty configuration.
    job, proposal = published(database, product_type='Configured pump')
    configured = review.create('scenario-test', 'create-configured', job, proposal,
                               'Configured pump', None, 'synthetic-expert')['draft_id']
    view = decided(review, configured)
    assert save(review, configured, view, scenario_review(), 'configure-empty')['saved'] is True
    assert Review(database).resume(configured)['scenario_review'] == scenario_review()
    assert 'scenario_review' not in Review(database).read(configured, revision=0)


def invalid_scenarios():
    reference = dict(attribute_id='flow', scenario_id='new_project', requirement='required', condition='')
    yield None
    yield []
    yield dict(scenario_review(), unexpected=True)
    yield dict(scenario_review(), version='unknown')
    yield dict(scenario_review(), registry=dict(version='unknown', scenarios=[]))
    altered = scenario_review()
    altered['registry']['scenarios'][0]['label'] = 'Changed meaning'
    yield altered
    yield dict(scenario_review(), registry={'version': []})
    yield dict(scenario_review(), references={})
    yield scenario_review([None])
    yield scenario_review([dict(reference, unexpected=True)])
    yield scenario_review([reference, deepcopy(reference)])
    yield scenario_review([dict(reference, attribute_id='absent')])
    yield scenario_review([dict(reference, attribute_id=[])])
    yield scenario_review([dict(reference, scenario_id='unknown')])
    yield scenario_review([dict(reference, scenario_id={})])
    yield scenario_review([dict(reference, requirement='optional')])
    yield scenario_review([dict(reference, condition='When selected')])
    yield scenario_review([dict(reference, requirement='conditional', condition='  ')])
    yield scenario_review([dict(reference, requirement='conditional', condition={'execute': 'never'})])
    missing = deepcopy(reference)
    del missing['condition']
    yield scenario_review([missing])


def test_malformed_or_unrecognized_scenario_references_refuse_without_saving(database):
    review, draft, view = draft_review(database)
    for index, invalid in enumerate(invalid_scenarios()):
        rejected = save(review, draft, view, invalid, f'invalid-{index}')
        assert rejected['saved'] is False and rejected['category'] == 'validation_error'
        assert rejected['errors'] and all(error['field'].startswith('scenario_review')
                                          for error in rejected['errors'])
        assert review.resolve('scenario-test', f'invalid-{index}') == rejected
        assert Review(database).resume(draft)['revision'] == 0
    view['definitions'][0]['decision'] = 'deferred'
    rejected = save(review, draft, view, scenario_review([
        dict(attribute_id='flow', scenario_id='new_project', requirement='required', condition=''),
    ]), 'unselected')
    assert rejected['category'] == 'validation_error'
    assert rejected['errors'][0]['field'] == 'scenario_review.references[0].attribute_id'


def test_configured_save_replays_and_stale_or_unknown_commands_are_honest(database):
    review, draft, view = draft_review(database)
    scenario = scenario_review([
        dict(attribute_id='flow', scenario_id='new_project', requirement='required', condition=''),
    ])
    assert review.resolve('scenario-test', 'save')['category'] == 'unknown_command'
    saved = save(review, draft, view, scenario)
    assert Review(database).resolve('scenario-test', 'save') == saved
    assert save(Review(database), draft, view, scenario) == saved
    assert save(review, draft, view, scenario_review())['category'] == 'command_conflict'
    assert save(review, draft, view, scenario, 'stale')['category'] == 'revision_conflict'
    assert review.approve('scenario-test', 'stale-approve', draft, 0, 'Fixture pump',
                           'synthetic-expert', True)['category'] == 'revision_conflict'
    assert Review(database).resume(draft)['scenario_review'] == scenario
    assert review.approve('scenario-test', 'approve', draft, 1, 'Fixture pump',
                           'synthetic-expert', True)['approved'] is True
    assert save(review, draft, view, scenario_review(), 'locked', 1)['category'] == 'revision_conflict'
