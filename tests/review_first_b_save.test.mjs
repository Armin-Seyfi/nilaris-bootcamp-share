import test from 'node:test';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {StageSave} from '../src/nilaris/review_web/stage_save.mjs';

const stages = ['supplier-offer', 'before-manufacture'];
test('a missing reply requires checking the original before retrying it', async () => {
  const calls = [];
  const post = async (path, body) => {
    calls.push({path, body});
    if (path === 'prepare') return {journal_tag:'stamp'};
    if (path === 'command') throw Error('reply lost');
    if (path === 'reconcile') return {known:false, category:'unknown_command', journal_pending:true, journal_tag:'stamp'};
    if (path === 'retry') return {known:true, saved:true, revision:1, journal_tag:'stamp'};
    return {state:'acknowledged'};
  };
  const store = {save:async command => ({command}), remove:async () => {}};
  const save = new StageSave(post,store);
  const command = {operation:'review_stage_save', command_id:'original', expected_revision:0, stages};
  await save.apply(command);
  assert.equal(save.phase,'unknown');
  await save.retry();
  await save.apply({...command, command_id:'different'});
  assert.deepEqual(calls.map(c=>c.path),['prepare','command']);
  await save.check();
  assert.deepEqual(calls[2],{path:'reconcile',body:{command}});
  await save.retry();
  assert.equal(save.phase,'saved');
  assert.deepEqual(calls.slice(3).map(c=>[c.path,c.body.command_id,c.body.tag]),
    [['retry','original','stamp'],['acknowledge','original','stamp']]);
});

test('rejecting a save retains both selections and correction creates a new command', async () => {
  const writes = [];
  const post = async (path, body) => {
    if (path === 'command') {
      writes.push(body.command);
      return {known:true, saved:writes.length === 2, journal_tag:'stamp'};
    }
    return {journal_tag:'stamp'};
  };
  const store = {save:async command => ({command}), remove:async () => {}};
  const save = new StageSave(post,store);
  await save.apply({command_id:'one', expected_revision:0, stages});
  assert.equal(save.phase,'rejected');
  assert.deepEqual(save.original.stages, stages);
  await save.apply({command_id:'two', expected_revision:1, stages:save.original.stages});
  assert.equal(save.phase,'saved');
  assert.deepEqual(writes.map(w=>[w.command_id,w.stages]), [['one',stages],['two',stages]]);
});

test('a committed original with a missing reply resolves without another save', async () => {
  const calls = [];
  const post = async (path, body) => {
    calls.push({path,body});
    if (path === 'resolve') return {known:true,saved:true,revision:4,journal_tag:'stamp'};
    return {state:'acknowledged'};
  };
  const save = new StageSave(post,{});
  save.restore(null, {command_id:'original', stages}, 'stamp');
  assert.equal(save.phase,'unknown');
  await save.check();
  assert.equal(save.phase,'saved');
  assert.deepEqual(calls.map(c=>c.path),['resolve','acknowledge']);
});

test('a rejected selection survives reopening even when a fresh revision cannot be read', async () => {
  let local;
  const store = {
    save: async (command, previous, rejection) => {
      assert.equal(previous ?? null, local ?? null);
      return local = {command, rejection};
    },
    remove: async () => { local = null; }
  };
  const post = async path => path === 'command'
    ? {known:true, saved:false, category:'revision_conflict', current_revision:4, journal_tag:'stamp'}
    : {journal_tag:'stamp'};
  const save = new StageSave(post, store);
  await save.apply({command_id:'old', stages, expected_revision:2});
  const restored = new StageSave(post, store);
  restored.restore(local, null, null);
  assert.equal(restored.phase, 'rejected');
  assert.equal(restored.result.current_revision, 4);
  assert.deepEqual(restored.original.stages, stages);
});

