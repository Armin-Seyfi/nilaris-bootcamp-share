"""Exact completed-Job continuation. No provider, credential or worker capability."""
import argparse
import copy
import json
import sys

import psycopg
from psycopg.rows import dict_row

from nilaris.command_service import Configuration, refused, serve
from nilaris.drive155_private import read_private_json
from nilaris.drive155_source import SourceBinding


def refusal(category='resume_scope_refused', known=True):
    return dict(known=known, category=category, coverage='unknown',
                next_action='read_original_operation_and_job')


class Resume:
    """Read original journals and fence existing commands to one immutable result."""
    def __init__(self, database, source, authority, manifest):
        self.binding = SourceBinding(source, authority)
        required = {'version','discovery_operation','acquisition_operation','page_id',
                    'job_id','publication_id','revision','generation'}
        if (set(manifest) != required or type(manifest['version']) is not int
                or manifest['version'] != 1
                or any(not isinstance(manifest[k], str) or not manifest[k].strip()
                       or len(manifest[k]) > 256 for k in required - {'version','revision','generation'})
                or any(type(manifest[k]) is not int or manifest[k] < 0
                       for k in ('revision','generation'))
                or manifest['discovery_operation'] == manifest['acquisition_operation']):
            raise ValueError('resume configuration unavailable')
        self.database = database
        self.manifest = copy.deepcopy(manifest)
        self.binding_id = source['binding_id']
        self.namespace = self.binding.namespace
        self.member = self.binding.allowed_member
        self.job_id = manifest['job_id']
        self.publication_id = manifest['publication_id']
        self.review_namespace = 'local-review-ui:' + self.job_id

    def _job_identity(self, job):
        return (isinstance(job, dict) and job.get('known') is True
                and job.get('id') == self.job_id and job.get('input') == self.member
                and job.get('content_pin') == self.member['expected_sha256']
                and job.get('canonical_job_id') == self.job_id)

    def valid_status(self, job):
        return (self._job_identity(job) and job.get('state') == 'completed'
                and type(job.get('revision')) is int and job['revision'] == self.manifest['revision']
                and type(job.get('generation')) is int and job['generation'] == self.manifest['generation']
                and isinstance(job.get('results'), list) and len(job['results']) == 1
                and isinstance(job['results'][0], dict)
                and job['results'][0].get('id') == self.publication_id
                and job['results'][0].get('initial_publication_id') == self.publication_id
                and job['results'][0].get('kind') == 'initial'
                and job['results'][0].get('approved') is False)

    def valid_journal(self, row):
        if (not isinstance(row, dict) or row.get('namespace') != self.namespace
                or row.get('state') != 'completed' or not isinstance(row.get('result'), dict)):
            return False
        if row.get('operation_id') == self.manifest['discovery_operation']:
            page = row['result'].get('page', {})
            return (row.get('kind') == 'discover'
                and row.get('request') == dict(page_id=self.manifest['page_id'])
                and isinstance(page, dict)
                and page.get('known') is True and page.get('namespace') == self.namespace
                and page.get('page_id') == self.manifest['page_id']
                and page.get('state') in ('staged','settled')
                and isinstance(page.get('members'),list) and len(page['members']) == 1
                and isinstance(page['members'][0], dict)
                and page['members'][0].get('member') == self.member)
        if row.get('operation_id') == self.manifest['acquisition_operation']:
            job = row['result'].get('job', {})
            request = row.get('request')
            return (row.get('kind') == 'acquire' and row.get('job_id') == self.job_id
                and isinstance(request,dict) and set(request) == {'job_id','expected_revision'}
                and request['job_id'] == self.job_id and type(request['expected_revision']) is int
                and request['expected_revision'] >= 0 and self._job_identity(job)
                and job.get('state') == 'queued' and type(job.get('revision')) is int
                and request['expected_revision'] < job['revision'] < self.manifest['revision']
                and type(row.get('generation')) is int and row['generation'] == job.get('generation')
                and row['generation'] < self.manifest['generation'])
        return False

    def drive_operation(self, binding_id, operation_id):
        if not self.permits_command('drive_operation', dict(binding_id=binding_id, operation_id=operation_id)):
            return refusal()
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            connection.execute('SET TRANSACTION READ ONLY')
            row = connection.execute('SELECT * FROM drive155_operations WHERE namespace=%s AND operation_id=%s',
                                     (self.namespace,operation_id)).fetchone()
        if row is None:
            return refusal('source_operation_unknown', False)
        if not self.valid_journal(row):
            return refusal('resume_journal_mismatch')
        result = dict(known=True, category='source_operation_completed', state='completed',
            namespace=self.namespace, operation_id=operation_id, coverage='complete',
            next_action='read_original_page_or_job', reserved_requests=row['reserved_requests'],
            reservation_bytes=row['reservation_bytes'], active_capacity_reserved=row['active'],
            reserved_cpu_seconds=row['reserved_cpu_seconds'], accounting=row['accounting'])
        result.update({k:v for k,v in row['request'].items() if k in ('page_id','job_id')})
        result.update(row['result'])
        return result

    def permits_command(self, operation, command):
        if operation == 'drive_operation':
            return (command.get('binding_id') == self.binding_id and command.get('operation_id')
                    in (self.manifest['discovery_operation'], self.manifest['acquisition_operation']))
        if operation in ('status','result'):
            return command.get('job_id') == self.job_id
        if operation == 'review_create':
            return (command.get('job_id') == self.job_id and command.get('proposal_id') == self.publication_id
                and command.get('namespace') == self.review_namespace and command.get('command_id') == 'create'
                and command.get('product_type') == self.member['product_type'])
        if operation not in ('review_read','review_resume','review_save','review_approve','review_resolve'):
            return False
        if 'namespace' in command and command['namespace'] != self.review_namespace:
            return False
        if operation == 'review_resolve':
            # Missing same-key reads are safe and remain truthfully unknown.
            with psycopg.connect(**self.database, row_factory=dict_row) as connection:
                row = connection.execute('SELECT job_id FROM review_receipts WHERE namespace=%s AND command_id=%s',
                    (self.review_namespace,command.get('command_id'))).fetchone()
            return row is None or row['job_id'] == self.job_id
        with psycopg.connect(**self.database, row_factory=dict_row) as connection:
            row = connection.execute('SELECT job_id,proposal_id,product_type FROM review_drafts WHERE id=%s',
                                     (command.get('draft_id'),)).fetchone()
        return (row is not None and row['job_id'] == self.job_id
            and row['proposal_id'] == self.publication_id and row['product_type'] == self.member['product_type'])

    def check(self, operation, command):
        """Run before dispatch; no generic fallback can reach a mutating operation."""
        if not self.permits_command(operation, command):
            return refusal()
        if operation != 'drive_operation':
            from nilaris.admission import Ingestion
            if not self.valid_status(Ingestion(self.database).status(self.job_id)):
                return refusal('resume_job_mismatch')
        return None


def configuration(command_config_path, source_path, authority_path, manifest_path, *, repository_root=None):
    from pathlib import Path
    root = Path(repository_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        source, authority, manifest = [read_private_json(path, root)
            for path in (source_path,authority_path,manifest_path)]
        private = Configuration.load(command_config_path)
        if private.drive is not None:
            raise ValueError
        policy = Resume(private.database,source,authority,manifest)
        private.source_operator = policy
        private.command_guard = policy.check
        return private
    except Exception:
        raise ValueError('resume configuration unavailable') from None


def main():
    class ResumeParser(argparse.ArgumentParser):
        def error(self, message):
            print(json.dumps(refused()), file=sys.stderr)
            raise SystemExit(2)

    parser = ResumeParser(description='Credential-free original Job resume listener')
    for name in ('command-config','source','authority','manifest'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        private = configuration(args.command_config,args.source,args.authority,args.manifest)
    except Exception:
        print(json.dumps(refused('configuration_unavailable')), file=sys.stderr)
        raise SystemExit(1) from None
    try:
        serve(private)
    except Exception:
        print(json.dumps(dict(known=True, category='listener_unavailable', affected={},
                              coverage='unknown', reason='Command listener is unavailable.',
                              next_action='restore_authorized_loopback_listener')), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
