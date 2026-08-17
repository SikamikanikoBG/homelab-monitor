#!/usr/bin/env node
/*
 * The TFLOPS logic that lives on the client: which cards a model server is
 * sitting on, and what a spilling model's share of them is worth.
 *
 * The backend half is covered by tests/test_gpuspec.py and
 * tests/test_gpu_tflops_api.py. What can only be tested here is the AI Models
 * tab's arithmetic, and the live fleet cannot exercise it: the hub has one card
 * and one fully-resident embedding model, so the multi-card FP16 path and the
 * RAM-spill path — the two that carry real reasoning — would ship unverified.
 *
 * Same technique as test_refresh_loop.js: the functions are lifted out of the
 * shipped static/dashboard.html and run in a `vm` context, so the test can only
 * pass by the real code behaving.
 *
 * Run: node tests/js/test_tflops_cells.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DASH = process.env.DASHBOARD_HTML
  || path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

function takeFunction(name) {
  const start = SRC.indexOf(`\nfunction ${name}(`);
  if (start < 0) {
    throw new Error(
      `function ${name}() was not found in ${DASH}.\n` +
      '       Either this page predates the TFLOPS feature (nothing to test), or the\n' +
      '       definition was renamed and this extractor needs updating to match.');
  }
  const end = SRC.indexOf('\n}', start);
  if (end < 0) throw new Error(`could not find the end of ${name}() — is it still a top-level function?`);
  return SRC.slice(start + 1, end + 2);
}

function takeLine(re, what) {
  const m = SRC.match(re);
  if (!m) throw new Error(`${what} was not found in ${DASH}.`);
  return m[0];
}

// ── context ──────────────────────────────────────────────────────────────────
// I18N.t/tf are reimplemented rather than extracted (they are object methods, not
// top-level functions) — with the SAME substitution semantics as the shipped
// pair, so a cell whose text depends on interpolation is really exercised.
function build(now) {
  const ctx = {
    Math, JSON, Object, Array, String, Number,
    D: { now },
    I18N: {
      t: (k, f) => (f != null ? f : k),
      tf(k, f, vars) {
        let s = this.t(k, f);
        if (vars) for (const n in vars) s = s.split('{' + n + '}').join(vars[n]);
        return s;
      },
    },
  };
  vm.createContext(ctx);
  vm.runInContext([
    takeLine(/^const esc = .*$/m, 'the esc helper'),
    takeFunction('fmtTF'),
    takeFunction('serverCompute'),
    takeFunction('modelComputeCell'),
  ].join('\n'), ctx);
  return ctx;
}

const C3090 = { fp32: 35.6, fp16: 71.0, cores: 10496, boost_mhz: 1695 };
const P2000 = { fp32: 3.0, cores: 1024, boost_mhz: 1480 };

const card = (idx, compute, name) => {
  const c = { idx, name: name || 'card' + idx };
  if (compute) c.compute = compute;
  return c;
};

let checks = 0, failures = 0;
function check(what, ok, detail) {
  checks++;
  if (ok) { console.log(`  ok   ${what}`); }
  else { failures++; console.log(`  FAIL ${what}${detail ? ' — ' + detail : ''}`); }
}
function section(t) { console.log(`\n${t}`); }

// ── fmtTF ────────────────────────────────────────────────────────────────────
section('fmtTF — precision follows magnitude, across four orders of magnitude');
{
  const c = build({});
  check('a sub-TFLOPS idle card keeps two decimals', c.fmtTF(0.28) === '0.28 T', c.fmtTF(0.28));
  check('a single card keeps one decimal', c.fmtTF(35.6) === '35.6 T', c.fmtTF(35.6));
  check('a three-card pool rounds to whole TFLOPS', c.fmtTF(106.8) === '107 T', c.fmtTF(106.8));
  check('zero does not become blank or NaN', c.fmtTF(0) === '0.00 T', c.fmtTF(0));
}

// ── serverCompute ────────────────────────────────────────────────────────────
section('serverCompute — which cards is this server actually on');
{
  const threeCards = [card(0, C3090), card(1, C3090), card(2, C3090)];

  const onTwo = build({
    gpus: threeCards,
    procs: [{ service: 'ollama', mem: 30000, by_card: { '0': 20000, '1': 10000, '2': 0 } }],
  }).serverCompute('ollama');
  check('only the cards holding its VRAM are counted',
    JSON.stringify(onTwo.cards) === '[0,1]', JSON.stringify(onTwo && onTwo.cards));
  check('their FP32 peaks are summed', Math.abs(onTwo.fp32 - 71.2) < 0.01, String(onTwo.fp32));
  check('their FP16 peaks are summed', Math.abs(onTwo.fp16 - 142.0) < 0.01, String(onTwo.fp16));
  check('the split is reported as attributed', onTwo.attributed === true);

  // A card listed in by_card with zero bytes is not a card it is on.
  const zeroed = build({
    gpus: threeCards,
    procs: [{ service: 'ollama', mem: 10, by_card: { '0': 0, '1': 0, '2': 100 } }],
  }).serverCompute('ollama');
  check('a zero-byte entry does not count as being on that card',
    JSON.stringify(zeroed.cards) === '[2]', JSON.stringify(zeroed.cards));

  // No attribution at all: every card is the honest answer, flagged as such.
  const noAttr = build({
    gpus: threeCards, procs: [{ service: 'ollama', mem: 30000 }],
  }).serverCompute('ollama');
  check('a host without per-card attribution falls back to the whole box',
    JSON.stringify(noAttr.cards) === '[0,1,2]', JSON.stringify(noAttr.cards));
  check('...and says the split was not attributed', noAttr.attributed === false);

  // The rule that must not drift from the backend's.
  const mixed = build({
    gpus: [card(0, C3090), card(1, P2000)], procs: [],
  }).serverCompute('ollama');
  check('a pool mixing tensor and non-tensor cards publishes no FP16',
    mixed.fp16 === null, JSON.stringify(mixed.fp16));
  check('...but still publishes FP32', Math.abs(mixed.fp32 - 38.6) < 0.01, String(mixed.fp32));

  check('a box whose cards are all unrecognised yields nothing',
    build({ gpus: [card(0, null)], procs: [] }).serverCompute('ollama') === null);
  check('a box with no GPU at all yields nothing',
    build({ gpus: [], procs: [] }).serverCompute('ollama') === null);
  check('an unknown service still resolves against the box',
    build({ gpus: [card(0, C3090)], procs: [] }).serverCompute('nope') !== null);
}

// ── modelComputeCell ─────────────────────────────────────────────────────────
section('modelComputeCell — a spilling model loses compute to the CPU');
{
  const c = build({ gpus: [card(0, C3090)], procs: [] });
  const K = c.serverCompute('ollama');

  check('an idle model shows a dash, not a zero',
    c.modelComputeCell(K, { vram: null, ram: null }).indexOf('—') >= 0);
  check('an unrecognised box shows a dash too',
    c.modelComputeCell(null, { vram: 100, ram: 0 }).indexOf('—') >= 0);

  const full = c.modelComputeCell(K, { vram: 20000, ram: 0 });
  check('a fully-resident model gets the full FP16 figure',
    full.indexOf('71.0 TF') >= 0, full.slice(-60));
  check('...and is not marked as reduced', full.indexOf('spillv') < 0);

  // 75% resident: three quarters of the tensor throughput, and the "of 71.0"
  // reference stays visible so the loss is legible rather than just a smaller
  // number the reader has nothing to compare against.
  const spill = c.modelComputeCell(K, { vram: 15000, ram: 5000 });
  check('a 75%-resident model is scaled to 75% of the figure',   // 71.0 × 0.75 = 53.25
    spill.indexOf('53.3 TF') >= 0, spill.slice(0, 200));
  check('...and still shows what it is losing against', spill.indexOf('of 71.0 TF') >= 0);
  check('...marked with the same spill styling the VRAM column uses',
    spill.indexOf('spillv') >= 0);
  check('...and says so in the tooltip', /only 75% of this model is resident/.test(spill));

  // Non-tensor hardware falls back to FP32 and labels it that way.
  const p = build({ gpus: [card(0, P2000)], procs: [] });
  const pk = p.serverCompute('ollama-embed');
  const cell = p.modelComputeCell(pk, { vram: 308, ram: 0 });
  check('a card without tensor cores reports FP32, not a missing FP16',
    cell.indexOf('3.00 TF') >= 0 && /FP32/.test(cell), cell.slice(0, 120));

  // The honesty line that has to survive refactors: this is a shared ceiling.
  check('the tooltip says the figure is shared, not per-model',
    /Shared with any other model loaded on the same server/.test(full));
  check('the tooltip says it is a ceiling, not a measurement',
    /not a measurement of work done/.test(full));

  // A model reporting zero bytes everywhere must not divide by zero.
  const zero = c.modelComputeCell(K, { vram: 0, ram: 0 });
  check('a zero-byte load does not produce NaN', zero.indexOf('NaN') < 0, zero.slice(0, 120));
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) { console.error(`${failures} check(s) FAILED`); process.exit(1); }
