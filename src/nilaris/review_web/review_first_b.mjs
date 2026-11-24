import {openIntentStore} from '/customer-intent.js';
import {StageSave, membershipLabels, stageDescription, originalPassageURL, postStageRequest, confirmedStageRecord, cellId} from '/stage-save.mjs';

const el = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const selection = {customer_id:params.get('customer'), job_id:params.get('job')};
const boxes = Array.from(el('editor').querySelectorAll('input[value]'));
let record, selected, preview, lastCell;
let context, draft, save, editing = false, refreshing = false, refreshFailed = false;

async function post(route, body) {
  return postStageRequest('/expert/' + route, {...selection, ...body});
}

function command(operation) {
  return post('command', {command:{operation, draft_id:context.draft_id}});
}

function person(actor) {
  return JSON.parse(actor)[1];
}

function showRecord(result) {
  record = result;
  if (record.attributes) {
    selected ??= {attribute_id:record.attributes[0].id, scenario_id:record.scenarios[0].id};
    showSelection();
  } else draft = record;
  renderRecord();
}

function showSelection() {
  const row = record.attributes.find(row => row.id === selected.attribute_id);
  const column = record.scenarios.find(col => col.id === selected.scenario_id);
  draft = {...record, attribute:row.label, scenario:column.label, membership:row.membership,
    membership_origin:row.membership_origin, ...row.cells[column.id]};
  document.querySelector('.source').hidden = row.id !== 'plunger-diameter';
}

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function inspectCell(attribute_id, scenario_id) {
  selected = {attribute_id, scenario_id};
  editing = false;
  showSelection();
  renderRecord();
  el('inspector').hidden = false;
  controls();
}

function drawMatrix() {
  if (!record.attributes) return;
  document.querySelector('aside').hidden = true;
  document.querySelector('.layout').classList.add('matrix-layout');
  el('review').hidden = false;
  const proposals = preview?.eligible_cells ?? [];
  const marks = new Set(proposals.map(pair => cellId(pair.attribute_id, pair.scenario_id)));
  const head = document.createElement('tr');
  head.append(element('th', 'Attribute'));
  for (const scenario of record.scenarios) {
    const column = element('th', scenario.label);
    column.scope = 'col';
    if (preview) column.append(element('small',
      proposals.filter(pair => pair.scenario_id === scenario.id).length + ' proposed'));
    head.append(column);
  }
  el('matrix').tHead.replaceChildren(head);
  const body = el('matrix').tBodies[0];
  body.replaceChildren();
  for (const row of record.attributes) {
    const tr = body.insertRow();
    const label = element('th', row.label);
    label.scope = 'row';
    tr.append(label);
    if (row.membership !== 'accepted') {
      const td = tr.insertCell();
      td.colSpan = record.scenarios.length;
      td.append(element('span', membershipLabels[row.membership]));
      if (!preview) {
        const inspect = element('button', 'Inspect ' + row.label);
        inspect.onclick = () => {
          inspectCell(row.id, record.scenarios[0].id);
          el('selected').focus();
        };
        td.append(inspect);
      }
      if (!preview && row.membership === 'awaiting') {
        const add = element('button', 'Add to Schema');
        add.onclick = () => addAttribute(row.id);
        td.append(add);
      }
      continue;
    }
    for (const scenario of record.scenarios) {
      const id = cellId(row.id, scenario.id);
      const proposed = marks.has(id);
      const text = proposed ? '↳ Proposed · Optional at all stages' : stageDescription(row.cells[scenario.id].stages).join(' · ');
      const node = element(preview ? 'span' : 'button', text);
      if (proposed) node.classList.add('proposed');
      if (!preview) {
        node.id = id;
        node.setAttribute('aria-label', [row.label, scenario.label, text].join(', '));
        node.onfocus = () => { lastCell = id; };
        node.onclick = () => {
          inspectCell(row.id, scenario.id);
          el('edit').click();
        };
      }
      tr.insertCell().append(node);
    }
  }
  const count = record.eligible_cells.length;
  el('summary').textContent = preview && count ? count + ' proposed cells' :
    count ? count + ' undetermined' : 'Stage review complete';
  el('finish').hidden = !!preview || !count;
  el('back').hidden = !preview;
  el('bulk').hidden = !preview || !count;
  el('bulk').textContent = 'Set ' + count + (count === 1 ? ' cell' : ' cells') + ' to optional & save draft';
}

