'use strict';
const byId = id => document.getElementById(id);
async function api(route, data) {
  const options = data === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  };
  const result = await fetch(route, {...options, cache: 'no-store'});
  if (!result.ok) {
    const messages = {401:'Open a session with the current Reviewer key.',
      404:'This item is unavailable in this local bundle.',
      409:'The draft changed or requires a saved Alias decision. Reload and review it before approving.',
      503:'Version storage is unavailable. Reopen the draft and check its status before retrying.'};
    throw new Error(messages[result.status] || 'The local Reviewer could not complete this request.');
  }
  return result.json();
}
let currentRun;
function runApi(route, data) {
  return api('/rfq/runs/' + currentRun + '/' + route, data);
}
function showError(error) { byId('status').textContent = error.message; }
function displayLocation(data) {
  const item = data.note ? 'Note ' + data.note : 'Row ' + data.row;
  byId('locator').textContent = [data.filename, 'Page ' + data.page, data.section, item].join(' · ');
  if (data.note) byId('pdf').textContent = 'Open original PDF at note ↗';
  byId('pdf').href = data.pdf_url;
}
async function displayEvidence() {
  const data = await runApi('evidence');
  displayLocation(data);
  byId('passage').textContent = data.passage;
  byId('comparison-row').textContent = data.qualifier_comparison;
  byId('source').href = data.source_url;
}
async function displayInspection() {
  const capture = await runApi('inspection');
  const {body_utf8, ...envelope} = capture.request;
  byId('captured-digest').textContent = capture.request.body_sha256;
  byId('test-response').textContent = JSON.stringify(savedDraft.initial_record, null, 2);
  byId('envelope').textContent = JSON.stringify(envelope, null, 2);
  byId('body').textContent = body_utf8;
  byId('comparison').textContent = JSON.stringify(capture.comparison, null, 2);
  byId('transport').textContent = JSON.stringify(capture.transport, null, 2);
}
let savedDraft;
function displayDraft(draft) {
  savedDraft = draft;
  const record = draft.initial_record;
  byId('run-id').textContent = 'Run ' + record.run_id;
  byId('initial-name').textContent = record.response.name;
  byId('selected-label').textContent = record.response.selected_label + ' · Row ' + record.source_row;
  byId('verdict').textContent = record.verdict === 'pass' ? 'Passed' : 'Failed · ' + record.verdict;
  byId('name').value = draft.name;
  byId('origin').textContent = draft.provenance;
}
function displayAlias(row) {
  const {initial, decision} = row.alias;
  const {table, note} = initial.evidence;
  byId('alias-initial').textContent = initial.attribute + ' and “' + initial.expression + '” are possible aliases.';
  byId('row-heading').textContent = table.filename + ' · Page ' + table.page + ' · Row ' + table.row;
  byId('note-heading').textContent = note.filename + ' · Page ' + note.page + ' · Note ' + note.note;
  byId('row-passage').textContent = table.passage;
  byId('note-context').textContent = 'Referenced by ' + note.context;
  byId('note-passage').textContent = note.passage;
  byId('alias-row-link').href = table.source_url;
  byId('alias-note-link').href = note.source_url;
  byId('alias-name').value = row.attribute;
  byId('alias-decision').value = decision || '';
  byId('alias-review').hidden = false;
}
let reviewedDraft;
let savedAlias;
const versionURL = id => '/?version=' + encodeURIComponent(id);
function showHistoryPage(view, items, cursor, list, button, render) {
  byId(list).append(...view[items].map(render));
  const more = byId(button);
  more.hidden = !view[cursor];
  more.disabled = false;
  more.onclick = async () => {
    more.disabled = true;
    try {
      showHistoryPage(await api(view[cursor]), items, cursor, list, button, render);
    } catch (error) {
      more.disabled = false;
      showError(error);
    }
  };
}

