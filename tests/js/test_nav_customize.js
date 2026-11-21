#!/usr/bin/env node
/*
 * Sidebar customisation (#33): per-browser tab order and visibility.
 *
 * The rules worth pinning are the ones a browser smoke test would not notice
 * breaking: a reorder never crosses zones, Settings-zone tabs cannot be hidden,
 * a tab added by a later release still shows up for someone with a saved order,
 * a new zone renders without code changes, and reset really clears both keys.
 *
 * Same technique as test_tflops_cells.js: the functions are lifted out of the
 * shipped static/dashboard.html and run in a `vm` context.
 *
 * Run: node tests/js/test_nav_customize.js
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

function takeLine(re, what) {
  const m = SRC.match(re);
  if (!m) throw new Error(`${what} was not found in ${DASH}.`);
  return m[0];
}

function takeTabs() {
  const start = SRC.indexOf('\nconst TABS = [');
  if (start < 0) throw new Error('the TABS array was not found');
  const end = SRC.indexOf('\n];', start);
  return SRC.slice(start + 1, end + 3);
}

const FUNCS = ['navLoad', 'navStore', 'tabZone', 'navHideable', 'navZones', 'navHiddenSet',
  'navOrdered', 'navMove', 'navToggleHidden', 'navReset', 'navEditRow', 'buildNav'];

function build(extraTabs) {
  const store = {};
  const nl = { innerHTML: '', classList: { toggle() {} }, querySelectorAll: () => [], querySelector: () => null };
  const ctx = {
    JSON, Array, Set, Object, String,
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; },
    },
    document: { getElementById: id => (id === 'navlist' ? nl : null) },
    I18N: { t: (k, f) => (f != null ? f : k) },
    svgIc: () => '', NAV_ICON: {}, TAB: 'overview',
    showTab() {}, navWireEdit() {}, toast() {},
  };
  vm.createContext(ctx);
  const code = [
    takeTabs(),
    extraTabs ? `TABS.push(...${JSON.stringify(extraTabs)});` : '',
    takeLine(/const NAV_ORDER_KEY = [^\n]+/, 'NAV_ORDER_KEY'),
    takeLine(/const NAV_ZONE_LABEL = [^\n]+/, 'NAV_ZONE_LABEL'),
    takeLine(/let NAV_EDITING = [^\n]+/, 'NAV_EDITING'),
    takeLine(/const NAV_EYE = [^\n]+/, 'NAV_EYE'),
    takeLine(/const NAV_EYE_OFF = [^\n]+/, 'NAV_EYE_OFF'),
    takeLine(/const tabBtn = [^\n]+/, 'tabBtn'),
    ...FUNCS.map(takeFunction),
    'this.TABS = TABS; this.setEditing = v => { NAV_EDITING = v; };',
  ].join('\n');
  vm.runInContext(code, ctx);
  return { ctx, store, nl };
}

// Values made inside the vm are from another realm; normalise before comparing.
const eq = (a, b) => assert.deepStrictEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)));
const ids = list => list.map(t => t.id);
const rendered = html => [...html.matchAll(/data-t="([^"]+)"/g)].map(m => m[1]);
let n = 0;
const test = (name, fn) => { fn(); n++; console.log('  ok ' + name); };

test('default layout is the TABS order, grouped by zone', () => {
  const { ctx, nl } = build();
  const zones = ctx.navZones();
  eq(zones.slice(0, 3), ['monitoring', 'ai', 'settings']);
  const expected = zones.flatMap(z => ids(ctx.TABS.filter(t => ctx.tabZone(t) === z)));
  ctx.buildNav();
  eq(rendered(nl.innerHTML), expected);
});

test('reorder within a zone persists and renders', () => {
  const { ctx, store, nl } = build();
  assert.ok(ctx.navMove('uptime', 'overview'));            // move up: lands before
  eq(ids(ctx.navOrdered('monitoring')).slice(0, 2), ['uptime', 'overview']);
  assert.ok(ctx.navMove('uptime', 'gpu'));                 // move down: lands after
  eq(ids(ctx.navOrdered('monitoring')).slice(0, 3), ['overview', 'gpu', 'uptime']);
  assert.ok(JSON.parse(store.hl_tab_order).includes('uptime'));
  ctx.buildNav();
  assert.strictEqual(rendered(nl.innerHTML).indexOf('uptime'), 2);
});

test('a move across zones is refused', () => {
  const { ctx, store } = build();
  assert.strictEqual(ctx.navMove('models', 'overview'), false);
  assert.strictEqual(store.hl_tab_order, undefined);
});

test('hide removes the tab from the nav; show brings it back', () => {
  const { ctx, nl } = build();
  ctx.navToggleHidden('models');
  ctx.buildNav();
  assert.ok(!rendered(nl.innerHTML).includes('models'));
  ctx.navToggleHidden('models');
  ctx.buildNav();
  assert.ok(rendered(nl.innerHTML).includes('models'));
});

test('a zone whose tabs are all hidden drops its heading', () => {
  const { ctx, nl } = build();
  ctx.TABS.filter(t => t.zone === 'ai').forEach(t => ctx.navToggleHidden(t.id));
  ctx.buildNav();
  assert.ok(!nl.innerHTML.includes('>AI<'));
});

test('settings-zone tabs cannot be hidden, even via a hand-edited key', () => {
  const { ctx, store, nl } = build();
  ctx.navToggleHidden('alerts');
  assert.ok(!ctx.navHiddenSet().has('alerts'));
  store.hl_tab_hidden = JSON.stringify(['alerts', 'hosts']);
  ctx.buildNav();
  assert.ok(rendered(nl.innerHTML).includes('alerts'));
});

test('edit mode lists hidden tabs too, marked off', () => {
  const { ctx, nl } = build();
  ctx.navToggleHidden('gpu');
  ctx.setEditing(true);
  ctx.buildNav();
  assert.ok(/class="nrow nedit noff" data-t="gpu"/.test(nl.innerHTML));
  assert.ok(!/data-eye="alerts"/.test(nl.innerHTML), 'settings rows get no hide button');
});

test('a tab and zone added after the order was saved still appear', () => {
  const { ctx, store, nl } = build([{ id: 'agent', label: 'Agent', zone: 'agents' }]);
  store.hl_tab_order = JSON.stringify(['uptime', 'overview']);
  assert.ok(ctx.navZones().includes('agents'));
  ctx.buildNav();
  const r = rendered(nl.innerHTML);
  assert.ok(r.includes('agent'));
  assert.ok(nl.innerHTML.includes('>Agents<'), 'unknown zone gets a capitalised fallback label');
  eq(r.slice(0, 2), ['uptime', 'overview']);
});

test('corrupt storage falls back to defaults', () => {
  const { ctx, store } = build();
  store.hl_tab_order = '{not json';
  store.hl_tab_hidden = '"models"';
  eq(ids(ctx.navOrdered('ai')), ids(ctx.TABS.filter(t => t.zone === 'ai')));
  assert.strictEqual(ctx.navHiddenSet().size, 0);
});

test('reset clears both keys', () => {
  const { ctx, store } = build();
  ctx.navMove('uptime', 'overview');
  ctx.navToggleHidden('models');
  ctx.navReset();
  assert.strictEqual(store.hl_tab_order, undefined);
  assert.strictEqual(store.hl_tab_hidden, undefined);
});

console.log(`nav customise: ${n} passed`);
