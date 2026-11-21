#!/usr/bin/env node
/*
 * static/csrf.js wraps window.fetch so the dashboard's ~35 existing POST/DELETE
 * call sites work behind the optional login without being edited (#354):
 *   - no password set  → requests go out exactly as written (no header added);
 *   - password set     → same-origin mutations carry X-CSRF-Token, GETs and
 *                        cross-origin requests don't;
 *   - a 401 auth_required → the page goes to /login?next=…
 *
 * The real file runs in a vm with a fake fetch/location.
 * Run: node tests/js/test_fetch_wrapper.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'csrf.js'), 'utf8');

function load(statusBody, responder) {
  const calls = [];
  const location = { href: 'http://hub:9800/#gpu', origin: 'http://hub:9800', pathname: '/', search: '', hash: '#gpu' };
  const fakeFetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    calls.push({ url, init: init || {} });
    if (url === '/api/auth/status') return resp(200, statusBody);
    return responder ? responder(url, init) : resp(200, { ok: true });
  };
  function resp(status, body) {
    return { status, ok: status < 400, json: async () => body, clone() { return resp(status, body); } };
  }
  const window = { fetch: fakeFetch };
  const ctx = { window, location, URL, Headers: class {
    constructor(h) { this.m = {}; if (h) for (const k of Object.keys(h)) this.m[k.toLowerCase()] = h[k]; }
    set(k, v) { this.m[k.toLowerCase()] = v; } get(k) { return this.m[k.toLowerCase()]; }
  }, console, JSON, String, Object, Promise, encodeURIComponent, setTimeout };
  vm.createContext(ctx);
  vm.runInContext(SRC + '\nthis.HLM_AUTH = HLM_AUTH;', ctx);
  return { ctx, calls, resp };
}

let n = 0;
async function check(name, fn) { await fn(); n++; console.log('ok', name); }

(async () => {
  await check('no password: a POST goes out unchanged', async () => {
    const { ctx, calls } = load({ password_set: false });
    await ctx.window.fetch('/api/settings', { method: 'POST', body: '{}' });
    const post = calls.find(c => c.url === '/api/settings');
    assert.strictEqual(post.init.headers, undefined);
  });

  await check('password set: same-origin POST carries the CSRF token', async () => {
    const { ctx, calls } = load({ password_set: true, authenticated: true, csrf: 'tok-1' });
    await ctx.window.fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    const post = calls.find(c => c.url === '/api/settings');
    assert.strictEqual(post.init.headers.get('X-CSRF-Token'), 'tok-1');
    assert.strictEqual(post.init.headers.get('Content-Type'), 'application/json');
  });

  await check('GETs never carry it', async () => {
    const { ctx, calls } = load({ password_set: true, csrf: 'tok-1' });
    await ctx.window.fetch('/api/fleet');
    assert.strictEqual(calls.find(c => c.url === '/api/fleet').init.headers, undefined);
  });

  await check('cross-origin requests never carry it', async () => {
    const { ctx, calls } = load({ password_set: true, csrf: 'tok-1' });
    await ctx.window.fetch('https://evil.example/x', { method: 'POST' });
    assert.strictEqual(calls.find(c => c.url === 'https://evil.example/x').init.headers, undefined);
  });

  await check('DELETE and FormData uploads are covered too', async () => {
    const { ctx, calls } = load({ password_set: true, csrf: 'tok-2' });
    await ctx.window.fetch('/api/hosts/x', { method: 'DELETE' });
    await ctx.window.fetch('/api/backup/restore', { method: 'POST', body: {} });
    assert.strictEqual(calls.find(c => c.url === '/api/hosts/x').init.headers.get('X-CSRF-Token'), 'tok-2');
    assert.strictEqual(calls.find(c => c.url === '/api/backup/restore').init.headers.get('X-CSRF-Token'), 'tok-2');
  });

  await check('401 auth_required sends the page to the login', async () => {
    const { ctx } = load({ password_set: true }, (url) => ({ status: 401, ok: false,
      json: async () => ({ error: 'auth_required' }), clone() { return this; } }));
    await ctx.window.fetch('/api/fleet');
    assert.strictEqual(ctx.location.href, '/login?next=' + encodeURIComponent('/#gpu'));
  });

  await check('sameOrigin helper', async () => {
    const { ctx } = load({ password_set: false });
    assert.strictEqual(ctx.HLM_AUTH.sameOrigin('/api/x'), true);
    assert.strictEqual(ctx.HLM_AUTH.sameOrigin('http://hub:9800/api/x'), true);
    assert.strictEqual(ctx.HLM_AUTH.sameOrigin('http://other:9800/api/x'), false);
  });

  console.log(`\n${n} fetch-wrapper checks passed`);
})().catch(e => { console.error(e); process.exit(1); });
