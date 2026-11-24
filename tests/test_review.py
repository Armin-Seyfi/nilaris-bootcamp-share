"""Synthetic expert actions at the public review seam; no human acceptance claim."""
from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from nilaris.results import Effect, StageOutput
from nilaris.review import Review
from test_admission import member


def proposal(value='12', partial=False):
    rows = [('flow', 'Flow rate', value, 'm3/h', 'decimal'),
            ('pressure', 'Pressure', '6', 'bar', 'decimal'),
            ('material', 'Material', 'Cast iron', None, 'text')]
    effects = []
    for ordinal, (key, label, observed, unit, value_type) in enumerate(rows):
        raw = f'{label}: {observed}' + (f' {unit}' if unit else '')
        effects.extend([
            Effect('observation', ordinal, dict(id='o-' + key, locator=dict(page=1, text=raw),
                kind='property', original_content=raw, value=observed, unit=unit,
                source_type='text', presence='observed')),
            Effect('association', ordinal, dict(id='a-' + key, observation_ids=['o-' + key],
                method='literal-fixture-rule-v1', rationale='matched source label', disposition='supported')),
            Effect('candidate', ordinal, dict(id=key, label=label, value_type=value_type,
                unit=unit, domain='technical', observation_ids=['o-' + key], requirements='unspecified'))])
    return StageOutput('partial' if partial else 'completed',
        dict(inspected=['page:1'], unread=['page:2'] if partial else [], limits=['literal test fixture']),
        tuple(effects))


def published(database, *, value='12', revision='r1', product_type='Fixture pump', partial=False, profile=None):
    migrate(database)
    ingestion = Ingestion(database, profile or AdmissionProfile.l1())
    item = dict(member(), product_type=product_type, source_revision=revision)
    job_id = ingestion.offer('test', 'offer-' + revision + product_type, [item])['members'][0]['job_id']
    ingestion.capture(job_id, ingestion.claim(job_id, 0),
                      ControlledSource({'P': (revision, b'literal fixture ' + value.encode())}))
    ingestion.publish(job_id, ingestion.claim(job_id, ingestion.status(job_id)['revision']),
                      proposal(value, partial))
    publication = ingestion.result(job_id)['publications'][0]
    return job_id, publication['id']


def test_frozen_proposal_and_human_label_save_survive_restart(database):
    job_id, proposal_id = published(database)
    review = Review(database)
    created = review.create('expert-test', 'create', job_id, proposal_id, 'Fixture pump', None, 'synthetic-expert')
    assert created['saved'] is True
    assert review.create('expert-test','other-create',job_id,proposal_id,'Fixture pump',None,'synthetic-expert')['category'] == 'active_draft_conflict'
    draft_id = created['draft_id']
    initial = review.read(draft_id)
    assert [definition['label'] for definition in initial['definitions']] == ['Flow rate', 'Pressure', 'Material']
    definitions = initial['definitions']
    for definition in definitions:
        definition['decision'] = 'accepted'
    definitions[0]['label'] = 'Rated flow rate'
    saved = review.save('expert-test', 'save', draft_id, 0, definitions,
                        initial['associations'], 'synthetic-expert', 'Use rated label')
    assert saved['saved'] is True and saved['revision'] == 1
    resumed = Review(database).resume(draft_id)
    assert resumed['definitions'][0]['label'] == 'Rated flow rate'
    assert resumed['definitions'][0]['origin']['kind'] == 'human'
    assert resumed['initial']['candidates'][0]['label'] == 'Flow rate'
    assert resumed['initial']['observations'][0]['original_content'] == 'Flow rate: 12 m3/h'
    assert resumed['approved_version'] is None
    assert Review(database).read(draft_id, revision=0)['definitions'][0]['label'] == 'Flow rate'


def decided(review, draft_id):
    view = review.read(draft_id)
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
    view['definitions'][0]['label'] = 'Rated flow rate'
    return view


