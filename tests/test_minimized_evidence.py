"""Ticket05 contract and authenticated preview readback against disposable tenants."""
from dataclasses import replace

import psycopg
import pytest
from psycopg.rows import dict_row

from nilaris.admission import AdmissionProfile, Ingestion
from nilaris.execution import ControlledSource, retained_usage
from nilaris.minimized_evidence import FIXTURE_ADAPTER, transform
from nilaris.results import Effect
from test_admission import member
from test_onboarding182_pg import customer_and_invitation, http_client, onboarding
from test_review import proposal

TECHNICAL = 'At 20 °C, flow is at least 12.5 m³/h when inlet pressure is no more than 3.2 bar.'
PERSONAL_AND_COMMERCIAL = 'Contact: Avery Example; quote: Q-731 for Example Pump Works.\n'
CONFLICT = 'Confidential specification includes the upper pressure limit.\n'
UNKNOWN = 'Unclassified field: RX-979.\n'


@pytest.mark.parametrize('prefix,expected', [
    (PERSONAL_AND_COMMERCIAL, (TECHNICAL, None)),
    (CONFLICT, (None, 'technical_meaning_at_risk')),
    (UNKNOWN, (None, 'sensitivity_unresolved')),
    ('', (None, 'sensitivity_unresolved')),
])
def test_synthetic_transform_is_exact_and_fails_closed(prefix, expected):
    assert transform(prefix + TECHNICAL) == expected


def publish_excerpt(onboarding, customer, prefix, revision, *, adapter=FIXTURE_ADAPTER):
    provisioner = onboarding.provisioner
    with onboarding._registry() as connection:
        row = connection.execute('SELECT * FROM provision181_operations WHERE customer_id=%s',
                                 (customer,)).fetchone()
    db = provisioner.customer_runtime(row)
    ingestion = Ingestion(db, AdmissionProfile.l1(), monitor_path=provisioner.config['service_monitor_path'])
    item = dict(member(), document_id='synthetic-spec-sheet', source_revision=revision)
    offered = ingestion.offer('synthetic', 'ticket05-' + revision, [item])
    job = offered['members'][0]['job_id']
    original = prefix + TECHNICAL
    ingestion.capture(job, ingestion.claim(job, 0), ControlledSource({'P': (revision, original.encode())}))
    existing = proposal()
    observation = dict(existing.effects[0].payload, id='technical-excerpt',
                       original_content=original, locator={'page': 2, 'text': original})
    output = replace(existing, adapter=adapter, effects=(*existing.effects, Effect('observation', 3, observation)))
    published = ingestion.publish(job, ingestion.claim(job, ingestion.status(job)['revision']), output)
    assert published['known']
    frozen = ingestion.result(job)
    assert frozen['publications'][0]['observations'][-1] == observation
    return job, ingestion, frozen


