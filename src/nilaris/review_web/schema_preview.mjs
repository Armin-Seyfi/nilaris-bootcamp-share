import {membershipLabels, stageDescription, postStageRequest, originalPassageURL} from '/stage-save.mjs';

const el = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const draft = params.get('draft');
const revision = Number(params.get('revision'));
const selection = {customer_id:params.get('customer'), job_id:params.get('job')};
function node(tag, text) {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
}

function actor(value) {
  try {
    const identity = JSON.parse(value);
    if (Array.isArray(identity)) return identity[1];
  } catch { /* Imported revisions can have a plain-text operator name. */ }
  return value;
}

function decision(cell) {
  const stages = stageDescription(cell?.stages ?? null).join(', ');
  return cell?.decided_by ? stages + ' · ' + actor(cell.decided_by) : stages;
}

function fields(value) {
  const list = document.createElement('dl');
  for (const [key, entry] of Object.entries(value)) {
    list.append(node('dt', key.replaceAll('_',' ')),
      node('dd', typeof entry === 'object' ? JSON.stringify(entry) : entry));
  }
  return list;
}

function command(operation, extra = {}) {
  return postStageRequest('/expert/command', {...selection, command:{operation, draft_id:draft, ...extra}});
}

function drawMatrix(view) {
  const head = el('matrix').createTHead().insertRow();
  for (const title of ['Attribute and membership origin', ...view.scenarios.map(s => s.label)]) head.append(node('th',title));
  const body = el('matrix').createTBody();
  const definitions = new Map(view.definitions.map(def => [def.id, def]));
  for (const attribute of view.attributes) {
    if (attribute.membership !== 'accepted') {
      el('excluded').append(node('li',attribute.label + ' · ' + membershipLabels[attribute.membership]));
      continue;
    }
    const row = body.insertRow();
    const heading = node('th',attribute.label);
    const origin = attribute.membership_origin;
    heading.append(node('small',origin?.actor ? 'Added by ' + actor(origin.actor) : origin?.kind ?? 'Origin unrecorded'));
    const details = document.createElement('details');
    details.append(node('summary','Definition'), fields(definitions.get(attribute.id)));
    if (definitions.get(attribute.id).observation_ids?.some(id => view.observation_ids.includes(id))) {
      const link = node('a','Inspect linked passage');
      link.href = '#evidence';
      details.append(link);
    }
    heading.append(details);
    row.append(heading);
    for (const scenario of view.scenarios) {
      const cell = attribute.cells[scenario.id];
      row.append(node('td',decision(cell)));
      if (cell.stages === null) el('unresolved').append(node('li',attribute.label + ' · ' + scenario.label));
    }
  }
  el('readiness').textContent = view.eligible_cells.length
    ? 'Needs a decision · ' + view.eligible_cells.length + ' unresolved' : 'Ready for final approval';
  if (!el('excluded').children.length) el('excluded').append(node('li','None'));
}

function history(view) {
  for (const item of view.history) {
    const li = document.createElement('li');
    const link = node('a','Revision ' + item.revision);
    const url = new URL(location.href);
    url.searchParams.set('revision',item.revision);
    link.href = url;
    li.append(link,' · ' + actor(item.actor) + ' · ' + item.reason);
    for (const change of item.changes) {
      if (change.scenario) {
        li.append(node('div',change.attribute + ' · ' + change.scenario + ' · ' +
          decision(change.before) + ' → ' + decision(change.after)));
      } else {
        const detail = document.createElement('details');
        detail.append(node('summary',change.category + ' · ' + change.label));
        for (const side of ['before', 'after']) {
          detail.append(node('strong',side === 'before' ? 'Before' : 'After'));
          detail.append(change[side] ? fields(change[side]) : node('span',' Absent'));
        }
        li.append(detail);
      }
    }
    el('history').append(li);
  }
  const button = el('older');
  button.hidden = view.older === null;
  button.onclick = async () => {
    button.disabled = true;
    try { history(await preview(view.older)); }
    catch (error) { el('status').textContent = error.message; }
    finally { button.disabled = false; }
  };
}

async function preview(through) {
  const result = await command('review_schema_preview',{revision, history_through:through});
  if (!result.known || result.draft_id !== draft || result.revision !== revision) throw Error('Saved revision unavailable.');
  return result;
}

async function open() {
  if (!/^\d+$/.test(params.get('revision')) || !Number.isSafeInteger(revision)) throw Error('Invalid revision.');
  const context = await postStageRequest('/expert/context', selection);
  selection.expected_scope = context.scope;
  const view = await preview(revision);
  el('pin').textContent = view.evidence.product_type + ' · Draft ' + draft + ' · Revision ' + revision;
  drawMatrix(view);
  el('associations').textContent = 'Associations (' + view.associations.length + ')';
  for (const association of view.associations) el('relations').append(fields(association));
  history(view);
  const evidence = view.evidence;
  el('document').textContent = evidence.source_name + ' · ' + evidence.location;
  el('passage').textContent = evidence.passage;
  el('digest').textContent = 'SHA-256 ' + evidence.source_sha256;
  let loading = false;
  el('inspect').ontoggle = async () => {
    if (!el('inspect').open || !el('pdf').hidden || loading) return;
    loading = true;
    el('pdf-status').textContent = 'Checking the original…';
    try {
      const result = await command('review_stage_source');
      el('pdf').href = await originalPassageURL(result, evidence);
      el('pdf').hidden = false;
      el('pdf-status').textContent = '';
    } catch { el('pdf-status').textContent = 'Original unavailable. Close and reopen to retry.'; }
    finally { loading = false; }
  };
  el('status').textContent = 'Read-only · ' + actor(context.actor);
  el('preview').hidden = false;
}
open().catch(error => { el('status').textContent = error.message; });
