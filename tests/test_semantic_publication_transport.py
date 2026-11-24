"""Root-owned real persistence/HTTP gates; independent synthetic toy evidence."""
import hashlib
import json

from nilaris.admission import AdmissionProfile, Ingestion, migrate
from nilaris.execution import ControlledSource
from nilaris.review import Review
from test_admission import member
from test_command_service import command_process
from test_semantic_publication import toy, repin


def published_semantic(database, product_type='Semantic toy', command='one'):
    migrate(database)
    ingestion = Ingestion(database,AdmissionProfile.l1())
    item = dict(member(),document_id='toy',product_type=product_type)
    job = ingestion.offer('semantic-test',command,[item])['members'][0]['job_id']
    source = b'Rated pressure: 12.00 bar'
    ingestion.capture(job,ingestion.claim(job,0),ControlledSource({'P':('r1',source)}))
    artifact,capture,config,_ = toy()
    artifact['raw_response_base64'] = 'unaltered synthetic provider bytes ' * 4000
    capture['documents'][0]['content_pin'] = hashlib.sha256(source).hexdigest()
    args = ('semantic-test',command,job,ingestion.status(job)['revision'],None,'synthetic-expert',
            artifact,capture,config,repin(artifact,capture,config),True)
    published = Review(database).promote_semantic(*args)
    assert published['saved'] is True
    assert Review(database).promote_semantic(*args) == published
    return job,published['proposal_id'],artifact


def test_semantic_provenance_http_is_explicit_bounded_and_scoped(database,command_process):
    job,proposal,artifact = published_semantic(database)
    other_job,other_proposal,_ = published_semantic(database,'Other semantic toy','two')
    client,start,_ = command_process
    start()
    default = client(dict(operation='result',job_id=job))
    context = default['publications'][0]['semantic_context']
    assert 'original_artifact' not in context
    assert context['provenance']['proposal_id'] == proposal
    command = dict(operation='result',job_id=job,proposal_id=proposal,provenance=True)
    page = client(command)
    cursor = page['next_cursor']
    fragments = [page['fragment']]
    while page['next_cursor']:
        page = client(dict(command,cursor=page['next_cursor']))
        fragments.append(page['fragment'])
    assert json.loads(''.join(fragments))['artifact'] == artifact
    assert client(dict(operation='result',job_id=job,cursor=cursor))['category'] == 'invalid_cursor'
    assert client(dict(operation='result',job_id=other_job,proposal_id=other_proposal,
                       provenance=True,cursor=cursor))['category'] == 'invalid_cursor'
    assert client(dict(command,proposal_id='missing'))['category'] == 'not_found'
    for bad in [dict(command,provenance='yes'),dict(command,provenance=1),
                dict(command,proposal_id=False),dict(operation='result',job_id=job,provenance=True)]:
        assert client(bad)['category'] == 'invalid_request'
    review = Review(database)
    created = review.create('semantic-test','draft',job,proposal,'Semantic toy',None,'synthetic-expert')
    assert created['saved'] is True
    view = client(dict(operation='review_read',draft_id=created['draft_id']))
    assert 'original_artifact' not in view['initial']['semantic_context']
    assert view['initial']['semantic_context']['provenance'] == context['provenance']
    assert view['definitions'][0]['decision'] == 'awaiting'
    assert client(dict(operation='result',job_id=job)) == default
