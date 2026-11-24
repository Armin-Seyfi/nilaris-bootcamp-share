"""Cold process failure at private journal publication using only a disposable directory."""
import json
import os
from multiprocessing import get_context

import pytest

from nilaris.customer_review import ReviewJournal
from nilaris.onboarding182 import Principal
from nilaris.onboarding182_ui import PrivateCommandJournal
from nilaris.provisioning181 import ProvisioningRefused


ACTOR = Principal('synthetic-issuer', 'reviewer', 'customer', 'synthetic-binding')


def die_after_publication(directory, channel, body):
    journal = PrivateCommandJournal(directory)
    target = journal._path(ACTOR, channel)
    def stop_after(operation):
        def call(source, destination):
            operation(source, destination)
            if destination == target:
                os._exit(73)
        return call
    os.link = stop_after(os.link)
    os.replace = stop_after(os.replace)
    journal.save(ACTOR, body, channel=channel)


@pytest.mark.parametrize('role,body', [
    ('Reviewer', {'command_id':'original-save','reason':'Synthetic correction'}),
    ('browser-key-Reviewer', {'key':'a' * 64}),
])
def test_published_expert_intent_or_key_survives_immediate_writer_death(tmp_path, role, body):
    tmp_path.chmod(0o700)
    channel = ReviewJournal('customer-a','job-a',role)
    writer = get_context('fork').Process(target=die_after_publication, args=(tmp_path, channel, body))
    writer.start()
    writer.join(6)
    assert writer.exitcode == 73
    cold = PrivateCommandJournal(tmp_path)
    assert cold._path(ACTOR, channel).stat().st_nlink == 1
    assert cold.read(ACTOR, channel=channel) == body
    with pytest.raises(PermissionError):
        cold.save(ACTOR, {'different':'body'}, channel=channel)


@pytest.mark.parametrize('link_name,owned', [('.pending-' + 'b'*32, True), ('unrelated', False)])
def test_legacy_reconciliation_requires_proven_same_inode_temporary_link(tmp_path, link_name, owned):
    tmp_path.chmod(0o700)
    channel = ReviewJournal('customer-a','job-a','Reviewer')
    journal = PrivateCommandJournal(tmp_path)
    target = journal._path(ACTOR, channel)
    temporary = tmp_path / link_name
    body = {'command_id':'original-save','reason':'Synthetic correction'}
    temporary.write_text(json.dumps(body))
    temporary.chmod(0o600)
    os.link(temporary, target)
    original_inode = target.stat().st_ino
    if owned:
        assert journal.read(ACTOR, channel=channel) == body
        assert not temporary.exists() and target.stat().st_nlink == 1
        assert target.stat().st_ino == original_inode
    else:
        with pytest.raises(ProvisioningRefused):
            journal.read(ACTOR, channel=channel)
        assert temporary.samefile(target) and target.stat().st_nlink == 2
