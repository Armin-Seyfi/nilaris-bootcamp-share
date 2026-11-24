"""Real task-owned PostgreSQL database; never reuse existing service data."""
from pathlib import Path
import uuid
import time

import psycopg
from psycopg import sql
import pytest
from nilaris.storage_coordination import storage_lock


class Database(dict):
    def __repr__(self):
        return "<task-owned disposable PostgreSQL database; credentials redacted>"


def database_command(options, name, action):
    """Fail without retaining connection exceptions during setup or exact cleanup."""
    try:
        # Connecting cannot mutate the database directory tree. Only the exact
        # owned CREATE/DROP needs exclusion from physical storage traversal.
        with psycopg.connect(**options, dbname='postgres', autocommit=True) as control:
            with storage_lock(exclusive=True, deadline=time.monotonic() + 1):
                control.execute(sql.SQL(action + ' DATABASE {}').format(sql.Identifier(name)))
        return
    except (psycopg.Error, OSError, RuntimeError):
        pass
    pytest.fail('fixture persistence_unavailable; ' + action.lower() + ' of owned database '
                + name + ' failed; check task readiness and retry exact owned cleanup',
                pytrace=False)


@pytest.fixture
def database():
    secrets = dict(line.split('=', 1) for line in Path(
        '/private/tmp/nilaris-a1-setup-02/secrets.env'
    ).read_text().splitlines() if line and not line.startswith('#'))
    options = Database(host='127.0.0.1', port=55432, user='nilaris_test',
                       password=secrets['A1_TEST_PASSWORD'], connect_timeout=5)
    del secrets
    name = 'nilaris_test_' + uuid.uuid4().hex
    database_command(options, name, 'CREATE')
    try:
        yield Database(options, dbname=name)
    finally:
        database_command(options, name, 'DROP')
