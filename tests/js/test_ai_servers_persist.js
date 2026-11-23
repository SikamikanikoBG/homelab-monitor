#!/usr/bin/env node
/*
 * Settings → AI servers: a Remove clicked while the previous save is still in
 * flight must be saved too (#371). Dropping it left the entry on the hub, which
 * kept probing a server the list no longer showed.
 *
 * Same technique as test_nav_customize.js.
 * Run: node tests/js/test_ai_servers_persist.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const DASH = process.env.DASHBOARD_HTML
  || path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

function take(marker) {
  const start = SRC.indexOf('\n' + marker);
  if (start < 0) throw new Error(`${marker} was not found in ${DASH}.`);
  const end = SRC.indexOf('\n}', start);
  return SRC.slice(start + 1, end + 2);
}
const flags = SRC.match(/^let _aiPersistBusy.*$/m);
if (!flags) throw new Error('_aiPersistBusy was not found');

const saved = [];
const pending = [];
const ctx = {
  JSON,
  AI_SERVERS_DRAFT: [{ name: 'a' }, { name: 'b' }, { name: 'c' }],
  AI_SERVERS_LOADED: true,
  showAiStatus() {},
  fetch: (url, opt) => new Promise(resolve => {
    const names = JSON.parse(JSON.parse(opt.body).custom_ai_servers).map(s => s.name);
    pending.push(() => { saved.push(names); resolve({ json: async () => ({ ok: true }) }); });
  }),
};
vm.createContext(ctx);
vm.runInContext([flags[0], take('function aiServersFromDraft('), take('function collectAiServers('),
  take('async function persistAiServers(')].join('\n'), ctx);

const tick = () => new Promise(r => setImmediate(r));
const remove = () => { ctx.AI_SERVERS_DRAFT.splice(0, 1); return ctx.persistAiServers(); };

(async () => {
  // Three quick Removes; the first save is still in flight for the other two.
  const all = [remove(), remove(), remove()];
  for (let i = 0; i < 5; i++) { while (pending.length) pending.shift()(); await tick(); }
  await Promise.all(all);
  assert.deepStrictEqual(saved[saved.length - 1], [], 'the hub must end up with the empty list');
  assert.strictEqual(saved.length, 2, 'changes made during a save are saved together, once');
  console.log('ok — a Remove during a save is saved too');
})().catch(e => { console.error(e); process.exit(1); });
