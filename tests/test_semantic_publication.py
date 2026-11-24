"""Offline toy publication contract: no database, provider or evaluated fixtures."""
from copy import deepcopy
import hashlib
import pytest

from nilaris.results import canonical


def toy():
    ref = dict(document_id='toy', source_revision='r1', locator_id='L1')
    output = dict(version='semantic-experiment-v1', observations=[dict(
        id='o1', **ref, label='Rated pressure', original_value='12.00',
        original_unit='bar', presence='observed', domain='technical')], associations=[],
        candidates=[dict(id='c1', label='Rated pressure', value_type='decimal', unit='bar',
                         domain='technical', evidence_refs=[ref], uncertainty='')])
    artifact = dict(status='completed', target_document_id='toy', initial_output=output)
    capture = dict(documents=[dict(document_id='toy', source_revision='r1',
        content_pin='a'*64, media_type='text/plain', spans=[dict(locator_id='L1',
        text='Rated pressure: 12.00 bar', locator=dict(text='line 1'))],
        coverage=dict(inspected=['line 1'], unread=[]))])
    config = dict(contract='toy-config', source_kind='synthetic')
    pins = {key: hashlib.sha256(canonical(value).encode()).hexdigest()
            for key, value in dict(artifact=artifact,capture=capture,configuration=config).items()}
    return artifact, capture, config, pins


def test_completed_scalar_preserves_initial_evidence_and_mapping():
    from nilaris.semantic_publication import build_projection
    args = toy()
    before = deepcopy(args)
    result = build_projection(*args)
    assert result['stage_contract'] == 'semantic-projection-v1'
    assert result['observations'][0]['value'] == '12.00'
    assert result['observations'][0]['unit'] == 'bar'
    assert result['candidates'][0]['observation_ids'] == ['o1']
    assert result['semantic_context']['original_output'] == args[0]['initial_output']
    assert result['semantic_context']['record_mapping'] == [
        dict(kind='observation',id='o1',disposition='represented',reason=None,projected_ids=['o1']),
        dict(kind='candidate',id='c1',disposition='represented',reason=None,projected_ids=['c1'])]
    assert args == before


@pytest.mark.parametrize('change', ['pin','shape','duplicate','forged','partial','timeout','unknown'])
def test_invalid_or_inconclusive_result_has_no_projection(change):
    from nilaris.semantic_publication import build_projection, digest
    artifact,capture,config,pins = toy()
    if change == 'shape':
        artifact['initial_output']['candidates'][0]['value_type'] = 4
    elif change == 'duplicate':
        artifact['initial_output']['observations'] *= 2
    elif change == 'forged':
        artifact['initial_output']['candidates'][0]['evidence_refs'][0]['source_revision'] = 'invented'
    elif change != 'pin':
        artifact['status'] = change
    if change != 'pin':
        pins['artifact'] = digest(artifact)
    else:
        pins['artifact'] = '0'*64
    with pytest.raises(ValueError):
        build_projection(artifact,capture,config,pins)


def test_public_command_requires_explicit_promotion_before_persistence():
    from nilaris.review import Review
    artifact,capture,configuration,expected_pins = toy()
    result = Review({}).promote_semantic('toy','one','job',0,None,'expert',
        artifact,capture,configuration,expected_pins,False)
    assert result == dict(known=True,saved=False,approved=False,category='invalid_request')


def test_public_command_refuses_forged_bundle_before_persistence():
    from nilaris.review import Review
    artifact,capture,configuration,expected_pins = toy()
    expected_pins['artifact'] = '0'*64
    result = Review({}).promote_semantic('toy','one','job',0,None,'expert',
        artifact,capture,configuration,expected_pins,True)
    assert result['category'] == 'invalid_semantic_projection'


