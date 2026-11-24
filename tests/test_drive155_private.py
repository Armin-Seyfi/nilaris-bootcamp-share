"""Offline custody checks using only generated temporary JSON, never owner files."""
from pathlib import Path

import pytest

from nilaris.drive155_auth import PrivateCredentialRegistry, SourceUnavailable
from nilaris.drive155_service import configuration


@pytest.mark.parametrize('consumer', ['credential', 'activation'])
@pytest.mark.parametrize('fault', ['relative', 'repository', 'symlink', 'public', 'oversized',
                                    'duplicate', 'nested_duplicate', 'array'])
def test_private_consumers_refuse_unsafe_or_ambiguous_synthetic_json(tmp_path, consumer, fault):
    repository = tmp_path / 'repo'
    repository.mkdir()
    path = tmp_path / 'synthetic.json'
    payload = {
        'oversized': b' ' * 65537,
        'duplicate': b'{"x":1,"x":2}',
        'nested_duplicate': b'{"x":{"y":1,"y":2}}',
        'array': b'[]',
    }.get(fault, b'{"synthetic":true}')
    if fault == 'repository':
        path = repository / 'synthetic.json'
    path.write_bytes(payload)
    path.chmod(0o644 if fault == 'public' else 0o600)
    if fault == 'relative':
        path = Path('synthetic.json')
    elif fault == 'symlink':
        link = tmp_path / 'synthetic-link.json'
        link.symlink_to(path)
        path = link
    if consumer == 'credential':
        reader = PrivateCredentialRegistry({'synthetic': str(path)}, repository_root=repository)
        with pytest.raises(SourceUnavailable, match='^source unavailable$'):
            reader('synthetic')
    else:
        with pytest.raises(ValueError, match='^source activation unavailable$'):
            configuration(tmp_path / 'absent-command', path, tmp_path / 'absent-authority',
                          tmp_path / 'absent-registry', repository_root=repository)


def test_private_registry_defers_opening_and_accepts_exact_bounded_dictionary(tmp_path):
    path = tmp_path / 'synthetic.json'
    registry = PrivateCredentialRegistry({'synthetic': str(path)}, repository_root=tmp_path / 'repo')
    # Constructing a registry must not open the absent private file.
    assert not path.exists()
    path.write_bytes(b'{"synthetic":true}' + b' ' * (65536 - 18))
    path.chmod(0o600)
    assert registry('synthetic') == {'synthetic': True}
