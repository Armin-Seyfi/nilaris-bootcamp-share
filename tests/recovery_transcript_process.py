"""Private literal-stage harness; never exposed by the production transport.

Requests (including disposable DB credentials) arrive only through stdin. Gates
surround public calls, never a SQL transaction or a production implementation.
"""
import json
import os
import sys

from nilaris.admission import Ingestion
from nilaris.results import Effect, StageOutput


def literal_output():
    return StageOutput(
        outcome='completed',
        adapter='private-recovery-transcript-literal-v1',
        coverage=dict(inspected=['page:1'], unread=[], limits=['literal-test-only']),
        effects=(
            Effect('observation', 0, dict(
                id='transcript-o1', locator={'page': 1, 'text': 'Pressure: 7 bar'},
                kind='property', original_content='Pressure: 7 bar', value='7',
                unit='bar', source_type='text', presence='observed')),
            Effect('candidate', 0, dict(
                id='transcript-c1', label='Pressure', value_type='decimal', unit='bar',
                domain='technical', observation_ids=['transcript-o1'],
                requirements='unspecified')),
        ))


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    request = json.loads(sys.stdin.readline())
    client = Ingestion(request.pop('database'))
    operation = request['operation']
    emit(dict(event='ready', pid=os.getpid()))
    if sys.stdin.readline().strip() != 'go':
        return
    job_id = request['job_id']
    if operation == 'publish':
        result = client.publish(job_id, request['claim'], literal_output())
        # The result is deliberately withheld until a second gate. A caller
        # can independently observe the commit and kill us without receiving it.
        emit(dict(event='returned_without_response'))
        if sys.stdin.readline().strip() != 'deliver':
            return
    elif operation == 'claim':
        result = client.claim(job_id, request['revision'])
    elif operation == 'cancel':
        result = client.cancel(job_id, request['revision'])
    elif operation == 'read':
        result = dict(status=client.status(job_id), result=client.result(job_id))
    else:
        raise ValueError('unknown private harness operation')
    emit(dict(event='response', result=result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not let exceptions retain connection details in test artifacts.
        raise SystemExit(1) from None
