"""Bounded synthetic operator provisioning. No production identity verifier is selected.

The supplied control database must belong to a separately bootstrapped empty cluster.
No API accepts a customer database name, role, or connection target.
"""
import argparse
from dataclasses import asdict
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.service_monitor import service_monitor_ready
from nilaris.storage_coordination import storage_lock


class ControlledOperatorVerifier:
    """Controlled test boundary only; never use this as production authentication."""
    def __init__(self, credentials):
        self.credentials = dict(credentials)

    def verify(self, credential):
        if not isinstance(credential, str):
            raise PermissionError('operator authentication required')
        for token, subject in self.credentials.items():
            if hmac.compare_digest(token, credential):
                return subject
        raise PermissionError('operator authentication required')


class ProvisioningRefused(RuntimeError):
    pass


def _private_json(path, *, limit=16384):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > limit):
            raise ProvisioningRefused('private configuration required')
        return json.load(stream)


class OperatorProvisioner:
    def __init__(self, config, verifier, *, fault=None):
        self.config = dict(config)
        self.admin = dict(config['admin'])
        self.verifier = verifier
        self.fault = fault
        self.directory = Path(config['state_directory'])
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ProvisioningRefused('private state directory required')
        if not re.fullmatch(r'(?:nilaris_control_[a-f0-9]{16,32}|provision181_control)', self.admin['dbname']):
            raise ProvisioningRefused('dedicated control database required')
        if not re.fullmatch(r'[a-f0-9]{32,128}', config['bootstrap_token']):
            raise ProvisioningRefused('bootstrap binding required')

    def _require_monitor(self):
        if not service_monitor_ready(self.config['service_monitor_path']):
            raise ProvisioningRefused('fresh service monitor required')

    def _authenticate(self, credential, operation_id=None):
        subject = self.verifier.verify(credential)
        if not isinstance(subject, str) or not subject or len(subject) > 200:
            raise PermissionError('operator authentication required')
        if operation_id is not None and (not isinstance(operation_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', operation_id)):
            raise ValueError('bounded operation identity required')
        return subject

    @contextmanager
    def _control(self, *, allocator=True):
        # Session lock covers journal commits and CREATE DATABASE's nontransactional gap.
        with psycopg.connect(**self.admin, autocommit=True, row_factory=dict_row) as c:
            c.execute("SET statement_timeout = '10s'")
            c.execute("SET lock_timeout = '3s'")
            if allocator:
                c.execute('SELECT pg_advisory_lock(181001)')
            try:
                actual = str(c.execute('SELECT system_identifier FROM pg_control_system()').fetchone()['system_identifier'])
                comment = c.execute('SELECT shobj_description(oid, \'pg_database\') AS binding FROM pg_database WHERE datname = current_database()').fetchone()['binding']
                if actual != self.config['system_identifier'] or comment != 'nilaris-provision181:' + self.config['bootstrap_token']:
                    raise ProvisioningRefused('cluster ownership binding refused')
                exists = c.execute("SELECT to_regclass('public.provision181_binding') AS name").fetchone()['name']
                if not exists and not allocator:
                    raise ProvisioningRefused('existing journal binding required')
                if not exists:
                    self._require_monitor()
                    databases = {r['datname'] for r in c.execute('SELECT datname FROM pg_database')}
                    roles = c.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> current_user").fetchall()
                    if databases != {'postgres', 'template0', 'template1', self.admin['dbname']} or roles:
                        raise ProvisioningRefused('fresh isolated cluster required')
                    with c.transaction():
                        c.execute('CREATE TABLE provision181_binding (system_identifier text NOT NULL, token text NOT NULL)')
                        c.execute('INSERT INTO provision181_binding VALUES (%s,%s)', (actual, self.config['bootstrap_token']))
                        c.execute('''CREATE TABLE provision181_operations (
                            operation_id text PRIMARY KEY, subject text NOT NULL, customer_id text NOT NULL UNIQUE,
                            mode text NOT NULL, database_name text NOT NULL UNIQUE,
                            migration_role text NOT NULL UNIQUE, runtime_role text NOT NULL UNIQUE,
                            secret_ref text NOT NULL UNIQUE, state text NOT NULL,
                            reason text, evidence jsonb NOT NULL DEFAULT '{}'::jsonb)''')
                        # Prevent every runtime role from reaching the control/default/template DBs.
                        for name in databases:
                            c.execute(sql.SQL('REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
                        c.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
                binding = c.execute('SELECT * FROM provision181_binding').fetchall()
                if binding != [dict(system_identifier=actual, token=self.config['bootstrap_token'])]:
                    raise ProvisioningRefused('journal binding refused')
                yield c
            finally:
                if allocator:
                    c.execute('SELECT pg_advisory_unlock(181001)')

    def _record(self, c, operation_id, subject):
        row = c.execute('SELECT * FROM provision181_operations WHERE operation_id=%s', (operation_id,)).fetchone()
        if row and row['subject'] != subject:
            raise PermissionError('operation ownership refused')
        return row

    def _view(self, row):
        return dict(operation_id=row['operation_id'], customer_id=row['customer_id'],
                    mode=row['mode'], state=row['state'], reason=row['reason'],
                    database_name=row['database_name'], runtime_role=row['runtime_role'],
                    migration_role=row['migration_role'],
                    storage_namespace='customer/' + row['customer_id'],
                    worker_namespace='customer/' + row['customer_id'],
                    secret_ref=row['secret_ref'],
                    migration_secret_ref=(row['customer_id'] + '.migration.json'
                        if row['secret_ref'] == row['customer_id'] + '.runtime.json'
                        else row['secret_ref']), **row['evidence'])

    def status(self, credential, operation_id):
        subject = self._authenticate(credential, operation_id)
        with self._control() as c:
            row = self._record(c, operation_id, subject)
            return self._view(row) if row else dict(operation_id=operation_id, state='absent')

    def inventory(self, credential):
        subject = self._authenticate(credential)
        with self._control() as c:
            return [self._view(row) for row in c.execute('SELECT * FROM provision181_operations WHERE subject=%s ORDER BY operation_id', (subject,))]

    def provision(self, credential, operation_id, *, mode='customer'):
        subject = self._authenticate(credential, operation_id)
        if mode not in ('customer', 'fixture'):
            raise ValueError('explicit customer or fixture mode required')
        with self._control() as c:
            row = self._record(c, operation_id, subject)
            if row and row['mode'] != mode:
                raise PermissionError('operation request alteration refused')
            if not row or row['state'] == 'provisioning':
                self._require_monitor()
            allocating = row is None
            if allocating:
                identity = uuid.uuid4().hex
                secret_ref = identity + '.runtime.json'
                # Autocommit persists the resource identity before any private-file effect.
                c.execute('''INSERT INTO provision181_operations
                    (operation_id,subject,customer_id,mode,database_name,migration_role,runtime_role,secret_ref,state)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'provisioning')''',
                    (operation_id,subject,identity,mode,'nc_' + identity,'nm_' + identity,'nr_' + identity,secret_ref))
                row = self._record(c, operation_id, subject)
            if row['state'] in ('ready', 'held'):
                return self._view(row)
            phase = 'secrets'
            try:
                if allocating:
                    for purpose in ('runtime', 'migration'):
                        filename = row['customer_id'] + '.' + purpose + '.json'
                        fd = os.open(self.directory / filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'w') as stream:
                            json.dump(dict(password=secrets.token_hex(32)), stream)
                            stream.flush()
                            os.fsync(stream.fileno())
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                passwords = self._passwords(row)
                phase = 'resources'
                self._resources(c, row, passwords)
                if self.fault:
                    self.fault('after_resources', operation_id)
                phase = 'migration_checks'
                self._prepare(row, passwords)
                c.execute("UPDATE provision181_operations SET state='ready', reason=NULL, evidence=%s WHERE operation_id=%s",
                          (Jsonb(dict(business_rows=0, runtime_restricted=True)), operation_id))
            except (psycopg.Error, RuntimeError, OSError, ValueError) as error:
                # Diagnostics are fixed phases/codes, never dependency messages or SQL.
                safe_runtime_codes = {
                    'storage coordination deadline reached': 'storage_lock_deadline',
                    'invalid storage coordination lock': 'storage_lock_invalid',
                    'storage coordination lock replaced': 'storage_lock_replaced',
                    'fresh service monitor required': 'service_monitor_unavailable',
                    'preexisting role refused': 'preexisting_role_refused',
                    'preexisting database refused': 'preexisting_database_refused',
                    'empty business tables required': 'business_tables_not_empty',
                    'restricted runtime role required': 'runtime_role_not_restricted',
                }
                code = (error.sqlstate if isinstance(error, psycopg.Error) else
                        safe_runtime_codes.get(str(error), type(error).__name__))
                reason = phase + '_' + (code or 'unavailable')
                # A killed process never runs this handler: its durable state remains provisioning.
                c.execute("UPDATE provision181_operations SET state='held',reason=%s WHERE operation_id=%s", (reason, operation_id))
            return self._view(self._record(c, operation_id, subject))

    def load_fixtures(self, credential, operation_id):
        """Load one inert synthetic Job only in an explicitly provisioned fixture DB."""
        subject = self._authenticate(credential, operation_id)
        with self._control() as control:
            row = self._record(control, operation_id, subject)
            if not row or row['mode'] != 'fixture' or row['state'] != 'ready':
                raise PermissionError('ready explicit fixture mode required')
            self._require_monitor()
            passwords = self._passwords(row)
            options = self._options(row, passwords['runtime'], 'runtime_role')
            job_id = 'synthetic-fixture-' + row['customer_id']
            with psycopg.connect(**options) as c:
                c.execute("""INSERT INTO admission_jobs
                    (id,input,state,reason,reservation_bytes,profile)
                    VALUES (%s,%s,'held','explicit_fixture_only',8388608,%s)
                    ON CONFLICT (id) DO NOTHING""",
                    (job_id, Jsonb(dict(document_id='synthetic-fixture-document',
                        source_revision='synthetic-r1', processing_contract='fixture-v1',
                        product_type='Synthetic fixture pump')), Jsonb(asdict(AdmissionProfile.l1()))))
            saved = Ingestion(options).status(job_id)
            return dict(operation_id=operation_id, fixture_job={
                key: saved[key] for key in ('id', 'state', 'reason', 'input', 'created_at', 'revision')})

    def customer_runtime(self, row):
        """Resolve the restricted login from the authenticated registry binding."""
        customer = row['customer_id']
        if (row['mode'] != 'customer' or row['state'] != 'ready' or
                row['database_name'] != 'nc_' + customer or row['runtime_role'] != 'nr_' + customer
                or row['secret_ref'] != customer + '.runtime.json'):
            raise ProvisioningRefused('customer runtime binding refused')
        secret = _private_json(self.directory / row['secret_ref'])
        password = secret.get('password', secret.get('runtime'))
        if not isinstance(password, str) or not password:
            raise ProvisioningRefused('customer runtime login unavailable')
        return self._options(row, password, 'runtime_role')

    def _passwords(self, row):
        runtime = _private_json(self.directory / row['secret_ref'])
        # Earlier disposable test records retain their original private operator bundle.
        # No runtime connection/configuration export exists in this ticket.
        if not isinstance(runtime, dict):
            raise ProvisioningRefused('private secrets invalid')
        if 'runtime' in runtime and 'migration' in runtime:
            passwords = dict(runtime=runtime['runtime'], migration=runtime['migration'])
        else:
            migration = _private_json(self.directory / (row['customer_id'] + '.migration.json'))
            if not isinstance(migration, dict):
                raise ProvisioningRefused('private secrets invalid')
            passwords = dict(runtime=runtime.get('password'), migration=migration.get('password'))
        if any(not isinstance(value, str) or not value for value in passwords.values()):
            raise ProvisioningRefused('private secrets invalid')
        return passwords

    def _resources(self, c, row, passwords):
        self._require_monitor()
        binding = 'nilaris-provision181:' + row['customer_id']
        for key, password_key in (('migration_role','migration'), ('runtime_role','runtime')):
            role = row[key]
            existing = c.execute("SELECT shobj_description(oid,'pg_authid') AS binding FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if existing and existing['binding'] != binding:
                raise ProvisioningRefused('preexisting role refused')
            if not existing:
                with c.transaction():
                    c.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2').format(sql.Identifier(role), sql.Literal(passwords[password_key])))
                    c.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(role), sql.Literal(binding)))
        database = c.execute('SELECT pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname=%s', (row['database_name'],)).fetchone()
        if database and database['owner'] != row['migration_role']:
            raise ProvisioningRefused('preexisting database refused')
        if not database:
            with storage_lock(exclusive=True, deadline=time.monotonic() + 1, path=self.config['storage_lock_path']):
                self._require_monitor()
                c.execute(sql.SQL('CREATE DATABASE {} OWNER {} TEMPLATE template0 ALLOW_CONNECTIONS false').format(sql.Identifier(row['database_name']),sql.Identifier(row['migration_role'])))
        c.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(row['database_name'])))
        c.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(row['database_name']),sql.Identifier(row['runtime_role'])))
        c.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(row['database_name'])))

    def _options(self, row, password, role):
        return dict(self.admin, dbname=row['database_name'], user=row[role], password=password)

    def _prepare(self, row, passwords):
        self._require_monitor()
        migration = self._options(row, passwords['migration'], 'migration_role')
        migrate(migration)
        with psycopg.connect(**migration) as c:
            c.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC')
            runtime = sql.Identifier(row['runtime_role'])
            c.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(runtime))
            c.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}').format(runtime))
            c.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(runtime))
            c.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}').format(runtime))
            c.execute(sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}').format(runtime))
            tables = c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
            if not tables or any(c.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM public.{})').format(sql.Identifier(table[0]))).fetchone()[0] for table in tables):
                raise ProvisioningRefused('empty business tables required')
        with psycopg.connect(**self._options(row, passwords['runtime'], 'runtime_role')) as c:
            flags = c.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            if any(flags) or c.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0]:
                raise ProvisioningRefused('restricted runtime role required')
            for table in tables:
                c.execute(sql.SQL('SELECT 1 FROM public.{} LIMIT 1').format(sql.Identifier(table[0])))


def main():
    parser = argparse.ArgumentParser(description='Controlled synthetic operator provisioning only')
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        config = _private_json(args.config)
        if config.get('authentication_mode') != 'controlled-test-only':
            raise ProvisioningRefused('production authentication is not configured')
        request = json.loads(sys.stdin.read(16385))
        if not isinstance(request, dict) or len(json.dumps(request)) > 16384:
            raise ValueError('bounded request required')
        operator = OperatorProvisioner(config, ControlledOperatorVerifier(config['controlled_credentials']))
        action = request.pop('action')
        if action not in ('provision', 'status', 'inventory', 'load_fixtures'):
            raise ValueError('unknown operator action')
        result = getattr(operator, action)(**request)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (KeyError, TypeError, ValueError, OSError, PermissionError, RuntimeError, psycopg.Error):
        print(json.dumps(dict(state='unknown', reason='operator_request_refused')), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
