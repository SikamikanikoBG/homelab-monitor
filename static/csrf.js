/*
 * The dashboard's login client (#327, #354). Loaded first, before any other script.
 *
 * 1. A global fetch wrapper. Every existing call site stays as it is: for a
 *    same-origin request that changes something (anything but GET/HEAD) it adds
 *    the session's X-CSRF-Token — only once a password is set; with no login
 *    nothing is added. A 401 "auth_required" sends the page to /login.
 * 2. The Settings → Security card: set, change or remove the admin password,
 *    see and end sessions.
 *
 * Strings go through I18N.t when the page has it (it is defined by the main
 * script, which runs after this file).
 */
'use strict';

const HLM_AUTH = (function () {
  const rawFetch = window.fetch.bind(window);
  let pending = null;
  const st = { state: null };

  function t(k, f) { return (typeof I18N !== 'undefined' && I18N.t) ? I18N.t(k, f) : f; }
  function escH(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function sameOrigin(input) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      return new URL(url, location.href).origin === location.origin;
    } catch (e) { return false; }
  }

  function methodOf(input, init) {
    return String((init && init.method) || (input && typeof input === 'object' && input.method) || 'GET').toUpperCase();
  }

  async function status(force) {
    if (!pending || force) {
      pending = rawFetch('/api/auth/status', { credentials: 'same-origin' })
        .then(r => r.json()).catch(() => ({ password_set: false }));
    }
    st.state = await pending;
    return st.state;
  }

  function forget() { pending = null; }

  function loginUrl() {
    const next = location.pathname + location.search + location.hash;
    return '/login?next=' + encodeURIComponent(next);
  }

  async function wrapped(input, init) {
    let opts = init || {};
    const m = methodOf(input, opts);
    const same = sameOrigin(input);
    if (m !== 'GET' && m !== 'HEAD' && same) {
      const s = await status();
      if (s.password_set && s.csrf) {
        const h = new Headers(opts.headers || {});
        h.set('X-CSRF-Token', s.csrf);
        opts = Object.assign({}, opts, { headers: h });
      }
    }
    const r = await rawFetch(input, opts);
    if (same && (r.status === 401 || r.status === 403)) {
      let body = null;
      try { body = await r.clone().json(); } catch (e) { body = null; }
      const err = body && body.error;
      if (err === 'auth_required' && !String(location.pathname).startsWith('/login')) {
        location.href = loginUrl();
      } else if (err === 'csrf') {
        forget();                    // the session changed under us; next call refetches the token
      }
    }
    return r;
  }

  async function post(url, method, body) {
    const r = await wrapped(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    let j = {};
    try { j = await r.json(); } catch (e) { j = {}; }
    return { ok: r.ok, status: r.status, body: j };
  }

  function note(el, level, text) {
    if (!el) return;
    el.className = 'ins ' + (level === 'ok' ? 'ok' : level === 'err' ? 'critical' : 'info');
    el.style.display = text ? '' : 'none';
    el.textContent = text || '';
  }

  // ── Settings → Security ────────────────────────────────────────────────────
  async function renderSecurity(root) {
    if (!root) return;
    const s = await status(true);
    if (!s.password_set) {
      root.innerHTML = `
        <p class="cap" style="margin:2px 0 12px">${escH(t('sec_auth.intro_off', 'This dashboard has no login, which is how it has always worked: anyone who can reach it on your network can see it — and, while container controls are on, restart things. Setting a password turns on a login for the dashboard and its API. The AI agent needs one before it can be switched on.'))}</p>
        <div id="secauth_status" class="ins info" style="display:none"></div>
        <div style="display:grid;gap:10px;max-width:420px">
          <div><div class="flabel">${escH(t('sec_auth.new_pw', 'New password (at least 10 characters)'))}</div>
            <input type="password" id="secauth_pw1" class="field" autocomplete="new-password"></div>
          <div><div class="flabel">${escH(t('sec_auth.repeat_pw', 'Repeat it'))}</div>
            <input type="password" id="secauth_pw2" class="field" autocomplete="new-password"></div>
          <p class="cap" style="margin:0">${escH(t('sec_auth.forgot_hint', 'Forgot it later? Run: docker exec homelab-monitor python -m backend.auth.cli reset-password'))}</p>
          <div><button class="rb on" id="secauth_set" type="button">${escH(t('sec_auth.protect', 'Protect this dashboard'))}</button></div>
        </div>`;
      root.querySelector('#secauth_set').onclick = async () => {
        const a = root.querySelector('#secauth_pw1').value, b = root.querySelector('#secauth_pw2').value;
        const out = root.querySelector('#secauth_status');
        if (a !== b) { note(out, 'err', t('sec_auth.mismatch', 'The two passwords differ.')); return; }
        const r = await post('/api/auth/setup', 'POST', { password: a });
        if (!r.ok) { note(out, 'err', r.body.message || r.body.error || ('HTTP ' + r.status)); return; }
        forget();
        note(out, 'ok', t('sec_auth.set_ok', 'Password set — you are logged in on this browser.'));
        setTimeout(() => renderSecurity(root), 600);
      };
      return;
    }
    root.innerHTML = `
      <p class="cap" style="margin:2px 0 12px">${escH(t('sec_auth.intro_on', 'The dashboard has a login. Browsers use a session cookie; scripts, Grafana and MCP clients use an API key with a scope (Integrations → API keys).'))}</p>
      <div id="secauth_status" class="ins info" style="display:none"></div>
      <div class="card-sub" style="font-weight:600">${escH(t('sec_auth.change', 'Change password'))}</div>
      <div style="display:grid;gap:8px;max-width:420px;margin:6px 0 14px">
        <input type="password" id="secauth_cur" class="field" placeholder="${escH(t('sec_auth.current', 'Current password'))}" autocomplete="current-password">
        <input type="password" id="secauth_new" class="field" placeholder="${escH(t('sec_auth.new_pw', 'New password (at least 10 characters)'))}" autocomplete="new-password">
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="rb on" id="secauth_change" type="button">${escH(t('sec_auth.change_btn', 'Change password'))}</button>
          <button class="rb" id="secauth_logout" type="button">${escH(t('sec_auth.logout', 'Log out'))}</button>
          <button class="rb" id="secauth_remove" type="button">${escH(t('sec_auth.remove', 'Remove the login'))}</button>
        </div>
      </div>
      <div class="card-sub" style="font-weight:600">${escH(t('sec_auth.sessions', 'Signed-in browsers'))}</div>
      <table style="margin-top:6px"><thead><tr><th>${escH(t('sec_auth.col_ip', 'Address'))}</th><th>${escH(t('sec_auth.col_seen', 'Last seen'))}</th><th>${escH(t('sec_auth.col_ua', 'Browser'))}</th><th></th></tr></thead>
        <tbody id="secauth_sessions"></tbody></table>`;
    const out = root.querySelector('#secauth_status');
    root.querySelector('#secauth_change').onclick = async () => {
      const r = await post('/api/auth/password', 'POST', { current: root.querySelector('#secauth_cur').value, new: root.querySelector('#secauth_new').value });
      note(out, r.ok ? 'ok' : 'err', r.ok ? t('sec_auth.changed', 'Password changed. Other browsers were signed out.') : (r.body.message || r.body.error));
      if (r.ok) loadSessions(root);
    };
    root.querySelector('#secauth_logout').onclick = async () => {
      await post('/api/auth/logout', 'POST', {});
      forget();
      location.href = '/login';
    };
    root.querySelector('#secauth_remove').onclick = async () => {
      if (!confirm(t('sec_auth.remove_confirm', 'Remove the login? Anyone on your network will be able to use the dashboard again.'))) return;
      const r = await post('/api/auth/password', 'DELETE', { current: root.querySelector('#secauth_cur').value });
      if (!r.ok) { note(out, 'err', r.body.message || r.body.error); return; }
      forget();
      renderSecurity(root);
    };
    loadSessions(root);
  }

  async function loadSessions(root) {
    const tb = root.querySelector('#secauth_sessions');
    if (!tb) return;
    let j = {};
    try { j = await (await wrapped('/api/auth/sessions')).json(); } catch (e) { j = {}; }
    const ago = ts => ts ? new Date(ts * 1000).toLocaleString() : '—';
    tb.innerHTML = (j.sessions || []).map(x => `<tr>
      <td><code>${escH(x.ip || '—')}</code>${x.current ? ' <span class="badge ok">' + escH(t('sec_auth.this_one', 'this browser')) + '</span>' : ''}</td>
      <td>${escH(ago(x.last_seen_at))}</td><td class="muted" style="font-size:12px">${escH((x.ua || '').slice(0, 60))}</td>
      <td>${x.current ? '' : `<button class="rb" data-sid="${escH(x.id)}" style="padding:3px 9px">${escH(t('sec_auth.end', 'Sign out'))}</button>`}</td></tr>`).join('');
    tb.querySelectorAll('button[data-sid]').forEach(b => b.onclick = async () => {
      await post('/api/auth/sessions/' + encodeURIComponent(b.dataset.sid), 'DELETE', {});
      loadSessions(root);
    });
  }

  window.fetch = wrapped;
  return { status, forget, sameOrigin, renderSecurity, loginUrl, _wrapped: wrapped, _raw: rawFetch };
})();
