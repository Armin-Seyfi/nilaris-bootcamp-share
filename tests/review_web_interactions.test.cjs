// Offline rendered-control/HTTP seam. No server, browser, database or third-party DOM.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../src/nilaris/review_web');
class Element {
  constructor(tag, attrs='', document) {
    this.tagName=tag; this.document=document; this.attrs={}; this.children=[]; this.events={}; this.hidden=false; this.disabled=false; this.value=''; this.textContent='';
    for(const match of attrs.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) this.attrs[match[1]]=match[2] ?? '';
    this.id=this.attrs.id; this.value=this.attrs.value || ''; this.disabled='disabled' in this.attrs; this.hidden='hidden' in this.attrs;
    this.dataset=Object.fromEntries(Object.entries(this.attrs).filter(([key])=>key.startsWith('data-')).map(([key,value])=>[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),value]));
  }
  set innerHTML(html) {this.html=html; this.children=[]; const stack=[this]; for(const match of html.matchAll(/<\/?([\w-]+)\b([^>]*?)>/g)) {if(match[0].startsWith('</')) {if(stack.length>1) stack.pop();continue;} const element=new Element(match[1],match[2],this.document);element.parentElement=stack.at(-1);stack.at(-1).children.push(element);if(!['input','br','hr','meta','link'].includes(match[1])) stack.push(element);} for(const select of this.querySelectorAll('select')) select.value=select.querySelectorAll('option').find(option=>'selected' in option.attrs)?.value || ''; }
  get innerHTML(){return this.html || '';}
  addEventListener(type,fn){(this.events[type] ||= []).push(fn);}
  getAttribute(name){return this.attrs[name] ?? null;}
  setAttribute(name,value){this.attrs[name]=value;}
  querySelectorAll(selector){const all=this.children.flatMap(child=>[child,...child.querySelectorAll('*')]);return selector==='*'?all:all.filter(element=>selector.split(',').some(part=>{part=part.trim();const attr=/^\[([^\]]+)\]$/.exec(part);return attr?attr[1] in element.attrs:element.tagName===part;}));}
  focus(){this.document.activeElement=this;} scrollIntoView(){this.document.scrolledElement=this;} showModal(){this.open=true;} close(){this.open=false;}
  async fire(type,value){if(value!==undefined)this.value=value;if(this.disabled)return;for(let element=this;element;element=element.parentElement) assert.equal(element.hidden,false,`Cannot interact with hidden #${this.id}`);for(const fn of this.events[type] || []) await fn({target:this,preventDefault(){}});}
}
const registry={version:'review-scenarios-v1',scenarios:[{id:'context-a',label:'First context',meaning:'First meaning'},{id:'context-b',label:'Second context',meaning:'Second meaning'}]};
async function workspace({create=false,saveOutcome='saved',approved=false,equipment=false,storage=new Map(),commandHandler=null,deadline=null,now=100000,publicationData=null}={}) {
  const document={}; const page=new Element('body','',document);page.innerHTML=fs.readFileSync(path.join(root,'index.html'),'utf8');
  document.getElementById=id=>page.querySelectorAll('*').find(element=>element.id===id);
  document.querySelectorAll=selector=>selector.split(',').flatMap(part=>{const match=/^#([\w-]+) (.+)$/.exec(part.trim());return match?document.getElementById(match[1])?.querySelectorAll(match[2]) || []:page.querySelectorAll(part);});
  const publication={id:'proposal',kind:'initial',job_id:'job',context:{job_id:'job',product_type:'Pump',document_id:'doc',source_revision:'one'},coverage:{unread:[]},observations:[],associations:[],candidates:[]};
  let saved={known:true,saved:true,draft_id:'draft',revision:1,product_type:'Pump',initial:publication,available_scenario_registry:registry,definitions:[{id:'flow',domain:'technical',label:'Flow',value_type:'decimal',unit:'m3/h',decision:'accepted',observation_ids:[]},{id:'head',domain:'technical',label:'Head',value_type:'decimal',unit:'m',decision:'deferred',observation_ids:[]}],associations:[]};
  if(equipment) saved.equipment_review={version:'equipment-review-v1',included_equipment:'Pump assembly',exclusions:'Motor',categories:['Exact label'],origin:{kind:'human',actor:'Equipment expert',reason:'Recorded wording',source_is_proof_of_edit:false}};
  if(approved) { saved.scenario_review={version:'scenario-review-v1',registry,references:[{attribute_id:'flow',scenario_id:'context-a',requirement:'required',condition:''}]}; saved.approved_schema={...structuredClone(saved),version:1,actor:'Expert'}; }
  if(publicationData) {Object.assign(publication,structuredClone(publicationData));saved.definitions=publication.candidates.map(c=>({...structuredClone(c),decision:'awaiting'}));saved.associations=structuredClone(publication.associations);}
  else publication.candidates=structuredClone(saved.definitions);
  const commands=[],requests=[];let confirm=true,readHandler=null,tick=null;const context={job_id:'job',namespace:'test',base_version:0,...(deadline===null?{}:{ui_deadline_unix_ms:deadline}),...(!create?{draft_id:'draft'}:{})};
  const sandbox={document,Date:{now:()=>now},setInterval:fn=>{tick=fn;},window:{addEventListener(){},confirm:()=>confirm},sessionStorage:{getItem:key=>storage.get(key) ?? null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},crypto:{randomUUID:()=> 'uuid'},fetch:async(url,options)=>{requests.push({url,body:options.body});const body=options.body&&JSON.parse(options.body);if(body) commands.push(body);let value=context;if(url==='/api/command'){const override=commandHandler && await commandHandler(body);if(override)return override;if(body.operation==='result')value={known:true,publications:[publication]};else if(body.operation==='review_resolve')value={known:false,saved:false,category:'unknown_command',next_action:'retry_same_key'};else if(body.operation==='review_save' && saveOutcome==='unknown') value={known:false};else if(body.operation==='review_save' && saveOutcome==='refused') value={known:true,saved:false,category:'validation_error',errors:[{field:'scenario_review.references[0].condition',reason:'Conditional requirement needs nonblank condition text.'}]};else if(body.operation==='review_save'){saved={...saved,...structuredClone(body),revision:saved.revision+1};value={known:true,saved:true,draft_id:'draft',revision:saved.revision};}else if(body.operation==='review_create')value={known:true,saved:true,draft_id:'draft'};else if(body.operation==='review_read' && readHandler)value=await readHandler(structuredClone(saved));else value=saved;}return {ok:true,json:async()=>structuredClone(value)};}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'app.js'),'utf8'),sandbox);
  await new Promise(resolve=>setImmediate(resolve));
  const get=id=>{const element=document.getElementById(id);assert.ok(element,`Rendered control #${id} exists`);return element;};
  get('actor').value='Expert';get('reason').value='Review contexts';
  return {get,commands,requests,tick:value=>{now=value;tick?.();},setRead:handler=>{readHandler=handler;},confirm:value=>{confirm=value;},document};
}
test('explicit configuration starts empty, cell defaults Required, save and reload retain references',async()=>{
  const ui=await workspace();
  assert.match(ui.get('scenarioState').textContent,/not recorded/i);
  await ui.get('configureScenarios').fire('click');
  await ui.get('saveDraft').fire('click');
  assert.deepEqual(ui.commands.findLast(c=>c.operation==='review_save').scenario_review.references,[]);
  await ui.get('cell-0-0').fire('click');
  await ui.get('saveDraft').fire('click');
  assert.deepEqual(ui.commands.findLast(c=>c.operation==='review_save').scenario_review,{version:'scenario-review-v1',registry,references:[{attribute_id:'flow',scenario_id:'context-a',requirement:'required',condition:''}]});
  assert.equal(ui.get('requirement-0').disabled,false,'immediate editing works after save readback');
});
test('conditional text and reference removal survive readback without changing the other reference',async()=>{
  const ui=await workspace();
  await ui.get('configureScenarios').fire('click');
  await ui.get('cell-0-0').fire('click');
  await ui.get('requirement-0').fire('change','conditional');
  await ui.get('condition-0').fire('input','When the nominated specification requires <this>.');
  await ui.get('cell-0-1').fire('click');
  const remove=ui.get('inspector').querySelectorAll('[data-remove-reference]').find(button=>button.dataset.removeReference==='1');
  await remove.fire('click');
  await ui.get('saveDraft').fire('click');
  const saved=ui.commands.findLast(c=>c.operation==='review_save');
  assert.deepEqual(saved.scenario_review.references,[{attribute_id:'flow',scenario_id:'context-a',requirement:'conditional',condition:'When the nominated specification requires <this>.'}]);
  await ui.get('reviewApproval').fire('click');
  assert.match(ui.get('approvalSummary').innerHTML,/First meaning/);
  assert.match(ui.get('approvalSummary').innerHTML,/When the nominated specification requires &lt;this&gt;\./);
  assert.deepEqual(JSON.parse(ui.get('approvalSnapshot').textContent).scenario_review,saved.scenario_review);
});
test('cancelling attribute removal retains edits and references; confirmed defer retains its distinct decision',async()=>{
  const ui=await workspace();
  await ui.get('configureScenarios').fire('click');await ui.get('cell-0-0').fire('click');
  await ui.get('editLabel').fire('input','Expert flow');
  ui.confirm(false);await ui.get('editDecision').fire('change','rejected');
  assert.equal(ui.get('editDecision').value,'accepted');
  ui.confirm(true);await ui.get('editDecision').fire('change','deferred');
  await ui.get('saveDraft').fire('click');
  const saved=ui.commands.findLast(c=>c.operation==='review_save');
  assert.equal(saved.definitions[0].decision,'deferred');assert.equal(saved.definitions[0].label,'Expert flow');
  assert.deepEqual(saved.scenario_review.references,[]);
  await ui.get('suggestedView').fire('click');
  assert.match(ui.get('definitions').innerHTML,/Expert flow/);assert.match(ui.get('definitions').innerHTML,/Leave for later/);
  await ui.get('editDecision').fire('change','accepted');await ui.get('saveDraft').fire('click');
  assert.equal(ui.commands.findLast(c=>c.operation==='review_save').definitions[0].label,'Expert flow');
});
test('legacy save omits scenario assignments and create readback permits immediate edits',async()=>{
  const ui=await workspace({create:true});await ui.get('createDraft').fire('click');
  assert.equal(ui.get('editLabel').disabled,false);
  await ui.get('editLabel').fire('input','Human label');await ui.get('saveDraft').fire('click');
  const saved=ui.commands.findLast(c=>c.operation==='review_save');
  assert.equal(saved.definitions[0].label,'Human label');assert.equal('scenario_review' in saved,false);
});
test('authoritative refusal links the named scenario condition and keeps dirty edits',async()=>{
  const ui=await workspace({saveOutcome:'refused'});await ui.get('configureScenarios').fire('click');await ui.get('cell-0-0').fire('click');
  await ui.get('requirement-0').fire('change','conditional');await ui.get('saveDraft').fire('click');
  await ui.get('suggestedView').fire('click');
  await ui.get('issues').querySelectorAll('[data-backend-item]')[0].fire('click');
  assert.equal(ui.document.activeElement?.id,'condition-0','refusal action focuses the scenario condition in the selected attribute inspector');
  assert.equal(ui.document.scrolledElement?.id,'condition-0');
  assert.match(ui.get('definitions').innerHTML,/Flow/);
  assert.match(ui.get('issues').innerHTML,/Flow · First context: Conditional requirement needs nonblank condition text\./);
  assert.doesNotMatch(ui.get('issues').innerHTML,/scenario_review|references\[/);
  assert.match(ui.get('errorTechnical').textContent,/scenario_review.references\[0\].condition/);
  assert.match(ui.get('errorMessage').textContent,/not recorded/);
  assert.match(ui.get('saveState').textContent,/Unsaved changes/);
  assert.equal(ui.get('condition-0').disabled,false);assert.equal(ui.get('reviewApproval').disabled,true);
});
test('blank condition hint opens the named scenario and clears when expert enters text',async()=>{
  const ui=await workspace();await ui.get('configureScenarios').fire('click');await ui.get('cell-0-1').fire('click');
  await ui.get('requirement-1').fire('change','conditional');
  assert.match(ui.get('issues').innerHTML,/Flow · Second context: enter nonempty condition text before saving\./);
  await ui.get('suggestedView').fire('click');
  await ui.get('issues').querySelectorAll('[data-hint-item]')[0].fire('click');
  assert.equal(ui.document.activeElement?.id,'condition-1');
  assert.equal(ui.document.scrolledElement?.id,'condition-1');
  await ui.get('condition-1').fire('input','When specified by the expert.');
  assert.doesNotMatch(ui.get('issues').innerHTML,/enter nonempty condition text/);
  assert.equal(ui.commands.some(command=>command.operation==='review_save'),false);
});
test('unknown save locks scenario edits and preserves pending-resolution controls',async()=>{
  const ui=await workspace({saveOutcome:'unknown'});await ui.get('configureScenarios').fire('click');await ui.get('cell-0-0').fire('click');await ui.get('saveDraft').fire('click');
  assert.match(ui.get('saveState').textContent,/Outcome unknown/);assert.equal(ui.get('requirement-0').disabled,true);
  assert.equal(ui.get('resolvePending').hidden,false);assert.equal(ui.get('resolvePending').disabled,false);
  const before=ui.commands.length;await ui.get('cell-0-1').fire('click');assert.equal(ui.commands.length,before);
});
test('approved readback locks all definition and scenario fields and includes the snapshot summary',async()=>{
  const ui=await workspace({approved:true});
  for(const id of ['editLabel','editUnit','editDecision','requirement-0','actor','reason'])assert.equal(ui.get(id).disabled,true,id);
  assert.match(ui.get('approvedSummary').innerHTML,/First meaning/);
  assert.equal(JSON.parse(ui.get('approvedSnapshot').textContent).scenario_review.registry.version,'review-scenarios-v1');
});

test('typing condition text updates inline and matrix guidance without replacing the focused textarea',async()=>{
  const ui=await workspace();await ui.get('configureScenarios').fire('click');await ui.get('cell-0-0').fire('click');
  await ui.get('requirement-0').fire('change','conditional');
  const input=ui.get('condition-0');input.focus();
  const hint=ui.get('inspector').querySelectorAll('p').find(p=>p.getAttribute('class')==='validation');
  assert.equal(hint.hidden,false);
  assert.match(ui.get('cell-0-0').getAttribute('class'),/missing/);
  await input.fire('input','When required <plain text>.');
  assert.equal(hint.hidden,true,'inline missing-condition guidance clears as valid text is typed');
  assert.equal(ui.get('condition-0'),input);assert.equal(ui.document.activeElement,input);
  assert.doesNotMatch(ui.get('cell-0-0').getAttribute('class'),/missing/);
  const context=ui.get('definitions').querySelectorAll('p').find(p=>p.getAttribute('class')==='cell-context');
  assert.equal(context.textContent,'When required <plain text>.');
  await input.fire('input','   ');
  assert.equal(hint.hidden,false);assert.match(ui.get('cell-0-0').getAttribute('class'),/missing/);
  assert.equal(context.textContent,'Condition needed');
  assert.equal(ui.get('condition-0'),input);assert.equal(ui.document.activeElement,input);
});
test('scenario status and configuration notice follow live additions and removals',async()=>{
  const ui=await workspace();await ui.get('configureScenarios').fire('click');
  await ui.get('cell-0-0').fire('click');
  assert.match(ui.get('scenarioState').textContent,/1 scenario references/);
  assert.doesNotMatch(ui.get('notice').textContent,/no selections/i);
  await ui.get('inspector').querySelectorAll('[data-remove-reference]')[0].fire('click');
  assert.match(ui.get('scenarioState').textContent,/no scenario selections/i);
  assert.match(ui.get('notice').textContent,/no selections/i);
});
test('adding a manual decimal requires explicit content and edits a source-free stable definition',async()=>{
  const ui=await workspace();await ui.get('addManual').fire('click');
  await ui.get('manualForm').fire('submit');assert.equal(ui.document.activeElement.id,'manualLabel');
  await ui.get('manualLabel').fire('input','Expert <pressure>');
  await ui.get('manualForm').fire('submit');assert.equal(ui.document.activeElement.id,'manualType');
  await ui.get('manualType').fire('change','decimal');
  await ui.get('manualForm').fire('submit');assert.equal(ui.document.activeElement.id,'manualUnit');
  await ui.get('manualUnit').fire('input','literal <bar>');await ui.get('manualForm').fire('submit');
  assert.equal(ui.get('editLabel').value,'Expert &lt;pressure&gt;');
  assert.match(ui.get('inspector').innerHTML,/Human-added · no documentary evidence/);
  assert.match(ui.get('inspector').innerHTML,/Definition unit \(literal\)/);
  assert.match(ui.get('inspector').innerHTML,/Expert-declared unit; no documentary support/);
  assert.match(ui.get('scenarioState').textContent,/not recorded/i);
  await ui.get('editLabel').fire('input','Renamed pressure');await ui.get('saveDraft').fire('click');
  const save=ui.commands.findLast(c=>c.operation==='review_save'),manual=save.definitions.at(-1);
  assert.deepEqual(manual,{id:'manual:uuid',definition_kind:'human_manual_v1',label:'Renamed pressure',value_type:'decimal',unit:'literal <bar>',requirements:'unspecified',conditions:'unspecified',observation_ids:[],decision:'accepted'});
  assert.equal(save.associations.length,0);assert.equal('scenario_review' in save,false);
  await ui.get('editUnit').fire('input','edited token');await ui.get('saveDraft').fire('click');
  assert.equal(ui.commands.findLast(c=>c.operation==='review_save').definitions.at(-1).id,manual.id);
  await ui.get('reviewApproval').fire('click');assert.match(ui.get('approvalSummary').innerHTML,/Human-added · no documentary evidence/);
});
test('manual text exclusion cancels atomically and restoration retains identity with reassessment guidance',async()=>{
  const ui=await workspace();await ui.get('configureScenarios').fire('click');
  await ui.get('addManual').fire('click');await ui.get('manualLabel').fire('input','Flow');
  await ui.get('manualType').fire('change','text');assert.equal(ui.get('manualUnit').disabled,true);
  await ui.get('manualForm').fire('submit');assert.equal(ui.get('editUnit').disabled,true);
  await ui.get('cell-2-0').fire('click');await ui.get('requirement-0').fire('change','conditional');await ui.get('condition-0').fire('input','Expert condition');
  await ui.get('editLabel').fire('input','Expert text');ui.confirm(false);await ui.get('removeManual').fire('click');
  assert.equal(ui.get('editDecision').value,'accepted');assert.equal(ui.get('condition-0').value,'Expert condition');
  await ui.get('saveDraft').fire('click');let save=ui.commands.findLast(c=>c.operation==='review_save');
  assert.equal(save.scenario_review.references[0].condition,'Expert condition');
  ui.confirm(true);await ui.get('removeManual').fire('click');await ui.get('saveDraft').fire('click');
  save=ui.commands.findLast(c=>c.operation==='review_save');assert.equal(save.definitions[2].decision,'rejected');assert.deepEqual(save.scenario_review.references,[]);
  await ui.get('restoreManual').fire('click');assert.match(ui.get('notice').textContent,/reassess.*scenario references/i);
  await ui.get('saveDraft').fire('click');save=ui.commands.findLast(c=>c.operation==='review_save');
  assert.deepEqual(save.definitions[2],{id:'manual:uuid',definition_kind:'human_manual_v1',label:'Expert text',value_type:'text',unit:null,requirements:'unspecified',conditions:'unspecified',observation_ids:[],decision:'accepted'});
  assert.deepEqual(save.scenario_review.references,[]);assert.equal(save.definitions[0].id,'flow');
  await ui.get('editDecision').fire('change','deferred');await ui.get('saveDraft').fire('click');assert.equal(ui.commands.findLast(c=>c.operation==='review_save').definitions[2].decision,'deferred');
});
test('approved reads visibly wait for authoritative immutable matches, distinguish success and preserve last success on failure',async()=>{
  const ui=await workspace({approved:true});let release;
  ui.setRead(saved=>new Promise(resolve=>{release=()=>resolve(saved);}));
  const reading=ui.get('readApproved').fire('click');
  assert.match(ui.get('approvedReadState').textContent,/Reading.*backend/i);
  assert.equal(ui.get('readApproved').disabled,true);assert.equal(ui.get('addManual').disabled,true);
  release();await reading;
  assert.match(ui.get('approvedReadState').textContent,/Fresh read 1.*version 1.*revision 1/i);
  ui.setRead(saved=>saved);await ui.get('readApproved').fire('click');
  assert.match(ui.get('approvedReadState').textContent,/Fresh read 2/i);
  ui.setRead(()=>{throw new Error('offline');});await ui.get('readApproved').fire('click');
  assert.match(ui.get('approvedReadState').textContent,/Read failed.*last successful.*2/i);
  assert.doesNotMatch(ui.get('notice').textContent,/read back/i);
  ui.setRead(saved=>{saved.approved_schema.definitions[0].label='Forged changed version';return saved;});await ui.get('readApproved').fire('click');
  assert.match(ui.get('approvedReadState').textContent,/Read failed.*last successful.*2/i);
  assert.doesNotMatch(ui.get('approvedSummary').innerHTML,/Forged changed version/);
});
test('manual entries are marked in the matrix and retained excluded approval history',async()=>{
  const ui=await workspace();await ui.get('addManual').fire('click');await ui.get('manualLabel').fire('input','Manual material');await ui.get('manualType').fire('change','text');await ui.get('manualForm').fire('submit');
  assert.match(ui.get('definitions').innerHTML,/Human-added · no documentary evidence/);
  await ui.get('removeManual').fire('click');assert.match(ui.get('definitions').innerHTML,/Human-added · no documentary evidence/);
  await ui.get('saveDraft').fire('click');await ui.get('reviewApproval').fire('click');
  assert.match(ui.get('approvalSummary').innerHTML,/Manual material: Exclude attribute.*Human-added · no documentary evidence/);
});

test('equipment metadata requires explicit empty configuration and survives exact saved approval summary',async()=>{
  const ui=await workspace();
  assert.match(ui.get('equipmentState').textContent,/not recorded/i);
  await ui.get('editLabel').fire('input','Expert flow');await ui.get('saveDraft').fire('click');
  assert.equal('equipment_review' in ui.commands.findLast(c=>c.operation==='review_save'),false);
  await ui.get('configureEquipment').fire('click');
  assert.equal(ui.get('equipmentDetails').hidden,false);
  assert.equal(ui.document.activeElement?.id,'includedEquipment');
  assert.equal(ui.get('includedEquipment').value,'');assert.equal(ui.get('equipmentExclusions').value,'');
  await ui.get('saveDraft').fire('click');
  assert.deepEqual(ui.commands.findLast(c=>c.operation==='review_save').equipment_review,{version:'equipment-review-v1',included_equipment:'',exclusions:'',categories:[]});
  await ui.get('toggleEquipment').fire('click');
  await ui.get('includedEquipment').fire('input','  Pump <assembly>\nExact wording  ');
  await ui.get('equipmentExclusions').fire('input','Motor excluded; no applicability rule.');
  await ui.get('categoryLabel').fire('input','  Exact label  ');await ui.get('addCategory').fire('click');
  await ui.get('categoryLabel').fire('input','Utilities');await ui.get('addCategory').fire('click');
  await ui.get('saveDraft').fire('click');
  const save=ui.commands.findLast(c=>c.operation==='review_save');
  assert.deepEqual(save.equipment_review,{version:'equipment-review-v1',included_equipment:'  Pump <assembly>\nExact wording  ',exclusions:'Motor excluded; no applicability rule.',categories:['  Exact label  ','Utilities']});
  assert.equal(save.definitions[0].unit,'m3/h');
  await ui.get('reviewApproval').fire('click');
  assert.deepEqual(JSON.parse(ui.get('approvalSnapshot').textContent).equipment_review,save.equipment_review);
  assert.match(ui.get('approvalSummary').innerHTML,/Pump &lt;assembly&gt;/);
  assert.match(ui.get('approvalSummary').innerHTML,/Motor excluded/);
  assert.match(ui.get('approvalSummary').innerHTML,/Exact label/);
  assert.match(ui.get('equipmentDisclaimer').textContent,/human|expert/i);
});

test('matrix label search, ordering and scenario predicate retain edits and explain filtered inspector',async()=>{
  const ui=await workspace();
  await ui.get('configureScenarios').fire('click');await ui.get('cell-0-0').fire('click');
  await ui.get('editLabel').fire('input','Edited flow');
  await ui.get('matrixSearch').fire('input','Head');
  assert.match(ui.get('definitions').innerHTML,/No matching attributes/);
  assert.match(ui.get('inspectorContext').textContent,/outside.*view/i);
  assert.equal(ui.get('editLabel').value,'Edited flow');
  assert.equal(ui.get('selectedView').textContent,'Selected (0 of 1)');
  await ui.get('suggestedView').fire('click');assert.match(ui.get('definitions').innerHTML,/Head/);
  assert.equal(ui.get('suggestedView').textContent,'Suggestions (1 of 1)');
  await ui.get('matrixSearch').fire('input','m3/h');assert.match(ui.get('definitions').innerHTML,/No matching attributes/);
  await ui.get('clearMatrixFilters').fire('click');await ui.get('selectedView').fire('click');
  await ui.get('addManual').fire('click');await ui.get('manualLabel').fire('input','A manual');await ui.get('manualType').fire('change','text');await ui.get('manualForm').fire('submit');
  await ui.get('matrixSort').fire('change','name');
  assert.ok(ui.get('definitions').innerHTML.indexOf('A manual')<ui.get('definitions').innerHTML.indexOf('Edited flow'));
  await ui.get('matrixSort').fire('change','priority');
  assert.ok(ui.get('definitions').innerHTML.indexOf('Edited flow')<ui.get('definitions').innerHTML.indexOf('A manual'));
  await ui.get('matrixScenario').fire('change','context-a');await ui.get('matrixRequirement').fire('change','required');
  assert.match(ui.get('definitions').innerHTML,/Edited flow/);assert.doesNotMatch(ui.get('definitions').innerHTML,/A manual/);
  assert.match(ui.get('inspectorContext').textContent,/outside.*view/i);
  await ui.get('matrixRequirement').fire('change','conditional');assert.match(ui.get('definitions').innerHTML,/No matching attributes/);
  await ui.get('clearMatrixFilters').fire('click');assert.match(ui.get('definitions').innerHTML,/A manual/);
  await ui.get('saveDraft').fire('click');const save=ui.commands.findLast(c=>c.operation==='review_save');
  assert.equal(save.definitions[0].label,'Edited flow');assert.equal(save.scenario_review.references.length,1);
  assert.equal('matrixSearch' in save,false);assert.equal('matrixSort' in save,false);
});

test('equipment fields lock during busy, unknown and immutable approved readback and preserve provenance',async()=>{
  const pending=await workspace({equipment:true,saveOutcome:'unknown'});
  await pending.get('toggleEquipment').fire('click');
  await pending.get('equipmentExclusions').fire('input','Exact changed exclusion');await pending.get('saveDraft').fire('click');
  for(const id of ['includedEquipment','equipmentExclusions','categoryLabel','addCategory'])assert.equal(pending.get(id).disabled,true,id);
  const ui=await workspace({equipment:true,approved:true});
  for(const id of ['includedEquipment','equipmentExclusions','categoryLabel','addCategory'])assert.equal(ui.get(id).disabled,true,id);
  assert.equal(ui.get('configureEquipment').hidden,true);
  assert.match(ui.get('approvedSummary').innerHTML,/Equipment expert/);
  assert.match(ui.get('approvedSummary').innerHTML,/Recorded wording/);
  assert.equal(JSON.parse(ui.get('approvedSnapshot').textContent).equipment_review.origin.source_is_proof_of_edit,false);
  ui.setRead(saved=>{saved.approved_schema.equipment_review.exclusions='Different immutable wording';return saved;});
  await ui.get('readApproved').fire('click');
  assert.match(ui.get('approvedReadState').textContent,/Read failed/);
  assert.equal(JSON.parse(ui.get('approvedSnapshot').textContent).equipment_review.exclusions,'Motor');
  const editable=await workspace({equipment:true});let release;
  await editable.get('toggleEquipment').fire('click');
  await editable.get('includedEquipment').fire('input','Edited equipment');
  editable.setRead(saved=>new Promise(resolve=>{release=()=>resolve(saved);}));
  const saving=editable.get('saveDraft').fire('click');
  await new Promise(resolve=>setImmediate(resolve));
  for(const id of ['includedEquipment','equipmentExclusions','categoryLabel','addCategory'])assert.equal(editable.get(id).disabled,true,id);
  release();await saving;assert.equal(editable.get('includedEquipment').disabled,false);
});

test('Suggestions ignore selected scenario predicates while retaining independent label search',async()=>{
  const ui=await workspace();await ui.get('configureScenarios').fire('click');
  await ui.get('matrixScenario').fire('change','context-a');await ui.get('matrixRequirement').fire('change','required');
  assert.match(ui.get('definitions').innerHTML,/No matching attributes/);
  await ui.get('suggestedView').fire('click');
  assert.match(ui.get('definitions').innerHTML,/Head/);
  assert.equal(ui.get('suggestedView').textContent,'Suggestions (1)');
  assert.equal(ui.get('matrixScenario').disabled,true);assert.equal(ui.get('matrixRequirement').disabled,true);
  await ui.get('matrixSearch').fire('input','Flow');assert.match(ui.get('definitions').innerHTML,/No matching attributes/);
  assert.equal(ui.get('suggestedView').textContent,'Suggestions (0 of 1)');
  await ui.get('matrixSearch').fire('input','Head');assert.match(ui.get('definitions').innerHTML,/Head/);
  await ui.get('selectedView').fire('click');assert.equal(ui.get('matrixScenario').disabled,false);
  assert.equal(ui.get('matrixScenario').value,'context-a');assert.equal(ui.get('matrixRequirement').value,'required');
});


test('equipment disclosure stays compact and retains dirty wording and pending category while toggling',async()=>{
  const ui=await workspace({equipment:true});
  assert.equal(ui.get('equipmentDetails').hidden,true,'saved equipment starts collapsed');
  assert.equal(ui.get('toggleEquipment').getAttribute('aria-expanded'),'false');
  await ui.get('toggleEquipment').fire('click');
  assert.equal(ui.get('equipmentDetails').hidden,false);
  assert.equal(ui.document.activeElement?.id,'includedEquipment');
  const wording=ui.get('includedEquipment');
  await wording.fire('input','Live expert wording');
  assert.equal(ui.get('includedEquipment'),wording,'typing retains the focused textarea');
  assert.equal(ui.document.activeElement,wording);
  await ui.get('categoryLabel').fire('input','Pending label');
  assert.doesNotMatch(ui.get('equipmentEditor').innerHTML,/<h4>Included equipment|Pump assembly<\/p>/,'no stale duplicate preview');
  await ui.get('toggleEquipment').fire('click');
  assert.equal(ui.document.activeElement?.id,'toggleEquipment');
  await ui.get('matrixSearch').fire('input','Flow');
  await ui.get('toggleEquipment').fire('click');
  await ui.get('addCategory').fire('click');
  await ui.get('saveDraft').fire('click');
  const save=ui.commands.findLast(c=>c.operation==='review_save');
  assert.equal(save.equipment_review.included_equipment,'Live expert wording');
  assert.deepEqual(save.equipment_review.categories,['Exact label','Pending label']);
  assert.equal(ui.get('equipmentDetails').hidden,true,'save readback collapses equipment');
  await ui.get('toggleEquipment').fire('click');
  await ui.get('reloadDraft').fire('click');
  assert.equal(ui.get('equipmentDetails').hidden,true,'reload stays compact');
});


test('edited candidate units are definition wording and leave original source excerpts intact',async()=>{
  const ui=await workspace();
  ui.setRead(saved=>{
    saved.initial.observations=[{id:'flow-source',original_content:'Source flow 10 m3/h',value:'10',unit:'m3/h',document_id:'doc',locator:{}}];
    saved.definitions[0].observation_ids=['flow-source'];
    return saved;
  });
  await ui.get('reloadDraft').fire('click');
  assert.match(ui.get('inspector').innerHTML,/<blockquote>Source flow 10 m3\/h<\/blockquote>/);
  await ui.get('editUnit').fire('input','expert literal');
  await ui.get('matrixSearch').fire('input','Flow');
  assert.match(ui.get('inspector').innerHTML,/Definition unit \(literal\)/);
  assert.doesNotMatch(ui.get('inspector').innerHTML,/Unit as written in source/);
  assert.match(ui.get('inspector').innerHTML,/Original source units remain in the excerpts below/);
  assert.equal(ui.get('editUnit').value,'expert literal');
  assert.match(ui.get('inspector').innerHTML,/<blockquote>Source flow 10 m3\/h<\/blockquote>/);
  assert.match(ui.get('inspector').innerHTML,/Original value: <strong>10<\/strong> m3\/h/);
});

test('live label feedback preserves the active input and selection while updating matrix and inspector',async()=>{
  const ui=await workspace();const input=ui.get('editLabel');input.focus();input.selectionStart=2;input.selectionEnd=4;
  await input.fire('input','Edited <flow>');
  assert.equal(ui.get('selectedTitle').textContent,'Edited <flow>');
  assert.equal(ui.get('definitions').querySelectorAll('[data-select]')[0].textContent,'Edited <flow>');
  assert.equal(ui.get('editLabel'),input);assert.equal(ui.document.activeElement,input);
  assert.equal(input.selectionStart,2);assert.equal(input.selectionEnd,4);
  await input.fire('input','');assert.match(ui.get('issues').innerHTML,/Included attribute needs a name/);
  assert.equal(ui.get('reviewApproval').disabled,true);assert.equal(ui.commands.some(c=>c.operation==='review_save'),false);
});

test('collapsed equipment shows bounded wording with full exact text available on demand',async()=>{
  const ui=await workspace({equipment:true});assert.equal(ui.get('equipmentDetails').hidden,true);
  assert.match(ui.get('equipmentState').textContent,/Included: Pump assembly.*Exclusions: Motor.*Categories: Exact label/);
  await ui.get('toggleEquipment').fire('click');const input=ui.get('includedEquipment');input.focus();
  await input.fire('input','<pump> '+ 'x'.repeat(400));
  assert.equal(ui.get('includedEquipment'),input);assert.equal(ui.document.activeElement,input);
  assert.ok(ui.get('equipmentState').textContent.length<500);assert.match(ui.get('equipmentState').textContent,/<pump>.*…/);
  await ui.get('toggleEquipment').fire('click');assert.equal(ui.get('equipmentDetails').hidden,true);
  await ui.get('toggleEquipment').fire('click');assert.equal(ui.get('includedEquipment').value.length,407);
  const legacy=await workspace();assert.match(legacy.get('equipmentState').textContent,/not recorded/);
});

test('prominent command status retains unknown recovery across session reload without replay',async()=>{
  const storage=new Map();const ui=await workspace({saveOutcome:'unknown',storage});
  await ui.get('editLabel').fire('input','Pending label');await ui.get('saveDraft').fire('click');
  assert.equal(ui.get('commandStatus').getAttribute('role'),'status');
  assert.match(ui.get('commandState').textContent,/Outcome unknown.*exact pending command/i);
  assert.equal(ui.get('resolvePending').parentElement,ui.get('commandStatus'));
  assert.equal(ui.get('retryPending').parentElement,ui.get('commandStatus'));
  assert.equal(ui.get('commandStatus').parentElement,ui.get('workspace').parentElement);
  const pending=ui.commands.find(c=>c.operation==='review_save');
  const reloaded=await workspace({storage});
  assert.match(reloaded.get('commandState').textContent,/retained.*unknown/i);
  assert.equal(reloaded.get('editLabel').disabled,true);
  assert.equal(reloaded.commands.some(c=>c.operation==='review_save'),false);
  await reloaded.get('resolvePending').fire('click');assert.match(reloaded.get('commandState').textContent,/unknown/i);
  await reloaded.get('retryPending').fire('click');
  assert.deepEqual(reloaded.commands.find(c=>c.operation==='review_save'),pending);
  assert.match(reloaded.get('commandState').textContent,/Recovered/i);
  assert.equal(storage.size,0);
});

test('approval in flight has modal status and an unavailable response retains exact recovery',async()=>{
  let release;const ui=await workspace({commandHandler:body=>body.operation==='review_approve' ? new Promise(resolve=>{release=()=>resolve({ok:false,status:401,json:async()=>({category:'unauthorized'})});}) : null});
  await ui.get('reviewApproval').fire('click');ui.get('explicitApproval').checked=true;await ui.get('explicitApproval').fire('change');
  const approving=ui.get('confirmApproval').fire('click');
  assert.equal(ui.get('approvalDialog').open,true);assert.match(ui.get('approvalCommandState').textContent,/Working/);
  assert.equal(ui.get('confirmApproval').disabled,true);
  release();await approving;
  assert.equal(ui.get('approvalDialog').open,false);
  assert.match(ui.get('commandState').textContent,/Outcome unknown.*Session unavailable.*exact pending command/i);
  assert.equal(ui.get('resolvePending').disabled,true);assert.equal(ui.get('login').hidden,false);assert.equal(ui.get('editLabel').disabled,true);
  assert.equal(ui.commands.filter(c=>c.operation==='review_approve').length,1);
  assert.equal(ui.commands.some(c=>c.operation==='review_resolve'),false);
  assert.doesNotMatch(ui.get('commandState').textContent,/expired|approved version/i);
});
test('authoritative refusal is prominent without claiming unknown or losing unsaved edits',async()=>{
  const ui=await workspace({saveOutcome:'refused'});await ui.get('editLabel').fire('input','Refused edit');await ui.get('saveDraft').fire('click');
  assert.match(ui.get('commandState').textContent,/failed or refused.*not recorded/i);
  assert.equal(ui.get('retryPending').hidden,true);assert.equal(ui.get('editLabel').value,'Refused edit');
  assert.equal(ui.get('editLabel').disabled,false);assert.equal(ui.get('reviewApproval').disabled,true);
});

test('unavailable fresh approved read gives service guidance and retains the last confirmed snapshot',async()=>{
  for(const category of ['transport_unavailable','persistence_unavailable']) {
    const ui=await workspace({approved:true});
    await ui.get('readApproved').fire('click');
    ui.setRead(()=>({known:false,category}));
    await ui.get('readApproved').fire('click');
    assert.match(ui.get('commandState').textContent,/Local backend unavailable.*until the local service is ready/i);
    assert.doesNotMatch(ui.get('commandState').textContent,/expired|transport_unavailable|persistence_unavailable/i);
    assert.match(ui.get('approvedReadState').textContent,/Read failed.*Last successful fresh read: 1.*snapshot retained/i);
    assert.equal(ui.get('selectedTitle').textContent,'Flow');
    assert.equal(ui.get('editLabel').disabled,true);
    assert.equal(ui.get('readApproved').disabled,false);
    assert.equal(ui.commands.some(c=>c.operation==='review_approve'),false);
  }
});

test('unavailable mutation and resolution retain the exact pending command with service guidance',async()=>{
  for(const category of ['transport_unavailable','persistence_unavailable']) {
    const storage=new Map();
    const ui=await workspace({storage,commandHandler:body=>['review_save','review_resolve'].includes(body.operation) ? {ok:true,json:async()=>({known:false,category})} : null});
    await ui.get('editLabel').fire('input','Pending service recovery');
    await ui.get('saveDraft').fire('click');
    const pending=ui.commands.find(c=>c.operation==='review_save');
    const journal=storage.get('nilaris-review-pending:job');
    for(const action of [null,'resolvePending','retryPending']) {
      if(action) await ui.get(action).fire('click');
      assert.match(ui.get('commandState').textContent,/Outcome unknown.*Local backend unavailable.*exact pending command/i);
      assert.equal(storage.get('nilaris-review-pending:job'),journal);
      assert.equal(ui.get('editLabel').disabled,true);
      assert.equal(ui.get('editLabel').value,'Pending service recovery');
      assert.equal(ui.get('resolvePending').disabled,false);
      assert.equal(ui.get('retryPending').disabled,true);
      assert.doesNotMatch(ui.get('commandState').textContent,/expired|Recovered|not recorded/i);
    }
    assert.deepEqual(ui.commands.filter(c=>c.operation==='review_save'),[pending]);
    assert.equal(ui.commands.filter(c=>c.operation==='review_resolve').length,1);
    const reloaded=await workspace({storage});
    assert.equal(storage.get('nilaris-review-pending:job'),journal);
    assert.match(reloaded.get('commandState').textContent,/retained.*unknown/i);
    assert.equal(reloaded.get('editLabel').disabled,true);
    assert.equal(reloaded.commands.some(c=>c.operation==='review_save'),false);
  }
});

 test('known UI deadline warns in primary area and approval without inferring guard life',async()=>{
 const ui=await workspace({deadline:200000});
 assert.match(ui.get('sessionState').textContent,/100 seconds.*UI.*Guard.*unknown/i);
 await ui.get('reviewApproval').fire('click');
 assert.match(ui.get('approvalSessionState').textContent,/warning.*100 seconds/i);
 ui.tick(201000);
 assert.match(ui.get('sessionState').textContent,/UI deadline reached.*outcome.*unknown/i);
 assert.match(ui.get('approvalSessionState').textContent,/UI deadline reached/i);
 assert.equal(ui.commands.some(c=>c.operation==='review_approve'),false);
 });

test('401 exposes same-tab sign-in, preserves journal, and requires resolve before exact explicit retry',async()=>{
 const storage=new Map();let refused=true;
 const ui=await workspace({storage,commandHandler:body=>body.operation==='review_save' && refused ? {ok:false,status:401,json:async()=>{throw Error('invalid response');}} : null});
 await ui.get('editLabel').fire('input','Preserve <exact>');await ui.get('saveDraft').fire('click');
 const journal=storage.get('nilaris-review-pending:job');
 assert.match(ui.get('commandState').textContent,/Sign in.*same tab/i);
 assert.equal(ui.get('login').hidden,false);assert.equal(ui.get('retryPending').disabled,true);
 assert.equal(ui.document.scrolledElement?.id,'login','401 error rendering keeps the exposed sign-in form in view');
 assert.equal(ui.document.activeElement?.id,'token','401 error rendering leaves the sign-in token ready for input');
 ui.get('token').value='synthetic-token';await ui.get('loginForm').fire('submit');
 assert.equal(ui.get('token').value,'');assert.equal(storage.get('nilaris-review-pending:job'),journal);
 assert.equal(ui.get('login').hidden,true);assert.equal(ui.get('editLabel').disabled,true);
 assert.equal(ui.get('retryPending').disabled,true);
 assert.equal(ui.commands.filter(c=>c.operation==='review_save').length,1);
 refused=false;await ui.get('resolvePending').fire('click');
 assert.equal(storage.get('nilaris-review-pending:job'),journal);assert.equal(ui.get('retryPending').disabled,false);
 await ui.get('retryPending').fire('click');
 assert.equal(ui.requests.filter(r=>r.url==='/api/command' && r.body?.includes('review_save')).at(-1).body,journal);
 assert.equal(storage.size,0);assert.match(ui.get('commandState').textContent,/Recovered/);
});
test('transport and parse failures retain uncertainty without asserting authentication refusal',async()=>{
 for(const failure of [()=>{throw Error('offline');},()=>({ok:true,json:async()=>{throw Error('parse');}})]) {
 const storage=new Map();const ui=await workspace({storage,commandHandler:body=>body.operation==='review_save'?failure():null});
 await ui.get('editLabel').fire('input','Pending');await ui.get('saveDraft').fire('click');
 assert.match(ui.get('commandState').textContent,/Outcome unknown.*Response unavailable/i);
 assert.doesNotMatch(ui.get('commandState').textContent,/Sign in|expired/i);
 assert.equal(ui.get('login').hidden,true);assert.equal(ui.get('editLabel').disabled,true);
 assert.equal(ui.document.scrolledElement?.id,'error','ordinary unavailable responses keep their error guidance in view');
 assert.ok(storage.get('nilaris-review-pending:job'));
 }
});

test('committed receipt is resolved read-only and failed readback retains the exact journal',async()=>{
 const pending={operation:'review_save',namespace:'test',command_id:'saved-elsewhere',draft_id:'draft',expected_revision:0};
 const journal=JSON.stringify(pending,null,2),storage=new Map([['nilaris-review-pending:job',journal]]);
 const ui=await workspace({storage,commandHandler:body=>body.operation==='review_resolve'?{ok:true,json:async()=>({known:true,saved:true,draft_id:'draft',revision:1})}:null});
 ui.setRead(()=>{throw Error('offline during authoritative read');});
 await ui.get('resolvePending').fire('click');
 assert.equal(storage.get('nilaris-review-pending:job'),journal);
 assert.equal(ui.get('editLabel').disabled,true);assert.doesNotMatch(ui.get('commandState').textContent,/Recovered/);
 ui.setRead(saved=>saved);await ui.get('resolvePending').fire('click');
 assert.equal(storage.size,0);assert.match(ui.get('commandState').textContent,/Recovered/);
 assert.equal(ui.commands.some(c=>c.operation==='review_save'),false);
});
test('receipt recovery reads the exact saved revision before an explicit reload of newer work',async()=>{
 const pending={operation:'review_save',namespace:'test',command_id:'saved-elsewhere',draft_id:'draft',expected_revision:1};
 const journal=JSON.stringify(pending),storage=new Map([['nilaris-review-pending:job',journal]]);
 let snapshot;
 const ui=await workspace({storage,commandHandler:body=>{
   if(body.operation==='review_resolve') return {ok:true,json:async()=>({known:true,saved:true,draft_id:'draft',revision:2})};
   if(body.operation==='review_read' && snapshot) return {ok:true,json:async()=>({...structuredClone(snapshot),revision:body.revision ?? 3,definitions:snapshot.definitions.map(d=>({...d,label:body.revision === 2 ? 'Exact saved label' : 'Newer label'}))})};
 }});
 // First simulate a backend ignoring the exact selector; the second read honors it.
 ui.setRead(saved=>{snapshot=saved;return {...saved,revision:3};});
 await ui.get('resolvePending').fire('click');
 assert.equal(ui.commands.findLast(c=>c.operation==='review_read').revision,2);
 assert.equal(storage.get('nilaris-review-pending:job'),journal,'a different revision cannot discharge the receipt');
 assert.doesNotMatch(ui.get('commandState').textContent,/Recovered/);
 await ui.get('resolvePending').fire('click');
 assert.equal(storage.size,0);assert.equal(ui.get('editLabel').value,'Exact saved label');
 await ui.get('reloadDraft').fire('click');assert.equal(ui.get('editLabel').value,'Newer label');
 assert.equal(ui.get('revisionState').textContent,'Draft r3 · unapproved');
 assert.equal(ui.commands.some(c=>c.operation==='review_save'),false);
});
test('retry permission is consumed before every retry or resolution, including unavailable responses',async()=>{
 for(const operation of ['review_save','review_resolve']) for(const failure of ['network','parse']) {
   const pending={operation:'review_save',namespace:'test',command_id:'same-key',draft_id:'draft',expected_revision:1};
   const journal=JSON.stringify(pending,null,2),storage=new Map([['nilaris-review-pending:job',journal]]);
   let fail=false,release;
   const ui=await workspace({storage,commandHandler:body=>{
     if(fail && body.operation===operation) return new Promise((resolve,reject)=>{release=()=>failure==='network' ? reject(Error('offline')) : resolve({ok:true,json:async()=>{throw Error('parse');}});});
   }});
   await ui.get('resolvePending').fire('click');assert.equal(ui.get('retryPending').disabled,false);
   fail=true;
   const attempt=ui.get(operation==='review_save'?'retryPending':'resolvePending').fire('click');
   assert.equal(ui.get('retryPending').disabled,true);
   release();await attempt;
   assert.equal(ui.get('retryPending').disabled,true,`${operation} ${failure} needs a fresh absent-receipt resolution`);
   assert.equal(storage.get('nilaris-review-pending:job'),journal);
   const before=ui.commands.length;await ui.get('retryPending').fire('click');assert.equal(ui.commands.length,before);
   fail=false;await ui.get('resolvePending').fire('click');assert.equal(ui.get('retryPending').disabled,false);
   await ui.get('retryPending').fire('click');
   assert.equal(ui.requests.filter(r=>r.body?.includes('review_save')).at(-1).body,journal);
   assert.equal(ui.commands.findLast(c=>c.operation==='review_read').revision,2,'retry receipt readback selects its exact saved revision');
   assert.equal(storage.size,0);assert.match(ui.get('commandState').textContent,/Recovered/);
 }
});
test('resolved approval needs its matching immutable version before reporting recovery',async()=>{
 const storage=new Map([['nilaris-review-pending:job',JSON.stringify({operation:'review_approve',namespace:'test',command_id:'approval',draft_id:'draft',expected_revision:1})]]);
 const ui=await workspace({storage,commandHandler:body=>body.operation==='review_resolve'?{ok:true,json:async()=>({known:true,saved:true,approved:true,draft_id:'draft',revision:1,approved_version:'immutable-id'})}:null});
 await ui.get('resolvePending').fire('click');
 assert.equal(storage.size,1);assert.equal(ui.get('editLabel').disabled,true);
 assert.match(ui.get('commandState').textContent,/matching.*receipt/i);assert.doesNotMatch(ui.get('commandState').textContent,/Recovered/);
 ui.setRead(saved=>({...saved,approved_schema:{id:'immutable-id',version:1,revision:1,definitions:saved.definitions,associations:[],actor:'Expert'}}));
 await ui.get('resolvePending').fire('click');
 assert.equal(storage.size,0);assert.match(ui.get('commandState').textContent,/Recovered/);
 assert.equal(ui.get('editLabel').disabled,true);
 assert.equal(ui.commands.some(c=>c.operation==='review_approve'),false);
});

function semanticToy() {
  const source_ref={document_id:'toy',source_revision:'r1',locator_id:'L1'};
  return {adapter:'semantic-projection-v1',stage_contract:'semantic-projection-v1',context:{product_type:'Pump',document_id:'toy',source_revision:'r1'},
    observations:[{id:'o1',label:'Rated pressure',value:'12.00',unit:'bar',original_content:'Rated pressure: 12.00 bar',presence:'observed',source_ref,content_pin:'a'.repeat(64),locator:{text:'line 1'}}],
    candidates:[{id:'c1',domain:'technical',label:'Rated pressure',value_type:'decimal',unit:'bar',observation_ids:['o1'],requirements:'unspecified'}],associations:[],
    semantic_context:{mapping_version:'semantic-projection-v1',origin:{artifact:'b'.repeat(64),capture:'c'.repeat(64),configuration:'d'.repeat(64)},target:{document_id:'toy',source_revision:'r1',content_pin:'a'.repeat(64)},original_status:'completed',
      documents:[{document_id:'toy',source_revision:'r1',content_pin:'a'.repeat(64),coverage:{inspected:['line 1'],unread:[]},spans:[{locator_id:'L1',text:'Rated pressure: 12.00 bar',locator:{text:'line 1'}}]}],
      original_artifact:{status:'completed',retained:'raw toy artifact'},record_mapping:[{kind:'candidate',id:'c1',disposition:'represented',reason:null,projected_ids:['c1']}],read_only_records:[]}};
}
test('semantic proposal labels its origin and operational scope without implying quality',async()=>{
  const ui=await workspace({publicationData:semanticToy()});
  assert.match(ui.get('publicationOrigin').textContent,/Semantic experiment/);
  assert.doesNotMatch(ui.get('publicationOrigin').textContent,/fixture/i);
  assert.match(ui.get('coverage').textContent,/completed/);
  assert.match(ui.get('coverage').textContent,/semantic quality/i);
  assert.match(ui.get('semanticInventory').innerHTML,/toy/);
  assert.match(ui.get('semanticInventory').innerHTML,/line 1/);
  await ui.get('suggestedView').fire('click');
  assert.match(ui.get('definitions').innerHTML,/Not decided/);
  await ui.get('editDecision').fire('change','accepted');await ui.get('saveDraft').fire('click');
  await ui.get('reviewApproval').fire('click');
  assert.doesNotMatch(ui.get('approvalSummary').innerHTML,/Synthetic fixture/);
  assert.match(ui.get('approvalSummary').innerHTML,/Semantic experiment/);
});
test('read-only semantic records retain reasons and directed document-qualified context without edit controls',async()=>{
  const data=semanticToy(),semantic=data.semantic_context;
  const external={document_id:'family',source_revision:'r9',locator_id:'X2'},local=data.observations[0].source_ref;
  semantic.documents.push({document_id:'family',source_revision:'r9',content_pin:'e'.repeat(64),coverage:{inspected:['page 4'],unread:['page 5']},spans:[{locator_id:'X2',text:'Family context only',locator:{page:4}}]});
  semantic.read_only_records=[
    {kind:'candidate',reason:'unsupported_value_type',record:{id:'bool',label:'Boolean flag',value_type:'boolean',domain:'technical',unit:null,evidence_refs:[local]}},
    {kind:'candidate',reason:'unsupported_domain',record:{id:'cost',label:'Commercial charge',value_type:'decimal',domain:'commercial',unit:'EUR',evidence_refs:[local]}},
    {kind:'candidate',reason:'external_context_evidence',record:{id:'context',label:'Family attribute',value_type:'text',domain:'technical',unit:null,evidence_refs:[external]}},
    {kind:'association',reason:'cross_document_relation',record:{id:'cross',source_refs:[external],target_ref:local,relation:'qualifies',disposition:'ambiguous'}}];
  const ui=await workspace({publicationData:data});
  const html=ui.get('semanticReadOnly').innerHTML;
  for(const wording of ['Boolean flag','boolean','Commercial charge','commercial','EUR','Family attribute','Family context only','Document family · revision r9','X2','Page 4','qualifies','ambiguous','Source endpoint','Target endpoint','Read-only','Only technical','decimal and text','another document']) assert.ok(html.includes(wording),wording);
  assert.equal(ui.get('semanticReadOnly').querySelectorAll('input,select,textarea,button').length,0);
  assert.doesNotMatch(ui.get('definitions').innerHTML,/Boolean flag|Commercial charge|Family attribute/);
  assert.match(ui.get('semanticInventory').innerHTML,/Other-document context/);
  assert.equal(ui.get('semanticRaw').parentElement.getAttribute('open'),null);
});
test('literal source identity and initial suggestion stay readable after human edits and persisted readback',async()=>{
  const data=semanticToy();
  data.context.document_id='fallback-would-be-wrong';
  const ui=await workspace({publicationData:data});
  assert.match(ui.get('inspector').innerHTML,/Document toy · revision r1/);
  assert.match(ui.get('inspector').innerHTML,/L1/);
  assert.match(ui.get('inspector').innerHTML,/line 1/);
  assert.match(ui.get('inspector').innerHTML,/12\.00/);
  assert.match(ui.get('inspector').innerHTML,/Content pin: a{64}/);
  assert.match(ui.get('inspector').innerHTML,/Original suggestion/);
  await ui.get('editLabel').fire('input','Human pressure');await ui.get('editUnit').fire('input','literal expert unit');
  await ui.get('saveDraft').fire('click');
  assert.equal(ui.get('editLabel').value,'Human pressure');
  assert.match(ui.get('inspector').innerHTML,/Rated pressure/);
  assert.match(ui.get('inspector').innerHTML,/12\.00/);
  assert.match(ui.get('inspector').innerHTML,/bar/);
  assert.doesNotMatch(ui.get('inspector').innerHTML,/Document fallback-would-be-wrong/);
  const saved=ui.commands.findLast(c=>c.operation==='review_save');
  assert.equal('semantic_context' in saved,false);
  assert.equal('value' in saved.definitions[0],false);
  assert.deepEqual(saved.definitions[0].observation_ids,['o1']);
});
test('local semantic relation preserves original ambiguity and direction while expert link decision stays explicit',async()=>{
  const data=semanticToy(),ref=data.observations[0].source_ref;
  data.associations=[{id:'a1',observation_ids:['o1'],method:'semantic',rationale:'describes',disposition:'ambiguous',source_refs:[ref],target_ref:ref}];
  const ui=await workspace({publicationData:data});
  assert.match(ui.get('inspector').innerHTML,/Original disposition: ambiguous/);
  assert.match(ui.get('inspector').innerHTML,/Directed relation: describes/);
  assert.match(ui.get('inspector').innerHTML,/Source endpoint/);
  assert.match(ui.get('inspector').innerHTML,/Target endpoint/);
  assert.match(ui.get('inspector').innerHTML,/no match was chosen/i);
  await ui.get('association-0').fire('change','deferred');await ui.get('saveDraft').fire('click');
  assert.equal(ui.commands.findLast(c=>c.operation==='review_save').associations[0].disposition,'deferred');
  assert.match(ui.get('inspector').innerHTML,/Original disposition: ambiguous/);
  assert.match(ui.get('inspector').innerHTML,/describes/);
});
test('unmapped or ineligible semantic evidence explains why no editable match is invented',async()=>{
  const data=semanticToy();
  data.semantic_context.read_only_records=[
    {kind:'candidate',reason:'missing_observation_mapping',record:{id:'missing',label:'Missing observation',value_type:'text',domain:'technical',evidence_refs:[]}},
    {kind:'candidate',reason:'ambiguous_observation_mapping',record:{id:'ambiguous',label:'Multiple observations',value_type:'text',domain:'technical',evidence_refs:[]}},
    {kind:'candidate',reason:'unsupported_evidence',record:{id:'ineligible',label:'Absent evidence',value_type:'text',domain:'technical',evidence_refs:[]}}];
  const ui=await workspace({publicationData:data});
  const html=ui.get('semanticReadOnly').innerHTML;
  assert.match(html,/no mapped target observation/);
  assert.match(html,/more than one observation; no match was chosen/);
  assert.match(html,/not an observed technical value/);
  assert.equal(ui.get('semanticReadOnly').querySelectorAll('input,select,textarea,button').length,0);
  assert.doesNotMatch(ui.get('definitions').innerHTML,/Missing observation|Multiple observations|Absent evidence/);
});
test('other-document observations retain literal value, presence and source scope as read-only context',async()=>{
  const data=semanticToy(),semantic=data.semantic_context;
  semantic.documents.push({document_id:'other',source_revision:'r2',content_pin:'f'.repeat(64),coverage:{inspected:['line 8'],unread:[]},spans:[{locator_id:'O8',text:'Context temperature: 030.0 °C',locator:{text:'line 8'}}]});
  semantic.read_only_records=[{kind:'observation',reason:'external_context_evidence',record:{id:'external',document_id:'other',source_revision:'r2',locator_id:'O8',label:'Context temperature',original_value:'030.0',original_unit:'°C',presence:'observed',domain:'technical'}}];
  const ui=await workspace({publicationData:data});
  const html=ui.get('semanticReadOnly').innerHTML;
  assert.match(html,/Original value: 030\.0/);
  assert.match(html,/Literal unit: °C/);
  assert.match(html,/Presence: observed/);
  assert.match(html,/Document other · revision r2/);
  assert.match(html,/Context temperature: 030\.0 °C/);
  assert.doesNotMatch(ui.get('inspector').innerHTML,/Context temperature/);
});

function semanticProvenanceToy() {
  const data=semanticToy();
  delete data.semantic_context.original_artifact;
  data.semantic_context.provenance={available:true,operation:'result',job_id:'job',proposal_id:'proposal',provenance:true,artifact_pin:'b'.repeat(64)};
  return data;
}
test('original semantic artifact is explicitly read through bounded pages with visible loading and collapsed read-only output',async()=>{
  const expected={operation:'result',job_id:'job',proposal_id:'proposal',provenance:true};
  const artifact={retained:'Original <raw> artifact',large:'x'.repeat(25000)};
  const encoded=JSON.stringify({known:true,job_id:'job',proposal_id:'proposal',artifact_pin:'b'.repeat(64),artifact});
  let release;
  const ui=await workspace({publicationData:semanticProvenanceToy(),commandHandler:body=>{
    if(!body.provenance)return;
    if(!body.cursor)return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({encoding:'json-ascii',offset:0,fragment:encoded.slice(0,24000),next_cursor:'page-two'})});});
    return {ok:true,json:async()=>({encoding:'json-ascii',offset:24000,fragment:encoded.slice(24000),next_cursor:null})};
  }});
  assert.equal(ui.commands.some(c=>c.provenance),false);
  assert.equal(ui.get('semanticArtifactRaw').textContent,'');
  const reading=ui.get('readSemanticArtifact').fire('click');
  assert.match(ui.get('semanticArtifactState').textContent,/Reading.*original semantic artifact/i);
  assert.equal(ui.get('readSemanticArtifact').disabled,true);
  release();await reading;
  assert.deepEqual(ui.commands.filter(c=>c.provenance),[expected,{...expected,cursor:'page-two'}]);
  assert.match(ui.get('semanticArtifactState').textContent,/Read completed.*immutable/i);
  assert.deepEqual(JSON.parse(ui.get('semanticArtifactRaw').textContent),artifact);
  assert.equal(ui.get('semanticArtifactRaw').parentElement.getAttribute('open'),null);
  assert.equal(ui.get('semanticArtifactRaw').parentElement.querySelectorAll('input,textarea,select').length,0);
  assert.equal(ui.get('readSemanticArtifact').disabled,false);
});
test('semantic artifact refusal and transport failure clear stale success and permit exact-selector retry',async()=>{
  let mode='success';
  const original={known:true,job_id:'job',proposal_id:'proposal',artifact_pin:'b'.repeat(64),artifact:{retained:'original-only'}};
  const ui=await workspace({publicationData:semanticProvenanceToy(),commandHandler:body=>{
    if(!body.provenance)return;
    if(mode==='offline')throw new Error('offline');
    return {ok:true,json:async()=> mode==='refusal' ? {known:true,category:'not_found'} : mode==='mismatch' ? {...original,artifact_pin:'wrong-pin'} : original};
  }});
  await ui.get('readSemanticArtifact').fire('click');
  assert.match(ui.get('semanticArtifactState').textContent,/Read completed/);
  for(mode of ['refusal','offline','mismatch']) {
    await ui.get('readSemanticArtifact').fire('click');
    assert.match(ui.get('semanticArtifactState').textContent,/Read failed or refused.*No original artifact loaded.*Retry/);
    assert.doesNotMatch(ui.get('semanticArtifactState').textContent,/Read completed/);
    assert.equal(ui.get('semanticArtifactRaw').textContent,'');
    assert.equal(ui.get('semanticArtifactDetails').hidden,true);
    assert.equal(ui.get('readSemanticArtifact').disabled,false);
    mode==='refusal' && assert.match(ui.get('semanticArtifactState').textContent,/not_found/);
    mode==='offline' && assert.match(ui.get('semanticArtifactState').textContent,/Response unavailable/);
    mode==='mismatch' && assert.match(ui.get('semanticArtifactState').textContent,/identity/);
    const failed=mode; mode='success';await ui.get('readSemanticArtifact').fire('click');mode=failed;
    assert.match(ui.get('semanticArtifactState').textContent,/Read completed/);
    assert.deepEqual(JSON.parse(ui.get('semanticArtifactRaw').textContent),original.artifact);
  }
  assert.ok(ui.commands.filter(c=>c.provenance).every(c=>JSON.stringify(c)===JSON.stringify({operation:'result',job_id:'job',proposal_id:'proposal',provenance:true})));
  assert.equal(ui.commands.some(c=>['review_save','review_approve'].includes(c.operation)),false);
});
test('semantic artifact exceeding the unchanged reader bound refuses without displaying truncated content',async()=>{
  const encoded=JSON.stringify({known:true,job_id:'job',proposal_id:'proposal',artifact_pin:'b'.repeat(64),artifact:{large:'x'.repeat(193000)}});
  const ui=await workspace({publicationData:semanticProvenanceToy(),commandHandler:body=>{
    if(!body.provenance)return;
    const offset=body.cursor ? Number(body.cursor) : 0,end=Math.min(offset+24000,encoded.length);
    return {ok:true,json:async()=>({encoding:'json-ascii',offset,fragment:encoded.slice(offset,end),next_cursor:end<encoded.length ? String(end) : null})};
  }});
  await ui.get('readSemanticArtifact').fire('click');
  assert.equal(ui.commands.filter(c=>c.provenance).length,8);
  assert.match(ui.get('semanticArtifactState').textContent,/Read failed or refused.*page bound reached/);
  assert.equal(ui.get('semanticArtifactRaw').textContent,'');
  assert.equal(ui.get('semanticArtifactDetails').hidden,true);
  assert.equal(ui.get('readSemanticArtifact').disabled,false);
});