test('a stalled response or body reaches unknown and permits original-command reconciliation', async () => {
  const {postStageRequest} = await import('../src/nilaris/review_web/stage_save.mjs');
  for (const stuckBody of [false, true]) {
    let aborted = false;
    const post = async (path, body) => {
      if (path === 'prepare') return {journal_tag:'stamp'};
      if (path === 'reconcile') return {known:false, category:'unknown_command', journal_pending:true};
      return postStageRequest('/expert/' + path, body, 20, async (_url, {signal}) => {
        function waitForAbort() {
          return new Promise((_, reject) => signal.addEventListener('abort', () => { aborted = true; reject(signal.reason); }));
        }
        return stuckBody ? {ok:true, json:waitForAbort} : waitForAbort();
      });
    };
    const save = new StageSave(post, {save: async command => ({command})});
    await save.apply({command_id:'original', stages});
    assert.equal(aborted, true);
    assert.equal(save.phase, 'unknown');
    await save.check();
    assert.equal(save.phase, 'retry');
  }
});

test('a readback must confirm the same draft and revision before replacing the visible decision', async () => {
  const {confirmedStageRecord} = await import('../src/nilaris/review_web/stage_save.mjs');
  const record = {known:true, draft_id:'draft', revision:2, membership:'accepted', stages, decided_by:'["issuer","expert"]',
    product_type:'pump', scenario:'scenario', attribute:'diameter', value:'46 mm',
    passage:'Plunger Dia: 46 mm', scope_note:'offer only', source_name:'offer.pdf', location:'page 1',
    source_sha256:'a'.repeat(64), source_fragment:'page=1', documents:[{name:'offer.pdf', sha256:'a'.repeat(64)}]};
  assert.equal(confirmedStageRecord(record, 'draft', 2), record);
  for (const value of [{known:false,category:'persistence_unavailable'}, {...record, revision:1}, {...record, membership:null},
                       {...record, stages:['wrong']}, {...record, decided_by:'broken'}, {...record, documents:null}]) {
    assert.throws(() => confirmedStageRecord(value, 'draft', 2));
  }
});

test('the encrypted browser journal preserves a receiptless rejection when acknowledgement loses its reply',
  {skip:!process.env.NILARIS_TEST_CHROME}, async () => {
    const {createRequire} = await import('node:module');
    const {chromium} = createRequire(import.meta.url)('playwright-core');
    const browser = await chromium.launch({executablePath:process.env.NILARIS_TEST_CHROME});
    try {
      const page = await browser.newPage();
      const origin = 'http://127.0.0.1';
      await page.route(origin + '/', route => route.fulfill({contentType:'text/html', body:'<html lang="en"></html>'}));
      for (const name of ['customer_intent.js', 'stage_save.mjs']) {
        const path = fileURLToPath(new URL('../src/nilaris/review_web/' + name, import.meta.url));
        await page.route(origin + '/' + name, route => route.fulfill({path, contentType:'text/javascript'}));
      }
      await page.goto(origin);
      const result = await page.evaluate(async () => {
        const {openIntentStore} = await import('/customer_intent.js');
        const {StageSave} = await import('/stage_save.mjs');
        const store = await openIntentStore(['Reviewer'], async () => ({scope:'test',slot:'stage',key:'ab'.repeat(32)}));
        const calls = [];
        const post = async route => {
          calls.push(route);
          if (route === 'prepare') return {journal_tag:'tag'};
          if (route === 'command') return {known:true, saved:false, category:'byte_capacity', receipt_stored:false, journal_tag:'tag'};
          if (route === 'acknowledge') throw Error('reply lost after acknowledgement');
          if (route === 'context') return {pending:null, pending_tag:null};
          return {known:false, category:'unknown_command', journal_pending:false};
        };
        const decision = new StageSave(post, store);
        await decision.apply({operation:'review_stage_save', command_id:'original', stages:['supplier-offer','before-manufacture']});
        const uncertain = decision.phase;
        await decision.check();
        await decision.retry();
        const local = await store.read();
        return [uncertain, decision.phase, local.command.stages, calls];
      });
      assert.deepEqual(result, ['unknown','rejected', stages, ['prepare','command','acknowledge','context']]);
    } finally {
      await browser.close();
    }
  });

