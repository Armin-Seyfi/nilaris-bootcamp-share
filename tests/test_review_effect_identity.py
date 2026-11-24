"""Shared immutable publication/effect JSON identity remains strict and replayable."""
from dataclasses import replace

from nilaris.admission import Ingestion
from test_recovery import literal_output, processing


def with_metadata(output, value):
    effects = list(output.effects)
    effects[0] = replace(effects[0],payload=dict(effects[0].payload,extra_metadata=value))
    return replace(output,effects=tuple(effects))


def test_changed_boolean_to_number_same_attempt_is_not_acknowledged_as_replay(database):
    client,job,claim = processing(database)
    first = with_metadata(literal_output(),True)
    completed = client.publish(job,claim,first)
    assert completed['state'] == 'completed'
    before = client.result(job)
    assert client.publish(job,claim,with_metadata(literal_output(),1))['category'] == 'stale_commit'
    assert Ingestion(database).result(job) == before


def test_changed_boolean_to_number_existing_effect_cannot_publish_supplement(database):
    client,job,claim = processing(database)
    first = with_metadata(replace(literal_output(),outcome='partial',
        coverage=dict(inspected=['page:1'],unread=['page:2'],limits=[])),True)
    partial = client.publish(job,claim,first)
    before = client.result(job)
    queued = client.retry(job,partial['revision'],partial['generation'],'operator','Read missing page')
    claim = client.claim(job,queued['revision'])
    assert client.publish(job,claim,with_metadata(literal_output(),1))['category'] == 'effect_conflict'
    assert Ingestion(database).result(job) == before


def test_same_numeric_snapshot_and_effect_metadata_replays_after_database_normalization(database):
    client,job,claim = processing(database)
    first = with_metadata(replace(literal_output(),outcome='partial',
        coverage=dict(inspected=['page:1'],unread=['page:2'],limits=[])),1e100)
    partial = client.publish(job,claim,first)
    assert client.publish(job,claim,first)['state'] == 'partial'
    queued = client.retry(job,partial['revision'],partial['generation'],'operator','Read missing page')
    completed = with_metadata(literal_output(),1e100)
    current = client.claim(job,queued['revision'])
    assert client.publish(job,current,completed)['state'] == 'completed'
    assert client.publish(job,current,completed)['state'] == 'completed'
