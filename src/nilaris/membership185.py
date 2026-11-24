"""Customer-scoped membership commands, ordered on the existing customer row."""
import hashlib
import json
import math
import secrets
import time
import uuid

from psycopg.types.json import Jsonb


BUNDLES = {'Administrator', 'Reviewer', 'Approver'}


def install(c):
    c.execute("""ALTER TABLE onboarding182_memberships
        ADD COLUMN IF NOT EXISTS roles text[] NOT NULL DEFAULT ARRAY['Administrator'],
        ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true,
        ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 0;
        ALTER TABLE onboarding182_invitations
        ADD COLUMN IF NOT EXISTS roles text[] NOT NULL DEFAULT ARRAY['Administrator'],
        ADD COLUMN IF NOT EXISTS administrator_binding text,
        ADD COLUMN IF NOT EXISTS recipient_revision bigint;
        ALTER TABLE provision181_operations
        ADD COLUMN IF NOT EXISTS roster_revision bigint NOT NULL DEFAULT 0""")
    primary = c.execute("""SELECT cardinality(conkey) AS columns FROM pg_constraint
        WHERE conrelid='onboarding182_memberships'::regclass AND contype='p'""").fetchone()
    if primary['columns'] == 1:
        c.execute("""ALTER TABLE onboarding182_memberships DROP CONSTRAINT onboarding182_memberships_pkey,
            ADD PRIMARY KEY (customer_id,issuer,subject)""")
    c.execute("""CREATE TABLE IF NOT EXISTS membership185_commands (
        customer_id text NOT NULL, issuer text NOT NULL, subject text NOT NULL,
        command_id text NOT NULL, kind text NOT NULL, target text NOT NULL,
        body_hash text NOT NULL, result jsonb NOT NULL,
        PRIMARY KEY(customer_id,issuer,subject,command_id))""")


def member(c, customer, issuer, subject, binding):
    return c.execute("""SELECT * FROM onboarding182_memberships WHERE customer_id=%s
        AND issuer=%s AND subject=%s AND recipient_binding=%s""",
        (customer, issuer, subject, binding)).fetchone()


def require_admin(c, customer, issuer, subject, binding):
    row = member(c, customer, issuer, subject, binding)
    if not row or not row['active'] or 'Administrator' not in row['roles']:
        raise PermissionError('active customer Administrator required')


def roster(c, customer):
    revision = c.execute('SELECT roster_revision FROM provision181_operations WHERE customer_id=%s',
                         (customer,)).fetchone()['roster_revision']
    users = c.execute("""SELECT invitation_id AS membership_id,issuer,subject,roles,active
        FROM onboarding182_memberships WHERE customer_id=%s ORDER BY issuer,subject""", (customer,)).fetchall()
    invites = c.execute("""SELECT invitation_id, recipient_issuer, recipient_subject, roles, expires_at, state
        FROM onboarding182_invitations WHERE customer_id=%s AND state='pending'
        AND expires_at>extract(epoch FROM clock_timestamp()) ORDER BY issued_at""", (customer,)).fetchall()
    return dict(state='available', revision=revision, users=users, invitations=invites)


def permitted_roles(roles):
    return (isinstance(roles, list) and bool(roles)
            and all(isinstance(role, str) and role in BUNDLES for role in roles)
            and len(set(roles)) == len(roles))


def validate(command):
    fields = {'invite': {'roles','recipient_issuer','recipient_subject','recipient_binding','expires_at'},
              'roles': {'target','roles'}, 'deactivate': {'target'}, 'revoke': {'target'}}
    if not isinstance(command, dict) or not isinstance(command.get('kind'), str):
        raise ValueError('invalid membership command')
    kind = command['kind']
    if kind not in fields or set(command) != fields[kind] | {'kind'}:
        raise ValueError('invalid membership command')
    if 'roles' in command and not permitted_roles(command['roles']):
        raise ValueError('select permitted roles')
    for field in fields[kind] - {'roles','expires_at'}:
        if not isinstance(command[field], str) or not 0 < len(command[field]) <= 200:
            raise ValueError('invalid target')
    if kind == 'invite':
        expiry = command['expires_at']
        if type(expiry) not in (int,float) or not math.isfinite(expiry):
            raise ValueError('invalid expiry')