test('B uses confirmed Schema membership to expose the stage editor',
  {skip:!process.env.NILARIS_TEST_CHROME}, async () => {
    const {createRequire} = await import('node:module');
    const {chromium} = createRequire(import.meta.url)('playwright-core');
    const browser = await chromium.launch({executablePath:process.env.NILARIS_TEST_CHROME});
    try {
      const page = await browser.newPage();
      const origin = 'http://127.0.0.1';
      const actor = '["issuer","Morgan"]';
      const record = {known:true, draft_id:'draft', revision:0, membership:'awaiting',
        product_type:'pump', scenario:'procurement', attribute:'Plunger diameter', value:'46 mm',
        passage:'Plunger Dia: 46 mm', scope_note:'offer only', source_name:'offer.pdf',
        location:'page 1', source_sha256:'a'.repeat(64), source_fragment:'page=1', documents:[],
        stages:null, decided_by:null};
      for (const [route, file] of Object.entries({'review-first-b':'review_first_b.html',
        'review-first-b.mjs':'review_first_b.mjs','stage-save.mjs':'stage_save.mjs',
        'customer-intent.js':'customer_intent.js','review-first-b.css':'review_first_b.css'})) {
        const path = fileURLToPath(new URL('../src/nilaris/review_web/' + file, import.meta.url));
        await page.route(origin + '/' + route, req => req.fulfill({path}));
      }
      let reject = true;
      let releaseRead;
      const delayedRead = new Promise(resolve => {releaseRead = resolve;});
      let observedRead;
      const waiting = new Promise(resolve => {observedRead = resolve;});
      await page.route(origin + '/expert/*', async route => {
        const path = new URL(route.request().url()).pathname;
        const {command} = route.request().postDataJSON();
        let json;
        if (path === '/expert/context') json = {scope:'test', draft_id:'draft', actor};
        else if (path === '/expert/unlock') json = {scope:'test', slot:'stage', key:'ab'.repeat(32)};
        else if (path === '/expert/prepare') json = {journal_tag:'tag'};
        else if (path === '/expert/acknowledge') json = {};
        else if (command.operation === 'review_stage_read') {
          if (record.revision === 1) {observedRead(); await delayedRead;}
          json = record;
        } else {
          assert.equal(command.operation, 'review_stage_add');
          assert.equal(command.expected_revision, 0);
          if (reject) json = {known:true, saved:false, category:'byte_capacity', journal_tag:'tag'};
          else {
            Object.assign(record, {revision:1, membership:'accepted', membership_origin:{kind:'reviewer', actor}});
            json = {known:true, saved:true, revision:1, journal_tag:'tag'};
          }
        }
        await route.fulfill({json});
      });
      await page.goto(origin + '/review-first-b');
      await page.locator('#row').click();
      const edit = page.getByRole('button', {name:'Edit stage obligations'});
      const add = page.getByRole('button', {name:'Add to Schema'});
      await add.click();
      await page.getByText('Addition rejected.').waitFor();
      assert.equal(await edit.isVisible(), false);
      assert.equal(await page.locator('#membership').textContent(), 'Suggested');
      reject = false;
      await add.click();
      await waiting;
      assert.equal(await edit.isVisible(), false);
      releaseRead();
      await edit.click();
      assert.equal(await page.locator('#membership').textContent(), 'In Schema · Added by Morgan');
      assert.equal(await page.locator('#rowState').textContent(), 'Undetermined');
      Object.assign(record, {revision:0, membership_origin:{kind:'system'}});
      await page.reload();
      await page.locator('#row').click();
      await edit.click();
      assert.equal(await page.locator('#membership').textContent(), 'In Schema · Added by system');
      assert.equal(await add.isVisible(), false);
      Object.assign(record, {revision:2, membership:'awaiting', membership_origin:null,
        stages:['supplier-offer','before-manufacture'], decided_by:actor});
      await page.reload();
      await add.waitFor();
      assert.equal(await edit.isVisible(), false);
      assert.match(await page.locator('#confirmed').textContent(), /Supplier offer.*After order and before manufacture/);
      assert.match(await page.locator('#attribution').textContent(), /Previously recorded by Morgan/);

      Object.assign(record, {revision:0, stages:null, decided_by:null, scenarios:[{id:'case001',label:'Case_001'}],
        eligible_cells:[], attributes:[{id:'plunger-diameter',label:'Plunger diameter',membership:'awaiting',
          cells:{case001:{stages:null,decided_by:null}}}]});
      await page.reload();
      await page.getByRole('button', {name:'Inspect Plunger diameter'}).click({timeout:2500});
      assert.equal(await page.getByText('Inspect evidence', {exact:true}).isVisible(), true);
      assert.equal(await edit.isVisible(), false);
      assert.equal(await page.locator('#revision').textContent(), 'Saved revision 0');

    } finally {
      await browser.close();
    }
  });

