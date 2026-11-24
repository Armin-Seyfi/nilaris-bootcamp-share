"""Synthetic development fixtures for the Pump RFQ v1 preview."""
from dataclasses import replace
import json

import pytest

from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.execution import ControlledSource
from nilaris import pump_rfq_minimization
from nilaris.results import Effect
from test_admission import member
from test_onboarding182_pg import customer_and_invitation, http_client, onboarding
from test_review import proposal

FLOW = 'Flow: at least 7.625 L/s at differential head 43.20 m.'
INLET = 'Inlet pressure: at most 265 kPa(g) when liquid temperature is below +41.25 °C.'
CONTACT = 'Leto Devcanary'
EMAIL = 'leto.devcanary@training.invalid'
BUYER = 'Oriole Mock Procurement'
QUOTE = 'RFQ-QA77-KX'
DOC_ID = 'dev-private-rfq-document'
RFQ = '\n'.join((
    'PUMP RFQ V1', '[Contact]', f'Contact name: {CONTACT}', f'Contact email: {EMAIL}',
    '[Commercial]', f'Buyer: {BUYER}', f'Quote ID: {QUOTE}', '[Duty]',
    FLOW + ' | Contact email: second.devcanary@training.invalid',
    '[Operating limits]', INLET + ' | Quote ID: RFQ-QA77-TR',
))
PREVIEW = FLOW + '\n' + INLET
AMBIGUOUS = RFQ.replace('below +41.25 °C', 'below TBD-CANARY [unit obscured]')
CANARIES = (CONTACT, EMAIL, BUYER, QUOTE, DOC_ID, 'second.devcanary@training.invalid', 'RFQ-QA77-TR')


def test_rule_accepts_varying_values_and_keeps_complete_clauses_verbatim():
    assert pump_rfq_minimization.transform(RFQ, complete=True) == (PREVIEW, None)
    alternative = (RFQ.replace('at least 7.625', 'exactly 1.05')
                   .replace('kPa(g)', 'kPa(abs)').replace('below +41.25', 'at most −4.8'))
    expected = (PREVIEW.replace('at least 7.625', 'exactly 1.05')
                .replace('kPa(g)', 'kPa(abs)').replace('below +41.25', 'at most −4.8'))
    assert pump_rfq_minimization.transform(alternative, complete=True) == (expected, None)


@pytest.mark.parametrize('excerpt,complete,reason', [
    (AMBIGUOUS, True, 'technical_meaning_at_risk'),
    (RFQ.replace('at differential head 43.20 m.', 'at unknown head.'), True, 'technical_meaning_at_risk'),
    (RFQ.replace('kPa(g)', 'kPa'), True, 'technical_meaning_at_risk'),
    (RFQ, False, 'technical_meaning_at_risk'),
    (RFQ.replace(' | Quote ID:', ' | Private condition:'), True, 'sensitivity_unresolved'),
    (RFQ.replace(BUYER, 'Differential Head'), True, 'sensitivity_unresolved'),
    (RFQ.replace(EMAIL, 'unresolved contact'), True, 'sensitivity_unresolved'),
    (RFQ + '\n[Unclassified]', True, 'sensitivity_unresolved'),
    ('X' * 2049, True, 'sensitivity_unresolved'),
])
def test_rule_fails_closed_for_unknown_meaning_or_sensitivity(excerpt, complete, reason):
    assert pump_rfq_minimization.transform(excerpt, complete=complete) == (None, reason)


def publish_rfq(onboarding, customer, excerpt, revision, *, partial=False, presence='observed'):
    original = dict(id='pump-rfq-document', locator={'page': 3, 'range': 'lines 1–11'},
                    kind='excerpt', original_content=excerpt, value=None, unit=None,
                    source_type='text', presence=presence)
    return publish_document(onboarding, customer, original, revision, 'pump-rfq-document-v1', partial=partial)


def publish_document(onboarding, customer, original, revision, adapter, *, partial=False):
    excerpt = original['original_content']
    provisioner = onboarding.provisioner
    with onboarding._registry() as connection:
        row = connection.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',
                                 (customer,)).fetchone()
    db = provisioner.customer_runtime(row)
    ingest = Ingestion(db, AdmissionProfile.l1(), monitor_path=provisioner.config['service_monitor_path'])
    item = dict(member(), document_id=DOC_ID, source_revision=revision)
    offered = ingest.offer('synthetic', 'pump-rfq-dev-' + revision, [item])
    job = offered['members'][0]['job_id']
    ingest.capture(job, ingest.claim(job, 0), ControlledSource({'P': (revision, excerpt.encode())}))
    base = proposal(partial=partial)
    output = replace(base, adapter=adapter,
                     effects=(*base.effects, Effect('observation', 3, original)))
    ingest.publish(job, ingest.claim(job, ingest.status(job)['revision']), output)
    frozen = ingest.result(job)
    assert frozen['publications'][0]['observations'][-1] == original
    return job, ingest, frozen


