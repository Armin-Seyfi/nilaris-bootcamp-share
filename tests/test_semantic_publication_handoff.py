"""Root-run promotion gates over finite captured handoffs; no generation."""
import hashlib
import os
import subprocess

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from nilaris.review import Review
from test_admission import member
from test_semantic_publication import toy, repin


def captured(database, *, document='toy', product_type='Semantic handoff', command='one'):
    migrate(database)
    ingestion = Ingestion(database, AdmissionProfile.l1())
    item = dict(member(), document_id=document, product_type=product_type)
    receipt = ingestion.offer('handoff', command, [item])
    offered = receipt['members'][0]
    assert offered['disposition'] == 'accepted', offered
    job = offered['job_id']
    source = b'Rated pressure: 12.00 bar'
    ingestion.capture(job, ingestion.claim(job, 0), ControlledSource({'P': ('r1', source)}))
    artifact, capture, config, _ = toy()
    artifact['target_document_id'] = document
    for observation in artifact['initial_output']['observations']:
        observation['document_id'] = document
    for candidate in artifact['initial_output']['candidates']:
        for reference in candidate['evidence_refs']:
            reference['document_id'] = document
    capture['documents'][0]['document_id'] = document
    capture['documents'][0]['content_pin'] = hashlib.sha256(source).hexdigest()
    args = ['handoff', 'promote-' + command, job, ingestion.status(job)['revision'], None,
            'synthetic-expert', artifact, capture, config, repin(artifact, capture, config), True]
    return ingestion, job, args


def test_finite_capture_promotes_without_another_attempt_and_releases_capacity(database):
    ingestion, job, args = captured(database)
    before = ingestion.status(job)
    assert before['state'] == 'queued' and before['active_capacity_reserved'] is True
    review = Review(database)
    result = review.promote_semantic(*args)
    assert result['saved'] is True and result['approved'] is False
    after = ingestion.status(job)
    assert after['state'] == 'completed' and after['active_capacity_reserved'] is False
    assert after['attempts'] == before['attempts']
    assert after['generation'] == before['generation']
    assert after['reservation_bytes'] == after['usage']['retained_bytes']
    assert after['reservation_bytes'] < before['reservation_bytes']
    assert review.promote_semantic(*args) == result
    changed = list(args)
    changed[5] = 'different-expert'
    assert review.promote_semantic(*changed)['category'] == 'command_conflict'
    draft = review.create('handoff', 'draft', job, result['proposal_id'], 'Semantic handoff', None, 'synthetic-expert')
    assert draft['saved'] is True
    view = review.read(draft['draft_id'])
    assert view['definitions'][0]['decision'] == 'awaiting'
    assert view['approved_version'] is None


@pytest.mark.parametrize('mutation,category', [
    ("UPDATE job_attempts SET stopped=false", 'active_job_conflict'),
    ("UPDATE job_attempts SET completion_evidence=NULL", 'active_job_conflict'),
    ("UPDATE job_attempts SET acquisition_read_token='unknown-read'", 'active_job_conflict'),
    ("UPDATE job_attempts SET stage='unknown'", 'active_job_conflict'),
    ("UPDATE admission_jobs SET generation=generation+1", 'active_job_conflict'),
    ("DELETE FROM job_attempts", 'active_job_conflict'),
    ("UPDATE admission_jobs SET captured_input=NULL", 'source_identity_conflict'),
    ("UPDATE admission_jobs SET captured_input=decode('00','hex')", 'source_identity_conflict'),
    ("UPDATE admission_jobs SET processing_key=NULL", 'source_identity_conflict'),
    ("UPDATE admission_jobs SET input=jsonb_set(input,'{source_revision}','\"other-revision\"')", 'source_identity_conflict'),
    ("UPDATE admission_jobs SET input=jsonb_set(input,'{case_id}','\"other-source\"')", 'source_identity_conflict'),
])
def test_uncertain_or_changed_capture_never_releases_reservation(database, mutation, category):
    ingestion, job, args = captured(database)
    # Corrupt legacy/accounting states are setup only; observe through public commands.
    with psycopg.connect(**database) as connection:
        connection.execute(mutation)
    before = ingestion.status(job)
    result = Review(database).promote_semantic(*args)
    assert result['saved'] is False and result['category'] == category
    after = ingestion.status(job)
    assert after['active_capacity_reserved'] is True
    assert after['revision'] == before['revision']
    assert after['attempts'] == before['attempts']
    assert ingestion.result(job)['publications'] == []


