"""Fresh passage selection fixtures, using the publication observation shape."""
from nilaris import passage_rfq_minimization as rules
from nilaris.block_rfq_minimization import observation

TASK = 'State the air velocity with the fan running.'
FLOW = 'Air velocity shall be exactly 7.3 m/s with the fan running.'
OTHER = 'Air velocity must be at least 9 m/s with the fan running.'
NOTE = 'Delivery is scheduled for Friday.'


def decide(*sentences, task=TASK):
    record = observation(task, 'duct-test.txt', [dict(id='duct', text=' '.join(sentences))])
    return rules.decide(record['original_content'], complete=True)


def test_unrelated_sentence_preserves_passage_and_preview():
    original = decide(FLOW)
    assert original[:2] == (FLOW, None)
    assert decide(FLOW, NOTE) == original


def test_competing_applicable_passages_have_exact_private_ranges():
    second_start = len(FLOW + ' ' + NOTE + ' ')
    for task in (TASK, 'State the air velocity.'):
        preview, reason, trace = decide(FLOW, NOTE, OTHER, task=task)
        assert (preview, reason) == (None, rules.MEANING)
        assert trace['cause'] == 'competing_passages'
        assert trace['candidates'] == [
            dict(id='duct', start=0, end=len(FLOW)),
            dict(id='duct', start=second_start, end=second_start + len(OTHER)),
        ]


def test_back_reference_holds_even_without_quantity_name():
    preview, reason, trace = decide(FLOW, 'This limit applies only with the damper open.')
    assert (preview, reason) == (None, rules.MEANING)
    assert trace['cause'] == 'unresolved_passage'


def test_logistics_subject_cannot_hide_technical_exception():
    text = NOTE[:-1] + ' unless the above value changes.'
    assert decide(FLOW, text)[2]['cause'] == 'unresolved_passage'


def test_private_selection_has_exact_span_after_unrelated_prose():
    private = FLOW[:-1] + ' for quote RIME-481.'
    preview, reason, trace = decide(NOTE, private)
    assert (preview, reason) == (None, rules.SENSITIVITY)
    assert trace['selected'] == dict(id='duct', start=len(NOTE) + 1, end=len(NOTE + ' ' + private))


def test_general_requirement_can_compete_with_condition_specific_requirement():
    general = 'Air velocity shall be exactly 11 m/s.'
    assert decide(FLOW, general)[2]['cause'] == 'competing_passages'
    assert decide(general)[2]['cause'] == 'no_applicable_passage'


def test_compatible_conditions_can_compete_but_cannot_answer_for_each_other():
    guarded = OTHER.replace('with the fan running', 'with the guard closed')
    assert decide(FLOW, guarded)[2]['cause'] == 'competing_passages'
    assert decide(guarded)[2]['cause'] == 'no_applicable_passage'


def test_padding_is_outside_unicode_source_range():
    preview, reason, trace = decide('\n  ' + FLOW + '\r\n')
    assert (preview, reason) == (FLOW, None)
    assert trace['selected'] == dict(id='duct', start=3, end=3 + len(FLOW))


def test_document_routing_does_not_qualify_velocity():
    assert decide(FLOW, 'Quotations are delivered by email.') == decide(FLOW)
