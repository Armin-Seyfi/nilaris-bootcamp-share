"""Private exact command journal, the persistence edge used by native UI."""
import pytest
from nilaris.onboarding182 import Principal
from nilaris.onboarding182_ui import PrivateCommandJournal


def test_pending_command_survives_restart_and_other_identity_cannot_see_it(tmp_path):
    tmp_path.chmod(0o700)
    alice = Principal('local-qa', 'alice', 'customer', 'alice-designated')
    bob = Principal('local-qa', 'bob', 'customer', 'bob-designated')
    request = {'action': 'accept', 'invitation_id': 'a'*32, 'secret': 'private-secret', 'command_id': 'exact-accept'}
    journal = PrivateCommandJournal(tmp_path)
    journal.save(alice, request)
    fresh = PrivateCommandJournal(tmp_path)
    assert fresh.read(alice) == request
    assert fresh.read(bob) is None
    with pytest.raises(PermissionError):
        fresh.save(alice, dict(request, command_id='replacement-key'))
    fresh.mark_known(alice)
    fresh.acknowledge(alice)
    assert fresh.read(alice) is None


def test_unknown_outcome_cannot_discard_exact_pending_command(tmp_path):
    tmp_path.chmod(0o700)
    alice = Principal('local-qa', 'alice', 'customer', 'alice-designated')
    journal = PrivateCommandJournal(tmp_path)
    journal.save(alice, {'action': 'accept', 'command_id': 'same-key'})
    with pytest.raises(PermissionError):
        journal.acknowledge(alice)
    journal.mark_known(alice)
    journal.acknowledge(alice)
    assert journal.read(alice) is None


def test_unknown_invitation_and_explicit_revocation_survive_independently(tmp_path):
    tmp_path.chmod(0o700)
    operator = Principal('local-qa', 'operator', 'operator', '')
    invite = {'action': 'invite', 'command_id': 'original-invite', 'recipient_binding': 'exact-original'}
    revoke = {'action': 'revoke', 'command_id': 'original-invite'}
    journal = PrivateCommandJournal(tmp_path)
    journal.save(operator, invite)
    journal.save(operator, revoke, channel='revocation')
    fresh = PrivateCommandJournal(tmp_path)
    assert fresh.read(operator) == invite
    assert fresh.read(operator, channel='revocation') == revoke
    with pytest.raises(PermissionError):
        fresh.acknowledge(operator)
    with pytest.raises(PermissionError):
        fresh.acknowledge(operator, channel='revocation')
    fresh.mark_known(operator, channel='revocation')
    fresh.acknowledge(operator, channel='revocation')
    assert fresh.read(operator) == invite
    assert fresh.read(operator, channel='revocation') is None
    with pytest.raises(PermissionError):
        fresh.acknowledge(operator)


def test_suspension_command_preserves_unresolved_invitation(tmp_path):
    tmp_path.chmod(0o700)
    operator = Principal('local-qa', 'operator', 'operator', '')
    journal = PrivateCommandJournal(tmp_path)
    invite = dict(action='invite', command_id='invite')
    suspend = dict(action='change_access', command_id='suspend', operation_id='own',
                   access_state='suspended', expected_revision=0)
    journal.save(operator, invite)
    journal.save(operator, suspend, channel='access')
    fresh = PrivateCommandJournal(tmp_path)
    assert fresh.read(operator, channel='access') == suspend
    with pytest.raises(PermissionError):
        fresh.save(operator, dict(suspend, expected_revision=1), channel='access')
    fresh.mark_known(operator, channel='access')
    fresh.acknowledge(operator, channel='access')
    assert fresh.read(operator) == invite