def test_explicit_whole_schema_approval_replays_and_rejects_changed_key(database):
    job_id, proposal_id = published(database)
    review = Review(database)
    create_args = ('test', 'create', job_id, proposal_id, 'Fixture pump', None, 'synthetic-expert')
    created = review.create(*create_args)
    assert review.create(*create_args) == created
    draft_id = created['draft_id']
    view = decided(review, draft_id)
    args = ('test', 'save', draft_id, 0, view['definitions'], view['associations'], 'synthetic-expert', 'Accept definitions')
    saved = review.save(*args)
    assert Review(database).save(*args) == saved
    assert Review(database).resolve('test', 'save') == saved
    assert review.save('test', 'save', draft_id, 0, [], [], 'synthetic-expert', 'Different')['category'] == 'command_conflict'
    assert review.save('test', 'stale', draft_id, 0, [], [], 'synthetic-expert', 'Stale')['category'] == 'revision_conflict'
    assert review.read(draft_id)['approved_version'] is None
    assert review.approve('test','stale-approval',draft_id,0,'Fixture pump','synthetic-expert',True)['category'] == 'revision_conflict'
    approved = review.approve('test', 'approve', draft_id, 1, 'Fixture pump', 'synthetic-expert', True)
    assert approved['approved'] is True
    assert Review(database).approve('test', 'approve', draft_id, 1, 'Fixture pump', 'synthetic-expert', True) == approved
    schema = Review(database).read(draft_id)['approved_schema']
    assert [d['label'] for d in schema['definitions']] == ['Rated flow rate', 'Pressure', 'Material']
    assert [d['requirements'] for d in schema['definitions']] == ['unspecified'] * 3
    assert [d['conditions'] for d in schema['definitions']] == ['unspecified'] * 3
    assert not any('default' in d or 'value' in d for d in schema['definitions'])
    assert schema['revision'] == 1 and schema['actor'] == 'synthetic-expert'
    assert review.read(draft_id, revision=0)['definitions'][0]['label'] == 'Flow rate'


def test_invalid_included_fields_block_atomic_approval_and_other_type_remains_reviewable(database):
    job_id, proposal_id = published(database)
    review = Review(database)
    draft = review.create('test', 'create', job_id, proposal_id, 'Fixture pump', None, 'synthetic-expert')['draft_id']
    view = decided(review, draft)
    view['definitions'][0]['value_type'] = 'mystery'
    view['definitions'][1]['unit'] = None
    view['definitions'][2]['conditions'] = [dict(operand_ids=['missing-attribute'])]
    view['associations'][0]['disposition'] = 'unresolved'
    saved = review.save('test','invalid',draft,0,view['definitions'],view['associations'],'synthetic-expert','Keep unresolved decisions')
    assert saved['saved'] is True
    rejected = review.approve('test','approve-invalid',draft,1,'Fixture pump','synthetic-expert',True)
    assert rejected['approved'] is False and rejected['category'] == 'validation_error'
    fields = {error['field'] for error in rejected['errors']}
    assert {'definitions[0].value_type','definitions[1].unit',
            'definitions[2].conditions[0].operand_ids','associations[0].disposition'} <= fields
    assert review.read(draft)['approved_version'] is None
    other_job, other_proposal = published(database,product_type='Other pump')
    other = review.create('test','create-other',other_job,other_proposal,'Other pump',None,'synthetic-expert')['draft_id']
    good = decided(review,other)
    review.save('test','save-other',other,0,good['definitions'],good['associations'],'synthetic-expert','Decide')
    assert review.approve('test','approve-other',other,1,'Other pump','synthetic-expert',True)['approved'] is True


