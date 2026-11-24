"""Explicit synthetic two-scenario seed for fresh, dedicated test customer databases."""
from hashlib import sha256
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.case001_import import reserve_import, store
from nilaris.results import canonical

FIXTURE_REVIEWER = '["synthetic","Fixture Reviewer"]'


def seed(database, check_mutation):
    template = Path(__file__).parents[1] / 'src/nilaris/scoped_fixtures/pump-sheet.pdf'
    # Equal-length replacement preserves the original PDF stream and xref offsets.
    original = template.read_bytes().replace(b'Rated flow: 12 m3/h', b'Plunger dia: 57 mm ')
    pin = sha256(original).hexdigest()
    observation = dict(attribute='Plunger diameter', product_type='Plunger pump · synthetic review',
                       scenario='Synthetic procurement', source_name='synthetic-pump.pdf', source_sha256=pin,
                       passage='Plunger dia: 57 mm', value='57 mm', source_fragment='page=1',
                       scope_note='Synthetic equipment sheet', location='Page 1',
                       documents=[dict(name='synthetic-pump.pdf', sha256=pin)])
    imported = store(database, observation, original, 'Synthetic fixture', check_mutation=check_mutation)
    with psycopg.connect(**database, row_factory=dict_row) as c:
        c.execute('SELECT pg_advisory_xact_lock(714012001)')
        draft = imported['draft_id']
        row = c.execute('SELECT revision FROM review_drafts WHERE id=%s', (draft,)).fetchone()
        if row['revision'] != 0:
            return imported
        payload = c.execute('SELECT payload FROM review_revisions WHERE draft_id=%s AND revision=0',
                            (draft,)).fetchone()['payload']
        for key, label, membership in [('stroke-length', 'Stroke length', 'accepted'),
                                       ('rated-flow', 'Rated flow', 'accepted'),
                                       ('seal-flush-rate', 'Seal flush rate', 'awaiting')]:
            payload['definitions'].append(dict(id=key, label=label, decision=membership, observation_ids=[],
                                              membership_origin=dict(kind='system') if membership=='accepted' else None))
        def decided(*stages):
            return dict(stages=list(stages), decided_by=FIXTURE_REVIEWER)
        payload.update(revision=1, reason='Initialize synthetic scenarios',
                       scenarios=[dict(id='new', label='New pump purchase'), dict(id='reorder', label='Like-for-like reorder')],
                       stage_cells={'plunger-diameter': {'new': decided('supplier-offer','before-manufacture')},
                                    'rated-flow': {'new': decided('customer-rfq'), 'reorder': decided()}})
        c.execute('INSERT INTO review_revisions VALUES (%s,1,%s,%s)',
                  (draft, Jsonb(payload), 8192 + len(canonical(payload))))
        c.execute('UPDATE review_drafts SET revision=1 WHERE id=%s', (draft,))
        profile = c.execute('SELECT profile FROM admission_jobs WHERE id=%s', (imported['job_id'],)).fetchone()
        reserve_import(c, imported['job_id'], profile['profile'])
        check_mutation()
    return imported