def execute(service, c, actor, customer, key, revision, command, *, inspect_only):
    validate(command)
    if type(revision) is not int or revision < 0:
        raise ValueError('expected roster revision required')
    kind = command['kind']
    target = (json.dumps([command['recipient_issuer'], command['recipient_subject']])
              if kind == 'invite' else command['target'])
    digest = hashlib.sha256(json.dumps([revision,command], sort_keys=True).encode()).hexdigest()
    identity = (customer, actor.issuer, actor.subject, key)
    prior = c.execute("""SELECT body_hash,result FROM membership185_commands
        WHERE customer_id=%s AND issuer=%s AND subject=%s AND command_id=%s""", identity).fetchone()
    if prior:
        if prior['body_hash'] != digest:
            raise ValueError('original command required')
        result = prior['result']
        if kind == 'invite' and 'invitation_id' in result:
            row = c.execute('''SELECT state,delivery,expires_at FROM onboarding182_invitations WHERE customer_id=%s AND invitation_id=%s''',
                            (customer,result['invitation_id'])).fetchone()
            result.update(state=row['state'], delivery=row['delivery'])
            if row['state'] == 'pending' and row['expires_at'] <= time.time():
                result['state'] = 'expired'
        return result
    if inspect_only:
        return dict(state='absent')
    operation = c.execute("""SELECT roster_revision,operation_id FROM provision181_operations
        WHERE customer_id=%s""",(customer,)).fetchone()
    if revision != operation['roster_revision']:
        result = dict(state='refused',reason='stale roster revision')
    elif kind == 'invite':
        result = issue(service, c, actor, customer, operation['operation_id'], command)
    else:
        result = change(c, actor, customer, command)
    if result['state'] != 'refused':
        result['reason'] = 'explicit customer Administrator ' + {
            'invite': 'invitation', 'roles': 'role assignment', 'deactivate': 'deactivation',
            'revoke': 'invitation revocation'}[kind]
        c.execute('UPDATE provision181_operations SET roster_revision=roster_revision+1 WHERE customer_id=%s',(customer,))
    c.execute('INSERT INTO membership185_commands VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
              (*identity, kind, target, digest, Jsonb(result)))
    return result


def issue(service, c, actor, customer, operation, command):
    issuer, subject, binding = (command[k] for k in ('recipient_issuer','recipient_subject','recipient_binding'))
    now = time.time()
    if not now < command['expires_at'] <= now + service.validity_ceiling:
        return dict(state='refused', reason='invitation expiry outside configured period')
    if service.identities.staff_subject(issuer, subject):
        return dict(state='refused', reason='customer recipient required')
    if (actor.issuer,actor.subject) == (issuer,subject):
        return dict(state='refused', reason='another Administrator must grant personal roles')
    pending = c.execute("""SELECT 1 FROM onboarding182_invitations WHERE customer_id=%s
        AND recipient_issuer=%s AND recipient_subject=%s AND state='pending'
        AND expires_at>extract(epoch FROM clock_timestamp())""", (customer, issuer, subject)).fetchone()
    if pending:
        return dict(state='refused', reason='reconcile and revoke the pending invitation first')
    existing = c.execute("""SELECT active, recipient_binding, revision FROM onboarding182_memberships
        WHERE customer_id=%s AND issuer=%s AND subject=%s""", (customer, issuer, subject)).fetchone()
    if existing and (not existing['active'] or existing['recipient_binding'] != binding):
        return dict(state='refused', reason='active matching recipient required')
    invitation, reference, secret = uuid.uuid4().hex, uuid.uuid4().hex, secrets.token_urlsafe(32)
    c.execute("""INSERT INTO onboarding182_invitations
        (invitation_id,command_id,issuer,subject,customer_id,provision_operation,
         recipient_issuer,recipient_subject,recipient_binding,expires_at,issued_at,verifier,
         delivery_reference,delivery,state,roles,administrator_binding,recipient_revision)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'unknown','pending',%s,%s,%s)""",
        (invitation, invitation, actor.issuer, actor.subject, customer, operation, issuer, subject, binding,
         command['expires_at'], now, hashlib.sha256(secret.encode()).hexdigest(), reference,
         command['roles'], actor.recipient, existing['revision'] if existing else None))
    result = dict(state='pending', delivery='unknown', invitation_id=invitation, delivery_reference=reference)
    service.defer_delivery(c, actor, customer, secret, result)
    return result


def change(c, actor, customer, command):
    kind, target = command['kind'], command['target']
    if kind == 'revoke':
        row = c.execute("""UPDATE onboarding182_invitations SET state='revoked'
            WHERE customer_id=%s AND invitation_id=%s AND administrator_binding IS NOT NULL
            AND state='pending' AND expires_at>extract(epoch FROM clock_timestamp())
            RETURNING invitation_id""", (customer,target)).fetchone()
        if row:
            return dict(state='revoked', invitation_id=target)
        return dict(state='refused', reason='pending invitation required')
    row = c.execute("""SELECT issuer,subject,roles FROM onboarding182_memberships
        WHERE customer_id=%s AND invitation_id=%s AND active""",(customer,target)).fetchone()
    if not row:
        return dict(state='refused', reason='active membership required')
    roles = command.get('roles', [])
    if (actor.issuer,actor.subject) == (row['issuer'],row['subject']):
        if (set(roles) - set(row['roles'])) & {'Reviewer','Approver'}:
            return dict(state='refused', reason='another Administrator must grant personal roles')
    if 'Administrator' in row['roles'] and 'Administrator' not in roles:
        other = c.execute("""SELECT 1 FROM onboarding182_memberships WHERE customer_id=%s
            AND active AND 'Administrator'=ANY(roles) AND invitation_id<>%s""",(customer,target)).fetchone()
        if not other:
            return dict(state='refused', reason='last active Administrator required')
    c.execute("""UPDATE onboarding182_memberships SET active=%s, roles=%s, revision=revision+1
        WHERE customer_id=%s AND invitation_id=%s""",(kind!='deactivate',roles,customer,target))
    return dict(state='deactivated' if kind=='deactivate' else 'updated', membership_id=target, roles=roles)
