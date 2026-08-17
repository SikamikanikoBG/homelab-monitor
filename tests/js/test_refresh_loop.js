#!/usr/bin/env node
/*
 * Behavioural tests for the dashboard's history-refresh loop and live tail.
 *
 * The client is one 745 KB HTML file with no build step, so there is nothing to
 * import. Instead this lifts the functions under test straight out of
 * static/dashboard.html and runs them in a `vm` context against a fake clock —
 * the shipped source is never modified, patched or duplicated, so the test can
 * only ever pass by the real code behaving.
 *
 * Covers the four ways the refresh loop was found to stop refreshing:
 *   R2  a throw out of a synchronous renderer killed the chain permanently
 *   R3  a flapping SSE reconnect reset the timer faster than it could fire
 *   R4  LIVE_ON tracked connection events, so a dead-but-open stream froze
 *       the cadence and silently stopped the fleet poll entirely
 *   R7  a range switch never re-armed the timer with the new period
 * plus the live-tail merge that keeps the newest chart point moving between
 * history polls.
 *
 * Run: node tests/js/test_refresh_loop.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// DASHBOARD_HTML lets the suite be pointed at another build of the page — used
// to confirm these tests genuinely fail against the version that shipped the bug.
const DASH = process.env.DASHBOARD_HTML
  || path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

// ── extraction ───────────────────────────────────────────────────────────────
// Top-level declarations in dashboard.html start at column 0 and a function's
// closing brace is the next `}` at column 0. That is enough structure to slice
// out exactly the definitions under test without parsing the whole file.
function missing(what) {
  return new Error(
    `${what} was not found in ${DASH}.\n` +
    '       Either the page predates the chart-refresh fix (nothing to test — these\n' +
    '       functions arrived with it), or the definition was renamed and this\n' +
    '       extractor needs updating to match.');
}

function takeFunction(name) {
  const start = SRC.indexOf(`\nfunction ${name}(`);
  if (start < 0) throw missing(`function ${name}()`);
  const end = SRC.indexOf('\n}', start);
  if (end < 0) throw new Error(`could not find the end of ${name}() — is it still a top-level function?`);
  return SRC.slice(start + 1, end + 2);
}

function takeLine(re, what) {
  const m = SRC.match(re);
  if (!m) throw missing(what);
  return m[0];
}

// ── fake clock ───────────────────────────────────────────────────────────────
let NOW = 1700000000000, SEQ = 0;
const timers = new Map();
const setTimeout_ = (fn, ms) => { const id = ++SEQ; timers.set(id, { at: NOW + ms, fn }); return id; };
const clearTimeout_ = (id) => { timers.delete(id); };
// Throws that escape a timer callback are recorded rather than propagated. An
// escape IS a failure — it is exactly the v0.30.0 bug — but letting it kill the
// node process turns a regression into a stack trace with no indication of which
// invariant broke. The R2 suite asserts on `escaped` and reports it as a FAIL.
let escaped = [];
function advance(ms) {
  const end = NOW + ms;
  for (;;) {
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next.t.at)) next = { id, t };
    if (!next) break;
    NOW = next.t.at;
    timers.delete(next.id);
    try { next.t.fn(); }
    catch (e) { escaped.push(`${e.constructor.name}: ${e.message}`); }
  }
  NOW = end;
}
const pending = () => timers.size;

// ── context ──────────────────────────────────────────────────────────────────
function build(opts = {}) {
  const ctx = {
    // mutable state the shipped code reads and writes
    LIVE_ON: opts.liveOn || false,
    HIST_TIMER: null,
    HIST_NEXT_AT: 0,
    LIVE_LAST_FRAME: 0,
    LIVE_ES: opts.liveEs === undefined ? {} : opts.liveEs,
    D: opts.D || null,
    GPUC: opts.GPUC || null,
    LIVE_TAIL: { b: null, n: 0 },
    GPU_TAIL: { b: null, n: 0 },
    TAB: opts.tab || 'overview',
    CURRENT_HOST: opts.host || 'local',
    GPU_VIEW: opts.gpuView || 'card',
    LOCAL_ONLY_TABS: new Set(['models', 'experiments', 'containers']),
    // counters
    loadDataCalls: 0, loadFleetCalls: 0, loadHealthCalls: 0, openStreamCalls: 0,
    buildChartsCalls: 0, gpuChartCalls: 0,
    // environment
    Math, JSON, Set, Array, Object,
    Date: { now: () => NOW },
    setTimeout: setTimeout_, clearTimeout: clearTimeout_,
    document: { hidden: false, getElementById: () => ({ checked: opts.auto !== false }) },
  };
  // Captured rather than printed: the loop is supposed to log what it caught,
  // and the R2 test asserts on that instead of letting a stack trace spam CI.
  ctx.errors = [];
  ctx.console = { log: () => {}, warn: () => {}, error: (...a) => ctx.errors.push(a.map(String).join(' ')) };
  ctx.autoOn = () => { const el = ctx.document.getElementById('auto'); return !el || el.checked; };
  ctx.loadData = () => { ctx.loadDataCalls++; };
  ctx.loadHealth = () => { ctx.loadHealthCalls++; };
  ctx.loadFleet = () => { ctx.loadFleetCalls++; };
  ctx.openLiveStream = () => { ctx.openStreamCalls++; ctx.LIVE_ES = {}; };
  ctx.renderHostTab = () => {};
  ctx.renderNetwork = () => {};
  ctx.renderSecurity = () => {};
  ctx.renderGpuTab = () => {};
  ctx.renderLocalOnlyNotice = () => {};
  ctx.renderDiskIo = () => {
    if (opts.diskIoThrows) throw new TypeError("Cannot read properties of undefined (reading 'items')");
  };
  ctx.buildCharts = () => { ctx.buildChartsCalls++; };
  ctx.renderGpuMetricChart = () => { ctx.gpuChartCalls++; };
  ctx.renderGpuCombined = () => { ctx.gpuChartCalls++; };
  ctx.renderGpuKpis = () => {};
  vm.createContext(ctx);
  vm.runInContext([
    takeFunction('liveStale'),
    takeFunction('noteLiveFrame'),
    takeFunction('historyPeriod'),
    takeFunction('refreshHistory'),
    takeFunction('scheduleHistory'),
    takeLine(/^const _tailMean=.*$/m, 'the _tailMean helper'),
    takeFunction('_tailPush'),
    takeFunction('_tailSlot'),
    takeFunction('mergeLiveTail'),
    takeFunction('mergeGpuTail'),
    takeFunction('paintLiveCharts'),
  ].join('\n\n'), ctx);
  return ctx;
}

// ── assertions ───────────────────────────────────────────────────────────────
let failures = 0, checks = 0;
function check(label, cond, detail) {
  checks++;
  if (cond) { console.log(`  ok   ${label}`); return; }
  failures++;
  console.log(`  FAIL ${label}${detail ? '\n         ' + detail : ''}`);
}
function suite(name) { console.log(`\n${name}`); }

function reset() { NOW = 1700000000000; timers.clear(); escaped = []; }

// ── R2: a throwing renderer must not kill the chain ──────────────────────────
suite('R2  a sync throw inside refreshHistory() must not stop the loop');
{
  reset();
  const c = build({ tab: 'diskio', diskIoThrows: true, liveOn: false });
  c.scheduleHistory();
  advance(20000);
  const afterThrow = c.loadDataCalls;
  check('the tick still ran', afterThrow >= 1, `refreshes=${afterThrow}`);
  check('a timer is still pending after the throw', pending() === 1, `pending=${pending()}`);
  advance(600000);
  check('still refreshing 10 minutes later',
    c.loadDataCalls >= 40, `refreshes=${c.loadDataCalls} (expected ~41 at 15s)`);
  check('the failure was logged, not swallowed',
    c.errors.length > 0 && c.errors[0].includes('refreshHistory failed'),
    `errors=${JSON.stringify(c.errors.slice(0, 1))}`);
  check('no throw escaped the timer callback', escaped.length === 0,
    `escaped=${JSON.stringify(escaped.slice(0, 2))} — the reschedule is not protected`);
}

// ── R3: reconnect flapping must not starve the timer ─────────────────────────
suite('R3  a 3s SSE reconnect flap must not starve the 15s poll');
{
  reset();
  const c = build({ liveOn: false });
  c.scheduleHistory();
  for (let i = 0; i < 100; i++) { advance(3000); c.LIVE_ON = false; c.scheduleHistory(); }
  check('refreshes still happened during 300s of flapping',
    c.loadDataCalls >= 15, `refreshes=${c.loadDataCalls} (expected ~20)`);
  check('no timer stacking', pending() === 1, `pending=${pending()}`);
}

// ── R3b: a re-arm must never push the fire time further out ──────────────────
suite('R3b a re-arm must never delay an already-pending tick');
{
  reset();
  const c = build({ liveOn: false });
  c.scheduleHistory();                       // 15s
  advance(14000);
  c.LIVE_ON = true; c.D = { bucket_sec: 60 };  // would compute a 60s period
  c.scheduleHistory();
  advance(1100);
  check('the pending 15s tick still fired on time', c.loadDataCalls === 1,
    `refreshes=${c.loadDataCalls}`);
}

// ── R3c: a shorter period must be adopted immediately ────────────────────────
suite('R3c a genuinely sooner period must replace the pending tick');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60 } });
  c.scheduleHistory();                       // 60s
  advance(1000);
  c.LIVE_ON = false;                         // stream lost -> 15s
  c.scheduleHistory();
  advance(15100);
  check('re-armed at the shorter period', c.loadDataCalls === 1, `refreshes=${c.loadDataCalls}`);
}

// ── R4: watchdog on a dead-but-open stream ───────────────────────────────────
suite('R4  a stream that stops delivering must be demoted');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60, fast_interval: 2 } });
  c.noteLiveFrame();                          // one frame arrives, then silence
  check('period is the slow cadence while live', c.historyPeriod() === 60000,
    `period=${c.historyPeriod()}`);
  check('fleet is not polled while genuinely live', (() => {
    c.refreshHistory(); return c.loadFleetCalls === 0;
  })(), `loadFleet=${c.loadFleetCalls}`);
  advance(60000);                             // 60s of silence, > max(6*2s, 45s)
  check('watchdog reports the stream stale', c.liveStale() === true);
  c.refreshHistory();
  check('LIVE_ON demoted to false', c.LIVE_ON === false);
  check('period fell back to 15s', c.historyPeriod() === 15000, `period=${c.historyPeriod()}`);
  check('fleet polling resumed', c.loadFleetCalls === 1, `loadFleet=${c.loadFleetCalls}`);
}

// ── R4a: historyPeriod() must consult the watchdog on its own ────────────────
// Without this the `|| liveStale()` clause is dead code as far as the suite is
// concerned: every other R4 check goes through refreshHistory(), which demotes
// LIVE_ON first, so historyPeriod() answers via its `!LIVE_ON` branch and the
// staleness clause is never exercised. Assert it directly, LIVE_ON still true.
suite('R4a historyPeriod() alone must fall back on a stale stream');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60, fast_interval: 2 } });
  c.noteLiveFrame();
  check('slow cadence while frames are arriving', c.historyPeriod() === 60000,
    `period=${c.historyPeriod()}`);
  advance(120000);                            // two minutes of silence
  check('LIVE_ON is still true (nothing demoted it)', c.LIVE_ON === true);
  check('but the period already fell back to 15s', c.historyPeriod() === 15000,
    `period=${c.historyPeriod()} — historyPeriod() is not consulting liveStale()`);
}

// ── R4b: a live stream must not be demoted ───────────────────────────────────
suite('R4b a stream still delivering frames must stay live');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60, fast_interval: 2 } });
  for (let i = 0; i < 30; i++) { c.noteLiveFrame(); advance(2000); }
  check('watchdog leaves a healthy stream alone', c.liveStale() === false);
  c.refreshHistory();
  check('LIVE_ON still true', c.LIVE_ON === true);
  check('fleet still left to the stream', c.loadFleetCalls === 0, `loadFleet=${c.loadFleetCalls}`);
}

// ── R7: range switch re-arms with the new period ─────────────────────────────
suite('R7  a range switch must re-arm the timer with the new period');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 240 } });   // 24h -> 60s cap
  c.scheduleHistory();
  advance(1000);
  c.D = { bucket_sec: 10 };                    // user picks 1h
  c.scheduleHistory(true);                     // what the .rb handler now does
  advance(15100);
  check('refreshed at the new 15s period, not the old 60s one',
    c.loadDataCalls === 1, `refreshes=${c.loadDataCalls}`);
}

// ── stream self-heal ─────────────────────────────────────────────────────────
suite('R8  a refresh tick must reopen a stream that failed terminally');
{
  reset();
  const c = build({ liveOn: false, liveEs: null });
  c.refreshHistory();
  check('openLiveStream() was called when LIVE_ES was null', c.openStreamCalls === 1,
    `openStreamCalls=${c.openStreamCalls}`);
}

// ── live tail ────────────────────────────────────────────────────────────────
function mkD(lastBucket, bucketSec) {
  const labels = [], mkArr = () => [];
  const n = 5;
  for (let i = n - 1; i >= 0; i--) labels.push(lastBucket - i * bucketSec);
  const arr = (v) => Array(n).fill(v);
  return {
    bucket_sec: bucketSec, interval: 10, labels,
    total: {
      cpu: arr(10), ram_used: arr(1000), ram_total: arr(4000), load1: arr(1), ctemp: arr(40),
      util: arr(5), mem: arr(500), mempk: arr(500), power: arr(20), temp: arr(30),
    },
  };
}

suite('tail  the newest bucket keeps moving between history polls');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const c = build({ D });
  // a frame 20s into the open bucket
  const ok = c.mergeLiveTail({ fast_ts: last + 20, host: { cpu: 50, ram_used: 2000, ram_total: 4000, load1: 3, ctemp: 55 }, gpu_avail: false });
  check('merge reported success', ok === true);
  check('label window did not grow', D.labels.length === 5, `len=${D.labels.length}`);
  check('newest label unchanged (same bucket)', D.labels[4] === last, `last=${D.labels[4]}`);
  check('cpu moved toward the live value', D.total.cpu[4] > 10 && D.total.cpu[4] <= 50,
    `cpu=${D.total.cpu[4]}`);
  check('a GPU-less box grew no GPU value', D.total.util[4] === 5, `util=${D.total.util[4]}`);
}

suite('tail  a new bucket appends and slides the window');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const oldest = D.labels[0];
  const c = build({ D });
  c.mergeLiveTail({ fast_ts: last + bk + 5, host: { cpu: 70 }, gpu_avail: false });
  check('window length is unchanged', D.labels.length === 5, `len=${D.labels.length}`);
  check('a new bucket was appended', D.labels[4] === last + bk, `last=${D.labels[4]}`);
  check('the oldest bucket was dropped', D.labels[0] !== oldest);
  check('the new point took the live value', D.total.cpu[4] === 70, `cpu=${D.total.cpu[4]}`);
  check('every series slid together', D.total.power.length === 5, `len=${D.total.power.length}`);
}

suite('tail  VRAM is a step metric and must not be averaged away');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const c = build({ D });
  // a model loads: VRAM jumps from 500 MB to 20 GB inside the open bucket
  c.mergeLiveTail({ fast_ts: last + 20, host: {}, gpu_avail: true, util: 90, mem_used: 20000, power: 250, temp: 70 });
  check('VRAM shows the loaded value, not a mean', D.total.mem[4] === 20000, `mem=${D.total.mem[4]}`);
  check('the VRAM peak was captured', D.total.mempk[4] === 20000, `mempk=${D.total.mempk[4]}`);
  check('util was averaged, not slammed', D.total.util[4] > 5 && D.total.util[4] < 90,
    `util=${D.total.util[4]}`);
}

suite('tail  an old frame is ignored, and a remote host is left alone');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const c = build({ D });
  const before = D.total.cpu[4];
  const ok = c.mergeLiveTail({ fast_ts: last - 5 * bk, host: { cpu: 99 }, gpu_avail: false });
  check('a frame older than the series is rejected', ok === false);
  check('nothing was written', D.total.cpu[4] === before, `cpu=${D.total.cpu[4]}`);

  const GPUC = {
    has_gpu: true, host: 'local', bucket_sec: bk, interval: 10,
    labels: D.labels.slice(),
    combined: { util: [1, 1, 1, 1, 1], vram: [10, 10, 10, 10, 10], power: [1, 1, 1, 1, 1], temp_max: [1, 1, 1, 1, 1] },
    cards: [], now_pooled: {},
  };
  const c2 = build({ GPUC, host: 'vader' });
  check('a remote host does not get the hub\'s live frame',
    c2.mergeGpuTail({ fast_ts: last + 20, gpu_avail: true, util: 99, mem_used: 9999, gpus: [] }) === false);
  check('the remote series is untouched', GPUC.combined.util[4] === 1, `util=${GPUC.combined.util[4]}`);
}

suite('tail  the GPU tab rides the same fold');
{
  reset();
  const bk = 60, last = 1786788000;
  const GPUC = {
    has_gpu: true, host: 'local', bucket_sec: bk, interval: 10,
    labels: [last - 4 * bk, last - 3 * bk, last - 2 * bk, last - bk, last],
    combined: { util: [1, 1, 1, 1, 1], vram: [10, 10, 10, 10, 10], vram_total: [5120, 5120, 5120, 5120, 5120], power: [5, 5, 5, 5, 5], temp_max: [30, 30, 30, 30, 30], fan_max: [40, 40, 40, 40, 40] },
    cards: [{ idx: 0, series: { util: [1, 1, 1, 1, 1], vram: [10, 10, 10, 10, 10], temp: [30, 30, 30, 30, 30] }, now: {} }],
    now_pooled: { util: 1, mem_used: 10, power: 5, temp_max: 30 },
  };
  const c = build({ GPUC, host: 'local' });
  const ok = c.mergeGpuTail({
    fast_ts: last + 20, gpu_avail: true, util: 95, mem_used: 4800, mem_total: 5120,
    power: 70, temp: 72, gpus: [{ idx: 0, util: 95, mem_used: 4800, mem_total: 5120, power: 70, temp: 72, fan: 80 }],
  });
  check('merge reported success', ok === true);
  check('pooled VRAM took the latest value', GPUC.combined.vram[4] === 4800,
    `vram=${GPUC.combined.vram[4]}`);
  check('per-card VRAM took the latest value', GPUC.cards[0].series.vram[4] === 4800,
    `vram=${GPUC.cards[0].series.vram[4]}`);
  check('hottest card is a max, not a mean', GPUC.combined.temp_max[4] === 72,
    `temp_max=${GPUC.combined.temp_max[4]}`);
  check('KPI tiles were refreshed from the frame', GPUC.now_pooled.util === 95,
    `util=${GPUC.now_pooled.util}`);
}

// ── host scope: the live repaint is hub-only ─────────────────────────────────
// D is always this hub's own series. Driving the System chart from a live frame
// while a remote machine is selected animates the hub's CPU/RAM/load under the
// remote's name — and now it would do so every two seconds instead of sitting
// still, which reads as authoritative rather than obviously stale.
suite('scope  the live repaint must not paint hub data under a remote host');
{
  reset();
  const c = build({ tab: 'host', host: 'local' });
  c.paintLiveCharts();
  check('the hub repaints its own System chart', c.buildChartsCalls === 1,
    `buildCharts=${c.buildChartsCalls}`);

  const c2 = build({ tab: 'host', host: 'vader' });
  c2.paintLiveCharts();
  check('a remote host does not repaint from the hub frame', c2.buildChartsCalls === 0,
    `buildCharts=${c2.buildChartsCalls} — the hub's series would animate under the remote's name`);
}

// ── tail: everything indexed against D.labels slides together ────────────────
// D.total is not the only thing the length of D.labels. The per-service memory
// series, the "other" remainder and every per-device disk-I/O series are read
// with the same indices, so a rollover that slides only D.total leaves them one
// bucket out of step until the next authoritative poll.
suite('tail  sibling series slide with the window on a bucket rollover');
{
  reset();
  const bk = 60, last = 1786788000;
  const five = v => [v, v, v, v, v];
  const D = {
    bucket_sec: bk, interval: 10,
    labels: [last - 4 * bk, last - 3 * bk, last - 2 * bk, last - bk, last],
    total: { cpu: five(3), ram_used: five(8000), ram_total: five(16000), load1: five(1) },
    services: { 'ollama-embed': five(500) },
    other: five(200),
    disk_io: { md0: { read_mb_s: five(1), write_mb_s: five(2), util_pct: five(3) } },
  };
  const c = build({ D, host: 'local' });
  // A frame in the NEXT bucket forces the window to slide.
  const ok = c.mergeLiveTail({ fast_ts: last + bk + 5, host: { cpu: 40 } });
  check('merge reported success', ok === true);
  check('the window still holds five buckets', D.labels.length === 5,
    `labels=${D.labels.length}`);
  check('labels slid to the new bucket', D.labels[4] === last + bk,
    `last label=${D.labels[4]}`);
  check('D.total slid with the labels', D.total.cpu.length === 5 && D.total.cpu[4] === 40,
    `cpu=${JSON.stringify(D.total.cpu)}`);
  check('the per-service series slid too',
    D.services['ollama-embed'].length === 5 && D.services['ollama-embed'][4] === null,
    `svc=${JSON.stringify(D.services['ollama-embed'])}`);
  check('the "other" remainder slid too',
    D.other.length === 5 && D.other[4] === null, `other=${JSON.stringify(D.other)}`);
  check('every disk-I/O series slid too',
    D.disk_io.md0.read_mb_s.length === 5 && D.disk_io.md0.write_mb_s.length === 5
      && D.disk_io.md0.util_pct.length === 5 && D.disk_io.md0.read_mb_s[4] === null,
    `read=${JSON.stringify(D.disk_io.md0.read_mb_s)}`);
  check('the oldest bucket was dropped, not kept',
    D.services['ollama-embed'][0] === 500 && D.labels[0] === last - 3 * bk,
    `first label=${D.labels[0]}`);
}

// ── GPU small multiples refresh in place ─────────────────────────────────────
// The per-card grid used to repaint only on the 60s history fetch, because the
// full render replaces #pergpu wholesale and re-binds every handler. The result
// was a grid of temp/watt/util cards sitting still while the detail chart behind
// them tracked live. The live path now writes each card's body and leaves the
// wrapper — handlers, focus, tabindex — alone.
suite('gpu   the small-multiples cards refresh in place off a live frame');
{
  reset();

  // Minimal DOM: a #pergpu box holding one wrapper per card. Handlers live on
  // the wrapper, so the test asserts the wrapper objects are never replaced.
  function mkBox(idxs, opts = {}) {
    const kids = idxs.map(i => ({
      dataset: { idx: String(i) }, className: 'gpu-mini st-ok',
      innerHTML: `ORIGINAL-${i}`, onclick: () => {},
    }));
    return {
      hidden: opts.hidden || false, innerHTML: 'GRID', _kids: kids,
      querySelectorAll: () => kids,
    };
  }
  function ctxWith(box, opts = {}) {
    const c = build({ tab: 'gpu' });
    c.GPU_VIEW = opts.view || 'card';
    c.GPU_SPARK_ROWS = ['util', 'temp'];
    c.gpuSharedRange = () => ({ min: 0, max: 100 });
    c.gpuMiniCardBody = (card) => `BODY-${card.idx}-${card.now.temp}`;
    c.document.getElementById = (id) => (id === 'pergpu' ? box : null);
    vm.runInContext(takeFunction('updateGpuCardsLive'), c);
    return c;
  }

  const d = (t0, t1) => ({
    has_gpu: true,
    cards: [{ idx: 0, status: 'ok', now: { temp: t0 } }, { idx: 1, status: 'ok', now: { temp: t1 } }],
  });

  const box = mkBox([0, 1]);
  const c = ctxWith(box);
  const ok = c.updateGpuCardsLive(d(61, 70));
  check('the live frame repainted the cards', ok === true);
  check('card 0 took the new reading', box._kids[0].innerHTML === 'BODY-0-61',
    `html=${box._kids[0].innerHTML}`);
  check('card 1 took the new reading', box._kids[1].innerHTML === 'BODY-1-70',
    `html=${box._kids[1].innerHTML}`);
  check('the grid itself was NOT rebuilt', box.innerHTML === 'GRID',
    'replacing #pergpu would drop every click handler and the focus ring');
  check('the wrapper elements were not replaced',
    typeof box._kids[0].onclick === 'function', 'the click handler must survive');

  // A second frame keeps moving the numbers.
  c.updateGpuCardsLive(d(72, 75));
  check('a later frame moves them again', box._kids[0].innerHTML === 'BODY-0-72',
    `html=${box._kids[0].innerHTML}`);

  // Structural change: a card appeared. The full render owns that, not this.
  const c2 = ctxWith(mkBox([0, 1]));
  check('a changed card count defers to the full render',
    c2.updateGpuCardsLive({
      has_gpu: true,
      cards: [{ idx: 0, status: 'ok', now: { temp: 1 } }, { idx: 1, status: 'ok', now: { temp: 2 } },
              { idx: 2, status: 'ok', now: { temp: 3 } }],
    }) === false);

  // A status change still lands, since it rides on the wrapper's class.
  const box3 = mkBox([0, 1]);
  const c3 = ctxWith(box3);
  c3.updateGpuCardsLive({
    has_gpu: true,
    cards: [{ idx: 0, status: 'hot', now: { temp: 90 } }, { idx: 1, status: 'ok', now: { temp: 40 } }],
  });
  check('a card that turned hot got the new status class',
    box3._kids[0].className === 'gpu-mini st-hot', `class=${box3._kids[0].className}`);

  // Not on the small-multiples view, or hidden: nothing to do.
  check('the metric view is left to the chart path',
    ctxWith(mkBox([0, 1]), { view: 'metric' }).updateGpuCardsLive(d(1, 2)) === false);
  check('a hidden grid is not painted',
    ctxWith(mkBox([0, 1], { hidden: true })).updateGpuCardsLive(d(1, 2)) === false);
}

// ── in-place painters: the KPI row and disk bars ─────────────────────────────
// The live lane repaints these every ~2s. Rebuilding their markup destroyed and
// recreated the subtree on every frame, which re-composited the backdrop-filter
// on the surrounding .card — the flicker. They are patched in place now.
//
// Both panels have a SECOND writer: renderHostTab() paints a remote host (or a
// "waiting for its first probe" message) into the same elements. A painter that
// trusted its cache against that markup would index off the end of it and throw,
// and a throw here kills the whole live frame.
suite('paint the live panels must repaint in place, and never patch foreign markup');
{
  reset();

  const node = () => ({ textContent: '' });
  const diskRow = () => ({ children: [
    { children: [node(), node()] },
    { classList: { contains: c => c === 'dbar' }, firstElementChild: { style: { width: '', background: '' } } },
  ] });
  const kpiCell = () => ({ children: [node(), node(), node()] });

  // innerHTML is modelled the way the parser behaves: assigning markup replaces
  // the children with one row per row-template found in it.
  function mkHost(kind) {
    const el = { _children: [], rebuilds: 0, _html: '' };
    Object.defineProperty(el, 'children', { get: () => el._children });
    Object.defineProperty(el, 'innerHTML', {
      get: () => el._html,
      set(html) {
        el._html = html; el.rebuilds++;
        const re = kind === 'disks' ? /class="dbar"/g : /class="kpi"/g;
        const n = (html.match(re) || []).length;
        el._children = Array.from({ length: n }, kind === 'disks' ? diskRow : kpiCell);
      },
    });
    return el;
  }
  // What renderHostTab() leaves behind: markup this painter did not build.
  const foreign = (el, kids) => { el._children = kids; };

  function ctx() {
    const c = build({});
    c.sev = pct => (pct >= 90 ? 'var(--crit)' : 'var(--ok)');
    vm.runInContext([
      takeLine(/^function setText\(.*$/m, 'the setText helper'),
      takeFunction('ourRows'),
      takeFunction('paintKpis'),
      takeFunction('paintDisks'),
    ].join('\n\n'), c);
    return c;
  }

  const disks = (pct) => [
    { mount: '/', used: 10, total: 100, pct },
    { mount: '/home', used: 20, total: 200, pct: 5 },
  ];

  // 1. The anti-flicker invariant: same mounts, new numbers, no rebuild.
  const c = ctx();
  const el = mkHost('disks');
  c.paintDisks(el, disks(50));
  const built = el.rebuilds;
  const firstRow = el.children[0];
  c.paintDisks(el, disks(60));
  check('a repaint with the same mounts does not rebuild the rows',
    el.rebuilds === built, `rebuilds ${built} -> ${el.rebuilds}`);
  check('the row objects survived the repaint', el.children[0] === firstRow,
    'rebuilding is what re-composites the .card blur behind them');
  check('the numbers still moved',
    el.children[0].children[1].firstElementChild.style.width === '60%',
    `width=${el.children[0].children[1].firstElementChild.style.width}`);
  check('the mount label is set as text, not markup',
    el.children[0].children[0].children[0].textContent === '💾 /');

  // 2. A mount appearing rebuilds, because the row count changed.
  c.paintDisks(el, disks(60).concat([{ mount: '/boot', used: 1, total: 2, pct: 50 }]));
  check('a new filesystem rebuilds the rows', el.rebuilds === built + 1);

  // 3. REGRESSION: renderHostTab left a "waiting for probe" message here, so the
  //    row count no longer matches the cache. Patching it would throw.
  const c3 = ctx();
  const el3 = mkHost('disks');
  c3.paintDisks(el3, disks(50));
  foreign(el3, [{ children: [] }]);              // the muted <div> message
  let threw = null;
  try { c3.paintDisks(el3, disks(50)); } catch (e) { threw = e; }
  check('coming back from an offline remote does not throw', threw === null,
    threw && String(threw));
  check('and the disk rows were rebuilt from scratch', el3.children.length === 2,
    `children=${el3.children.length}`);

  // 4. REGRESSION, harder: the foreign markup has the SAME row count, so only a
  //    shape check can catch it. This is a remote host with two disks whose rows
  //    this painter did not build.
  const c4 = ctx();
  const el4 = mkHost('disks');
  c4.paintDisks(el4, disks(50));
  foreign(el4, [kpiCell(), kpiCell()]);          // right count, wrong shape
  threw = null;
  try { c4.paintDisks(el4, disks(50)); } catch (e) { threw = e; }
  check('foreign rows of the same count do not throw', threw === null,
    threw && String(threw));
  check('they were replaced with real disk rows',
    el4.children.length === 2 && el4.children[0].children.length === 2,
    'a same-count cache hit must still verify the row shape');

  // 5. The KPI row gets the same treatment.
  const c5 = ctx();
  const el5 = mkHost('kpis');
  const items = v => [
    { v, l: 'CPU utilization', s: '4 cores' }, { v: '9%', l: 'RAM used', s: '1 / 2' },
    { v: '0.5', l: 'Load (1m)', s: 'of 4' },   { v: '1d', l: 'Uptime' },
    { v: '40 °C', l: 'CPU/system temp' },
  ];
  c5.paintKpis(el5, items('10%'));
  const kpiBuilt = el5.rebuilds, cell0 = el5.children[0];
  c5.paintKpis(el5, items('80%'));
  check('a KPI repaint does not rebuild the cells', el5.rebuilds === kpiBuilt);
  check('the KPI cell objects survived', el5.children[0] === cell0);
  check('the KPI value moved', el5.children[0].children[0].textContent === '80%');
  check('an absent sub-label is blank, not undefined',
    el5.children[3].children[2].textContent === '');

  foreign(el5, [{ children: [] }, { children: [] }, { children: [] }, { children: [] }, { children: [] }]);
  threw = null;
  try { c5.paintKpis(el5, items('12%')); } catch (e) { threw = e; }
  check('foreign KPI cells of the same count do not throw', threw === null,
    threw && String(threw));
  check('and they were rebuilt into real cells',
    el5.children.length === 5 && el5.children[0].children.length === 3);
}

// ── result ───────────────────────────────────────────────────────────────────
console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) { console.error(`${failures} check(s) FAILED`); process.exit(1); }
