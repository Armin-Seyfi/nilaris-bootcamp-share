"""Shared typed stage/publication contract for controlled work and future parsers.

Results carry source observations separately from proposed definitions. Persisted
publications are immutable snapshots; expert review has no write path here.
"""
from dataclasses import dataclass
import json

CONTRACT = 'proposal-set-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True)
class Effect:
    kind: str
    ordinal: int
    payload: dict


@dataclass(frozen=True)
class StageOutput:
    outcome: str
    coverage: dict
    effects: tuple[Effect, ...]
    stage_contract: str = 'fixture-v1'
    adapter: str = 'controlled-test-stage-v1'

    def snapshot(self):
        if self.outcome not in ('completed', 'partial') or self.stage_contract != 'fixture-v1':
            raise ValueError('unsupported stage outcome or contract')
        if not isinstance(self.adapter, str) or not self.adapter.strip():
            raise ValueError('labeled adapter required')
        if (not isinstance(self.coverage, dict) or
                any(not isinstance(self.coverage.get(key), list) for key in ('inspected', 'unread', 'limits')) or
                any(not isinstance(item, str) or not item.strip()
                    for key in ('inspected', 'unread', 'limits') for item in self.coverage[key]) or
                (self.outcome == 'completed' and self.coverage['unread'])):
            raise ValueError('declared inspected/unread coverage and limits required')
        keys = set()
        identities = set()
        effects = []
        for effect in self.effects:
            if (not isinstance(effect, Effect) or effect.kind not in ('observation', 'association', 'candidate') or
                    type(effect.ordinal) is not int or not 0 <= effect.ordinal <= 2147483647 or
                    (effect.kind, effect.ordinal) in keys or not isinstance(effect.payload, dict)):
                raise ValueError('unique typed effect key required')
            keys.add((effect.kind, effect.ordinal))
            payload = effect.payload
            if (not isinstance(payload.get('id'), str) or not payload['id'].strip() or
                    payload['id'] in identities):
                raise ValueError('stable output identity required')
            identities.add(payload['id'])
            required = {
                'observation': ('locator', 'kind', 'original_content', 'value', 'unit', 'source_type', 'presence'),
                'association': ('observation_ids', 'method', 'rationale', 'disposition'),
                'candidate': ('label', 'value_type', 'unit', 'domain', 'observation_ids', 'requirements'),
            }[effect.kind]
            if any(key not in payload for key in required):
                raise ValueError('incomplete typed output')
            if effect.kind in ('observation', 'candidate') and payload['unit'] is not None and not isinstance(payload['unit'], str):
                raise ValueError('unit descriptor must be text or explicitly absent')
            if effect.kind == 'observation':
                locator = payload['locator']
                if (not isinstance(locator, dict) or not locator or
                        any(not isinstance(key, str) for key in locator) or
                        any(type(locator[key]) is not int or locator[key] <= 0
                            for key in ('page', 'row', 'column') if key in locator) or
                        any(not isinstance(locator[key], str) or not locator[key].strip()
                            for key in ('sheet', 'cell', 'range', 'text') if key in locator) or
                        any(not isinstance(payload[key], str) or not payload[key].strip()
                            for key in ('kind', 'source_type')) or
                        (payload['value'] is not None and not isinstance(payload['value'], str)) or
                        payload['presence'] not in ('observed', 'blank', 'missing_marker', 'absent_in_inspected_scope', 'unread', 'ambiguous')):
                    raise ValueError('typed source location, value and explicit presence required')
            if effect.kind != 'observation' and (not isinstance(payload['observation_ids'], list) or
                    not payload['observation_ids'] or
                    any(not isinstance(item, str) or not item.strip() for item in payload['observation_ids']) or
                    len(set(payload['observation_ids'])) != len(payload['observation_ids'])):
                raise ValueError('observation links required')
            if effect.kind == 'association' and any(not isinstance(payload[key], str) or not payload[key].strip()
                                                    for key in ('method', 'rationale', 'disposition')):
                raise ValueError('textual association operands required')
            if effect.kind == 'candidate' and (not isinstance(payload['label'], str) or not payload['label'].strip() or
                    payload['requirements'] != 'unspecified' or
                    payload['value_type'] not in ('decimal', 'text', 'unresolved') or
                    payload['domain'] not in ('technical', 'logistical', 'other', 'unresolved')):
                raise ValueError('fixture candidate types and unspecified requirements required')
            effects.append(dict(kind=effect.kind, ordinal=effect.ordinal, payload=payload))
        snapshot = dict(contract=CONTRACT, stage_contract=self.stage_contract, adapter=self.adapter,
                        outcome=self.outcome, coverage=self.coverage, effects=effects)
        encoded = canonical(snapshot)
        if len(encoded) > 2 * 1024**2:
            raise ValueError('serialized output limit')
        return json.loads(encoded)
