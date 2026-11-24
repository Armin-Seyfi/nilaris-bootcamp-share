"""Versioned expert-review context data and scenario-reference validation."""
from copy import deepcopy


_REGISTRY = dict(version='review-scenarios-v1', scenarios=[
    dict(id='new_project', label='New project', meaning='Specify equipment for a new project without assuming an existing equipment specification as its baseline.'),
    dict(id='exact_repeat', label='Exact repeat', meaning='Repeat a nominated equipment specification; this is not duplicate-document or duplicate-RFQ identity.'),
    dict(id='equivalent_replacement', label='Equivalent replacement', meaning='Consider replacement equipment that may differ from the original while meeting the stated need; this label does not establish engineering equivalence.'),
])
# Retain recognized snapshots here when a later registry revision is introduced.
_RECOGNIZED = {_REGISTRY['version']: _REGISTRY}


def available_registry():
    """Return current read-only catalogue data, without configuring a draft."""
    return deepcopy(_REGISTRY)


def validation_errors(scenario, definitions):
    """Validate a complete snapshot and references; never infer selections."""
    errors = []

    def error(field, reason):
        if len(errors) < 64:
            errors.append(dict(field='scenario_review' + field, reason=reason))

    if not isinstance(scenario, dict):
        error('', 'Scenario review object required')
        return errors
    if set(scenario) != {'version', 'registry', 'references'}:
        error('', 'Declare exactly version, registry and references')
    if scenario.get('version') != 'scenario-review-v1':
        error('.version', 'Use recognized scenario-review-v1 contract')
    registry = scenario.get('registry')
    version = registry.get('version') if isinstance(registry, dict) else None
    recognized = _RECOGNIZED.get(version) if isinstance(version, str) else None
    if recognized is None or registry != recognized:
        error('.registry', 'Use an exact recognized scenario registry snapshot')
    scenario_ids = {row['id'] for row in recognized['scenarios']} if recognized else set()
    references = scenario.get('references')
    if not isinstance(references, list):
        error('.references', 'Scenario references must be a list')
        return errors
    accepted = {d['id'] for d in definitions
                if isinstance(d, dict) and isinstance(d.get('id'), str) and d.get('decision') == 'accepted'}
    seen = set()
    for index, reference in enumerate(references):
        path = f'.references[{index}]'
        if not isinstance(reference, dict):
            error(path, 'Scenario reference object required')
            continue
        if set(reference) != {'attribute_id', 'scenario_id', 'requirement', 'condition'}:
            error(path, 'Declare exactly attribute_id, scenario_id, requirement and condition')
        attribute = reference.get('attribute_id')
        context = reference.get('scenario_id')
        if not isinstance(attribute, str) or attribute not in accepted:
            error(path + '.attribute_id', 'Reference an accepted Attribute definition')
        if not isinstance(context, str) or context not in scenario_ids:
            error(path + '.scenario_id', 'Reference a scenario in the recognized registry snapshot')
        if isinstance(attribute, str) and isinstance(context, str):
            pair = (attribute, context)
            if pair in seen:
                error(path, 'Use only one reference for each Attribute and scenario pair')
            seen.add(pair)
        requirement = reference.get('requirement')
        condition = reference.get('condition')
        if requirement not in ('required', 'conditional'):
            error(path + '.requirement', 'Choose required or conditional')
        elif requirement == 'required' and condition != '':
            error(path + '.condition', 'Required references must have empty condition text')
        elif requirement == 'conditional' and (not isinstance(condition, str) or not condition.strip()):
            error(path + '.condition', 'Conditional references require nonempty plain text')
    return errors