// Opt in only on a freshly seeded, disposable verification customer.
test('authenticated preview, keyboard exception, stale confirmation and lost bulk reply', {
  skip: !process.env.NILARIS_MATRIX_QA
}, async () => {
  const {createRequire} = await import('node:module');
  const {readFile} = await import('node:fs/promises');
  const {join} = await import('node:path');
  const {chromium} = createRequire(import.meta.url)('playwright-core');
  const root = process.env.NILARIS_MATRIX_QA;
  const read = file => readFile(join(root, file), 'utf8');
  const {customer_id} = JSON.parse(await read('qa-identity.json'));
  const {job_id, draft_id} = JSON.parse(await read('qa-draft.json'));
  assert.notEqual(draft_id, JSON.parse(await read('owner-draft.json')).draft_id);
  const token = (await read('qa-reviewer.token')).trim();
  const origin = 'http://127.0.0.1:18797';
  const browser = await chromium.launch({executablePath:process.env.NILARIS_TEST_CHROME});
  try {
    async function open() {
      const page = await browser.newPage();
      await page.goto(origin);
      assert.equal(new URL(page.url()).origin, origin);
      await page.locator('#credential').fill(token);
      await page.locator('#login').evaluate(form => form.requestSubmit());
      await page.locator('#identity').filter({hasText:'Signed in as'}).waitFor();
      const opening = page.waitForResponse(response => response.url() === origin + '/expert/context');
      await page.goto(origin + '/review-first-b?customer=' + customer_id + '&job=' + job_id);
      assert.equal((await (await opening).json()).draft_id, draft_id);
      await page.locator('#finish').waitFor({timeout:5000});
      return page;
    }
    const first = await open();
    const second = await open();
    async function preview(page, counts) {
      await page.getByRole('button', {name:'Finish review', exact:true}).click();
      await page.getByRole('button', {name:'Back to editing'}).waitFor();
      assert.deepEqual(await page.locator('#matrix thead small').allTextContents(), counts);
    }
    await preview(first, ['1 proposed','2 proposed']);
    assert.equal(await first.locator('#matrix .proposed').count(), 3);
    await preview(second, ['1 proposed','2 proposed']);
    await second.getByRole('button', {name:'Back to editing'}).click();
    assert.equal(await second.evaluate(() => document.activeElement.id), 'finish');
    await preview(second, ['1 proposed','2 proposed']);
    await second.keyboard.press('Escape');
    assert.equal(await second.evaluate(() => document.activeElement.id), 'finish');
    assert.equal(await second.locator('#revision').innerText(), 'Saved revision 1');
    await second.getByRole('button', {name:/Stroke length, New pump purchase/}).click();
    await second.getByRole('checkbox', {name:'Customer RFQ', exact:true}).check();
    await second.getByRole('button', {name:'Apply', exact:true}).click();
    await second.getByText('Saved revision 2', {exact:true}).waitFor();
    await first.getByRole('button', {name:'Set 3 cells to optional & save draft'}).click();
    await first.locator('#status').filter({hasText:'Another revision'}).waitFor();
    await preview(first, ['0 proposed','2 proposed']);
    assert.equal(await first.locator('#matrix .proposed').count(), 2);
    const capture = [];
    await first.route('**/expert/command', async route => {
      const {command} = route.request().postDataJSON();
      if (command.operation !== 'review_stage_bulk') return route.continue();
      const response = await route.fetch();
      capture.push({command, receipt:await response.json()});
      await route.abort();
    });
    await first.getByRole('button', {name:'Set 2 cells to optional & save draft'}).click();
    await first.getByRole('button', {name:'Check save result'}).click();
    await first.getByText('Saved revision 3', {exact:true}).waitFor();
    assert.equal(capture.length, 1);
    assert.equal(capture[0].receipt.revision, 3);
    await first.reload();
    await first.getByText('Stage review complete', {exact:true}).waitFor();
    assert.equal(await first.locator('#finish').isVisible(), false);
    assert.equal(await first.getByText('Whole Schema · Draft').isVisible(), true);
    for (const name of [/Stroke length, New pump purchase, Customer RFQ/,
      /Stroke length, Like-for-like reorder, Optional at all stages/,
      /Plunger diameter, Like-for-like reorder, Optional at all stages/,
      /Plunger diameter, New pump purchase, Supplier offer · After order and before manufacture/,
      /Rated flow, Like-for-like reorder, Optional at all stages/]) {
      assert.equal(await first.getByRole('button', {name}).isVisible(), true);
    }
    assert.match(await first.locator('#matrix').innerText(), /Seal flush rate.*Suggested/s);
    for (const [cell, actor] of [
      [/Stroke length, New pump purchase/, 'Alex Chen'], [/Plunger diameter, New pump purchase/, 'Fixture Reviewer'],
      [/Stroke length, Like-for-like reorder/, 'Alex Chen'], [/Rated flow, Like-for-like reorder/, 'Fixture Reviewer']
    ]) {
      await first.getByRole('button', {name:cell}).click();
      assert.equal(await first.locator('#attribution').innerText(), 'Recorded by ' + actor);
      await first.getByRole('button', {name:'Cancel', exact:true}).click();
    }
  } finally {
    await browser.close();
  }
});

test('unconfirmed matrix readback cannot replace a saved preview', async () => {
  const {confirmedStageRecord} = await import('../src/nilaris/review_web/stage_save.mjs');
  const record = {known:true, draft_id:'draft', revision:1, membership:'accepted', stages:null, decided_by:null,
    product_type:'pump', scenario:'test', attribute:'diameter', value:'57 mm', passage:'diameter 57',
    scope_note:'test', source_name:'source', location:'1', source_sha256:'abc', source_fragment:'page=1', documents:[],
    scenarios:[{id:'reorder',label:'Reorder'}], attributes:[{id:'diameter',label:'Diameter',membership:'accepted',
      cells:{reorder:{stages:null,decided_by:null}}}], eligible_cells:[{attribute_id:'diameter',scenario_id:'reorder'}]};
  assert.equal(confirmedStageRecord(record, 'draft', 1), record);
  for (const changes of [{eligible_cells:[]}, {eligible_cells:[...record.eligible_cells,...record.eligible_cells]},
    {attributes:[]}, {scenarios:[{id:'unknown',label:'Unknown'}]}]) {
    assert.throws(() => confirmedStageRecord({...record, ...changes}, 'draft', 1));
  }
});
