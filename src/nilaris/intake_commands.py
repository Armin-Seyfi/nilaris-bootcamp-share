"""Private recovery for one original admission or disclosure policy command per authority."""
from dataclasses import dataclass
import secrets

from nilaris import customer_review
from nilaris.onboarding182 import is_command_key
from nilaris.scoped_intake import SOURCE_KEYS, validate_policy


@dataclass(frozen=True)
class IntakeJournal:
    customer: str
    binding: str
    kind: str
    part: str

    @property
    def suffix(self):
        return '-intake-' + customer_review.digest([self.customer,self.binding,self.kind,self.part])


def validate(kind, command):
    fields = {'command_id','source_key','policy_revision'} if kind == 'admit' else {'command_id','expected_revision','policy'}
    if not isinstance(command, dict) or set(command) != fields or not is_command_key(command.get('command_id')):
        raise ValueError('exact intake command required')
    if kind == 'admit':
        if (command['source_key'] not in SOURCE_KEYS or type(command['policy_revision']) is not int
                or command['policy_revision'] < 0):
            raise ValueError('unknown synthetic source')
    else:
        if type(command['expected_revision']) is not int or command['expected_revision'] < 0:
            raise ValueError('invalid policy revision')
        validate_policy(command['policy'])


def original_outcome(intake, customer, kind, command):
    if kind == 'admit':
        result = intake.admission_status(customer,command['command_id'])
        matches = (result.get('source_key') == command['source_key'] and
                   result.get('policy_revision') == command['policy_revision'])
    else:
        result = intake.policy_status(customer,command['command_id'])
        matches = (result.get('revision') == command['expected_revision'] + 1 and
                   customer_review.same_command(result.get('policy'), command['policy']))
    if result.get('state') == 'absent':
        return None
    return result if matches else dict(state='refused', reason='Command ID already belongs to another original.')


def handle(path, body, intake, customer, principal, journal, locks):
    kind = path if path in ('admit','set-policy') else body.get('kind')
    if kind not in ('admit','set-policy') or body.get('kind') != kind:
        raise ValueError('intake command kind required')
    binding, policy_revision = intake.intent_authority(customer, kind)
    channel = IntakeJournal(customer,binding,kind,'original')
    keys = IntakeJournal(customer,binding,kind,'browser-key')
    outcome_channel = IntakeJournal(customer,binding,kind,'outcome')
    locks.enter_context(journal.locked(principal, channel))
    key = journal.read(principal,channel=keys)
    if path == 'unlock':
        if set(body) != {'kind'}:
            raise ValueError('invalid key request')
        if key is None:
            key = {'key':secrets.token_hex(32)}
            journal.save(principal,key,channel=keys)
        slot = customer_review.digest(['intake',key['key']])
        return dict(**key,slot=slot,scope=slot,policy_revision=policy_revision)
    if not key or body.get('slot') != customer_review.digest(['intake',key['key']]):
        raise PermissionError('original customer and command authority required')
    fields = set(body)-{'kind','slot'}
    saved = journal.read(principal,channel=channel)
    if path in ('admit','set-policy','ack'):
        if fields != {'tag'} or not saved or body['tag'] != customer_review.journal_tag(saved):
            raise ValueError('inspect the original command first')
        command = saved
    else:
        if path == 'retry':
            fields.discard('tag')
        if fields not in ({'command'}, set()) or (path == 'prepare' and fields != {'command'}):
            raise ValueError('invalid recovery request')
        command = body.get('command',saved)
        if command is None:
            if path != 'pending':
                raise ValueError('original command required')
            return dict(state='empty')
        validate(kind,command)
        if saved and not customer_review.same_command(saved,command):
            return dict(state='occupied',reason='Inspect the pending original before submitting a different command.')
    tag = customer_review.journal_tag(command)
    if path == 'retry' and body.get('tag') != tag:
        raise ValueError('inspect the exact original before retry')
    if path == 'prepare' or (path == 'retry' and not saved):
        if not saved:
            # A crash after unlinking an acknowledged original may leave its outcome.
            if journal.exists(principal,channel=outcome_channel):
                journal.mark_known(principal,channel=outcome_channel)
                journal.acknowledge(principal,channel=outcome_channel)
            journal.save(principal,command,channel=channel)
        if path == 'prepare':
            return dict(state='pending' if saved else 'prepared',tag=tag)
        saved = command
    recorded = journal.read(principal,channel=outcome_channel) if saved else None
    result = recorded['outcome'] if recorded and recorded['tag']==tag else original_outcome(intake,customer,kind,command)
    if path == 'pending':
        return dict(state='known' if result is not None else 'absent',command=command,tag=tag,
                    staged=bool(saved),outcome=result)
    if path == 'ack':
        if result is None:
            raise PermissionError('resolve the original outcome first')
        journal.mark_known(principal,channel=channel)
        journal.acknowledge(principal,channel=channel)
        if recorded:
            journal.mark_known(principal,channel=outcome_channel)
            journal.acknowledge(principal,channel=outcome_channel)
        return dict(state='acknowledged')
    if path not in ('retry','admit','set-policy'):
        raise ValueError('invalid intake action')
    if result is None:
        try:
            if kind == 'admit':
                result = intake.admit(customer,command['command_id'],command['source_key'],
                                      expected_policy_revision=command['policy_revision'])
            else:
                result = intake.set_policy(customer,**command)
        except ValueError:
            result = dict(state='refused',reason='Original command refused. Inspect the current policy or job before changing it.')
        if result.get('state') == 'refused':
            journal.save(principal,dict(tag=tag,outcome=result),channel=outcome_channel)
    return dict(result,intent_tag=tag)
