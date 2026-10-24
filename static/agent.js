/*
 * HomeLab Monitor — the AI agent's UI (#354): the Agent cockpit tab (Chat,
 * Automations, Approvals, Activity, Agent policy, Models & keys) and the
 * floating Ask widget on every other tab.
 *
 * Loaded by dashboard.html only when /api/agent/status answers (the agent is
 * allowed on this install). Uses the page's own globals: esc, I18N, TAB,
 * TABS, CURRENT_HOST, showTab, setHost, buildNav, toast, FLEET. Live updates
 * arrive as `hl:agent` events (frames of the dashboard's one /api/stream);
 * the database is the truth, so any event just triggers a refetch.
 *
 * Model text is rendered as text: HTML is escaped first, then a tiny
 * markdown subset is applied. The only links made clickable are internal
 * dashboard links like #containers?host=lab1; anything else stays plain text.
 * Everything here is top-level functions so tests/js can lift them.
 */
'use strict';

const AG_TABS = ['overview', 'gpu', 'costs', 'containers', 'services', 'host', 'disks', 'diskio', 'network',
  'security', 'uptime', 'models', 'benchmarks', 'experiments', 'hosts', 'agent'];
const AG_VIEWS = ['chat', 'auto', 'approvals', 'activity', 'policy', 'models'];
const AG_SCHEDULES = [['0 3 * * *', 'Daily · 03:00'], ['0 8 * * *', 'Daily · 08:00'], ['30 7 * * *', 'Daily · 07:30'],
  ['0 9 * * *', 'Daily · 09:00'], ['0 3 * * 0', 'Weekly · Sun 03:00'], ['0 4 * * 0', 'Weekly · Sun 04:00'],
  ['0 5 * * 0', 'Weekly · Sun 05:00'], ['0 2 * * 6', 'Weekly · Sat 02:00'], ['0 8 * * 1', 'Weekly · Mon 08:00'],
  ['0 */6 * * *', 'Every 6 hours']];
const AG = {
  status: null, view: 'chat', convs: [], convId: null, detail: null, providers: [], models: [], discovered: [],
  presets: [], approvals: [], tasks: [], templates: [], policy: null, runs: [], runSel: null, runDetail: null,
  streaming: {}, chipOpen: {}, stopping: {}, widgetOpen: false, widgetConv: null, pollTimer: null, refetchTimer: null,
  lastEvent: 0, filters: {}, policyHost: null, autoView: 'templates', settings: null, listOpen: false,
};

function agT(k, f) { return (typeof I18N !== 'undefined' && I18N.t) ? I18N.t(k, f) : f; }
function agTf(k, f, v) { let s = agT(k, f); for (const n in (v || {})) s = s.split('{' + n + '}').join(v[n]); return s; }
function agEsc(s) { return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function agApi(path, method, body) {
  const opts = { method: method || 'GET', headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); } catch (e) { return { ok: false, status: 0, body: { message: String(e) } }; }
  let j = {};
  try { j = await r.json(); } catch (e) { j = {}; }
  return { ok: r.ok, status: r.status, body: j };
}

function agToast(level, msg) { if (typeof toast === 'function') toast(level, msg); }

// ── pure helpers (tests/js/test_agent_ui.js) ──────────────────────────────────

/** An internal dashboard link from model text, or null. Only #tab or #tab?host=NAME. */
function agDeepLink(href) {
  const m = /^#([a-z]+)(?:\?host=([A-Za-z0-9_.\-]{1,128}))?$/.exec(String(href || ''));
  if (!m || AG_TABS.indexOf(m[1]) < 0) return null;
  return { tab: m[1], host: m[2] || null };
}

