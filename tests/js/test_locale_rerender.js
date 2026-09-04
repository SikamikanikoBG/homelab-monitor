#!/usr/bin/env node
/*
 * Locale changes hydrate static markup inside I18N.set(), then the
 * `lang:changed` listener rebuilds the dashboard pieces whose text is produced
 * by JavaScript. A browser smoke test can miss stale labels in tabs that were
 * not open during the run, so exercise every branch of that listener here.
 *
 * As with test_refresh_loop.js and test_tflops_cells.js, the listener is lifted
 * directly from static/dashboard.html and evaluated in a `vm` context. The
 * assertions therefore cover the code that ships rather than a test copy.
 *
 * Run: node tests/js/test_locale_rerender.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DASH = process.env.DASHBOARD_HTML
  || path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

function takeLangChangedListener() {
  const marker = "\ndocument.addEventListener('lang:changed'";
  const start = SRC.indexOf(marker);
  if (start < 0) {
    throw new Error(
      `the lang:changed listener was not found in ${DASH}.\n` +
      '       Either the locale re-render path was renamed, or this extractor\n' +
      '       needs updating to follow its new entry point.');
  }

  // The listener itself closes at column zero. Nested callbacks are indented,
  // so this does not stop at one of the safe(() => ...) calls inside it.
  const end = SRC.indexOf('\n});', start);
  if (end < 0) throw new Error('could not find the end of the lang:changed listener');
  return SRC.slice(start + 1, end + 4);
}

const LISTENER = takeLangChangedListener();
const VIEW_RENDERERS = [
  'renderServicesTab',
  'renderExperiments',
  'renderCosts',
  'renderNetwork',
  'renderSecurity',
  'loadHosts',
  'renderDisksTab',
];

function build(tab, options = {}) {
  const calls = [];
  const listeners = {};
  const disksBody = { dataset: { sig: 'cached-before-locale-change' } };
  const fail = new Set(options.fail || []);

  const record = name => (...args) => {
    calls.push({ name, args, diskSig: disksBody.dataset.sig });
    if (fail.has(name)) throw new Error(`${name} failed`);
  };

  const document = {
    addEventListener(type, listener) { listeners[type] = listener; },
    getElementById(id) { return id === 'disksbody' ? disksBody : null; },
  };

  const ctx = {
    document,
    TAB: tab,
    FLEET: options.withData === false ? null : { hosts: [] },
    D: options.withData === false ? null : { now: {} },
    HH: options.withData === false ? null : { health: {} },
    buildNav: record('buildNav'),
    applyTheme: record('applyTheme'),
    buildLangSwitch: record('buildLangSwitch'),
    renderFleet: record('renderFleet'),
    renderData: record('renderData'),
    renderHealth: record('renderHealth'),
  };
  for (const name of VIEW_RENDERERS) ctx[name] = record(name);

  vm.createContext(ctx);
  vm.runInContext(LISTENER, ctx);
  if (typeof listeners['lang:changed'] !== 'function') {
    throw new Error('the extracted code did not register a lang:changed listener');
  }

  return {
    calls,
    disksBody,
    fire: () => listeners['lang:changed'](),
  };
}

let checks = 0, failures = 0;
function check(what, ok, detail) {
  checks++;
  if (ok) { console.log(`  ok   ${what}`); }
  else { failures++; console.log(`  FAIL ${what}${detail ? ' — ' + detail : ''}`); }
}
function section(title) { console.log(`\n${title}`); }
function names(run) { return run.calls.map(call => call.name); }

section('shell and loaded data — every JS-built label is refreshed');
{
  const run = build('overview');
  run.fire();
  const actual = names(run);
  const expected = [
    'buildNav', 'applyTheme', 'buildLangSwitch',
    'renderFleet', 'renderData', 'renderHealth',
  ];
  check('the shell and loaded datasets re-render in order',
    JSON.stringify(actual) === JSON.stringify(expected), JSON.stringify(actual));
  check('an unrelated tab renderer is not called',
    !actual.some(name => VIEW_RENDERERS.includes(name)), JSON.stringify(actual));
}

section('not-yet-loaded data — locale switching stays safe during startup');
{
  const run = build('overview', { withData: false });
  run.fire();
  const actual = names(run);
  check('the shell still re-renders',
    JSON.stringify(actual) === JSON.stringify(['buildNav', 'applyTheme', 'buildLangSwitch']),
    JSON.stringify(actual));
  check('absent fleet, dashboard, and health payloads are not rendered',
    !actual.includes('renderFleet') && !actual.includes('renderData') && !actual.includes('renderHealth'),
    JSON.stringify(actual));
}

section('active views — only the visible tab takes the locale change');
{
  const cases = [
    ['services', 'renderServicesTab', []],
    ['experiments', 'renderExperiments', [true]],
    ['costs', 'renderCosts', [true]],
    ['network', 'renderNetwork', []],
    ['security', 'renderSecurity', []],
    ['hosts', 'loadHosts', []],
    ['disks', 'renderDisksTab', []],
  ];

  for (const [tab, renderer, expectedArgs] of cases) {
    const run = build(tab, { withData: false });
    run.fire();
    const viewCalls = run.calls.filter(call => VIEW_RENDERERS.includes(call.name));
    check(`${tab} re-renders through ${renderer} only`,
      viewCalls.length === 1 && viewCalls[0].name === renderer,
      JSON.stringify(viewCalls.map(call => call.name)));
    check(`${renderer} receives the expected refresh arguments`,
      JSON.stringify(viewCalls[0] && viewCalls[0].args) === JSON.stringify(expectedArgs),
      JSON.stringify(viewCalls[0] && viewCalls[0].args));
  }
}

section('disks — cached row signatures are invalidated before repaint');
{
  const run = build('disks', { withData: false });
  run.fire();
  const repaint = run.calls.find(call => call.name === 'renderDisksTab');
  check('the cached signature is cleared', run.disksBody.dataset.sig === '', run.disksBody.dataset.sig);
  check('the disk renderer observes the cleared signature', repaint && repaint.diskSig === '',
    repaint && repaint.diskSig);
}

section('failure isolation — one stale view cannot block the rest');
{
  const cases = [
    ['buildNav', 'services', 'applyTheme'],
    ['applyTheme', 'services', 'buildLangSwitch'],
    ['buildLangSwitch', 'services', 'renderFleet'],
    ['renderFleet', 'services', 'renderData'],
    ['renderData', 'services', 'renderHealth'],
    ['renderHealth', 'services', 'renderServicesTab'],
    ['renderServicesTab', 'services', null],
    ['renderExperiments', 'experiments', null],
    ['renderCosts', 'costs', null],
    ['renderNetwork', 'network', null],
    ['renderSecurity', 'security', null],
    ['loadHosts', 'hosts', null],
    ['renderDisksTab', 'disks', null],
  ];

  for (const [failing, tab, nextCall] of cases) {
    const run = build(tab, { fail: [failing] });
    let escaped = null;
    try { run.fire(); } catch (error) { escaped = error; }
    const actual = names(run);
    check(`${failing} failure does not escape the locale event`,
      escaped === null, escaped && escaped.message);
    if (nextCall) {
      check(`${nextCall} still runs after ${failing} fails`,
        actual.includes(nextCall), JSON.stringify(actual));
    }
  }
}

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) { console.error(`${failures} check(s) FAILED`); process.exit(1); }
