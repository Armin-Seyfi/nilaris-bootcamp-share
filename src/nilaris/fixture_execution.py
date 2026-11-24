"""A single bounded HTTP execution runner over existing claims and supervision."""
import threading

from nilaris.execution import conflict


class FixtureExecution:
    def __init__(self, ingestion, source, *, captured_policy=None):
        self.ingestion = ingestion
        self.source = source
        self.captured_policy = captured_policy
        self._slot = threading.Lock()

    def execute_fixture_v1(self, job_id, expected_revision):
        if self.source is None and self.captured_policy is None:
            return conflict(job_id, 'unapproved_source')
        if not self._slot.acquire(blocking=False):
            return conflict(job_id, 'active_capacity')
        dispatched = False
        try:
            before = self.ingestion.status(job_id)
            if before.get('known') is False:
                return before
            policy = self.source if before.get('state') == 'acquiring' else self.captured_policy or self.source
            if (before.get('state') not in ('acquiring', 'queued') or
                    before.get('revision') != expected_revision or
                    policy is None or not policy.permits(before.get('input'))):
                return conflict(job_id)
            owned = None
            prepared = None
            if before['state'] == 'acquiring':
                owned = self.ingestion.claim(job_id, expected_revision)
                if not owned.get('claimed'):
                    return owned
                # The acquisition claim is already durable. No token crosses HTTP.
                response = dict(known=True, category='execution_acknowledged',
                                affected=dict(job_id=job_id, attempt_id=owned['attempt_id']),
                                coverage='pending', reason='Acquisition claim stored; query Job for outcome.',
                                next_action='query_job')
            else:
                if before.get('content_pin') != before['input']['expected_sha256']:
                    return conflict(job_id, 'invalid_captured_input')
                from nilaris.worker import prepare_fixture
                prepared = prepare_fixture(self.ingestion, job_id, expected_revision)
                if not prepared.get('prepared'):
                    return prepared
                response = dict(known=True, category='processing_acknowledged', state='running',
                                affected=dict(job_id=job_id,
                                    attempt_id=prepared['owned']['attempt_id']),
                                content_pin=before['content_pin'], coverage='pending',
                                next_action='query_job',
                                reason='Processing claim stored; query Job for outcome.')
            runner = threading.Thread(target=self._run, args=(job_id, owned, prepared), daemon=False)
            runner.start()
            dispatched = True
            return response
        finally:
            if not dispatched:
                self._slot.release()

    def _run(self, job_id, owned, prepared):
        try:
            if owned is None:
                from nilaris.worker import run_prepared_fixture
                run_prepared_fixture(self.ingestion, job_id, prepared)
                return
            captured = self.ingestion.capture(job_id, owned, self.source.controlled_source())
            if captured.get('state') == 'queued':
                self.ingestion.run_fixture(job_id, captured['revision'])
        except Exception:
            # Never fabricate completion/termination. Durable status and reviewed
            # reconciliation expose an interrupted claim; do not retry automatically.
            pass
        finally:
            self._slot.release()