el('finish').onclick = async () => {
  editing = false;
  await refresh();
  if (refreshFailed || !record.eligible_cells.length) return;
  preview = record;
  el('inspector').hidden = true;
  drawMatrix();
  controls();
  el('back').focus();
};
function closePreview() {
  preview = null;
  drawMatrix();
  controls();
  (el(lastCell) ?? el('finish')).focus();
}
el('back').onclick = closePreview;
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && preview) {
    event.preventDefault();
    closePreview();
  }
});
el('bulk').onclick = () => {
  const {draft_id, revision, eligible_cells} = preview;
  preview = null;
  drawMatrix();
  run(() => save.apply({operation:'review_stage_bulk', command_id:crypto.randomUUID(),
    draft_id, expected_revision:revision, cells:eligible_cells}));
};

function renderRecord() {
  drawMatrix();
  el('customer').textContent = selection.customer_id + ' · Reviewer ' + person(context.actor);
  el('product').textContent = draft.product_type;
  el('revision').textContent = 'Saved revision ' + draft.revision;
  const included = draft.membership === 'accepted';
  const membershipLabel = membershipLabels[draft.membership];
  el('membership').textContent = membershipLabel;
  if (included && draft.membership_origin?.kind === 'system')
    el('membership').textContent += ' · Added by system';
  if (included && draft.membership_origin?.kind === 'reviewer')
    el('membership').textContent += ' · Added by ' + person(draft.membership_origin.actor);
  const needsInclusion = draft.membership === 'awaiting';
  const needsStages = included && draft.stages === null;
  let reason = 'No outstanding stage decision.';
  if (needsStages) reason = 'Stage obligations need a decision.';
  if (needsInclusion) reason = 'Review suggested Attribute.';
  el('reason').textContent = reason;
  el('count').textContent = needsInclusion || needsStages ? '(1)' : '(0)';
  el('attribute').textContent = el('selected').textContent = draft.attribute;
  el('rowState').textContent = !included ? membershipLabel : stageDescription(draft.stages).join(' · ');
  el('scenario').textContent = draft.scenario;
  el('value').textContent = draft.value;
  el('scope').textContent = draft.scope_note;
  el('locator').textContent = draft.source_name + ' · ' + draft.location;
  el('passage').textContent = draft.passage;
  el('documents').replaceChildren(...draft.documents.map(doc => {
    const item = document.createElement('li');
    item.textContent = doc.name;
    return item;
  }));
  el('confirmed').replaceChildren(...(included || draft.stages !== null ? stageDescription(draft.stages) : []).map(label => {
    const item = document.createElement('li');
    item.textContent = (draft.stages === null ? '○ ' : '✓ ') + label;
    return item;
  }));
  el('attribution').textContent = draft.decided_by
    ? (included ? 'Recorded by ' : 'Previously recorded by ') + person(draft.decided_by) : '';
}

const messages = {
  ready:'Draft open', saving:'Saving…', checking:'Checking the original command…',
  saved:'Saved', rejected:'Save rejected. Your stage selections are retained.',
  unknown:'Save result unknown. Check the original command before continuing.',
  retry:'No receipt for the original command. You can retry that same save.',
  reopen:'Reopen this draft to recover the current command.'
};
function controls() {
  const conflict = save.phase === 'rejected' && save.result.category === 'revision_conflict';
  const stale = conflict && draft.revision < save.result.current_revision;
  const adding = save.original?.operation === 'review_stage_add';
  const bulkRejected = save.phase === 'rejected' && save.original?.operation === 'review_stage_bulk';
  const status = bulkRejected ? 'Bulk save rejected. Open Finish review for a fresh preview.' : adding && save.phase === 'rejected' ? 'Addition rejected.' : messages[save.phase];
  el('status').textContent = status +
    (conflict ? ' Another revision was saved. Review the current decision before applying your choices.' : '') +
    (refreshFailed ? ' Refresh the current decision to verify its revision.' : '');
  el('refresh').hidden = !refreshFailed;
  el('refresh').disabled = refreshing;
  el('editor').hidden = !editing;
  const busy = refreshing || !['ready','saved','rejected'].includes(save.phase);
  el('finish').disabled = busy || refreshFailed || editing;
  el('matrix').querySelectorAll('button').forEach(button => { button.disabled = busy || refreshFailed; });
  el('add').hidden = draft.membership !== 'awaiting' || busy || refreshFailed || stale;
  el('edit').hidden = draft.membership !== 'accepted' || editing || busy ||
    (refreshFailed && save.phase === 'saved') || (adding && (refreshFailed || stale));
  el('edit').textContent = save.phase === 'rejected' && !adding ? 'Review choices and retry' : 'Edit stage obligations';
  el('check').hidden = save.phase !== 'unknown';
  el('retry').hidden = save.phase !== 'retry';
  el('apply').disabled = refreshing || stale || (!el('optional').checked && !boxes.some(box => box.checked));
}

