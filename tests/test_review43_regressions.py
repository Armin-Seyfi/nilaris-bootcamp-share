"""Review correctness through public commands over immutable synthetic evidence."""
from dataclasses import replace

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from nilaris.review import Review
from test_admission import member
from test_review import decided, proposal, published


def test_supplement_compares_retained_context_without_inventing_removals(database):
    job, proposal_id = published(database, partial=True)
    review = Review(database)
    draft = review.create('test', 'create', job, proposal_id, 'Fixture pump',
                          None, 'synthetic-expert')['draft_id']
    original = review.read(draft, revision=0)
    view = decided(review, draft)
    assert review.save('test', 'save', draft, 0, view['definitions'],
                       view['associations'], 'synthetic-expert',
                       'Accept explicit partial scope')['saved'] is True
    assert review.approve('test', 'approve', draft, 1, 'Fixture pump',
                          'synthetic-expert', True)['approved'] is True
    protected = review.read(draft)
    ingestion = Ingestion(database)
    status = ingestion.status(job)
    queued = ingestion.retry(job, status['revision'], status['generation'],
                             'operator', 'Read page two')
    output = proposal()
    added = dict(id='o-page2', locator=dict(page=2, text='Flow rate: 13 m3/h'),
                 kind='property', original_content='Flow rate: 13 m3/h', value='13',
                 unit='m3/h', source_type='text', presence='observed')
    observation = replace(output.effects[0], ordinal=3, payload=added)
    assert ingestion.publish(job, ingestion.claim(job, queued['revision']),
                             replace(output, effects=(*output.effects, observation)))['state'] == 'completed'
    history = ingestion.result(job)
    incoming_id = history['publications'][1]['id']

    comparison = review.read_incoming(draft, job, incoming_id)

    assert comparison['differences'] == dict(
        observations=[dict(id='o-page2', before=None, after=added)],
        associations=[], candidates=[])
    assert comparison['incoming'] == history['publications'][1]
    assert comparison['incoming']['observations'] == [added]
    assert comparison['read_only'] is True
    assert comparison['incoming_initial_proposal_id'] == proposal_id
    assert comparison['draft_proposal_id'] == proposal_id
    assert comparison['base_version'] == protected['approved_version']
    assert comparison['incoming_base_known'] is True
    assert review.read(draft) == protected
    assert review.read(draft, revision=0) == dict(original,
        approved_version=protected['approved_version'],
        approved_schema=protected['approved_schema'])
    assert ingestion.result(job) == history


def published_metadata(database, confidence, revision='r1'):
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    job = ingestion.offer('test', 'typed-' + revision,
        [dict(member(), product_type='Fixture pump', source_revision=revision)])['members'][0]['job_id']
    ingestion.capture(job, ingestion.claim(job, 0),
                      ControlledSource({'P': (revision, ('typed-source-' + revision).encode())}))
    output = proposal()
    effects = list(output.effects)
    effects[1] = replace(effects[1], payload=dict(effects[1].payload,
                        confidence=confidence, numeric_metadata=1e100))
    assert ingestion.publish(job, ingestion.claim(job, ingestion.status(job)['revision']),
                             replace(output, effects=tuple(effects)))['state'] == 'completed'
    return job, ingestion.result(job)['publications'][0]['id']


def test_typed_human_edits_preserve_source_and_numeric_origin_on_unchanged_save(database):
    job, proposal_id = published_metadata(database, True)
    review = Review(database)
    draft = review.create('test', 'create', job, proposal_id, 'Fixture pump',
                          None, 'synthetic-expert')['draft_id']
    source = review.read(draft)
    first = decided(review, draft)
    first['definitions'][0]['conditions'] = [dict(operand_ids=['o-flow'], confidence=True)]
    assert review.save('test', 'first', draft, 0, first['definitions'],
                       first['associations'], 'first-expert', 'Declare condition')['saved'] is True
    previous = review.read(draft)
    assert 'origin' not in previous['associations'][0]
    changed = review.read(draft)
    changed['associations'][0]['confidence'] = 1
    changed['definitions'][0]['conditions'][0]['confidence'] = 1

    assert review.save('test', 'typed-change', draft, 1, changed['definitions'],
                       changed['associations'], 'second-expert', 'Correct typed confidence')['saved'] is True
    saved = Review(database).resume(draft)

    assert saved['associations'][0]['origin'] == dict(kind='human', actor='second-expert',
        reason='Correct typed confidence', source_is_proof_of_edit=False)
    assert saved['definitions'][0]['origin'] == dict(kind='human', actor='second-expert',
        reason='Correct typed confidence', source_is_proof_of_edit=False, initial_candidate_id='flow')
    assert type(saved['initial']['associations'][0]['confidence']) is bool
    assert saved['initial'] == source['initial']
    assert review.read(draft, revision=1) == previous
    unchanged = review.read(draft)
    unchanged['associations'][0]['numeric_metadata'] = 1e100
    assert review.save('test', 'unchanged', draft, 2, unchanged['definitions'],
                       unchanged['associations'], 'third-expert', 'Confirm unchanged values')['saved'] is True
    resumed = Review(database).resume(draft)
    assert resumed['associations'][0]['origin'] == saved['associations'][0]['origin']
    assert resumed['definitions'][0]['origin'] == saved['definitions'][0]['origin']


def test_incoming_boolean_to_number_is_visible_without_changing_approved_review(database):
    job, proposal_id = published_metadata(database, True)
    review = Review(database)
    draft = review.create('test', 'create', job, proposal_id, 'Fixture pump',
                          None, 'synthetic-expert')['draft_id']
    view = decided(review, draft)
    assert review.save('test', 'save', draft, 0, view['definitions'],
                       view['associations'], 'synthetic-expert', 'Decide schema')['saved'] is True
    assert review.approve('test', 'approve', draft, 1, 'Fixture pump',
                          'synthetic-expert', True)['approved'] is True
    protected = review.read(draft)
    original = review.read(draft, revision=0)
    incoming_job, incoming_id = published_metadata(database, 1, revision='r2')
    incoming_history = Ingestion(database).result(incoming_job)

    comparison = review.read_incoming(draft, incoming_job, incoming_id)

    assert [item['id'] for item in comparison['differences']['associations']] == ['a-flow']
    difference = comparison['differences']['associations'][0]
    assert difference['before']['confidence'] is True
    assert type(difference['after']['confidence']) is int and difference['after']['confidence'] == 1
    assert comparison['differences']['observations'] == []
    assert comparison['differences']['candidates'] == []
    assert comparison['incoming'] == incoming_history['publications'][0]
    assert comparison['base_version'] == protected['approved_version']
    assert comparison['draft_base_version'] is None
    assert comparison['incoming_initial_proposal_id'] == incoming_id
    assert comparison['read_only'] is True
    assert review.read(draft) == protected
    assert review.read(draft, revision=0) == original
    assert Ingestion(database).result(incoming_job) == incoming_history
