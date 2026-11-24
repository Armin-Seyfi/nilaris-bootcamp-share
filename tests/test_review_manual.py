"""Source-free manual definitions through durable public Review commands."""
from copy import deepcopy

import psycopg
from psycopg.types.json import Jsonb

from nilaris.results import canonical
from nilaris.review import Review
from test_review_scenarios import draft_review, scenario_review


def manual(identity='manual-opaque-135', **changes):
    return dict(dict(id=identity, definition_kind='human_manual_v1', label='Expert rating',
        value_type='decimal', unit='literal unit', requirements='unspecified',
        conditions='unspecified', observation_ids=[], decision='accepted'), **changes)


def save(review, draft, view, revision=0, key='save', **kwargs):
    return review.save('manual-test', key, draft, revision, view['definitions'],
        view['associations'], 'synthetic-manual-expert', 'Expert definition', **kwargs)


def test_manual_definition_survives_second_save_and_source_free_whole_approval(database):
    review, draft, view = draft_review(database)
    initial = deepcopy(view['initial'])
    view['definitions'].append(manual())
    assert save(review, draft, view)['saved'] is True
    resumed = Review(database).resume(draft)
    resumed['definitions'][-1]['label'] = 'Renamed expert rating'
    assert save(review, draft, resumed, 1, 'edit')['saved'] is True
    saved = Review(database).read(draft)
    added = saved['definitions'][-1]
    assert added['origin'] == dict(kind='human', actor='synthetic-manual-expert',
        reason='Expert definition', source_is_proof_of_edit=False, initial_candidate_id=None)
    assert added['observation_ids'] == []
    assert saved['initial'] == initial
    assert saved['associations'] == view['associations']
    assert 'scenario_review' not in saved
    approved = review.approve('manual-test', 'approve', draft, 2, 'Fixture pump',
        'synthetic-manual-expert', True)
    assert approved['approved'] is True
    assert Review(database).read(draft)['approved_schema']['definitions'][-1] == added


def test_manual_lineage_and_source_forgery_refuse_save_without_changing_revision(database):
    review, draft, view = draft_review(database)
    view['definitions'].extend([manual(), dict(manual('legacy-unmarked'), observation_ids=['o-flow'])])
    del view['definitions'][-1]['definition_kind']
    assert save(review, draft, view)['saved'] is True
    baseline = Review(database).resume(draft)
    cases = []

    def changed(field, value):
        edited = deepcopy(baseline)
        edited['definitions'][-2][field] = value
        cases.append(edited)

    for field, value in [('definition_kind', 'forged'), ('definition_kind', None),
                         ('observation_ids', ['o-flow']), ('observation_ids', None),
                         ('source_ids', []), ('domain', 'technical'), ('default', 12),
                         ('value', '12'), ('requirements', 'required'),
                         ('conditions', []), ('decision', 'unknown')]:
        changed(field, value)
    missing_links = deepcopy(baseline)
    del missing_links['definitions'][-2]['observation_ids']
    cases.append(missing_links)
    omitted = deepcopy(baseline)
    omitted['definitions'].pop(-2)
    cases.append(omitted)
    unmarked = deepcopy(baseline)
    del unmarked['definitions'][-2]['definition_kind']
    cases.append(unmarked)
    for target in [0, -1]:
        converted = deepcopy(baseline)
        converted['definitions'][target].update(definition_kind='human_manual_v1', observation_ids=[])
        cases.append(converted)
    collision = deepcopy(baseline)
    collision['definitions'].append(manual('flow'))
    cases.append(collision)
    duplicate = deepcopy(baseline)
    duplicate['definitions'].append(manual())
    cases.append(duplicate)
    associated = deepcopy(baseline)
    associated['associations'][0]['candidate_ids'] = ['manual-opaque-135']
    cases.append(associated)
    for index, invalid in enumerate(cases):
        refused = save(review, draft, invalid, 1, f'invalid-{index}')
        assert refused['saved'] is False, f'case {index} silently saved'
        assert refused['category'] == 'validation_error'
        assert 0 < len(refused['errors']) <= 64
        assert all(error['field'].startswith(('definitions', 'associations')) for error in refused['errors'])
        assert review.resolve('manual-test', f'invalid-{index}') == refused
        assert Review(database).resume(draft) == baseline


