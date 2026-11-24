"""Map the local pump Reviewer onto the canonical PostgreSQL Review commands."""
import hashlib
import json

import psycopg

from nilaris.review import Review, refusal
from nilaris.rfq_publication import ALIAS_ASSOCIATION
from nilaris.service_monitor import service_monitor_ready


HISTORY_PAGE_SIZE = 10
ACTOR = 'Technical Reviewer'
NAMESPACE = 'local-pump-rfq'
DISPOSITIONS = {'confirm': 'accepted', 'separate': 'rejected'}


def require_known(result):
    if not result.get('known'):
        raise OSError('The durable review outcome is unavailable.')
    return result


def alias_selection(revision):
    association = next(item for item in revision['associations'] if item['id'] == ALIAS_ASSOCIATION)
    definition = next(item for item in revision['definitions'] if item['id'] == association['candidate_ids'][0])
    decision = next((key for key, value in DISPOSITIONS.items() if value == association['disposition']), None)
    return definition, association, dict(name=definition['label'], decision=decision)


class PumpSchemaReview:
    def __init__(self, config, job_id, proposal_id, alias_row_number):
        self.alias_row_number = alias_row_number
        self.config = config
        self.job_id = job_id
        self.proposal_id = proposal_id
        self.review = Review(config.database, check_mutation=self.check_monitor)
        self.draft_id = None

    def check_monitor(self):
        if not service_monitor_ready(self.config.monitor_path):
            raise OSError('The local PostgreSQL monitor is unavailable.')

    def versions(self, through=2**63-1):
        with psycopg.connect(**self.config.database) as db:
            records = db.execute('''SELECT a.id, a.version FROM review_approvals a
                JOIN review_drafts d ON d.id=a.draft_id WHERE d.proposal_id=%s AND a.version<=%s
                ORDER BY a.version DESC LIMIT %s''', (self.proposal_id, through, HISTORY_PAGE_SIZE + 1)).fetchall()
        more = len(records) > HISTORY_PAGE_SIZE
        return dict(versions=[dict(id=identity, version=version) for identity, version in records[:HISTORY_PAGE_SIZE]],
                    older_versions=f'/rfq/schema/versions-before/{records[-1][1]}' if more else None)

    def owns_draft(self, draft):
        with psycopg.connect(**self.config.database) as db:
            return db.execute('SELECT 1 FROM review_drafts WHERE id=%s AND proposal_id=%s',
                              (draft, self.proposal_id)).fetchone() is not None

    def replay(self, draft, key):
        if not self.owns_draft(draft):
            return refusal('revision_conflict')
        result = self.review.resolve(NAMESPACE, key)
        if result.get('category') != 'unknown_command':
            return require_known(result)
        return None

    def current_draft(self):
        if self.draft_id is None:
            versions = self.versions()['versions']
            base = versions[0]['id'] if versions else None
            key = f'create/{self.proposal_id}/{base}'
            self.check_monitor()
            receipt = require_known(self.review.create(
                NAMESPACE, key, self.job_id, self.proposal_id, 'Pump', base, ACTOR))
            if not receipt.get('saved'):
                raise OSError('The pump review draft could not be opened.')
            self.draft_id = receipt['draft_id']
        return self.draft_id

    def read(self, draft_id):
        page = require_known(self.review.read(draft_id))
        if page.get('encoding') != 'json-ascii':
            return page
        parts = [page['fragment']]
        while page['next_cursor']:
            page = require_known(self.review.read(draft_id, cursor=page['next_cursor']))
            parts.append(page['fragment'])
        return json.loads(''.join(parts))

    def project(self, view):
        rows = [item['locator']['rfq_row'] for item in view['initial']['observations']
                if 'rfq_row' in item['locator']]
        alias = next(row for row in rows if 'alias' in row)
        alias_definition, _, selection = alias_selection(view)
        alias['attribute'] = selection['name']
        alias['alias']['decision'] = selection['decision']
        proposed = {candidate['id']: candidate for candidate in view['initial']['candidates']}
        rows_by_source = {row['source_url']: row for row in rows}
        origins = {}
        for item in view['definitions']:
            if item['id'] not in proposed:
                origin = item['origin']
                origins[item['id']] = f"Reviewer definition · {origin['actor']} · {origin['reason']}"
                continue
            candidate = proposed[item['id']]
            for source in candidate['observation_ids']:
                if source in rows_by_source:
                    rows_by_source[source]['definition_decision'] = item['decision']
            original = candidate['label']
            label = 'Initial proposal' if item['label'] == original else f'Renamed from “{original}”'
            if item['id'] == alias_definition['id'] and selection['decision']:
                label += ' · Alias reviewed'
            if item['origin']['kind'] == 'human':
                label += ' · Inclusion saved by ' + item['origin']['actor']
            origins[item['id']] = label
        return dict(rows=rows, definition_origins=origins,
                    **self.corrections(view['draft_id'], view['revision']))

    def corrections(self, draft, through):
        with psycopg.connect(**self.config.database) as db:
            revisions = db.execute('''SELECT r.payload FROM review_revisions r
                JOIN review_drafts d ON d.id=r.draft_id
                WHERE r.draft_id=%s AND d.proposal_id=%s AND r.revision<=%s
                ORDER BY r.revision DESC LIMIT %s''',
                (draft, self.proposal_id, through, HISTORY_PAGE_SIZE + 1)).fetchall()
        corrections = []
        for (revision,), (previous,) in zip(revisions, revisions[1:]):
            _, _, after = alias_selection(revision)
            _, _, before = alias_selection(previous)
            if before != after:
                corrections.append(dict(revision=revision['revision'], row=self.alias_row_number,
                                        actor=revision['actor'], before=before, after=after))
        next_revision = revisions[-1][0]['revision'] if revisions else 0
        return dict(corrections=corrections, older_corrections=(
            f'/rfq/schema/history/{draft}/{next_revision}' if next_revision else None))

    def state(self):
        view = self.read(self.current_draft())
        return dict(draft_id=view['draft_id'], revision=view['revision'], definitions=view['definitions'],
                    approved_version=view['approved_version'], **self.versions(), **self.project(view))

    def reopen(self, identity):
        with psycopg.connect(**self.config.database) as db:
            record = db.execute('''SELECT d.id FROM review_drafts d
                WHERE d.approval_id=%s AND d.proposal_id=%s''', (identity, self.proposal_id)).fetchone()
        if record:
            view = self.read(record[0])
            return dict(view['approved_schema'], **self.project(view))
        return None

    def correct(self, name, decision, expected_draft, expected_revision):
        fingerprint = hashlib.sha256(json.dumps([name, decision]).encode()).hexdigest()
        key = f"save/{expected_draft}/{expected_revision}/{fingerprint}"
        prior = self.replay(expected_draft, key)
        if prior is not None:
            return prior
        view = self.read(expected_draft)
        if (expected_draft != view['draft_id'] or expected_revision != view['revision']
                or view['approved_version']):
            return refusal('revision_conflict')
        definition, association, _ = alias_selection(view)
        candidates = {item['id'] for item in view['initial']['candidates']}
        for item in view['definitions']:
            if item['id'] in candidates and item['decision'] == 'awaiting':
                item['decision'] = 'accepted'
        definition['label'] = name
        definition['observation_ids'] = (association['observation_ids'] if decision == 'confirm'
                                         else association['observation_ids'][:1])
        association['disposition'] = DISPOSITIONS[decision]
        self.check_monitor()
        return require_known(self.review.save(
            NAMESPACE, key, expected_draft, expected_revision, view['definitions'],
            view['associations'], ACTOR, 'Save pump Alias decision and proposed Schema scope'))

    def approve(self, body):
        draft = body['draft_id']
        key = f"approve/{draft}/{body['revision']}"
        prior = self.replay(draft, key)
        if prior is not None:
            return prior
        self.check_monitor()
        return require_known(self.review.approve(
            NAMESPACE, key, draft, body['revision'], 'Pump', ACTOR, body['explicit']))
