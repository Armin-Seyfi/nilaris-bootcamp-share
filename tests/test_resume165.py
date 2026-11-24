"""Credential-free resume policy: offline public validation and dispatch bounds."""
import copy
import subprocess

import pytest

from test_drive155_source import configuration, authority


def resume():
    from nilaris.resume165 import Resume
    source = configuration()
    return Resume({}, source, authority(source), dict(version=1,
        discovery_operation='discover-one', acquisition_operation='acquire-one',
        page_id='page-one', job_id='original-job', publication_id='original-publication',
        revision=4, generation=2))


def status(policy):
    return dict(known=True, id='original-job', state='completed', revision=4, generation=2,
        input=policy.member, content_pin=policy.member['expected_sha256'],
        canonical_job_id='original-job', results=[dict(id='original-publication', kind='initial',
        initial_publication_id='original-publication', approved=False)])


def test_resume_refuses_all_fresh_dispatch_and_foreign_reads_without_dependencies():
    policy = resume()
    for operation in ('offer','resolve','discovery_stage','discovery_read','discovery_admit',
                      'drive_discover','drive_admit','drive_acquire','execute_fixture_v1',
                      'cancel','retry','reprocess','reconcile','review_incoming'):
        assert not policy.permits_command(operation, {})
    assert policy.permits_command('status', {'job_id':'original-job'})
    assert not policy.permits_command('result', {'job_id':'other-job'})
    assert policy.permits_command('drive_operation', dict(binding_id='synthetic-binding', operation_id='discover-one'))
    assert not policy.permits_command('drive_operation', dict(binding_id='synthetic-binding', operation_id='fresh'))


def test_status_requires_exact_completed_job_input_pin_context_and_publication():
    policy = resume()
    assert policy.valid_status(status(policy))
    for key, value in [('known',False), ('id','other'), ('state','queued'), ('revision',3),
                       ('generation',1), ('input',{}), ('content_pin','wrong'), ('results',[])]:
        assert not policy.valid_status(dict(status(policy), **{key:value}))


def test_journal_requires_original_kind_request_namespace_and_historical_job():
    policy = resume()
    row = dict(namespace=policy.namespace, operation_id='acquire-one', kind='acquire',
        state='completed', request=dict(job_id='original-job', expected_revision=0),
        job_id='original-job', generation=1,
        result=dict(job=dict(status(policy), state='queued', revision=2, generation=1)))
    assert policy.valid_journal(row)
    for key, value in [('namespace','wrong'), ('kind','discover'), ('state','pending'),
                       ('job_id','other'), ('request',{}), ('result',{})]:
        assert not policy.valid_journal(dict(row, **{key:value}))
    wrong = copy.deepcopy(row)
    wrong['result']['job']['input']['intent_id'] = 'other'
    assert not policy.valid_journal(wrong)


def test_resume_import_never_reaches_provider_or_credential_modules(tmp_path):
    script = '''
import sys
class Tripwire:
    def find_spec(self, fullname, *args):
        if fullname in {'nilaris.drive155_auth','nilaris.drive155_service',
                        'nilaris.drive155_journey','nilaris.drive155_supervision'}:
            raise AssertionError('credential or provider dependency reached')
sys.meta_path.insert(0, Tripwire())
from nilaris.resume165 import Resume
'''
    source = configuration()
    manifest = resume().manifest
    script += '\nsource,authority,manifest = ' + repr((source, authority(source), manifest))
    script += '''
r=Resume({},source,authority,manifest)
for command in ({'binding_id':'synthetic-binding','operation_id':'fresh'},
                {'binding_id':'foreign','operation_id':'discover-one'}):
    assert r.drive_operation(**command)['category']=='resume_scope_refused'
assert r.check('drive_acquire',{})['category']=='resume_scope_refused'
assert not any(name in sys.modules for name in ('nilaris.drive155_auth',
    'nilaris.drive155_service','nilaris.drive155_journey','nilaris.drive155_supervision'))
'''
    probe = tmp_path / 'import_probe.py'
    probe.write_text(script)
    result = subprocess.run(['/opt/miniconda3/bin/conda','run','--no-capture-output',
        '-n','nilaris','python','runtime/a1/with_overlay.py',str(probe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_malformed_completed_shapes_refuse_instead_of_escaping_validation():
    policy = resume()
    assert not policy.valid_status(dict(status(policy),results=[None]))
    for page in (None, [], 'page'):
        assert not policy.valid_journal(dict(namespace=policy.namespace,
            operation_id='discover-one',kind='discover',state='completed',
            request=dict(page_id='page-one'),result=dict(page=page)))