async function displaySchema() {
  const id = new URLSearchParams(window.location.search).get('version');
  const view = await api(id === null ? '/rfq/schema/review' : '/rfq/schema/versions/' + encodeURIComponent(id));
  if (id === null && view.approved_version) {
    window.location.replace(versionURL(view.approved_version));
    return;
  }
  const frozen = id !== null;
  reviewedDraft = (frozen || !view.draft_id) ? null : {draft_id:view.draft_id, revision:view.revision};
  const rows = view.rows;
  const unresolved = rows.filter(row => row.unresolved_reason).length;
  const aliasRow = rows.find(row => row.alias);
  if (aliasRow) {
    displayAlias(aliasRow);
    savedAlias = {name:aliasRow.attribute, decision:aliasRow.alias.decision};
  }
  byId('definition-section').hidden = !frozen && !reviewedDraft;
  byId('definition-heading').textContent = frozen ? 'Approved Schema definitions' : 'Schema definitions for approval';
  const included = view.definitions.filter(item => item.decision === 'accepted');
  const proposed = view.definitions.filter(item => item.decision === 'accepted' || item.decision === 'awaiting');
  byId('definitions').replaceChildren(...proposed.map(definition => {
    const entry = document.createElement('tr');
    const origin = view.definition_origins[definition.id];
    for (const value of [definition.label, definition.value_type, definition.unit ?? 'None', origin]) {
      entry.insertCell().textContent = value;
    }
    const sources = entry.insertCell();
    for (const url of definition.observation_ids) {
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = url === aliasRow?.alias.initial.evidence.note.source_url ? 'Original note' : 'Original row';
      sources.append(link, document.createElement('br'));
    }
    return entry;
  }));
  if (frozen) {
    byId('schema-title').textContent = 'Approved pump Schema · Version ' + view.version;
    byId('version-id').hidden = false;
    byId('version-id').textContent = 'Version ID ' + view.id + ' · Approved draft revision ' + view.revision;
    for (const input of byId('alias-review').elements) input.disabled = true;
    byId('alias-review').querySelector('button').hidden = true;
    byId('fixtures').hidden = true;
  }
  byId('approval').hidden = !reviewedDraft || !aliasRow;
  byId('approve').disabled = !aliasRow?.alias.decision;
  byId('approval-scope').textContent = 'Revision ' + view.revision + ' includes ' +
    included.length + ' definitions selected for approval. Approval retains the Alias decision, corrections and evidence. ' +
    unresolved + ' unresolved rows remain outside the approved definitions.';
  byId('corrections').replaceChildren();
  showHistoryPage(view, 'corrections', 'older_corrections', 'corrections', 'older-corrections', change => {
    const li = document.createElement('li');
    const values = item => item.name + ' (' + (item.decision || 'undecided') + ')';
    li.textContent = 'Revision ' + change.revision + ' · ' + change.actor + ' · Row ' +
      change.row + ' · ' + values(change.before) + ' → ' + values(change.after);
    return li;
  });
  byId('history').hidden = !view.corrections.length && !view.older_corrections;
  if (!frozen) {
    byId('version-links').replaceChildren();
    showHistoryPage(view, 'versions', 'older_versions', 'version-links', 'older-versions', version => {
      const li = document.createElement('li');
      const link = document.createElement('a');
      link.href = versionURL(version.id);
      link.textContent = 'Version ' + version.version + ' · ' + version.id;
      li.append(link);
      return li;
    });
    byId('versions').hidden = !view.versions.length;
  }
  const excludedRows = rows.filter(row => ['deferred', 'rejected'].includes(row.definition_decision)).length;
  byId('coverage').textContent = `${rows.length} numbered rows in PUMP TECHNICAL DATA on page 1. ` +
    `${rows.length - unresolved - excludedRows} ${frozen ? 'approved' : 'proposed'}, ${unresolved} unresolved. ` +
    (excludedRows ? excludedRows + ' rows excluded from the Schema. ' : '') +
    (aliasRow ? 'Row 5 also carries note 7 from the second PDF. ' : 'Other sections and notes are outside this draft. ') +
    'A dash marks an empty source cell. Unassigned fields require review.';
  byId('schema-rows').replaceChildren(...rows.map(row => {
    const entry = document.createElement('tr');
    const outcomes = {deferred:'Deferred', rejected:'Rejected', accepted:frozen ? 'Approved' : 'Proposed'};
    const outcome = row.unresolved_reason ? 'Unresolved · ' + row.unresolved_reason : outcomes[row.definition_decision] || 'Proposed';
    for (const value of [row.row, row.attribute, row.value, row.unit, outcome]) {
      entry.insertCell().textContent = value === null ? 'Unassigned' : value === '' ? '—' : value;
    }
    const link = document.createElement('a');
    link.href = row.source_url;
    link.textContent = 'View row ' + row.row;
    link.target = '_blank';
    link.rel = 'noopener';
    const evidence = entry.insertCell();
    evidence.append(link);
    if (row.alias) {
      const note = document.createElement('a');
      note.href = row.alias.initial.evidence.note.source_url;
      note.textContent = 'View note 7';
      note.target = '_blank';
      note.rel = 'noopener';
      evidence.append(document.createElement('br'), note);
    }
    return entry;
  }));
}
async function openFixtures() {
  const choices = await api('/rfq/runs');
  const selected = new URLSearchParams(window.location.search).get('run');
  currentRun = selected || choices[0].run_id;
  if (!choices.some(choice => choice.run_id === currentRun)) {
    throw new Error('This local run has expired. Open the Reviewer’s root URL for this session.');
  }
  const chooser = byId('run-choice');
  chooser.replaceChildren(...choices.map(choice => new Option(choice.fixture, choice.run_id)));
  chooser.value = currentRun;
  displayDraft(await runApi('proposal'));
  byId('evidence').open = false;
  byId('inspection').open = false;
}

