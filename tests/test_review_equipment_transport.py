"""Equipment contract through the real authenticated HTTP command adapter."""
from http.client import HTTPConnection
import json

from test_command_service import command_process
from test_review import published
from test_review_equipment import equipment


def command(body):
    connection = HTTPConnection('127.0.0.1', 18765, timeout=3)
    try:
        connection.request('POST', '/command', json.dumps(body),
                           {'Authorization': 'Bearer synthetic-test-token'})
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status in (200, 400)
        return result
    finally:
        connection.close()


def test_equipment_http_preserves_literal_bounds_and_provenance_through_approval(database, command_process):
    job, proposal = published(database)
    _, start, _ = command_process
    start()
    created = command(dict(operation='review_create', namespace='equipment-http',
        command_id='create', job_id=job, proposal_id=proposal, product_type='Fixture pump',
        expected_base=None, actor='synthetic-expert'))
    draft = created['draft_id']
    view = command(dict(operation='review_read', draft_id=draft))
    assert 'equipment_review' not in view
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
    metadata = equipment(included_equipment='i' * 4000, exclusions='e' * 4000,
        categories=['Pumps', 'pumps', ' Pumps '] + [str(index) + 'c' * 199 for index in range(10)] +
                   ['A' * 200, 'B' * 200, 'C' * 200],
        origin=dict(kind='initial_proposal', actor='forged', source_is_proof_of_edit=True))
    body = dict(operation='review_save', namespace='equipment-http', command_id='save',
        draft_id=draft, expected_revision=0, definitions=view['definitions'],
        associations=view['associations'], actor='synthetic-expert', reason='Describe equipment',
        equipment_review=metadata)
    saved = command(body)
    assert saved.get('saved') is True and saved.get('revision') == 1, saved
    expected = dict(metadata, origin=dict(kind='human', actor='synthetic-expert',
                    reason='Describe equipment', source_is_proof_of_edit=False))
    assert command(dict(operation='review_resume', draft_id=draft))['equipment_review'] == expected
    assert command(body) == saved
    assert command(dict(body, equipment_review=equipment()))['category'] == 'command_conflict'
    assert command(dict(operation='review_resolve', namespace='equipment-http', command_id='save')) == saved
    unchanged = dict(body, command_id='unchanged', expected_revision=1,
                     actor='different-expert', reason='Review unchanged wording')
    assert command(unchanged)['revision'] == 2
    assert command(dict(operation='review_resume', draft_id=draft))['equipment_review'] == expected
    omitted = dict(unchanged, command_id='omit', expected_revision=2)
    del omitted['equipment_review']
    assert command(omitted)['revision'] == 3
    assert command(dict(operation='review_resume', draft_id=draft))['equipment_review'] == expected
    changed = equipment(included_equipment='<script>plain text only</script>',
                        exclusions='  Preserve wording  ', categories=['Pumps', 'pumps'])
    updated = dict(body, command_id='change', expected_revision=3, actor='different-expert',
                   reason='Revise wording', equipment_review=changed)
    assert command(updated)['revision'] == 4
    expected = dict(changed, origin=dict(kind='human', actor='different-expert',
                    reason='Revise wording', source_is_proof_of_edit=False))
    assert command(dict(operation='review_resume', draft_id=draft))['equipment_review'] == expected
    rejected = command(dict(updated, command_id='invalid', expected_revision=4,
                            equipment_review=equipment(categories=['duplicate', 'duplicate'])))
    assert rejected['category'] == 'validation_error' and rejected['saved'] is False
    assert command(dict(updated, command_id='stale'))['category'] == 'revision_conflict'
    approved = command(dict(operation='review_approve', namespace='equipment-http',
        command_id='approve', draft_id=draft, expected_revision=4, product_type='Fixture pump',
        actor='synthetic-expert', explicit=True))
    assert approved['approved'] is True
    final = command(dict(operation='review_read', draft_id=draft))['approved_schema']
    assert final['equipment_review'] == expected
    assert command(dict(updated, command_id='locked', expected_revision=4))['category'] == 'revision_conflict'
    assert command(dict(operation='review_read', draft_id=draft))['approved_schema'] == final
