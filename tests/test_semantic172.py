from decimal import Decimal

import pytest

from nilaris.semantic172 import Ledger, prepare


def capture():
    return {'documents': [dict(document_id=k, source_revision='r1', content_pin=k * 64,
        media_type='synthetic/text', spans=[dict(locator_id='p1', text=f'{k} pressure 8 bar', locator={})],
        layout={}, coverage=dict(inspected=['p1'], unread=[])) for k in ('A', 'B')]}


def test_unknown_reservation_stops_fixed_order_batch_without_refund(tmp_path):
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    assert sink.reserve('A') == 1
    with pytest.raises(ValueError):
        sink.reserve('B')
    assert Decimal(sink.status()['new_reserved_usd']) == Decimal('0.10144')
    assert sink.status()['combined_reserved_usd'] == '0.40576'
    assert sink.status()['historical_reserved_usd'] == '0.30432'
    with pytest.raises(FileExistsError):
        sink.write('attempt-01.json', {})


def test_full_pair_requests_preserve_schema_and_correct_only_prospective_unit_rule():
    from nilaris.semantic172 import request_body
    from nilaris.semantic_provider import request_body as historical
    a = request_body(capture(), 'A')
    old = historical(capture(), 'A')
    assert a['text'] == old['text']
    assert a['model'] == 'gpt-5.4-2026-03-05'
    assert a['store'] is False and a['background'] is False
    assert a['prompt_cache_retention'] == 'in_memory'
    assert 'unitless text definitions use schema null' in a['instructions']
    assert 'Absent units and blank source values use empty' in old['instructions']
    assert a['input'] == old['input']


def test_provider_failure_is_one_exchange_and_never_repairs_output():
    import httpx
    import time
    from nilaris.semantic172 import request_body
    from nilaris.semantic_provider import interpret
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(429, stream=httpx.ByteStream(b'limited'))
    result = interpret(request_body(capture(), 'A'), credential='synthetic-offline', mode='offline',
        deadline=time.monotonic()+2, transport=httpx.MockTransport(fail), timing_profile='semantic172-v1')
    assert result['status'] == 'http_error' and result['http_status'] == 429
    assert len(calls) == 1


def test_unknown_profile_cannot_widen_http_limit():
    from nilaris.semantic_provider import interpret
    with pytest.raises(ValueError):
        interpret({}, credential='synthetic-offline', mode='offline', deadline=0,
                  timing_profile='105')


def test_completed_a_allows_b_once_then_finite_batch_stops(tmp_path):
    from nilaris.semantic172 import parent_outcome
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    original = dict(version='semantic-experiment-v1', observations=[], associations=[], candidates=[])
    for target, number in [('A', 1), ('B', 2)]:
        assert sink.reserve(target) == number
        sink.write(f'dispatch-{number:02}.json', {'attempt': number})
        sink.write(f'result-{number:02}.json', dict(status='completed', initial_output=original))
        parent_outcome(sink, number, 'completed')
    assert sink.status()['completed'] == 2
    assert sink.status()['new_reserved_usd'] == '0.20288'
    assert sink.status()['combined_reserved_usd'] == '0.50720'
    with pytest.raises(ValueError):
        sink.reserve('A')


def test_frozen_capture_mutation_blocks_before_reservation(tmp_path):
    from nilaris.semantic_contract import canonical
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    changed = capture()
    changed['documents'][0]['spans'][0]['text'] = 'unfrozen replacement'
    (sink.root / 'capture.json').write_bytes(canonical(changed))
    with pytest.raises(ValueError, match='mutation'):
        sink.reserve('A')
    assert sink.status()['attempted'] == 0


def test_batch_deadline_and_bad_parent_are_terminal(tmp_path, monkeypatch):
    import time
    from nilaris.semantic172 import parent_outcome
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    sink.reserve('A')
    sink.write('dispatch-01.json', {'attempt': 1})
    sink.write('result-01.json', dict(status='completed', initial_output=dict(
        version='semantic-experiment-v1', observations=[], associations=[], candidates=[])))
    parent_outcome(sink, 1, 'completed')
    reserved = sink.status()['attempts'][0]['reserved_at']
    monkeypatch.setattr(time, 'time', lambda: reserved + 601)
    with pytest.raises(ValueError):
        sink.reserve('B')
    assert sink.status()['new_reserved_usd'] == '0.10144'


def test_initial_invalid_output_is_retained_and_never_coerced(tmp_path):
    from nilaris.semantic172 import finalize_capture
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    sink.reserve('A')
    original = {'not': 'the schema', 'literal': '08.00 bar'}
    response = dict(status='received', initial_output=original)
    result = finalize_capture(sink, dict(attempt=1, target_document_id='A'), response)
    assert result['status'] == 'invalid_output'
    assert sink.read('result-01.json')['initial_output'] == original
    with pytest.raises(ValueError):
        sink.reserve('B')