def test_persisted_rfq_preview_and_hold_keep_the_private_source(outcomes, http_client):
    a, good, ambiguous, ingest, frozen = outcomes
    selectors = [dict(customer_id=a, job_id=job) for job in (good, ambiguous)]
    with http_client[0]('bob-login') as post:
        responses = []
        for selector in selectors:
            code, absent = post('/minimized-evidence/read', selector)
            assert code == 200 and absent['state'] == 'absent'
            scoped = dict(selector, expected_scope=absent['scope'])
            code, response = post('/minimized-evidence/build', scoped)
            assert code == 200
            assert post('/minimized-evidence/build', scoped) == (200, response)
            responses.append(response)
        preview, held = responses
        assert preview == dict(state='preview', preview_text=PREVIEW, hold_reason=None,
                               contract='pump-rfq-minimization-v1', detector='pump-rfq-field-rules-v1',
                               scope=preview['scope'])
        assert held == dict(state='held', preview_text=None, hold_reason='technical_meaning_at_risk',
                            contract=preview['contract'], detector=preview['detector'], scope=held['scope'])
    with http_client[0]('bob-login', second_credential='alice-login') as (fresh, administrator):
        for selector, response in zip(selectors, responses, strict=True):
            assert fresh('/minimized-evidence/read', selector) == (200, response)
            source = dict(selector, expected_scope=response['scope'])
            assert administrator('/minimized-evidence/source', source)[0] == 403
            code, original = fresh('/minimized-evidence/source', source)
            assert code == 200
            assert original['excerpt'] == (RFQ if response['state'] == 'preview' else AMBIGUOUS)
            assert original['document_id'] == DOC_ID
            assert original['locator'] == {'page': 3, 'range': 'lines 1–11'}
            if response['state'] == 'preview':
                assert original['publication_id'] == frozen['publications'][0]['id']
                assert original['source_revision'] == 'dev-safe-1'
                assert original['content_pin'] == frozen['publications'][0]['content_pin']
                assert original['selected_blocks'] == [
                    {'heading': 'Duty', 'lines': [8, 9]}, {'heading': 'Operating limits', 'lines': [10, 11]},
                ]
            else:
                assert 'selected_blocks' not in original
    assert ingest.result(good) == frozen


@pytest.fixture
def outcomes(onboarding):
    a, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    good, ingest, frozen = publish_rfq(onboarding, a, RFQ, 'dev-safe-1')
    ambiguous, _, _ = publish_rfq(onboarding, a, AMBIGUOUS, 'dev-hold-1')
    return a, good, ambiguous, ingest, frozen


def test_reachable_preview_and_error_bodies_hide_private_canaries(outcomes, http_client, monkeypatch):
    a, job, _, _, _ = outcomes
    selector = dict(customer_id=a, job_id=job)

    def injected_failure(excerpt, *, complete):
        assert complete and excerpt == RFQ
        raise RuntimeError(excerpt + DOC_ID)

    with http_client[0]('bob-login') as post:
        code, absent = post('/minimized-evidence/read', selector)
        assert code == 200 and absent['state'] == 'absent'
        scoped = dict(selector, expected_scope=absent['scope'])
        with monkeypatch.context() as patch:
            patch.setattr(pump_rfq_minimization, 'transform', injected_failure)
            failed = post('/minimized-evidence/build', scoped)
        assert failed[0] == 503 and failed[1]['state'] == 'unknown'
        assert post('/minimized-evidence/read', selector) == (200, absent)
        invalid = post('/minimized-evidence/build', dict(scoped, excerpt=RFQ))
        assert invalid[0] == 400 and invalid[1]['state'] == 'refused'
        code, preview = post('/minimized-evidence/build', scoped)
        assert code == 200 and preview['preview_text'] == PREVIEW
        assert post('/minimized-evidence/read', selector) == (200, preview)
        code, original = post('/minimized-evidence/source', scoped)
        assert code == 200 and all(marker in json.dumps(original) for marker in CANARIES)
        for response in (failed[1], invalid[1], preview):
            envelope = json.dumps(response)
            assert not any(marker in envelope for marker in CANARIES)


def test_completed_publication_with_ambiguous_observation_is_held(onboarding, http_client):
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, _, frozen = publish_rfq(onboarding, customer, RFQ, 'dev-uncertain-r1', presence='ambiguous')
    assert frozen['publications'][0]['outcome'] == 'completed'
    selector = dict(customer_id=customer, job_id=job)
    with http_client[0]('bob-login') as post:
        _, absent = post('/minimized-evidence/read', selector)
        code, held = post('/minimized-evidence/build', dict(selector, expected_scope=absent['scope']))
        assert code == 200 and held['state'] == 'held'
        assert held['hold_reason'] == 'technical_meaning_at_risk' and held['preview_text'] is None
    with http_client[0]('bob-login') as fresh:
        assert fresh('/minimized-evidence/read', selector) == (200, held)
