"""Finite synthetic inventory for the local n8n P/W/P2 demonstration only."""
import base64
import binascii
import copy
import re

from nilaris.execution import ControlledSource


class SyntheticFixtureDrive:
    """Exact configured metadata and revision bytes; no filesystem/network reads.

    Keeps the existing synthetic-drive-v1 connector so ingestion's immutable
    source-pin controls also govern this multi-object synthetic substitution.
    """
    label = 'synthetic-fixtures-v1'

    def __init__(self, configuration):
        required = {'member_key', 'connector', 'source_id', 'source_revision', 'source_root',
                    'expected_sha256', 'document_id', 'case_id', 'membership_revision',
                    'product_type', 'processing_contract', 'reference_set', 'selection_policy',
                    'intent_id', 'input_bytes', 'media_type', 'identity'}

        def valid(member):
            return (isinstance(member, dict) and set(member) == required and
                    all(isinstance(member[k], str) and member[k].strip() and len(member[k]) <= 256
                        for k in required - {'input_bytes'}) and
                    member['connector'] == 'synthetic-drive-v1' and
                    member['media_type'] in ('application/pdf',
                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') and
                    re.fullmatch('[0-9a-f]{64}', member['expected_sha256']) and
                    type(member['input_bytes']) is int and 0 < member['input_bytes'] <= 8192)

        if (not isinstance(configuration, dict) or
                set(configuration) != {'mode', 'entries', 'aliases'} or
                configuration['mode'] != self.label or
                not isinstance(configuration['entries'], list) or
                not 1 <= len(configuration['entries']) <= 3 or
                not isinstance(configuration['aliases'], list) or len(configuration['aliases']) > 1):
            raise ValueError('bounded synthetic inventory unavailable')
        members, objects = [], {}
        total = 0
        for entry in configuration['entries']:
            if not isinstance(entry, dict) or set(entry) != {'allowed_member', 'object'}:
                raise ValueError('bounded synthetic inventory unavailable')
            member, obj = entry['allowed_member'], entry['object']
            if (not valid(member) or not isinstance(obj, dict) or
                    set(obj) != {'source_id', 'source_revision', 'source_root', 'content_b64'} or
                    any(obj[key] != member[key] for key in ('source_id', 'source_revision', 'source_root'))):
                raise ValueError('bounded synthetic inventory unavailable')
            try:
                content = base64.b64decode(obj['content_b64'], validate=True)
            except (ValueError, TypeError, binascii.Error):
                raise ValueError('bounded synthetic inventory unavailable') from None
            total += len(content)
            key = member['source_id'], member['source_revision']
            if key in objects or total > 8192:
                raise ValueError('bounded synthetic inventory unavailable')
            objects[key] = bytes(content)
            members.append(copy.deepcopy(member))
        for alias in configuration['aliases']:
            if not valid(alias) or not any(
                    {k: v for k, v in alias.items() if k != 'member_key'} ==
                    {k: v for k, v in member.items() if k != 'member_key'} for member in members):
                raise ValueError('bounded synthetic alias unavailable')
            members.append(copy.deepcopy(alias))
        if len({m['member_key'] for m in members}) != len(members):
            raise ValueError('bounded synthetic inventory unavailable')
        self._members, self._objects = members, objects

    def permits(self, member):
        return any(member == allowed for allowed in self._members)

    def controlled_source(self):
        return _InventorySource(self._objects)


class _InventorySource(ControlledSource):
    """Trusted finite bytes keyed by exact ID/revision, including shared IDs."""
    label = 'synthetic-fixture-inventory-source-v1'

    def read(self, source_id, revision, limit):
        content = self._objects.get((source_id, revision))
        if content is None or type(limit) is not int or len(content) > limit:
            return None
        return content
