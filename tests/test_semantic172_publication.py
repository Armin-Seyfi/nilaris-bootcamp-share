"""ROOT ONLY: completed supervisor state publishes without another generation.

The fixture uses real offer/capture/claim commands, then records the exact
successful semantic172 finalizer transition. No worker or provider is started.
"""
import os
import subprocess

import psycopg
import pytest
from psycopg.types.json import Jsonb

from nilaris.review import Review
from test_semantic_publication_handoff import captured


def completed_semantic172(database):
    ingestion, job, args = captured(database)
    claim = ingestion.claim(job, args[3])
    assert claim['claimed'] is True and claim['stage'] == 'processing', claim
    exited = subprocess.Popen(['/usr/bin/true'], start_new_session=True)
    exited.wait(timeout=5)
    with psycopg.connect(**database) as connection:
        connection.execute("""UPDATE job_attempts SET stopped=true,
            reason='semantic_completed',worker=%s,accounting_complete=true,
            completion_evidence='process_group_reaped' WHERE id=%s""",
            (Jsonb(dict(pgid=exited.pid, adapter='semantic172-v1')), claim['attempt_id']))
        # worker.run's successful semantic finalizer revokes the owned generation
        # exactly once and stops that same processing attempt in one transaction.
        connection.execute("""UPDATE admission_jobs SET state='completed',
            reason='semantic_completed',revision=revision+1,generation=generation+1,
            held_active_reservation=false WHERE id=%s""", (job,))
    args[3] = ingestion.status(job)['revision']
    return ingestion, job, args


@pytest.mark.parametrize('mutation', [
    None,
    'larger_generation_gap',
    'wrong_job_reason',
    'wrong_attempt_reason',
    'unaccounted',
    'unstopped',
    'latest_acquisition',
    'changed_adapter',
    'missing_reaping_proof',
    'live_process_group',
    'stale_revision',
])
def test_completed_semantic172_publication_preserves_finalizer_fence(database, mutation):
    ingestion, job, args = completed_semantic172(database)
    changes = {
        'larger_generation_gap': 'UPDATE admission_jobs SET generation=generation+1',
        'wrong_job_reason': "UPDATE admission_jobs SET reason='unknown'",
        'wrong_attempt_reason': "UPDATE job_attempts SET reason='unknown' WHERE stage='processing'",
        'unaccounted': "UPDATE job_attempts SET accounting_complete=false WHERE stage='processing'",
        'unstopped': "UPDATE job_attempts SET stopped=false WHERE stage='processing'",
        'latest_acquisition': "UPDATE job_attempts SET stage='acquisition' WHERE stage='processing'",
        'changed_adapter': "UPDATE job_attempts SET worker=jsonb_set(worker,'{adapter}','\"unknown\"') WHERE stage='processing'",
        'missing_reaping_proof': "UPDATE job_attempts SET completion_evidence=NULL WHERE stage='processing'",
    }
    with psycopg.connect(**database) as connection:
        if mutation in changes:
            connection.execute(changes[mutation])
        elif mutation == 'live_process_group':
            connection.execute("""UPDATE job_attempts SET
                worker=jsonb_set(worker,'{pgid}',%s) WHERE stage='processing'""",
                (Jsonb(os.getpgrp()),))
    if mutation == 'stale_revision':
        args[3] -= 1
    before = ingestion.status(job)
    assert before['state'] == 'completed'
    assert before['generation'] == (4 if mutation == 'larger_generation_gap' else 3)
    assert len(before['attempts']) == 2
    result = Review(database).promote_semantic(*args)
    after = ingestion.status(job)
    assert after['generation'] == before['generation']
    assert after['attempts'] == before['attempts']
    if mutation is None:
        assert result['saved'] is True, result
        assert result['approved'] is False
        assert after['revision'] == before['revision'] + 1
        assert after['active_capacity_reserved'] is False
        assert len(ingestion.result(job)['publications']) == 1
        assert Review(database).promote_semantic(*args) == result
    else:
        assert result['saved'] is False, result
        assert result['category'] == ('revision_conflict' if mutation == 'stale_revision'
                                      else 'active_job_conflict')
        assert after['revision'] == before['revision']
        assert ingestion.result(job)['publications'] == []