def test_removed_legacy_identity_cannot_be_reused_to_claim_manual_approval(database):
    review, draft, view = draft_review(database)
    legacy = manual('old-unmarked-identity')
    del legacy['definition_kind']
    view['definitions'].append(legacy)
    assert save(review, draft, view)['saved'] is True
    refused = review.approve('manual-test', 'legacy-approve', draft, 1, 'Fixture pump',
        'synthetic-manual-expert', True)
    assert refused['category'] == 'validation_error'
    assert any(e['field'] == 'definitions[3].observation_ids' for e in refused['errors'])
    assert 'definition_kind' not in Review(database).read(draft)['definitions'][-1]
    view['definitions'].pop()
    assert save(review, draft, view, 1, 'remove-legacy')['saved'] is True
    view['definitions'].append(manual('old-unmarked-identity'))
    refused = save(review, draft, view, 2, 'reuse-legacy')
    assert refused['saved'] is False
    assert refused['category'] == 'validation_error'
    assert refused['errors'][0]['field'] == 'definitions[3].definition_kind'
    assert Review(database).resume(draft)['revision'] == 2
    historical = Review(database).read(draft, revision=1)['definitions'][-1]
    assert historical == dict(legacy, origin=dict(kind='human',actor='synthetic-manual-expert',
        reason='Expert definition',source_is_proof_of_edit=False,initial_candidate_id=None))


def test_incomplete_manual_content_can_be_saved_but_cannot_be_approved(database):
    review, draft, view = draft_review(database)
    view['definitions'].append(manual(value_type='text', unit=None))
    cases = [({'label': ' '}, 'label'), ({'value_type': 'integer'}, 'value_type'),
             ({'value_type': 'decimal', 'unit': ''}, 'unit'),
             ({'unit': 'invented text unit'}, 'unit'), ({}, 'unit')]
    for index, (changes, field) in enumerate(cases):
        invalid = deepcopy(view)
        invalid['definitions'][-1].update(changes)
        if not changes:
            del invalid['definitions'][-1]['unit']
        assert save(review, draft, invalid, index, f'incomplete-{index}')['saved'] is True
        refused = review.approve('manual-test', f'approve-incomplete-{index}', draft,
            index + 1, 'Fixture pump', 'synthetic-manual-expert', True)
        assert refused['approved'] is False, f'case {index} was approved'
        assert refused['category'] == 'validation_error'
        assert any(e['field'] == 'definitions[3].' + field for e in refused['errors'])
        assert Review(database).read(draft)['approved_schema'] is None


def test_manual_scenarios_exclusion_restore_dependencies_and_exact_revision_guards(database):
    review, draft, view = draft_review(database)
    initial = deepcopy(view['initial'])
    view['definitions'].extend([manual(origin=dict(kind='model',initial_candidate_id='flow')),
        manual('manual-text', label='Expert rating', value_type='text', unit=None)])
    assert review.resolve('manual-test', 'save')['category'] == 'unknown_command'
    first = save(review, draft, view)
    assert first['revision'] == 1
    assert save(review, draft, view) == first
    assert Review(database).resolve('manual-test', 'save') == first
    different = deepcopy(view)
    different['definitions'][-1]['label'] = 'Different request'
    assert save(review, draft, different)['category'] == 'command_conflict'
    assert save(review, draft, view, 0, 'stale')['category'] == 'revision_conflict'
    view = Review(database).resume(draft)
    assert 'scenario_review' not in view
    assert view['definitions'][-2]['origin']['kind'] == 'human'
    assert view['definitions'][-2]['origin']['initial_candidate_id'] is None
    original_origin = deepcopy(view['definitions'][0]['origin'])
    view['definitions'][-2]['origin'] = dict(kind='initial_proposal', candidate_id='flow')
    required = dict(attribute_id='manual-opaque-135',scenario_id='new_project',
                    requirement='required',condition='')
    conditional = dict(attribute_id='manual-text',scenario_id='exact_repeat',
                       requirement='conditional',condition='Only when nominated <plain text>.')
    assert save(review, draft, view, 1, 'configure',
        scenario_review=scenario_review([required,conditional]))['revision'] == 2
    view = Review(database).resume(draft)
    assert view['definitions'][0]['origin'] == original_origin
    assert view['definitions'][-2]['origin']['kind'] == 'human'
    changed = dict(required,requirement='conditional',condition='When the expert specifies it.')
    view['definitions'][0]['conditions'] = [dict(operand_ids=['manual-text'])]
    assert save(review, draft, view, 2, 'change-reference',
        scenario_review=scenario_review([changed]))['revision'] == 3
    view = Review(database).resume(draft)
    view['definitions'][-2]['decision'] = 'rejected'
    refused = save(review, draft, view, 3, 'exclude-referenced')
    assert refused['category'] == 'validation_error'
    assert refused['errors'][0]['field'] == 'scenario_review.references[0].attribute_id'
    view['definitions'][-2]['decision'] = 'accepted'
    view['definitions'][-1]['decision'] = 'deferred'
    assert save(review, draft, view, 3, 'defer')['revision'] == 4
    refused = review.approve('manual-test', 'dependent-approve', draft, 4,
        'Fixture pump', 'synthetic-manual-expert', True)
    assert refused['category'] == 'validation_error'
    assert any(e['field'] == 'definitions[0].conditions[0].operand_ids' for e in refused['errors'])
    view = Review(database).resume(draft)
    view['definitions'][0]['conditions'] = 'unspecified'
    view['definitions'][-1]['decision'] = 'accepted'
    view['definitions'][-1]['label'] = 'Restored expert text'
    assert save(review, draft, view, 4, 'restore')['revision'] == 5
    restored = Review(database).resume(draft)
    assert restored['scenario_review']['references'] == [changed]
    assert restored['definitions'][-1]['id'] == 'manual-text'
    assert restored['definitions'][-1]['label'] == 'Restored expert text'
    restored['definitions'][-1]['decision'] = 'rejected'
    assert save(review, draft, restored, 5, 'exclude')['revision'] == 6
    oversized = deepcopy(restored)
    oversized['definitions'][-2]['label'] = 'x' * 70000
    assert save(review, draft, oversized, 6, 'oversized')['category'] == 'metadata_limit'
    assert review.approve('manual-test', 'stale-approve', draft, 5, 'Fixture pump',
        'synthetic-manual-expert', True)['category'] == 'revision_conflict'
    assert review.approve('manual-test', 'not-explicit', draft, 6, 'Fixture pump',
        'synthetic-manual-expert', False)['category'] == 'explicit_whole_schema_approval_required'
    approved = review.approve('manual-test', 'approve', draft, 6, 'Fixture pump',
        'synthetic-manual-expert', True)
    assert approved['approved'] is True
    final = Review(database).read(draft)
    assert final['initial'] == initial
    assert final['associations'] == view['associations']
    assert final['definitions'][-1]['decision'] == 'rejected'
    assert 'manual-text' not in [d['id'] for d in final['approved_schema']['definitions']]
    assert final['approved_schema']['scenario_review'] == scenario_review([changed])
    for _ in range(2):
        assert Review(database).read(draft)['approved_schema'] == final['approved_schema']
    assert save(review, draft, restored, 6, 'locked')['category'] == 'revision_conflict'
    assert review.approve('manual-test', 'approve-again', draft, 6, 'Fixture pump',
        'synthetic-manual-expert', True) == approved


