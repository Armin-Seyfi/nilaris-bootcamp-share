'use strict';
const $ = id => document.getElementById(id);
const state = {context:null, publication:null, view:null, selected:0, list:'selected', dirty:false, pending:null, approval:null, busy:false, backendErrors:[], approvedReads:0, matrixSearch:'', matrixSort:'name', matrixScenario:'', matrixRequirement:'all', equipmentOpen:false, categoryLabel:'', authRequired:false, retryReady:false};
let commandFailure='', recoveredCommand=false, retainedCommand=false;
const clone = value => JSON.parse(JSON.stringify(value));
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const text = (id, value) => { $(id).textContent = value; };
// The customer entry point uses only the onboarding cookie and a server-side private journal.
const customerMode = typeof location !== 'undefined' && location.pathname === '/expert';
function customerScope() {
  const query = new URLSearchParams(location.search);
  return {customer_id:query.get('customer'), job_id:query.get('job')};
}
const permitted = role => !customerMode || Boolean(state.context?.roles.includes(role));
const namespace = () => state.context.namespace;
const pendingKey = () => 'nilaris-review-pending:' + state.context.job_id;
const locked = () => !permitted('Reviewer') || !state.view || Boolean(state.view.approved_schema) || Boolean(state.pending) || state.busy || state.authRequired;
function error(message) {
  commandFailure=message;renderCommandState();
  text('errorMessage', message); $('error').hidden = false;
  $('error').scrollIntoView({block:'center'});
  if (state.authRequired) {
    $('login').scrollIntoView({block:'center'});
    $('token').focus({preventScroll:true});
  }
}
function clearError() { commandFailure=''; recoveredCommand=false; $('error').hidden = true; text('errorMessage',''); text('errorTechnical',''); }
function notice(message) { text('notice',message); }
async function api(path, body, exactBody) {
  const options = {credentials:'same-origin', cache:'no-store'};
  if (body !== undefined) Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:exactBody ?? JSON.stringify(body)});
  let response, value;
  try { response = await fetch(path,options); }
  catch { throw new Error('Response unavailable. Preserve pending command and resolve its outcome before another mutation.'); }
  if (customerMode && (response.status === 403 || response.status === 401)) {
    state.authRequired=true;state.dirty=false;state.pending=null;
    location.replace('/?review=reauthenticate');
    throw new Error('Review access refused. Sign in again to inspect your permitted proposals.');
  }
  if (response.status === 401) {
    state.authRequired=true;state.retryReady=false;state.approval=null;
    $('approvalDialog').close();$('manualDialog').close();$('login').hidden=false;
    $('token').focus();
    throw new Error('Session unavailable. Sign in again in this same tab; retain the pending journal and resolve its exact key before an explicit retry.');
  }
  try { value = await response.json(); }
  catch { throw new Error('Response unavailable. Preserve pending command and resolve its outcome before another mutation.'); }
  if (!response.ok) throw new Error(value.reason || value.category || 'Local request refused');
  return value;
}
function editorScope(path) {
  const selection=customerScope();
  if (path !== 'context') selection.expected_scope=state.context.scope;
  return selection;
}
function customerRequest(path, fields = {}) {
  return api('/expert/' + path, {...editorScope(path), ...fields});
}
function customerCommand(body) {
  const {namespace, actor, ...operation} = body;
  return operation;
}
function command(body, exactBody) {
  if (!customerMode) return api('/api/command', body, exactBody);
  return customerRequest('command', {command:customerCommand(body)});
}
let vault, browserIntent, fitsReviewEnvelopes, commandFingerprint, representableReview, serverIntent, serverTag;
let originalSource='local', discardReason='';
function fitsLimit(command) {
  return fitsReviewEnvelopes(editorScope('command'), command, state.context.request_limit_bytes);
}
function selectOriginal(source) {
  discardReason='';
  originalSource=source;
  setPending(source === 'server' ? serverIntent : browserIntent?.command);
  notice(source === 'server' ? 'Server original selected.' : 'Original from this browser profile selected.');
}
async function refreshServerIntent() {
  const current=await customerRequest('context', {expected_scope:state.context.scope});
  serverIntent=current.pending;serverTag=current.pending_tag;
}
function serverSelector(id) {
  return {command_id:id, ...(serverTag ? {tag:serverTag} : {})};
}
function localMatches(pending) {
  return originalSource === 'local' && browserIntent && pending?.operation === browserIntent.command.operation &&
    pending.command_id === browserIntent.command.command_id;
}
async function freshOriginal(pending) {
  const saved=await vault.read();
  if (!saved || saved.command.command_id !== pending.command_id ||
      saved.command.operation !== pending.operation || !browserIntent ||
      !saved.iv.every((byte,i) => byte === browserIntent.iv[i])) {
    throw new Error('The private local command changed. Reopen the proposal to inspect its current state.');
  }
  return saved.command;
}
async function clearKnown(result) {
  if (customerMode) {
    if (result.journal_pending !== false) {
      await customerRequest('acknowledge', {command_id:state.pending.command_id,tag:result.journal_tag});
      serverIntent=null;serverTag=null;
    }
    if (result.journal_pending === false) {serverIntent=null;serverTag=null;}
    if (localMatches(state.pending)) {
      await vault.remove(browserIntent);
      browserIntent=null;
    }
  }
  discardReason='';
  if (browserIntent) selectOriginal('local');
  else if (serverIntent) selectOriginal('server');
  else setPending(null);
}
function serviceFailure(value) {
  return ['transport_unavailable','persistence_unavailable'].includes(value.category) ? 'Local backend unavailable. Wait until the local service is ready.' : '';
}
async function readWhole(body) {
  let value = await command(body);
  if (value.encoding !== 'json-ascii') {
    if (!value.known) throw new Error(serviceFailure(value) || value.category || 'Read outcome unknown');
    return value;
  }
  let encoded = '', page = 0;
  while (true) {
    if (value.encoding !== 'json-ascii' || value.offset !== encoded.length || typeof value.fragment !== 'string') throw new Error('Invalid result page sequence; no editable snapshot loaded.');
    encoded += value.fragment;
    if (encoded.length > 192000) throw new Error('Result exceeds local reader bound; no editable snapshot loaded.');
    if (value.next_cursor == null) break;
    if (++page >= 8 || typeof value.next_cursor !== 'string' || !value.next_cursor) throw new Error('Incomplete result: page bound reached; no editable snapshot loaded.');
    value = await command({...body,cursor:value.next_cursor});
  }
  const result = JSON.parse(encoded);
  if (!result.known) throw new Error(serviceFailure(result) || result.category || 'Read outcome unknown');
  return result;
}
function setPending(body) {
  // This journal contains exact synthetic review commands, never credentials.
  if (!customerMode) {
    if (body) sessionStorage.setItem(pendingKey(),JSON.stringify(body));
    else sessionStorage.removeItem(pendingKey());
  }
  state.pending = body;state.retryReady=false;
  controls();
}
function outcome(value) {
  if (value.known === false) throw new Error((serviceFailure(value) ? serviceFailure(value)+' ' : '')+'Command outcome unknown. Resolve the exact pending key; do not create another command.');
  if (!value.saved) {
    const errors=(value.errors || []).map(e => {
      if (e.field.startsWith('scenario_review')) {
        const match=/^scenario_review\.references\[(\d+)\](?:\.(\w+))?$/.exec(e.field);
        const ref=match && state.view?.scenario_review?.references[Number(match[1])];
        return scenarioIssue(ref,e.reason,match?.[2]);
      }
      const definition=/^definitions\[(\d+)\]/.exec(e.field), association=/^associations\[(\d+)\]/.exec(e.field);
      const name=definition ? definitions()[Number(definition[1])]?.label : association ? 'Source link ' + (Number(association[1])+1) : 'Review';
      const suffix=e.field.split('.').pop();
      const field={label:'editLabel',unit:'editUnit',value_type:'editType',decision:'editDecision'}[suffix];
      return {label:(name || 'Review') + ' · ' + e.field + ': ' + e.reason,kind:definition ? 'definition' : association ? 'association' : null,index:definition ? Number(definition[1]) : association ? Number(association[1]) : null,field};
    });
    state.backendErrors=errors; renderReviewStatus();
    text('errorTechnical',JSON.stringify(value,null,2));
    throw new Error(value.category === 'validation_error' ? 'The command was not recorded. Review the validation items and technical refusal details, correct the fields, then retry.' : 'The review command was refused: ' + (value.category || 'unknown refusal').replaceAll('_',' ') + '. No approval was assumed.');
  }
  return value;
}
async function mutate(body) {
  if (state.pending) throw new Error('Resolve the pending command before another mutation.');
  let value;
  if (customerMode) {
    const original=customerCommand(body);
    if (!fitsLimit(original)) throw new Error('This edit exceeds the UTF-8 request limit. Shorten it before saving. Your edits remain here.');
    originalSource='local';
    browserIntent=await vault.save(original);
    setPending(original);
    let staged;
    try { staged=await customerRequest('prepare', {command:original}); }
    catch(e) {
      throw new Error('Mutation was not submitted. The exact command is protected in this browser profile. Reconnect and resolve it. '+e.message);
    }
    if (staged.state !== 'prepared') throw new Error('Original command already pending. Resolve it before any deliberate retry.');
    value=await customerRequest('command', {command:original, prepared:true});
    if (value.saved) await loadDraft(value.draft_id,null,value);
  } else {
    setPending(clone(body));
    value=await command(body);
  }
  if (value.known !== false) await clearKnown(value);
  return outcome(value);
}
function actor() {
  if (customerMode) return state.context.actor;
  const value = $('actor').value.trim();
  if (!value) throw new Error('Enter your expert name for the audit record.');
  return value;
}
function mutationId(kind) { return kind + ':' + crypto.randomUUID(); }
function markDirty() { state.dirty = true; state.approval = null; state.backendErrors=[]; if(state.view?.scenario_review) notice(state.view.scenario_review.references.length ? 'Scenarios configured with '+state.view.scenario_review.references.length+' references. Save to record these selections.' : 'Scenarios configured with no selections. Save to record this explicit configuration.'); controls(); renderReviewStatus(); }
function renderSessionState() {
  const deadline=state.context?.ui_deadline_unix_ms;
  const remaining=Number.isFinite(deadline) ? Math.max(0,Math.ceil((deadline-Date.now())/1000)) : null;
  const message=remaining === null ? 'UI deadline unknown. Guard/backend deadline unknown.' : remaining === 0 ? 'UI deadline reached. Any pending outcome remains unknown until resolved; guard/backend lifetime is unknown.' : (remaining <= 120 ? 'Session warning · ' : '')+remaining+' seconds until the known UI deadline. Guard/backend deadline unknown; this timer does not prove availability.';
  text('sessionState',message);text('approvalSessionState',message);
}
function renderCommandState() {
  renderSessionState();
  const pending=Boolean(state.pending);
  const message=state.busy ? 'Working… wait for confirmation. Any pending command is retained.' : pending ? (retainedCommand ? 'Retained command · outcome unknown. ' : 'Outcome unknown. ')+(commandFailure ? commandFailure+' ' : '')+'Resolve the exact pending command before continuing; Retry exact command uses the same key and body.' : commandFailure ? 'Command/read failed or refused. '+commandFailure : recoveredCommand ? 'Recovered: exact pending command confirmed and saved draft read back.' : 'Ready · '+(state.view?.approved_schema ? 'Approved version is locked.' : 'Save records a draft; approval requires explicit confirmation.');
  text('commandState',message);text('approvalCommandState',message);
  $('commandStatus').setAttribute('data-state',state.busy ? 'working' : pending ? 'unknown' : commandFailure ? 'failure' : recoveredCommand ? 'recovered' : 'ready');
}
function controls() {
  renderCommandState();
  $('readSemanticArtifact').disabled=!permitted('Reviewer') || state.busy || state.authRequired || !semanticContext()?.provenance?.available;
  const hasDraft = Boolean(state.view), approved = Boolean(state.view?.approved_schema), pending = Boolean(state.pending);
  $('createDraft').hidden = hasDraft;
  $('createDraft').disabled = !permitted('Reviewer') || pending || state.busy || state.authRequired;
  $('saveDraft').disabled = !permitted('Reviewer') || !hasDraft || approved || pending || !state.dirty || state.busy || state.authRequired;
  $('reloadDraft').disabled = !hasDraft || pending || state.busy || state.authRequired;
  $('reloadDraft').textContent = state.dirty ? 'Discard edits & reload…' : 'Reload saved';
  $('reviewApproval').disabled = !permitted('Approver') || !hasDraft || approved || pending || state.dirty || state.busy || state.authRequired;
  const both = customerMode && Boolean(serverIntent && browserIntent);
  $('chooseServerIntent').hidden=!both || originalSource === 'server';
  $('chooseLocalIntent').hidden=!both || originalSource === 'local';
  $('discardLocalIntent').hidden=!discardReason;
  $('discardLocalIntent').textContent=discardReason === 'server_refusal' ? 'Acknowledge refused server original' :
    discardReason === 'matching' ? 'Acknowledge matching local receipt' : 'Discard confirmed local intent and correct edits';
  for (const id of ['chooseLocalIntent','chooseServerIntent','discardLocalIntent']) $(id).disabled=state.busy || state.authRequired;
  $('resolvePending').hidden = !pending;
  $('retryPending').hidden = !pending;
  $('resolvePending').disabled = !pending || state.busy || state.authRequired;
  $('retryPending').disabled = !pending || state.busy || state.authRequired || !state.retryReady;
  $('readApproved').disabled = !approved || pending || state.busy || state.authRequired;
  $('confirmApproval').disabled = !$('explicitApproval').checked || !state.approval || pending || state.dirty || state.busy || state.authRequired;
  document.querySelectorAll('#inspector input, #inspector select, #inspector textarea, #inspector button, #definitions [data-cell], #associations select').forEach(control => {
    control.disabled = locked() || (control.id === 'editUnit' && definitions()[state.selected]?.value_type === 'text');
  });
  $('configureScenarios').disabled = locked();
  $('configureEquipment').disabled = locked();
  $('equipmentEditor').querySelectorAll('input, textarea, button').forEach(control => {control.disabled=locked();});
  for(const id of ['addManual','manualLabel','manualType','manualUnit','confirmManual','cancelManual']) $(id).disabled=locked() || (id === 'manualUnit' && $('manualType').value === 'text');
  $('actor').disabled = customerMode || approved || pending || state.busy || state.authRequired;
  $('reason').disabled = !permitted('Reviewer') || approved || pending || state.busy || state.authRequired;
  $('explicitApproval').disabled = !permitted('Approver') || approved || pending || state.busy || state.authRequired;
  text('saveState', state.busy ? 'Working… wait for confirmation before reloading' : pending ? 'Outcome unknown — resolve before continuing' : approved ? 'Approved version · locked' : state.dirty ? 'Unsaved changes — saved revision ' + state.view.revision + ' is unchanged' : hasDraft ? 'Saved revision ' + state.view.revision : 'No draft yet — start a draft to review');
  text('revisionState', approved ? 'Approved v' + state.view.approved_schema.version : hasDraft ? 'Draft r' + state.view.revision + ' · unapproved' : 'Published · unapproved');
}
function definitions() {
  return state.view?.definitions || state.publication.candidates.filter(c => c.domain === 'technical');
}
function associationRecords() { return (state.view?.associations || state.publication.associations).map(a => a || {}); }
function refs(row, key) {
  return Array.isArray(row?.[key]) ? row[key].filter(id => typeof id === 'string') : [];
}
function choices(options, selected) { return options.map(([value,label]) => `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(label)}</option>`).join(''); }
const attributeChoices = [['awaiting','Not decided'],['accepted','Include attribute'],['deferred','Leave for later'],['rejected','Exclude attribute']];
const associationChoices = [['awaiting_expert_decision','Not reviewed'],['unresolved','Unresolved match'],['supported','Supported by source'],['accepted','Accepted by expert'],['deferred','Leave link for later'],['rejected','Exclude link']];
const labelFor = (options,value) => options.find(row => row[0] === value)?.[1] || String(value || 'Not decided');
const detail = (label,value) => `<details class="technical-details"><summary>${esc(label)}</summary><pre>${esc(JSON.stringify(value,null,2))}</pre></details>`;
const semanticContext = () => state.publication?.semantic_context;
const publicationOrigin = () => semanticContext() ? 'Semantic experiment · immutable proposal projection. Expert review is separate from the original interpretation.' : 'Synthetic fixture review. These proposals come from a narrow test layout, not a general engineering interpretation.';
const isManual = d => d.definition_kind === 'human_manual_v1';
const manualEvidence = 'Human-added · no documentary evidence';
const typeLabel = d => d.value_type === 'decimal' ? 'Number' : d.value_type === 'text' ? 'Text' : d.value_type;
function locatorLabel(locator) {
  if (locator?.text) return locator.text;
  if (locator?.page != null) return 'Page ' + locator.page;
  const sheet = locator?.sheet || locator?.worksheet;
  const cell = locator?.cell || locator?.range;
  return [sheet && 'Sheet ' + sheet,cell].filter(Boolean).join(' · ') || 'Location recorded in technical details';
}
function documentLabel(observation = {}) {
  const context=semanticContext()?.target || state.publication.context, ref=observation.source_ref || observation;
  return 'Document ' + (ref.document_id || context.document_id) + ' · revision ' + (ref.source_revision || context.source_revision);
}
function associationLabel(association) {
  const sources=state.publication.observations.filter(o => refs(association,'observation_ids').includes(o.id)).map(o => o.label || o.original_content);
  const targets=refs(association,'candidate_ids').map(id => definitions().find(d => d.id === id)?.label || 'Unknown attribute target');
  return (sources.join(', ') || 'Source link') + ' → ' + (targets.length ? targets.join(', ') : 'No attribute target recorded; evidence link only');
}
function associationExplanation(association) {
  if (association.origin?.kind === 'human' && association.origin.reason) return 'Expert note: ' + association.origin.reason;
  if (association.rationale === 'matched source label') return 'The source label was matched.';
  return 'The recorded source relationship is retained; inspect technical details for its original rationale.';
}
function sourceExcerpt(observation) {
  return `<article class="source-evidence"><blockquote>${esc(observation.original_content)}</blockquote><p>Original value: <strong>${esc(observation.value)}</strong>${observation.unit != null ? ' ' + esc(observation.unit) : ''}</p><p class="metadata">${esc(documentLabel(observation))} · ${esc(locatorLabel(observation.locator))}${observation.source_ref ? ' · Locator '+esc(observation.source_ref.locator_id) : ''}${observation.presence ? ' · Presence: '+esc(observation.presence) : ''}</p>${observation.content_pin ? '<p class="evidence-pin">Content pin: '+esc(observation.content_pin)+'</p>' : ''}${detail('Source technical details',observation)}</article>`;
}
function associationControls(association,index) {
  const current=association.disposition;
  const original=state.publication.associations.find(row => row.id === association.id);
  const semanticRelation=semanticContext() && original?.source_refs && original.target_ref ? '<p>Directed relation: '+esc(original.rationale)+' · Original disposition: '+esc(original.disposition)+'</p>'+(['ambiguous','unresolved'].includes(original.disposition) ? '<p class="metadata">The original match remains inconclusive; no match was chosen by publication.</p>' : '')+original.source_refs.map(ref => semanticReference(ref,'Source endpoint')).join('')+semanticReference(original.target_ref,'Target endpoint') : '';
  const options=associationChoices.some(row => row[0] === current) ? associationChoices : [[current,'Unresolved: ' + current],...associationChoices];
  return `<p class="link-relationship"><strong>${esc(associationLabel(association))}</strong></p><p class="metadata">${esc(associationExplanation(association))}</p>${semanticRelation}<label>Source link decision<select id="association-${index}" data-association="${index}" ${locked() ? 'disabled' : ''}>${choices(options,current)}</select></label><p class="metadata">Supported by source and accepted by expert both include this link. Leaving it for later or excluding it omits the link. A link decision does not approve an attribute or prove your edited meaning.</p>${detail('Link technical details',association)}`;
}
function relatedIndexes(definition) {
  return associationRecords().map((association,index) => ({association,index})).filter(({association}) => refs(association,'observation_ids').some(id => refs(definition,'observation_ids').includes(id)) || refs(association,'candidate_ids').includes(definition.id));
}
function scenarioIssue(ref,reason,field) {
  const index=definitions().findIndex(d => d.id === ref?.attribute_id);
  const scenarios=state.view?.scenario_review?.registry.scenarios || [];
  const scenarioIndex=scenarios.findIndex(s => s.id === ref?.scenario_id);
  const name=[definitions()[index]?.label,scenarios[scenarioIndex]?.label].filter(Boolean).join(' · ') || 'Scenario references';
  return {label:name + ': ' + reason,kind:index >= 0 ? 'definition' : null,index,
    field:scenarioIndex >= 0 ? (field === 'condition' ? 'condition-' : 'requirement-') + scenarioIndex : 'editDecision'};
}
function navigateReview(kind,index,field) {
  if (kind === 'definition') {state.selected=index; state.list=definitions()[index]?.decision === 'accepted' ? 'selected' : 'suggested'; render(); const control=$(field || 'editDecision') || $('editDecision'); control?.scrollIntoView({block:'center'}); control?.focus();}
  else {
    const target=definitions().findIndex(d => relatedIndexes(d).some(row => row.index === index));
    if (target >= 0) {state.selected=target; state.list=definitions()[target].decision === 'accepted' ? 'selected' : 'suggested'; render();}
    const control=$('association-' + index); control?.scrollIntoView({block:'center'}); control?.focus();
  }
}
function renderReviewStatus() {
  const rows=definitions(), associations=associationRecords(), pending=[];
  rows.forEach((d,index) => {if (!['accepted','deferred','rejected'].includes(d.decision)) pending.push({kind:'definition',index,label:d.label + ': decide whether to include this attribute'});});
  associations.forEach((a,index) => {if (!['supported','accepted','deferred','rejected'].includes(a.disposition)) {
    pending.push({kind:'association',index,label:associationLabel(a) + ': review the source link'});
  }});
  const hints=[];
  rows.forEach((d,index) => {
    if (d.decision !== 'accepted') return;
    if (!(typeof d.label === 'string' && d.label.trim())) hints.push({kind:'definition',index,field:'editLabel',label:'Included attribute needs a name'});
    if (d.value_type === 'decimal' && !(typeof d.unit === 'string' && d.unit.trim())) hints.push({kind:'definition',index,field:'editUnit',label:d.label + (isManual(d) ? ': an included number needs a literal expert-declared unit' : ': an included number needs its original unit')});
  });
  state.view?.scenario_review?.references.forEach(ref => {
    if (ref.requirement === 'conditional' && !ref.condition.trim()) hints.push(scenarioIssue(ref,'enter nonempty condition text before saving.','condition'));
  });
  const included=new Set(rows.filter(d => d.decision === 'accepted').map(d => d.id));
  associations.forEach((association,index) => {
    if (['supported','accepted'].includes(association.disposition) && refs(association,'candidate_ids').some(id => !included.has(id))) hints.push({kind:'association',index,label:associationLabel(association) + ': a linked attribute is not included; review the link or attribute decision'});
  });
  const items=(list,attribute) => list.map((item,index) => `<li><button type="button" ${attribute}="${index}">${esc(item.label)}</button></li>`).join('');
  $('issues').innerHTML=state.view?.approved_schema ? '<strong>This saved schema is approved and locked.</strong>' : `<strong>${pending.length ? pending.length + ' decisions to review' : 'No outstanding decisions'}</strong><p>You can save an unfinished review. Approval requires resolved decisions and valid included records; the backend makes the final validation.</p><ul>${items(pending,'data-pending-item')}</ul>${hints.length ? '<p>Potential approval blockers in your current edits:</p><ul>' + items(hints,'data-hint-item') + '</ul>' : ''}${state.backendErrors.length ? '<p>The backend refused the last command:</p><ul>' + items(state.backendErrors,'data-backend-item') + '</ul>' : ''}`;
  for (const [attribute,list] of [['data-pending-item',pending],['data-hint-item',hints],['data-backend-item',state.backendErrors]]) $('issues').querySelectorAll('['+attribute+']').forEach(button => button.addEventListener('click',() => {const item=list[Number(button.getAttribute(attribute))];if(item.kind) navigateReview(item.kind,item.index,item.field);else $('error').scrollIntoView({block:'center'});}));
}
function scenarioSummary(snapshot) {
  const review=snapshot.scenario_review;
  if (!review) return '<h3>Scenario data not recorded</h3><p>Requirements remain unspecified. No scenario assignments were inferred.</p>';
  return '<h3>Scenario references</h3><p>Registry '+esc(review.registry.version)+'</p><ul>'+review.registry.scenarios.map(s => '<li><strong>'+esc(s.label)+'</strong>: '+esc(s.meaning)+'</li>').join('')+'</ul>'+ (review.references.length ? '<table class="summary-table"><thead><tr><th>Attribute</th><th>Scenario</th><th>Requirement / condition</th></tr></thead><tbody>'+review.references.map(ref => '<tr><td>'+esc(snapshot.definitions.find(d => d.id === ref.attribute_id)?.label || ref.attribute_id)+'</td><td>'+esc(review.registry.scenarios.find(s => s.id === ref.scenario_id)?.label || ref.scenario_id)+'</td><td>'+esc(ref.requirement === 'required' ? 'Required' : 'Conditional')+(ref.requirement === 'conditional' ? ': '+esc(ref.condition) : '')+'</td></tr>').join('')+'</tbody></table>' : '<p>Configured with no scenario selections.</p>');
}
function renderScenarios(d) {
  const review=state.view?.scenario_review;
  if (!review) return '<section class="review-section"><h3>Scenario data not recorded</h3><p>Use Configure scenarios to begin an empty reference set. No requirements are assumed.</p></section>';
  if (d.decision !== 'accepted') return '<section class="review-section"><h3>Scenario references</h3><p>Include this attribute before selecting scenario references.</p></section>';
  return '<section class="review-section"><h3>Scenario references</h3><p class="metadata">Conditional text is an expert statement, never an executable rule or inferred source support.</p>'+review.registry.scenarios.map((s,index) => {
    const ref=review.references.find(r => r.attribute_id === d.id && r.scenario_id === s.id);
    return '<div class="scenario-reference"><h4>'+esc(s.label)+'</h4><p class="metadata">'+esc(s.meaning)+'</p>'+(ref ? '<label>Requirement<select id="requirement-'+index+'" data-requirement="'+index+'">'+choices([['required','Required'],['conditional','Conditional']],ref.requirement)+'</select></label>'+(ref.requirement === 'conditional' ? '<label>Condition (plain text)<textarea id="condition-'+index+'" data-condition="'+index+'">'+esc(ref.condition)+'</textarea></label>'+'<p id="conditionHint-'+index+'" class="validation" '+(ref.condition.trim() ? 'hidden' : '')+'>Enter nonempty condition text before saving.</p>' : '')+'<button data-remove-reference="'+index+'">Remove reference</button>' : '<p>Not selected</p><button data-add-reference="'+index+'">Add as Required</button>')+'</div>';
  }).join('')+'</section>';
}
function addReference(attributeIndex,scenarioIndex) {
  if(locked()) return;
  const d=definitions()[attributeIndex], review=state.view.scenario_review, scenario=review.registry.scenarios[scenarioIndex];
  if(d.decision !== 'accepted') return;
  if(!review.references.some(r => r.attribute_id === d.id && r.scenario_id === scenario.id)) {
    review.references.push({attribute_id:d.id,scenario_id:scenario.id,requirement:'required',condition:''}); markDirty();
  }
  state.selected=attributeIndex; render();
}
function equipmentSummary(snapshot) {
  const review=snapshot.equipment_review;
  if(!review) return '<h3>Equipment metadata not recorded</h3><p>No equipment wording, exclusions or category labels were inferred.</p>';
  return '<h3>Equipment coverage & categories</h3><h4>Included equipment</h4><p class="plain-text">'+esc(review.included_equipment || 'Not specified')+'</p><h4>Exclusions (descriptive only)</h4><p class="plain-text">'+esc(review.exclusions || 'Not specified')+'</p><h4>Category labels</h4>'+(review.categories.length ? '<ul>'+review.categories.map(label => '<li class="plain-text">'+esc(label)+'</li>').join('')+'</ul>' : '<p>None recorded</p>')+'<p>Expert-authored human judgment, not documentary evidence. Exclusions do not execute; categories confer no inheritance.</p>'+(review.origin ? '<p class="plain-text">Recorded by '+esc(review.origin.actor)+' · '+esc(review.origin.reason)+'</p>'+detail('Equipment provenance',review.origin) : '<p>Human provenance is recorded by the backend on Save.</p>');
}
function renderEquipmentState() {
  const review=state.view?.equipment_review;
  const preview=value => {const wording=value.replace(/\s+/g,' ').trim();return wording.length>110 ? wording.slice(0,110)+'…' : wording;};
  text('equipmentState',review ? 'Included: '+(preview(review.included_equipment) || 'Not specified')+' · Exclusions: '+(preview(review.exclusions) || 'Not specified')+' · Categories: '+(preview(review.categories.join('; ')) || 'None recorded') : 'Equipment metadata not recorded');
}
function renderEquipment() {
  text('equipmentDisclaimer','Expert-authored descriptive metadata. Human judgment has no documentary support; exclusions do not execute applicability rules. Category labels confer no inheritance or source evidence.');
  const review=state.view?.equipment_review;
  renderEquipmentState();
  $('configureEquipment').hidden=Boolean(review) || !state.view || Boolean(state.view.approved_schema);
  $('toggleEquipment').hidden=!review;
  $('toggleEquipment').textContent=state.equipmentOpen ? 'Close equipment metadata' : state.view?.approved_schema ? 'View equipment metadata' : 'Edit equipment metadata';
  $('toggleEquipment').setAttribute('aria-expanded',String(state.equipmentOpen));
  $('equipmentDetails').hidden=!state.equipmentOpen;
  $('equipmentEditor').innerHTML=review ? '<label>Included equipment (plain text)<textarea id="includedEquipment" maxlength="4000">'+esc(review.included_equipment)+'</textarea></label><label>Exclusions (plain text)<textarea id="equipmentExclusions" maxlength="4000">'+esc(review.exclusions)+'</textarea></label><h3>Category labels</h3><p>Up to 16 exact labels; no taxonomy or inheritance. Whitespace is preserved.</p><ul>'+review.categories.map((label,index) => '<li><span class="plain-text">'+esc(label)+'</span> <button type="button" data-remove-category="'+index+'">Remove category '+esc(label)+'</button></li>').join('')+'</ul><label>New category label<input id="categoryLabel" maxlength="200" autocomplete="off" value="'+esc(state.categoryLabel)+'"></label><button id="addCategory" type="button">Add category</button><p id="categoryError" role="alert"></p>' : '<p>Configure explicitly to start with empty wording and no categories. Legacy metadata stays absent until you configure and Save.</p>';
  if(!review) return;
  for(const [id,field] of [['includedEquipment','included_equipment'],['equipmentExclusions','exclusions']]) $(id).addEventListener('input',event => {if(locked())return;review[field]=event.target.value;markDirty();renderEquipmentState();});
  $('categoryLabel').addEventListener('input',event => {if(!locked())state.categoryLabel=event.target.value;});
  $('addCategory').addEventListener('click',() => {
    if(locked())return;
    const label=$('categoryLabel').value;
    const invalid=!label.trim() ? 'Enter a nonblank category label.' : label.length>200 ? 'Category labels allow at most 200 characters.' : review.categories.includes(label) ? 'This exact category label is already recorded.' : review.categories.length>=16 ? 'At most 16 category labels are allowed.' : '';
    if(invalid) {text('categoryError',invalid);$('categoryLabel').focus();return;}
    review.categories.push(label);state.categoryLabel='';markDirty();renderEquipment();controls();$('categoryLabel').focus();
  });
  $('equipmentEditor').querySelectorAll('[data-remove-category]').forEach(button => button.addEventListener('click',() => {if(locked())return;review.categories.splice(Number(button.dataset.removeCategory),1);markDirty();renderEquipment();controls();$('categoryLabel').focus();}));
}
function schemaSummary(snapshot,savedReview = null) {
  const included=snapshot.definitions.filter(d => d.decision === 'accepted');
  const excluded=snapshot.definitions.filter(d => d.decision !== 'accepted');
  const associations=(snapshot.associations || []).map(a => a || {});
  const definitionDecisions=list => list.map(d => '<li>'+esc(d.label)+': '+esc(labelFor(attributeChoices,d.decision))+(isManual(d) ? ' · '+manualEvidence : '')+'</li>').join('');
  const linkDecisions=list => list.map(a => '<li>'+esc(associationLabel(a))+': '+esc(labelFor(associationChoices,a.disposition))+'</li>').join('');
  const sameSavedRevision=savedReview && savedReview.revision === snapshot.revision && savedReview.draft_id === snapshot.draft_id;
  let retainedExclusions='';
  if (sameSavedRevision) {
    const omittedDefinitions=savedReview.definitions.filter(d => d.decision !== 'accepted');
    const omittedLinks=savedReview.associations.map(a => a || {}).filter(a => !['supported','accepted'].includes(a.disposition));
    retainedExclusions=`<section class="retained-decisions"><h3>Excluded decisions retained in saved revision ${esc(savedReview.revision)}</h3><p>These decisions belong to the saved review history, not the immutable approved schema.</p>${omittedDefinitions.length || omittedLinks.length ? '<ul>'+definitionDecisions(omittedDefinitions)+linkDecisions(omittedLinks)+'</ul>' : '<p>No excluded decisions were recorded in this saved revision.</p>'}</section>`;
  }
  return `<h3>Included technical attributes (${included.length})</h3>${included.length ? '<table class="summary-table"><thead><tr><th>Attribute</th><th>Type</th><th>Unit</th></tr></thead><tbody>' + included.map(d => '<tr><td>'+esc(d.label)+(isManual(d) ? '<br><small>'+manualEvidence+'</small>' : '')+'</td><td>'+esc(typeLabel(d))+'</td><td>'+esc(d.unit ?? 'None — text')+'</td></tr>').join('') + '</tbody></table>' : '<p>No technical attributes are included.</p>'}${excluded.length ? '<h3>Other attribute decisions</h3><ul>'+definitionDecisions(excluded)+'</ul>' : ''}<h3>Source link decisions</h3><ul>${linkDecisions(associations)}</ul>${retainedExclusions}${equipmentSummary(snapshot)}${scenarioSummary(snapshot)}<p class="summary-limitations">${esc(semanticContext() ? 'Semantic experiment projection. Publication and approval do not establish original semantic quality.' : 'Synthetic fixture only.')} Scenario selections and conditions are expert statements. Source values are evidence, never defaults. Human edits are not independently verified by the source.</p>`;
}
function matrixRows(rows) {
  const references=state.view?.scenario_review?.references || [];
  const compare=(a,b) => a<b ? -1 : a>b ? 1 : 0;
  const label=d => typeof d.label === 'string' ? d.label.toLocaleLowerCase() : '';
  const count=(d,kind) => references.filter(ref => ref.attribute_id === d.id && ref.requirement === kind).length;
  return rows.map((d,index) => ({d,index})).filter(({d}) => {
    if(!label(d).includes(state.matrixSearch.toLocaleLowerCase()))return false;
    if(d.decision !== 'accepted')return true;
    const refs=references.filter(ref => ref.attribute_id === d.id && (!state.matrixScenario || ref.scenario_id === state.matrixScenario));
    return state.matrixRequirement === 'all' || (state.matrixRequirement === 'referenced' ? refs.length>0 : state.matrixRequirement === 'unreferenced' ? refs.length===0 : refs.some(ref => ref.requirement === state.matrixRequirement));
  }).sort((a,b) => (state.matrixSort === 'priority' ? count(b.d,'required')-count(a.d,'required') || count(b.d,'conditional')-count(a.d,'conditional') : 0) || compare(label(a.d),label(b.d)) || compare(a.d.label,b.d.label) || compare(a.d.id,b.d.id));
}
const readOnlyReasons = {
  unsupported_value_type:'Only decimal and text definitions can be reviewed here; the original type is retained.',
  unsupported_domain:'Only technical definitions can be reviewed here; this domain remains separate.',
  external_context_evidence:'Evidence includes another document and cannot become target-document support.',
  missing_observation_mapping:'An evidence location has no mapped target observation.',
  ambiguous_observation_mapping:'An evidence location maps to more than one observation; no match was chosen.',
  unsupported_evidence:'Evidence is not an observed technical value eligible for this definition.',
  cross_document_relation:'This relation spans documents and remains separate from editable local source links.'
};
function semanticReference(ref,heading='Evidence endpoint') {
  const doc=semanticContext().documents.find(doc => doc.document_id === ref.document_id && doc.source_revision === ref.source_revision);
  const span=doc?.spans.find(span => span.locator_id === ref.locator_id);
  return '<div class="semantic-endpoint"><strong>'+esc(heading)+'</strong><p>'+esc(documentLabel(ref))+' · Locator '+esc(ref.locator_id)+(span ? ' · '+esc(locatorLabel(span.locator)) : '')+'</p>'+(span ? '<blockquote>'+esc(span.text)+'</blockquote>' : '')+(doc ? '<p class="evidence-pin">Content pin: '+esc(doc.content_pin)+'</p>' : '')+'</div>';
}
function readOnlyRecord(item) {
  const row=item.record;
  const endpoints=item.kind === 'association' ? (row.source_refs || []).map(ref => semanticReference(ref,'Source endpoint')).join('')+(row.target_ref ? semanticReference(row.target_ref,'Target endpoint') : '') : (row.evidence_refs || (row.document_id ? [row] : [])).map(ref => semanticReference(ref)).join('');
  return '<article class="semantic-record"><h3>'+esc(row.label || row.relation || row.id)+'</h3><p class="metadata">'+esc(item.kind)+' · '+esc(row.id)+' · Read-only</p><p>'+esc(readOnlyReasons[item.reason] || 'This original record cannot be represented by the current review contract.')+'</p>'+(item.kind === 'association' ? '<p>Directed relation: '+esc(row.relation)+' · Original disposition: '+esc(row.disposition)+'</p>' : '<p>'+(item.kind === 'observation' ? 'Presence: '+esc(row.presence) : 'Original type: '+esc(row.value_type ?? 'Not recorded'))+' · Domain: '+esc(row.domain ?? 'Not recorded')+' · Literal unit: '+esc(row.unit ?? row.original_unit ?? 'None')+'</p>')+(row.original_value != null ? '<p>Original value: '+esc(row.original_value)+'</p>' : '')+endpoints+detail('Original record & projection reason',item)+'</article>';
}
function originalSuggestion(definition) {
  if(isManual(definition)) return '';
  const original=state.publication.candidates.find(candidate => candidate.id === definition.id);
  if(!original) return '';
  return '<section class="review-section"><h3>Original suggestion</h3><p>'+esc(original.label)+' · '+esc(typeLabel(original))+' · '+esc(original.unit ?? 'No unit')+'</p><p class="metadata">Requirements remain '+esc(original.requirements || 'unspecified')+'. Source values are evidence, never defaults. Human edits do not change the original suggestion or prove its edited meaning.</p>'+(definition.origin?.actor ? '<p class="human-note">Recorded by '+esc(definition.origin.actor)+(definition.origin.reason ? ' · '+esc(definition.origin.reason) : '')+'</p>' : '')+'</section>';
}
function renderSemanticContext() {
  const semantic=semanticContext();
  text('publicationOrigin',publicationOrigin());
  $('semanticPanel').hidden=!semantic;
  if(!semantic) return;
  text('coverage','Original operational status: '+semantic.original_status+'. Operational completeness is separate from semantic quality and equipment coverage; partial, unread, timeout or unknown scope is inconclusive.');
  $('semanticInventory').innerHTML=semantic.documents.map(doc => '<article class="semantic-record"><h3>'+esc(documentLabel(doc))+'</h3><p>'+esc(doc.document_id === semantic.target.document_id && doc.source_revision === semantic.target.source_revision ? 'Target document' : 'Other-document context · not target evidence')+'</p><p class="evidence-pin">Content pin: '+esc(doc.content_pin)+'</p>'+detail('Recorded operational coverage',doc.coverage)+'<ul>'+(doc.spans || []).map(span => '<li>'+esc(span.locator_id)+' · '+esc(locatorLabel(span.locator))+'<p class="plain-text">'+esc(span.text)+'</p></li>').join('')+'</ul></article>').join('');
  text('semanticReadOnlySummary','Read-only original records ('+semantic.read_only_records.length+') · unsupported suggestions & document context');
  $('semanticReadOnly').innerHTML='<div class="panel-head"><h3>Read-only original records ('+semantic.read_only_records.length+')</h3><p>These records cannot be edited, accepted as technical definitions, or used as invented documentary support. Manual attributes remain separate human judgments.</p></div>'+semantic.read_only_records.map(readOnlyRecord).join('');
  text('semanticRaw',JSON.stringify({mapping_version:semantic.mapping_version,origin:semantic.origin,target:semantic.target,original_status:semantic.original_status,record_mapping:semantic.record_mapping,configuration:semantic.configuration,original_output:semantic.original_output,provenance:semantic.provenance},null,2));
  $('readSemanticArtifact').hidden=!semantic.provenance?.available;
}
function render() {
  if (customerMode) {
    const link = $('minimizedEvidenceLink');
    const supported = ['ticket05-synthetic-evidence-v1', 'pump-rfq-document-v1', 'rfq-text-blocks-v1', 'rfq-text-blocks-v2', 'rfq-text-blocks-v3', 'rfq-text-blocks-v4'];
    link.hidden = !permitted('Reviewer') || !supported.includes(state.publication.adapter);
    link.href = '/minimized-evidence' + location.search;
  }
  $('workspace').hidden = false; $('login').hidden = !state.authRequired;
  text('productTitle',state.publication.context.product_type);
  text('jobContext',documentLabel() + ' · Original excerpts remain unchanged');
  text('coverage',state.publication.schema_inspection ? 'Only source records linked to included schema records are displayed.' : state.publication.coverage.unread?.length ? 'Some source content was not inspected. See technical details.' : 'Recorded source scope inspected; this is not a completeness guarantee.');
  text('workspaceTechnical',JSON.stringify({context:state.context,coverage:state.publication.coverage,draft:state.view && {draft_id:state.view.draft_id,revision:state.view.revision,base_version:state.view.base_version,pending_conflicts:state.view.pending_conflicts}},null,2));
  renderSemanticContext();
  renderEquipment();
  const rows=definitions(); if (state.selected >= rows.length) state.selected=0;
  const review=state.view?.scenario_review, scenarios=review?.registry.scenarios || [];
  const filtered=matrixRows(rows), selectedCount=rows.filter(d => d.decision === 'accepted').length;
  const selectedMatches=filtered.filter(({d}) => d.decision === 'accepted').length;
  const scenarioFiltering=Boolean(state.matrixScenario || state.matrixRequirement !== 'all');
  const filtering=Boolean(state.matrixSearch || (state.list === 'selected' && scenarioFiltering));
  text('selectedView','Selected ('+(state.matrixSearch || scenarioFiltering ? selectedMatches+' of ' : '')+selectedCount+')');
  text('suggestedView','Suggestions ('+(state.matrixSearch ? (filtered.length-selectedMatches)+' of ' : '')+(rows.length-selectedCount)+')');
  $('matrixScenario').innerHTML=choices([['','Any scenario'],...scenarios.map(s => [s.id,s.label])],state.matrixScenario);
  $('matrixScenario').disabled=state.list !== 'selected' || !scenarios.length;
  $('matrixRequirement').disabled=state.list !== 'selected' || !review;
  $('matrixSearch').value=state.matrixSearch; $('matrixSort').value=state.matrixSort; $('matrixRequirement').value=state.matrixRequirement;
  $('selectedView').setAttribute('aria-pressed',String(state.list === 'selected'));
  $('suggestedView').setAttribute('aria-pressed',String(state.list === 'suggested'));
  text('scenarioState',review ? (review.references.length ? review.references.length+' scenario references · '+review.registry.version : 'Configured · no scenario selections') : 'Scenario data not recorded · requirements unspecified');
  $('configureScenarios').hidden=Boolean(review) || !state.view || Boolean(state.view.approved_schema);
  $('matrixHead').innerHTML='<tr><th>Attribute / unit</th><th>Your decision</th>'+scenarios.map(s => '<th title="'+esc(s.meaning)+'">'+esc(s.label)+'</th>').join('')+'</tr>';
  const visible=filtered.filter(({d}) => (d.decision === 'accepted') === (state.list === 'selected'));
  text('inspectorContext',rows[state.selected] && !visible.some(row => row.index === state.selected) ? 'The focused attribute is outside the current list or filtered view. Its edits are retained; clear search & filters or switch lists to see its row.' : 'Focused attribute is visible in the matrix.');
  $('definitions').innerHTML=visible.map(({d,index}) => '<tr class="'+(index === state.selected ? 'chosen' : '')+'"><th scope="row"><button class="row-name" data-select="'+index+'">'+esc(d.label)+'</button><small>'+esc(typeLabel(d))+' · '+esc(d.unit ?? 'No unit')+'</small>'+(isManual(d) ? '<small class="manual-origin">'+manualEvidence+'</small>' : '')+'</th><td><span class="decision-pill">'+esc(labelFor(attributeChoices,d.decision || 'awaiting'))+'</span></td>'+scenarios.map((s,scenarioIndex) => {
    if(d.decision !== 'accepted') return '<td class="metadata">Include attribute first</td>';
    const ref=review.references.find(r => r.attribute_id === d.id && r.scenario_id === s.id);
    return '<td><button id="cell-'+index+'-'+scenarioIndex+'" class="cell-control '+(ref?.requirement === 'conditional' && !ref.condition.trim() ? 'missing' : '')+'" data-cell="'+index+'" data-scenario="'+scenarioIndex+'" aria-label="'+esc(d.label+' · '+s.label+': '+(ref ? ref.requirement : 'Not selected; add as Required'))+'">'+(ref ? ref.requirement === 'required' ? 'Required' : 'Conditional' : '+ Add')+'</button>'+(ref?.requirement === 'conditional' ? '<p id="conditionContext-'+index+'-'+scenarioIndex+'" class="cell-context">'+esc(ref.condition.trim() ? ref.condition : 'Condition needed')+'</p>' : '')+'</td>';
  }).join('')+'</tr>').join('') || '<tr><td colspan="'+(2+scenarios.length)+'" class="no-results">'+(filtering ? 'No matching attributes in this list. Clear search & filters to recover the full list.' : state.list === 'selected' ? 'No selected attributes. Inspect Suggestions to make an explicit decision.' : 'No suggestions. All attributes are selected.')+'</td></tr>';
  $('definitions').querySelectorAll('[data-select]').forEach(button => button.addEventListener('click',() => {state.selected=Number(button.dataset.select);render();}));
  $('definitions').querySelectorAll('[data-cell]').forEach(button => button.addEventListener('click',() => {if(locked()) return;addReference(Number(button.dataset.cell),Number(button.dataset.scenario));$('requirement-'+button.dataset.scenario)?.focus();}));
  const d=rows[state.selected], disabled=locked() ? 'disabled' : '';
  text('selectedTitle',d?.label || 'No technical attributes');
  const linked=d ? relatedIndexes(d) : [];
  const observations=state.publication.observations.filter(o => refs(d,'observation_ids').includes(o.id));
  $('inspector').innerHTML=d ? `<section class="review-section"><h3>Attribute definition</h3><label>Display label<input id="editLabel" value="${esc(d.label)}" ${disabled}></label><label>Value type<select id="editType" ${disabled}>${choices([['decimal','Number'],['text','Text']],d.value_type)}</select></label><label>Definition unit (literal)<input id="editUnit" value="${esc(d.unit ?? '')}" ${disabled || (d.value_type === 'text' ? 'disabled' : '')}></label><p class="metadata">${isManual(d) ? 'Expert-declared unit; no documentary support.' : 'This definition unit can be edited. Original source units remain in the excerpts below.'} Text has no unit. No conversion is performed.</p><label>Attribute decision<select id="editDecision" ${disabled}>${choices(attributeChoices,d.decision || 'awaiting')}</select></label><p class="metadata">Include adds this definition to the schema. Leave for later and Exclude both omit it, while retaining your distinct decision. Not decided prevents approval.</p>${isManual(d) ? '<button id="'+(d.decision === 'accepted' ? 'removeManual' : 'restoreManual')+'" type="button">'+(d.decision === 'accepted' ? 'Remove from schema' : 'Restore to Selected')+'</button>' : ''}</section>${originalSuggestion(d)}${renderScenarios(d)}<section class="review-section"><h3>Source & related links</h3>${isManual(d) ? '<p class="manual-origin">'+manualEvidence+'</p><p>Human judgment has no documentary support and is not independently verified.</p>' : ''}${observations.map(sourceExcerpt).join('') || '<p>No linked source excerpts.</p>'}${linked.map(({association,index}) => associationControls(association,index)).join('')}<p class="metadata">${isManual(d) ? 'No source observations or source links are attached to this manual definition.' : 'Original evidence is retained even when you change a definition.'}</p></section>${detail('Definition technical details',{definition:d,original_candidate:state.publication.candidates.find(c => c.id === d.id)})}` : '<p>No technical attributes in this proposal.</p>';
  if (state.view && !state.view.approved_schema && d) {
    $('editLabel').addEventListener('input',event => {if(locked()) return;d.label=event.target.value;markDirty();
      text('selectedTitle',d.label);
      $('definitions').querySelectorAll('[data-select]').forEach(button => {if(Number(button.dataset.select) === state.selected) button.textContent=d.label;});
      $('definitions').querySelectorAll('[data-cell]').forEach(button => {if(Number(button.dataset.cell) === state.selected) {
        const scenario=state.view.scenario_review.registry.scenarios[Number(button.dataset.scenario)];
        const ref=state.view.scenario_review.references.find(r => r.attribute_id === d.id && r.scenario_id === scenario.id);
        button.setAttribute('aria-label',d.label+' · '+scenario.label+': '+(ref ? ref.requirement : 'Not selected; add as Required'));
      }});});
    $('editType').addEventListener('change',event => {if(locked()) return;d.value_type=event.target.value;d.unit=d.value_type === 'text' ? null : '';markDirty();render();});
    $('editUnit').addEventListener('input',event => {if(locked()) return;d.unit=event.target.value;markDirty();});
    const changeDecision=(next,control) => {if(locked()) return;const review=state.view.scenario_review, restoring=isManual(d) && d.decision !== 'accepted' && next === 'accepted';
      const refs=review?.references.filter(r => r.attribute_id === d.id) || [];
      if(next !== 'accepted' && refs.length) {
        if(!window.confirm('Remove all '+refs.length+' scenario references for '+d.label+' and change its decision to '+labelFor(attributeChoices,next)+'? Your edited definition and original evidence will be retained.')) {if(control) control.value=d.decision;return;}
        review.references=review.references.filter(r => r.attribute_id !== d.id);
      }
      if(restoring && review) review.references=review.references.filter(r => r.attribute_id !== d.id);
      d.decision=next;state.list=next === 'accepted' ? 'selected' : 'suggested';markDirty();render();
      if(restoring) notice('Restored to Selected. Reassess scenario references; previous selections were not restored.');
    };
    $('editDecision').addEventListener('change',event => changeDecision(event.target.value,event.target));
    $('removeManual')?.addEventListener('click',() => changeDecision('rejected'));
    $('restoreManual')?.addEventListener('click',() => changeDecision('accepted'));
  }
  if(state.view?.scenario_review && d) {
    const review=state.view.scenario_review;
    const reference=index => review.references.find(r => r.attribute_id === d.id && r.scenario_id === review.registry.scenarios[index].id);
    $('inspector').querySelectorAll('[data-add-reference]').forEach(button => button.addEventListener('click',() => addReference(state.selected,Number(button.dataset.addReference))));
    $('inspector').querySelectorAll('[data-remove-reference]').forEach(button => button.addEventListener('click',() => {if(locked())return;const ref=reference(Number(button.dataset.removeReference));review.references=review.references.filter(r => r !== ref);markDirty();render();}));
    $('inspector').querySelectorAll('[data-requirement]').forEach(select => select.addEventListener('change',() => {if(locked())return;const ref=reference(Number(select.dataset.requirement));ref.requirement=select.value;if(ref.requirement === 'required')ref.condition='';markDirty();render();}));
    $('inspector').querySelectorAll('[data-condition]').forEach(input => input.addEventListener('input',() => {
      if(locked())return;
      const index=Number(input.dataset.condition), missing=!input.value.trim();
      reference(index).condition=input.value;
      $('conditionHint-'+index).hidden=!missing;
      const cell=$('cell-'+state.selected+'-'+index), context=$('conditionContext-'+state.selected+'-'+index);
      if(cell) cell.setAttribute('class','cell-control '+(missing ? 'missing' : ''));
      if(context) context.textContent=missing ? 'Condition needed' : input.value;
      markDirty();
    }));
  }
  const technicalAssociations=new Set(rows.flatMap(definition => relatedIndexes(definition).map(row => row.index)));
  const others=associationRecords().map((association,index) => ({association,index})).filter(row => !technicalAssociations.has(row.index));
  const allLinked=new Set(associationRecords().flatMap(a => a.observation_ids || []));
  const technicalObservations=new Set(rows.flatMap(definition => definition.observation_ids || []));
  const unlinked=state.publication.observations.filter(o => !allLinked.has(o.id) && !technicalObservations.has(o.id));
  $('otherEvidencePanel').hidden=!others.length && !unlinked.length;
  $('associations').innerHTML=others.map(({association,index}) => '<article class="other-evidence">'+state.publication.observations.filter(o => refs(association,'observation_ids').includes(o.id)).map(sourceExcerpt).join('')+associationControls(association,index)+'</article>').join('')+unlinked.map(sourceExcerpt).join('');
  document.querySelectorAll('[data-association]').forEach(select => select.addEventListener('change',event => {if(locked()) return;state.view.associations[Number(select.dataset.association)].disposition=event.target.value;markDirty();render();}));
  renderReviewStatus(); text('original',JSON.stringify(state.publication,null,2));
  $('approvedPanel').hidden=!state.view?.approved_schema;
  if(state.view?.approved_schema) {
    const approved=state.view.approved_schema;
    text('approvedMeta','Approved version '+approved.version+' · saved revision '+approved.revision+' · '+approved.actor);
    $('approvedSummary').innerHTML=schemaSummary(approved,state.view);text('approvedSnapshot',JSON.stringify(approved,null,2));
  }
  controls();
}
const immutableSignature = value => JSON.stringify(value, function(key,item) {return item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(name => [name,item[name]])) : item;});
async function loadDraft(draftId,expectedApproval = null,receipt = null) {
  const view = await readWhole({operation:'review_read',draft_id:draftId,...(receipt ? {revision:receipt.revision} : {})});
  if (!view.saved || view.initial.job_id !== state.context.job_id) throw new Error('Selected draft does not match this Job.');
  if(receipt && (view.draft_id !== receipt.draft_id || !Number.isInteger(receipt.revision) || !Number.isInteger(view.revision) || view.revision !== receipt.revision || (receipt.approved && (!view.approved_schema || view.approved_schema.id !== receipt.approved_version || view.approved_schema.revision !== receipt.revision)))) throw new Error('The backend did not return a saved snapshot matching the resolved receipt. Preserve the pending command and resolve again.');
  if(expectedApproval && (view.draft_id !== draftId || !view.approved_schema || immutableSignature(view.approved_schema) !== immutableSignature(expectedApproval))) throw new Error('The backend did not return the matching immutable approved version. The last successful snapshot is retained.');
  state.equipmentOpen=false; state.categoryLabel='';
  state.view=view; state.publication=view.initial; state.dirty=false; state.approval=null; state.backendErrors=[];
  if (!$('actor').value) $('actor').value=view.actor || '';
  render();
}
async function openWorkspace() {
  state.context = customerMode ? await customerRequest('context') : await api('/api/context');
  if (customerMode) {
    const intentModule=await import('/customer-intent.js');
    fitsReviewEnvelopes=intentModule.fitsReviewEnvelopes;commandFingerprint=intentModule.commandFingerprint;
    representableReview=intentModule.representableReview;
    vault=await intentModule.openIntentStore(state.context.roles,role=>customerRequest('unlock',{role}));
    browserIntent=await vault.read();
    serverIntent=state.context.pending;serverTag=state.context.pending_tag;
    originalSource=browserIntent ? 'local' : 'server';
    state.pending=browserIntent?.command || serverIntent || null;
    $('actor').value=state.context.actor;
  }
  state.authRequired=false;state.retryReady=false;
  if (customerMode && state.context.draft_id) {
    await loadDraft(state.context.draft_id);
    if(state.pending) notice('Resolve the original pending command before continuing.');
    return;
  }
  const result = await readWhole({operation:'result',job_id:state.context.job_id});
  const publications = result.publications.filter(p => p.kind === 'initial');
  if (publications.length !== 1) throw new Error('Select exactly one actual initial publication; no proposal was fabricated.');
  state.publication = publications[0];
  try { state.pending = customerMode ? state.pending : JSON.parse(sessionStorage.getItem(pendingKey()) || 'null'); }
  catch { throw new Error('Pending command journal is unreadable; preserve it before proceeding.'); }
  let id = state.context.draft_id;
  if (!id && !customerMode) {
    const resolved=await command({operation:'review_resolve',namespace:namespace(),command_id:'create'});
    if (resolved.saved && resolved.draft_id) id=resolved.draft_id;
    else if (resolved.known !== false) throw new Error(resolved.category || 'Draft selection refused');
  }
  if (id) await loadDraft(id); else render();
  if (state.pending) {retainedCommand=true;controls();}
  if (state.pending) notice('A pending command was retained. Resolve its exact key before editing or approving.');
}
function action(id, handler) {
  $(id).addEventListener('click', async () => {
    if (state.busy || state.authRequired) return;
    clearError(); state.busy=true; controls();
    try { await handler(); } catch (e) {
      if (id === 'confirmApproval') {
        $('approvalDialog').close(); state.approval=null;
      }
      error(e.message);
    }
    finally { state.busy=false; controls(); }
  });
}
$('loginForm').addEventListener('submit', async event => {
  event.preventDefault(); clearError();
  const token=$('token').value; $('token').value='';
  if(customerMode){location.assign('/');return;}
  try { await api('/api/session',{token}); await openWorkspace(); }
  catch(e) { error(e.message); }
});
$('addManual').addEventListener('click',() => {
  if(locked()) return;
  $('manualLabel').value=''; $('manualType').value=''; $('manualUnit').value=''; text('manualError',''); controls();
  $('manualDialog').showModal(); $('manualLabel').focus();
});
$('cancelManual').addEventListener('click',() => {$('manualDialog').close();});
$('manualType').addEventListener('change',() => {if(locked())return;if($('manualType').value === 'text') $('manualUnit').value='';controls();});
$('manualForm').addEventListener('submit',event => {
  event.preventDefault(); if(locked()) return;
  const label=$('manualLabel').value.trim(), value_type=$('manualType').value, unit=$('manualUnit').value.trim();
  const invalid=!label ? ['manualLabel','Enter an attribute name.'] : !['decimal','text'].includes(value_type) ? ['manualType','Choose Number or Text.'] : value_type === 'decimal' && !unit ? ['manualUnit','Enter a literal unit token for this number.'] : null;
  if(invalid) {text('manualError',invalid[1]);$(invalid[0]).focus();return;}
  const id='manual:'+crypto.randomUUID();
  if(definitions().some(d => d.id === id) || state.publication.candidates.some(d => d.id === id)) {text('manualError','A new identity could not be allocated. Try adding again.');return;}
  state.view.definitions.push({id,definition_kind:'human_manual_v1',label,value_type,unit:value_type === 'text' ? null : unit,requirements:'unspecified',conditions:'unspecified',observation_ids:[],decision:'accepted'});
  state.selected=state.view.definitions.length-1;state.list='selected';$('manualDialog').close();markDirty();render();$('editLabel').focus();
});
for(const [id,key,event] of [['matrixSearch','matrixSearch','input'],['matrixSort','matrixSort','change'],['matrixScenario','matrixScenario','change'],['matrixRequirement','matrixRequirement','change']]) {
  $(id).addEventListener(event,() => {state[key]=$(id).value;render();});
}
$('clearMatrixFilters').addEventListener('click',() => {state.matrixSearch='';state.matrixScenario='';state.matrixRequirement='all';render();});
$('selectedView').addEventListener('click',() => {state.list='selected';render();});
$('suggestedView').addEventListener('click',() => {state.list='suggested';render();});
$('toggleEquipment').addEventListener('click',() => {
  state.equipmentOpen=!state.equipmentOpen;
  $('equipmentDetails').hidden=!state.equipmentOpen;
  $('toggleEquipment').setAttribute('aria-expanded',String(state.equipmentOpen));
  $('toggleEquipment').textContent=state.equipmentOpen ? 'Close equipment metadata' : state.view?.approved_schema ? 'View equipment metadata' : 'Edit equipment metadata';
  (state.equipmentOpen && !locked() ? $('includedEquipment') : $('toggleEquipment')).focus();
});
$('configureEquipment').addEventListener('click',() => {
  if(locked() || state.view.equipment_review)return;
  state.view.equipment_review={version:'equipment-review-v1',included_equipment:'',exclusions:'',categories:[]};
  state.equipmentOpen=true;state.categoryLabel='';
  markDirty();render();$('includedEquipment').focus();notice('Equipment metadata configured empty. Save to record this explicit configuration.');
});
$('configureScenarios').addEventListener('click',() => {
  if(locked() || state.view.scenario_review) return;
  if(!state.view.available_scenario_registry) {error('The backend did not supply an available scenario registry. Reload before configuring.');return;}
  state.view.scenario_review={version:'scenario-review-v1',registry:clone(state.view.available_scenario_registry),references:[]};
  markDirty();render();notice('Scenarios configured with no selections. Save to record this explicit configuration.');
});
action('createDraft',async () => {
  const created=await mutate({operation:'review_create',namespace:namespace(),command_id:'create',job_id:state.context.job_id,proposal_id:state.publication.id,product_type:state.publication.context.product_type,expected_base:state.context.base_version,actor:actor()});
  if(!customerMode) await loadDraft(created.draft_id);
  notice('Draft created from the actual publication. No approval recorded.');
});
action('saveDraft',async () => {
  const reason=$('reason').value.trim(); if (!reason) throw new Error('Enter a save reason.');
  const saved=await mutate({operation:'review_save',namespace:namespace(),command_id:mutationId('save'),draft_id:state.view.draft_id,expected_revision:state.view.revision,definitions:clone(state.view.definitions),associations:clone(state.view.associations),...(state.view.equipment_review ? {equipment_review:clone(state.view.equipment_review)} : {}),...(state.view.scenario_review ? {scenario_review:clone(state.view.scenario_review)} : {}),actor:actor(),reason});
  if(!customerMode) await loadDraft(saved.draft_id);
  notice('Saved and read back persisted revision ' + saved.revision + '.');
});
action('reloadDraft',async () => {
  if (state.dirty && !window.confirm('Discard unsaved edits and reload the persisted draft?')) return;
  await loadDraft(state.view.draft_id); notice('Reloaded persisted draft from the backend.');
});
action('reviewApproval',async () => {
  if (state.dirty || state.pending || !state.view || state.view.approved_schema) throw new Error('Save and reload the exact revision before approval.');
  state.approval={revision:state.view.revision,draft_id:state.view.draft_id,product_type:state.view.product_type,actor:actor()};
  text('approvalScope',state.view.product_type + ' · saved revision ' + state.view.revision + ' · approval by ' + state.approval.actor);
  $('approvalSummary').innerHTML=schemaSummary(state.view);
  text('approvalSnapshot',JSON.stringify({revision:state.view.revision,definitions:state.view.definitions,associations:state.view.associations,...(state.view.equipment_review ? {equipment_review:state.view.equipment_review} : {}),...(state.view.scenario_review ? {scenario_review:state.view.scenario_review} : {})},null,2));
  $('explicitApproval').checked=false; $('confirmApproval').disabled=true; $('approvalDialog').showModal();
});
$('explicitApproval').addEventListener('change',controls);
$('cancelApproval').addEventListener('click',() => {$('approvalDialog').close(); state.approval=null;});
action('confirmApproval',async () => {
  const snapshot=state.approval;
  if (!$('explicitApproval').checked || !snapshot || state.dirty || state.pending || snapshot.revision !== state.view.revision) throw new Error('Approval confirmation is stale or incomplete.');
  const approved=await mutate({operation:'review_approve',namespace:namespace(),command_id:mutationId('approve'),draft_id:snapshot.draft_id,expected_revision:snapshot.revision,product_type:snapshot.product_type,actor:snapshot.actor,explicit:true});
  $('approvalDialog').close();
  if(!customerMode) await loadDraft(approved.draft_id);
  notice('Immutable approved version read back from the backend.');
});
action('readSemanticArtifact',async () => {
  const provenance=semanticContext()?.provenance;
  if(!provenance?.available) return;
  text('semanticArtifactState','Reading original semantic artifact from the backend…');
  text('semanticArtifactRaw',''); $('semanticArtifactDetails').hidden=true; $('semanticArtifactDetails').open=false;
  try {
    const result=await readWhole({operation:'result',job_id:state.publication.job_id,proposal_id:state.publication.id,provenance:true});
    if(result.category) throw new Error('Original semantic artifact read refused: '+result.category+'.');
    if(result.job_id !== state.publication.job_id || result.proposal_id !== state.publication.id || result.artifact_pin !== provenance.artifact_pin || !result.artifact || typeof result.artifact !== 'object') throw new Error('Original semantic artifact identity or content did not match the selected publication.');
    text('semanticArtifactRaw',JSON.stringify(result.artifact,null,2));
    $('semanticArtifactDetails').hidden=false;
    text('semanticArtifactState','Read completed · immutable original semantic artifact. Expand the read-only details to inspect it.');
  } catch(e) {
    const message=e.message.startsWith('Response unavailable.') ? 'Response unavailable. Original semantic artifact read did not complete.' : e.message.replaceAll('no editable snapshot loaded','no original artifact loaded');
    text('semanticArtifactState','Read failed or refused. No original artifact loaded. '+message+' Retry Read original semantic artifact when ready.');
    throw new Error(message);
  }
});
action('readApproved',async () => {
  const expected=clone(state.view.approved_schema);
  text('approvedReadState','Reading immutable approved version from the backend…');notice('');
  try {
    await loadDraft(state.view.draft_id,expected);
    state.approvedReads++;
    text('approvedReadState','Fresh read '+state.approvedReads+' completed · approved version '+expected.version+' · saved revision '+expected.revision+'.');
  } catch(e) {
    text('approvedReadState','Read failed. '+(state.approvedReads ? 'Last successful fresh read: '+state.approvedReads+'.' : 'No successful fresh read in this session.')+' Previously loaded snapshot retained.');
    throw e;
  }
});
action('chooseServerIntent',async () => {await refreshServerIntent();selectOriginal(serverIntent ? 'server' : 'local');});
action('chooseLocalIntent',async () => {selectOriginal('local');});
async function boundedStatus(command) {
  return customerRequest('local-status',{operation:command.operation,command_id:command.command_id,
    fingerprint:await commandFingerprint(command)});
}
async function readMatchingReceipt(status) {
  if (status.state !== 'matching' || status.receipt?.known !== true)
    throw new Error('The matching receipt could not be confirmed. The local original is retained.');
  if (status.receipt.saved) await loadDraft(status.receipt.draft_id,null,status.receipt);
}
async function completeRecovery(value, message) {
  await loadDraft(value.draft_id,null,value);
  await clearKnown(value);
  recoveredCommand=true;
  notice(message);
}
action('discardLocalIntent',async () => {
  if (discardReason === 'server_refusal') {
    if (!window.confirm('Acknowledge the refusal for this exact server original?')) return;
    await clearKnown({journal_tag:serverTag});
    notice('Exact refused server original acknowledged.');
    return;
  }
  const matching=discardReason === 'matching';
  if (!discardReason || !window.confirm(matching ? 'Acknowledge this local copy after verifying the matching receipt again?' :
      'Discard only this local intent after verifying it produced no effect?')) return;
  const original=await freshOriginal(state.pending);
  let safe;
  if (matching) {
    await readMatchingReceipt(await boundedStatus(original));
    await clearKnown({journal_pending:false});
    notice('Matching receipt acknowledged. Only the encrypted local copy was removed.');
    return;
  }
  if (discardReason === 'bounded') {
    safe=['absent','different'].includes((await boundedStatus(original)).state);
  } else {
    const result=await customerRequest('reconcile',{command:original});
    safe=result.category === 'command_conflict' && result.journal_pending === false;
  }
  if (!safe) throw new Error('The authoritative command state changed. Resolve your original again.');
  await vault.remove(browserIntent);browserIntent=null;discardReason='';
  setPending(null);
  if (original.operation === 'review_save' && original.draft_id === state.view?.draft_id &&
      original.expected_revision === state.view.revision && !state.view.approved_schema) {
    for (const field of ['definitions','associations','equipment_review','scenario_review'])
      if (field in original) state.view[field]=clone(original[field]);
    $('reason').value=original.reason;
    markDirty();render();
  }
  notice('Local intent discarded. Review your edits before submitting a new command.');
});
action('resolvePending',async () => {
  const pending=state.pending; if (!pending) return;
  state.retryReady=false;
  let value;
  if (customerMode && localMatches(pending)) {
    discardReason='';
    const original=await freshOriginal(pending);
    if (!fitsLimit(original) || !representableReview(original)) {
      const status=await boundedStatus(original);
      if (status.state === 'matching') {
        await readMatchingReceipt(status);
        discardReason='matching';
        notice('The receipt matches this exact local original. You can acknowledge the encrypted local copy.');
      } else if (['absent','different'].includes(status.state)) {
        discardReason='bounded';
        const evidence=status.state === 'different' ? 'The occupied key belongs to a different body.' : 'No receipt exists at this key.';
        notice(evidence+' This local original had no effect. You can explicitly discard it and correct the edits.');
      } else {
        await refreshServerIntent();
        notice('A server journal or receipt already exists. The local original is retained. Select the server original to inspect it.');
      }
      return;
    }
    value=await customerRequest('reconcile',{command:original});
    if (value.category === 'other_pending') {
      await refreshServerIntent();
      throw new Error('Two original bodies differ. Select the server original to reconcile it. The browser original is retained.');
    }
    if (value.category === 'command_conflict' && value.journal_pending === false) {
      discardReason='conflict';
      notice('The persisted receipt belongs to a different body. This local original had no effect. You may explicitly discard it and correct the edits.');
      return;
    }
  } else {
    value=customerMode ? await customerRequest('resolve',serverSelector(pending.command_id))
      : await command({operation:'review_resolve',namespace:pending.namespace,command_id:pending.command_id});
  }
  if (customerMode && originalSource === 'server' && value.category === 'unrepresentable_command') {
    serverTag=value.journal_tag; discardReason='server_refusal';
    notice('This exact server original contains unsupported text. Reconciliation confirmed it had no effect. Explicitly acknowledge its refusal before correcting it.');
    return;
  }
  if (value.known === false) {state.retryReady=value.category === 'unknown_command' && value.next_action === 'retry_same_key';notice('Outcome remains unknown. Preserve this exact pending command; Retry exact command is an explicit same-key action.'); outcome(value);}
  try { outcome(value); } catch(e) {await clearKnown(value);throw e;}
  await completeRecovery(value,'Pending command resolved from its durable receipt.');
});
action('retryPending',async () => {
  const pending=state.pending; if (!pending || state.authRequired || !state.retryReady || !window.confirm('Retry the exact retained command with the same key and body?')) return;
  state.retryReady=false;
  let value;
  if (customerMode && localMatches(pending)) {
    const original=await freshOriginal(pending);
    value=await customerRequest('reconcile',{command:original});
    if (value.category === 'command_conflict' && value.journal_pending === false) {
      discardReason='conflict';
      notice('A different body owns this receipt. The local original is retained for explicit correction.');
      return;
    }
    if (value.category === 'unknown_command') {
      const staged=await customerRequest('prepare',{command:original});
      value=await customerRequest('retry',{command_id:pending.command_id,tag:staged.journal_tag});
    }
  } else {
    value=customerMode ? await customerRequest('retry',serverSelector(pending.command_id))
      : await command(pending,sessionStorage.getItem(pendingKey()));
  }
  try { outcome(value); } catch(e) {if(value.known !== false) await clearKnown(value);throw e;}
  await completeRecovery(value,'Exact pending command resolved/replayed.');
});
// A browser reload reuses only the HttpOnly session. There is no automatic mutation.
if (customerMode) {
  $('login').hidden=true;
  const back=document.createElement('a');
  back.href='/';back.textContent='Customer dashboard';
  document.querySelector('nav').prepend(back);
}
openWorkspace().catch(e => {error(e.message);});
setInterval(renderSessionState,1000);
window.addEventListener('beforeunload',event => {if(!state.authRequired && (state.dirty || state.pending || state.busy)) {event.preventDefault();event.returnValue='';}});