@pytest.mark.parametrize('malformed', ['configuration', 'artifact', 'coverage'])
@pytest.mark.parametrize('value', [None, [], 'invalid', 7])
def test_public_command_refuses_malformed_bundle_objects_before_persistence(malformed, value):
    from nilaris.review import Review
    artifact,capture,configuration,_ = toy()
    if malformed == 'configuration':
        configuration = value
    elif malformed == 'artifact':
        artifact = value
    else:
        capture['documents'][0]['coverage'] = value
    result = Review({}).promote_semantic('toy','one','job',0,None,'expert',
        artifact,capture,configuration,repin(artifact,capture,configuration),True)
    assert result == dict(known=True,saved=False,approved=False,
                          category='invalid_semantic_projection')


@pytest.mark.parametrize('value_type,unit', [
    ('decimal',None), ('decimal',''), ('decimal',' \t'),
    ('text','bar'), ('text',''), ('text',' \t')])
def test_incompatible_candidate_unit_is_retained_read_only_without_coercion(value_type, unit):
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    row = artifact['initial_output']['candidates'][0]
    row.update(value_type=value_type,unit=unit)
    pins = repin(artifact,capture,config)
    before = deepcopy((artifact,capture,config,pins))
    result = build_projection(artifact,capture,config,pins)
    assert result['candidates'] == []
    context = result['semantic_context']
    assert context['read_only_records'] == [dict(kind='candidate',record=row,reason='unsupported_unit')]
    assert context['record_mapping'] == [
        dict(kind='observation',id='o1',disposition='represented',reason=None,projected_ids=['o1']),
        dict(kind='candidate',id='c1',disposition='read_only',reason='unsupported_unit',projected_ids=[])]
    assert result['observations'][0]['value'] == '12.00'
    assert result['observations'][0]['unit'] == 'bar'
    assert context['original_artifact'] == artifact
    assert context['original_output'] == artifact['initial_output']
    assert context['origin'] == pins
    assert (artifact,capture,config,pins) == before


@pytest.mark.parametrize('value_type,unit', [('decimal','bar'), ('decimal',' bar '), ('text',None)])
def test_compatible_candidate_unit_remains_literal_and_represented(value_type, unit):
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    artifact['initial_output']['candidates'][0].update(value_type=value_type,unit=unit)
    pins = repin(artifact,capture,config)
    result = build_projection(artifact,capture,config,pins)
    assert result['candidates'] == [dict(id='c1',label='Rated pressure',value_type=value_type,
        unit=unit,domain='technical',observation_ids=['o1'],requirements='unspecified')]
    assert result['semantic_context']['read_only_records'] == []
    assert result['semantic_context']['record_mapping'][1] == dict(
        kind='candidate',id='c1',disposition='represented',reason=None,projected_ids=['c1'])
    assert result['semantic_context']['original_artifact'] == artifact
    assert result['semantic_context']['origin'] == pins


@pytest.mark.parametrize('malformed', ['configuration', 'coverage'])
def test_local_cli_reports_malformed_projection_as_structured_refusal(
        tmp_path, monkeypatch, capsys, malformed):
    import json
    from nilaris.semantic_publication_cli import main
    artifact,capture,configuration,_ = toy()
    if malformed == 'configuration':
        configuration = []
    else:
        capture['documents'][0]['coverage'] = []
    bundle_path = tmp_path / 'bundle.json'
    bundle_path.write_text(json.dumps(dict(artifact=artifact,capture=capture,
        configuration=configuration,expected_pins=repin(artifact,capture,configuration))))
    local_configuration = tmp_path / 'offline.json'
    local_configuration.write_text(json.dumps(dict(database={},token='offline-test-token')))
    local_configuration.chmod(0o600)
    monkeypatch.setattr('sys.argv', ['semantic-publication', '--configuration',str(local_configuration),
        '--bundle',str(bundle_path),'--namespace','toy','--command-id','one','--job-id','job',
        '--expected-revision','0','--expected-base','null','--actor','expert','--explicit'])
    assert main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == dict(known=True,saved=False,approved=False,
                                        category='invalid_semantic_projection')
    assert output.err == ''


def test_unsupported_candidate_remains_visible_without_editable_definition():
    from nilaris.semantic_publication import build_projection, digest
    artifact,capture,config,pins = toy()
    artifact['initial_output']['candidates'][0]['value_type'] = 'boolean'
    pins['artifact'] = digest(artifact)
    result = build_projection(artifact,capture,config,pins)
    assert result['candidates'] == []
    assert result['semantic_context']['read_only_records'] == [dict(kind='candidate',
        record=artifact['initial_output']['candidates'][0],reason='unsupported_value_type')]


