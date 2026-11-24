"""Bounded authenticated textual review actions; synthetic decisions only."""
from http.client import HTTPConnection
import json
import time

from test_command_service import command_process, stop
from test_review import published


def test_textual_review_lost_save_response_resolves_after_restart(database,command_process):
    job,proposal = published(database)
    client,start,_ = command_process
    server = start()
    created = client(dict(operation='review_create',namespace='test',command_id='create',job_id=job,
                          proposal_id=proposal,product_type='Fixture pump',expected_base=None,actor='synthetic-expert'))
    assert created['saved'] is True
    draft = created['draft_id']
    status = client(dict(operation='status',job_id=job))
    assert type(status['usage']['retained_bytes']) is int
    assert type(status['admission']['review_retained_bytes']) is int
    assert status['reservation_bytes'] == status['usage']['retained_bytes']
    view = client(dict(operation='review_read',draft_id=draft))
    for definition in view['definitions']:
        definition['decision'] = 'accepted'
    view['definitions'][0]['label'] = 'Rated flow rate'
    command = dict(operation='review_save',namespace='test',command_id='lost-save',draft_id=draft,
                   expected_revision=0,definitions=view['definitions'],associations=view['associations'],
                   actor='synthetic-expert',reason='Synthetic reviewed label')
    connection = HTTPConnection('127.0.0.1',18765,timeout=3)
    connection.request('POST','/command',json.dumps(command),{'Authorization':'Bearer synthetic-test-token'})
    connection.close()
    for _ in range(30):
        resolved = client(dict(operation='review_resolve',namespace='test',command_id='lost-save'))
        if resolved.get('saved'):
            break
        time.sleep(.02)
    assert resolved['saved'] is True
    assert client(command) == resolved
    grown_status = client(dict(operation='status',job_id=job))
    assert type(grown_status['usage']['retained_bytes']) is int
    assert type(grown_status['admission']['review_retained_bytes']) is int
    assert grown_status['usage']['retained_bytes'] > status['usage']['retained_bytes']
    assert grown_status['reservation_bytes'] == grown_status['usage']['retained_bytes']
    stop(server)
    uncertain = client(dict(command,command_id='unavailable',expected_revision=1))
    assert uncertain['known'] is False and uncertain.get('saved') is not True
    start()
    resumed = client(dict(operation='review_resume',draft_id=draft))
    assert resumed['revision'] == 1 and resumed['definitions'][0]['label'] == 'Rated flow rate'
    approved = client(dict(operation='review_approve',namespace='test',command_id='approve',draft_id=draft,
                          expected_revision=1,product_type='Fixture pump',actor='synthetic-expert',explicit=True))
    assert approved['approved'] is True
    incoming_job,incoming_id = published(database,value='13',revision='r2')
    compared = client(dict(operation='review_incoming',draft_id=draft,job_id=incoming_job,proposal_id=incoming_id))
    assert compared['read_only'] is True and compared['base_version'] == approved['approved_version']
    assert compared['incoming']['observations'][0]['value'] == '13'
    assert client(dict(operation='attribute_approve',draft_id=draft))['category'] == 'invalid_request'
