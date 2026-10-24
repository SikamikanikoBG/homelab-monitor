#!/usr/bin/env node
/*
 * The agent UI's logic (static/agent.js, #354), lifted out and run in a vm:
 *   - model text is rendered safely (HTML escaped; only internal #tab links clickable),
 *   - the floating Ask button's visibility rules (agent off, kiosk, the Agent tab),
 *   - the policy page's Unattended column (medium/high/root locked),
 *   - folding live events into state, building chat items with chips and cards,
 *   - the Activity step lines,
 *   - every page global agent.js relies on still exists in dashboard.html.
 *
 * Run: node tests/js/test_agent_ui.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.join(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static', 'agent.js'), 'utf8');
const DASH = fs.readFileSync(path.join(ROOT, 'static', 'dashboard.html'), 'utf8');

function take(name) {
  const start = SRC.indexOf('\nfunction ' + name + '(');
  if (start < 0) throw new Error('function ' + name + '() not found in agent.js');
  const end = SRC.indexOf('\n}', start);
  return SRC.slice(start + 1, end + 2);
}
function takeConst(name) {
  const m = SRC.match(new RegExp('\\nconst ' + name + ' = [\\s\\S]*?;\\n'));
  if (!m) throw new Error('const ' + name + ' not found');
  return m[0].replace('const ' + name, 'var ' + name);
}

const ctx = { console, JSON, Object, Array, String, Number, Math, Date, RegExp, TAB: 'gpu', CURRENT_HOST: 'lab1' };
vm.createContext(ctx);
vm.runInContext([takeConst('AG_TABS'), take('agT'), take('agTf'), take('agEsc'), take('agDeepLink'), take('agMarkdown'),
  take('agContext'), take('agFabVisible'), take('agUnattended'), take('agReduce'), take('agChatItems'),
  take('agChipLabel'), take('agStepLine')].join('\n'), ctx);

let n = 0;
function check(name, fn) { fn(); n++; console.log('ok', name); }
const plain = o => JSON.parse(JSON.stringify(o));   // objects from the vm realm

check('deep links: only known tabs and safe host names', () => {
  assert.deepStrictEqual(plain(ctx.agDeepLink('#containers?host=lab1')), { tab: 'containers', host: 'lab1' });
  assert.deepStrictEqual(plain(ctx.agDeepLink('#gpu')), { tab: 'gpu', host: null });
  for (const bad of ['#evil', 'http://x', 'javascript:alert(1)', '#gpu?host=a"b', '#gpu?host=<x>', '', null]) {
    assert.strictEqual(ctx.agDeepLink(bad), null, String(bad));
  }
});

check('markdown escapes HTML first', () => {
  const out = ctx.agMarkdown('<img src=x onerror=alert(1)> **bold** `code`');
  assert.ok(!out.includes('<img'));
  assert.ok(out.includes('&lt;img'));
  assert.ok(out.includes('<b>bold</b>'));
  assert.ok(out.includes('<code>code</code>'));
});

check('markdown: internal links clickable, external links plain text', () => {
  const out = ctx.agMarkdown('See #containers?host=lab1 and [the GPU](#gpu) or [this](https://evil.example) https://evil.example');
  assert.ok(out.includes('<a href="#containers?host=lab1"'));
  assert.ok(out.includes('<a href="#gpu" data-ag-link="#gpu">the GPU</a>'));
  assert.ok(!out.includes('href="https'));
  assert.ok(out.includes('https://evil.example'));
});

check('markdown: quotes in a name cannot break out of an attribute', () => {
  const out = ctx.agMarkdown('container x" onmouseover="alert(1)');
  assert.ok(!/onmouseover="/.test(out));
});

check('markdown: lists and paragraphs', () => {
  const out = ctx.agMarkdown('Two things:\n- one\n- two\n\nDone.');
  assert.ok(out.includes('<ul><li>one</li><li>two</li></ul>'));
  assert.ok(out.includes('<p>Done.</p>'));
});

check('widget context sends the tab and host, never the agent tab itself', () => {
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.agContext())), { tab: 'gpu', host: 'lab1' });
  vm.runInContext('TAB = "agent"', ctx);
  assert.strictEqual(ctx.agContext().tab, null);
  vm.runInContext('TAB = "gpu"', ctx);
});

check('the Ask button: hidden when off, without a password, in kiosk mode, on the Agent tab', () => {
  const on = { password_set: true, enabled: true };
  assert.strictEqual(ctx.agFabVisible(on, 'overview', false), true);
  assert.strictEqual(ctx.agFabVisible(on, 'agent', false), false);
  assert.strictEqual(ctx.agFabVisible(on, 'overview', true), false);
  assert.strictEqual(ctx.agFabVisible({ password_set: true, enabled: false }, 'overview', false), false);
  assert.strictEqual(ctx.agFabVisible({ password_set: false, enabled: true }, 'overview', false), false);
  assert.strictEqual(ctx.agFabVisible(null, 'overview', false), false);
});

check('unattended column: medium/high/root locked unless unlocked', () => {
  const low = { risk: 'low' }, med = { risk: 'medium' }, high = { risk: 'high', unattended: 'never' }, root = { risk: 'medium', sudo: true };
  assert.strictEqual(ctx.agUnattended(low, 'allow').text, 'Allow · rate-limited');
  assert.strictEqual(ctx.agUnattended(med, 'allow', false).text, 'Ask · locked');
  assert.strictEqual(ctx.agUnattended(med, 'allow', false).unlockable, true);
  assert.strictEqual(ctx.agUnattended(med, 'allow', true).text, 'Allow · unlocked');
  assert.strictEqual(ctx.agUnattended(high, 'allow', true).text, 'Ask · locked');
  assert.strictEqual(ctx.agUnattended(high, 'allow', true).unlockable, undefined);
  assert.strictEqual(ctx.agUnattended(root, 'allow', true).unlockable, undefined);
  assert.strictEqual(ctx.agUnattended(low, 'deny').text, 'Deny');
  assert.strictEqual(ctx.agUnattended({ probe: true }, 'allow').locked, true);
});

check('events: text deltas accumulate, a message clears them and asks for a refetch', () => {
  const st = { streaming: {} };
  ctx.agReduce(st, { type: 'text', run_id: 'r1', text: 'Hel' });
  ctx.agReduce(st, { type: 'text', run_id: 'r1', text: 'lo' });
  assert.strictEqual(st.streaming.r1, 'Hello');
  const need = ctx.agReduce(st, { type: 'message', run_id: 'r1' });
  assert.strictEqual(need.conv, true);
  assert.strictEqual(st.streaming.r1, undefined);
  assert.strictEqual(ctx.agReduce(st, { type: 'approval' }).approvals, true);
  assert.strictEqual(ctx.agReduce(st, { type: 'task' }).tasks, true);
  assert.strictEqual(ctx.agReduce(st, null).conv, false);
});

check('replaying the same events twice does not duplicate state', () => {
  const st = { streaming: {} };
  const evs = [{ type: 'run', run_id: 'r', status: 'running' }, { type: 'message', run_id: 'r' }, { type: 'run', run_id: 'r', status: 'done' }];
  evs.concat(evs).forEach(e => ctx.agReduce(st, e));
  assert.deepStrictEqual(JSON.parse(JSON.stringify(st.streaming)), {});
});

check('chat items: chips carry their result and their approval card', () => {
  const detail = {
    messages: [
      { role: 'user', content: 'restart web', run_id: 'r' },
      { role: 'assistant', content: null, run_id: 'r', tool_calls: [{ id: 'c1', name: 'container__restart', arguments: { host: 'lab1', name: 'web' } }] },
      { role: 'tool', tool_call_id: 'c1', content: '{"tool":"x"}' },
      { role: 'assistant', content: 'Done.', run_id: 'r', tool_calls: null }],
    approvals: [{ id: 'a1', call_id: 'c1', status: 'done' }],
  };
  const items = ctx.agChatItems(detail);
  assert.strictEqual(items.length, 3);
  assert.strictEqual(items[1].chips[0].result.content, '{"tool":"x"}');
  assert.strictEqual(items[1].chips[0].card.id, 'a1');
  assert.strictEqual(ctx.agChipLabel(items[1].chips[0]).text, '✓ container.restart · lab1');
  assert.strictEqual(ctx.agChipLabel({ name: 'x', args: {}, result: { content: 'not allowed: y' } }).bad, true);
});

check('activity lines name what, where and why', () => {
  const l = ctx.agStepLine({ kind: 'policy', tool: 'container.stop', host: 'nas', target: 'smb', decision: 'deny', decision_reason: 'host_passive', excerpt: 'nas is Passive' });
  assert.ok(l.text.includes('container.stop · nas → smb'));
  assert.ok(l.text.includes('host_passive'));
  assert.strictEqual(ctx.agStepLine({ kind: 'model', tokens_in: 10, tokens_out: 2 }).tag, 'model');
});

check('every page global agent.js uses exists in dashboard.html', () => {
  for (const g of ['const esc =', 'const I18N =', 'let TAB =', 'const TABS =', 'let CURRENT_HOST =', 'function showTab(',
    'function setHost(', 'function buildNav(', 'function toast(', 'let FLEET =', 'const INITIAL_TAB =', 'function setSettingsPane(']) {
    assert.ok(DASH.includes(g), 'dashboard.html lost: ' + g);
  }
  assert.ok(DASH.includes("group(I18N.t('nav.group.agents','Agent'),'agents')"), 'agents nav group');
  assert.ok(DASH.includes("new CustomEvent('hl:tab'"), 'hl:tab hook');
  assert.ok(DASH.includes("es.addEventListener('agent'"), 'agent frames on the stream');
});

check('agent.js and agent.css load with a version cache-bust', () => {
  assert.ok(DASH.includes("'/static/agent.js?v='+v"));
  assert.ok(DASH.includes("'/static/agent.css?v='+v"));
});

check('agent.js builds no HTML from template literals', () => {
  // Markup is built by concatenation with agEsc() — no ${...} interpolation to forget it in.
  assert.ok(!SRC.includes('${'));
});

check('alpha #7: a typed #tab?host= link switches host and tab', () => {
  const c2 = { console, String, RegExp, location: { hash: '#containers?host=lab1' }, calls: [] };
  vm.createContext(c2);
  vm.runInContext([takeConst('AG_TABS'), take('agDeepLink'), take('agOnHash'),
    'function agOpenFromHash(h){ calls.push(["agent", h]); }', 'function agFollowLink(h){ calls.push(["follow", h]); }'].join('\n'), c2);
  c2.agOnHash();
  assert.deepStrictEqual(plain(c2.calls), [['follow', '#containers?host=lab1']]);
});

check('alpha #2/#6: phone chat bar, recent chats in the widget, an inline error line', () => {
  assert.ok(SRC.includes('id="ag-list"') && SRC.includes('id="ag-new2"'));
  assert.ok(SRC.includes('-err" role="alert"'));
  assert.ok(SRC.includes('id="agw-hist"'));
});

check('alpha #5: Stop shows a stopping state', () => {
  assert.ok(SRC.includes("agT('agent.stopping', 'Stopping…')"));
});

check('alpha #1/#36: automations offer the catalogue actions; channels need to be set up', () => {
  const c3 = { console, String, Array, Object, AG: { policy: { actions: [{ id: 'a', probe: false }, { id: 'p', probe: true }] },
    settings: { channels_configured: ['ntfy'] } } };
  vm.createContext(c3);
  vm.runInContext([take('agActionChoices'), take('agChannelReady')].join('\n'), c3);
  assert.deepStrictEqual(plain(c3.agActionChoices().map(a => a.id)), ['a']);
  assert.strictEqual(c3.agChannelReady('inapp'), true);
  assert.strictEqual(c3.agChannelReady('ntfy'), true);
  assert.strictEqual(c3.agChannelReady('discord'), false);
});

check('alpha #24/#31/#36: outcomes and provider errors read as sentences, not raw text', () => {
  vm.runInContext([take('agApprovalOutcome'), take('agTestText'), take('agProbeText'), take('agStatusLabel'),
    take('agTriggerLabel'), take('agWhenFull')].join('\n'), ctx);
  assert.strictEqual(ctx.agApprovalOutcome({ status: 'refused', result: 'lab1 is Passive' }), 'Refused at run time: lab1 is Passive');
  assert.strictEqual(ctx.agTestText({ ok: false, error_kind: 'protocol', detail: '<html>' }).includes('<'), false);
  assert.strictEqual(ctx.agTestText({ ok: true, models: ['a', 'b'] }), '2 models');
  assert.ok(ctx.agProbeText({ tools: 'no' }).includes('can’t drive the agent'));
  assert.strictEqual(ctx.agStatusLabel('waiting_approval'), 'waiting for you');
  assert.strictEqual(ctx.agTriggerLabel('manual'), 'run now');
  assert.ok(/\d{4}/.test(ctx.agWhenFull(1735689600)));     // the date, with the year, always (#32)
});

console.log('\n' + n + ' agent UI checks passed');