test('customer intent admission counts the exact UTF-8 prepared submit envelope before reservation',async()=>{
  const {fitsReviewEnvelopes}=await import('../src/nilaris/review_web/customer_intent.js');
  const scope={customer_id:'customer',job_id:'job',expected_scope:'authenticated-review:'+ 'a'.repeat(64)};
  const original={operation:'review_save',command_id:'same',reason:'流'.repeat(1000)};
  const bytes=Buffer.byteLength(JSON.stringify({...scope,command:original,prepared:true}),'utf8');
  assert.equal(fitsReviewEnvelopes(scope, original, bytes),true);
  assert.equal(fitsReviewEnvelopes(scope, original, bytes-1),false);
  assert.equal(fitsReviewEnvelopes(scope, original, JSON.stringify({...scope,command:original}).length),false);
});

test('an incomplete saved schema can be inspected and shown for exact approval validation',async()=>{
  const ui=await workspace();
  ui.setRead(value=>{
    delete value.definitions[0].label;
    delete value.definitions[0].decision;
    value.definitions[1].observation_ids={'malformed':'operand'};
    value.associations=[null,{id:'incomplete',candidate_ids:3,observation_ids:'malformed'}];
    return value;
  });
  await ui.get('reloadDraft').fire('click');
  assert.equal(ui.get('error').hidden,true);
  await ui.get('reviewApproval').fire('click');
  assert.equal(ui.get('approvalDialog').open,true);
  const shown=JSON.parse(ui.get('approvalSnapshot').textContent);
  assert.equal('decision' in shown.definitions[0],false);
  assert.deepEqual(shown.associations,[null,{id:'incomplete',candidate_ids:3,observation_ids:'malformed'}]);
});

test('bounded original comparison covers Unicode, ordered arrays, sorted object keys and typed decimal numbers',async()=>{
  const {commandFingerprint}=await import('../src/nilaris/review_web/customer_intent.js');
  const value={'é':'🌊',z:[true,0,null,1.25e-7]};
  const canonical='["object",[["z",["array",[true,["number","0"],null,["number","125e-9"]]]],["\\u00e9","\\ud83c\\udf0a"]]]';
  const expected=require('node:crypto').createHash('sha256').update(canonical).digest('hex');
  assert.equal(await commandFingerprint(value),expected);
  assert.equal(await commandFingerprint({z:value.z,'é':'🌊'}),expected);
  assert.notEqual(await commandFingerprint({...value,z:[1,0,null,1.25e-7]}),expected);
});

test('expert browser admission requires representable Unicode keys and values',async () => {
  const {representableReview}=await import('../src/nilaris/review_web/customer_intent.js');
  for (const value of ['\u0000','x\udfff','\ud800z']) {
    assert.equal(representableReview({definitions:[{reason:value}]}),false);
    assert.equal(representableReview({definitions:[{[value]:'key'}]}),false);
  }
  assert.equal(representableReview({'🐋':'🌊'}),true);
});