def test_preview_reloads_with_private_immutable_source_and_denies_other_authorities(onboarding, http_client):
    a, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    b, _, redeem = customer_and_invitation(onboarding, ['Administrator', 'Reviewer'],
                                           bootstrap_command='bootstrap-b')
    onboarding.accept(*redeem[:-1], 'accept-bob-b')
    roster = onboarding.members('bob-login', b)
    alice = next(row for row in roster['users'] if row['subject'] == 'alice')
    grant = dict(kind='roles', target=alice['membership_id'], roles=['Administrator', 'Reviewer'])
    assert onboarding.manage('bob-login', b, 'b-reviewer', roster['revision'], grant)['state'] == 'updated'
    b_job, _, _ = publish_excerpt(onboarding, b, PERSONAL_AND_COMMERCIAL, 'synthetic-b-r1')
    job, ingestion, frozen = publish_excerpt(onboarding, a, PERSONAL_AND_COMMERCIAL, 'synthetic-r1')
    unrelated, _, _ = publish_excerpt(onboarding, a, UNKNOWN, 'other-r1', adapter='other-adapter')
    selector = dict(customer_id=a, job_id=job)
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        before = retained_usage(connection, job)
        reserved = connection.execute('SELECT reservation_bytes FROM admission_jobs WHERE id=%s',
                                      (job,)).fetchone()['reservation_bytes']
    with http_client[0]('bob-login') as post:
        assert post('/minimized-evidence/read', dict(selector, job_id=unrelated))[0] == 403
        code, absent = post('/minimized-evidence/read', selector)
        assert code == 200 and absent['state'] == 'absent'
        scoped = dict(selector, expected_scope=absent['scope'])
        assert post('/minimized-evidence/source', scoped)[0] == 403
        assert post('/minimized-evidence/build', dict(scoped, excerpt=TECHNICAL))[0] == 400
        code, preview = post('/minimized-evidence/build', scoped)
        assert code == 200 and preview == dict(
            state='preview', contract='minimized-evidence-v1', detector='synthetic-span-detector-v1',
            preview_text=TECHNICAL, hold_reason=None, scope=absent['scope'])
        assert post('/minimized-evidence/build', scoped) == (200, preview)
    with http_client[0]('bob-login') as fresh:
        assert fresh('/minimized-evidence/read', selector) == (200, preview)
        code, private = fresh('/minimized-evidence/source', scoped)
        publication = frozen['publications'][0]
        assert code == 200 and private == dict(
            state='original', publication_id=publication['id'], observation_id='technical-excerpt',
            document_id='synthetic-spec-sheet', source_revision='synthetic-r1',
            content_pin=publication['content_pin'], locator={'page': 2, 'text': PERSONAL_AND_COMMERCIAL + TECHNICAL},
            excerpt=PERSONAL_AND_COMMERCIAL + TECHNICAL, contract=preview['contract'], detector=preview['detector'])
        assert fresh('/minimized-evidence/read', dict(selector, customer_id=b))[0] == 403
        assert fresh('/minimized-evidence/source', dict(scoped, customer_id=b))[0] == 403
    with http_client[0]('alice-login', second_credential='operator-login') as (b_reviewer, operator):
        assert b_reviewer('/minimized-evidence/read', dict(customer_id=b, job_id=b_job))[1]['state'] == 'absent'
        for ungranted in (b_reviewer, operator):
            assert ungranted('/minimized-evidence/source', scoped)[0] == 403
            assert ungranted('/minimized-evidence/read', selector)[0] == 403
    assert ingestion.result(job) == frozen
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        assert retained_usage(connection, job)['retained_bytes'] == before['retained_bytes'] + 8192
        assert connection.execute('SELECT reservation_bytes FROM admission_jobs WHERE id=%s',
                                  (job,)).fetchone()['reservation_bytes'] == reserved + 8192


@pytest.mark.parametrize('prefix,reason', [
    (CONFLICT, 'technical_meaning_at_risk'),
    (UNKNOWN, 'sensitivity_unresolved'),
])
def test_holds_are_durable_and_return_no_preview(onboarding, http_client, prefix, reason):
    a, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, ingestion, frozen = publish_excerpt(onboarding, a, prefix, 'synthetic-hold-r1')
    selector = dict(customer_id=a, job_id=job)
    with http_client[0]('bob-login') as post:
        _, absent = post('/minimized-evidence/read', selector)
        scoped = dict(selector, expected_scope=absent['scope'])
        code, held = post('/minimized-evidence/build', scoped)
        assert code == 200 and held == dict(
            state='held', contract='minimized-evidence-v1', detector='synthetic-span-detector-v1',
            preview_text=None, hold_reason=reason, scope=absent['scope'])
    with http_client[0]('bob-login') as fresh:
        assert fresh('/minimized-evidence/read', selector) == (200, held)
    assert ingestion.result(job) == frozen


def test_existing_schema_can_publish_and_read_an_ordinary_job(onboarding, monkeypatch):
    from nilaris import provisioning181

    migrate = provisioning181.migrate

    def create_disposable_legacy_schema(options):
        migrate(options)
        with psycopg.connect(**options) as connection:
            connection.execute('DROP TABLE minimized_evidence')

    monkeypatch.setattr(provisioning181, 'migrate', create_disposable_legacy_schema)
    customer, _, redeem = customer_and_invitation(onboarding, ['Reviewer'])
    onboarding.accept(*redeem)
    job, ingestion, frozen = publish_excerpt(onboarding, customer, '', 'legacy-r1', adapter='other-adapter')
    assert ingestion.status(job)['state'] == 'completed'
    assert ingestion.result(job) == frozen
