"""Task-only commit pause harness; never imported by production command service."""
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from nilaris import discovery
from nilaris.command_service import main

connect = discovery.psycopg.connect
mode = os.environ.get('I10_COMMIT_PAUSE', '')
marker = os.environ.get('I10_COMMIT_MARKER', '')


def pause():
    Path(marker).write_text(mode)
    while True:
        time.sleep(0.1)


class Connection:
    def __init__(self, *args, **kwargs):
        self.connection = connect(*args, **kwargs)
        self.reconciled = False

    def __enter__(self):
        self.connection.__enter__()
        return self

    def execute(self, statement, *args, **kwargs):
        if isinstance(statement, str) and statement.startswith('UPDATE discovery_pages SET records='):
            self.reconciled = True
        return self.connection.execute(statement, *args, **kwargs)

    def __exit__(self, *args):
        if self.reconciled and mode == 'before' and args[0] is None:
            pause()
        result = self.connection.__exit__(*args)
        if self.reconciled and mode == 'after' and args[0] is None:
            pause()
        return result


discovery.psycopg = SimpleNamespace(connect=Connection)
# Existing fixture appends '-m nilaris.command_service serve ...'.
sys.argv = [sys.argv[0], *sys.argv[3:]]
main()