def test_logistical_observation_does_not_supply_technical_definition():
    from nilaris.semantic_publication import build_projection, digest
    artifact,capture,config,pins = toy()
    artifact['initial_output']['observations'][0]['domain'] = 'logistical'
    artifact['initial_output']['observations'][0]['original_unit'] = ''
    pins['artifact'] = digest(artifact)
    result = build_projection(artifact,capture,config,pins)
    assert result['observations'][0]['domain'] == 'logistical'
    assert result['observations'][0]['unit'] == ''
    assert result['candidates'] == []
    assert result['semantic_context']['read_only_records'][0]['reason'] == 'unsupported_evidence'


def test_declared_unread_source_cannot_be_promoted_as_complete():
    from nilaris.semantic_publication import build_projection, digest
    artifact,capture,config,pins = toy()
    capture['documents'][0]['coverage']['unread'] = ['page 2']
    pins['capture'] = digest(capture)
    with pytest.raises(ValueError):
        build_projection(artifact,capture,config,pins)


def repin(artifact, capture, config):
    from nilaris.semantic_publication import digest
    return {k:digest(v) for k,v in dict(artifact=artifact,capture=capture,configuration=config).items()}


def test_foreign_observation_and_directed_relation_remain_read_only_context():
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    foreign = deepcopy(capture['documents'][0])
    foreign.update(document_id='context', source_revision='r2', content_pin='b'*64)
    foreign['spans'][0]['text'] = 'Context pressure: 8 bar'
    capture['documents'].append(foreign)
    ref = dict(document_id='context',source_revision='r2',locator_id='L1')
    output = artifact['initial_output']
    observation = dict(output['observations'][0],id='o2',**ref)
    output['observations'].append(observation)
    output['candidates'][0]['evidence_refs'].append(ref)
    association = dict(id='a1',source_refs=[ref],target_ref=deepcopy(output['candidates'][0]['evidence_refs'][0]),
                       relation='supersedes',disposition='ambiguous')
    output['associations'].append(association)
    result = build_projection(artifact,capture,config,repin(artifact,capture,config))
    assert [o['id'] for o in result['observations']] == ['o1']
    assert result['candidates'] == result['associations'] == []
    context = result['semantic_context']
    assert context['read_only_records'] == [
        dict(kind='observation',record=observation,reason='external_context_evidence'),
        dict(kind='candidate',record=output['candidates'][0],reason='external_context_evidence'),
        dict(kind='association',record=association,reason='cross_document_relation')]
    assert context['documents'][1] == foreign
    assert context['record_mapping'][1] == dict(kind='observation',id='o2',disposition='read_only',
                                               reason='external_context_evidence',projected_ids=[])
    assert context['original_artifact'] == artifact


def test_default_publication_read_preserves_provenance_pin_without_dumping_raw_provider():
    from nilaris.semantic_publication import build_projection, publication_view
    artifact,capture,config,_ = toy()
    artifact['raw_response_base64'] = 'private retained bytes' * 6000
    result = build_projection(artifact,capture,config,repin(artifact,capture,config))
    result.update(id='publication-one',job_id='job-one')
    before = deepcopy(result)
    visible = publication_view(result)
    assert 'original_artifact' not in visible['semantic_context']
    assert visible['semantic_context']['original_output'] == artifact['initial_output']
    assert visible['semantic_context']['provenance'] == dict(available=True,operation='result',
        job_id='job-one',proposal_id='publication-one',provenance=True,
        artifact_pin=result['semantic_context']['origin']['artifact'])
    assert result == before
    assert len(canonical(visible)) < 10000


