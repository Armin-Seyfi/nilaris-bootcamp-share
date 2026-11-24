'use strict';

const query = new URLSearchParams(location.search);
const selection = {customer_id: query.get('customer'), job_id: query.get('job')};
const statusLine = document.getElementById('status');
const preview = document.getElementById('preview');
const build = document.getElementById('build');
const source = document.getElementById('source');
const original = document.getElementById('original');
const mapping = document.getElementById('mapping');
const excerpt = document.getElementById('excerpt');
let scope;
let view = 0;

function clearOriginal() {
  original.hidden = true;
  mapping.textContent = '';
  excerpt.textContent = '';
}

async function request(action) {
  const body = action === 'read' ? selection : {...selection, expected_scope: scope};
  const response = await fetch('/minimized-evidence/' + action, {
    method: 'POST',
    cache: 'no-store',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (response.status === 403) throw new Error('Evidence access refused. Sign in with the current Reviewer role.');
  if (!response.ok) throw new Error('Evidence unavailable. Reload to check the saved result.');
  return response.json();
}

const holdMessages = {
  technical_meaning_at_risk: 'Held · Required technical meaning could not be preserved.',
  sensitivity_unresolved: 'Held · Sensitivity could not be resolved.',
};

async function show(action) {
  const current = ++view;
  clearOriginal();
  preview.hidden = build.hidden = source.hidden = true;
  preview.textContent = '';
  try {
    const result = await request(action);
    if (current !== view) return;
    scope = result.scope;
    if (result.state === 'absent') {
      statusLine.textContent = 'No saved preview for this excerpt.';
      build.hidden = false;
    } else if (result.state === 'held') {
      statusLine.textContent = holdMessages[result.hold_reason] || 'Held';
      source.hidden = false;
    } else if (result.state === 'preview') {
      statusLine.textContent = 'Saved minimized preview · ' + result.contract + ' · ' + result.detector;
      preview.textContent = result.preview_text;
      preview.hidden = source.hidden = false;
    }
  } catch (error) {
    if (current === view) statusLine.textContent = error.message;
  }
}

build.addEventListener('click', () => show('build'));
source.addEventListener('click', async () => {
  const current = view;
  clearOriginal();
  source.disabled = true;
  try {
    const result = await request('source');
    if (current !== view) return;
    mapping.textContent = [
      'Document ' + result.document_id,
      'Revision ' + result.source_revision,
      'Publication ' + result.publication_id,
      'Observation ' + result.observation_id,
      'Locator ' + JSON.stringify(result.locator),
      ...(result.selected_blocks || []).map(block => {
        if (!block.id) return block.heading + ' · source lines ' + block.lines.join('–');
        const span = Number.isInteger(block.start) ? ' · characters ' + (block.start + 1) + '–' + block.end : '';
        return 'Source block ' + block.id + span;
      }),
      result.contract + ' · ' + result.detector,
    ].join('\n');
    excerpt.textContent = result.excerpt;
    original.hidden = false;
  } catch (error) {
    if (current !== view) return;
    preview.textContent = '';
    preview.hidden = source.hidden = true;
    statusLine.textContent = error.message;
  } finally {
    if (current === view) source.disabled = false;
  }
});
window.addEventListener('pagehide', () => {
  view++;
  source.disabled = false;
  clearOriginal();
  preview.textContent = '';
  preview.hidden = source.hidden = build.hidden = true;
});
window.addEventListener('pageshow', event => {
  if (event.persisted) show('read');
});
show('read');
