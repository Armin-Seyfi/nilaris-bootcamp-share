"""Admission command identity remains strict as review shares its byte ledger."""
from nilaris.admission import AdmissionProfile, Ingestion, migrate
from test_admission import member


def test_offer_boolean_to_number_metadata_change_conflicts_under_same_key(database):
    migrate(database)
    client = Ingestion(database,AdmissionProfile.l1())
    initial = dict(member(),expert_flag=True)
    first = client.offer('test','typed-offer',[initial])
    assert first['totals']['accepted'] == 1
    changed = dict(initial,expert_flag=1)
    assert client.offer('test','typed-offer',[changed])['category'] == 'command_conflict'
    assert Ingestion(database).resolve('test','typed-offer') == first


def test_numeric_json_metadata_replays_after_postgresql_normalization(database):
    migrate(database)
    client = Ingestion(database,AdmissionProfile.l1())
    payload = dict(member(),numeric_metadata=1e100)
    first = client.offer('test','numeric-offer',[payload])
    assert first['totals']['accepted'] == 1
    assert Ingestion(database,AdmissionProfile.l1()).offer('test','numeric-offer',[payload]) == first


def test_review_numeric_command_payload_replays_after_database_normalization(database):
    from nilaris.review import Review
    from test_review import published
    job,proposal_id = published(database)
    review = Review(database)
    draft = review.create('test','create',job,proposal_id,'Fixture pump',None,'synthetic-expert')['draft_id']
    view = review.read(draft)
    view['definitions'][0]['unresolved_numeric_detail'] = 1e100
    args = ('test','numeric-review',draft,0,view['definitions'],view['associations'],'synthetic-expert','Retain unresolved detail')
    first = review.save(*args)
    assert first['saved'] is True
    assert Review(database).save(*args) == first
