from contextlib import contextmanager
from types import SimpleNamespace

import psycopg

from nilaris.admission import migrate
from nilaris.review import Review
from nilaris.rfq_demo import schema_draft
from nilaris.service_monitor import STATUS_PATH
from rfq_workspace import pump_bundle, reviewer_workspace


def test_exact_approval_reopens_canonical_postgres_schema(database, pump_bundle, monkeypatch):
    # Exercise both continuation boundaries with a small number of durable commands.
    monkeypatch.setattr('nilaris.rfq_approval.HISTORY_PAGE_SIZE', 1)
    migrate(database)
    config = SimpleNamespace(database=database, monitor_path=STATUS_PATH)
    schema = schema_draft(pump_bundle)

    @contextmanager
    def workspace(key='review-key'):
        with reviewer_workspace(schema, key=key, bundle=pump_bundle, review_config=config) as client:
            yield client.json

    with workspace() as request:
        assert request('/rfq/session', {'token': 'review-key'})[0] == 200
        state = request('/rfq/schema/review')[1]
        draft = state['draft_id']
        approve = dict(draft_id=draft, revision=0, explicit=True)
        assert request('/rfq/schema/approve', approve)[0] == 409
        correction = dict(draft_id=draft, revision=0, name='Shut-off head', decision='confirm')
        status, saved = request('/rfq/schema/alias', correction)
        assert status == 200
        assert request('/rfq/schema/alias', correction) == (200, saved)
        assert request('/rfq/schema/alias', dict(correction, name='Unseen stale edit'))[0] == 409
        reviewed = request('/rfq/schema/review')[1]
        assert reviewed['revision'] == 1 and reviewed['approved_version'] is None
        assert request('/rfq/schema/approve', approve)[0] == 409

    with workspace('fresh-key') as request:
        assert request('/rfq/schema/review')[0] == 401
        assert request('/rfq/session', {'token': 'fresh-key'})[0] == 200
        assert request('/rfq/schema/alias', correction) == (200, saved)
        assert request('/rfq/schema/review')[1] == reviewed
        approve['revision'] = 1
        assert request('/rfq/schema/approve', dict(approve, explicit=False))[0] == 400
        with monkeypatch.context() as stopped:
            stopped.setattr('nilaris.rfq_approval.service_monitor_ready', lambda path: False)
            assert request('/rfq/schema/approve', approve)[0] == 503
        assert request('/rfq/schema/review')[1]['approved_version'] is None
        status, receipt = request('/rfq/schema/approve', approve)
        assert status == 200 and receipt['approved'] is True
        assert request('/rfq/schema/approve', approve)[1] == receipt
        assert request('/rfq/schema/alias', dict(correction, revision=1))[0] == 409
        url = '/rfq/schema/versions/' + receipt['approved_version']
        frozen = request(url)[1]
        canonical = Review(database).read(draft, receipt_operations=['approve'])['approved_schema']
        assert {key: frozen[key] for key in canonical} == canonical
        assert frozen['id'] == receipt['approved_version']
        assert frozen['product_type'] == 'Pump' and frozen['revision'] == 1
        alias = next(row for row in frozen['rows'] if 'alias' in row)
        original = next(row['alias']['initial'] for row in schema[0] if 'alias' in row)
        assert alias['alias'] == dict(initial=original, decision='confirm')
        assert frozen['corrections'] == [dict(revision=1, row=alias['row'], actor='Technical Reviewer',
            before=dict(name=original['attribute'], decision=None), after=dict(name='Shut-off head', decision='confirm'))]
        assert len(frozen['definitions']) == 14 and sum(bool(r['unresolved_reason']) for r in frozen['rows']) == 6
        definition = next(d for d in frozen['definitions'] if d['label'] == correction['name'])
        assert frozen['definition_origins'][definition['id']] == (
            'Renamed from “' + original['attribute'] + '” · Alias reviewed · Inclusion saved by Technical Reviewer')
        assert {origin for key, origin in frozen['definition_origins'].items() if key != definition['id']} == {
            'Initial proposal · Inclusion saved by Technical Reviewer'}
        assert definition['observation_ids'] == [original['evidence'][k]['source_url'] for k in ('table', 'note')]

    with workspace('later-key') as request:
        assert request(url)[0] == 401
        assert request('/rfq/session', {'token': 'later-key'})[0] == 200
        assert request(url) == (200, frozen)
        with monkeypatch.context() as stopped:
            stopped.setattr('nilaris.rfq_approval.service_monitor_ready', lambda path: False)
            assert request('/rfq/schema/approve', approve) == (200, receipt)
            assert request('/rfq/schema/alias', correction) == (200, saved)
            foreign = dict(approve, draft_id='another-proposals-draft')
            assert request('/rfq/schema/approve', foreign)[0] == 409
        with psycopg.connect(**database) as db:
            assert db.execute('SELECT count(*) FROM review_drafts').fetchone() == (1,)
        following = request('/rfq/schema/review')[1]
        successor = dict(draft_id=following['draft_id'], revision=0, name='Separate head', decision='separate')
        assert request('/rfq/schema/alias', successor)[0] == 200
        second = dict(successor, revision=1, name='Separate corrected head')
        assert request('/rfq/schema/alias', second)[0] == 200
        status, newer = request('/rfq/schema/approve', dict(draft_id=following['draft_id'], revision=2, explicit=True))
        assert status == 200 and newer['version'] == receipt['version'] + 1
        next_version = request('/rfq/schema/versions/' + newer['approved_version'])[1]
        assert [r['revision'] for r in next_version['corrections']] == [2]
        prior_page = request(next_version['older_corrections'])[1]
        assert [r['revision'] for r in prior_page['corrections']] == [1]
        assert prior_page['older_corrections'] is None
        index = request('/rfq/schema/review')[1]
        assert [v['id'] for v in index['versions']] == [newer['approved_version']]
        earlier = request(index['older_versions'])[1]
        assert earlier['versions'] == [dict(id=frozen['id'], version=frozen['version'])]
        assert earlier['older_versions'] is None
        assert next_version['base_version'] == frozen['id']
        assert next_version['rows'][4]['alias']['decision'] == 'separate'
        assert request(url) == (200, frozen)


