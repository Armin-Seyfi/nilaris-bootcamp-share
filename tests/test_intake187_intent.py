"""Private original command recovery through the authenticated HTTP boundary."""
import pytest
from test_onboarding182_pg import http_client, onboarding
from test_scoped_intake import setup_source
from nilaris.scoped_intake import CONTROLLED_ROUTE


@pytest.mark.parametrize('kind', ['admit', 'set-policy'])
def test_reauthentication_recovers_original_body_before_and_after_commit(onboarding, http_client, kind):
    intake, customer, _, _ = setup_source(onboarding)
    connect, _ = http_client
    common = dict(customer_id=customer, kind=kind)
    command = dict(command_id='original', source_key='pump-sheet',policy_revision=0) if kind == 'admit' else dict(
        command_id='original', expected_revision=0, policy=dict(active=True, route=CONTROLLED_ROUTE,
                                                               evidence_ref='owner-local-decision'))
    with connect('alice-login', intake=intake) as client:
        code, unlocked = client('/intake/unlock', common)
        assert code == 200
        selected = dict(common, slot=unlocked['slot'])
        code, checked = client('/intake/pending', dict(selected, command=command))
        assert code == 200 and checked['state'] == 'absent' and checked['command'] == command
        code, staged = client('/intake/prepare',dict(selected,command=command))
        assert code == 200 and staged['state'] == 'prepared'
        code, result = client('/intake/'+kind, dict(selected,tag=staged['tag']))
        assert code == 200 and result.get('job_id',result.get('revision')) and result['intent_tag']
    with connect('bob-login', intake=intake) as other:
        assert other('/intake/pending', selected)[0] == 403
    with connect('alice-login', intake=intake) as fresh:
        fresh_key = fresh('/intake/unlock', common)[1]
        assert all(fresh_key[k] == unlocked[k] for k in ('slot', 'scope', 'key'))
        assert fresh_key['policy_revision'] == (0 if kind == 'admit' else None)
        code, checked = fresh('/intake/pending', selected)
        assert code == 200 and checked['state'] == 'known' and checked['command'] == command
        assert checked['outcome']['job_id' if kind == 'admit' else 'revision'] == result.get('job_id', result.get('revision'))
        assert fresh('/intake/ack', dict(selected, tag=checked['tag']))[0] == 200
        assert fresh('/intake/pending', selected)[1]['state'] == 'empty'


def test_unissued_original_keeps_policy_revision_and_cannot_admit_after_policy_change(onboarding, http_client):
    intake, customer, _, _ = setup_source(onboarding)
    connect, _ = http_client
    with connect('alice-login', intake=intake) as client:
        def select(kind):
            selector = dict(customer_id=customer, kind=kind)
            return dict(selector, slot=client('/intake/unlock', selector)[1]['slot'])
        admit = select('admit')
        command = dict(source_key='pump-sheet', command_id='unissued', policy_revision=0)
        policy = select('set-policy')
        change = dict(command_id='activate', expected_revision=0,
                      policy=dict(active=True, route=CONTROLLED_ROUTE, evidence_ref='original'))
        tag = client('/intake/prepare', dict(policy, command=change))[1]['tag']
        assert client('/intake/set-policy', dict(policy, tag=tag))[1]['revision'] == 1
    with connect('alice-login', intake=intake) as fresh:
        # The browser retained the original before the first request reached the server.
        inspected = fresh('/intake/pending', dict(admit, command=command))[1]
        assert inspected['state'] == 'absent' and not inspected['staged']
        result = fresh('/intake/retry', dict(admit, tag=inspected['tag'], command=command))[1]
        assert result['state'] == 'refused'
        assert fresh('/intake/pending', admit)[1]['outcome']['state'] == 'refused'
        assert fresh('/intake/jobs', dict(customer_id=customer))[1]['jobs'] == []
