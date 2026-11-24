"""Approved identity/private-delivery edges; no service or database required."""
import pytest
from nilaris.onboarding182 import ControlledIdentityVerifier, Principal, PrivateInvitationDelivery


def test_identity_is_independent_of_invitation_and_revocation_is_current():
    principal = Principal('local-qa', 'alice', 'customer', 'alice-designated')
    verifier = ControlledIdentityVerifier({'independent-login': principal})
    assert verifier.verify('independent-login') == principal
    with pytest.raises(PermissionError):
        verifier.verify('invitation-secret')
    verifier.revoke('independent-login')
    with pytest.raises(PermissionError):
        verifier.verify('independent-login')


def test_delivery_is_private_single_handoff_and_never_overwrites(tmp_path):
    import json
    import stat
    tmp_path.chmod(0o700)
    delivery = PrivateInvitationDelivery(tmp_path)
    reference = 'a' * 32
    delivery.deliver(reference, {'secret': 'first'})
    with pytest.raises(FileExistsError):
        delivery.deliver(reference, {'secret': 'replacement'})
    assert json.loads((tmp_path / reference).read_text()) == {'secret': 'first'}
    assert stat.S_IMODE((tmp_path / reference).stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        delivery.deliver('../outside', {})


def test_current_issuer_requires_matching_current_provisioner_authority():
    from nilaris.provisioning181 import ControlledOperatorVerifier
    operator = Principal('local-qa', 'operator-a', 'operator', '')
    identities = ControlledIdentityVerifier({'operator-login': operator})
    authority = ControlledOperatorVerifier({'operator-login': 'operator-a'})
    assert identities.current_operator('local-qa', 'operator-a', authority)
    authority.credentials.clear()
    assert not identities.current_operator('local-qa', 'operator-a', authority)
    authority.credentials['operator-login'] = 'operator-b'
    assert not identities.current_operator('local-qa', 'operator-a', authority)
