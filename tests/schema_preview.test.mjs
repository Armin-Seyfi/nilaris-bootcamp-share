import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {createRequire} from 'node:module';

test('named Approver can inspect ready and unresolved saved revisions from the dashboard', {
  skip: !process.env.NILARIS_PREVIEW_QA
}, async () => {
  const {chromium} = createRequire(import.meta.url)('playwright-core');
  const read = name => readFile(join(process.env.NILARIS_PREVIEW_QA, name), 'utf8');
  const {customer_id} = JSON.parse(await read('owner-identity.json'));
  const {draft_id} = JSON.parse(await read('owner-draft.json'));
  const origin = 'http://127.0.0.1:18808';
  const browser = await chromium.launch({executablePath:process.env.NILARIS_TEST_CHROME});
  try {
    const page = await browser.newPage();
    await page.goto(origin);
    assert.equal(new URL(page.url()).origin, origin);
    await page.locator('#credential').fill((await read('owner-approver.token')).trim());
    await page.locator('#login').evaluate(form => form.requestSubmit());
    await page.locator('#expert-customer').fill(customer_id);
    await page.getByRole('button',{name:'View proposals'}).click();
    await page.getByRole('link',{name:/Preview whole Schema/}).click();
    await page.getByRole('heading',{name:'Ready for final approval'}).waitFor();
    assert.ok((await page.locator('#pin').textContent()).includes(draft_id));
    assert.equal(await page.locator('#excluded').innerText(), 'Seal flush rate · Suggested');
    assert.equal(await page.locator('#associations').innerText(),'Associations (0)');
    await page.getByRole('link',{name:'Revision 1', exact:true}).click();
    await page.locator('#unresolved li').first().waitFor();
    assert.deepEqual(await page.locator('#unresolved li').allTextContents(),[
      'Plunger diameter · Like-for-like reorder', 'Stroke length · New pump purchase',
      'Stroke length · Like-for-like reorder']);
    assert.equal(await page.getByRole('link',{name:'Revision 2',exact:true}).count(),0);
    const before = await page.locator('#preview').innerText();
    await page.reload();
    await page.locator('#preview').waitFor();
    assert.equal(await page.locator('#preview').innerText(), before);
  } finally {
    await browser.close();
  }
});
