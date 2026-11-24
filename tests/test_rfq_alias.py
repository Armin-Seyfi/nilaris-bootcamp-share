"""Local integration checks using the two owner-selected pump PDFs on macOS."""
from hashlib import sha256
from rfq_workspace import pump_bundle, reviewer_workspace

from nilaris.rfq_demo import schema_draft


def test_cross_document_proposal_starts_undecided_with_both_originals(pump_bundle):
    rows, locations = schema_draft(pump_bundle)
    assert len([row for row in rows if row['attribute']]) >= 2
    proposal, = [row for row in rows if 'alias' in row]
    assert proposal['row'] == 5
    alias = proposal['alias']
    assert alias['decision'] is None
    evidence = alias['initial']['evidence']
    table = evidence['table']
    note = evidence['note']
    assert table['row'] == 5 and table['filename'] == '2.pdf'
    assert note['note'] == 7 and note['filename'] == '1.pdf'
    assert table['sha256'] != note['sha256']
    assert note['passage'].startswith('7) ') and note['passage'].endswith('pump shut off pressure')
    assert '3 barg' in note['passage']
    for observation in (table, note):
        asset = locations[observation['prefix']]
        assert asset.location == observation
        assert sha256(asset.source.pdf).hexdigest() == observation['sha256']
        assert observation['pdf_url'].startswith(observation['prefix'] + '/original.pdf#page=1&zoom=')


def test_saved_alias_correction_retains_the_initial_proposal_and_exact_sources(pump_bundle):
    with reviewer_workspace(schema_draft(pump_bundle), bundle=pump_bundle, key='alias-test') as client:
        endpoint = '/rfq/schema/alias'
        assert client.json(endpoint, {'decision': 'confirm', 'name': 'Shut-off head'})[0] == 401
        status, _ = client.json('/rfq/session', {'token': 'alias-test'})
        assert status == 200

        def reviewed_row():
            rows = client.json('/rfq/schema')[1]
            return next(row for row in rows if row['row'] == 5)

        original = reviewed_row()['alias']['initial']
        assert client.json(endpoint, {'decision': 'automatic', 'name': 'New name'})[0] == 400
        for decision in ['confirm', 'separate']:
            status, _ = client.json(endpoint, {'decision': decision, 'name': 'Shut-off head'})
            assert status == 200
            current = reviewed_row()
            assert current['attribute'] == 'Shut-off head'
            assert current['alias']['decision'] == decision
            assert current['alias']['initial'] == original
            for evidence in original['evidence'].values():
                assert client.json(evidence['prefix'] + '/location.json')[1] == evidence
                status, pdf = client.raw(evidence['pdf_url'].partition('#')[0])
                assert status == 200 and sha256(pdf).hexdigest() == evidence['sha256']
