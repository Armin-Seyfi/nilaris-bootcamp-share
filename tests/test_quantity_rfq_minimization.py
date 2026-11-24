"""New conveyor and drying-cabinet RFQs for the explicit quantity contract."""
import json

import pytest

from nilaris import quantity_rfq_minimization as rules
from nilaris.block_rfq_minimization import observation
from test_onboarding182_pg import customer_and_invitation, http_client, onboarding
from test_pump_rfq_minimization import publish_document

COVER = 'Buyer reference: CEDAR-58391.'
BELT = 'Belt tension must be at least 318.75 N during startup.'
AIR = 'Air velocity shall be at most 4.125 m/s with the fan running.'
TEMP = 'Surface temperature must be at most 39 °C.'
BELT_TASK = 'Report the belt tension requirement.'
AIR_TASK = 'What is the required air velocity?'
FILENAME = 'private-CEDAR-58391.txt'
SENSITIVE = AIR.replace('with the fan running', 'with the fan running under account CEDAR-58391')
CASES = [
    ('conveyor', BELT_TASK, [COVER + ' ' + BELT, TEMP], BELT, None),
    ('dryer', AIR_TASK, [TEMP, AIR + ' ' + COVER], AIR, None),
    ('competing', AIR_TASK, [AIR, AIR.replace('4.125', '5')], None, rules.MEANING),
    ('incomplete', BELT_TASK, [BELT.replace('during startup', 'during [illegible]')], None, rules.MEANING),
    ('sensitive', AIR_TASK, [SENSITIVE], None, rules.SENSITIVITY),
]


def make_observation(task, texts):
    return observation(task, FILENAME, [dict(id=f'clause-{i}', text=text) for i, text in enumerate(texts)])


@pytest.mark.parametrize('task', [BELT_TASK, 'What is the required belt tension?'])
def test_task_paraphrase(task):
    original = make_observation(task, [COVER, TEMP, BELT])
    assert rules.transform(original['original_content'], complete=True) == (BELT, None)


def test_saved_quantity_journeys(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    for revision, task, texts, passage, reason in CASES:
        observation = make_observation(task, texts)
        job, _, frozen = publish_document(onboarding, customer, observation, revision, rules.PUBLICATION_ADAPTER)
        selector = dict(customer_id=customer, job_id=job)
        with http_client[0]('bob-login') as post:
            _, absent = post('/minimized-evidence/read', selector)
            scoped = dict(selector, expected_scope=absent['scope'])
            code, result = post('/minimized-evidence/build', scoped)
            assert code == 200
            assert result == dict(scope=absent['scope'], state='held' if reason else 'preview',
                                  preview_text=passage, hold_reason=reason, contract=rules.CONTRACT,
                                  detector=rules.DETECTOR)
        with http_client[0]('bob-login') as fresh:
            assert fresh('/minimized-evidence/read', selector) == (200, result)
            code, private = fresh('/minimized-evidence/source', scoped)
            assert code == 200 and private['source_revision'] == revision
            assert private['excerpt'] == observation['original_content']
            assert private['content_pin'] == frozen['publications'][0]['content_pin']
            if passage:
                link, = private['selected_blocks']
                block, = [row for row in json.loads(private['excerpt'])['blocks'] if row['id'] == link['id']]
                assert block['text'][link['start']:link['end']] == passage
            else:
                assert 'selected_blocks' not in private
