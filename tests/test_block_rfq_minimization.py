"""Independent local enclosure RFQ development fixtures."""
import json

import pytest

from nilaris import block_rfq_minimization as rules
from test_onboarding182_pg import customer_and_invitation, http_client, onboarding
from test_pump_rfq_minimization import publish_document

PREVIEW = 'Enclosure sound level shall be at most 67.25 dB(A) at 3 m with the access panel latched.'
FILENAME = 'silencer-ACOU-59173.txt'
BLOCKS = [{'id': 'cover-slip', 'text': 'Buyer reference: ACOU-59173.'},
          {'id': 'acoustic-note', 'text': PREVIEW}]
AMBIGUOUS = [*BLOCKS, {'id': 'second-note', 'text': PREVIEW.replace('67.25', '71')}]


def publish_blocks(service, customer, blocks, revision):
    original = rules.observation(rules.TASK, FILENAME, blocks)
    return publish_document(service, customer, original, revision, rules.PUBLICATION_ADAPTER)


@pytest.mark.parametrize('blocks,task,complete,reason', [
    (list(reversed(BLOCKS)), rules.TASK, True, None),
    (AMBIGUOUS, rules.TASK, True, 'technical_meaning_at_risk'),
    (BLOCKS, 'Report transit time.', True, 'technical_meaning_at_risk'),
    (BLOCKS, rules.TASK, False, 'technical_meaning_at_risk'),
    ([BLOCKS[0], dict(BLOCKS[1], id='cover-slip')], rules.TASK, True, 'sensitivity_unresolved'),
])
def test_block_contract(blocks, task, complete, reason):
    excerpt = rules.observation(task, FILENAME, blocks)['original_content']
    assert rules.transform(excerpt, complete=complete) == (None if reason else PREVIEW, reason)


def test_private_block_after_fresh_read(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, ingestion, frozen = publish_blocks(onboarding, customer, BLOCKS, 'acoustics-r3')
    selector = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        _, absent = post('/minimized-evidence/read', selector)
        scoped = dict(selector, expected_scope=absent['scope'])
        status, saved = post('/minimized-evidence/build', scoped)
        assert status == 200 and saved == dict(scope=absent['scope'], state='preview',
            preview_text=PREVIEW, hold_reason=None, contract=rules.CONTRACT, detector=rules.DETECTOR)
    with http_client[0]('bob-login') as fresh:
        assert fresh('/minimized-evidence/read', selector) == (200, saved)
        code, original = fresh('/minimized-evidence/source', scoped)
        assert code == 200
        publication = frozen['publications'][0]
        for key in ('id', 'content_pin'):
            assert original['publication_id' if key == 'id' else key] == publication[key]
        assert original['source_revision'] == 'acoustics-r3'
        assert original['contract'] == saved['contract']
        assert original['selected_blocks'] == [{'id': 'acoustic-note'}]
        assert json.loads(original['excerpt']) == dict(task=rules.TASK, filename=FILENAME, blocks=BLOCKS)
    assert ingestion.result(job) == frozen
