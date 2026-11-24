// Apply, status inspection and explicit retry use the same private command journal
// as the authenticated expert workspace. An absent receipt alone cannot start a write.
export class StageSave {
  constructor(post, store) {
    this.post = post;
    this.store = store;
    this.phase = 'ready';
  }

  restore(local, pending, tag) {
    this.local = local;
    this.original = local?.command ?? pending;
    this.tag = tag;
    if (local?.rejection && !pending) {
      this.phase = 'rejected';
      this.result = local.rejection;
    } else if (this.original) this.phase = 'unknown';
  }

  async apply(command) {
    if (!['ready', 'saved', 'rejected'].includes(this.phase)) return;
    this.phase = 'saving';
    try {
      this.local = await this.store.save(command, this.local);
      this.original = command;
      await this.submit();
    } catch {
      // A competing tab may already hold the encrypted slot. Reopen to recover
      // its original instead of staging this selection under another identity.
      this.phase = this.local ? 'unknown' : 'reopen';
    }
  }

  async submit() {
    const command = this.local.command;
    const staged = await this.post('prepare', {command});
    this.tag = staged.journal_tag;
    await this.finish(await this.post('command', {command, prepared:true}), true);
  }

  async check() {
    if (this.phase !== 'unknown') return;
    this.phase = 'checking';
    try {
      if (this.local?.rejection) {
        const context = await this.post('context', {});
        if (context.pending && context.pending_tag !== this.local.rejection.journal_tag) {
          this.phase = 'reopen';
          return;
        }
        await this.finish(this.local.rejection, !!context.pending);
        return;
      }
      const result = this.local
        ? await this.post('reconcile', {command:this.local.command})
        : await this.post('resolve', {command_id:this.original.command_id, tag:this.tag});
      this.staged = this.local ? result.journal_pending : true;
      this.tag = result.journal_tag ?? this.tag;
      if (!result.known && result.category === 'unknown_command') this.phase = 'retry';
      else await this.finish(result, this.staged);
    } catch {
      this.phase = 'unknown';
    }
  }

  async retry() {
    if (this.phase !== 'retry') return;
    this.phase = 'saving';
    try {
      if (this.staged) {
        const result = await this.post('retry', {command_id:this.original.command_id, tag:this.tag});
        await this.finish(result, true);
      } else {
        await this.submit();
      }
    } catch {
      this.phase = 'unknown';
    }
  }

  async finish(result, staged) {
    if (result.known !== true) {
      this.phase = 'unknown';
      return;
    }
    if (result.saved !== true && !this.local?.rejection) {
      this.local = await this.store.save(this.original, this.local, result);
    }
    if (staged) {
      await this.post('acknowledge', {command_id:this.original.command_id, tag:result.journal_tag});
    }
    if (result.saved === true && this.local) {
      await this.store.remove(this.local);
      this.local = null;
    }
    this.phase = result.saved === true ? 'saved' : 'rejected';
    this.result = result;
  }
}

export async function postStageRequest(path, body, timeout = 15000, request = fetch) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await request(path, {
      method:'POST', headers:{'Content-Type':'application/json'}, cache:'no-store',
      body:JSON.stringify(body), signal:controller.signal
    });
    if (!response.ok) throw Error(response.status === 403
      ? 'Access refused. Sign in through the customer workspace and reopen the draft.'
      : 'The customer service could not complete the request.');
    return await response.json();
  } finally {
    clearTimeout(deadline);
  }
}

const text = value => typeof value === 'string' && value.length > 0;
const memberships = ['accepted','awaiting','deferred','rejected'];
const invalidRead = 'The customer service could not confirm the current draft.';

function checkDecision(decision) {
  if (decision?.stages === null && decision.decided_by === null) return;
  const stages = decision?.stages;
  if (!Array.isArray(stages) || new Set(stages).size !== stages.length ||
      !stages.every(code => ['customer-rfq','supplier-offer','before-manufacture'].includes(code)))
    throw Error(invalidRead);
  const actor = JSON.parse(decision.decided_by);
  if (!Array.isArray(actor) || actor.length !== 2 || !actor.every(text)) throw Error(invalidRead);
}

export function cellId(attribute, scenario) {
  return JSON.stringify([attribute, scenario]);
}

function checkMatrix({scenarios, attributes, eligible_cells}) {
  for (const axis of [scenarios, attributes]) {
    if (!Array.isArray(axis) || !axis.length || !axis.every(item => text(item?.id) && text(item.label)) ||
        new Set(axis.map(item => item.id)).size !== axis.length) throw Error(invalidRead);
  }
  const eligible = new Set();
  for (const row of attributes) {
    if (!memberships.includes(row.membership)) throw Error(invalidRead);
    for (const column of scenarios) {
      const decision = row.cells[column.id];
      checkDecision(decision);
      if (row.membership === 'accepted' && decision.stages === null)
        eligible.add(cellId(row.id, column.id));
    }
  }
  if (!Array.isArray(eligible_cells)) throw Error(invalidRead);
  for (const pair of eligible_cells) {
    if (!eligible.delete(cellId(pair.attribute_id, pair.scenario_id))) throw Error(invalidRead);
  }
  if (eligible.size) throw Error(invalidRead);
}

export function confirmedStageRecord(record, draftId, minimumRevision) {
  const fields = ['product_type','scenario','attribute','value','passage','scope_note',
    'source_name','location','source_sha256','source_fragment'];
  if (record?.known !== true || record.draft_id !== draftId || !Number.isSafeInteger(record.revision) ||
      !memberships.includes(record.membership) ||
      record.revision < minimumRevision || !fields.every(key => text(record[key])) ||
      !Array.isArray(record.documents) || !record.documents.every(doc => text(doc?.name) && text(doc.sha256)))
    throw Error(invalidRead);
  checkDecision(record);
  if ('attributes' in record || 'scenarios' in record || 'eligible_cells' in record) checkMatrix(record);
  return record;
}

export async function originalPassageURL(source, evidence) {
  const bytes = Uint8Array.from(atob(source.pdf_base64), c => c.charCodeAt(0));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hex = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2,'0')).join('');
  if (hex !== evidence.source_sha256) throw Error('The original offer revision differs.');
  return URL.createObjectURL(new Blob([bytes], {type:'application/pdf'})) + '#' + evidence.source_fragment.replace(/^#/, '');
}

export const membershipLabels = {accepted:'In Schema', awaiting:'Suggested', deferred:'Deferred', rejected:'Excluded'};

export function stageDescription(stages) {
  if (stages === null) return ['Undetermined'];
  if (!stages.length) return ['Optional at all stages'];
  const labels = {'customer-rfq':'Customer RFQ', 'supplier-offer':'Supplier offer',
    'before-manufacture':'After order and before manufacture'};
  return stages.map(code => labels[code] ?? code);
}