def test_duplicate_content_aliases_and_revisions_share_support_but_distinct_locators_do_not():
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    output = artifact['initial_output']
    alias = deepcopy(capture['documents'][0])
    alias.update(document_id='alias',source_revision='r9',delivery_id='delivery-2')
    alias['spans'].append(dict(locator_id='L2',text='Repeated at another location',locator=dict(text='line 2')))
    capture['documents'].append(alias)
    output['observations'].extend([
        dict(output['observations'][0],id='alias-observation',document_id='alias',source_revision='r9',label='Different label'),
        dict(output['observations'][0],id='distinct-location',document_id='alias',source_revision='r9',locator_id='L2')])
    result = build_projection(artifact,capture,config,repin(artifact,capture,config))
    groups = result['semantic_context']['support_groups']
    assert len(groups) == 2
    assert groups[0]['observation_ids'] == ['o1','alias-observation']
    assert groups[1]['observation_ids'] == ['distinct-location']
    assert groups[0]['source_refs'] == [dict(document_id='toy',source_revision='r1',locator_id='L1'),
                                       dict(document_id='alias',source_revision='r9',locator_id='L1')]
    assert result['observations'][0]['support_id'] == groups[0]['id']
    assert result['semantic_context']['documents'] == capture['documents']
    assert result['semantic_context']['original_output'] == output


def test_provenance_read_pages_exact_artifact_and_rejects_foreign_view_cursor():
    import json
    from nilaris.semantic_publication import build_projection, provenance_page
    artifact,capture,config,_ = toy()
    artifact['raw_response_base64'] = 'original bytes' * 5000
    publication = build_projection(artifact,capture,config,repin(artifact,capture,config))
    publication.update(id='p1',job_id='j1')
    page = provenance_page(publication)
    first_cursor = page['next_cursor']
    fragments = [page['fragment']]
    while page['next_cursor']:
        page = provenance_page(publication,page['next_cursor'])
        fragments.append(page['fragment'])
    assert json.loads(''.join(fragments))['artifact'] == artifact
    assert provenance_page(dict(publication,id='p2'),first_cursor)['category'] == 'invalid_cursor'
    assert provenance_page(dict(publication,job_id='j2'),first_cursor)['category'] == 'invalid_cursor'
    assert provenance_page(publication,'not-a-cursor')['category'] == 'invalid_cursor'


@pytest.mark.parametrize('selector', [dict(provenance='yes',proposal_id='p'),
    dict(provenance=1,proposal_id='p'),dict(provenance=True),dict(proposal_id='p'),
    dict(provenance=True,proposal_id=False)])
def test_result_refuses_invalid_provenance_selector_before_persistence(selector):
    from nilaris.admission import Ingestion
    result = Ingestion({}).result('j1',**selector)
    assert result['category'] == 'invalid_request'


def test_colliding_record_identity_across_kinds_refuses_publication():
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    artifact['initial_output']['candidates'][0]['id'] = 'o1'
    with pytest.raises(ValueError):
        build_projection(artifact,capture,config,repin(artifact,capture,config))


def test_identical_target_deliveries_preserve_inventory_without_multiplying_observations():
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    capture['documents'][0]['delivery_id'] = 'delivery-one'
    duplicate = deepcopy(capture['documents'][0])
    duplicate['delivery_id'] = 'delivery-two'
    capture['documents'].append(duplicate)
    result = build_projection(artifact,capture,config,repin(artifact,capture,config))
    assert len(result['observations']) == len(result['semantic_context']['support_groups']) == 1
    assert result['semantic_context']['documents'] == capture['documents']


@pytest.mark.parametrize('collision', ['revision','pin','span'])
def test_conflicting_delivery_identity_never_changes_original_mapping(collision):
    from nilaris.semantic_publication import build_projection
    artifact,capture,config,_ = toy()
    duplicate = deepcopy(capture['documents'][0])
    if collision == 'revision':
        duplicate['source_revision'] = 'r2'
    elif collision == 'pin':
        duplicate['content_pin'] = 'b'*64
    else:
        duplicate['spans'][0]['text'] = 'Conflicting captured text'
    capture['documents'].append(duplicate)
    before = deepcopy((artifact,capture,config))
    with pytest.raises(ValueError):
        build_projection(artifact,capture,config,repin(artifact,capture,config))
    assert (artifact,capture,config) == before
