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
// Four result columns need ~4x330px of track plus the sidebar, or the grid wraps to a
// second row and the comparison stops reading left-to-right.
const WIDE = { width: 1840, height: 1050, deviceScaleFactor: 2 };
const TABLET = { width: 834, height: 1112, deviceScaleFactor: 2 };
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
const EQUITY = 'What is the equity refresh policy for executives?';
const LEAVE = 'What is the parental leave policy in the Netherlands?';

/** Clip to the band from `fromSel`'s top down to the lowest element matching `toSel`.
 *
 *  Validates its own output. A selector that matches nothing yields a negative height, and
 *  puppeteer's error for that ("'height' in 'clip' must be positive") says nothing about
 *  which selector was wrong. */
async function band(page, fromSel, toSel, pad = 24) {
  const clip = await page.evaluate((f, t, p) => {
    const from = document.querySelector(f);
    const tos = [...document.querySelectorAll(t)];
    if (!from) return { error: `no match for ${f}` };
    if (!tos.length) return { error: `no match for ${t}` };
    const top = from.getBoundingClientRect().top;
    const bottom = Math.max(...tos.map(e => e.getBoundingClientRect().bottom));
    return {
      x: 0, y: Math.max(0, top - p),
      width: document.documentElement.clientWidth,
      height: Math.min(document.documentElement.clientHeight - Math.max(0, top - p),
                       bottom - top + p * 2),
    };
  }, fromSel, toSel, pad);
  if (clip.error) throw new Error(`band(): ${clip.error}`);
  if (!(clip.height > 0)) throw new Error(`band(): non-positive height for ${fromSel} → ${toSel}`);
  return clip;
}

/** The main portfolio image: the question and the four principals' verdicts, nothing else.
 *
 *  Deliberately excludes the sidebar. Including it meant slicing a principal row in half at
 *  whatever height the crop landed on, which reads as a botched screenshot rather than a
 *  deliberate one. Ends on the "N of 8 sources" baseline so the bottom edge falls between
 *  elements instead of through an answer mid-sentence. */