/** Model text → safe HTML: escape everything, then a small markdown subset. */
function agMarkdown(text) {
  const src = agEsc(text || '');
  const inline = s => s
    .replace(/`([^`\n]+)`/g, (_, c) => '<code>' + c + '</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, (_, b) => '<b>' + b + '</b>')
    .replace(/\[([^\]\n]{1,80})\]\((#[a-z]+(?:\?host=[A-Za-z0-9_.\-]{1,128})?)\)/g,
      (all, label, href) => agDeepLink(href) ? '<a href="' + href + '" data-ag-link="' + href + '">' + label + '</a>' : all)
    .replace(/(^|[\s(])(#[a-z]+(?:\?host=[A-Za-z0-9_.\-]{1,128})?)(?=$|[\s).,;:!?])/g,
      (all, pre, href) => agDeepLink(href) ? pre + '<a href="' + href + '" data-ag-link="' + href + '">' + href + '</a>' : all);
  const out = [];
  let list = null;
  for (const line of src.split('\n')) {
    const li = /^\s*(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (li) {
      if (!list) { list = []; }
      list.push('<li>' + inline(li[1]) + '</li>');
      continue;
    }
    if (list) { out.push('<ul>' + list.join('') + '</ul>'); list = null; }
    if (line.trim()) out.push('<p>' + inline(line) + '</p>');
  }
  if (list) out.push('<ul>' + list.join('') + '</ul>');
  return out.join('');
}

/** What the page context the widget sends with a question looks like. */
function agContext() {
  const tab = (typeof TAB !== 'undefined') ? TAB : null;
  const host = (typeof CURRENT_HOST !== 'undefined') ? CURRENT_HOST : null;
  return { tab: tab === 'agent' ? null : tab, host: host };
}

/** Whether the floating Ask button shows. */
function agFabVisible(status, tab, displayMode) {
  if (!status || !status.password_set || !status.enabled) return false;
  if (displayMode) return false;
  return tab !== 'agent';
}

/** The Unattended column of the policy table for one action. */
function agUnattended(action, rule, unlocked) {
  if (action.probe) return { text: agT('agent.pol.readonly', 'read-only'), locked: true };
  if (rule === 'deny' || !rule) return { text: agT('agent.rule.deny', 'Deny'), locked: true };
  if (rule === 'ask') return { text: agT('agent.rule.ask', 'Ask'), locked: true };
  if (action.sudo || action.risk === 'high' || action.unattended === 'never') {
    return { text: agT('agent.pol.ask_locked', 'Ask · locked'), locked: true };
  }
  if (action.risk === 'medium') {
    return unlocked ? { text: agT('agent.pol.allow_unlocked', 'Allow · unlocked'), locked: false, unlockable: true }
      : { text: agT('agent.pol.ask_locked', 'Ask · locked'), locked: false, unlockable: true };
  }
  return { text: agT('agent.pol.allow_limited', 'Allow · rate-limited'), locked: true };
}

/** Fold one live event into the streaming state; returns what needs a refetch. */
function agReduce(state, ev) {
  const need = { conv: false, approvals: false, status: false, tasks: false, runs: false };
  if (!ev || !ev.type) return need;
  if (ev.type === 'text') {
    state.streaming[ev.run_id] = (state.streaming[ev.run_id] || '') + (ev.text || '');
    return need;
  }
  if (ev.type === 'message' || ev.type === 'step') {
    delete state.streaming[ev.run_id];
    need.conv = true;
  }
  if (ev.type === 'run') {
    if (ev.status !== 'running') delete state.streaming[ev.run_id];
    need.conv = true; need.status = true; need.runs = true;
  }
  if (ev.type === 'approval') { need.approvals = true; need.status = true; need.conv = true; }
  if (ev.type === 'task') { need.tasks = true; }
  return need;
}

function agRiskPill(risk) {
  const t = { low: agT('agent.risk.low', 'low risk'), medium: agT('agent.risk.medium', 'medium risk'),
    high: agT('agent.risk.high', 'high risk'), none: agT('agent.risk.none', 'read-only') }[risk] || risk;
  return '<span class="ag-pill ' + agEsc(risk) + '">' + agEsc(t) + '</span>';
}

/** Date and time, always (Activity lists runs from many days). */
function agWhenFull(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function agTriggerLabel(t) {
  return { chat: agT('agent.trig.chat', 'chat'), schedule: agT('agent.trig.schedule', 'schedule'), manual: agT('agent.trig.manual', 'run now'),
    dry_run: agT('agent.trig.dry_run', 'dry run') }[t] || t;
}

function agWhen(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function agStatusLabel(st) {
  return { done: agT('agent.st.done', 'done'), running: agT('agent.st.running', 'running'),
    queued: agT('agent.st.queued', 'queued'), waiting_approval: agT('agent.st.waiting', 'waiting for you'),
    failed: agT('agent.st.failed', 'failed'), refused: agT('agent.st.refused', 'refused'),
    cancelled: agT('agent.st.cancelled', 'stopped'), interrupted: agT('agent.st.interrupted', 'interrupted'),
    budget_exhausted: agT('agent.st.budget', 'out of budget') }[st] || st;
}

function agStatusPill(st) {
  const cls = { done: 'ok', running: 'info', queued: 'info', waiting_approval: 'warn', failed: 'crit', refused: 'crit',
    cancelled: '', interrupted: 'warn', budget_exhausted: 'warn' }[st] || '';
  const label = agStatusLabel(st);
  return '<span class="ag-pill ' + cls + '">' + agEsc(label) + '</span>';
}

// ── bootstrap ────────────────────────────────────────────────────────────────

async function agBoot() {
  const r = await agApi('/api/agent/status');
  if (!r.ok) return;
  AG.status = r.body;
  if (!TABS.some(t => t.id === 'agent')) {
    TABS.push({ id: 'agent', label: 'Agent', charts: false, zone: 'agents' });
  }
  if (!document.querySelector('section[data-tab="agent"]')) {
    const anchor = document.querySelector('section[data-tab="overview"]');
    const sec = document.createElement('section');
    sec.dataset.tab = 'agent';
    sec.hidden = true;
    sec.className = 'ag-root';
    sec.innerHTML = '<div id="ag-cockpit"></div>';
    anchor.parentNode.appendChild(sec);
  }
  buildNav();
  agWatchNav();
  agPaintBadge();
  agMountWidget();
  document.addEventListener('hl:tab', e => agOnTab(e.detail));
  document.addEventListener('hl:agent', e => { let ev = null; try { ev = JSON.parse(e.detail); } catch (x) { return; } agOnEvent(ev); });
  window.addEventListener('hashchange', agOnHash);
  setInterval(agHeartbeat, 1500);
  const want = (typeof INITIAL_TAB !== 'undefined') ? INITIAL_TAB : '';
  if (String(want).startsWith('agent')) agOpenFromHash('#' + want);
  else if (agDeepLink('#' + want) && agDeepLink('#' + want).host) agFollowLink('#' + want);
  else if (TAB === 'agent') agRender();
}

function agWatchNav() {
  const nl = document.getElementById('navlist');
  if (!nl || nl.dataset.agWatched) return;
  nl.dataset.agWatched = '1';
  new MutationObserver(() => agPaintBadge()).observe(nl, { childList: true });
}

function agPaintBadge() {
  const n = (AG.status && AG.status.pending_approvals) || 0;
  const row = document.querySelector('.nrow[data-t="agent"]');
  if (row) {
    let b = row.querySelector('.ag-badge');
    if (n && !b) { b = document.createElement('span'); b.className = 'ag-badge'; row.appendChild(b); }
    if (b) { if (n) b.textContent = String(n); else b.remove(); }
  }
  const fb = document.querySelector('.ag-fab .ag-badge');
  if (fb) { fb.textContent = String(n); fb.hidden = !n; }
  const tb = document.querySelector('[data-agv="approvals"] .ag-badge');
  if (tb) { tb.textContent = String(n); tb.hidden = !n; }
}

function agOnHash() {
  if (location.hash.startsWith('#agent')) { agOpenFromHash(location.hash); return; }
  const l = agDeepLink(location.hash);
  if (l && l.host) agFollowLink(location.hash);          // #containers?host=lab1 typed or pasted (#7)
}

function agOpenFromHash(hash) {
  const parts = String(hash).replace(/^#/, '').split('/');
  if (parts[1] === 'run' && parts[2]) { AG.view = 'activity'; AG.runSel = parts[2]; }
  else if (parts[1] && AG_VIEWS.indexOf(parts[1]) >= 0) AG.view = parts[1];
  if (TAB !== 'agent') showTab('agent'); else agRender();
}

function agOnTab(id) {
  agSyncFab();
  if (id === 'agent') agRender();
}

async function agRefreshStatus() {
  const r = await agApi('/api/agent/status');
  if (r.ok) { AG.status = r.body; agPaintBadge(); agSyncFab(); }
}

function agOnEvent(ev) {
  AG.lastEvent = Date.now();
  const need = agReduce(AG, ev);
  if (ev.type === 'text') { agPaintStreaming(ev.run_id); return; }
  if (need.status) agRefreshStatus();
  if (need.conv || need.approvals || need.tasks || need.runs) agScheduleRefetch(need);
}

function agScheduleRefetch(need) {
  AG.pendingNeed = Object.assign(AG.pendingNeed || {}, need);
  if (AG.refetchTimer) return;
  AG.refetchTimer = setTimeout(async () => {
    const n = AG.pendingNeed || {};
    AG.pendingNeed = null; AG.refetchTimer = null;
    if (n.conv) { await agLoadConv(AG.convId); if (AG.widgetConv && AG.widgetConv !== AG.convId) await agLoadWidgetConv(); }
    if (n.approvals || n.conv) await agLoadApprovals();
    if (n.tasks && AG.view === 'auto') await agLoadTasks();
    if (n.runs && AG.view === 'activity') await agLoadRuns();
    agRepaint();
  }, 250);
}

/** Fallback when the stream is quiet: poll the open conversation while a run is active. */
function agHeartbeat() {
  const active = agActiveRun(AG.detail) || agActiveRun(AG.widgetDetail);
  if (!active) return;
  if (Date.now() - AG.lastEvent > 3000) agScheduleRefetch({ conv: true, status: true });
}

function agActiveRun(detail) {
  if (!detail || !detail.runs) return null;
  return detail.runs.find(r => r.status === 'running' || r.status === 'queued') || null;
}

// ── rendering: cockpit shell ─────────────────────────────────────────────────

function agRoot() { return document.getElementById('ag-cockpit'); }

function agRepaint() {
  if (TAB === 'agent') agRender(true);
  if (AG.widgetOpen) agRenderWidget();
}

async function agRender(soft) {
  const root = agRoot();
  if (!root) return;
  const st = AG.status || {};
  if (!st.password_set) {
    root.innerHTML = '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.title', 'AI agent')) + '</div><p>' +
      agEsc(agT('agent.need_pw', 'The agent can look at your lab and, where you allow it, act. Because of that it only runs behind a login. Set a dashboard password first.')) +
      '</p><button class="ag-btn pri" id="ag-go-sec">' + agEsc(agT('agent.go_security', 'Set a password')) + '</button></div>';
    root.querySelector('#ag-go-sec').onclick = () => { showTab('alerts'); if (typeof setSettingsPane === 'function') setSettingsPane('security'); };
    return;
  }
  if (!soft || !root.querySelector('.ag-tabs')) {
    root.innerHTML = '<div class="ag-tabs" role="tablist">' + AG_VIEWS.map(v => {
      const label = { chat: agT('agent.v.chat', 'Chat'), auto: agT('agent.v.auto', 'Automations'),
        approvals: agT('agent.v.approvals', 'Approvals'), activity: agT('agent.v.activity', 'Activity'),
        policy: agT('agent.v.policy', 'Agent policy'), models: agT('agent.v.models', 'Models & keys') }[v];
      return '<button role="tab" data-agv="' + v + '" class="' + (AG.view === v ? 'on' : '') + '">' + agEsc(label) +
        (v === 'approvals' ? '<span class="ag-badge" hidden></span>' : '') + '</button>';
    }).join('') + '</div><div id="ag-view"></div>';
    root.querySelectorAll('[data-agv]').forEach(b => b.onclick = () => { AG.view = b.dataset.agv; agRender(); });
    agPaintBadge();
  }
  const view = root.querySelector('#ag-view');
  if (!st.enabled && AG.view !== 'models' && AG.view !== 'policy') {
    view.innerHTML = agEnableCard();
    agWireEnable(view);
    return;
  }
  if (!soft) await agLoadView();
  ({ chat: agRenderChat, auto: agRenderAuto, approvals: agRenderApprovals, activity: agRenderActivity,
    policy: agRenderPolicy, models: agRenderModels })[AG.view](view);
}

function agEnableCard() {
  return '<div class="ag-card" style="max-width:640px"><div class="ag-h">' + agEsc(agT('agent.enable_h', 'Switch the agent on')) + '</div><p>' +
    agEsc(agT('agent.enable_p', 'When it is on you can chat with your lab and run automations. New hosts start Passive (read only) and every action starts at Deny until you change it under Agent policy; switching the agent off and on again keeps the policy you set.')) +
    '</p>' + (AG.status && !AG.status.has_provider ? '<p class="ag-mut">' + agEsc(agT('agent.enable_model', 'It needs a model too: pick one under Models & keys.')) + '</p>' : '') +
    '<div class="ag-row"><button class="ag-btn pri" id="ag-enable">' + agEsc(agT('agent.enable_btn', 'Enable the agent')) + '</button>' +
    '<button class="ag-btn" id="ag-to-models">' + agEsc(agT('agent.v.models', 'Models & keys')) + '</button></div></div>';
}

function agWireEnable(root) {
  const b = root.querySelector('#ag-enable');
  if (b) b.onclick = async () => {
    const r = await agApi('/api/agent/enable', 'POST', { enabled: true });
    if (!r.ok) { agToast('critical', r.body.message || 'Failed'); return; }
    await agRefreshStatus(); agRender();
  };
  const m = root.querySelector('#ag-to-models');
  if (m) m.onclick = () => { AG.view = 'models'; agRender(); };
}

async function agLoadView() {
  if (AG.view === 'chat') { await Promise.all([agLoadConvs(), agLoadApprovals(), agLoadModelChoices(), agLoadPolicy(), agLoadTasks()]); if (AG.convId) await agLoadConv(AG.convId); }
  if (AG.view === 'auto') await Promise.all([agLoadTasks(), agLoadTemplates(), agLoadModelChoices(), agLoadPolicy()]);  // model choices load settings too
  if (AG.view === 'approvals') await agLoadApprovals();
  if (AG.view === 'activity') { await agLoadRuns(); if (AG.runSel) await agLoadRun(AG.runSel); }
  if (AG.view === 'policy') await agLoadPolicy();
  if (AG.view === 'models') await Promise.all([agLoadModels(true), agLoadSettings()]);
}

// ── loaders ──────────────────────────────────────────────────────────────────

async function agLoadConvs() { const r = await agApi('/api/agent/conversations?limit=100'); if (r.ok) AG.convs = r.body.items || []; }
async function agLoadConv(id) {
  if (!id) { AG.detail = null; return; }
  const r = await agApi('/api/agent/conversations/' + encodeURIComponent(id));
  if (r.ok) AG.detail = r.body; else if (r.status === 404) { AG.convId = null; AG.detail = null; }
}
async function agLoadApprovals() { const r = await agApi('/api/agent/approvals?status=pending'); if (r.ok) AG.approvals = r.body.items || []; }
async function agLoadTasks() { const r = await agApi('/api/agent/tasks'); if (r.ok) AG.tasks = r.body.items || []; }
async function agLoadTemplates() { const r = await agApi('/api/agent/templates'); if (r.ok) AG.templates = r.body.templates || []; }
async function agLoadPolicy() { const r = await agApi('/api/agent/policy'); if (r.ok) AG.policy = r.body; }
async function agLoadSettings() { const r = await agApi('/api/agent/settings'); if (r.ok) AG.settings = r.body; }
async function agLoadRuns(more) {
  const q = Object.entries(AG.filters).filter(([k, v]) => v && k !== 'sinceDate').map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  const cur = more && AG.runsNext ? '&cursor=' + AG.runsNext : '';
  const r = await agApi('/api/agent/runs?limit=50' + (q ? '&' + q : '') + cur);
  if (!r.ok) return;
  AG.runs = more ? AG.runs.concat(r.body.items || []) : (r.body.items || []);
  AG.runsNext = r.body.next || null;
}
async function agLoadRun(id) { const r = await agApi('/api/agent/runs/' + encodeURIComponent(id)); AG.runDetail = r.ok ? r.body : null; }
async function agLoadModelChoices() {
  const [p, m, s] = await Promise.all([agApi('/api/agent/providers'), agApi('/api/agent/models?discover=0'), agApi('/api/agent/settings')]);
  if (p.ok) AG.providers = p.body.providers || [];
  if (m.ok) AG.models = m.body.models || [];
  if (s.ok) AG.settings = s.body;
}
async function agLoadModels(discover) {
  const [p, m, pr] = await Promise.all([agApi('/api/agent/providers'), agApi('/api/agent/models' + (discover ? '' : '?discover=0')), agApi('/api/agent/presets')]);
  if (p.ok) AG.providers = p.body.providers || [];
  if (m.ok) { AG.models = m.body.models || []; AG.discovered = m.body.discovered || []; }
  if (pr.ok) AG.presets = pr.body.presets || [];
}

function agModelOptions(selected) {
  const prov = {};
  AG.providers.forEach(p => { prov[p.id] = p; });
  const opts = AG.models.filter(m => prov[m.provider_id] && m.tools !== 'no').map(m => {
    const v = m.provider_id + '|' + m.model;
    const p = prov[m.provider_id];
    const tag = p.local ? agT('agent.local', 'local') : agT('agent.cloud', 'cloud');
    return '<option value="' + agEsc(v) + '"' + (v === selected ? ' selected' : '') + '>' + agEsc(m.model + ' · ' + p.label + ' (' + tag + ')') + '</option>';
  });
  return opts.join('');
}

function agDefaultModel() {
  const s = AG.settings || {};
  return (s.default_provider_id && s.default_model) ? s.default_provider_id + '|' + s.default_model : '';
}

// ── chat ─────────────────────────────────────────────────────────────────────

function agRenderChat(view) {
  const hasModel = AG.models.some(m => m.tools !== 'no');
  const convs = AG.convs.filter(c => c.origin === 'chat');
  const tasks = AG.convs.filter(c => c.origin === 'task').slice(0, 15);
  const pend = AG.approvals;
  const modes = {};
  (AG.policy ? AG.policy.hosts : []).forEach(h => { modes[h.host] = h.mode; });
  const nextRuns = AG.tasks.filter(t => t.enabled && t.next_fire).sort((a, b) => a.next_fire - b.next_fire).slice(0, 5);
  const kept = agSaveInput('ag');
  view.innerHTML = '<div class="ag-cols' + (AG.listOpen ? ' show-list' : '') + '">' +
    '<div class="ag-convs"><button class="ag-btn sm pri" id="ag-new">' + agEsc(agT('agent.new_chat', '+ New chat')) + '</button>' +
    convs.map(c => '<button class="ag-conv' + (c.id === AG.convId ? ' on' : '') + '" data-cid="' + agEsc(c.id) + '">' + agEsc(c.title || agT('agent.untitled', 'New chat')) + '<small>' + agEsc(agWhen(c.updated_at)) + '</small></button>').join('') +
    (tasks.length ? '<div class="ag-h" style="margin:10px 0 4px">' + agEsc(agT('agent.auto_runs', 'Automation runs')) + '</div>' +
      tasks.map(c => '<button class="ag-conv' + (c.id === AG.convId ? ' on' : '') + '" data-cid="' + agEsc(c.id) + '">' + agEsc(c.title || '') + '<small>' + agEsc(agWhen(c.updated_at)) + ' · ' + agEsc(c.last_status || '') + '</small></button>').join('') : '') +
    '</div>' +
    '<div class="ag-chatcol"><div class="ag-phonebar"><button class="ag-btn sm" id="ag-list">' + agEsc(agT('agent.chats', '☰ Chats')) + '</button>' +
    '<button class="ag-btn sm pri" id="ag-new2">' + agEsc(agT('agent.new_chat', '+ New chat')) + '</button></div>' +
    '<div class="ag-msgs" id="ag-msgs" aria-live="polite"></div>' + agComposer('ag', hasModel) + '</div>' +
    '<div class="ag-rail"><div><div class="ag-h">' + agEsc(agT('agent.pending', 'Pending approvals')) + (pend.length ? ' <span class="ag-badge">' + pend.length + '</span>' : '') + '</div>' +
    (pend.length ? pend.slice(0, 6).map(a => agApprovalCard(a, true)).join('') : '<p class="ag-mut">' + agEsc(agT('agent.none', 'None')) + '</p>') + '</div>' +
    '<div><div class="ag-h">' + agEsc(agT('agent.fleet_access', 'Fleet access')) + '</div><div class="ag-mini">' +
    Object.keys(modes).map(h => '<div class="r"><span class="ag-mono">' + agEsc(h) + '</span><span class="ag-sp"></span>' + agModePill(modes[h]) + '</div>').join('') +
    '</div><a href="#agent/policy" class="ag-mut" style="font-size:12px">' + agEsc(agT('agent.edit_policy', 'Edit policy →')) + '</a></div>' +
    '<div><div class="ag-h">' + agEsc(agT('agent.next_runs', 'Next runs')) + '</div><div class="ag-mini">' +
    (nextRuns.length ? nextRuns.map(t => '<div class="r">' + agEsc(t.name) + '<span class="ag-sp"></span><span class="ag-mono ag-mut">' + agEsc(agWhen(t.next_fire)) + '</span></div>').join('') : '<span class="ag-mut">' + agEsc(agT('agent.none', 'None')) + '</span>') +
    '</div></div></div></div>';
  const fresh = async () => { AG.convId = null; AG.detail = null; AG.listOpen = false; agRender(true); };
  view.querySelector('#ag-new').onclick = fresh;
  view.querySelector('#ag-new2').onclick = fresh;
  view.querySelector('#ag-list').onclick = () => { AG.listOpen = true; agRender(true); };
  view.querySelectorAll('[data-cid]').forEach(b => b.onclick = async () => { AG.convId = b.dataset.cid; AG.listOpen = false; await agLoadConv(AG.convId); agRender(true); });
  agWireApprovalButtons(view);
  agPaintMessages(view.querySelector('#ag-msgs'), AG.detail, hasModel);
  agWireComposer(view, 'ag', () => AG.convId, id => { AG.convId = id; }, () => null);
  agRestoreInput('ag', kept);
}

function agModePill(mode) {
  const label = { off: agT('agent.mode.off', 'Off'), passive: agT('agent.mode.passive', 'Passive'), active: agT('agent.mode.active', 'Active') }[mode] || mode;
  return '<span class="ag-pill ' + (mode === 'active' ? 'ok' : mode === 'passive' ? 'info' : '') + '">' + agEsc(label) + '</span>';
}

function agComposer(prefix, hasModel) {
  return '<div class="ag-composer"><div style="display:grid;gap:6px;flex:1;min-width:0">' +
    '<select class="ag-modelsel" id="' + prefix + '-model" aria-label="' + agEsc(agT('agent.model', 'Model')) + '">' + agModelOptions(agDefaultModel()) + '</select>' +
    '<textarea id="' + prefix + '-ta" rows="2" aria-label="' + agEsc(agT('agent.message', 'Message')) + '" placeholder="' + agEsc(agT('agent.ask_ph', 'Ask about your homelab…')) + '"' + (hasModel ? '' : ' disabled') + '></textarea>' +
    '<div class="ag-err" id="' + prefix + '-err" role="alert"></div></div>' +
    '<div style="display:grid;gap:6px"><button class="ag-btn" id="' + prefix + '-stop" hidden>' + agEsc(agT('agent.stop', 'Stop')) + '</button>' +
    '<button class="ag-btn pri" id="' + prefix + '-send"' + (hasModel ? '' : ' disabled') + '>' + agEsc(agT('agent.send', 'Send')) + '</button></div></div>';
}

/** Keep what the user is typing (and the picked model) across a repaint. */
function agSaveInput(prefix) {
  const ta = document.getElementById(prefix + '-ta'), sel = document.getElementById(prefix + '-model');
  if (!ta) return null;
  return { v: ta.value, focus: document.activeElement === ta, a: ta.selectionStart, b: ta.selectionEnd, model: sel ? sel.value : null };
}

function agRestoreInput(prefix, st) {
  if (!st) return;
  const ta = document.getElementById(prefix + '-ta'), sel = document.getElementById(prefix + '-model');
  if (ta) { ta.value = st.v; if (st.focus) { ta.focus(); try { ta.setSelectionRange(st.a, st.b); } catch (e) { /* not a text input */ } } }
  if (sel && st.model && [...sel.options].some(o => o.value === st.model)) sel.value = st.model;
}

function agWireComposer(root, prefix, getConv, setConv, getContext) {
  const ta = root.querySelector('#' + prefix + '-ta'), send = root.querySelector('#' + prefix + '-send');
  const stop = root.querySelector('#' + prefix + '-stop'), sel = root.querySelector('#' + prefix + '-model');
  const detail = prefix === 'ag' ? AG.detail : AG.widgetDetail;
  const active = agActiveRun(detail) || (detail && detail.runs && detail.runs.find(r => r.status === 'waiting_approval'));
  const err = root.querySelector('#' + prefix + '-err');
  const say = msg => { if (err) err.textContent = msg || ''; else if (msg) agToast('critical', msg); };
  if (stop) {
    const stopping = active && AG.stopping[active.id];
    stop.hidden = !active;
    stop.disabled = !!stopping;
    stop.textContent = stopping ? agT('agent.stopping', 'Stopping…') : agT('agent.stop', 'Stop');
    stop.onclick = async () => {
      if (!active) return;
      AG.stopping[active.id] = true;
      stop.disabled = true; stop.textContent = agT('agent.stopping', 'Stopping…');
      const r = await agApi('/api/agent/runs/' + active.id + '/cancel', 'POST', {});
      if (!r.ok) say(r.body.message || r.body.error || agT('agent.failed', 'Something went wrong.'));
      agScheduleRefetch({ conv: true, status: true });
    };
  }
  if (!send) return;
  const go = async () => {
    const text = (ta.value || '').trim();
    if (!text) return;
    send.disabled = true;
    let cid = getConv();
    if (!cid) {
      const c = await agApi('/api/agent/conversations', 'POST', {});
      if (!c.ok) { send.disabled = false; say(c.body.message || agT('agent.failed', 'Something went wrong.')); return; }
      cid = c.body.id; setConv(cid);
    }
    const [pid, model] = String((sel && sel.value) || '').split('|');
    const r = await agApi('/api/agent/conversations/' + encodeURIComponent(cid) + '/messages', 'POST',
      { text, provider_id: pid || undefined, model: model || undefined, context: getContext() });
    send.disabled = false;
    if (!r.ok) { say(r.body.message || r.body.error || agT('agent.failed', 'Something went wrong.')); return; }
    say('');
    ta.value = '';
    AG.lastEvent = 0;
    if (prefix === 'ag') { await agLoadConvs(); await agLoadConv(cid); agRender(true); } else { await agLoadWidgetConv(); agRenderWidget(); }
  };
  send.onclick = go;
  ta.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); } };
}

/** The items of a conversation, in order: user bubbles, assistant text, tool chips, confirm cards. */
function agChatItems(detail) {
  if (!detail) return [];
  const results = {};
  (detail.messages || []).forEach(m => { if (m.role === 'tool') results[m.tool_call_id] = m; });
  const cards = {};
  (detail.approvals || []).forEach(a => { if (a.call_id) cards[a.call_id] = a; });
  const items = [];
  (detail.messages || []).forEach(m => {
    if (m.role === 'user') items.push({ kind: 'user', text: m.content, run_id: m.run_id });
    if (m.role === 'assistant') {
      const chips = (m.tool_calls || []).map(c => ({ id: c.id, name: c.name, args: c.arguments || {}, result: results[c.id] || null, card: cards[c.id] || null }));
      items.push({ kind: 'assistant', text: m.content, chips, run_id: m.run_id });
    }
  });
  return items;
}

function agChipLabel(c) {
  const host = c.args.host || c.args.name || '';
  const ok = c.result ? !/^(not allowed|not permitted|error|not run)/.test(c.result.content || '') : null;
  return { text: (ok === null ? '… ' : ok ? '✓ ' : '✕ ') + c.name.replace(/__/g, '.') + (host ? ' · ' + host : ''), bad: ok === false };
}

function agPaintMessages(box, detail, hasModel) {
  if (!box) return;
  if (!hasModel) {
    box.innerHTML = '<div class="ag-empty"><p>' + agEsc(agT('agent.no_model', 'No model is set up yet. The agent can use a local model (Ollama, vLLM, llama.cpp, LM Studio) or a cloud one with your own key.')) +
      '</p><button class="ag-btn pri" data-agjump="models">' + agEsc(agT('agent.pick_model', 'Pick a model')) + '</button></div>';
    box.querySelector('[data-agjump]').onclick = () => { AG.view = 'models'; agRender(); };
    return;
  }
  if (!detail) {
    box.innerHTML = '<div class="ag-empty">' + agEsc(agT('agent.empty_chat', 'Ask anything about your lab — "why is my server slow?", "what is using the GPU?", "restart web on lab1".')) + '</div>';
    return;
  }
  const items = agChatItems(detail);
  const runs = {};
  (detail.runs || []).forEach(r => { runs[r.id] = r; });
  let html = '';
  for (const it of items) {
    if (it.kind === 'user') { html += '<div class="ag-msg u">' + agEsc(it.text) + '</div>'; continue; }
    let inner = '';
    if (it.chips.length) {
      inner += '<div class="ag-chips">' + it.chips.map(c => {
        const l = agChipLabel(c);
        return '<button class="ag-chip' + (AG.chipOpen[c.id] ? ' open' : '') + (l.bad ? ' bad' : '') + '" data-chip="' + agEsc(c.id) + '">' + agEsc(l.text) + '</button>';
      }).join('') + '</div>';
      for (const c of it.chips) {
        if (AG.chipOpen[c.id]) inner += '<div class="ag-chipbody">' + agEsc(agPretty(c)) + '</div>';
        if (c.card) inner += agApprovalCard(c.card, false);
      }
    }
    if (it.text) inner += agMarkdown(it.text);
    if (inner) html += '<div class="ag-msg a">' + inner + '</div>';
  }
  for (const r of detail.runs || []) {
    if (AG.streaming[r.id] && (r.status === 'running' || r.status === 'queued')) {
      html += '<div class="ag-msg a" data-stream="' + agEsc(r.id) + '">' + agMarkdown(AG.streaming[r.id]) + '</div>';
    }
  }
  const active = agActiveRun(detail);
  if (active) html += '<div class="ag-typing" data-typing="' + agEsc(active.id) + '">' + agEsc(agT('agent.thinking', 'Working…')) + '</div>';
  const waiting = (detail.runs || []).find(r => r.status === 'waiting_approval');
  if (waiting) html += '<div class="ag-typing">' + agEsc(agT('agent.waiting', 'Waiting for your approval above.')) + '</div>';
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = html;
  box.querySelectorAll('[data-chip]').forEach(b => b.onclick = () => { AG.chipOpen[b.dataset.chip] = !AG.chipOpen[b.dataset.chip]; agRepaint(); });
  box.querySelectorAll('[data-ag-link]').forEach(a => a.onclick = e => { e.preventDefault(); agFollowLink(a.dataset.agLink); });
  agWireApprovalButtons(box);
  if (atBottom || active) box.scrollTop = box.scrollHeight;
}

function agPretty(c) {
  let txt = c.result ? (c.result.content || '') : agT('agent.no_result', '(no result yet)');
  try { const j = JSON.parse(txt); txt = JSON.stringify(j.untrusted_data !== undefined ? j.untrusted_data : j, null, 1); } catch (e) { /* plain text result */ }
  const args = Object.keys(c.args).length ? JSON.stringify(c.args) + '\n\n' : '';
  return (args + txt).slice(0, 6000);
}

function agPaintStreaming(runId) {
  const el = document.querySelector('[data-stream="' + runId + '"]');
  if (el) { el.innerHTML = agMarkdown(AG.streaming[runId] || ''); const box = el.parentNode; box.scrollTop = box.scrollHeight; return; }
  const typing = document.querySelector('[data-typing="' + runId + '"]');
  if (typing) {
    const d = document.createElement('div');
    d.className = 'ag-msg a'; d.dataset.stream = runId; d.innerHTML = agMarkdown(AG.streaming[runId] || '');
    typing.parentNode.insertBefore(d, typing);
  }
}

function agFollowLink(href) {
  const l = agDeepLink(href);
  if (!l) return;
  if (l.host && typeof setHost === 'function' && typeof FLEET !== 'undefined' && FLEET && (FLEET.hosts || []).some(h => h.name === l.host)) setHost(l.host);
  if (AG.widgetOpen && l.tab !== 'agent') agCloseWidget();
  showTab(l.tab);
}

// ── approval cards ───────────────────────────────────────────────────────────

function agApprovalCard(a, compact) {
  const st = a.status;
  const cls = st === 'pending' ? '' : (st === 'done' || st === 'approved' || st === 'executing') ? ' done' : ' gone';
  const target = Object.values(a.params || {}).join(' ');
  let html = '<div class="ag-confirm' + cls + '" data-appr="' + agEsc(a.id) + '">';
  html += '<div class="q">' + agEsc(a.title) + (target ? ' <span class="ag-mono">' + agEsc(target) + '</span>' : '') + ' · <span class="ag-mono">' + agEsc(a.host) + '</span></div>';
  if (!compact) {
    html += '<div class="ag-row">' + agRiskPill(a.risk) + '<span class="ag-pill">' + agEsc(agTf('agent.policy_line', 'policy: {host} {mode} · {rule}', { host: a.host, mode: a.host_mode, rule: a.rule })) + '</span>' +
      (a.seen_count > 1 ? '<span class="ag-pill">' + agEsc(agTf('agent.seen', 'proposed {n}×', { n: a.seen_count })) + '</span>' : '') + '</div>';
    if (a.command_preview) html += '<div class="ag-mono ag-mut">' + agEsc(a.command_preview) + '</div>';
    if (a.rationale) html += '<div class="ag-why"><b>' + agEsc(agT('agent.reasoning', "Agent's reasoning:")) + '</b> ' + agEsc(a.rationale) + '</div>';
  } else {
    html += '<div class="ag-row">' + agRiskPill(a.risk) + '</div>';
  }
  if (st === 'pending') {
    html += '<div class="ag-row"><button class="ag-btn ok sm" data-ap="' + agEsc(a.id) + '">' + agEsc(agT('agent.approve', 'Approve')) + '</button>' +
      '<button class="ag-btn no sm" data-rj="' + agEsc(a.id) + '">' + agEsc(agT('agent.reject', 'Reject')) + '</button>' +
      (compact ? '' : '<span class="ag-mut" style="font-size:12px">' + agEsc(agT('agent.nothing_runs', 'Nothing runs until you approve.')) + '</span>') + '</div>';
  } else {
    const label = { done: agT('agent.ap.done', 'Approved and done'), approved: agT('agent.ap.approved', 'Approved'),
      executing: agT('agent.ap.executing', 'Running…'), rejected: agT('agent.ap.rejected', 'Rejected — nothing was run'),
      expired: agT('agent.ap.expired', 'Expired — nothing was run'), failed: agT('agent.ap.failed', 'Approved, but it failed'),
      refused: agT('agent.ap.refused', 'Refused at run time') }[st] || st;
    html += '<div class="ag-mut" style="font-size:13px"><b>' + agEsc(label) + '</b>' + (a.decided_by ? ' · ' + agEsc(a.decided_by) : '') + (a.result ? ' — ' + agEsc(String(a.result).slice(0, 200)) : '') + '</div>';
  }
  return html + '</div>';
}

/** One line saying what became of an approval (#31). */
function agApprovalOutcome(a) {
  const base = { done: agT('agent.ap.done', 'Approved and done'), approved: agT('agent.ap.approved', 'Approved'),
    executing: agT('agent.ap.executing', 'Running…'), rejected: agT('agent.ap.rejected', 'Rejected — nothing was run'),
    expired: agT('agent.ap.expired', 'Expired — nothing was run'), failed: agT('agent.ap.failed', 'Approved, but it failed'),
    refused: agT('agent.ap.refused', 'Refused at run time') }[a.status] || a.status;
  return a.result && (a.status === 'refused' || a.status === 'failed') ? base + ': ' + String(a.result).slice(0, 160) : base;
}

function agWireApprovalButtons(root) {
  root.querySelectorAll('[data-ap],[data-rj]').forEach(b => b.onclick = async () => {
    const id = b.dataset.ap || b.dataset.rj;
    root.querySelectorAll('[data-ap="' + id + '"],[data-rj="' + id + '"]').forEach(x => { x.disabled = true; });
    const r = await agApi('/api/agent/approvals/' + encodeURIComponent(id) + '/decision', 'POST', { decision: b.dataset.ap ? 'approve' : 'reject' });
    if (!r.ok && r.body.error === 'expired') agToast('info', agT('agent.ap.expired', 'Expired — nothing was run'));
    else if (!r.ok && r.status === 409) agToast('info', agT('agent.already_decided', 'Already decided (another tab or device).'));
    else if (!r.ok) agToast('critical', r.body.message || agT('agent.failed', 'Something went wrong.'));
    else agToast(r.body.status === 'done' || r.body.status === 'rejected' ? 'ok' : r.body.status === 'refused' || r.body.status === 'failed' ? 'warning' : 'info',
      agApprovalOutcome(r.body.approval || { status: r.body.status }));
    await Promise.all([agLoadApprovals(), agRefreshStatus()]);
    agScheduleRefetch({ conv: true, runs: true });
  });
}

function agRenderApprovals(view) {
  const p = AG.approvals;
  view.innerHTML = p.length ? '<div style="display:grid;gap:10px;max-width:780px">' + p.map(a => agApprovalCard(a, false) +
    '<div class="ag-mut" style="font-size:12px;margin:-4px 0 4px">' + agEsc(agTf('agent.expires', 'expires {when}', { when: agWhen(a.expires_at) })) +
    ' · <a href="#agent/run/' + agEsc(a.run_id) + '">' + agEsc(agT('agent.see_run', 'see the run')) + '</a></div>').join('') + '</div>'
    : '<div class="ag-empty">' + agEsc(agT('agent.no_pending', 'No pending approvals.')) + '</div>';
  agWireApprovalButtons(view);
}

// ── automations ──────────────────────────────────────────────────────────────

function agRenderAuto(view) {
  const sub = AG.autoView;
  const head = '<div class="ag-row" style="margin-bottom:10px">' +
    ['templates', 'mine'].map(v => '<button class="ag-btn sm' + (sub === v ? ' pri' : '') + '" data-autov="' + v + '">' +
      agEsc(v === 'templates' ? agT('agent.auto.templates', 'Templates') : agTf('agent.auto.mine', 'My automations ({n})', { n: AG.tasks.length })) + '</button>').join('') +
    '<button class="ag-btn sm" id="ag-custom">' + agEsc(agT('agent.auto.custom', '+ New from a prompt')) + '</button><span class="ag-sp"></span>' +
    '<button class="ag-btn sm" id="ag-pause">' + agEsc(AG.status && AG.status.paused ? agT('agent.resume_all', 'Resume automations') : agT('agent.pause_all', 'Pause all automations')) + '</button></div>' +
    (AG.status && AG.status.paused ? '<div class="ag-banner warn">' + agEsc(agT('agent.paused_note', 'All automations are paused. Nothing scheduled runs until you resume.')) + '</div>' : '');
  let body = '';
  if (sub === 'templates') {
    const active = {};
    AG.tasks.forEach(t => { if (t.template_id) active[t.template_id] = t; });
    body = '<div class="ag-grid">' + AG.templates.map(t => {
      const on = active[t.id];
      return '<div class="ag-tpl' + (on ? ' on' : '') + '"><h4>' + agEsc(agT('agent.tpl.' + t.id + '.name', t.name)) + '</h4><p>' + agEsc(agT('agent.tpl.' + t.id + '.desc', t.desc)) + '</p>' +
        '<div class="meta"><span class="ag-pill">' + agEsc(agCronLabel(t.default_cron)) + '</span>' +
        (t.est_tokens ? '<span class="ag-pill" title="' + agEsc(agT('agent.est_t', 'Estimated tokens per run, on your default model')) + '">' +
          agEsc(agTf('agent.est_short', '~{n}k tokens', { n: Math.round(t.est_tokens / 1000) })) + '</span>' : '') +
        (t.actions.length ? '<span class="ag-pill medium">' + agEsc(agTf('agent.may_propose', 'may act on {n}', { n: t.actions.length })) + '</span>' : '<span class="ag-pill low">' + agEsc(agT('agent.report_only', 'report only')) + '</span>') +
        (t.eligible_hosts.length ? '' : '<span class="ag-pill">' + agEsc(agT('agent.no_hosts', 'no matching host')) + '</span>') + '</div>' +
        '<div class="ag-row">' + (on ? '<span class="ag-pill low">● ' + agEsc(agT('agent.active', 'Active')) + '</span>' : '') +
        '<button class="ag-btn sm pri" data-act="' + agEsc(t.id) + '">' + agEsc(on ? agT('agent.add_another', 'Add another') : agT('agent.activate', 'Activate')) + '</button></div></div>';
    }).join('') + '</div>';
  } else {
    body = AG.tasks.length ? '<div class="ag-card" style="padding:4px 6px"><div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.col.name', 'Automation')) + '</th><th>' + agEsc(agT('agent.col.schedule', 'Schedule')) + '</th><th>' +
      agEsc(agT('agent.col.mode', 'Mode')) + '</th><th>' + agEsc(agT('agent.col.next', 'Next')) + '</th><th>' + agEsc(agT('agent.col.last', 'Last run')) + '</th><th></th></tr></thead><tbody>' +
      AG.tasks.map(t => '<tr><td><b>' + agEsc(t.name) + '</b>' + (t.warning ? ' <span class="ag-pill crit">' + agEsc(t.warning.replace(/_/g, ' ')) + '</span>' : '') +
        (t.update_available ? ' <span class="ag-pill info">' + agEsc(agT('agent.update_avail', 'template updated')) + '</span>' : '') +
        '<div class="ag-mut ag-mono" style="font-size:12px">' + agEsc((t.hosts || []).join(', ') || '—') + '</div></td>' +
        '<td class="ag-mono">' + agEsc(t.cron) + ' <span class="ag-mut">' + agEsc(t.tz) + '</span></td><td>' + agEsc(agModeLabel(t.mode)) + '</td>' +
        '<td class="ag-mono">' + agEsc(t.enabled ? (t.next_fire_local || '—').replace('T', ' ') : agT('agent.off', 'off')) + '</td>' +
        '<td>' + (t.last_run ? '<a href="#agent/run/' + agEsc(t.last_run.id) + '">' + agStatusPill(t.last_run.status) + '</a>' : '—') + '</td>' +
        '<td class="ag-row"><button class="ag-btn sm" data-trun="' + agEsc(t.id) + '">' + agEsc(agT('agent.run_now', 'Run now')) + '</button>' +
        '<button class="ag-btn sm" data-tdry="' + agEsc(t.id) + '">' + agEsc(agT('agent.dry_run', 'Dry run')) + '</button>' +
        '<button class="ag-btn sm" data-tedit="' + agEsc(t.id) + '">' + agEsc(agT('agent.edit', 'Edit')) + '</button>' +
        '<button class="ag-btn sm" data-tdup="' + agEsc(t.id) + '">' + agEsc(agT('agent.duplicate', 'Duplicate')) + '</button>' +
        '<button class="ag-btn sm" data-ttog="' + agEsc(t.id) + '">' + agEsc(t.enabled ? agT('agent.disable', 'Turn off') : agT('agent.enable', 'Turn on')) + '</button>' +
        '<button class="ag-btn sm no" data-tdel="' + agEsc(t.id) + '">' + agEsc(agT('agent.delete', 'Delete')) + '</button></td></tr>').join('') +
      '</tbody></table></div></div>' : '<div class="ag-empty">' + agEsc(agT('agent.no_tasks', 'No automations yet. Activate a template to start.')) + '</div>';
  }
  view.innerHTML = head + body;
  view.querySelectorAll('[data-autov]').forEach(b => b.onclick = () => { AG.autoView = b.dataset.autov; agRenderAuto(view); });
  view.querySelector('#ag-custom').onclick = () => agTaskDialog(null, null);
  view.querySelector('#ag-pause').onclick = async () => {
    const r = await agApi('/api/agent/pause', 'POST', { paused: !(AG.status && AG.status.paused) });
    if (r.ok) { await agRefreshStatus(); agRenderAuto(view); }
  };
  view.querySelectorAll('[data-act]').forEach(b => b.onclick = () => agTaskDialog(AG.templates.find(t => t.id === b.dataset.act), null));
  const byId = id => AG.tasks.find(t => t.id === id);
  view.querySelectorAll('[data-trun],[data-tdry]').forEach(b => b.onclick = async () => {
    const id = b.dataset.trun || b.dataset.tdry;
    const r = await agApi('/api/agent/tasks/' + id + '/run', 'POST', { dry_run: !!b.dataset.tdry });
    agToast(r.ok ? 'ok' : 'critical', r.ok ? agT('agent.started', 'Started — follow it in Activity.') : (r.body.message || 'Failed'));
  });
  view.querySelectorAll('[data-tedit]').forEach(b => b.onclick = () => agTaskDialog(null, byId(b.dataset.tedit)));
  view.querySelectorAll('[data-tdup]').forEach(b => b.onclick = () => { const t = Object.assign({}, byId(b.dataset.tdup)); t.id = null; t.name = t.name + ' ' + agT('agent.copy', '(copy)'); agTaskDialog(null, t); });
  view.querySelectorAll('[data-ttog]').forEach(b => b.onclick = async () => { const t = byId(b.dataset.ttog); await agApi('/api/agent/tasks/' + t.id, 'PATCH', { enabled: !t.enabled }); await agLoadTasks(); agRenderAuto(view); });
  view.querySelectorAll('[data-tdel]').forEach(b => b.onclick = async () => {
    if (!confirm(agT('agent.confirm_delete', 'Delete this automation? Its past runs stay in Activity.'))) return;
    await agApi('/api/agent/tasks/' + b.dataset.tdel, 'DELETE'); await agLoadTasks(); agRenderAuto(view);
  });
}

/** A friendly name for the schedules the dialog offers; any other cron shows as itself. */
function agCronLabel(expr) {
  const hit = AG_SCHEDULES.find(s => s[0] === expr);
  return hit ? agT('agent.sched.' + hit[0].replace(/[^0-9a-z]+/gi, '_'), hit[1]) : expr;
}

function agModeLabel(m) {
  return { report: agT('agent.mode_report', 'Report only'), propose: agT('agent.mode_propose', 'Propose'), act: agT('agent.mode_act', 'Act within policy') }[m] || m;
}


const AG_CHANNELS = ['inapp', 'all', 'discord', 'ntfy', 'telegram', 'email', 'slack', 'webhook'];
const AG_CHANNEL_NAMES = { inapp: 'In-app', all: 'All alert channels', discord: 'Discord', ntfy: 'ntfy', telegram: 'Telegram',
  email: 'Email', slack: 'Slack', webhook: 'Webhook' };

/** A channel can be picked once it is set up in Settings → Alerts (in-app always). */
function agChannelReady(c) {
  if (c === 'inapp' || c === 'all') return true;
  const ready = (AG.settings && AG.settings.channels_configured) || [];
  return ready.indexOf(c) >= 0;
}

/** The catalogue actions an automation may be allowed to take (not the read-only checks). */
function agActionChoices() {
  return ((AG.policy && AG.policy.actions) || []).filter(a => !a.probe);
}

/** Activate a template (tpl) or edit/duplicate/create a task (task). */
function agTaskDialog(tpl, task) {
  const editing = task && task.id;
  const base = task || {};
  const hosts = tpl ? tpl.eligible_hosts : (AG.policy ? AG.policy.hosts.filter(h => h.mode !== 'off').map(h => h.host) : ['local']);
  const chosenHosts = base.hosts && base.hosts.length ? base.hosts : hosts.slice(0, 1);
  const cronNow = base.cron || (tpl && tpl.default_cron) || '0 3 * * *';
  const mode = base.mode || (tpl && tpl.default_mode) || 'report';
  const channels = (base.delivery && base.delivery.channels) || ['inapp'];
  const chosenActions = base.actions || (tpl ? tpl.actions.map(a => a.id) : []);
  const sel = base.provider_id && base.model ? base.provider_id + '|' + base.model : agDefaultModel();
  const scrim = document.createElement('div');
  scrim.className = 'ag-scrim ag-root';
  const sched = AG_SCHEDULES.some(s => s[0] === cronNow) ? cronNow : 'custom';
  scrim.innerHTML = '<div class="ag-dialog" role="dialog" aria-modal="true" aria-label="' + agEsc(agT('agent.activate', 'Activate')) + '">' +
    '<h3>' + agEsc(editing ? agT('agent.edit_auto', 'Edit automation') : tpl ? agTf('agent.activate_t', 'Activate · {name}', { name: agT('agent.tpl.' + tpl.id + '.name', tpl.name) }) : agT('agent.new_auto', 'New automation')) + '</h3>' +
    (tpl ? '<p class="ag-mut" style="margin:0;font-size:13px">' + agEsc(agT('agent.tpl.' + tpl.id + '.desc', tpl.desc)) + '</p>' +
      (tpl.runs_alone.length || tpl.waits_for_approval.length ? '<p class="ag-mut" style="margin:0;font-size:12px">' +
        agEsc(agTf('agent.tpl_split', 'Runs on its own (if your policy allows): {a}. Always waits for approval: {b}.', { a: tpl.runs_alone.join(', ') || '—', b: tpl.waits_for_approval.join(', ') || '—' })) + '</p>' : '') : '') +
    '<div class="ag-fld"><label for="agd-name">' + agEsc(agT('agent.f.name', 'Name')) + '</label><input class="ag-field" id="agd-name" value="' + agEsc(base.name || (tpl ? agT('agent.tpl.' + tpl.id + '.name', tpl.name) : '')) + '"></div>' +
    (tpl ? '' : '<div class="ag-fld"><label for="agd-prompt">' + agEsc(agT('agent.f.prompt', 'What should it do?')) + '</label><textarea class="ag-field" id="agd-prompt" rows="4">' + agEsc(base.prompt || '') + '</textarea></div>') +
    '<div class="ag-fld"><span class="lbl">' + agEsc(agT('agent.f.hosts', 'Hosts')) + '</span><div class="ag-checks">' +
    hosts.map(h => '<label><input type="checkbox" data-h="' + agEsc(h) + '"' + (chosenHosts.indexOf(h) >= 0 ? ' checked' : '') + '> ' + agEsc(h) + '</label>').join('') + '</div></div>' +
    '<div class="ag-fld"><label for="agd-sched">' + agEsc(agT('agent.f.schedule', 'Schedule')) + '</label><select class="ag-field" id="agd-sched">' +
    AG_SCHEDULES.map(s => '<option value="' + s[0] + '"' + (sched === s[0] ? ' selected' : '') + '>' + agEsc(agT('agent.sched.' + s[0].replace(/[^0-9a-z]+/gi, '_'), s[1])) + '</option>').join('') +
    '<option value="custom"' + (sched === 'custom' ? ' selected' : '') + '>' + agEsc(agT('agent.sched.custom', 'Custom cron…')) + '</option></select>' +
    '<input class="ag-field ag-mono" id="agd-cron" value="' + agEsc(cronNow) + '"' + (sched === 'custom' ? '' : ' hidden') + '><div class="ag-mut ag-mono" id="agd-preview" style="font-size:12px"></div></div>' +
    '<div class="ag-fld"><label for="agd-mode">' + agEsc(agT('agent.f.mode', 'Mode')) + '</label><select class="ag-field" id="agd-mode">' +
    ['act', 'propose', 'report'].map(m => '<option value="' + m + '"' + (m === mode ? ' selected' : '') + '>' + agEsc({ act: agT('agent.mode_act_long', 'Act within policy (low risk runs, the rest waits for approval)'), propose: agT('agent.mode_propose_long', 'Propose only (everything waits for approval)'), report: agT('agent.mode_report', 'Report only') }[m]) + '</option>').join('') + '</select></div>' +
    '<div class="ag-fld"><span class="lbl">' + agEsc(agT('agent.f.actions', 'Actions it may take (your Agent policy still decides each one)')) + '</span><div class="ag-checks">' +
    agActionChoices().map(a => '<label title="' + agEsc(a.risk) + '"><input type="checkbox" data-act-id="' + agEsc(a.id) + '"' + (chosenActions.indexOf(a.id) >= 0 ? ' checked' : '') + '> ' +
      agEsc(agT('agent.act.' + a.id, a.title)) + '</label>').join('') + '</div>' +
    '<div class="ag-mut" style="font-size:12px">' + agEsc(agT('agent.f.actions_note', 'None ticked = it only reads and reports.')) + '</div></div>' +
    '<div class="ag-fld"><label for="agd-model">' + agEsc(agT('agent.model', 'Model')) + '</label><select class="ag-field" id="agd-model">' + agModelOptions(sel) + '</select>' +
    '<div class="ag-mut" id="agd-est" style="font-size:12px"></div></div>' +
    '<div class="ag-fld"><span class="lbl">' + agEsc(agT('agent.f.delivery', 'Send the report to')) + '</span><div class="ag-checks">' +
    AG_CHANNELS.map(c => { const ok = agChannelReady(c); return '<label' + (ok ? '' : ' class="ag-mut"') + '><input type="checkbox" data-ch="' + c + '"' + (channels.indexOf(c) >= 0 && ok ? ' checked' : '') + (ok ? '' : ' disabled') + '> ' +
      agEsc(agT('agent.ch.' + c, AG_CHANNEL_NAMES[c] || c)) + (ok ? '' : ' (' + agEsc(agT('agent.ch.not_set', 'not set up')) + ')') + '</label>'; }).join('') + '</div></div>' +
    '<div class="ag-err" id="agd-err"></div>' +
    '<div class="ag-row" style="justify-content:flex-end"><button class="ag-btn" id="agd-cancel">' + agEsc(agT('agent.cancel', 'Cancel')) + '</button>' +
    (editing ? '' : '<button class="ag-btn" id="agd-dry">' + agEsc(agT('agent.run_once', 'Run once now (report only)')) + '</button>') +
    '<button class="ag-btn pri" id="agd-ok">' + agEsc(editing ? agT('agent.save', 'Save') : agT('agent.activate', 'Activate')) + '</button></div></div>';
  document.body.appendChild(scrim);
  const $ = s => scrim.querySelector(s);
  const close = () => scrim.remove();
  scrim.addEventListener('click', e => { if (e.target === scrim) close(); });
  scrim.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  $('#agd-cancel').onclick = close;
  const cronVal = () => $('#agd-sched').value === 'custom' ? $('#agd-cron').value.trim() : $('#agd-sched').value;
  const preview = async () => {
    const r = await agApi('/api/agent/cron/preview', 'POST', { cron: cronVal() });
    $('#agd-preview').textContent = r.body.ok ? agT('agent.next_fires', 'Next: ') + r.body.next.slice(0, 3).map(x => x.replace('T', ' ')).join(' · ') + ' (' + r.body.tz + ')' : (r.body.error || '');
  };
  $('#agd-sched').onchange = () => { $('#agd-cron').hidden = $('#agd-sched').value !== 'custom'; preview(); };
  $('#agd-cron').oninput = preview;
  preview();
  const collect = () => {
    const [pid, model] = String($('#agd-model').value || '').split('|');
    const body = { name: $('#agd-name').value.trim(), cron: cronVal(), mode: $('#agd-mode').value,
      hosts: [...scrim.querySelectorAll('[data-h]')].filter(x => x.checked).map(x => x.dataset.h),
      delivery: { channels: [...scrim.querySelectorAll('[data-ch]')].filter(x => x.checked).map(x => x.dataset.ch) },
      provider_id: pid || null, model: model || null };
    if (tpl) body.template_id = tpl.id;
    else if (!editing && base.template_id) { body.template_id = base.template_id; }
    const p = $('#agd-prompt'); if (p) body.prompt = p.value.trim();
    body.actions = [...scrim.querySelectorAll('[data-act-id]')].filter(x => x.checked).map(x => x.dataset.actId);
    if (!tpl && base.tools && !editing) body.tools = base.tools;
    return body;
  };
  const est = () => {
    const box = $('#agd-est'); if (!box) return;
    const [pid, model] = String($('#agd-model').value || '').split('|');
    const m = AG.models.find(x => x.provider_id === pid && x.model === model);
    const p = AG.providers.find(x => x.id === pid);
    const tokens = (tpl && tpl.est_tokens) || base.est_tokens || 0;
    if (!tokens) { box.textContent = ''; return; }
    const price = m ? (m.price_in || 0) : 0;
    box.textContent = agTf('agent.est', 'About {n} tokens per run', { n: tokens.toLocaleString() }) + ' · ' +
      (p && p.local ? agT('agent.est_local', 'local model: free') : price ? agTf('agent.est_cost', '≈ {c} per run', { c: (tokens * price / 1e6).toFixed(3) }) : agT('agent.est_noprice', 'set a price under Models & keys to see the cost'));
  };
  $('#agd-model').onchange = est;
  est();
  const save = async () => {
    const body = collect();
    const r = editing ? await agApi('/api/agent/tasks/' + task.id, 'PATCH', body) : await agApi('/api/agent/tasks', 'POST', body);
    if (!r.ok) { $('#agd-err').textContent = Object.entries(r.body.fields || {}).map(([k, v]) => k + ': ' + v).join(' · ') || r.body.message || 'Failed'; return null; }
    return r.body.task;
  };
  $('#agd-ok').onclick = async () => { const t = await save(); if (t) { close(); await agLoadTasks(); AG.autoView = 'mine'; agRender(true); } };
  const dry = $('#agd-dry');
  if (dry) dry.onclick = async () => {
    const t = await save(); if (!t) return;
    await agApi('/api/agent/tasks/' + t.id + '/run', 'POST', { dry_run: true });
    close(); await agLoadTasks(); AG.view = 'activity'; agRender();
  };
  $('#agd-name').focus();
}

// ── activity ─────────────────────────────────────────────────────────────────

function agRenderActivity(view) {
  const hosts = AG.policy ? AG.policy.hosts.map(h => h.host) : [];
  const f = AG.filters;
  const qs = Object.entries(f).filter(([, v]) => v).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  let html = '<div class="ag-row" style="margin-bottom:10px">' +
    '<select class="ag-modelsel" id="agf-host" aria-label="' + agEsc(agT('agent.f.host', 'Host')) + '"><option value="">' + agEsc(agT('agent.all_hosts', 'All hosts')) + '</option>' + hosts.map(h => '<option' + (f.host === h ? ' selected' : '') + '>' + agEsc(h) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-status" aria-label="' + agEsc(agT('agent.f.outcome', 'Outcome')) + '"><option value="">' + agEsc(agT('agent.all_outcomes', 'All outcomes')) + '</option>' +
    ['done', 'waiting_approval', 'failed', 'refused', 'cancelled', 'interrupted', 'budget_exhausted'].map(s => '<option value="' + s + '"' + (f.status === s ? ' selected' : '') + '>' + agEsc(agStatusLabel(s)) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-trigger" aria-label="' + agEsc(agT('agent.f.trigger', 'Trigger')) + '"><option value="">' + agEsc(agT('agent.all_triggers', 'Chat and automations')) + '</option>' +
    ['chat', 'schedule', 'manual', 'dry_run'].map(s => '<option value="' + s + '"' + (f.trigger === s ? ' selected' : '') + '>' + agEsc(agTriggerLabel(s)) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-task" aria-label="' + agEsc(agT('agent.f.task', 'Automation')) + '"><option value="">' + agEsc(agT('agent.all_tasks', 'All automations')) + '</option>' +
    AG.tasks.map(t => '<option value="' + agEsc(t.id) + '"' + (f.task_id === t.id ? ' selected' : '') + '>' + agEsc(t.name) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-action" aria-label="' + agEsc(agT('agent.pol.action', 'Action')) + '"><option value="">' + agEsc(agT('agent.all_actions', 'All actions')) + '</option>' +
    ((AG.policy && AG.policy.actions) || []).map(a => '<option value="' + agEsc(a.id) + '"' + (f.action === a.id ? ' selected' : '') + '>' + agEsc(agT('agent.act.' + a.id, a.title)) + '</option>').join('') + '</select>' +
    '<input type="date" class="ag-modelsel" id="agf-since" aria-label="' + agEsc(agT('agent.f.since', 'Since')) + '" value="' + agEsc(f.sinceDate || '') + '">' +
    '<input class="ag-modelsel" id="agf-q" placeholder="' + agEsc(agT('agent.f.search', 'Search titles')) + '" value="' + agEsc(f.q || '') + '">' +
    '<span class="ag-sp"></span><a class="ag-btn sm" href="/api/agent/audit/export?format=json' + (qs ? '&' + agEsc(qs) : '') + '" download>' + agEsc(agT('agent.export_json', 'Export JSON')) + '</a>' +
    '<a class="ag-btn sm" href="/api/agent/audit/export?format=csv' + (qs ? '&' + agEsc(qs) : '') + '" download>' + agEsc(agT('agent.export_csv', 'Export CSV')) + '</a></div>';
  html += '<div class="ag-card" style="padding:4px 6px"><div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.col.when', 'When')) + '</th><th>' + agEsc(agT('agent.col.run', 'Run')) + '</th><th>' +
    agEsc(agT('agent.col.trigger', 'Trigger')) + '</th><th>' + agEsc(agT('agent.model', 'Model')) + '</th><th>' + agEsc(agT('agent.col.tokens', 'Tokens')) + '</th><th>' + agEsc(agT('agent.col.outcome', 'Outcome')) + '</th></tr></thead><tbody>' +
    (AG.runs.length ? AG.runs.map(r => '<tr class="clk' + (AG.runSel === r.id ? ' sel' : '') + '" data-run="' + agEsc(r.id) + '"><td class="ag-mono">' + agEsc(agWhenFull(r.created_at)) + '</td><td>' + agEsc(r.title || r.id.slice(0, 8)) + '</td><td class="ag-mut">' +
      agEsc(agTriggerLabel(r.trigger)) + (r.actor ? ' · ' + agEsc(r.actor) : '') + '</td><td class="ag-mono ag-mut">' + agEsc(r.model || '—') + '</td><td class="ag-mono">' + agEsc((r.prompt_tokens || 0) + ' / ' + (r.completion_tokens || 0)) + (r.cost ? ' · ' + agEsc(r.cost.toFixed(4)) : '') + '</td><td>' + agStatusPill(r.status) + '</td></tr>').join('')
      : '<tr><td colspan="6" class="ag-mut">' + agEsc(agT('agent.no_runs', 'Nothing yet.')) + '</td></tr>') + '</tbody></table></div>' +
    (AG.runsNext ? '<div class="ag-row" style="justify-content:center;margin:6px"><button class="ag-btn sm" id="agf-more">' + agEsc(agT('agent.load_more', 'Load more')) + '</button></div>' : '') + '</div>';
  html += '<div id="ag-rundetail"></div>';
  view.innerHTML = html;
  const reload = async () => { await agLoadRuns(); agRenderActivity(view); };
  view.querySelector('#agf-host').onchange = e => { AG.filters.host = e.target.value; reload(); };
  view.querySelector('#agf-status').onchange = e => { AG.filters.status = e.target.value; reload(); };
  view.querySelector('#agf-trigger').onchange = e => { AG.filters.trigger = e.target.value; reload(); };
  view.querySelector('#agf-task').onchange = e => { AG.filters.task_id = e.target.value; reload(); };
  view.querySelector('#agf-action').onchange = e => { AG.filters.action = e.target.value; reload(); };
  view.querySelector('#agf-since').onchange = e => { AG.filters.sinceDate = e.target.value; AG.filters.since = e.target.value ? Math.floor(new Date(e.target.value + 'T00:00:00').getTime() / 1000) : ''; reload(); };
  view.querySelector('#agf-q').onchange = e => { AG.filters.q = e.target.value.trim(); reload(); };
  const more = view.querySelector('#agf-more');
  if (more) more.onclick = async () => { await agLoadRuns(true); agRenderActivity(view); };
  view.querySelectorAll('[data-run]').forEach(tr => tr.onclick = async () => {
    AG.runSel = tr.dataset.run; await agLoadRun(AG.runSel); agRenderActivity(view);
    const box = document.getElementById('ag-rundetail'); if (box) box.scrollIntoView({ block: 'start' });   // phone: the detail is far below (#11)
  });
  if (AG.runDetail && AG.runSel === AG.runDetail.run.id) agPaintRunDetail(view.querySelector('#ag-rundetail'), AG.runDetail);
}

/** One line per step: who/what/where/why, readable on a phone. */
function agStepLine(s) {
  const tool = (s.tool || '').replace(/__/g, '.');
  const where = s.host ? ' · ' + s.host + (s.target ? ' → ' + s.target : '') : '';
  switch (s.kind) {
    case 'model': return { tag: agT('agent.k.model', 'model'), text: (s.tokens_in || 0) + '→' + (s.tokens_out || 0) + ' tok' + (s.excerpt ? ' · ' + s.excerpt.slice(0, 160) : (s.args && s.args.tool_calls ? ' · ' + s.args.tool_calls.join(', ') : '')) };
    case 'tool': return { tag: agT('agent.k.read', 'read'), text: tool + where + ' → ' + (s.result_bytes || 0) + ' B' + (s.ok ? '' : ' ✕') };
    case 'probe_intent': return { tag: agT('agent.k.check', 'check'), text: tool + where };
    case 'probe_result': return { tag: agT('agent.k.check', 'check'), text: tool + where + (s.ok ? ' ✓' : ' ✕') + (s.excerpt ? ' · ' + s.excerpt.slice(0, 160) : '') };
    case 'action_intent': return { tag: agT('agent.k.act', 'act'), text: tool + where + ' · ' + agT('agent.k.policy', 'policy') + ' ' + (s.decision_reason || '') + (s.approval_id ? ' · ' + agT('agent.k.approval', 'approval') + ' ' + s.approval_id.slice(0, 8) : '') };
    case 'action_result': return { tag: agT('agent.k.act', 'act'), text: tool + where + (s.ok ? ' ✓ ' : ' ✕ ') + (s.excerpt || '').slice(0, 200) };
    case 'policy': return { tag: agT('agent.k.policy', 'policy'), text: tool + where + ' · ' + (s.decision || '') + ' (' + (s.decision_reason || '') + ')' + (s.excerpt ? ' · ' + s.excerpt.slice(0, 200) : '') };
    case 'approval': return { tag: agT('agent.k.approval', 'approval'), text: tool + where + ' · ' + agT('agent.k.parked', 'waiting for approval') + (s.excerpt ? ' · "' + s.excerpt.slice(0, 160) + '"' : '') };
    default: return { tag: agT('agent.k.note', 'note'), text: (s.excerpt || tool || '').slice(0, 300) };
  }
}

function agPaintRunDetail(box, d) {
  const r = d.run;
  const dur = r.started_at && r.ended_at ? (r.ended_at - r.started_at) + ' s' : '—';
  box.innerHTML = '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.run_detail', 'Run detail')) + ' · ' + agEsc(r.title || r.id.slice(0, 8)) + ' ' + agStatusPill(r.status) + '</div>' +
    '<dl class="ag-kv"><dt>' + agEsc(agT('agent.col.trigger', 'Trigger')) + '</dt><dd>' + agEsc(r.trigger + ' · ' + (r.actor || '') + (r.attended ? '' : ' · ' + agT('agent.unattended', 'unattended'))) + '</dd>' +
    '<dt>' + agEsc(agT('agent.model', 'Model')) + '</dt><dd class="ag-mono">' + agEsc((r.model || '—') + ' · ' + (r.prompt_tokens || 0) + ' in / ' + (r.completion_tokens || 0) + ' out' + (r.cost ? ' · ' + r.cost.toFixed(4) : '')) + '</dd>' +
    '<dt>' + agEsc(agT('agent.col.mode', 'Mode')) + '</dt><dd>' + agEsc(agModeLabel(r.mode)) + '</dd>' +
    '<dt>' + agEsc(agT('agent.duration', 'Duration')) + '</dt><dd>' + agEsc(dur + ' · ' + (r.steps || 0) + ' ' + agT('agent.steps', 'steps')) + '</dd>' +
    (r.status_reason ? '<dt>' + agEsc(agT('agent.why_ended', 'Why it ended')) + '</dt><dd>' + agEsc(r.status_reason) + '</dd>' : '') + '</dl>' +
    '<div style="margin-top:10px">' + d.steps.map(s => { const l = agStepLine(s); return '<div class="ag-step"><span class="ag-mono ag-mut">' + agEsc(new Date(s.ts * 1000).toLocaleTimeString()) + '</span><span><b>' + agEsc(l.tag) + '</b> ' + agEsc(l.text) + '</span></div>'; }).join('') + '</div>' +
    (d.approvals.length ? '<div class="ag-h" style="margin-top:12px">' + agEsc(agT('agent.v.approvals', 'Approvals')) + '</div>' + d.approvals.map(a => agApprovalCard(a, false)).join('') : '') +
    '<div class="ag-row" style="margin-top:10px"><button class="ag-btn sm" id="ag-open-conv">' + agEsc(agT('agent.open_conv', 'Open the conversation')) + '</button>' +
    ((r.status === 'running' || r.status === 'queued' || r.status === 'waiting_approval') ? '<button class="ag-btn sm no" id="ag-stop-run">' + agEsc(agT('agent.stop', 'Stop')) + '</button>' : '') + '</div></div>';
  agWireApprovalButtons(box);
  box.querySelector('#ag-open-conv').onclick = async () => { AG.convId = r.conversation_id; AG.view = 'chat'; agRender(); };
  const st = box.querySelector('#ag-stop-run');
  if (st) st.onclick = async () => { await agApi('/api/agent/runs/' + r.id + '/cancel', 'POST', {}); await agLoadRun(r.id); agRender(true); };
}

// ── policy ───────────────────────────────────────────────────────────────────

function agRenderPolicy(view) {
  const p = AG.policy;
  if (!p) { view.innerHTML = '<div class="ag-empty">…</div>'; return; }
  if (!AG.policyHost || !p.hosts.some(h => h.host === AG.policyHost)) AG.policyHost = (p.hosts.find(h => h.mode === 'active') || p.hosts[0]).host;
  const H = p.hosts.find(h => h.host === AG.policyHost);
  const rules = (p.rules || {})[H.host] || {};
  let html = p.controls_enabled
    ? '<div class="ag-banner">' + agEsc(agT('agent.pol.controls_on', 'Controls are on (ENABLE_CONTROLS=1). Turning them off makes every action Deny, whatever is set here. The manual buttons on the Containers and Services tabs follow ENABLE_CONTROLS only, not this page.')) + '</div>'
    : '<div class="ag-banner warn">' + agEsc(agT('agent.pol.controls_off', 'Controls are off (ENABLE_CONTROLS=0): the agent can read and run read-only checks, but every action is Deny.')) + '</div>';
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.per_box', 'Per box')) + '</div><div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.pol.host', 'Host')) + '</th><th>' +
    agEsc(agT('agent.pol.access', 'Agent access')) + '</th><th>' + agEsc(agT('agent.pol.meaning', 'Meaning')) + '</th></tr></thead><tbody>' +
    p.hosts.map(h => '<tr><td><span class="ag-mono">' + agEsc(h.host) + '</span><div class="ag-mut" style="font-size:12px">' + agEsc(h.os + (h.capabilities.length ? ' · ' + h.capabilities.join(', ') : '')) + '</div></td><td><div class="ag-tri" data-host="' + agEsc(h.host) + '">' +
      [['off', 'n', agT('agent.mode.off', 'Off')], ['passive', 'p', agT('agent.mode.passive', 'Passive')], ['active', 'y', agT('agent.mode.active', 'Active')]].map(([m, c, l]) => '<button data-mode="' + m + '" class="' + (h.mode === m ? 'on ' + c : '') + '">' + agEsc(l) + '</button>').join('') +
      '</div></td><td class="ag-mut" style="font-size:12px">' + agEsc({ off: agT('agent.pol.off_m', 'The agent cannot see this box'), passive: agT('agent.pol.passive_m', 'Read only'), active: agT('agent.pol.active_m', 'Can act, within the actions you allow') }[h.mode]) + '</td></tr>').join('') +
    '</tbody></table></div><p class="ag-mut" style="font-size:12px;margin:8px 0 0">' + agEsc(agT('agent.pol.mcp_note', 'New hosts start Passive. Off applies to this in-app agent only; the read-only MCP server still sees the whole fleet.')) + '</p></div>';
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.actions_on', 'Allowed actions on')) + ' <select class="ag-modelsel" id="agp-host">' + p.hosts.map(h => '<option' + (h.host === H.host ? ' selected' : '') + '>' + agEsc(h.host) + '</option>').join('') + '</select></div>' +
    (H.mode !== 'active' ? '<div class="ag-banner warn">' + agEsc(agTf('agent.pol.not_active', '{host} is not Active, so none of these run there until you set it Active.', { host: H.host })) + '</div>' : '') +
    '<div class="ag-scrollhint">' + agEsc(agT('agent.swipe', '← swipe the table to see the rules →')) + '</div>' +
    '<div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.pol.action', 'Action')) + '</th><th>' + agEsc(agT('agent.pol.risk', 'Risk')) + '</th><th>' + agEsc(agT('agent.pol.runs', 'Runs')) + '</th><th>' +
    agEsc(agT('agent.pol.in_chat', 'In chat')) + '</th><th>' + agEsc(agT('agent.pol.unattended', 'Unattended')) + '</th></tr></thead><tbody>' +
    p.actions.filter(a => !a.probe).map(a => {
      const cur = rules[a.id] || {};
      const rule = cur.rule || 'deny';
      const un = agUnattended(a, rule, cur.unattended_unlocked);
      const key = H.os === 'windows' ? 'windows' : H.host === 'local' ? 'local' : 'linux';
      const supported = a.targets.indexOf(key) >= 0 && (a.requires || []).every(r => H.capabilities.indexOf(r) >= 0);
      return '<tr' + (supported ? '' : ' style="opacity:.5"') + '><td>' + agEsc(agT('agent.act.' + a.id, a.title)) + (a.sudo ? ' <span class="ag-pill">root</span>' : '') + '</td><td>' + agRiskPill(a.risk) + '</td><td class="ag-mono ag-mut" style="font-size:12px">' + agEsc(a.preview[key] || agT('agent.pol.na', 'not available here')) + '</td>' +
        '<td><div class="ag-tri' + (supported ? '' : ' locked') + '" data-action="' + agEsc(a.id) + '">' + [['deny', 'd', agT('agent.rule.deny', 'Deny')], ['ask', 'a', agT('agent.rule.ask', 'Ask')], ['allow', 'y', agT('agent.rule.allow', 'Allow')]].map(([v, c, l]) => '<button data-rule="' + v + '" class="' + (rule === v ? 'on ' + c : '') + '">' + agEsc(l) + '</button>').join('') + '</div></td>' +
        '<td>' + (un.unlockable && rule === 'allow' ? '<button class="ag-btn sm" data-unlock="' + agEsc(a.id) + '" data-on="' + (cur.unattended_unlocked ? '1' : '') + '">' + agEsc(un.text) + '</button>' : '<span class="ag-pill">' + agEsc(un.text) + '</span>') + '</td></tr>';
    }).join('') + '</tbody></table></div><p class="ag-mut" style="font-size:12px;margin:8px 0 0">' + agEsc(agT('agent.pol.unatt_note', 'Unattended runs only do low-risk actions on their own, at most 3 times a day per target. Medium risk waits for approval unless you unlock it here; high risk and anything needing root always waits.')) + '</p></div>';
  const prot = p.protected.filter(x => x.host === H.host);
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.protected', 'Protected on')) + ' ' + agEsc(H.host) + '</div><p class="ag-mut" style="font-size:12px;margin:0 0 8px">' +
    agEsc(agT('agent.pol.protected_p', 'The agent never acts on these. The monitor\'s own container and whatever serves the model in use are always protected. Mark anything else that keeps your lab reachable (VPN, DNS, the reverse proxy).')) + '</p>' +
    '<div class="ag-checks">' + prot.map(x => '<label><input type="checkbox" checked data-prot="' + agEsc(x.kind + '|' + x.name) + '"> ' + agEsc(x.kind + ': ' + x.name) + '</label>').join('') + '</div>' +
    '<div class="ag-row" style="margin-top:8px"><select class="ag-modelsel" id="agp-pkind"><option value="container">container</option><option value="service">service</option><option value="model">model</option></select><input class="ag-field" id="agp-pname" placeholder="' + agEsc(agT('agent.pol.name_ph', 'name')) + '"><button class="ag-btn sm" id="agp-padd">' + agEsc(agT('agent.pol.protect', 'Protect')) + '</button></div></div>';
  const paths = (p.watched_paths || {})[H.host] || [];
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.paths', 'Watched paths on')) + ' ' + agEsc(H.host) + '</div><p class="ag-mut" style="font-size:12px;margin:0 0 8px">' +
    agEsc(agT('agent.pol.paths_p', 'The only folders the file checks (old files, backup freshness) may look at. One absolute path per line. Nothing is ever deleted.')) + '</p>' +
    '<textarea class="ag-field ag-mono" id="agp-paths" rows="3" style="width:100%">' + agEsc(paths.join('\n')) + '</textarea><div class="ag-row" style="margin-top:6px"><button class="ag-btn sm" id="agp-psave">' + agEsc(agT('agent.save', 'Save')) + '</button></div></div>';
  view.innerHTML = html;
  const reload = async () => { await agLoadPolicy(); agRenderPolicy(view); };
  view.querySelectorAll('.ag-tri[data-host] button').forEach(b => b.onclick = async () => {
    const host = b.parentNode.dataset.host;
    const r = await agApi('/api/agent/policy/hosts/' + encodeURIComponent(host), 'PUT', { mode: b.dataset.mode });
    if (!r.ok) agToast('critical', r.body.message || 'Failed');
    reload();
  });
  view.querySelector('#agp-host').onchange = e => { AG.policyHost = e.target.value; agRenderPolicy(view); };
  view.querySelectorAll('.ag-tri[data-action] button').forEach(b => b.onclick = async () => {
    const action = b.parentNode.dataset.action;
    const r = await agApi('/api/agent/policy/hosts/' + encodeURIComponent(H.host) + '/actions/' + encodeURIComponent(action), 'PUT', { rule: b.dataset.rule });
    if (!r.ok) agToast('critical', r.body.message || 'Failed');
    reload();
  });
  view.querySelectorAll('[data-unlock]').forEach(b => b.onclick = async () => {
    const on = !b.dataset.on;
    if (on && !confirm(agT('agent.pol.unlock_confirm', 'Let automations do this on their own, with nobody watching? It is logged, and you can lock it again any time.'))) return;
    await agApi('/api/agent/policy/hosts/' + encodeURIComponent(H.host) + '/actions/' + encodeURIComponent(b.dataset.unlock), 'PUT', { rule: 'allow', unattended_unlocked: on, acknowledge: on });
    reload();
  });
  view.querySelectorAll('[data-prot]').forEach(c => c.onchange = async () => {
    const [kind, name] = c.dataset.prot.split('|');
    await agApi('/api/agent/policy/protected', 'PUT', { host: H.host, kind, name, protected: c.checked });
    reload();
  });
  view.querySelector('#agp-padd').onclick = async () => {
    const name = view.querySelector('#agp-pname').value.trim(); if (!name) return;
    const r = await agApi('/api/agent/policy/protected', 'PUT', { host: H.host, kind: view.querySelector('#agp-pkind').value, name, protected: true });
    if (!r.ok) agToast('critical', r.body.message || 'Failed');
    reload();
  };
  view.querySelector('#agp-psave').onclick = async () => {
    const paths2 = view.querySelector('#agp-paths').value.split('\n').map(s => s.trim()).filter(Boolean);
    const r = await agApi('/api/agent/policy/paths', 'PUT', { host: H.host, paths: paths2 });
    agToast(r.ok ? 'ok' : 'critical', r.ok ? agT('agent.saved', 'Saved.') : (r.body.message || 'Failed'));
    if (r.ok) reload();
  };
}

// ── models & keys ────────────────────────────────────────────────────────────

/** A provider test result in the page's language (#24); errors by kind, never a raw body (#36). */
function agTestText(t) {
  if (!t) return '';
  if (t.ok) return agTf('agent.m.models_n', '{n} models', { n: (t.models || []).length || parseInt(t.detail, 10) || 0 });
  const kinds = { auth: agT('agent.err.auth', 'The provider rejected the key (401/403).'), unreachable: agT('agent.err.unreachable', 'The provider can’t be reached.'),
    rate_limited: agT('agent.err.rate_limited', 'The provider is rate-limiting (429).'), server: agT('agent.err.server', 'The provider had an internal error (5xx).'),
    bad_request: agT('agent.err.bad_request', 'The provider refused the request — check the URL.'), timeout: agT('agent.err.timeout', 'The provider took too long to answer.'),
    protocol: agT('agent.err.protocol', 'The answer wasn’t what a model server sends — check the URL.') };
  return kinds[t.error_kind] || t.detail || agT('agent.failed', 'Something went wrong.');
}

/** A tool-probe result in the page's language. */
function agProbeText(r) {
  if (r.tools === 'yes' && r.multi_step === 'yes') return agT('agent.probe.good', 'Calls tools and follows up on their results.');
  if (r.tools === 'yes') return agT('agent.probe.single', 'Calls tools, but didn’t follow up on a tool result — fine for questions, weak for multi-step tasks.');
  if (r.tools === 'no') return agT('agent.probe.no', 'This model answered in text instead of calling the tool, so it can’t drive the agent.');
  return agTf('agent.probe.unknown', 'Couldn’t test it: {why}', { why: agTestText({ ok: false, error_kind: null, detail: r.detail }) });
}

function agBadge(m) {
  const t = { yes: ['ok', agT('agent.badge.tools_yes', 'tool calls: works')], no: ['crit', agT('agent.badge.tools_no', 'no tool calls')], unknown: ['', agT('agent.badge.untested', 'untested')] }[m.tools] || ['', m.tools];
  let html = '<span class="ag-pill ' + t[0] + '">' + agEsc(t[1]) + '</span>';
  if (m.tools === 'yes') html += ' <span class="ag-pill ' + (m.multi_step === 'yes' ? 'ok' : m.multi_step === 'no' ? 'warn' : '') + '">' + agEsc(m.multi_step === 'yes' ? agT('agent.badge.multi_yes', 'multi-step: yes') : m.multi_step === 'no' ? agT('agent.badge.multi_no', 'multi-step: weak') : agT('agent.badge.multi_unknown', 'multi-step: ?')) + '</span>';
  return html;
}

function agRenderModels(view) {
  const st = AG.status || {};
  const s = AG.settings || {};
  const def = agDefaultModel();
  const byProv = {};
  AG.models.forEach(m => { (byProv[m.provider_id] = byProv[m.provider_id] || []).push(m); });
  let html = '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.m.agent', 'The agent')) + '</div><div class="ag-row">' +
    '<label class="ag-row"><input type="checkbox" id="agm-on"' + (st.enabled ? ' checked' : '') + '> ' + agEsc(agT('agent.m.enabled', 'Agent enabled')) + '</label>' +
    '<label class="ag-row">' + agEsc(agT('agent.m.tz', 'Time zone')) + ' <input class="ag-field ag-mono" id="agm-tz" style="width:180px" value="' + agEsc(s.tz || '') + '" placeholder="' + agEsc(s.tz_effective || 'UTC') + '"></label>' +
    '<label class="ag-row">' + agEsc(agT('agent.m.cap_day', 'Cloud spend cap per day')) + ' <input class="ag-field" id="agm-cap" style="width:80px" value="' + agEsc(s.cost_cap_day || '') + '"></label>' +
    '<button class="ag-btn sm" id="agm-save">' + agEsc(agT('agent.save', 'Save')) + '</button></div></div>';
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.m.found', 'Found on your lab')) + '</div>' +
    (AG.discovered.length ? '<div class="ag-tw"><table><tbody>' + AG.discovered.map((d, i) => '<tr><td class="ag-mono">' + agEsc(d.server || d.provider_key) + '</td><td class="ag-mono ag-mut">' + agEsc(d.preset + '@' + d.host) + '</td><td>' +
      (d.reachable ? '<span class="ag-pill ok">' + agEsc(agTf('agent.m.models_n', '{n} models', { n: d.models.length })) + '</span>' : '<span class="ag-pill">' + agEsc(agT('agent.m.unreachable', 'seen, but not reachable from the hub')) + '</span>') + '</td><td>' +
      (d.provider_id ? '<span class="ag-pill info">' + agEsc(agT('agent.m.added', 'added')) + '</span>' : '<button class="ag-btn sm pri" data-disc="' + i + '"' + (d.reachable ? '' : ' disabled') + '>' + agEsc(agT('agent.m.add', 'Add')) + '</button>') + '</td></tr>' +
      (d.reachable && d.models.length ? '<tr><td colspan="4"><div class="ag-row">' + d.models.slice(0, 12).map(m => {
        const known = d.provider_id ? AG.models.find(x => x.provider_id === d.provider_id && x.model === m) : null;
        return '<span class="ag-row" style="gap:4px"><span class="ag-mono">' + agEsc(m) + '</span>' + (known ? agBadge(known) : '') +
          '<button class="ag-btn sm" data-disc-use="' + i + '|' + agEsc(m) + '"' + (known && known.tools === 'no' ? ' disabled' : '') + '>' + agEsc(agT('agent.m.use', 'Use')) + '</button></span>';
      }).join('') + '</div></td></tr>' : '')).join('') + '</tbody></table></div>'
      : '<p class="ag-mut">' + agEsc(agT('agent.m.none_found', 'No model servers found on your lab. Add Ollama, vLLM, llama.cpp or LM Studio as a custom AI server in Settings → General, or add one by URL below.')) + '</p>') + '</div>';
  html += AG.providers.map(p => '<div class="ag-card"><div class="ag-h">' + agEsc(p.label) + ' <span class="ag-pill">' + agEsc(p.local ? agT('agent.local', 'local') : agT('agent.cloud', 'cloud')) + '</span>' + (p.experimental ? '<span class="ag-pill warn">' + agEsc(agT('agent.m.experimental', 'experimental')) + '</span>' : '') +
    '<span class="ag-sp"></span><button class="ag-btn sm" data-ptest="' + agEsc(p.id) + '">' + agEsc(agT('agent.m.test', 'Test & list models')) + '</button><button class="ag-btn sm no" data-pdel="' + agEsc(p.id) + '">' + agEsc(agT('agent.delete', 'Delete')) + '</button></div>' +
    '<div class="ag-mut ag-mono" style="font-size:12px">' + agEsc(p.base_url) + (p.key_set ? ' · ' + agEsc(agT('agent.m.key', 'key')) + ' ' + agEsc(p.key_ref ? p.key_ref : '••••' + (p.key_last4 || '')) : '') + '</div>' +
    (p.last_test ? '<div style="font-size:12px;margin-top:4px">' + (p.last_test.ok ? '<span class="ag-pill ok">' + agEsc(agTestText(p.last_test)) + '</span>' : '<span class="ag-pill crit">' + agEsc(agTestText(p.last_test)) + '</span>') + '</div>' : '') +
    (p.local ? '' : '<div class="ag-row" style="margin-top:6px"><input class="ag-field" type="password" autocomplete="new-password" placeholder="' + agEsc(p.key_set ? agT('agent.m.replace_key', 'Replace key') : agT('agent.m.paste_key', 'Paste API key')) + '" data-pkey="' + agEsc(p.id) + '"><button class="ag-btn sm" data-pkeysave="' + agEsc(p.id) + '">' + agEsc(agT('agent.save', 'Save')) + '</button></div>') +
    ((byProv[p.id] || []).length ? '<div class="ag-tw" style="margin-top:8px"><table><tbody>' + byProv[p.id].map(m => {
      const v = m.provider_id + '|' + m.model;
      return '<tr><td class="ag-mono">' + agEsc(m.model) + (m.context_window ? ' <span class="ag-mut">' + agEsc(Math.round(m.context_window / 1024) + 'k ctx') + '</span>' : '') + '</td><td>' + agBadge(m) + (m.probed_at ? '<div class="ag-mut" style="font-size:12px">' + agEsc(agProbeText(m)) + '</div>' : '') + '</td>' +
        (p.local ? '' : '<td><input class="ag-field" style="width:70px" data-pin="' + agEsc(v) + '" value="' + agEsc(m.price_in || '') + '" placeholder="in/M" title="' + agEsc(agT('agent.m.price_t', 'Price per million tokens, in the Costs tab currency')) + '"> <input class="ag-field" style="width:70px" data-pout="' + agEsc(v) + '" value="' + agEsc(m.price_out || '') + '" placeholder="out/M"></td>') +
        '<td class="ag-row"><button class="ag-btn sm" data-probe="' + agEsc(v) + '">' + agEsc(agT('agent.m.test_model', 'Test')) + '</button>' +
        (v === def ? '<span class="ag-pill info">' + agEsc(agT('agent.m.default', 'default')) + '</span>' : '<button class="ag-btn sm pri" data-use="' + agEsc(v) + '"' + (m.tools === 'no' ? ' disabled' : '') + '>' + agEsc(agT('agent.m.use', 'Use')) + '</button>') + '</td></tr>';
    }).join('') + '</tbody></table></div>' : '') + '</div>').join('');
  const presets = AG.presets;
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.m.add_provider', 'Add a model provider')) + '</div><div class="ag-row">' +
    '<select class="ag-field" id="agm-preset">' + presets.map(p => '<option value="' + agEsc(p.id) + '">' + agEsc(p.label + (p.local ? '' : ' (' + agT('agent.m.own_key', 'your key') + ')') + (p.experimental ? ' · ' + agT('agent.m.experimental', 'experimental') : '')) + '</option>').join('') + '</select>' +
    '<input class="ag-field" id="agm-url" placeholder="http://host:port/v1" style="min-width:220px">' +
    '<input class="ag-field" id="agm-key" type="password" autocomplete="new-password" placeholder="' + agEsc(agT('agent.m.key_opt', 'API key (or env:NAME)')) + '">' +
    '<input class="ag-field" id="agm-dep" placeholder="deployment (Azure)" hidden>' +
    '<button class="ag-btn pri sm" id="agm-addp">' + agEsc(agT('agent.m.add', 'Add')) + '</button></div>' +
    '<p class="ag-mut" style="font-size:12px;margin:8px 0 0">' + agEsc(agT('agent.m.key_note', 'Keys are stored on the hub, never shown again, and left out of backups. With env:NAME or file:/path the key stays outside the database.')) + '</p></div>';
  view.innerHTML = html;
  const reload = async () => { await agLoadModels(false); agRenderModels(view); };
  view.querySelector('#agm-on').onchange = async e => {
    const r = await agApi('/api/agent/enable', 'POST', { enabled: e.target.checked });
    if (!r.ok) agToast('critical', r.body.message || 'Failed');
    await agRefreshStatus();
  };
  view.querySelector('#agm-save').onclick = async () => {
    const r = await agApi('/api/agent/settings', 'PUT', { tz: view.querySelector('#agm-tz').value.trim(), cost_cap_day: view.querySelector('#agm-cap').value.trim() || '2.00' });
    agToast(r.ok ? 'ok' : 'critical', r.ok ? agT('agent.saved', 'Saved.') : (r.body.message || 'Failed'));
    if (r.ok) AG.settings = r.body;
  };
  const presetSel = view.querySelector('#agm-preset');
  const syncPreset = () => {
    const p = presets.find(x => x.id === presetSel.value) || {};
    const u = view.querySelector('#agm-url');
    u.hidden = !!(p.base_url && !p.local && p.id !== 'azure_openai');
    u.placeholder = p.id === 'azure_openai' ? 'https://NAME.openai.azure.com' : p.adapter === 'ollama' ? 'http://host:11434' : 'http://host:port/v1';
    view.querySelector('#agm-key').hidden = !p.needs_key && p.local;
    view.querySelector('#agm-dep').hidden = (p.extra_fields || []).indexOf('deployment') < 0;
  };
  presetSel.onchange = syncPreset; syncPreset();
  view.querySelector('#agm-addp').onclick = async () => {
    const k = view.querySelector('#agm-key').value.trim();
    const body = { preset: presetSel.value, base_url: view.querySelector('#agm-url').value.trim() || undefined };
    if (k.startsWith('env:') || k.startsWith('file:')) body.key_ref = k; else if (k) body.api_key = k;
    const dep = view.querySelector('#agm-dep').value.trim(); if (dep) body.extra = { deployment: dep };
    const r = await agApi('/api/agent/providers', 'POST', body);
    if (!r.ok) { agToast('critical', r.body.message || 'Failed'); return; }
    await agApi('/api/agent/providers/' + r.body.provider.id + '/test', 'POST', {});
    await agLoadSettings(); reload();
  };
  view.querySelectorAll('[data-disc]').forEach(b => b.onclick = async () => {
    const d = AG.discovered[+b.dataset.disc];
    const r = await agApi('/api/agent/providers', 'POST', { preset: d.preset, base_url: d.base_url, label: (d.server || d.preset) + ' @ ' + d.host, source: 'discovered' });
    if (r.ok) { await agApi('/api/agent/providers/' + r.body.provider.id + '/test', 'POST', {}); await agLoadSettings(); }
    await agLoadModels(true); agRenderModels(view);
  });
  view.querySelectorAll('[data-disc-use]').forEach(b => b.onclick = async () => {
    const cut = b.dataset.discUse.indexOf('|');
    const d = AG.discovered[+b.dataset.discUse.slice(0, cut)], model = b.dataset.discUse.slice(cut + 1);
    b.disabled = true; b.textContent = agT('agent.m.testing', 'Testing…');
    let pid = d.provider_id;
    if (!pid) {
      const r = await agApi('/api/agent/providers', 'POST', { preset: d.preset, base_url: d.base_url, label: (d.server || d.preset) + ' @ ' + d.host, source: 'discovered' });
      if (!r.ok) { agToast('critical', r.body.message || agT('agent.failed', 'Something went wrong.')); return; }
      pid = r.body.provider.id;
      await agApi('/api/agent/providers/' + pid + '/test', 'POST', {});
    }
    const pr = await agApi('/api/agent/models/probe', 'POST', { provider_id: pid, model });
    if (pr.body.tools === 'yes') {
      const r = await agApi('/api/agent/settings', 'PUT', { default_provider_id: pid, default_model: model });
      if (r.ok) AG.settings = r.body;
    }
    agToast(pr.body.tools === 'yes' ? 'ok' : 'warning', agProbeText(pr.body));
    await agLoadModels(true); agRenderModels(view);
  });
  view.querySelectorAll('[data-ptest]').forEach(b => b.onclick = async () => {
    b.disabled = true; const r = await agApi('/api/agent/providers/' + b.dataset.ptest + '/test', 'POST', {});
    agToast(r.body.ok ? 'ok' : 'critical', agTestText(r.body)); reload();
  });
  view.querySelectorAll('[data-pdel]').forEach(b => b.onclick = async () => {
    if (!confirm(agT('agent.m.del_confirm', 'Remove this provider? Automations using it will show a warning.'))) return;
    await agApi('/api/agent/providers/' + b.dataset.pdel, 'DELETE'); await agLoadSettings(); reload();
  });
  view.querySelectorAll('[data-pkeysave]').forEach(b => b.onclick = async () => {
    const inp = view.querySelector('[data-pkey="' + b.dataset.pkeysave + '"]');
    const v = inp.value.trim(); if (!v) return;
    const body = (v.startsWith('env:') || v.startsWith('file:')) ? { key_ref: v } : { api_key: v };
    const r = await agApi('/api/agent/providers/' + b.dataset.pkeysave, 'PATCH', body);
    agToast(r.ok ? 'ok' : 'critical', r.ok ? agT('agent.saved', 'Saved.') : (r.body.message || 'Failed')); reload();
  });
  view.querySelectorAll('[data-probe]').forEach(b => b.onclick = async () => {
    const [pid, model] = b.dataset.probe.split('|');
    b.disabled = true; b.textContent = agT('agent.m.testing', 'Testing…');
    const r = await agApi('/api/agent/models/probe', 'POST', { provider_id: pid, model });
    agToast(r.body.tools === 'yes' ? 'ok' : 'warning', agProbeText(r.body)); reload();
  });
  view.querySelectorAll('[data-use]').forEach(b => b.onclick = async () => {
    const [pid, model] = b.dataset.use.split('|');
    const r = await agApi('/api/agent/settings', 'PUT', { default_provider_id: pid, default_model: model });
    if (r.ok) AG.settings = r.body; reload();
  });
  view.querySelectorAll('[data-pin],[data-pout]').forEach(inp => inp.onchange = async () => {
    const [pid, model] = (inp.dataset.pin || inp.dataset.pout).split('|');
    const body = { provider_id: pid, model };
    body[inp.dataset.pin ? 'price_in' : 'price_out'] = inp.value || 0;
    const r = await agApi('/api/agent/models', 'PUT', body);
    if (!r.ok) agToast('critical', r.body.message || 'Failed');
  });
}

// ── the floating Ask widget ──────────────────────────────────────────────────

function agMountWidget() {
  if (document.querySelector('.ag-fab')) return;
  const fab = document.createElement('button');
  fab.className = 'ag-fab';
  fab.type = 'button';
  fab.setAttribute('aria-label', agT('agent.ask_aria', 'Ask the AI agent'));
  fab.innerHTML = '✦ ' + agEsc(agT('agent.ask', 'Ask')) + '<span class="ag-badge" hidden></span>';
  fab.onclick = () => (AG.widgetOpen ? agCloseWidget() : agOpenWidget());
  document.body.appendChild(fab);
  agSyncFab();
}

function agSyncFab() {
  const fab = document.querySelector('.ag-fab');
  if (!fab) return;
  const show = agFabVisible(AG.status, typeof TAB !== 'undefined' ? TAB : '', document.body.classList.contains('display'));
  fab.hidden = !show;
  if (!show && AG.widgetOpen) agCloseWidget();
  agPaintBadge();
}

async function agOpenWidget() {
  AG.widgetOpen = true;
  await Promise.all([agLoadModelChoices(), agLoadConvs()]);
  if (AG.widgetConv) await agLoadWidgetConv();
  agRenderWidget();
  const ta = document.querySelector('#agw-ta'); if (ta) ta.focus();
}

function agCloseWidget() {
  AG.widgetOpen = false;
  const p = document.querySelector('.ag-panel'); if (p) p.remove();
}

async function agLoadWidgetConv() {
  if (!AG.widgetConv) { AG.widgetDetail = null; return; }
  const r = await agApi('/api/agent/conversations/' + encodeURIComponent(AG.widgetConv));
  AG.widgetDetail = r.ok ? r.body : null;
}

function agRenderWidget() {
  let panel = document.querySelector('.ag-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'ag-panel ag-root';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', agT('agent.widget_title', 'Ask your homelab'));
    document.body.appendChild(panel);
    panel.addEventListener('keydown', e => { if (e.key === 'Escape') agCloseWidget(); });
  }
  const ctx = agContext();
  const hasModel = AG.models.some(m => m.tools !== 'no');
  const kept = agSaveInput('agw');
  panel.innerHTML = '<div class="ag-ph"><b>' + agEsc(agT('agent.widget_title', 'Ask your homelab')) + '</b>' +
    (ctx.tab ? '<span class="ag-ctx">' + agEsc(agTf('agent.context', 'context: {tab}{host}', { tab: ctx.tab, host: ctx.host ? ' · ' + ctx.host : '' })) + '</span>' : '') +
    '<span class="ag-sp"></span><button class="ag-btn sm" id="agw-new" title="' + agEsc(agT('agent.new_chat', '+ New chat')) + '">+</button>' +
    '<button class="ag-btn sm" id="agw-open">' + agEsc(agT('agent.open_in_agent', 'Open in Agent')) + '</button>' +
    '<button class="ag-btn sm" id="agw-close" aria-label="' + agEsc(agT('agent.close', 'Close')) + '">✕</button></div>' +
    '<div class="ag-ph" style="padding-top:0;border-bottom:0"><select class="ag-modelsel" id="agw-hist" aria-label="' + agEsc(agT('agent.recent', 'Recent chats')) + '">' +
    '<option value="">' + agEsc(agT('agent.recent', 'Recent chats')) + '</option>' +
    AG.convs.filter(c => c.origin === 'chat').slice(0, 20).map(c => '<option value="' + agEsc(c.id) + '"' + (c.id === AG.widgetConv ? ' selected' : '') + '>' +
      agEsc((c.title || agT('agent.untitled', 'New chat')).slice(0, 60)) + '</option>').join('') + '</select></div>' +
    '<div class="ag-msgs" id="agw-msgs" aria-live="polite"></div>' + agComposer('agw', hasModel);
  agPaintMessages(panel.querySelector('#agw-msgs'), AG.widgetDetail, hasModel);
  panel.querySelector('#agw-close').onclick = agCloseWidget;
  panel.querySelector('#agw-hist').onchange = async e => { AG.widgetConv = e.target.value || null; await agLoadWidgetConv(); agRenderWidget(); };
  panel.querySelector('#agw-new').onclick = () => { AG.widgetConv = null; AG.widgetDetail = null; agRenderWidget(); };
  panel.querySelector('#agw-open').onclick = () => { AG.convId = AG.widgetConv; AG.view = 'chat'; agCloseWidget(); showTab('agent'); };
  agWireComposer(panel, 'agw', () => AG.widgetConv, id => { AG.widgetConv = id; }, agContext);
  agRestoreInput('agw', kept);
}

agBoot();
