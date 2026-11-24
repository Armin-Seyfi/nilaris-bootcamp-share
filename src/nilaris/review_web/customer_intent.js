// The private server journal takes over once prepare arrives. This encrypted copy
// protects the preceding interval, including a request that never reached the server.
const encode = value => new TextEncoder().encode(value);
const roleFor = operation => operation === 'review_approve' ? 'Approver' : 'Reviewer';

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('nilaris-expert-intent', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('commands');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transaction(db, mode, action) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction('commands', mode, {durability:'strict'});
    const request = action(tx.objectStore('commands'));
    tx.oncomplete = () => resolve(request.result);
    tx.onabort = () => reject(tx.error || new Error('Local review journal unavailable.'));
  });
}

async function importKey(hex, usage) {
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error('Private review key unavailable.');
  const bytes = Uint8Array.from(hex.match(/../g), pair => Number.parseInt(pair,16));
  return crypto.subtle.importKey('raw', bytes, 'AES-GCM', false, [usage]);
}

export function openIntentStore(roles, unlock) {
  return openCommandStore(roles.filter(r => ['Reviewer','Approver'].includes(r)),unlock,command => roleFor(command.operation));
}

export async function openCommandStore(permissions, unlock, permissionFor) {
  const db = await openDatabase();
  const access = new Map();
  let scope;
  for (const role of permissions) {
    const authorized = await unlock(role);
    if (scope && scope !== authorized.scope) throw new Error('Review identity changed. Reopen the proposal.');
    scope = authorized.scope;
    access.set(role, {slot:authorized.slot, key:await importKey(authorized.key,'encrypt')});
  }
  if (!scope) throw new Error('Expert permission required.');

  function cipher(slot, iv) {
    return {name:'AES-GCM', iv, additionalData:encode(slot)};
  }
  return {
    async save(command, previous = null, rejection = null) {
      const {slot,key} = access.get(permissionFor(command));
      if (!representableReview(command)) throw new Error('A review field contains a null character or incomplete Unicode character. Correct it before saving.');
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const bytes = await crypto.subtle.encrypt(cipher(slot,iv),key,encode(JSON.stringify(rejection ? {rejectedCommand:command, rejection} : command)));
      const packet = {slot, iv, bytes};
      // add (never put) serializes competing tabs even before either reaches the server.
      try {
        await transaction(db,'readwrite',store => {
          if (!previous) return store.add(packet,scope);
          const read = store.get(scope);
          read.onsuccess = () => {
            const stored = read.result;
            if (stored && stored.iv.length === previous.iv.length &&
                stored.iv.every((byte,i) => byte === previous.iv[i])) store.put(packet,scope);
            else store.transaction.abort();
          };
          return read;
        });
      }
      catch { throw new Error('Could not reserve the private local journal. Inspect the pending command before submitting again.'); }
      return {command, iv, rejection};
    },
    async read() {
      const packet = await transaction(db,'readonly',store => store.get(scope));
      if (!packet) return null;
      const permit = [...access].find(([,value]) => value.slot === packet.slot);
      if (!permit) return null;
      // Fetch the current permission again before every unseal, including retries.
      const fresh = await unlock(permit[0]);
      if (fresh.scope !== scope || fresh.slot !== packet.slot) throw new Error('Review identity changed.');
      const key = await importKey(fresh.key,'decrypt');
      const plain = await crypto.subtle.decrypt(cipher(packet.slot,packet.iv),key,packet.bytes);
      const decoded = JSON.parse(new TextDecoder().decode(plain));
      return decoded.rejectedCommand
        ? {command:decoded.rejectedCommand, rejection:decoded.rejection, iv:packet.iv}
        : {command:decoded, iv:packet.iv};
    },
    async remove(original) {
      await transaction(db,'readwrite',store => {
        const request = store.get(scope);
        request.onsuccess = () => {
          const stored = request.result;
          if (stored && stored.iv.length === original.iv.length &&
              stored.iv.every((byte,i) => byte === original.iv[i])) store.delete(scope);
        };
        return request;
      });
    }
  };
}

export function fitsReviewEnvelopes(scope, command, limit) {
  // The submit payload contains every byte used by prepare and reconcile, plus the prepared flag.
  return encode(JSON.stringify({...scope, command, prepared:true})).byteLength <= limit;
}

export function representableReview(value) {
  if (typeof value === 'string') return !/[\u0000\ud800-\udfff]/u.test(value);
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(representableReview);
  if (value && typeof value === 'object')
    return Object.entries(value).every(([key,item]) => representableReview(key) && representableReview(item));
  return true;
}

export async function commandFingerprint(command) {
  // Match exact JSONB decimals, UTF-16 key ordering, JSON types and array order.
  function typed(value) {
    if (Array.isArray(value)) return ['array',value.map(typed)];
    if (value && typeof value === 'object') {
      return ['object',Object.keys(value).sort().map(key => [key,typed(value[key])])];
    }
    if (typeof value !== 'number') return value;
    if (value === 0) return ['number','0'];
    const [mantissa,exp='0']=Math.abs(value).toString().split('e');
    const [integer,fraction='']=mantissa.split('.');
    const digits=(integer+fraction).replace(/^0+/,'');
    const significant=digits.replace(/0+$/,'');
    const exponent=Number(exp)-fraction.length+digits.length-significant.length;
    return ['number',(value<0 ? '-' : '')+significant+'e'+exponent];
  }
  const ascii=JSON.stringify(typed(command)).replace(/[\u007f-\uffff]/g,
    c => '\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
  const hash=await crypto.subtle.digest('SHA-256',encode(ascii));
  return Array.from(new Uint8Array(hash), n => n.toString(16).padStart(2,'0')).join('');
}