async function heroClip(page, pad = 28) {
  const clip = await page.evaluate(p => {
    const section = document.querySelector('section');
    const ask = document.querySelector('.ask');
    const cols = [...document.querySelectorAll('.cols > .col')];
    if (!section || !ask || !cols.length) return { error: 'hero anchors missing' };

    // There is no horizontal line that crosses no element. Columns are ragged by
    // construction: a principal with an expired grant has no "N of 8 sources" line, so its
    // result box starts ~19px above everyone else's, and widening the viewport does not
    // close the gap (checked at 1840, 2000, 2200 and 2400).
    //
    // So rather than chase a clean cut, make the cut obviously deliberate: end far enough
    // into the boxes that every column shows its top edge and a full first line of text.
    // A crop that clearly continues reads as a crop; one that shaves 19px off a border
    // reads as a mistake.
    const boxTops = cols.map(c => {
      const box = c.querySelector('.answer, .empty');
      return box ? box.getBoundingClientRect().top : null;
    }).filter(t => t !== null);
    if (!boxTops.length) return { error: 'no result boxes to anchor the crop' };

    const left = Math.max(0, section.getBoundingClientRect().left - p);
    const top = Math.max(0, ask.getBoundingClientRect().top - p);
    const bottom = Math.max(...boxTops) + 58;
    return {
      x: left, y: top,
      width: document.documentElement.clientWidth - left - p,
      height: Math.min(bottom, document.documentElement.clientHeight) - top,
    };
  }, pad);
  if (clip.error) throw new Error(`heroClip(): ${clip.error}`);
  if (!(clip.height > 0 && clip.width > 0)) throw new Error('heroClip(): empty clip');
  return clip;
}

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

  // ---- wide: four principals, one question ----------------------------------
  // The sharpest framing available: one question, four different amounts of truth.
  //
  // "How much can I expense for a meal on a business trip?" gives the security engineer and
  // the CFO real figures ($120/day, $80 per person), while the anonymous visitor gets "the
  // provided sources do not contain any information" — sitting directly beneath "8 results
  // withheld by authorization", so cause and effect are in the same frame. The auditor's
  // grant expired last week and they get nothing at all.
  //
  // The equity-refresh question was tried first and rejected: it withholds more, but even
  // the CFO's answer comes back "no specific policy described", so three of four columns
  // read as an assistant that cannot answer rather than an access model that works.
  console.log('light / wide — four principals');
  const w = await browser.newPage();
  await w.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await w.setViewport(WIDE);
  await w.goto(BASE, { waitUntil: 'networkidle2' });
  await signInAs(w, 'sam');
  await select(w, ['sam', 'mira', 'guest', 'wren']);
  await ask(w, EXPENSE);
  await w.evaluate(() => {
    const n = document.querySelectorAll('.cols > .col').length;
    if (n !== 4) throw new Error(`expected 4 columns, got ${n}`);
  });
  await shot(w, '08-four-principals-one-question');

  // 09 — the hero. Cropped to the band that carries the argument: the question, the four
  // principals, and four different withheld counts. Full-page prose does not survive being
  // shown at portfolio-card size; this does.
  await shot(w, '00-hero', { clip: await heroClip(w) });

  // 10 — a grant that expired. Access here is time-bound, and the console says which date
  // rather than rendering an empty column that looks like a bug.
  const wren = await w.$$('.col');
  for (const col of wren) {
    const t = await col.evaluate(e => e.textContent);
    if (t.includes('grant expired')) { await col.screenshot({ path: `${OUT}/10-expired-grant.png` }); break; }
  }
  shots.push(`${OUT}/10-expired-grant.png`);
  console.log(`  ✓ ${OUT}/10-expired-grant.png`);

  // 11 — the same question for an anonymous visitor and the CFO, side by side. The CFO gets
  // the actual limit; the visitor is told the sources do not cover it, with eight results
  // withheld immediately above to say why.
  await select(w, ['sam', 'guest']);
  await clearQuestion(w);
  await ask(w, EXPENSE);
  await shot(w, '11-guest-vs-cfo');

  // 12 — jurisdiction. Per-country employment policy is scoped by region, not just by rank.
  await clearQuestion(w);
  await ask(w, LEAVE);
  await shot(w, '12-jurisdiction');

  // 13 — the sensitivity spectrum in one source list. Pick the column whose results span
  // the most classification levels rather than whichever renders last: the first attempt
  // grabbed the anonymous visitor's list, which is eight PUBLIC rows and demonstrates
  // nothing about classification.
  await select(w, ['sam', 'mira']);
  await clearQuestion(w);
  await ask(w, INCIDENT);
  await w.evaluate(() => {
    document.querySelectorAll('ol.src details').forEach(d => { d.open = false; });
  });
  const richest = await w.evaluateHandle(() => {
    let best = null, bestN = 0;
    document.querySelectorAll('.col ol.src').forEach(list => {
      const kinds = new Set([...list.querySelectorAll('.tag')].map(t => t.className));
      if (kinds.size > bestN) { bestN = kinds.size; best = list; }
    });
    return best;
  });
  const spread = await w.evaluate(l => l
    ? [...new Set([...l.querySelectorAll('.tag')].map(t => t.textContent.trim()))] : [], richest);
  if (spread.length < 2) throw new Error(`13: only ${spread.length} sensitivity level(s): ${spread}`);
  await richest.asElement().screenshot({ path: `${OUT}/13-sensitivity-tags.png` });
  shots.push(`${OUT}/13-sensitivity-tags.png`);
  console.log(`  ✓ ${OUT}/13-sensitivity-tags.png  (${spread.join(', ')})`);

  // ---- tablet ---------------------------------------------------------------
  console.log('light / tablet');
  const t = await browser.newPage();
  await t.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
  await t.setViewport(TABLET);
  await t.goto(BASE, { waitUntil: 'networkidle2' });
  await signInAs(t, 'sam');
  await select(t, ['sam', 'mira']);
  await ask(t, INCIDENT);
  await shot(t, '14-tablet');

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

  console.log('dark / mobile');
  await m.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
  await new Promise(r => setTimeout(r, 300));
  await shot(m, '15-mobile-dark', { fullPage: false });

  console.log(`\n${shots.length} screenshots in ${OUT}/`);
  if (process.argv.includes('--open')) execFile('open', [OUT]);
} finally {
  await browser.close();
}