function focusRow() {
  el('row').setAttribute('aria-current', 'true');
  el('inspector').hidden = false;
  (el(lastCell) ?? (record.attributes ? el('finish') : el('row'))).focus();
}

el('row').onclick = focusRow;
function addAttribute(attribute) {
  run(() => save.apply({operation:'review_stage_add', command_id:crypto.randomUUID(),
    draft_id:context.draft_id, expected_revision:draft.revision, ...(attribute ? {attribute_id:attribute} : {})}));
}
el('add').onclick = () => addAttribute(selected?.attribute_id);
el('edit').onclick = () => {
  const stages = save.phase === 'rejected' && save.original.operation === 'review_stage_save' &&
    (!selected || (selected.attribute_id === save.original.attribute_id && selected.scenario_id === save.original.scenario_id)) ? save.original.stages : draft.stages;
  boxes.forEach(box => { box.checked = stages?.includes(box.value) ?? false; });
  el('optional').checked = Array.isArray(stages) && stages.length === 0;
  editing = true;
  controls();
  boxes[0].focus();
};
el('editor').onchange = ({target}) => {
  if (target === el('optional') && target.checked) boxes.forEach(box => { box.checked = false; });
  else if (boxes.includes(target) && target.checked) el('optional').checked = false;
  controls();
};
el('cancel').onclick = () => { editing = false; controls(); focusRow(); };

async function run(action) {
  editing = false;
  const pending = action();
  controls();
  focusRow();
  await pending;
  refreshFailed = false;
  if (['saved','rejected'].includes(save.phase)) await refresh();
  else controls();
}

async function refresh() {
  refreshing = true;
  controls();
  try {
    const minimum = Math.max(draft.revision, save.result?.revision ?? 0, save.result?.current_revision ?? 0);
    showRecord(confirmedStageRecord(await command('review_stage_read'), context.draft_id, minimum));
    refreshFailed = false;
  } catch {
    refreshFailed = true;
    el('inspector').hidden = false;
  } finally {
    refreshing = false;
    controls();
  }
}
el('refresh').onclick = refresh;
el('editor').onsubmit = event => {
  event.preventDefault();
  if (!editing || el('apply').disabled) return;
  const stages = boxes.filter(box => box.checked).map(box => box.value);
  run(() => save.apply({operation:'review_stage_save', command_id:crypto.randomUUID(),
    draft_id:context.draft_id, expected_revision:draft.revision, ...selected, stages}));
};
el('check').onclick = () => run(() => save.check());
el('retry').onclick = () => run(() => save.retry());

let loadingPdf = false;
el('evidence').ontoggle = async () => {
  if (!el('evidence').open || !el('original').hidden || loadingPdf) return;
  loadingPdf = true;
  el('pdfStatus').textContent = 'Opening the original offer…';
  try {
    const source = await command('review_stage_source');
    el('original').href = await originalPassageURL(source, draft);
    el('original').hidden = false;
    el('pdfStatus').textContent = '';
  } catch {
    el('pdfStatus').textContent = 'Could not open the original offer. Close and reopen Inspect evidence to retry.';
  } finally {
    loadingPdf = false;
  }
};

async function open() {
  context = await post('context', {});
  selection.expected_scope = context.scope;
  const store = await openIntentStore(['Reviewer'], role => post('unlock', {role}));
  save = new StageSave(post, store);
  const local = await store.read();
  if ([local?.command, context.pending].some(command => command && !['review_stage_save','review_stage_add','review_stage_bulk'].includes(command.operation)))
    throw Error('Return to the customer workspace to resolve the pending expert command.');
  save.restore(local, context.pending, context.pending_tag);
  showRecord(confirmedStageRecord(await command('review_stage_read'), context.draft_id, 0));
  controls();
  el('workspace').hidden = false;
  el('loading').hidden = true;
  if (save.phase !== 'ready' || draft.stages !== null) focusRow();
}
open().catch(error => { el('loading').textContent = error.message; });
