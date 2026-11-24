"""Bounded retained discovery journal; admission remains the job authority."""
import json

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from nilaris.service_monitor import service_monitor_ready

MIGRATION = '''
CREATE TABLE IF NOT EXISTS discovery_streams (
    namespace text PRIMARY KEY, cursor text, exhausted boolean NOT NULL DEFAULT false,
    reservation_bytes bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_pages (
    namespace text NOT NULL REFERENCES discovery_streams(namespace), page_id text NOT NULL,
    request jsonb NOT NULL, records jsonb NOT NULL, state text NOT NULL DEFAULT 'staged',
    reservation_bytes bigint NOT NULL, PRIMARY KEY(namespace,page_id)
);
'''
PREFIX = 'discovery:'
STREAM_BYTES = 8192


def error(category, *, known=True):
    return dict(known=known, category=category, coverage='unknown',
                reason='Discovery page could not advance: ' + category,
                next_action='read_page_then_retry_same_key')


def identity(namespace, page_id):
    return all(isinstance(value, str) and value.strip() and len(value) <= 256
               for value in (namespace, page_id))


def load(connection, namespace, page_id):
    return connection.execute('SELECT * FROM discovery_pages WHERE namespace=%s AND page_id=%s',
                              (namespace, page_id)).fetchone()


def project(connection, row):
    stream = connection.execute('SELECT cursor, exhausted FROM discovery_streams WHERE namespace=%s',
                                (row['namespace'],)).fetchone()
    return dict(known=True, namespace=row['namespace'], page_id=row['page_id'],
                expected_cursor=row['request']['expected_cursor'], next_cursor=row['request']['next_cursor'],
                state=row['state'], members=row['records'], checkpoint=stream,
                reservation_bytes=row['reservation_bytes'],
                next_action='explicitly_admit_original_page' if row['state'] == 'staged' else 'read_pending_members')


