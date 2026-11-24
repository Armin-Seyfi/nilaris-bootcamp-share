"""Independent development examples for condition-aware passage selection."""
import pytest

from nilaris import qualified_rfq_minimization as rules
from nilaris.block_rfq_minimization import observation

COLD = 'Surface temperature must be at most 18.5 °C during startup.'
WARM = 'Surface temperature shall be at most 34 °C during steady operation.'
REFERENCE = 'Buyer reference: QUILL-839.'


def decide(task, clauses):
    record = observation(task, 'development-quill.txt', [
        dict(id='plate', text=REFERENCE + ' ' + ' '.join(clauses))])
    return rules.decide(record['original_content'], complete=True)


@pytest.mark.parametrize('task', ['What is the surface temperature during startup?',
                                  'Please specify the required surface temperature at startup.'])
def test_qualifier_survives_paraphrase(task):
    result, reason, trace = decide(task, [WARM, COLD])
    assert (result, reason) == (COLD, None)
    start = len(REFERENCE + ' ' + WARM + ' ')
    assert trace['selected'] == dict(id='plate', start=start, end=start + len(COLD))
    assert trace['request']['condition'] == 'during startup'


def test_ambiguity_is_resolved_before_sensitive_disclosure():
    private = WARM[:-1] + ' under account QUILL-839.'
    _, reason, trace = decide('Report the surface temperature.', [COLD, private])
    assert reason == rules.MEANING and trace['stage'] == 'selection' and 'selected' not in trace
    result, reason, trace = decide('Report the surface temperature during steady operation.', [COLD, private])
    assert (result, reason) == (None, rules.SENSITIVITY) and trace['stage'] == 'sensitivity'
    assert trace['selected']['end'] - trace['selected']['start'] == len(private)


def test_unknown_modifier_is_never_dropped():
    _, reason, trace = decide('Report the surface temperature except during startup.', [COLD])
    assert reason == rules.MEANING and not trace['understood']