def test_stale_and_wrong_base_promotions_keep_capture_reserved(database):
    ingestion, job, args = captured(database)
    review = Review(database)
    stale = list(args)
    stale[1], stale[3] = 'stale', args[3] - 1
    assert review.promote_semantic(*stale)['category'] == 'revision_conflict'
    wrong_base = list(args)
    wrong_base[1], wrong_base[4] = 'wrong-base', 'unknown-approval'
    assert review.promote_semantic(*wrong_base)['category'] == 'base_conflict'
    assert ingestion.status(job)['active_capacity_reserved'] is True
    assert ingestion.result(job)['publications'] == []


def test_changed_target_pin_cannot_publish_captured_bytes(database):
    ingestion, job, args = captured(database)
    args[7]['documents'][0]['content_pin'] = 'f' * 64
    args[9] = repin(args[6], args[7], args[8])
    result = Review(database).promote_semantic(*args)
    assert result['category'] == 'source_identity_conflict'
    assert ingestion.status(job)['active_capacity_reserved'] is True
    assert ingestion.result(job)['publications'] == []


@pytest.mark.parametrize('proof', ['missing', 'unaccounted', 'live', 'reaped'])
def test_worker_accounting_proof_is_required_before_promotion(database, proof):
    ingestion, job, args = captured(database)
    # A prior worker's migration/accounting evidence must be explicit. A negative
    # process group has exited; the current group is independently live.
    exited = subprocess.Popen(['/usr/bin/true'], start_new_session=True)
    exited.wait(timeout=5)
    worker = {'pgid': os.getpgrp() if proof == 'live' else exited.pid}
    with psycopg.connect(**database) as connection:
        connection.execute('UPDATE job_attempts SET worker=%s,accounting_complete=%s,completion_evidence=%s',
                           (Jsonb(worker), proof != 'unaccounted',
                            None if proof == 'missing' else 'process_group_reaped'))
    result = Review(database).promote_semantic(*args)
    assert result['saved'] is (proof == 'reaped')
    if proof != 'reaped':
        assert result['category'] == 'active_job_conflict'
    assert ingestion.status(job)['active_capacity_reserved'] is (proof != 'reaped')


def test_active_draft_and_approval_remain_unchanged_by_incoming_promotion(database):
    ingestion, job, args = captured(database)
    review = Review(database)
    proposal = review.promote_semantic(*args)['proposal_id']
    draft = review.create('handoff', 'draft', job, proposal, 'Semantic handoff', None, 'synthetic-expert')['draft_id']
    before = review.read(draft)
    incoming_ingestion, incoming_job, incoming = captured(database, document='incoming-toy', command='incoming')
    assert review.promote_semantic(*incoming)['category'] == 'active_draft_conflict'
    assert review.read(draft) == before
    assert incoming_ingestion.status(incoming_job)['active_capacity_reserved'] is True
    for definition in before['definitions']:
        definition['decision'] = 'accepted'
    for association in before['associations']:
        association['disposition'] = 'accepted'
    saved = review.save('handoff', 'save', draft, 0, before['definitions'], before['associations'],
                        'synthetic-expert', 'Explicit toy review')
    approved = review.approve('handoff', 'approve', draft, saved['revision'], 'Semantic handoff', 'synthetic-expert', True)
    assert approved['approved'] is True
    frozen = review.read(draft)
    incoming[1] = 'after-approval'
    assert review.promote_semantic(*incoming)['category'] == 'base_conflict'
    incoming[1], incoming[4] = 'with-base', frozen['approved_version']
    assert review.promote_semantic(*incoming)['saved'] is True
    assert review.read(draft) == frozen
