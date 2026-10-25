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

check('pass 2 N1: under each run, the actions it really took, from its records', () => {
  vm.runInContext(take('agRunFacts'), ctx);
  assert.strictEqual(ctx.agRunFacts({ status: 'running' }), '');                 // no data, no claim either way
  assert.ok(ctx.agRunFacts({ actions: [] }).includes('No actions were proposed in this run.'));
  const html = ctx.agRunFacts({ actions: [{ outcome: 'pending', action: 'container.restart', title: 'Restart a container', host: 'lab1', target: '<b>w</b>' }] });
  assert.ok(html.includes('Waiting for your approval'));
  assert.ok(html.includes('href="#agent/approvals"'));
  assert.ok(!html.includes('<b>w</b>'));                                       // names are escaped
  assert.ok(SRC.includes('agRunFacts(r) +'), 'run detail shows the list');
  assert.ok(/agFactsChip\(r\)/.test(take('agPaintMessages')), 'chat and widget show what the run did, as a chip');
});

check('pass 2 N3/N4/N5: filters load with Activity; no Use for models without tool calls; Since has a label', () => {
  assert.ok(/activity'\) \{ await Promise\.all\(\[agLoadRuns\(\), agLoadPolicy\(\), agLoadTasks\(\)\]\)/.test(SRC));
  assert.ok(/<label class="ag-flabel" for="agf-since">/.test(SRC));
  vm.runInContext(take('agUseBtn'), ctx);
  const no = ctx.agUseBtn('data-use', 'p|m', { tools: 'no' });
  assert.ok(!no.includes('<button') && /title="[^"]*without calling tools/.test(no));
  assert.ok(ctx.agUseBtn('data-use', 'p|m', { tools: 'yes' }).includes('<button'));
  assert.ok(ctx.agUseBtn('data-disc-use', '0|m', null).includes('data-disc-use="0|m"'));
});

check('UAT-1: markdown tables, headings, ordered lists, italics — escaped first', () => {
  const out = ctx.agMarkdown('## Disks\n| Host | Use |\n|---|---:|\n| ardi | **72%** |\n| <b>x</b> | 1 |\n\n1. one\n2. two\n*note*');
  assert.ok(out.includes('<h4>Disks</h4>'));
  assert.ok(out.includes('<div class="ag-tbl"><table><thead><tr><th>Host</th><th>Use</th></tr></thead>'));
  assert.ok(out.includes('<td>ardi</td><td><b>72%</b></td>'));
  assert.ok(out.includes('&lt;b&gt;x&lt;/b&gt;') && !out.includes('<b>x</b>'));
  assert.ok(out.includes('<ol><li>one</li><li>two</li></ol>'));
  assert.ok(out.includes('<i>note</i>'));
  assert.ok(!ctx.agMarkdown('a | b').includes('<table'), 'a lone pipe is not a table');
});

check('UAT-1: conversations grouped by Today / Yesterday / Earlier', () => {
  vm.runInContext([take('agDayBucket'), take('agGroupConvs')].join('\n'), ctx);
  const now = new Date(2026, 9, 25, 10, 0).getTime();
  const at = (d, h) => new Date(2026, 9, d, h, 0).getTime() / 1000;
  const g = plain(ctx.agGroupConvs([{ id: 'a', updated_at: at(25, 9) }, { id: 'b', updated_at: at(24, 23) }, { id: 'c', updated_at: at(20, 9) }, { id: 'd', updated_at: at(25, 1) }], now));
  assert.deepStrictEqual(g.map(x => [x[0], x[1].map(c => c.id)]), [['today', ['a', 'd']], ['yesterday', ['b']], ['earlier', ['c']]]);
});

check('UAT-1: one line for the work a run did, one chip for its actions', () => {
  vm.runInContext([take('agWorkLine'), take('agFactsChip')].join('\n'), ctx);
  assert.strictEqual(ctx.agWorkLine({ work: { machines: 4, lookups: 1, seconds: 6 } }, 1), 'Checked 4 machines · 1 look-up · 6 s');
  assert.strictEqual(ctx.agWorkLine(null, 3), 'Checked 3 look-ups');
  assert.ok(ctx.agFactsChip({ actions: [] }).includes('No actions proposed'));
  const w = ctx.agFactsChip({ actions: [{ outcome: 'pending', title: 'Restart a container', host: 'lab1', target: 'web' }] });
  assert.ok(w.includes('href="#agent/approvals"') && w.includes('1 action waiting for your approval'));
  assert.strictEqual(ctx.agFactsChip({ status: 'running' }), '');
});

