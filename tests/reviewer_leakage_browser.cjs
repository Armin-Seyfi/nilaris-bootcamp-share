// Run from test_reviewer_leakage.py with a disposable Chrome profile.
const {chromium} = require('playwright-core');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(0, 'utf8'));

async function main() {
  const browser = await chromium.launch({
    executablePath: process.env.NILARIS_TEST_CHROME, headless: true,
    args: ['--disable-background-networking', '--disable-component-update',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1',
      '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost'],
  });
  try {
    const page = await browser.newPage();
    const origin = 'http://127.0.0.1:' + config.port;
    const requests = [];
    page.on('request', r => requests.push(r.url()));
    const post = (path, body) => page.evaluate(async ([route, data]) =>
      (await fetch(route, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(data)})).status, [path, body]);
    const path = '/minimized-evidence?customer=' + config.customer + '&job=' + config.job;
    await page.goto(origin);
    assert.equal(await post('/session', {credential:'bob-login'}), 200);
    await page.goto(origin + path);
    await page.locator('#build').click();
    await page.locator('#preview').waitFor();
    assert.equal(await page.locator('#preview').innerText(), config.selected);
    await page.locator('#source').click();
    await page.locator('#original').waitFor();
    assert.ok((await page.locator('#mapping').innerText()).includes(config.mapping));
    assert.ok((await page.locator('#excerpt').innerText()).includes(config.filename));
    const readStores = () => page.evaluate(async () => ({
      local: {...localStorage}, session: {...sessionStorage}, databases: await indexedDB.databases(),
      caches: await caches.keys(), workers: (await navigator.serviceWorker.getRegistrations()).length,
    }));
    const stores = await readStores();
    assert.deepEqual(stores, {local:{}, session:{}, databases:[], caches:[], workers:0});
    const privateRequests = requests.filter(url => new URL(url).pathname === '/minimized-evidence/source');
    assert.equal(privateRequests.length, 1);
    assert.ok(requests.every(url => !decodeURIComponent(url).includes(config.filename)));
    // Calibrate the same URL observer after taking the journey sample.
    await page.evaluate(name => fetch('/style.css?probe=' + encodeURIComponent(name)), config.filename);
    assert.ok(requests.some(url => decodeURIComponent(url).includes(config.filename)));
    await page.reload();
    await page.locator('#preview').waitFor();
    assert.equal(await page.locator('#mapping').textContent(), '');
    assert.equal(await page.locator('#excerpt').textContent(), '');
    await page.locator('#source').click();
    await page.locator('#original').waitFor();
    await page.goto(origin);
    assert.equal(await post('/logout', {}), 200);
    assert.equal(await post('/session', {credential:'b-login'}), 200);
    await page.goBack();
    await page.waitForFunction(() => document.querySelector('#status')?.textContent.includes('refused'));
    assert.ok(!(await page.locator('body').textContent()).includes(config.mapping));
    assert.equal(await page.locator('#excerpt').textContent(), '');
    await page.goForward();
    assert.equal(new URL(page.url()).pathname, '/');
    // Calibrate storage inventory after history traversal in this disposable profile.
    await page.evaluate(async marker => {
      localStorage.setItem('control', marker);
      sessionStorage.setItem('control', marker);
      await caches.open(marker);
      await new Promise((resolve, reject) => {
        const request = indexedDB.open(marker);
        request.onsuccess = () => {request.result.close(); resolve();};
        request.onerror = () => reject(request.error);
      });
    }, config.mapping);
    const controls = await readStores();
    assert.equal(controls.local.control, config.mapping);
    assert.equal(controls.session.control, config.mapping);
    assert.ok(controls.caches.includes(config.mapping));
    assert.ok(controls.databases.some(db => db.name === config.mapping));
    console.log(JSON.stringify({sourceRequests: privateRequests.length, stores, historyDenied:true}));
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
