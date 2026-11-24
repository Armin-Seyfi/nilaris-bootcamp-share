"""Approval identity through public Review commands over synthetic evidence."""
from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.review import Review
from test_admission import member
from test_review import decided, published


def test_new_command_for_approved_revision_returns_original_version(database):
    job, proposal_id = published(database)
    review = Review(database)
    draft = review.create('test', 'create', job, proposal_id, 'Fixture pump',
                          None, 'original-expert')['draft_id']
    view = decided(review, draft)
    assert review.save('test', 'save', draft, 0, view['definitions'],
                       view['associations'], 'original-expert', 'Accept schema')['revision'] == 1
    approved = review.approve('test', 'approve', draft, 1, 'Fixture pump',
                              'original-expert', True)
    assert approved['approved'] is True
    protected = review.read(draft)
    retained = Ingestion(database).status(job)['usage']['retained_bytes']

    repeated = Review(database).approve('test', 'new-command', draft, 1,
                                        'Fixture pump', 'another-expert', True)

    assert repeated == approved
    assert Review(database).resolve('test', 'new-command') == approved
    assert Review(database).approve('test', 'new-command', draft, 1,
                                    'Fixture pump', 'another-expert', True) == approved
    assert Review(database).read(draft) == protected
    assert protected['approved_schema']['actor'] == 'original-expert'
    assert Ingestion(database).status(job)['usage']['retained_bytes'] > retained

    for key, revision, product_type, explicit, category in (
        ('stale', 0, 'Fixture pump', True, 'revision_conflict'),
        ('future', 2, 'Fixture pump', True, 'revision_conflict'),
        ('wrong-type', 1, 'Other pump', True, 'product_type_conflict'),
        ('not-explicit', 1, 'Fixture pump', False, 'explicit_whole_schema_approval_required'),
    ):
        rejected = review.approve('test', key, draft, revision, product_type,
                                  'another-expert', explicit)
        assert rejected['approved'] is False and rejected['category'] == category
        if explicit:
            assert Review(database).resolve('test', key) == rejected
    assert review.approve('test', 'new-command', draft, 1, 'Fixture pump',
                          'changed-expert', True)['category'] == 'command_conflict'
    assert review.approve('test', 'new-command', draft, 0, 'Fixture pump',
                          'another-expert', True)['category'] == 'command_conflict'
    assert review.approve('test', 'new-command', draft, 1, 'Other pump',
                          'another-expert', True)['category'] == 'command_conflict'
    assert review.save('test', 'edit-approved', draft, 1, [], [],
                       'another-expert', 'Cannot edit approved schema')['category'] == 'revision_conflict'
    assert review.read(draft)['approved_schema'] == protected['approved_schema']

    next_draft = review.create('test', 'next-draft', job, proposal_id, 'Fixture pump',
                               approved['approved_version'], 'next-expert')['draft_id']
    next_view = decided(review, next_draft)
    assert review.save('test', 'next-save', next_draft, 0, next_view['definitions'],
                       next_view['associations'], 'next-expert', 'Accept next schema')['saved'] is True
    successor = review.approve('test', 'next-approve', next_draft, 1, 'Fixture pump',
                               'next-expert', True)
    assert successor['version'] == 2
    assert successor['approved_version'] != approved['approved_version']
    assert Review(database).approve('test', 'repeat-after-successor', draft, 1,
                                    'Fixture pump', 'another-expert', True) == approved
    assert review.read(draft)['approved_schema'] == protected['approved_schema']


def test_new_approval_command_needs_capacity_for_its_durable_receipt(database):
    profile = AdmissionProfile.l1().lowered_for_test('approval-replay-capacity-v1',
                                                    logical_bytes=17 * 1024**2)
    job, proposal_id = published(database, profile=profile)
    ingestion = Ingestion(database, profile)
    review = Review(database)
    draft = review.create('test', 'create', job, proposal_id, 'Fixture pump',
                          None, 'original-expert')['draft_id']
    view = decided(review, draft)
    assert review.save('test', 'save', draft, 0, view['definitions'],
                       view['associations'], 'original-expert', 'Accept schema')['saved'] is True
    approved = review.approve('test', 'approve', draft, 1, 'Fixture pump',
                              'original-expert', True)
    assert approved['approved'] is True
    protected = review.read(draft)
    waiting = ingestion.offer('test', 'waiting', [dict(member('waiting'),
        document_id='waiting-document')])['members'][0]['job_id']
    actor = 'Synthetic expert ' + 'x' * 55000

    for index in range(30):
        key = f'approval-receipt-{index}'
        repeated = review.approve('test', key, draft, 1, 'Fixture pump', actor, True)
        if repeated.get('category') == 'byte_capacity':
            break
        assert repeated == approved
        assert Review(database).resolve('test', key) == approved
    else:
        raise AssertionError('new approval commands bypassed aggregate receipt capacity')

    assert repeated['saved'] is False and repeated['receipt_stored'] is False
    assert review.resolve('test', key)['known'] is False
    assert review.read(draft) == protected
    assert Review(database).approve('test', 'approve', draft, 1, 'Fixture pump',
                                    'original-expert', True) == approved
    assert ingestion.cancel(waiting, 0)['state'] == 'cancelled'
    assert Review(database).approve('test', key, draft, 1, 'Fixture pump', actor, True) == approved
    assert Review(database).resolve('test', key) == approved
    assert review.read(draft) == protected
