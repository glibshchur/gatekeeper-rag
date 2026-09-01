/**
 * Portfolio screenshots of the console, captured deterministically.
 *
 * Manual captures drift: a different window size, a stale session, whichever principals
 * happened to be selected. These are scripted so a re-shoot after a UI change produces
 * the same frames, at 2x for retina, in both themes.
 *
 * Prerequisites:
 *   make up && make ui          # infrastructure and the console on :8077
 *   npm install puppeteer-core  # drives the Chrome already installed on this machine
 *
 * Run:
 *   node scripts/screenshots.mjs            # writes docs/assets/*.png
 *   node scripts/screenshots.mjs --open     # ...and reveals them in Finder
 */
import puppeteer from 'puppeteer-core';
import { mkdir } from 'node:fs/promises';
import { execFile } from 'node:child_process';

const CHROME = process.env.CHROME_PATH
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.GK_CONSOLE || 'http://localhost:8077';
const OUT = 'docs/assets';

const DESKTOP = { width: 1440, height: 900, deviceScaleFactor: 2 };
const MOBILE = { width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true };

/** Sign in cleanly. The console keeps a token in sessionStorage, so without clearing it
 *  the picker is already in checkbox mode and clicking a name only toggles a comparison
 *  row — the identity stays whoever signed in last. That cost me a wrong screenshot. */
async function signInAs(page, handle) {
  await page.evaluate(() => sessionStorage.removeItem('gk_token'));
  await page.reload({ waitUntil: 'networkidle2' });
  await page.waitForSelector('#who input[type=radio]');
  await page.evaluate(h => {
    document.querySelector(`#who input[value="${h}"]`).click();
  }, handle);
  await page.waitForSelector('#signout', { timeout: 30000 });
  await page.waitForFunction(() => document.querySelectorAll('#who input[type=checkbox]').length > 0);
  // boot() keeps running after the picker appears: it fetches each principal's access
  // surface and rewrites the sidebar rows. Selecting before that finishes gets silently
  // undone, and the shot ends up with a column whose checkbox is unchecked.
  //
  // Wait for the sidebar to stop changing rather than for every row to resolve: a
  // principal whose surface request fails keeps its placeholder indefinitely, so
  // "all resolved" is a condition that legitimately never arrives.
  await settle(page, '#who');
}

/** Resolve once `selector`'s text has not changed for `quiet` ms. */
async function settle(page, selector, quiet = 1200, timeout = 60000) {
  const started = Date.now();
  let last = null, lastChange = Date.now();
  while (Date.now() - started < timeout) {
    const now = await page.evaluate(s => document.querySelector(s)?.textContent ?? '', selector);
    if (now !== last) { last = now; lastChange = Date.now(); }
    else if (Date.now() - lastChange >= quiet) return;
    await new Promise(r => setTimeout(r, 200));
  }
}

/** Select exactly these handles. raj and mira are pre-checked on every boot, so a shot
 *  that wants anything else has to clear them explicitly. */
async function select(page, handles) {
  await page.evaluate(hs => {
    document.querySelectorAll('#who input').forEach(i => {
      const want = hs.includes(i.value);
      if (i.checked !== want) i.click();
    });
  }, handles);
  // Assert rather than hope: a checkbox that did not take leaves the sidebar disagreeing
  // with the columns, which is exactly the kind of thing nobody notices until the
  // screenshot is already in a README.
  const actual = await page.evaluate(() =>
    [...document.querySelectorAll('#who input:checked')].map(i => i.value).sort());
  const want = [...handles].sort();
  if (actual.join() !== want.join()) {
    throw new Error(`selection did not stick: wanted [${want}], got [${actual}]`);
  }
}

