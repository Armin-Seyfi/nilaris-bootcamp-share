// macOS integration check against a running, private local Reviewer fixture.
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright-core');

async function main() {
  const [port, bundle] = process.argv.slice(2);
  assert.match(port, /^\d+$/);
  const origin = 'http://127.0.0.1:' + port;
  const pin = fs.readFileSync(path.join(__dirname, '../src/nilaris/review_web/rfq_revision.txt'), 'utf8').trim();
  const browser = await chromium.launch({
    headless: false, executablePath: process.env.NILARIS_TEST_CHROME,
    args: ['--disable-background-networking', '--disable-component-update', '--disable-sync',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
      '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1'],
  });
  try {
    const context = await browser.newContext({viewport: {width: 1200, height: 820}});
    const page = await context.newPage();
    const automaticFixtureRequests = [];
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/rfq/runs') automaticFixtureRequests.push(request.url());
    });
    await page.goto(origin);
    await page.locator('#token').fill(fs.readFileSync(path.join(bundle, 'reviewer.key'), 'utf8').trim());
    await page.locator('#login-form button').click();
    await page.locator('#workspace').waitFor();
    await page.getByRole('heading', {name: 'Product Type Schema draft'}).waitFor({timeout: 3000});
    const rows = page.locator('#schema-rows tr');
    assert.equal(await rows.count(), 20);
    const flowRows = [];
    const manifest = JSON.parse(fs.readFileSync(path.join(bundle, 'manifest.json'), 'utf8'));
    if (manifest.note_original) {
      const review = page.getByRole('form', {name: 'Product Type Alias review'});
      await review.waitFor({timeout: 3000});
      const choice = review.getByLabel('Alias decision');
      const name = review.getByLabel('Attribute name');
      assert.equal(await choice.inputValue(), '');
      const initial = await review.locator('#alias-initial').innerText();
      const note = await review.locator('#note-passage').innerText();
      assert.match(note, /^7\) .*3 barg.*pump shut off pressure$/);
      assert.match(await review.innerText(), /Pump design pressure\(7\)/);
      const sources = review.getByRole('link');
      const hrefs = await sources.evaluateAll(links => links.map(link => link.href));
      assert.equal(hrefs.length, 2);
      for (const [index, kind] of ['Row 5', 'Note 7'].entries()) {
        const [source] = await Promise.all([context.waitForEvent('page'), sources.nth(index).click()]);
        await source.locator('#pdf[href]').waitFor();
        assert.ok((await source.locator('#locator').innerText()).endsWith(kind));
        if (index === 1) assert.equal(await source.locator('#original-row-text').innerText(), note);
        const [pdf] = await Promise.all([context.waitForEvent('page'), source.locator('#pdf').click()]);
        await pdf.waitForURL('**/original.pdf#*');
        const bytes = await (await context.request.get(pdf.url().split('#')[0])).body();
        assert.deepEqual(bytes, fs.readFileSync(path.join(bundle, index ? 'note-original.pdf' : 'original.pdf')));
        await pdf.close();
        await source.close();
      }
      for (const decision of ['confirm', 'separate']) {
        await name.fill('Reviewed shut-off head');
        await choice.selectOption(decision);
        await review.getByRole('button', {name: 'Save Alias review'}).click();
        await page.getByText('Alias review saved for this session.').waitFor();
        await page.reload();
        await review.waitFor();
        assert.equal(await choice.inputValue(), decision);
        assert.equal(await name.inputValue(), 'Reviewed shut-off head');
        assert.equal(await review.locator('#alias-initial').innerText(), initial);
        assert.equal(await review.locator('#note-passage').innerText(), note);
        assert.deepEqual(await sources.evaluateAll(links => links.map(link => link.href)), hrefs);
      }
    }
    for (const passage of [manifest.passage, manifest.qualifier_comparison]) {
      const [number, label, value, unit] = passage.split('\t');
      const row = rows.filter({has: page.getByRole('cell', {name: label, exact: true})});
      assert.equal(await row.count(), 1);
      const cells = row.getByRole('cell');
      assert.equal(await cells.nth(0).innerText(), number);
      assert.equal(await cells.nth(2).innerText(), value);
      assert.equal(unit, 'm\n3/h');
      assert.equal(await cells.nth(3).innerText(), 'm³/h');
      flowRows.push(row);
    }
    const unresolved = rows.filter({has: page.getByRole('link', {name: 'View row 12', exact: true})});
    assert.equal(await unresolved.getByRole('cell').nth(1).innerText(), 'Unassigned');
    assert.match(await unresolved.innerText(), /Unresolved.*original label/i);
    assert.match(await page.locator('#coverage').innerText(), /14 proposed.*6 unresolved/i);
    async function checkNativePDF(link, label) {
      const [pdf] = await Promise.all([context.waitForEvent('page'), link.click()]);
      await pdf.bringToFront();
      await pdf.waitForURL('**/original.pdf#*');
      const response = await context.request.get(pdf.url().split('#')[0]);
      assert.equal(response.headers()['content-type'], 'application/pdf');
      assert.equal(createHash('sha256').update(await response.body()).digest('hex'), pin);

      // The PDF is painted by Chrome's native viewer, outside the page DOM.
      let positions = {};
      for (let attempt = 0; attempt < 5; attempt++) {
        const result = spawnSync('swift', [path.join(__dirname, 'rfq_pdf_labels.swift')], {
          input: await pdf.screenshot(), encoding: 'utf8',
        });
        assert.equal(result.status, 0, result.stderr);
        positions = JSON.parse(result.stdout);
        if (Object.keys(positions).length) break;
        await pdf.waitForTimeout(250);
      }
      // At this viewport size the toolbar ends near 7% of screenshot height.
      const top = positions[label];
      assert.ok(top > .065 && top < .13, 'Selected row must appear directly below the PDF toolbar');
      if (label === 'flownormal') assert.equal(positions.flowrated, undefined);
      await pdf.close();
    }
    const checkSource = async (row, rowNumber, label) => {
      const [source] = await Promise.all([context.waitForEvent('page'), row.getByRole('link').click()]);
      await source.locator('#revision').waitFor();
      assert.equal(await source.locator('#revision').innerText(), 'SHA-256 ' + pin);
      assert.match(await source.locator('#locator').innerText(), new RegExp('Page 1.*Row ' + rowNumber + '$'));
      if (label) {
        assert.ok((await source.locator('#original-row-text').innerText({timeout: 3000})).includes(label));
      }
      const locator = await source.locator('#pdf').getAttribute('href');
      assert.match(locator, new RegExp('/' + pin + '/row-' + rowNumber + '/original.pdf#page=1&zoom='));
      const original = await context.request.get(origin + locator.split('#')[0]);
      assert.equal(createHash('sha256').update(await original.body()).digest('hex'), pin);
      const mark = await source.locator('#evidence-row').boundingBox();
      const image = await source.locator('figure img').boundingBox();
      assert.ok(mark.height > 0 && mark.y >= image.y && mark.y + mark.height <= image.y + image.height);
      if (label) await checkNativePDF(source.locator('#pdf'), label.toLowerCase().replace('-', ''));
      await source.close();
    };
    await checkSource(flowRows[0], 1, 'Flow-rated');
    await checkSource(flowRows[1], 2, 'Flow-normal');
    await checkSource(unresolved, 12);
    const nested = rows.filter({has: page.getByRole('link', {name: 'View row 20', exact: true})});
    assert.match(await nested.innerText(), /Unresolved.*nested columns/);
    assert.deepEqual((await nested.getByRole('cell').allInnerTexts()).slice(1, 4),
      ['Unassigned', 'Unassigned', 'Unassigned']);
    assert.equal(await unresolved.getByRole('cell').nth(2).innerText(), '—');
    await checkSource(nested, 20);
    assert.deepEqual(automaticFixtureRequests, [], 'Opening a Schema draft must be independent of response fixtures');
    await page.getByText('Recorded test responses', {exact: true}).click();
    for (const [choice, label] of [['Rated selection', 'flowrated'], ['Normal selection', 'flownormal']]) {
      await page.locator('#run-choice').selectOption({label: choice});
      await page.locator('#workspace').waitFor();
      await page.locator('#evidence summary').click();
      const link = page.locator('#pdf[href]');
      await link.waitFor();
      await checkNativePDF(link, label);
    }
    console.log('Chrome verified draft coverage, unresolved rows, source links, and both original PDF flow locations.');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