def test_opt_in_admission_keeps_ordinary_profile_closed():
    from nilaris.admission import AdmissionProfile, _member_disposition
    from test_admission import member
    source = member()
    source['processing_contract'] = 'semantic172-v1'
    assert _member_disposition(source, AdmissionProfile.l1())['reason'] == 'unsupported_context'
    assert _member_disposition(source, AdmissionProfile.l1(), semantic172=True) is None
    source['connector'] = 'drive-readonly-v1'
    assert _member_disposition(source, AdmissionProfile.l1(), semantic172=True)['reason'] == 'unsupported_context'


def test_http_absolute_deadline_marks_timeout_without_retry():
    import httpx
    import time
    from nilaris.semantic_provider import interpret
    from nilaris.semantic172 import request_body
    calls = []
    def delayed(request):
        calls.append(request)
        time.sleep(.2)
        return httpx.Response(200, stream=httpx.ByteStream(b'{}'))
    result = interpret(request_body(capture(), 'A'), credential='synthetic-offline', mode='offline',
        deadline=time.monotonic()+.03, transport=httpx.MockTransport(delayed), timing_profile='semantic172-v1')
    assert result['status'] == 'timeout'
    assert len(calls) == 1


def test_received_initial_output_and_raw_bytes_are_immutable(tmp_path):
    import base64
    import httpx
    import time
    from nilaris.semantic_contract import canonical, MODEL
    from nilaris.semantic172 import request_body, finalize_capture
    from nilaris.semantic_provider import interpret
    original_text = '{ "version":"semantic-experiment-v1", "observations":[], "associations":[], "candidates":[] }'
    raw = canonical(dict(model=MODEL, status='completed', usage=dict(input_tokens=42, output_tokens=37),
        output=[dict(type='message', content=[dict(type='output_text', text=original_text)])]))
    response = interpret(request_body(capture(), 'A'), credential='synthetic-offline', mode='offline',
        deadline=time.monotonic()+2, timing_profile='semantic172-v1',
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(raw))))
    assert response['status'] == 'received'
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    sink.reserve('A')
    sink.write('response-01.json', response)
    result = finalize_capture(sink, dict(attempt=1, target_document_id='A'), response)
    assert result['status'] == 'completed'
    assert base64.b64decode(sink.read('response-01.json')['raw_response_base64']) == raw
    assert sink.read('response-01.json')['status'] == 'received'
    with pytest.raises(FileExistsError):
        finalize_capture(sink, dict(attempt=1, target_document_id='A'), response)


def test_child_completion_never_becomes_parent_completion(tmp_path):
    from nilaris.semantic172 import read_result
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    sink.reserve('A')
    sink.write('dispatch-01.json', {'attempt': 1})
    sink.write('result-01.json', dict(status='completed', initial_output=dict(
        version='semantic-experiment-v1', observations=[], associations=[], candidates=[])))
    assert read_result(sink.root, 1, mode='offline')['status'] == 'parent_outcome_unknown'
    with pytest.raises(ValueError):
        sink.reserve('B')


def test_ordinary_supervision_still_rejects_105_seconds():
    from nilaris.results import StageOutput
    from nilaris.worker import run_controlled
    output = StageOutput('complete', {}, (), 'fixture-v1', 'test')
    result = run_controlled(None, 'job', 0, output, test_wall_seconds=105, test_delay_seconds=0)
    assert result['category'] == 'invalid_controlled_stage'


@pytest.mark.parametrize('credential_path', [None, '/private/tmp/another-owner-only-key',
    '/private/tmp/nilaris-semantic128-private/credentials/../credentials/openai-api-key'])
def test_live_operator_rejects_credential_override_before_any_artifact_or_database_access(credential_path):
    from types import SimpleNamespace
    from nilaris.semantic172 import run
    with pytest.raises(ValueError, match='designated live credential'):
        run(SimpleNamespace(semantic172=True), 'unused', 0, artifact_root='/does-not-exist',
            target_document_id='A', expected_freeze_sha256='unused', mode='live',
            credential_path=credential_path)


def test_live_child_rejects_credential_override_before_any_artifact_or_credential_read(tmp_path):
    from nilaris.semantic172 import execute_capture
    sink = Ledger.__new__(Ledger)
    sink.mode, sink.root = 'live', tmp_path / 'absent-ledger'
    with pytest.raises(ValueError, match='designated live credential'):
        execute_capture(sink, {'credential_path': '/private/tmp/another-owner-only-key'}, deadline=0)


def test_designated_credential_identity_is_part_of_frozen_configuration(tmp_path):
    from nilaris.semantic_contract import canonical
    sink = prepare(tmp_path / 'batch', capture(), ['A', 'B'], mode='offline')
    frozen = sink.read('freeze.json')
    assert frozen['configuration']['live_credential_path'] == (
        '/private/tmp/nilaris-semantic128-private/credentials/openai-api-key')
    frozen['configuration']['live_credential_path'] = '/private/tmp/unapproved-key'
    (sink.root / 'freeze.json').write_bytes(canonical(frozen))
    with pytest.raises(ValueError, match='mutation'):
        sink.verify()
