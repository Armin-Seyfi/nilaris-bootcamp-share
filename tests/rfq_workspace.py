"""Loopback client and server for RFQ reviewer integration tests."""
from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
from threading import Thread

import pytest

from nilaris.rfq_demo import SELECTED_REVISION, SourceBundle, reviewer_server


class ReviewerClient:
    def __init__(self, address):
        self.host, self.port = address
        self.cookie = ''

    def raw(self, path, body=None):
        headers = {'Cookie': self.cookie, 'Origin': f'http://{self.host}:{self.port}',
                   'Content-Type': 'application/json'}
        with closing(HTTPConnection(self.host, self.port, timeout=5)) as connection:
            connection.request('GET' if body is None else 'POST', path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            if cookie := response.getheader('Set-Cookie'):
                self.cookie = cookie.split(';')[0]
            return response.status, response.read()

    def json(self, path, body=None):
        status, content = self.raw(path, body)
        return status, json.loads(content)


@contextmanager
def reviewer_workspace(schema, *, key, bundle=None, review_config=None):
    with reviewer_server(bundle, {}, key, port=0, schema=schema, review_config=review_config) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield ReviewerClient(server.server_address)
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture(scope='module')
def pump_bundle(tmp_path_factory):
    case = os.environ.get('NILARIS_TEST_PUMP_CASE')
    if not case:
        pytest.skip('Set NILARIS_TEST_PUMP_CASE to the pinned Case_003 directory.')
    case = Path(case)
    directory = tmp_path_factory.mktemp('rfq') / 'bundle'
    subprocess.run(['swift', 'tools/rfq/prepare.swift', str(case / '2.pdf'),
                    str(case / '1.pdf'), str(directory)], check=True)
    return SourceBundle.load(directory, SELECTED_REVISION)
