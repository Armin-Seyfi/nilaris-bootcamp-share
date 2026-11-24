"""Finite credential-free completed-result client. Never dispatches intake or review writes."""
import argparse
import json
import sys

from nilaris.command_service import MAX_RESPONSE, request, refused
from nilaris.resume165 import configuration

CONTEXT = ('document_id', 'source_revision', 'case_id', 'membership_revision',
           'product_type', 'processing_contract', 'reference_set', 'selection_policy', 'intent_id')


class ResumeRefused(ValueError):
    """A read failed to establish the exact complete persisted outcome."""


def equal(left, right):
    """JSON equality preserves booleans separately from numeric identities."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def gate(condition, reason):
    if not condition:
        raise ResumeRefused(reason)


def read_complete_result(command, job_id, publication_id, member):
    """Return (complete result, page count), or refuse within eight bounded reads."""
    pages, encoded, offset, seen = [], '', 0, set()
    query = dict(operation='result', job_id=job_id)
    for _ in range(8):
        p = command(query)
        gate(isinstance(p, dict), 'result shape')
        gate(len(json.dumps(p, ensure_ascii=True, allow_nan=False).encode()) <= MAX_RESPONSE,
             'result page size')
        pages.append(p)
        if p.get('encoding') != 'json-ascii':
            gate(len(pages) == 1 and not any(k in p for k in ('next_cursor','fragment','encoding')),
                 'single complete result')
            value = p
            break
        gate(p.get('known') is True and p.get('job_id') == job_id
             and p.get('publication_ids') == [publication_id]
             and type(p.get('offset')) is int and p['offset'] == offset
             and isinstance(p.get('fragment'), str) and bool(p['fragment']),
             'fragment identity and continuity')
        encoded += p['fragment']
        offset += len(p['fragment'])
        cursor = p.get('next_cursor', False)
        if cursor is None:
            try:
                value = json.loads(encoded)
            except (ValueError, RecursionError):
                raise ResumeRefused('malformed complete result') from None
            break
        gate(isinstance(cursor, str) and bool(cursor.strip()) and len(cursor) <= 65536,
             'cursor completion')
        gate(cursor not in seen, 'cursor cycle')
        seen.add(cursor)
        query = dict(operation='result', job_id=job_id, cursor=cursor)
    else:
        raise ResumeRefused('incomplete result at eight-page bound')
    gate(isinstance(value, dict) and value.get('known') is True
         and value.get('job_id') == job_id and value.get('approved') is False
         and isinstance(value.get('effects'), list)
         and isinstance(value.get('publications'), list) and len(value['publications']) == 1,
         'complete result identity')
    p = value['publications'][0]
    gate(isinstance(p, dict) and p.get('id') == publication_id
         and p.get('initial_publication_id') == publication_id and p.get('job_id') == job_id
         and p.get('kind') == 'initial' and p.get('approved') is False
         and p.get('content_pin') == member['expected_sha256']
         and equal(p.get('context'), {k: member[k] for k in CONTEXT})
         and all(isinstance(p.get(k), list) for k in ('observations','associations','candidates')),
         'publication source and context')
    return value, len(pages)


def verify_resume(policy, command):
    """Read original journals/status/result only; return an owner review handoff."""
    m = policy.manifest
    for kind in ('discovery', 'acquisition'):
        operation_id = m[kind + '_operation']
        x = command(dict(operation='drive_operation', binding_id=policy.binding_id,
                         operation_id=operation_id))
        gate(isinstance(x, dict) and x.get('known') is True
             and x.get('category') == 'source_operation_completed'
             and x.get('state') == 'completed' and x.get('coverage') == 'complete'
             and x.get('namespace') == policy.namespace and x.get('operation_id') == operation_id,
             kind + ' completion and identity')
        if kind == 'discovery':
            p = x.get('page')
            gate(x.get('page_id') == m['page_id'] and isinstance(p, dict)
                 and p.get('known') is True and p.get('namespace') == policy.namespace
                 and p.get('page_id') == m['page_id'] and p.get('state') in ('staged','settled')
                 and isinstance(p.get('members'), list) and len(p['members']) == 1
                 and isinstance(p['members'][0], dict) and equal(p['members'][0].get('member'), policy.member),
                 'discovery page and source')
        else:
            j = x.get('job')
            gate(x.get('job_id') == policy.job_id and policy._job_identity(j)
                 and equal(j.get('input'), policy.member) and j.get('state') == 'queued' and type(j.get('revision')) is int
                 and j['revision'] < m['revision'] and type(j.get('generation')) is int
                 and j['generation'] < m['generation'], 'historical acquired Job')
    current = command(dict(operation='status', job_id=policy.job_id))
    gate(policy.valid_status(current) and equal(current.get('input'), policy.member),
         'current completed Job')
    value, count = read_complete_result(command, policy.job_id, policy.publication_id, policy.member)
    return dict(resume_verified=True, job_id=policy.job_id, publication_id=policy.publication_id,
                result_page_count=count, result_incomplete=False,
                review_url='http://127.0.0.1:18766/', owner_decision_pending=True, result=value)


def main():
    class ResumeParser(argparse.ArgumentParser):
        def error(self, message):
            print(json.dumps(refused()), file=sys.stderr)
            raise SystemExit(2)

    parser = ResumeParser(description='Read-only original completed Job verification')
    for name in ('command-config', 'source', 'authority', 'manifest'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        private = configuration(args.command_config, args.source, args.authority, args.manifest)
        handoff = verify_resume(private.source_operator, lambda command: request(private, command))
    except Exception as error:
        reason = str(error) if isinstance(error, ResumeRefused) else 'configuration or response unavailable'
        print(json.dumps(dict(known=False, category='resume_unverified', coverage='unknown',
            reason=reason, result_incomplete=True, next_action='read_original_operation_and_job')),
            file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(handoff, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