def test_partial_is_reviewable_checkpoints_are_not_and_incoming_is_read_only(database):
    job_id, proposal_id = published(database,partial=True)
    review = Review(database)
    draft = review.create('test','create',job_id,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    before = review.read(draft)
    assert before['initial']['coverage']['unread'] == ['page:2']
    incoming_job, incoming_id = published(database,value='13',revision='r2')
    comparison = review.read_incoming(draft,incoming_job,incoming_id)
    assert comparison['read_only'] is True
    assert comparison['incoming']['observations'][0]['value'] == '13'
    assert comparison['differences']['observations'][0] == dict(id='o-flow',before=before['initial']['observations'][0],
                                                              after=comparison['incoming']['observations'][0])
    assert comparison['base_version'] is None
    assert review.read(draft) == before
    assert review.create('test','not-published',incoming_job,'checkpoint-effect-id','Fixture pump',None,'synthetic-expert')['category'] == 'invalid_proposal_set'


def test_large_published_evidence_has_bounded_stable_review_pages(database):
    from dataclasses import replace
    import json
    migrate(database)
    ingestion = Ingestion(database,AdmissionProfile.l1())
    job = ingestion.offer('test','large',[dict(member(),product_type='Fixture pump')])['members'][0]['job_id']
    ingestion.capture(job,ingestion.claim(job,0),ControlledSource({'P':('r1',b'large-source')}))
    source = proposal()
    effects = list(source.effects)
    effects[0] = replace(effects[0],payload=dict(effects[0].payload,original_content='Source evidence ' * 12000))
    ingestion.publish(job,ingestion.claim(job,ingestion.status(job)['revision']),replace(source,effects=tuple(effects)))
    proposal_id = ingestion.result(job)['publication_ids'][0]
    review = Review(database)
    draft = review.create('test','create-large',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    first = review.read(draft)
    assert len(json.dumps(first).encode()) <= 131072
    assert first['encoding'] == 'json-ascii'
    pieces = [first['fragment']]
    cursor = first['next_cursor']
    while cursor:
        page = Review(database).read(draft,cursor=cursor)
        assert len(json.dumps(page).encode()) <= 131072
        pieces.append(page['fragment'])
        cursor = page['next_cursor']
    whole = json.loads(''.join(pieces))
    assert whole['initial']['observations'][0]['original_content'] == 'Source evidence ' * 12000
    assert whole['definitions'][0]['label'] == 'Flow rate'
    pinned = review.read(draft)
    changed = whole['definitions']
    changed[0]['label'] = 'Rated flow rate'
    assert review.save('test','changed-during-pages',draft,0,changed,whole['associations'],'synthetic-expert','Correct label')['saved'] is True
    fragments = [pinned['fragment']]
    cursor = pinned['next_cursor']
    while cursor:
        page = review.read(draft,cursor=cursor)
        fragments.append(page['fragment'])
        cursor = page['next_cursor']
    assert json.loads(''.join(fragments))['definitions'][0]['label'] == 'Flow rate'
    assert review.read(draft,cursor='invalid')['category'] == 'invalid_cursor'
    incoming = review.read_incoming(draft,job,proposal_id)
    assert incoming['encoding'] == 'json-ascii' and len(json.dumps(incoming).encode()) <= 131072


def test_review_history_is_charged_and_later_partial_processing_cannot_exceed_job_limit(database):
    from dataclasses import replace
    job, proposal_id = published(database,partial=True)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    initial = review.read(draft)
    ingestion = Ingestion(database)
    before = ingestion.status(job)
    assert before['reservation_bytes'] == before['usage']['retained_bytes']
    assert before['admission']['review_retained_bytes'] > 0
    assert before['admission']['job_reservation_bytes'] == before['reservation_bytes']
    revision = 0
    for index in range(100):
        result = review.save('test',f'large-save-{index}',draft,revision,initial['definitions'],
                             initial['associations'],'synthetic-expert','History reason ' + 'x' * 50000)
        if result.get('category') == 'byte_capacity':
            break
        assert result['saved'] is True
        revision = result['revision']
    else:
        raise AssertionError('retained review history did not reach finite refusal')
    assert result['saved'] is False and result['receipt_stored'] is False
    stable = review.read(draft)
    assert stable['revision'] == revision
    status = ingestion.status(job)
    assert 7 * 1024**2 < status['reservation_bytes'] <= 8388608
    assert status['reservation_bytes'] == status['usage']['retained_bytes']
    assert status['admission']['review_retained_bytes'] < status['reservation_bytes']
    assert review.resolve('test',f'large-save-{index}')['known'] is False
    retried = ingestion.retry(job,status['revision'],status['generation'],'operator','Finish unread coverage')
    assert retried['state'] == 'queued'
    output = proposal()
    effect = output.effects[0]
    effects = (replace(effect,ordinal=3,payload=dict(effect.payload,id='new-observation',original_content='x' * 200000)),)
    claim = ingestion.claim(job,ingestion.status(job)['revision'])
    refused = ingestion.publish(job,claim,replace(output,effects=effects))
    assert refused['category'] == 'resource_exhausted' and refused['reason'] == 'retained_byte_limit'
    assert review.read(draft) == stable
    assert len(ingestion.result(job)['publications']) == 1


def test_concurrent_save_and_approval_commands_have_one_durable_winner(database):
    from concurrent.futures import ThreadPoolExecutor
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = decided(review,draft)
    def save(key):
        return Review(database).save('test',key,draft,0,view['definitions'],view['associations'],'synthetic-expert',key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save,['save-left','save-right']))
    assert sum(r.get('saved') is True for r in results) == 1
    assert sum(r.get('category') == 'revision_conflict' for r in results) == 1
    assert [review.resolve('test',key) for key in ('save-left','save-right')] == results
    assert len(review.resume(draft)['pending_conflicts']) == 1
    def approve(key):
        return Review(database).approve('test',key,draft,1,'Fixture pump','synthetic-expert',True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approvals = list(pool.map(approve,['approve-left','approve-right']))
    assert all(r.get('approved') is True for r in approvals)
    assert approvals[0] == approvals[1]
    assert [review.resolve('test',key) for key in ('approve-left','approve-right')] == approvals
    winner = approvals[0]
    assert review.read(draft)['approved_version'] == winner['approved_version']
    assert review.create('test','stale-base',job,proposal_id,'Fixture pump',None,'synthetic-expert')['category'] == 'base_conflict'
    assert review.create('test','next-base',job,proposal_id,'Fixture pump',winner['approved_version'],'synthetic-expert')['saved'] is True


def test_invalid_identity_default_and_undecided_fields_require_explicit_resolution(database):
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = decided(review,draft)
    view['definitions'][1]['id'] = 'flow'
    view['definitions'][0]['default'] = '12'
    view['definitions'][2]['decision'] = 'awaiting'
    review.save('test','bad',draft,0,view['definitions'],view['associations'],'synthetic-expert','Unresolved')
    rejected = review.approve('test','bad-approve',draft,1,'Fixture pump','synthetic-expert',True)
    fields = {e['field'] for e in rejected['errors']}
    assert {'definitions[0].id','definitions[0].default','definitions[2].decision','definitions.pressure.decision'} <= fields
    good = decided(review,draft)
    good['definitions'][1]['id'] = 'pressure'
    del good['definitions'][0]['default']
    good['definitions'][2]['decision'] = 'deferred'
    review.save('test','fixed',draft,1,good['definitions'],good['associations'],'synthetic-expert','Defer material explicitly')
    assert review.approve('test','valid-approve',draft,2,'Fixture pump','synthetic-expert',True)['approved'] is True
    assert len(review.read(draft)['approved_schema']['definitions']) == 2


def test_incoming_records_approved_base_at_publication_without_changing_protected_state(database):
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    initial = review.read(draft)
    assert initial['initial_base_known'] is True and initial['initial_base_version'] is None
    view = decided(review,draft)
    review.save('test','save',draft,0,view['definitions'],view['associations'],'synthetic-expert','Accept')
    approved = review.approve('test','approve',draft,1,'Fixture pump','synthetic-expert',True)
    before = review.read(draft)
    incoming_job,incoming_id = published(database,value='13',revision='r2')
    incoming = review.read_incoming(draft,incoming_job,incoming_id)
    assert incoming['incoming_base_known'] is True
    assert incoming['base_version'] == approved['approved_version']
    assert incoming['draft_base_version'] is None
    assert review.read(draft) == before
    assert incoming['incoming']['observations'][0]['value'] == '13'


def test_review_growth_honors_aggregate_admission_capacity_without_borrowing_headroom(database):
    profile = AdmissionProfile.l1().lowered_for_test('review-aggregate-v1',logical_bytes=17 * 1024**2)
    job,proposal_id = published(database,profile=profile)
    ingestion = Ingestion(database,profile)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = review.read(draft)
    other = ingestion.offer('test','other',[dict(member('other'),document_id='other-document')])['members'][0]['job_id']
    revision = 0
    for index in range(20):
        before = review.read(draft)
        args = ('test',f'aggregate-{index}',draft,revision,view['definitions'],view['associations'],
                'synthetic-expert','Bounded history ' + 'x' * 55000)
        result = review.save(*args)
        if result.get('category') == 'byte_capacity':
            break
        revision = result['revision']
    else:
        raise AssertionError('review bypassed aggregate capacity')
    assert result['saved'] is False and result['receipt_stored'] is False
    assert review.read(draft) == before
    status = ingestion.status(job)
    usage = status['admission']
    assert usage['job_reservation_bytes'] + usage['receipt_reservation_bytes'] <= 9 * 1024**2
    assert status['reservation_bytes'] < 1024**2
    assert ingestion.offer('test','capacity-after-review',[dict(member('third'),document_id='third-document')])['members'][0]['reason'] == 'byte_capacity'
    ingestion.cancel(other,0)
    assert review.save(*args)['saved'] is True


def test_persistence_uncertainty_never_claims_a_saved_review(database,monkeypatch):
    import psycopg
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    before = review.read(draft)
    def unavailable(*args,**kwargs):
        raise psycopg.OperationalError('private dependency diagnostic must not escape')
    with monkeypatch.context() as fault:
        fault.setattr(psycopg,'connect',unavailable)
        result = review.save('test','uncertain',draft,0,before['definitions'],before['associations'],
                             'synthetic-expert','Synthetic unavailable save')
    assert result['known'] is False and result.get('saved') is not True
    assert result['category'] == 'persistence_unavailable' and result['next_action'] == 'retry_same_key'
    assert 'private dependency' not in str(result)
    assert review.read(draft) == before
    assert review.resolve('test','uncertain')['known'] is False


def test_supplement_stays_read_only_after_draft_save_and_approval(database):
    from dataclasses import replace
    job,proposal_id = published(database,partial=True)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = decided(review,draft)
    review.save('test','save',draft,0,view['definitions'],view['associations'],'synthetic-expert','Accept explicit partial scope')
    review.approve('test','approve',draft,1,'Fixture pump','synthetic-expert',True)
    before = review.read(draft)
    ingestion = Ingestion(database)
    status = ingestion.status(job)
    retried = ingestion.retry(job,status['revision'],status['generation'],'operator','Read page two')
    output = proposal()
    observation = replace(output.effects[0],ordinal=3,payload=dict(output.effects[0].payload,id='o-page2',
                          value='13',locator=dict(page=2,text='Flow rate: 13 m3/h'),original_content='Flow rate: 13 m3/h'))
    ingestion.publish(job,ingestion.claim(job,retried['revision']),replace(output,effects=(*output.effects,observation)))
    publications = ingestion.result(job)['publications']
    assert len(publications) == 2
    supplement = review.read_incoming(draft,job,publications[1]['id'])
    assert supplement['read_only'] is True and supplement['incoming']['kind'] == 'supplement'
    assert supplement['incoming_initial_proposal_id'] == proposal_id
    assert supplement['base_version'] == before['approved_version']
    assert supplement['incoming']['observations'][0]['value'] == '13'
    assert review.read(draft) == before
    assert review.create('test','supplement-create',job,publications[1]['id'],'Fixture pump',
                         before['approved_version'],'synthetic-expert')['category'] == 'invalid_proposal_set'


def test_command_key_conflicts_when_json_boolean_changes_to_number(database):
    from copy import deepcopy
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = review.read(draft)
    view['definitions'][0]['unresolved_detail'] = True
    first = review.save('test','typed-content',draft,0,view['definitions'],view['associations'],
                        'synthetic-expert','Retain unresolved field for review')
    changed = deepcopy(view['definitions'])
    changed[0]['unresolved_detail'] = 1
    replay = review.save('test','typed-content',draft,0,changed,view['associations'],
                         'synthetic-expert','Retain unresolved field for review')
    assert first['saved'] is True
    assert replay['category'] == 'command_conflict'
