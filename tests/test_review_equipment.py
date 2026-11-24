"""Equipment metadata through the public durable Review command seam."""
from nilaris.review import Review
from test_review import decided, published


def equipment(**changes):
    return dict(dict(version='equipment-review-v1', included_equipment='',
                     exclusions='', categories=[]), **changes)


def draft_review(database):
    job, proposal = published(database)
    review = Review(database)
    draft = review.create('equipment-test', 'create', job, proposal,
                          'Fixture pump', None, 'synthetic-expert')['draft_id']
    return review, draft, decided(review, draft)


def save(review, draft, view, metadata, key='save', revision=0):
    return review.save('equipment-test', key, draft, revision, view['definitions'],
                       view['associations'], 'synthetic-expert', 'Describe equipment',
                       equipment_review=metadata)


def test_explicit_empty_upgrade_roundtrips_and_whole_approval_is_immutable(database):
    review, draft, view = draft_review(database)
    assert 'equipment_review' not in view
    assert review.save('equipment-test', 'legacy', draft, 0, view['definitions'],
                       view['associations'], 'synthetic-expert', 'Legacy save')['revision'] == 1
    assert 'equipment_review' not in Review(database).resume(draft)
    assert save(review, draft, view, equipment(), revision=1)['revision'] == 2
    expected = equipment(origin=dict(kind='human', actor='synthetic-expert',
                                    reason='Describe equipment', source_is_proof_of_edit=False))
    assert Review(database).resume(draft)['equipment_review'] == expected
    assert 'equipment_review' not in review.read(draft, revision=1)
    approved = review.approve('equipment-test', 'approve', draft, 2, 'Fixture pump',
                              'synthetic-expert', True)
    assert approved['approved'] is True
    before = Review(database).read(draft)['approved_schema']
    assert before['equipment_review'] == expected
    assert save(review, draft, view, equipment(included_equipment='Changed'),
                'locked', 2)['category'] == 'revision_conflict'
    assert Review(database).read(draft)['approved_schema'] == before


def test_invalid_equipment_metadata_is_refused_without_advancing_revision(database):
    review, draft, view = draft_review(database)
    invalid = [None, [], {}, equipment(version='future'), equipment(extra='unsupported'),
               equipment(included_equipment=None), equipment(exclusions=[]),
               equipment(included_equipment='x' * 4001), equipment(exclusions='x' * 4001),
               equipment(categories={}), equipment(categories=['  ']),
               equipment(categories=[None]), equipment(categories=['x' * 201]),
               equipment(categories=['same', 'same']),
               equipment(categories=[str(index) for index in range(17)])]
    for index, metadata in enumerate(invalid):
        key = f'invalid-{index}'
        rejected = save(review, draft, view, metadata, key)
        assert rejected['saved'] is False and rejected['category'] == 'validation_error'
        assert rejected['errors'] and all(error['field'].startswith('equipment_review')
                                          for error in rejected['errors'])
        assert review.resolve('equipment-test', key) == rejected
        current = Review(database).resume(draft)
        assert current['revision'] == 0 and 'equipment_review' not in current