async function ask(page, question) {
  // Clear first: the wait below returns immediately if a previous render is still on
  // screen, and you photograph the last question instead of this one.
  await page.evaluate(() => { document.querySelector('#out').innerHTML = ''; });
  await page.type('#question', question, { delay: 0 });
  await page.evaluate(() => document.querySelector('#q').requestSubmit());
  // Answer generation is one LLM call per principal, so three columns can take a while.
  // Watch for the console's own error region too: a bare selector timeout tells you
  // nothing, and the page usually knows exactly what went wrong.
  await page.waitForFunction(() => {
    if (document.querySelector('.cols')) return true;
    const e = document.querySelector('#error').textContent.trim();
    if (e) throw new Error('console reported: ' + e.slice(0, 200));
    return false;
  }, { timeout: 240000, polling: 500 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await new Promise(r => setTimeout(r, 400));   // let fonts settle before the frame
}

async function clearQuestion(page) {
  await page.evaluate(() => { document.querySelector('#question').value = ''; });
}

const shots = [];
async function shot(page, name, opts = {}) {
  // Drop focus first, or the text caret blinks its way into the frame.
  await page.evaluate(() => document.activeElement?.blur?.());
  await new Promise(r => setTimeout(r, 120));
  const path = `${OUT}/${name}.png`;
  await page.screenshot({ path, ...opts });
  shots.push(path);
  console.log(`  ✓ ${path}`);
}

const INCIDENT = 'How do I report a security incident?';
const EXPENSE = 'How much can I expense for a meal on a business trip?';

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--hide-scrollbars', '--force-color-profile=srgb'],
});

try {
  await mkdir(OUT, { recursive: true });

  // ---- light theme, desktop -------------------------------------------------
  const page = await browser.newPage();
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await page.setViewport(DESKTOP);
  await page.goto(BASE, { waitUntil: 'networkidle2' });

  console.log('light / desktop');

  // 01 — signed out. The dev-auth banner is the honest part of the demo: show it.
  await page.evaluate(() => sessionStorage.removeItem('gk_token'));
  await page.reload({ waitUntil: 'networkidle2' });
  await page.waitForSelector('#who input[type=radio]');
  await shot(page, '01-signed-out');

  // 02 — the headline: one question, three principals, three different corpora.
  // Sam is a security engineer at clearance 1; Mira is the CFO at clearance 3. Sam reads
  // the restricted incident runbooks and Mira does not, because clearance is a ceiling
  // and the `security` group is the key. That is the whole product in one frame.
  await signInAs(page, 'sam');
  await select(page, ['sam', 'raj', 'mira']);
  await ask(page, INCIDENT);
  await shot(page, '02-clearance-is-a-ceiling');

  // 03 — the sidebar on its own: how much of the corpus each principal can reach.
  const aside = await page.$('aside.panel');
  await aside.screenshot({ path: `${OUT}/03-access-surface.png` });
  shots.push(`${OUT}/03-access-surface.png`);
  console.log(`  ✓ ${OUT}/03-access-surface.png`);

  // 04 — a source opened, showing its sensitivity tag and real handbook path.
  await page.evaluate(() => {
    const d = [...document.querySelectorAll('ol.src details')]
      .find(x => x.querySelector('.tag.restricted')) || document.querySelector('ol.src details');
    if (d) { d.open = true; d.scrollIntoView({ block: 'center' }); }
  });
  await new Promise(r => setTimeout(r, 300));
  await shot(page, '04-source-detail');

  // 05 — withheld counts differing across principals on a second question.
  await clearQuestion(page);
  await ask(page, EXPENSE);
  await shot(page, '05-withheld-counts');

  // ---- dark theme -----------------------------------------------------------
  console.log('dark / desktop');
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
  await new Promise(r => setTimeout(r, 300));
  await shot(page, '06-dark');

  // ---- mobile ---------------------------------------------------------------
  console.log('light / mobile');
  const m = await browser.newPage();
  await m.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await m.setViewport(MOBILE);
  await m.goto(BASE, { waitUntil: 'networkidle2' });
  await signInAs(m, 'sam');
  await select(m, ['sam', 'mira']);
  await ask(m, INCIDENT);
  await shot(m, '07-mobile', { fullPage: false });

  console.log(`\n${shots.length} screenshots in ${OUT}/`);
  if (process.argv.includes('--open')) execFile('open', [OUT]);
} finally {
  await browser.close();
}
