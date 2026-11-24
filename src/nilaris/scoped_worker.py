"""One bounded, customer-bound worker and a read-only transport recovery path."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import time

from psycopg import Connection
from psycopg.types.json import Jsonb

from nilaris import membership185
from nilaris.admission import Ingestion
from nilaris.execution import claim, current, locked_job, record_captured_input
from nilaris.local_transport import HandoffRefused, LocalTransport
from nilaris.onboarding182 import CustomerAccessDenied
from nilaris.scoped_intake import controlled_source, current_policy, current_source, policy_hold


@dataclass(frozen=True)
class WorkerJob:
    registry: Connection
    customer: dict
    database: Connection
    journal: dict


class ScopedWorker:
    def __init__(self, intake, customer, binding):
        self.intake = intake
        self.service = intake.service
        self.customer = customer
        self.binding = binding

    @contextmanager
    def _job(self, job_id, *, sending=False):
        if self.binding != self.intake.bindings.get(self.customer):
            raise CustomerAccessDenied('worker binding unavailable')
        with self.service.worker_customer_boundary(self.customer, sending=sending) as (registry,row):
            with self.intake.database(row) as c:
                job = c.execute('''SELECT * FROM scoped_jobs
                    WHERE job_id=%s AND customer_id=%s AND binding=%s FOR UPDATE''',
                    (job_id,self.customer,self.binding.digest(self.customer))).fetchone()
                if not job:
                    raise CustomerAccessDenied('scoped job unavailable')
                yield WorkerJob(registry, row, c, job)

    def _hold_reason(self, work):
        row, job = work.customer, work.journal
        if row['access_state'] != 'active' or row['access_revision'] != job['access_revision']:
            return 'customer_access_changed'
        source = current_source(work.database,row)
        if source.needs_operator:
            return 'source_binding_unresolved'
        if source.pin != job['binding'] or source.revision != job['source_revision']:
            return 'source_authority_changed'
        b = self.binding
        member = membership185.member(work.registry, self.customer, b.issuer, b.subject, b.recipient)
        if (not member or not member['active'] or member['revision'] != job['member_revision']
                or not self.service.identities.current_customer(b.issuer, b.subject, b.recipient)):
            return 'source_authority_changed'
        policy = current_policy(work.database)
        if policy['revision'] != job['policy_revision']:
            return 'disclosure_policy_changed'
        return policy_hold(policy['policy'])

    @staticmethod
    def _hold(c, job, reason):
        c.execute('SELECT pg_advisory_xact_lock(714012001)')
        c.execute("UPDATE scoped_jobs SET state='held',reason=%s WHERE job_id=%s", (reason, job['job_id']))
        if job['claim']:
            claim = job['claim']
            c.execute("""UPDATE job_attempts SET stopped=true,reason=%s,completion_evidence='finite_controlled_call'
                WHERE id=%s AND job_id=%s AND generation=%s AND token=%s AND NOT stopped""",
                (reason,claim['attempt_id'],job['job_id'],claim['generation'],claim['token']))
        # Keep a running Job's slot until a verified successor can atomically
        # exchange its waiting slot for it under the admission lock.
        c.execute("""UPDATE admission_jobs SET state='held',reason=%s,revision=revision+1,
            held_active_reservation=held_active_reservation OR state='running'
            WHERE id=%s AND state IN ('acquiring','queued','running')""",(reason,job['job_id']))

    def _status(self, work):
        job = work.journal
        return self.intake._status(work.database,self.customer,job['job_id'],job['binding'])

    def _ready(self, work, states, original=None):
        job = work.journal
        if job['state'] not in states or (original and original != job['claim']):
            return False
        reason = self._hold_reason(work)
        if reason:
            if reason != 'source_binding_unresolved':
                self._hold(work.database,job,reason)
            return False
        return True

    def _claim(self, c, job, observe):
        """Commit both finite claims, local pinned bytes and the scoped journal together."""
        job_id = job['job_id']
        ingestion = Ingestion(c.info.get_parameters(), scoped_binding=job['binding'],
                              monitor_path=self.service.provisioner.config['service_monitor_path'])
        if job['state'] == 'claimed':
            original = job['claim']
            if current(c, job_id, original, 'processing', scoped_binding=job['binding']):
                return original
            # Only a pre-intention, expired, finite local attempt can hand over.
            # Its generation and accounting remain in the original Job history.
            expired = c.execute("""UPDATE job_attempts SET stopped=true,reason='expired_before_intent',
                completion_evidence='finite_controlled_call' WHERE id=%s AND job_id=%s AND token=%s
                AND generation=%s AND expires_at<=clock_timestamp() AND worker IS NULL AND NOT stopped
                RETURNING id""",(original['attempt_id'],job_id,original['token'],original['generation'])).fetchone()
            if not expired:
                self._hold(c,job,'claim_unavailable_before_handoff')
                return None
            c.execute("""UPDATE admission_jobs SET state='queued',held_active_reservation=true,revision=revision+1
                WHERE id=%s AND generation=%s AND state='running'""",(job_id,original['generation']))
            c.execute("UPDATE scoped_jobs SET state='queued',reason='pre_intent_recovery',claim=NULL WHERE job_id=%s",
                      (job_id,))
        status = locked_job(c, ingestion, job_id)
        if status['state'] == 'acquiring':
            acquisition = claim(ingestion,job_id,status['revision'],transaction=c)
            if not acquisition.get('claimed'):
                return None
            observe('acquisition_claim')
            owned = current(c,job_id,acquisition,'acquisition',scoped_binding=job['binding'])
            if not owned:
                raise RuntimeError('finite acquisition claim expired')
            wall, cpu = time.monotonic(), time.process_time()
            content = controlled_source(job['source_key'])
            if hashlib.sha256(content).hexdigest() != job['content_pin']:
                content = None
            # This package file has no remote read, external fee or persistent
            # side effect. Byte capture and its resource charge commit atomically.
            elapsed_wall, elapsed_cpu = time.monotonic()-wall, time.process_time()-cpu
            c.execute("""UPDATE job_attempts SET acquisition_requests=acquisition_requests+1,
                wall_seconds=wall_seconds+%s,cpu_seconds=cpu_seconds+%s,measured_cpu_seconds=%s,
                cpu_accounting='measured_finite_source_call' WHERE id=%s""",
                (elapsed_wall,elapsed_cpu,elapsed_cpu,acquisition['attempt_id']))
            refusal = record_captured_input(ingestion,c,job_id,acquisition,owned,content,0)
            status = locked_job(c, ingestion,job_id)
            if refusal or status['state'] != 'queued':
                self._hold(c,job,status['reason'])
                return None
        if status['state'] != 'queued':
            self._hold(c,job,'processing_claim_unavailable')
            return None
        original = claim(ingestion,job_id,status['revision'],transaction=c)
        if not original.get('claimed'):
            if original.get('category') == 'resource_exhausted':
                self._hold(c,job,'processing_claim_limit')
            return None
        observe('processing_claim')
        c.execute("UPDATE scoped_jobs SET claim=%s,state='claimed',reason='processing_claim' WHERE job_id=%s",
                  (Jsonb(original),job_id))
        return original

    def run(self, job_id, *, checkpoint=None):
        """Only the caller which records the intention may cross its handoff boundary."""
        observe = checkpoint or (lambda phase: None)
        observe('queued')
        with self._job(job_id) as work:
            if not self._ready(work,('queued','claimed')):
                return self._status(work)
            original = self._claim(work.database,work.journal,observe)
            if not original:
                return self._status(work)
        observe('claimed')
        with self._job(job_id) as work:
            if not self._ready(work,('claimed',),original):
                return self._status(work)
        observe('prechecked')
        with self._job(job_id) as work:
            if not self._ready(work,('claimed',),original):
                return self._status(work)
            work.database.execute("""UPDATE scoped_jobs SET state='uncertain',reason='intent_recorded'
                WHERE job_id=%s""",(job_id,))
        observe('intent')
        with self._job(job_id, sending=True) as work:
            c, job = work.database, work.journal
            if (job['reason'] != 'intent_recorded' or
                    not self._ready(work,('uncertain',),original)):
                return self._status(work)
            owned = current(c,job_id,original,'processing',scoped_binding=job['binding'])
            if not owned:
                self._hold(c,job,'claim_expired_before_handoff')
                return self._status(work)
            payload = bytes(owned['captured_input'])
            if hashlib.sha256(payload).hexdigest() != job['content_pin']:
                self._hold(c, job, 'source_pin_changed')
                return self._status(work)
            transport = LocalTransport(self.service.provisioner.directory, self.customer)
            self.service.provisioner._require_monitor()
            try:
                response = transport.start(job['operation_id'],payload)
            except HandoffRefused:
                self._hold(c, job, 'local_receiver_unavailable_before_handoff')
                return self._status(work)
            except OSError:
                return self._status(work)
            observe('datagram_accepted')
            c.execute("UPDATE scoped_jobs SET reason='handoff_response_pending' WHERE job_id=%s", (job_id,))
        observe('handed_off')
        try:
            receipt = response.wait()
        except (OSError, TimeoutError):
            receipt = None
        return self._finish(job_id, receipt)

    def reconcile(self, job_id):
        with self._job(job_id) as work:
            job = work.journal
            if job['state'] != 'uncertain':
                return self._status(work)
            receipt = LocalTransport(self.service.provisioner.directory,self.customer).lookup(job['operation_id'])
        return self._finish(job_id, receipt)

    def _finish(self, job_id, receipt):
        with self._job(job_id) as work:
            c, job = work.database, work.journal
            if job['state'] == 'uncertain' and receipt is not None:
                expected = c.execute('SELECT octet_length(captured_input) AS size FROM admission_jobs WHERE id=%s',
                                     (job_id,)).fetchone()
                if receipt != {'sha256': job['content_pin'], 'bytes': expected['size']}:
                    raise ValueError('original receiver evidence required')
                claim = job['claim']
                original = c.execute("""UPDATE admission_jobs SET state='completed',
                    reason='controlled_receiver_verified',revision=revision+1 WHERE id=%s
                    AND state='running' AND generation=%s RETURNING id""",
                    (job_id,claim['generation'])).fetchone()
                if original:
                    c.execute("""UPDATE job_attempts SET stopped=true,reason='controlled_receiver_verified',
                        completion_evidence='finite_controlled_call' WHERE id=%s AND job_id=%s AND token=%s""",
                        (claim['attempt_id'], job_id, claim['token']))
                    c.execute("""UPDATE scoped_jobs SET state='completed',reason='controlled_receiver_verified',
                        receipt=%s WHERE job_id=%s""", (Jsonb(receipt),job_id))
            return self._status(work)
