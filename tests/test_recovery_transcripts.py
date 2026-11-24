"""Real process interruption and competing caller transcripts at Ingestion.

These finite literal-stage callers complement the supervised graph tests. Gates
are before/after public publish, not inside PostgreSQL's commit implementation.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from test_admission import member


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ['/opt/miniconda3/bin/conda', 'run', '--no-capture-output', '-n', 'nilaris',
          'python', str(ROOT / 'runtime/a1/with_overlay.py')]


class Caller:
    def __init__(self, process):
        self.process = process
        self.buffer = b''
        self.pid = None

    def send(self, value):
        self.process.stdin.write((value + '\n').encode())
        self.process.stdin.flush()

    def receive(self):
        deadline = time.monotonic() + 15
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'private caller response deadline exceeded'
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            assert ready, 'private caller response deadline exceeded'
            chunk = os.read(self.process.stdout.fileno(), 65536)
            assert chunk, 'private caller ended without response; diagnostics withheld'
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        return json.loads(line)

    def response(self):
        event = self.receive()
        assert event['event'] == 'response'
        return event['result']

    def kill_caller(self):
        # PID comes from this freshly spawned harness, never a process search.
        os.kill(self.pid, signal.SIGKILL)
        self.process.wait(timeout=10)
        assert self.process.returncode != 0


@contextmanager
def caller(database, operation, job_id, **arguments):
    process = subprocess.Popen(
        [*PYTHON, str(ROOT / 'tests/recovery_transcript_process.py')], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, bufsize=0)
    control = Caller(process)
    try:
        control.send(json.dumps(dict(database=database, operation=operation,
                                     job_id=job_id, **arguments)))
        ready = control.receive()
        assert ready['event'] == 'ready'
        control.pid = ready['pid']
        yield control
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        process.stdin.close()
        process.stdout.close()


def queued(database):
    migrate(database)
    client = Ingestion(database, AdmissionProfile.l1())
    offered = client.offer('transcript-operator', 'offer', [member()])
    assert offered['totals']['accepted'] == 1
    job_id = offered['members'][0]['job_id']
    acquisition = client.claim(job_id, client.status(job_id)['revision'])
    assert acquisition.get('claimed') is True
    captured = client.capture(job_id, acquisition,
                              ControlledSource({'P': ('r1', b'controlled input')}))
    assert captured['state'] == 'queued'
    return client, job_id


def restarted_read(database, job_id):
    with caller(database, 'read', job_id) as reader:
        reader.send('go')
        return reader.response()


def test_process_stop_before_publication_preserves_job_and_fences_old_return(database):
    client, job_id = queued(database)
    claim = client.claim(job_id, client.status(job_id)['revision'])
    assert claim.get('claimed') is True
    with caller(database, 'publish', job_id, claim=claim) as interrupted:
        interrupted.kill_caller()  # after claim, before public publication
    recovered = restarted_read(database, job_id)
    assert recovered['status']['state'] == 'running'
    assert recovered['status']['counters']['attempts'] == 2
    assert recovered['status']['active_capacity_reserved'] is True
    assert recovered['status']['coverage'] == dict(acquisition='captured', processing='not_started')
    assert recovered['result']['effects'] == []
    assert recovered['result']['publications'] == []
    status = recovered['status']
    with caller(database, 'publish', job_id, claim=claim) as returned:
        # This real caller already holds the old claim when recovery revokes it.
        held = client.reconcile(job_id, status['revision'], status['generation'],
                                'transcript-operator', 'caller killed before publication')
        assert held['state'] == 'held'
        assert held['generation'] > status['generation']
        assert held['active_capacity_reserved'] is True
        assert held['next_action'] == 'reconcile_interruption'
        returned.send('go')
        assert returned.receive()['event'] == 'returned_without_response'
        returned.send('deliver')
        assert returned.response()['category'] == 'stale_commit'
    final = restarted_read(database, job_id)
    assert final['status']['state'] == 'held'
    assert final['result'] == recovered['result']
    print('I1/I5 process-stop: before publish -> restarted running/no effects -> '
          'reviewed held/new generation -> restarted old caller stale_commit/no effects')


def test_committed_result_survives_killed_response_and_restarted_replay(database):
    client, job_id = queued(database)
    claim = client.claim(job_id, client.status(job_id)['revision'])
    assert claim.get('claimed') is True
    with caller(database, 'publish', job_id, claim=claim) as lost_response:
        lost_response.send('go')
        assert lost_response.receive()['event'] == 'returned_without_response'
        committed = client.result(job_id)
        assert len(committed['publications']) == 1
        assert len(committed['effects']) == 2
        lost_response.kill_caller()  # public call committed; caller response lost
    recovered = restarted_read(database, job_id)
    assert recovered['status']['state'] == 'completed'
    assert recovered['status']['effect_count'] == 2
    assert recovered['status']['counters']['attempts'] == 2
    assert recovered['status']['usage']['retained_bytes'] > 0
    assert recovered['status']['next_action'] == 'terminal'
    assert recovered['result'] == committed
    initial = committed['publications'][0]
    assert initial['kind'] == 'initial'
    assert initial['candidates'][0]['label'] == 'Pressure'
    with caller(database, 'publish', job_id, claim=claim) as replay:
        replay.send('go')
        assert replay.receive()['event'] == 'returned_without_response'
        replay.send('deliver')
        assert replay.response()['state'] == 'completed'
    assert restarted_read(database, job_id)['result'] == committed
    print('I1/I4/I5 lost response: committed initial=1/effects=2 -> caller SIGKILL -> '
          'fresh process completed -> old delivery replay completed; immutable result/effect IDs preserved')


def test_real_queued_cancel_claim_race_reports_winner_and_preserves_completion(database):
    client, job_id = queued(database)
    revision = client.status(job_id)['revision']
    with caller(database, 'claim', job_id, revision=revision) as claimant:
        with caller(database, 'cancel', job_id, revision=revision) as canceller:
            # Both independent processes are ready before either public call.
            claimant.send('go')
            canceller.send('go')
            claimed, cancelled = claimant.response(), canceller.response()
    current = restarted_read(database, job_id)
    if cancelled.get('state') == 'cancelled':
        assert claimed.get('category') == 'claim_conflict'
        assert current['status']['state'] == 'cancelled'
        assert current['result']['effects'] == []
        assert current['result']['publications'] == []
        print('I9 concurrent processes: effective queued cancellation won; claim_conflict; no effects')
    else:
        assert claimed.get('claimed') is True
        assert cancelled.get('category') == 'cancel_conflict'
        assert current['status']['state'] == 'running'
        print('I9 concurrent processes: claim won; cancellation reports cancel_conflict; current state running')
    # Independently demonstrate the completion side of the cancellation race,
    # regardless of which scheduler outcome the simultaneous race produced.
    if cancelled.get('state') == 'cancelled':
        item = dict(member('completion'), document_id='completion-document')
        other = client.offer('transcript-operator', 'completion', [item])['members'][0]['job_id']
        client.capture(other, client.claim(other, 0),
                       ControlledSource({'P': ('r1', b'controlled input')}))
        job_id = other
        claimed = client.claim(job_id, client.status(job_id)['revision'])
        assert claimed.get('claimed') is True
        revision = client.status(job_id)['revision']
    revision = client.status(job_id)['revision']
    with caller(database, 'publish', job_id, claim=claimed) as completing:
        with caller(database, 'cancel', job_id, revision=revision) as late_cancel:
            completing.send('go')
            late_cancel.send('go')
            assert completing.receive()['event'] == 'returned_without_response'
            assert late_cancel.response()['category'] == 'cancel_conflict'
            completing.send('deliver')
            assert completing.response()['state'] == 'completed'
    committed = client.result(job_id)
    recovered = restarted_read(database, job_id)
    assert recovered['status']['state'] == 'completed'
    assert recovered['result'] == committed
    assert len(committed['effects']) == 2
    assert len(committed['publications']) == 1
    print('I9 concurrent completion/cancel: cancellation cancel_conflict; fresh process completed/initial=1/effects=2')
