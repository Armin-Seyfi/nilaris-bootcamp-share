"""Root-only serial PostgreSQL/HTTP coverage. Never select in offline worker checks."""
import json
import subprocess
import time

import psycopg
from psycopg.types.json import Jsonb

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.command_service import Configuration, request
from test_command_service import PYTHON, ROOT, stop
from test_resume165 import resume, status


def test_resume_http_refuses_dispatch_and_reads_only_original_journals(database,tmp_path):
    migrate(database)
    policy=resume()
    member=policy.member
    # Build controlled completed journal records directly at the persistence boundary.
    # No provider, source runner or processing call participates in this test.
    offered=Ingestion(database,AdmissionProfile.l1()).offer('resume-test','offer',[dict(member,connector='synthetic')])
    job_id=offered['members'][0]['job_id']
    policy.manifest['job_id']=policy.job_id=job_id
    policy.review_namespace='local-review-ui:'+job_id
    historical=dict(status(policy),id=job_id,canonical_job_id=job_id,
                    state='queued',revision=2,generation=1,results=[])
    with psycopg.connect(**database) as connection:
        connection.execute('UPDATE admission_jobs SET input=%s,state=\'completed\',revision=4,generation=2,content_pin=%s,canonical_job_id=%s WHERE id=%s',
                           (Jsonb(member),member['expected_sha256'],job_id,job_id))
        # Existing table/migration shape; no DriveJourney import or dispatch.
        connection.execute('''INSERT INTO drive155_operations (namespace,operation_id,kind,request,state,result,job_id,generation,reserved_requests,reservation_bytes,active,reserved_cpu_seconds,accounting) VALUES (%s,%s,'acquire',%s,'completed',%s,%s,1,5,1048576,false,20,%s)''',
            (policy.namespace,'acquire-one',Jsonb(dict(job_id=job_id,expected_revision=0)),
             Jsonb(dict(job=historical)),job_id,Jsonb(dict(proved=True))))
    # Direct journal seam verifies real SQL namespace filtering and kind/request gates.
    policy.database=database
    assert policy.drive_operation('synthetic-binding','acquire-one')['job']['id']==job_id
    assert policy.drive_operation('synthetic-binding','missing')['category']=='resume_scope_refused'
    with psycopg.connect(**database) as connection:
        connection.execute("UPDATE drive155_operations SET kind='discover'")
    assert policy.drive_operation('synthetic-binding','acquire-one')['category']=='resume_journal_mismatch'
    with psycopg.connect(**database) as connection:
        connection.execute('DELETE FROM drive155_operations')
    absent=policy.drive_operation('synthetic-binding','acquire-one')
    assert absent['known'] is False and absent['category']=='source_operation_unknown'
    # Authenticated existing command HTTP parser must invoke the guard before dispatch.
    # A dedicated launcher is created only inside this disposable fixture directory.
    private=tmp_path/'command.json'
    private.write_text(json.dumps(dict(database=database,token='synthetic-resume-token')))
    private.chmod(0o600)
    launcher=tmp_path/'listener.py'
    launcher.write_text('''
import json,sys
from nilaris.command_service import Configuration,serve
from nilaris.resume165 import Resume
p=json.load(open(sys.argv[1])); c=Configuration.load(sys.argv[2])
r=Resume(c.database,p['source'],p['authority'],p['manifest'])
c.source_operator=r;c.command_guard=r.check
serve(c)
''')
    from test_drive155_source import configuration,authority
    source=configuration()
    binding=tmp_path/'binding.json'
    binding.write_text(json.dumps(dict(source=source,authority=authority(source),manifest=policy.manifest)))
    process=subprocess.Popen([*PYTHON,str(launcher),str(binding),str(private)],cwd=ROOT,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    config=Configuration.load(private)
    try:
        for _ in range(100):
            response=request(config,dict(operation='status',job_id='other'))
            if response.get('category')=='resume_scope_refused':break
            time.sleep(.05)
        else:raise AssertionError('resume listener unavailable')
        for command in [dict(operation='drive_discover',binding_id='synthetic-binding',operation_id='fresh',page_id='fresh'),
                        dict(operation='execute_fixture_v1',job_id=job_id,expected_revision=4),
                        dict(operation='result',job_id='other')]:
            assert request(config,command)['category']=='resume_scope_refused'
        assert request(config,dict(operation='status',job_id=job_id))['category']=='resume_job_mismatch'
        assert request(config,dict(operation='drive_operation',binding_id='synthetic-binding',operation_id='acquire-one'))['known'] is False
    finally:
        stop(process)
