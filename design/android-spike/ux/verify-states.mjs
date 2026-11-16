#!/usr/bin/env node
/* Design/android-spike/ux state-provenance verifier.
 *
 * Deliverable rule: a mockup frame showing a plausible-looking number without
 * provenance is a failed deliverable. This script asserts, per frame (.scr):
 *   1. at least one visible [data-state] provenance marker,
 *   2. rail/title/note/probe metadata (frames must teach, not just render),
 *   3. every data-go / data-back target resolves to a real frame id,
 *   4. every frame with a currency string carries a fresh/stale/offline marker,
 *   5. cross-frame pins (expired frame = 0 currency strings, fresh baseline = 4).
 * Run: node verify-states.mjs   (exit 0 = pass)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const FILES = ['flow-reentry.html', 'flow-notification.html', 'flow-offline.html'];
const KNOWN_STATES = new Set([
  'fresh', 'stale', 'offline', 'expired', 'demo', 'masked', 'shown', 'auth',
  'os-surface', 'unavailable', 'price-stale', 'permission', 'cleared', 'no-snapshot'
]);
const PROVENANCE_FOR_VALUES = ['fresh', 'stale', 'offline'];

let failures = 0;
const warn = (m) => console.log('  WARN  ' + m);
const fail = (m) => { failures++; console.log('  FAIL  ' + m); };

for (const file of FILES) {
  const html = readFileSync(join(here, file), 'utf8');
  const cuts = [];
  const re = /<div class="scr"/g;
  let m;
  while ((m = re.exec(html)) !== null) cuts.push(m.index);
  console.log(`\n${file}: ${cuts.length} frames`);

  const frames = cuts.map((start, i) => html.slice(start, cuts[i + 1] ?? html.length));
  const ids = frames.map((f) => (f.match(/id="([^"]+)"/) || [])[1]);
  const idSet = new Set(ids.filter(Boolean));

  frames.forEach((f, i) => {
    const id = ids[i] || `#${i}`;
    const attr = (name) => (f.match(new RegExp(`${name}="([^"]*)"`)) || [])[1];
    const states = [...f.matchAll(/data-state="([^"]+)"/g)].map((x) => x[1]);

    if (states.length === 0) fail(`${id}: no [data-state] provenance marker`);
    for (const s of states) if (!KNOWN_STATES.has(s)) warn(`${id}: unknown data-state "${s}"`);

    for (const meta of ['data-rail', 'data-title', 'data-note', 'data-probe']) {
      if (!attr(meta)) fail(`${id}: missing ${meta}`);
    }
    if (!f.includes('gesturebar')) fail(`${id}: missing system-back gesture pill`);

    for (const t of [...f.matchAll(/data-go="([^"]+)"/g)].map((x) => x[1])) {
      if (!idSet.has(t)) fail(`${id}: data-go → "${t}" does not resolve`);
    }
    const back = attr('data-back');
    if (back && !idSet.has(back)) fail(`${id}: data-back → "${back}" does not resolve`);

    const currencies = (f.match(/\$[\d,]+/g) || []).length;
    if (currencies > 0 && !states.some((s) => PROVENANCE_FOR_VALUES.includes(s))) {
      fail(`${id}: ${currencies} currency string(s) with no fresh/stale/offline marker`);
    }
    console.log(`  ok    ${id.padEnd(4)} states=[${states.join(',')}] currency_strings=${currencies} back=${back || '→root'}`);
  });

  // cross-frame pins
  const byId = Object.fromEntries(ids.map((id, i) => [id, frames[i]]));
  if (file === 'flow-offline.html') {
    const expiredCurrencies = ((byId.o4 || '').match(/\$[\d,]+/g) || []).length;
    if (expiredCurrencies !== 0) fail(`o4 (expired): expected 0 currency strings, found ${expiredCurrencies}`);
    const freshBaseline = ((byId.o1 || '').match(/\$[\d,]+/g) || []).length;
    if (freshBaseline !== 4) fail(`o1 (fresh baseline): expected 4 currency strings, found ${freshBaseline}`);
    const offlineBanner = (byId.o3 || '').includes('data-state="offline"');
    if (!offlineBanner) fail('o3: offline banner marker missing');
  }
}

console.log(failures === 0 ? '\nPASS — every frame carries honest provenance.' : `\nFAIL — ${failures} problem(s).`);
process.exit(failures === 0 ? 0 : 1);