def test_preexisting_marked_payloads_are_readable_but_do_not_bypass_approval_guards(database):
    review, draft, view = draft_review(database)
    view['definitions'].append(manual())
    assert save(review, draft, view)['saved'] is True
    saved = Review(database).read(draft)
    original = {key: deepcopy(saved[key]) for key in
                ('definitions','associations','actor','reason','revision')}
    variants = []
    unsupported = deepcopy(original)
    unsupported['definitions'][0]['definition_kind'] = 'forged-legacy-kind'
    variants.append((unsupported, 'definitions[0].definition_kind'))
    source_claim = deepcopy(original)
    source_claim['definitions'][-1]['observation_ids'] = ['o-flow']
    variants.append((source_claim, 'definitions[3].observation_ids'))
    omitted = deepcopy(original)
    omitted['definitions'].pop()
    variants.append((omitted, 'definitions'))
    switched = deepcopy(original)
    del switched['definitions'][-1]['definition_kind']
    switched['definitions'][-1]['observation_ids'] = ['o-flow']
    variants.append((switched, 'definitions'))
    # Boundary setup models a saved payload from before manual validation existed.
    # Each check uses public read/approve, never a storage query as its assertion.
    for index, (payload, field) in enumerate(variants):
        payload['revision'] = 2
        with psycopg.connect(**database) as connection:
            connection.execute('''INSERT INTO review_revisions
                (draft_id,revision,payload,reservation_bytes) VALUES(%s,2,%s,%s)
                ON CONFLICT(draft_id,revision) DO UPDATE
                SET payload=excluded.payload,reservation_bytes=excluded.reservation_bytes''',
                (draft,Jsonb(payload),8192 + len(canonical(payload))))
            connection.execute('UPDATE review_drafts SET revision=2 WHERE id=%s',(draft,))
        before = Review(database).read(draft)
        assert before['definitions'] == payload['definitions']
        refused = review.approve('manual-test',f'legacy-guard-{index}',draft,2,
            'Fixture pump','synthetic-manual-expert',True)
        assert refused['approved'] is False, f'legacy case {index} gained approval'
        assert refused['category'] == 'validation_error'
        assert any(error['field'] == field for error in refused['errors'])
        assert Review(database).read(draft)['definitions'] == payload['definitions']
