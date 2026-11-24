"""Read-only Python client contract at the authenticated command seam."""
import json

import pytest

from test_resume165 import resume, status


def publication(policy):
    return dict(id=policy.publication_id, initial_publication_id=policy.publication_id,
        job_id=policy.job_id, kind='initial', approved=False,
        content_pin=policy.member['expected_sha256'],
        context={k: policy.member[k] for k in ('document_id','source_revision','case_id',
            'membership_revision','product_type','processing_contract','reference_set',
            'selection_policy','intent_id')}, observations=[], associations=[], candidates=[])


def result(policy):
    return dict(known=True, job_id=policy.job_id, approved=False,
                effects=[], publications=[publication(policy)])


def test_complete_single_result_is_verified_without_dispatch():
    from nilaris.resume167 import read_complete_result
    policy = resume()
    commands = []
    def command(request):
        commands.append(request)
        return result(policy)
    value, count = read_complete_result(command, policy.job_id, policy.publication_id, policy.member)
    assert value == result(policy) and count == 1
    assert commands == [dict(operation='result', job_id='original-job')]


def fragments(policy, count=2):
    text = json.dumps(result(policy), ensure_ascii=True)
    cuts = [len(text) * i // count for i in range(count + 1)]
    return [dict(known=True, job_id=policy.job_id, publication_ids=[policy.publication_id],
        encoding='json-ascii', offset=cuts[i], fragment=text[cuts[i]:cuts[i+1]],
        next_cursor=str(i+1) if i+1<count else None) for i in range(count)]


def test_complete_fragmented_result_retains_all_pages():
    from nilaris.resume167 import read_complete_result
    policy = resume()
    pages = iter(fragments(policy))
    value, count = read_complete_result(lambda _: next(pages), policy.job_id,
                                       policy.publication_id, policy.member)
    assert value == result(policy) and count == 2


@pytest.mark.parametrize('change', [
    {'offset': 1}, {'job_id': 'foreign'}, {'publication_ids': ['foreign']},
    {'next_cursor': ''}, {'next_cursor': False}, {'next_cursor': '1'},
    {'fragment': ''}, {'encoding': 'other'}, {'known': False}])
def test_malformed_or_missing_result_page_refuses(change):
    from nilaris.resume167 import ResumeRefused, read_complete_result
    policy = resume()
    pages = fragments(policy, 3)
    pages[1].update(change)
    responses = iter(pages)
    with pytest.raises(ResumeRefused):
        read_complete_result(lambda _: next(responses), policy.job_id,
                             policy.publication_id, policy.member)


def test_more_than_eight_pages_refuses_with_no_ninth_read():
    from nilaris.resume167 import ResumeRefused, read_complete_result
    policy = resume()
    pages = fragments(policy, 9)
    calls = []
    def command(request):
        calls.append(request)
        return pages[len(calls)-1]
    with pytest.raises(ResumeRefused, match='eight-page'):
        read_complete_result(command, policy.job_id, policy.publication_id, policy.member)
    assert len(calls) == 8


@pytest.mark.parametrize('field,value', [('context', {}), ('content_pin','foreign'),
    ('id','foreign'), ('job_id','foreign'), ('approved', True), ('observations', None)])
def test_stale_or_foreign_publication_refuses(field, value):
    from nilaris.resume167 import ResumeRefused, read_complete_result
    policy = resume()
    wrong = result(policy)
    wrong['publications'][0][field] = value
    with pytest.raises(ResumeRefused):
        read_complete_result(lambda _: wrong, policy.job_id, policy.publication_id, policy.member)


def test_context_boolean_cannot_impersonate_integer_revision():
    from nilaris.resume167 import ResumeRefused, read_complete_result
    policy = resume()
    policy.member['membership_revision'] = 1
    wrong = result(policy)
    wrong['publications'][0]['context']['membership_revision'] = True
    with pytest.raises(ResumeRefused):
        read_complete_result(lambda _: wrong, policy.job_id, policy.publication_id, policy.member)


def original_responses(policy):
    common = dict(known=True, category='source_operation_completed', state='completed',
                  coverage='complete', namespace=policy.namespace)
    return [dict(common, operation_id=policy.manifest['discovery_operation'],
                 page_id=policy.manifest['page_id'], page=dict(known=True,
                 namespace=policy.namespace, page_id=policy.manifest['page_id'],
                 state='settled', members=[dict(member=policy.member)])),
            dict(common, operation_id=policy.manifest['acquisition_operation'],
                 job_id=policy.job_id, job=dict(status(policy), state='queued', revision=2, generation=1)),
            status(policy), result(policy)]


def test_resume_reads_original_identities_and_hands_off_without_review_writes():
    from nilaris.resume167 import verify_resume
    policy = resume()
    responses = iter(original_responses(policy))
    commands = []
    def command(request):
        commands.append(request)
        assert request['operation'] in ('drive_operation','status','result')
        return next(responses)
    handoff = verify_resume(policy, command)
    assert handoff['resume_verified'] and handoff['owner_decision_pending']
    assert handoff['review_url'] == 'http://127.0.0.1:18766/'
    assert [c.get('operation_id') for c in commands[:2]] == ['discover-one','acquire-one']
    assert handoff['result'] == result(policy)


@pytest.mark.parametrize('stage', range(4))
def test_unknown_original_read_stops_and_recovery_reuses_original_ids(stage):
    from nilaris.resume167 import ResumeRefused, verify_resume
    policy = resume()
    original = original_responses(policy)
    responses = original.copy()
    responses[stage] = dict(known=False, category='transport_unavailable')
    calls = []
    def command(request):
        calls.append(request)
        assert request['operation'] in ('drive_operation','status','result')
        return responses[len(calls)-1]
    with pytest.raises(ResumeRefused):
        verify_resume(policy, command)
    assert len(calls) == stage + 1
    retried = []
    def recovered(request):
        retried.append(request)
        return original[len(retried)-1]
    assert verify_resume(policy, recovered)['resume_verified']
    assert retried[:stage+1] == calls


def test_result_response_size_refuses_before_handoff():
    from nilaris.resume167 import ResumeRefused, read_complete_result
    policy = resume()
    too_large = result(policy)
    too_large['effects'] = ['x' * 131072]
    with pytest.raises(ResumeRefused, match='size'):
        read_complete_result(lambda _: too_large, policy.job_id, policy.publication_id, policy.member)


def test_invalid_cli_arguments_emit_json_without_usage(monkeypatch, capsys):
    from nilaris.resume167 import main
    monkeypatch.setattr('sys.argv', ['resume167', '--unexpected'])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().err)['category'] == 'invalid_request'


def test_verifier_import_cannot_reach_provider_credentials(tmp_path):
    import subprocess
    script = tmp_path / 'credential_tripwire.py'
    script.write_text('''import sys
class Tripwire:
    def find_spec(self, fullname, *args):
        if fullname in {'nilaris.drive155_auth','nilaris.drive155_service',
                        'nilaris.drive155_journey','nilaris.drive155_supervision'}:
            raise AssertionError('provider or credentials reached')
sys.meta_path.insert(0, Tripwire())
from nilaris.resume167 import verify_resume, read_complete_result
''')
    completed = subprocess.run(['/opt/miniconda3/bin/conda','run','--no-capture-output',
        '-n','nilaris','python','runtime/a1/with_overlay.py',str(script)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
