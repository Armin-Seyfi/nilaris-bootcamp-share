"""One finite intake invocation; durable journals remain the only progress authority.

No credential lookup or provider construction occurs here. The CLI enables only
existing controlled synthetic sources. Drive requires an explicitly injected
command transport backed by the existing supervised DriveJourney.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from nilaris.drive import PinnedDrive
from nilaris.drive155_source import SourceBinding
from nilaris.synthetic_fixture_drive import SyntheticFixtureDrive

CAPABILITIES = dict(metadata_discovery=True, exact_revision=True,
                    conditional_read=False, pagination=False, recursive_scope=False,
                    stable_object_identity=True)
PHASES = ('discover', 'admit', 'acquire', 'process')


def plan_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('ascii')).hexdigest()


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()


class DriveAdapter:
    """Exact P/PDF policy metadata; dispatch stays inside DriveJourney.

    v1 binding and authority bytes are never extended or rekeyed. The envelope
    describes capabilities without implying new historical permissions.
    """
    kind = 'drive-readonly-v1'

    def __init__(self, configuration, authority):
        binding = SourceBinding(configuration, authority)
        self._member = binding.allowed_member
        self._snapshot = dict(version=1, provider=self.kind,
            binding_id=configuration['binding_id'], namespace=binding.namespace,
            binding_digest=binding.configuration_digest, authority_id=authority['authority_id'],
            policy_version='drive-p-pdf-v1', capabilities=copy.deepcopy(CAPABILITIES))

    @property
    def snapshot(self):
        return copy.deepcopy(self._snapshot)

    @property
    def member(self):
        return copy.deepcopy(self._member)

    def discover(self, plan):
        return dict(operation='drive_discover', binding_id=self.snapshot['binding_id'],
                    expected_namespace=self.snapshot['namespace'],
                    operation_id=plan['discovery_operation'], page_id=plan['page_id'])

    def acquire(self, plan, job):
        return dict(operation='drive_acquire', binding_id=self.snapshot['binding_id'],
            expected_namespace=self.snapshot['namespace'],
            operation_id=plan['acquisition_operation'], job_id=job['id'],
            expected_revision=job['revision'])


class SyntheticAdapter:
    """One exact offer from an existing controlled source, never a new provider."""
    kind = 'synthetic-drive-v1'

    def __init__(self, source, member, connection):
        keys = {'version', 'customer_id', 'principal', 'credential_binding_id',
                'policy_version', 'authority_id', 'source_root'}
        if (not isinstance(source, (PinnedDrive, SyntheticFixtureDrive))
                or not source.permits(member) or set(connection) != keys
                or type(connection['version']) is not int or connection['version'] != 1
                or not all(_text(connection[k]) for k in keys - {'version'})
                or connection['source_root'] != member['source_root']):
            raise ValueError('controlled source unavailable')
        self._member = copy.deepcopy(member)
        binding = dict(connection=connection, member=self.member)
        digest = plan_digest(binding)
        self._snapshot = dict(version=1, provider=self.kind, binding_id=digest,
            namespace='intake167:' + digest, binding_digest=digest,
            authority_id=connection['authority_id'], policy_version=connection['policy_version'],
            capabilities=copy.deepcopy(CAPABILITIES))

    @property
    def snapshot(self):
        return copy.deepcopy(self._snapshot)

    @property
    def member(self):
        return copy.deepcopy(self._member)

    def discover(self, plan):
        return dict(operation='discovery_stage', namespace=self.snapshot['namespace'],
            page_id=plan['page_id'], expected_cursor=None, next_cursor=None,
            members=[copy.deepcopy(self.member)])

    def acquire(self, plan, job):
        # The established synthetic fixture command captures and processes under
        # real claims/supervision. Both permissions must be granted together.
        return dict(operation='execute_fixture_v1', job_id=job['id'],
                    expected_revision=job['revision'])


def freeze_plan(raw, adapter):
    """Validate a nonsecret plan before any command; return an independent copy."""
    keys = {'version', 'adapter', 'member', 'source_validator', 'provenance_locator',
            'discovery_operation', 'acquisition_operation', 'page_id', 'phases', 'fences', 'limits'}
    try:
        value = copy.deepcopy(raw)
        if (not isinstance(adapter, (DriveAdapter, SyntheticAdapter))
                or not isinstance(value, dict) or set(value) != keys
                or type(value['version']) is not int or value['version'] != 1
                or value['adapter'] != adapter.snapshot or value['member'] != adapter.member
                or value['source_validator'] is not None
                or not all(_text(value[k]) for k in ('provenance_locator',
                    'discovery_operation', 'acquisition_operation', 'page_id'))
                or value['discovery_operation'] == value['acquisition_operation']
                or not isinstance(value['phases'], list)
                or any(p not in PHASES for p in value['phases'])
                or len(set(value['phases'])) != len(value['phases'])
                or set(value['fences']) != {'acquire', 'process'}
                or any(set(f) != {'revision', 'generation'} or
                    any(type(n) is not int or n < 0 for n in f.values())
                    for f in value['fences'].values())
                or set(value['limits']) != {'command_calls', 'input_bytes'}
                or type(value['limits']['command_calls']) is not int
                or not 1 <= value['limits']['command_calls'] <= 64
                or type(value['limits']['input_bytes']) is not int
                or not adapter.member['input_bytes'] <= value['limits']['input_bytes'] <= 1024**2):
            raise ValueError
        if isinstance(adapter, SyntheticAdapter) and ('acquire' in value['phases']) != ('process' in value['phases']):
            raise ValueError
        if len(json.dumps(value, allow_nan=False)) > 16384:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('intake plan unavailable') from None


def _stop(category, **evidence):
    return dict(known=True, category=category, coverage='unknown',
        next_action='read_original_operation_and_job', **evidence)


def run_once(raw, adapter, command, *, authority):
    """Read originals, advance only known unstarted authorized phases, then stop.

    `authority` records a separately scoped owner grant; it is not a credential
    or a mechanism for obtaining owner approval. No retry/polling loop is used.
    """
    try:
        plan = freeze_plan(raw, adapter)
    except ValueError:
        return _stop('intake_plan_refused')
    if (not isinstance(authority, dict) or type(authority.get("version")) is not int
            or authority != dict(version=1, plan_digest=plan_digest(plan), phases=plan['phases'])):
        return _stop('intake_authority_required')
    calls = 0
    def send(operation, **fields):
        nonlocal calls
        if calls >= plan['limits']['command_calls']:
            raise ValueError('intake_command_limit')
        calls += 1
        try:
            response = command(dict(operation=operation, **fields))
        except (OSError, TimeoutError):
            raise ValueError("intake_response_unavailable") from None
        if not isinstance(response, dict):
            raise ValueError('intake_response_unavailable')
        return response
    def dispatch(request):
        return send(**request)
    def permitted(phase):
        if phase not in plan['phases']:
            raise ValueError('intake_phase_not_authorized')
    namespace, page_id = adapter.snapshot['namespace'], plan['page_id']
    try:
        # All original durable identities are read before any possible effect.
        journals = {}
        if isinstance(adapter, DriveAdapter):
            for phase, key in (('discover', 'discovery_operation'), ('acquire', 'acquisition_operation')):
                journals[phase] = send('drive_operation', binding_id=adapter.snapshot['binding_id'],
                                       operation_id=plan[key])
                row = journals[phase]
                if row.get('namespace') != namespace:
                    return _stop('intake_identity_mismatch')
                if row.get('state') in ('held', 'pending'):
                    return _stop('intake_operation_held', operation=row)
                if row.get('state') == 'completed':
                    if row.get('namespace') != namespace or row.get('operation_id') != plan[key]:
                        return _stop('intake_identity_mismatch')
                elif row.get('category') != 'source_operation_unknown':
                    return _stop('intake_operation_unknown', operation=row)
        page = send('discovery_read', namespace=namespace, page_id=page_id)
        receipt = send('resolve', namespace='discovery:' + namespace, command_id=page_id)
        if page.get('known') is not True:
            if page.get('category') != 'unknown' or receipt.get('category') != 'unknown':
                return _stop('intake_page_unknown', page=page, receipt=receipt)
            if journals.get('discover', {}).get('state') == 'completed':
                return _stop('intake_identity_mismatch')
            permitted('discover')
            dispatch(adapter.discover(plan))
            # A lost local response is resolved only from the original journals.
            if isinstance(adapter, DriveAdapter):
                journals['discover'] = send('drive_operation',
                    binding_id=adapter.snapshot['binding_id'],
                    operation_id=plan['discovery_operation'])
            page = send('discovery_read', namespace=namespace, page_id=page_id)
            if page.get('known') is not True:
                return _stop('intake_discovery_incomplete', page=page)
        if isinstance(adapter, DriveAdapter):
            original = journals['discover']
            if original.get('state') != 'completed':
                return _stop('intake_operation_unknown', operation=original)
            if (original.get('namespace') != namespace
                    or original.get('operation_id') != plan['discovery_operation']
                    or original.get('page_id') != page_id):
                return _stop('intake_identity_mismatch')
        if (page.get('namespace') != namespace or page.get('page_id') != page_id
                or page.get('expected_cursor') is not None or page.get('next_cursor') is not None
                or not isinstance(page.get('members'), list) or len(page['members']) != 1
                or not isinstance(page['members'][0], dict)
                or page['members'][0].get('member') != adapter.member):
            return _stop('intake_identity_mismatch')
        if receipt.get('receipt_stored') is not True:
            if receipt.get('category') != 'unknown':
                return _stop('intake_admission_unknown', receipt=receipt)
            permitted('admit')
            send('discovery_admit', namespace=namespace, page_id=page_id)
            receipt = send('resolve', namespace='discovery:' + namespace, command_id=page_id)
        if (receipt.get('known') is not True or receipt.get('receipt_stored') is not True
                or not isinstance(receipt.get('members'), list) or len(receipt['members']) != 1
                or not isinstance(receipt['members'][0], dict)
                or receipt['members'][0].get('member_key') != adapter.member['member_key']):
            return _stop('intake_admission_unknown', receipt=receipt)
        disposition = receipt['members'][0]
        job = None
        if disposition.get('disposition') in ('accepted', 'existing') and _text(disposition.get('job_id')):
            job = send('status', job_id=disposition['job_id'])
            if (job.get('known') is not True or job.get('id') != disposition['job_id']
                    or job.get('input') != adapter.member):
                return _stop('intake_identity_mismatch')
        # Reconciliation uses the original receipt, never a locally owned cursor.
        if page.get('state') != 'settled':
            permitted('admit')
            page = send('discovery_reconcile', namespace=namespace, page_id=page_id)
            if page.get('state') != 'settled':
                return _stop('intake_reconciliation_incomplete', page=page, receipt=receipt)
        if job is None:
            return _stop('intake_member_pending', page=page, receipt=receipt)
        if job.get('state') in ('acquiring', 'queued'):
            phase = 'acquire' if job['state'] == 'acquiring' else 'process'
            fence = plan['fences'][phase]
            if any(job.get(k) != v or type(job.get(k)) is not int for k, v in fence.items()):
                return _stop('intake_fence_mismatch', job=job)
            permitted(phase)
            if phase == 'acquire':
                if journals.get('acquire', {}).get('state') == 'completed':
                    return _stop('intake_identity_mismatch')
                dispatch(adapter.acquire(plan, job))
                job = send('status', job_id=job['id'])
                if isinstance(adapter, DriveAdapter) and job.get('state') == 'queued':
                    permitted('process')
                    if any(job.get(k) != v for k, v in plan['fences']['process'].items()):
                        return _stop('intake_fence_mismatch', job=job)
                    send('execute_fixture_v1', job_id=job['id'], expected_revision=job['revision'])
                    job = send('status', job_id=job['id'])
            else:
                send('execute_fixture_v1', job_id=job['id'], expected_revision=job['revision'])
                job = send('status', job_id=job['id'])
        if job.get('state') != 'completed':
            return _stop('intake_incomplete', job=job, page=page, receipt=receipt)
        results = job.get('results')
        if (not isinstance(results, list) or len(results) != 1
                or not isinstance(results[0], dict)
                or results[0].get('kind') != 'initial'
                or results[0].get('id') != results[0].get('initial_publication_id')
                or job.get('content_pin') != adapter.member['expected_sha256']):
            return _stop('intake_result_mismatch', job=job)
        from nilaris.resume167 import read_complete_result
        result, page_count = read_complete_result(lambda request: send(**request), job['id'], results[0]['id'], adapter.member)
        return dict(known=True, category='intake_review_handoff', coverage='complete',
            job=job, page=page, receipt=receipt, result=result, result_pages=page_count,
            next_action='open_existing_review_ui', review_url='http://127.0.0.1:18766/', review_job_id=job['id'])
    except ValueError as error:
        return _stop(str(error) if str(error).startswith('intake_') else 'intake_result_refused')


def main(argv=None, *, adapter=None, command=None, authority=None):
    """Explicit local-only configuration; never constructs a live provider."""
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError("invalid arguments")
    parser = Parser(description=__doc__)
    parser.add_argument('mode', choices=('check', 'run-once'))
    parser.add_argument('--plan', required=True)
    parser.add_argument('--source-config')
    parser.add_argument('--connection')
    parser.add_argument('--command-config')
    parser.add_argument('--authority')
    try:
        args = parser.parse_args(argv)
        raw = json.loads(Path(args.plan).read_text())
        if adapter is None:
            from nilaris.drive155_private import read_private_json
            config = read_private_json(args.source_config, Path(__file__).resolve().parents[2])
            source = (SyntheticFixtureDrive(config) if config.get('mode') == SyntheticFixtureDrive.label
                      else PinnedDrive(config))
            adapter = SyntheticAdapter(source, raw['member'],
                json.loads(Path(args.connection).read_text()))
        plan = freeze_plan(raw, adapter)
        if args.mode == 'check':
            result = dict(known=True, category='intake_plan_valid', plan_digest=plan_digest(plan))
        else:
            if authority is None:
                authority = json.loads(Path(args.authority).read_text())
            if command is None:
                from nilaris.command_service import Configuration, request
                configuration = Configuration.load(args.command_config)
                command = lambda value: request(configuration, value)
            result = run_once(plan, adapter, command, authority=authority)
    except (OSError, ValueError, TypeError, KeyError):
        result = _stop('intake_configuration_unavailable')
    print(json.dumps(result, allow_nan=False))
    return 0 if result.get('coverage') == 'complete' or result.get('category') == 'intake_plan_valid' else 2


if __name__ == '__main__':
    raise SystemExit(main())
