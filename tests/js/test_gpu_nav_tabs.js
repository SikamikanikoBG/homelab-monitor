#!/usr/bin/env node
/*
 * Which of the GPU / AI Models nav tabs the sidebar shows (#369).
 *
 * They used to hide whenever the HUB had no GPU, so a hub with only an iGPU
 * lost both tabs a few seconds after load, even with a GPU on another fleet
 * host and AI servers registered. The rules pinned here:
 *   - a GPU on any fleet host keeps the GPU tab (and AI Models);
 *   - any model the hub knows of keeps AI Models, GPU or not;
 *   - a broken GPU tool counts as a GPU (the tab is where that is reported);
 *   - a lab with neither hides both;
 *   - a payload that hasn't arrived yet hides nothing.
 *
 * Same technique as test_nav_customize.js: the functions are lifted out of the
 * shipped static/dashboard.html and run in a `vm` context.
 *
 * Run: node tests/js/test_gpu_nav_tabs.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const DASH = process.env.DASHBOARD_HTML
  || path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

function takeFunction(name) {
  const start = SRC.indexOf(`\nfunction ${name}(`);
  if (start < 0) throw new Error(`function ${name}() was not found in ${DASH}.`);
  const end = SRC.indexOf('\n}', start);
  if (end < 0) throw new Error(`could not find the end of ${name}()`);
  return SRC.slice(start + 1, end + 2);
}

const ctx = { JSON, Array, Object };
vm.createContext(ctx);
// teleLost() is a one-liner in the shipped file; takeFunction() needs a
// multi-line body, so take its line directly.
const tl = SRC.match(/\nfunction teleLost\(r\)\{[^\n]+\}/);
if (!tl) throw new Error('teleLost() was not found');
vm.runInContext(tl[0] + '\n' + takeFunction('navGpuTabs'), ctx);

const eq = (a, b) => assert.deepStrictEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)));
const hub = gpu => ({ now: { gpu } });
const noGpu = hub({ available: false });
const host = (name, gpus, extra) => Object.assign({ name, gpus }, extra || {});
const data = (models, summary) => ({ now: { models: models || [] }, model_summary: summary || [] });
let n = 0;
const test = (name, fn) => { fn(); n++; console.log('  ok ' + name); };

test('a GPU on a remote fleet host keeps the GPU tab on a GPU-less hub (#369)', () => {
  const fleet = { hosts: [host('local', []), host('gamer', [{ idx: 0, name: 'GTX 1660 SUPER' }])] };
  eq(ctx.navGpuTabs(noGpu, fleet, data()), { gpu: true, models: true });
});

test('a known model keeps AI Models without any GPU in the lab (#369)', () => {
  const fleet = { hosts: [host('local', [])] };
  eq(ctx.navGpuTabs(noGpu, fleet, data([{ model: 'qwen', service: 'lmstudio', host: 'local' }])),
    { gpu: false, models: true });
  eq(ctx.navGpuTabs(noGpu, fleet, data([], [{ model: 'qwen', service: 'lmstudio' }])),
    { gpu: false, models: true });
});

test('a lab with no GPU and no models hides both', () => {
  eq(ctx.navGpuTabs(noGpu, { hosts: [host('local', [])] }, data()), { gpu: false, models: false });
});

test('the hub\'s own GPU shows both, as before', () => {
  eq(ctx.navGpuTabs(hub({ available: true }), { hosts: [] }, data()), { gpu: true, models: true });
});

test('a broken GPU tool counts as a GPU, on the hub or a remote', () => {
  eq(ctx.navGpuTabs(hub({ available: false, telemetry: { ok: false } }), { hosts: [] }, data()).gpu, true);
  const fleet = { hosts: [host('box', [], { gpu_telemetry: { ok: false, error: 'driver mismatch' } })] };
  eq(ctx.navGpuTabs(noGpu, fleet, data()).gpu, true);
});

test('a payload that has not arrived yet hides nothing', () => {
  const fleet = { hosts: [] };
  eq(ctx.navGpuTabs(null, fleet, data()), { gpu: true, models: true });
  eq(ctx.navGpuTabs(noGpu, null, data()), { gpu: true, models: true });
  eq(ctx.navGpuTabs(noGpu, fleet, null), { gpu: true, models: true });
});

console.log(`${n} passed`);
