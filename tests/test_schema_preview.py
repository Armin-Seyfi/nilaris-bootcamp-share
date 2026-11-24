from nilaris.customer_review import required_roles


def test_preview_requires_approver():
    assert required_roles('review_schema_preview') == {'Approver'}


def test_preview_is_pinned_and_read_only(database):
    import base64
    from hashlib import sha256
    import psycopg
    from nilaris.admission import migrate
    from nilaris.review import Review
    from stage_matrix_fixture import seed

    migrate(database)
    draft = seed(database, lambda: None)['draft_id']
    review = Review(database, case_source_allowed=True)
    first = review.schema_preview(draft, 1)
    assert [(cell['attribute_id'], cell['scenario_id']) for cell in first['eligible_cells']] == [
        ('plunger-diameter', 'reorder'), ('stroke-length', 'new'), ('stroke-length', 'reorder')]
    reviewer = '["test","Morgan Taylor"]'
    saved = review.stage_bulk('test', 'finish', draft, 1, first['eligible_cells'], reviewer)
    assert saved['revision'] == 2
    ready = review.schema_preview(draft, 2)
    assert ready['eligible_cells'] == []
    excluded = [row for row in ready['attributes'] if row['membership'] != 'accepted']
    assert [(row['label'], row['membership']) for row in excluded] == [('Seal flush rate', 'awaiting')]
    assert ready['history'][0]['actor'] == reviewer
    assert len(ready['history'][0]['changes']) == 3
    assert ready['associations'] == []
    assert review.schema_preview(draft, 1) == first
    assert review.schema_preview(draft, 3)['category'] == 'revision_not_found'
    original = base64.b64decode(review.stage_source(draft)['pdf_base64'])
    assert sha256(original).hexdigest() == first['evidence']['source_sha256']
    assert b'Plunger dia: 57 mm' in original
    with psycopg.connect(**database) as connection:
        assert connection.execute('SELECT revision FROM review_drafts WHERE id=%s', (draft,)).fetchone() == (2,)
        assert connection.execute('SELECT count(*) FROM review_approvals').fetchone() == (0,)


def test_correction_history_includes_association_and_definition_changes():
    from nilaris.stage_matrix import stage_changes

    first = dict(definitions=[dict(id='p', label='Pressure', decision='awaiting')],
                 associations=[dict(id='a', disposition='deferred')], scenarios=[dict(id='s', label='Pump')])
    second = dict(first, definitions=[dict(id='p', label='Pressure', decision='accepted')],
                  associations=[dict(id='a', disposition='supported')], stage_cells={})
    changes = stage_changes(first, second, {'scenario':'Pump'})
    assert [(change['category'], change['label']) for change in changes] == [
        ('Definition', 'Pressure'), ('Association', 'a')]
    assert changes[1]['before']['disposition'] == 'deferred'
    assert changes[1]['after']['disposition'] == 'supported'