def test_unchanged_alias_receipt_and_manual_provenance(database, pump_bundle):
    from test_review_manual import manual

    migrate(database)
    config = SimpleNamespace(database=database, monitor_path=STATUS_PATH)
    with reviewer_workspace(schema_draft(pump_bundle), key='review-key', bundle=pump_bundle,
                            review_config=config) as client:
        request = client.json
        assert request('/rfq/session', {'token': 'review-key'})[0] == 200
        draft = request('/rfq/schema/review')[1]['draft_id']
        command = dict(draft_id=draft, revision=0, name='Shut-off head', decision='confirm')
        assert request('/rfq/schema/alias', command)[0] == 200
        unchanged = dict(command, revision=1)
        status, receipt = request('/rfq/schema/alias', unchanged)
        assert status == 200 and receipt['known'] and receipt['revision'] == 2
        assert request('/rfq/schema/alias', dict(command, revision=2, name='Reviewed head'))[0] == 200
        assert request('/rfq/schema/alias', unchanged) == (200, receipt)
        state = request('/rfq/schema/review')[1]
        assert state['revision'] == 3 and [c['revision'] for c in state['corrections']] == [3, 1]

        canonical = Review(database)
        before = canonical.read(draft, receipt_operations=['approve'])
        before['definitions'][0]['decision'] = 'deferred'
        definition = manual()
        deferred = manual('later-rating', decision='deferred')
        result = canonical.save('manual-test', 'pump-add', draft, 3, before['definitions'] + [definition, deferred],
                                before['associations'], 'Pump engineer', 'Design review')
        assert result['saved']
        status, edited = request('/rfq/schema/review')
        assert status == 200
        origin = 'Reviewer definition · Pump engineer · Design review'
        assert edited['definition_origins'][definition['id']] == origin
        assert request('/rfq/schema/alias', dict(command, revision=4, name='Final head'))[0] == 200
        status, approved = request('/rfq/schema/approve', dict(draft_id=draft, revision=5, explicit=True))
        assert status == 200
        status, saved = request('/rfq/schema/versions/' + approved['approved_version'])
        assert status == 200 and saved['definition_origins'][definition['id']] == origin
        retained = next(d for d in saved['definitions'] if d['id'] == definition['id'])
        assert retained['origin']['actor'] == 'Pump engineer' and retained['observation_ids'] == []
        included = {d['id'] for d in saved['definitions']}
        assert deferred['id'] not in included and before['definitions'][0]['id'] not in included
        source = before['definitions'][0]['observation_ids'][0]
        row = next(r for r in saved['rows'] if r['source_url'] == source)
        assert row['definition_decision'] == 'deferred'