def stage(ingestion, namespace, page_id, expected_cursor, next_cursor, members):
    from nilaris.admission import AdmissionProfile, _admission_usage
    if not identity(namespace, page_id):
        return error('invalid_request')
    if not isinstance(ingestion.profile, AdmissionProfile):
        return error('configuration_unavailable')
    if any(cursor is not None and (not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 1024)
           for cursor in (expected_cursor, next_cursor)):
        return error('invalid_request')
    if (not isinstance(members, list) or len(members) > ingestion.profile.batch_members or
            any(not isinstance(item, dict) or not isinstance(item.get('member_key'), str)
                or not item['member_key'].strip() for item in members)):
        return error('invalid_request')
    if len({item['member_key'] for item in members}) != len(members):
        return error('invalid_request')
    request = dict(expected_cursor=expected_cursor, next_cursor=next_cursor, members=members)
    try:
        size = len(json.dumps(dict(namespace=PREFIX + namespace, command_id=page_id, **request),
                              ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return error('invalid_request')
    if size > ingestion.profile.metadata_bytes:
        return error('metadata_limit')
    if not service_monitor_ready(ingestion.monitor_path):
        return error('service_monitor_unavailable', known=False)
    reservation = 4 * size + 65536
    records = [dict(member=item, disposition=None, pending=True) for item in members]
    if len(json.dumps(records, ensure_ascii=True)) + 3 * sum(len(json.dumps(item['member_key'])) for item in members) + 16384 > 131072:
        return error('metadata_limit')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        if not service_monitor_ready(ingestion.monitor_path):
            return error('service_monitor_unavailable', known=False)
        prior = load(connection, namespace, page_id)
        if prior:
            same = connection.execute('SELECT request=%s AS same FROM discovery_pages WHERE namespace=%s AND page_id=%s',
                                      (Jsonb(request), namespace, page_id)).fetchone()['same']
            return project(connection, prior) if same else error('page_conflict')
        stream = connection.execute('SELECT * FROM discovery_streams WHERE namespace=%s', (namespace,)).fetchone()
        if stream and (stream['exhausted'] or stream['cursor'] != expected_cursor):
            return error('cursor_conflict')
        if not stream and expected_cursor is not None:
            return error('cursor_conflict')
        if connection.execute("SELECT 1 FROM discovery_pages WHERE namespace=%s AND state='staged'", (namespace,)).fetchone():
            return error('page_in_progress')
        if not members and next_cursor is not None and next_cursor == expected_cursor:
            return error('cursor_conflict')
        usage = _admission_usage(connection)
        if usage['job_reservation_bytes'] + usage['receipt_reservation_bytes'] + reservation + (0 if stream else STREAM_BYTES) > ingestion.profile.logical_bytes - ingestion.profile.terminal_headroom_bytes:
            return error('journal_capacity')
        if not stream:
            connection.execute('INSERT INTO discovery_streams(namespace,reservation_bytes) VALUES (%s,%s)', (namespace, STREAM_BYTES))
        connection.execute('INSERT INTO discovery_pages(namespace,page_id,request,records,reservation_bytes) VALUES (%s,%s,%s,%s,%s)',
                           (namespace,page_id,Jsonb(request),Jsonb(records),reservation))
        return project(connection, load(connection, namespace, page_id))


def read(ingestion, namespace, page_id):
    if not identity(namespace, page_id):
        return error('invalid_request')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        row = load(connection, namespace, page_id)
        return project(connection, row) if row else error('unknown', known=False)


def admit(ingestion, namespace, page_id):
    if not identity(namespace, page_id):
        return error('invalid_request')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        row = load(connection, namespace, page_id)
    if not row:
        return error('unknown', known=False)
    if not row['request']['members']:
        return dict(known=True, category='no_offer_needed', next_action='reconcile_empty_page')
    return ingestion.offer(PREFIX + namespace, page_id, row['request']['members'], _discovery=True)


def valid_receipt(connection, row, receipt):
    members = row['request']['members']
    if not isinstance(receipt, dict) or receipt.get('known') is not True or receipt.get('receipt_stored') is not True:
        return False
    outcomes = receipt.get('members')
    if not isinstance(outcomes, list) or len(outcomes) != len(members):
        return False
    if any(not isinstance(item, dict) for item in outcomes):
        return False
    keys = [item.get('member_key') for item in outcomes]
    if keys != [item['member_key'] for item in members]:
        return False
    stored = connection.execute('SELECT request=%s AS same FROM admission_receipts WHERE namespace=%s AND command_id=%s',
                                (Jsonb(members), PREFIX + row['namespace'], row['page_id'])).fetchone()
    if not stored or not stored['same']:
        return False
    for member, outcome in zip(members, outcomes):
        disposition = outcome.get('disposition')
        if disposition not in ('accepted','existing','deferred','rejected') or not isinstance(outcome.get('next_action'),str) or not outcome['next_action'].strip():
            return False
        if disposition in ('accepted','existing'):
            if not isinstance(outcome.get('job_id'), str):
                return False
            semantic_key = [member.get(key) for key in (
                'document_id','source_revision','case_id','membership_revision','product_type',
                'processing_contract','reference_set','selection_policy','intent_id')]
            # Receipts retain the ID returned at admission: either the exact
            # alias, or its canonical Job when adoption had already completed.
            job = connection.execute('''SELECT j.input FROM admission_jobs j
                JOIN admission_jobs receipt_job ON receipt_job.id=%s
                WHERE j.semantic_key=%s AND (j.id=receipt_job.id OR j.canonical_job_id=receipt_job.id)''',
                (outcome['job_id'], Jsonb(semantic_key))).fetchone()
            if not job:
                return False
            if (any(label in (member.get('connector'), job['input'].get('connector'))
                    for label in ('synthetic-drive-v1', 'drive-readonly-v1'))
                    and any(job['input'].get(key) != member.get(key) for key in (
                        'connector','source_root','source_id','source_revision','expected_sha256','input_bytes'))):
                return False
        elif outcome.get('known') is not True or not isinstance(outcome.get('reason'),str) or not outcome['reason'].strip() or outcome.get('job_id') is not None:
            return False
    expected = dict(offered=len(members), accepted=sum(item['disposition'] in ('accepted','existing') for item in outcomes),
                    deferred=sum(item['disposition']=='deferred' for item in outcomes),
                    rejected=sum(item['disposition']=='rejected' for item in outcomes))
    return isinstance(receipt.get('totals'),dict) and receipt['totals'] == expected and all(type(value) is int for value in receipt['totals'].values())


def reconcile(ingestion, namespace, page_id):
    if not identity(namespace, page_id):
        return error('invalid_request')
    with psycopg.connect(**ingestion.database, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(714012001)')
        row = load(connection, namespace, page_id)
        if not row:
            return error('unknown', known=False)
        if row['state'] == 'settled':
            return project(connection, row)
        members = row['request']['members']
        if members:
            receipt = ingestion.resolve(PREFIX + namespace, page_id)
            if not isinstance(receipt,dict):
                return error('receipt_malformed', known=False)
            if receipt.get('known') is not True:
                category = receipt.get('category')
                if category not in ('unknown','persistence_unavailable'):
                    return error('receipt_malformed', known=False)
                return error('receipt_' + category, known=False)
            if not valid_receipt(connection, row, receipt):
                return error('receipt_malformed', known=False)
            records = [dict(member=member, disposition=outcome, pending=outcome['disposition'] in ('deferred','rejected'))
                       for member,outcome in zip(members,receipt['members'])]
        else:
            records = []
        candidate = dict(row, records=records, state='settled')
        result = project(connection, candidate)
        result['checkpoint'] = dict(cursor=row['request']['next_cursor'], exhausted=row['request']['next_cursor'] is None)
        if (len(json.dumps(result,ensure_ascii=True)) > 131072 or
                len(json.dumps(records,ensure_ascii=True)) + len(json.dumps(row['request'],ensure_ascii=True)) + 8192 > row['reservation_bytes']):
            return error('receipt_malformed', known=False)
        updated = connection.execute('UPDATE discovery_streams SET cursor=%s,exhausted=%s WHERE namespace=%s AND cursor IS NOT DISTINCT FROM %s AND exhausted=false RETURNING namespace',
            (row['request']['next_cursor'],row['request']['next_cursor'] is None,namespace,row['request']['expected_cursor'])).fetchone()
        if not updated:
            return error('cursor_conflict')
        connection.execute("UPDATE discovery_pages SET records=%s,state='settled' WHERE namespace=%s AND page_id=%s",(Jsonb(records),namespace,page_id))
        return project(connection,load(connection,namespace,page_id))
