#!/usr/bin/env node
/*
 * esc() is the one HTML-escaper the dashboard uses for every value it puts into
 * markup — and about a third of those values land inside an attribute
 * (title="${esc(name)}", data-x="${esc(id)}"). It used to escape only & < >, so a
 * container or model name with a double quote in it could close the attribute and
 * add its own (an onmouseover=…). The agent feature (#354) puts model-written
 * text and attacker-influenced names on screen, so quotes are escaped now.
 *
 * Lifted out of the shipped static/dashboard.html and run in a vm, like the other
 * tests here, so it can only pass by the real code behaving.
 *
 * Run: node tests/js/test_esc.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const DASH = path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

const line = SRC.split('\n').find(l => l.startsWith('const esc = '));
if (!line) throw new Error('top-level `const esc = ...` not found in dashboard.html');
const ctx = { String };
vm.createContext(ctx);
vm.runInContext(line.replace('const esc', 'var esc'), ctx);
const esc = ctx.esc;

let n = 0;
function check(name, fn) { fn(); n++; console.log('ok', name); }

check('escapes the five HTML specials', () => {
  assert.strictEqual(esc(`&<>"'`), '&amp;&lt;&gt;&quot;&#39;');
});

check('null and undefined become empty, numbers become text', () => {
  assert.strictEqual(esc(null), '');
  assert.strictEqual(esc(undefined), '');
  assert.strictEqual(esc(42), '42');
});

check('a value with a double quote cannot leave a double-quoted attribute', () => {
  const hostile = 'x" onmouseover="alert(1)';
  const html = `<span title="${esc(hostile)}">a</span>`;
  // The only double quotes left are the two the template wrote.
  assert.strictEqual((html.match(/"/g) || []).length, 2);
  assert.ok(!/onmouseover="/.test(html));
});

check('a value with a single quote cannot leave a single-quoted attribute', () => {
  const html = `<span title='${esc("x' onclick='alert(1)")}'>a</span>`;
  assert.strictEqual((html.match(/'/g) || []).length, 2);
});

check('unicode, emoji and RTL text pass through unchanged', () => {
  const s = 'контейнер 🐳 שלום';
  assert.strictEqual(esc(s), s);
});

check('mdMini escapes quotes inside its own helper too', () => {
  const m = SRC.match(/function mdMini\(md\)\{\n\s*const esc = s => ([^\n]+);/);
  assert.ok(m, 'mdMini esc helper not found');
  assert.ok(m[1].includes('&quot;'), 'mdMini esc must escape double quotes');
});

console.log(`\n${n} esc checks passed`);
