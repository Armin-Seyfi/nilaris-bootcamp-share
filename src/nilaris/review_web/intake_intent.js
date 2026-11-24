import {openCommandStore} from './customer-intent.js';

// A scope includes the verified principal, customer, permission and source binding.
export async function openIntakeCommands(kind, expectedSlot, send) {
  let authority;
  async function unlock() {
    authority = await send('unlock',{kind});
    if(authority.slot !== expectedSlot) throw Error('Customer authority changed. Reopen intake.');
    return authority;
  }
  const store = await openCommandStore([kind],unlock,()=>kind);
  const selector = {kind,slot:expectedSlot};
  async function inspect() {
    const local = await store.read();
    const result = await send('pending',{...selector,...(local ? {command:local.command} : {})});
    if(result.state==='occupied') return {local:null,result:await send('pending',selector)};
    return {local,result};
  }
  async function acknowledge(local,tag,staged) {
    if(local) await store.remove(local);
    if(staged) await send('ack',{...selector,tag});
  }
  return {
    async submit(fields) {
      const key = /^[A-Za-z0-9_-]{1,100}$/;
      if(!key.test(fields.command_id) || (kind==='set-policy' && !key.test(fields.policy.evidence_ref)))
        throw Error('Enter a command key and any required evidence reference using letters, digits, underscores or hyphens.');
      const before = await inspect();
      if(before.result.state !== 'empty') throw Error('Inspect the private pending command first.');
      const command = kind==='admit' ? {...fields,policy_revision:authority.policy_revision} : fields;
      const local = await store.save(command);
      const prepared = await send('prepare',{...selector,command});
      if(prepared.state!=='prepared') throw Error('Inspect the private pending command first.');
      const result = await send(kind,{...selector,tag:prepared.tag});
      if(result.intent_tag!==prepared.tag) throw Error('Inspect the original command to resolve its outcome.');
      await acknowledge(local,prepared.tag,true);
      return result;
    },
    async inspect() { return (await inspect()).result; },
    async retry(tag) {
      const {local,result} = await inspect();
      if(result.state!=='absent' || result.tag!==tag) throw Error('Inspect the original outcome again.');
      const outcome = await send('retry',{...selector,command:result.command,tag});
      if(outcome.intent_tag!==tag) throw Error('Inspect the original command to resolve its outcome.');
      await acknowledge(local,tag,true);
      return outcome;
    },
    async acknowledge(tag) {
      const {local,result} = await inspect();
      if(result.state!=='known' || result.tag!==tag) throw Error('Inspect the original outcome again.');
      await acknowledge(local,tag,result.staged);
      return {state:'acknowledged'};
    }
  };
}
