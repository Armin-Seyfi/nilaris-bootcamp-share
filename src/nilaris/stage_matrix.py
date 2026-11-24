"""Project saved stage obligations without relabelling historical Case_001 decisions."""
from nilaris.rfq_stage_decision import DIAMETER_ID


def matrix(record):
    payload = record['payload']
    scenarios = payload.get('scenarios', [dict(id='case001', label=record['observation']['scenario'])])
    attributes = []
    eligible = []
    for definition in payload['definitions']:
        attribute = definition['id']
        cells = {}
        for scenario in scenarios:
            key = scenario['id']
            decision = payload.get('stage_cells', {}).get(attribute, {}).get(key)
            if decision is None and attribute == DIAMETER_ID and key == 'case001':
                decision = payload.get('stage_decision')
            cells[key] = decision or dict(stages=None, decided_by=None)
            if definition['decision'] == 'accepted' and cells[key]['stages'] is None:
                eligible.append(dict(attribute_id=attribute, scenario_id=key))
        attributes.append(dict(id=attribute, label=definition['label'], membership=definition['decision'],
                               membership_origin=definition.get('membership_origin'), cells=cells))
    return dict(scenarios=scenarios, attributes=attributes, eligible_cells=eligible)


def exact_proposal(proposed, eligible):
    """Require each eligible pair exactly once, in any order."""
    if not eligible or not isinstance(proposed, list) or len(proposed) != len(eligible):
        return False
    keys = {'attribute_id', 'scenario_id'}
    if any(not isinstance(pair, dict) or pair.keys() != keys or
           not all(isinstance(value, str) for value in pair.values()) for pair in proposed):
        return False
    def identities(pairs):
        return {(pair['attribute_id'], pair['scenario_id']) for pair in pairs}
    return identities(proposed) == identities(eligible)


def record_decision(payload, attribute, scenario, stages, actor):
    decision = dict(stages=stages, decided_by=actor)
    if 'scenarios' not in payload and attribute == DIAMETER_ID and scenario == 'case001':
        payload['stage_decision'] = decision
    else:
        payload.setdefault('stage_cells', {}).setdefault(attribute, {})[scenario] = decision


def stage_changes(previous, saved, observation):
    """Name changed membership and stage decisions, including changes in attribution."""
    if previous is None:
        return []
    old = matrix(dict(payload=previous, observation=observation))
    new = matrix(dict(payload=saved, observation=observation))
    changes = []

    def differences(category, old_items, new_items):
        before = {item['id']: item for item in old_items}
        after = {item['id']: item for item in new_items}
        for key in dict.fromkeys([*after, *before]):
            if before.get(key) != after.get(key):
                item = after.get(key) or before[key]
                changes.append(dict(category=category, label=item.get('label', key),
                                    before=before.get(key), after=after.get(key)))

    differences('Definition', previous['definitions'], saved['definitions'])
    differences('Association', previous['associations'], saved['associations'])
    differences('Scenario', old['scenarios'], new['scenarios'])

    def decisions(view):
        return {(row['id'], scenario['id']): (row['label'], scenario['label'], row['cells'][scenario['id']])
                for row in view['attributes'] for scenario in view['scenarios']
                if row['cells'][scenario['id']]['stages'] is not None}

    before, after = decisions(old), decisions(new)
    for key in dict.fromkeys([*after, *before]):
        attribute, scenario, _ = after.get(key) or before[key]
        old_cell = before[key][2] if key in before else None
        new_cell = after[key][2] if key in after else None
        if old_cell != new_cell:
            changes.append(dict(attribute=attribute, scenario=scenario, before=old_cell, after=new_cell))
    return changes