check('UAT-1: chat turns per run, approval card with one primary Approve', () => {
  vm.runInContext([take('agRunGroups'), take('agApprovalCard'), take('agApprovalOutcome')].join('\n'), ctx);
  const g = plain(ctx.agRunGroups({ messages: [
    { role: 'user', content: 'q1', run_id: 'r1' }, { role: 'assistant', content: 'looking', run_id: 'r1', tool_calls: [{ id: 'c1', name: 'list_hosts' }] },
    { role: 'tool', tool_call_id: 'c1', content: 'x' }, { role: 'assistant', content: 'a1', run_id: 'r1' },
    { role: 'user', content: 'q2', run_id: 'r2' }, { role: 'assistant', content: 'a2', run_id: 'r2' }], approvals: [] }));
  assert.deepStrictEqual(g.map(x => [x.run_id, x.user, x.chips.length, x.texts]), [['r1', 'q1', 1, ['looking', 'a1']], ['r2', 'q2', 0, ['a2']]]);
  const card = ctx.agApprovalCard({ id: 'a1', action: 'container.restart', title: 'Restart a container', host: 'lab1', params: { name: 'web' }, risk: 'low', status: 'pending' }, false);
  assert.strictEqual((card.match(/class="ag-btn pri"/g) || []).length, 1);
  assert.ok(/class="ag-btn pri" data-ap="a1">Approve/.test(card));
  assert.ok(card.includes('role="group"'));
});

check('UAT-1: Overview card state, password meter, model pre-selection', () => {
  vm.runInContext([take('agHomeState'), take('agPwScore'), take('agPwLabel'), take('agPickModel')].join('\n'), ctx);
  assert.strictEqual(ctx.agHomeState(null, ''), 'none');
  assert.strictEqual(ctx.agHomeState({ allowed: true, done: false }, ''), 'invite');
  assert.strictEqual(ctx.agHomeState({ allowed: true, done: false, dismissed: true }, ''), 'strip');
  assert.strictEqual(ctx.agHomeState({ allowed: true, done: false }, 'folded'), 'strip');
  assert.strictEqual(ctx.agHomeState({ allowed: true, done: false }, 'hidden'), 'none');
  assert.strictEqual(ctx.agHomeState({ allowed: true, done: true, dismissed: true }, 'hidden'), 'ask');
  assert.strictEqual(ctx.agPwScore(''), 0);
  assert.strictEqual(ctx.agPwScore('short'), 1);
  assert.strictEqual(ctx.agPwScore('longenough'), 2);
  assert.strictEqual(ctx.agPwScore('Long-Enough-2026!'), 4);
  const ms = [{ provider_id: 'p', model: 'emb', tools: 'no' }, { provider_id: 'p', model: 'small', tools: 'yes' }, { provider_id: 'p', model: 'big', tools: 'yes', recommended: true }];
  assert.strictEqual(ctx.agPickModel(ms, ''), 'p|big');
  assert.strictEqual(ctx.agPickModel(ms, 'p|small'), 'p|small');
  assert.strictEqual(ctx.agPickModel(ms, 'p|emb'), 'p|big', 'a model that cannot call tools is never kept');
  assert.strictEqual(ctx.agPickModel([], ''), '');
});

check('UAT-1: suggestions are a declared list; the Agent sits in the AI group; the card hides in kiosk mode', () => {
  assert.ok(/const AG_SUGGEST = \[\['agent\.sug\./.test(SRC));
  assert.ok(SRC.includes("zone: 'ai'"));
  assert.ok(fs.readFileSync(path.join(ROOT, 'static', 'agent.css'), 'utf8').includes('body.display .ag-home{display:none!important}'));
  assert.ok(SRC.includes("const AG_VIEWS = ['chat', 'auto', 'approvals', 'activity', 'settings'];"));
});

console.log('\n' + n + ' agent UI checks passed');