let loadedFixtures;
function loadFixtures() {
  loadedFixtures ??= openFixtures().catch(error => {
    loadedFixtures = null;
    throw error;
  });
  return loadedFixtures;
}

async function openWorkspace() {
  await displaySchema();
  if (new URLSearchParams(window.location.search).has('run')) {
    await loadFixtures();
    byId('fixtures').open = true;
  }
  byId('workspace').hidden = false;
  byId('login').hidden = true;
}
if (byId('source-view')) {
  api('location.json').then(data => {
    displayLocation(data);
    byId('file').textContent = data.filename;
    byId('original-row-text').textContent = data.passage;
    byId('revision').textContent = 'SHA-256 ' + data.sha256;
    const image = document.querySelector('figure img');
    const centerRow = () => byId('evidence-row').scrollIntoView({block:'center'});
    if (image.complete) centerRow();
    else image.addEventListener('load', centerRow, {once:true});
  }).catch(showError);
} else {
  byId('alias-review').addEventListener('input', () => {
    byId('approve').disabled = !savedAlias?.decision ||
      savedAlias.name !== byId('alias-name').value.trim() || savedAlias.decision !== byId('alias-decision').value;
  });
  byId('approval').addEventListener('submit', async event => {
    event.preventDefault();
    if (!reviewedDraft) return;
    try {
      const receipt = await api('/rfq/schema/approve', {...reviewedDraft, explicit:true});
      window.location.assign(versionURL(receipt.approved_version));
    } catch (error) { showError(error); }
  });
  byId('alias-review').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api('/rfq/schema/alias', {...reviewedDraft, name:byId('alias-name').value, decision:byId('alias-decision').value});
      await displaySchema();
      byId('status').textContent = reviewedDraft ? 'Alias review saved to the draft.' : 'Alias review saved for this session.';
    } catch (error) { showError(error); }
  });
  byId('fixtures').addEventListener('toggle', () => {
    if (byId('fixtures').open) loadFixtures().catch(showError);
  });
  byId('run-choice').addEventListener('change', event => {
    window.location.assign('/?run=' + encodeURIComponent(event.target.value));
  });
  byId('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api('/rfq/session', {token:byId('token').value});
      byId('token').value = '';
      byId('status').textContent = '';
      await openWorkspace();
    } catch (error) { showError(error); }
  });
  byId('name').addEventListener('input', () => {
    const name = byId('name').value.trim();
    let provenance = 'The Reviewer is editing this Attribute name.';
    if (name === savedDraft.name) provenance = savedDraft.provenance;
    else if (name === savedDraft.initial_name) provenance = savedDraft.initial_provenance;
    byId('origin').textContent = provenance;
  });
  byId('editor').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const draft = await runApi('proposal', {name:byId('name').value});
      displayDraft(draft);
      byId('status').textContent = 'Saved for this local session.';
    } catch (error) { showError(error); }
  });
  for (const [id, load] of [['evidence', displayEvidence], ['inspection', displayInspection]]) {
    byId(id).addEventListener('toggle', () => {
      if (byId(id).open) load().catch(showError);
    });
  }
  openWorkspace().catch(showError);
}
