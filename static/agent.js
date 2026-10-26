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
const AG_VIEWS = ['chat', 'auto', 'approvals', 'activity', 'settings'];
const AG_SETTINGS = ['models', 'policy'];
// The suggestion chips: a short declared list (texts live in the locale files).
// The third field names what the lab needs for the question to make sense (from its data).
const AG_SUGGEST = [['agent.sug.disks', 'How are my disks?'], ['agent.sug.wrong', 'Anything wrong right now?'],
  ['agent.sug.gpu', 'What is using the GPU?', 'gpu'], ['agent.sug.clean', 'What can I clean up?'],
  ['agent.sug.containers', 'Which containers use the most memory?', 'docker']];
const AG_ICON_PATH = '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>';
const AG_ICON = '<svg class="ag-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + AG_ICON_PATH + '</svg>';
const AG_ICON_SM = AG_ICON.replace('ag-ic', 'ag-ic sm');
const AG_CHECK = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg>';
const AG_PLUS = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
const AG_SCHEDULES = [['0 3 * * *', 'Daily · 03:00'], ['0 8 * * *', 'Daily · 08:00'], ['30 7 * * *', 'Daily · 07:30'],
  ['0 9 * * *', 'Daily · 09:00'], ['0 3 * * 0', 'Weekly · Sun 03:00'], ['0 4 * * 0', 'Weekly · Sun 04:00'],
  ['0 5 * * 0', 'Weekly · Sun 05:00'], ['0 2 * * 6', 'Weekly · Sat 02:00'], ['0 8 * * 1', 'Weekly · Mon 08:00'],
  ['0 */6 * * *', 'Every 6 hours']];
const AG = {
  status: null, view: 'chat', convs: [], convId: null, detail: null, providers: [], models: [], discovered: [],
  presets: [], approvals: [], tasks: [], templates: [], policy: null, runs: [], runSel: null, runDetail: null,
  streaming: {}, chipOpen: {}, stopping: {}, widgetOpen: false, widgetConv: null, pollTimer: null, refetchTimer: null,
  lastEvent: 0, filters: {}, policyHost: null, autoView: 'templates', settings: null, listOpen: false,
  setup: null, overview: null, runOpen: {}, setSec: 'models', wiz: null, homeTimer: null, wizFocused: false,
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
/** Model text → safe HTML: escape everything, then a small markdown subset
 *  (paragraphs, headings, lists, tables, bold, italics, code, internal links). */
function agMarkdown(text) {
  const src = agEsc(text || '');
  const inline = s => s
    .replace(/`([^`\n]+)`/g, (_, c) => '<code>' + c + '</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, (_, b) => '<b>' + b + '</b>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, (_, p, x) => p + '<i>' + x + '</i>')
    .replace(/\[([^\]\n]{1,80})\]\((#[a-z]+(?:\?host=[A-Za-z0-9_.\-]{1,128})?)\)/g,
      (all, label, href) => agDeepLink(href) ? '<a href="' + href + '" data-ag-link="' + href + '">' + label + '</a>' : all)
    .replace(/(^|[\s(])(#[a-z]+(?:\?host=[A-Za-z0-9_.\-]{1,128})?)(?=$|[\s).,;:!?])/g,
      (all, pre, href) => agDeepLink(href) ? pre + '<a href="' + href + '" data-ag-link="' + href + '">' + agEsc(agLinkLabel(href)) + '</a>' : all);
  const row = l => /^\s*\|.*\|\s*$/.test(l);
  const sep = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  const out = [];
  let list = null, tag = 'ul', code = null;
  const flush = () => { if (list) { out.push('<' + tag + '>' + list.join('') + '</' + tag + '>'); list = null; } };
  const lines = src.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {                       // a fenced block: shown as written, never parsed
      if (code === null) { flush(); code = []; } else { out.push('<pre class="ag-pre"><code>' + code.join('\n') + '</code></pre>'); code = null; }
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    if (row(line) && i + 1 < lines.length && sep(lines[i + 1])) {
      flush();
      const head = cells(line), body = [];
      i += 2;
      while (i < lines.length && row(lines[i])) { body.push(cells(lines[i])); i++; }
      i--;
      out.push('<div class="ag-tbl"><table><thead><tr>' + head.map(h => '<th>' + inline(h) + '</th>').join('') +
        '</tr></thead><tbody>' + body.map(r => '<tr>' + head.map((_, k) => '<td>' + inline(r[k] || '') + '</td>').join('') + '</tr>').join('') +
        '</tbody></table></div>');
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); out.push('<hr>'); continue; }
    const h = /^\s*#{1,4}\s+(.*)$/.exec(line);
    if (h) { flush(); out.push('<h4>' + inline(h[1]) + '</h4>'); continue; }
    const ul = /^\s*[-*•]\s+(.*)$/.exec(line), ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ul || ol) {
      const t = ol ? 'ol' : 'ul';
      if (list && tag !== t) flush();
      if (!list) { list = []; tag = t; }
      list.push('<li>' + inline((ul || ol)[1]) + '</li>');
      continue;
    }
    flush();
    if (line.trim()) out.push('<p>' + inline(line) + '</p>');
  }
  flush();
  if (code !== null) out.push('<pre class="ag-pre"><code>' + code.join('\n') + '</code></pre>');
  return out.join('');
}

/** An internal link's text: the page's name (and host), not "#disks". */
function agLinkLabel(href) {
  const l = agDeepLink(href);
  if (!l) return href;
  const name = agT('nav.' + l.tab, l.tab.charAt(0).toUpperCase() + l.tab.slice(1));
  return l.host ? name + ' · ' + l.host : name;
}

/** What the page context the widget sends with a question looks like. */
/** The dashboard's UI language (the agent's fallback reply language). */
function agUiLang() {
  return (typeof I18N !== 'undefined' && I18N.locale) || (typeof navigator !== 'undefined' && navigator.language) || 'en';
}

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

/** Who started a run, in words: our own actor ids are kind:id. */
function agActorLabel(actor) {
  const kind = String(actor || '').split(':')[0];
  return { session: agT('agent.actor.you', 'you'), open: agT('agent.actor.you', 'you'), bearer: agT('agent.actor.key', 'an API key'),
    internal: agT('agent.actor.system', 'the hub'), scheduler: '', chat: '' }[kind] || '';
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
  if (typeof NAV_ICON !== 'undefined' && !NAV_ICON.agent) NAV_ICON.agent = AG_ICON_PATH;
  if (!TABS.some(t => t.id === 'agent')) {
    // First in the AI group (approved mockup), where the models already live.
    const at = TABS.findIndex(t => t.zone === 'ai');
    TABS.splice(at < 0 ? TABS.length : at, 0, { id: 'agent', label: 'Agent', charts: false, zone: 'ai' });
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
  agMountWidget();
  document.addEventListener('hl:tab', e => agOnTab(e.detail));
  document.addEventListener('hl:agent', e => { let ev = null; try { ev = JSON.parse(e.detail); } catch (x) { return; } agOnEvent(ev); });
  window.addEventListener('hashchange', agOnHash);
  setInterval(agHeartbeat, 1500);
  await agHomeRefresh();
  agPaintBadge();
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
  const fresh = AG.setup ? !AG.setup.done : !(AG.status && AG.status.enabled);
  const row = document.querySelector('.nrow[data-t="agent"]');
  if (row) {
    let b = row.querySelector('.ag-badge');
    const text = fresh ? agT('agent.new_badge', 'NEW') : n ? String(n) : '';
    if (text && !b) { b = document.createElement('span'); b.className = 'ag-badge'; row.appendChild(b); }
    if (b) { if (text) { b.textContent = text; b.classList.toggle('new', fresh); } else b.remove(); }
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
  else if (parts[1] === 'policy' || parts[1] === 'models') { AG.view = 'settings'; AG.setSec = parts[1]; }
  else if (parts[1] === 'routines' || parts[1] === 'auto') AG.view = 'auto';
  else if (parts[1] === 'setup') { agSetupOpen(); return; }
  else if (parts[1] && AG_VIEWS.indexOf(parts[1]) >= 0) AG.view = parts[1];
  if (TAB !== 'agent') showTab('agent'); else agRender();
}

function agOnTab(id) {
  document.body.classList.toggle('ag-tab-on', id === 'agent');
  agSyncFab();
  if (id === 'agent') agRender();
  if (id === 'overview') agHomeRefresh();
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
  if ((need.approvals || need.runs) && typeof TAB !== 'undefined' && TAB === 'overview') agHomeSoon();
}

function agScheduleRefetch(need) {
  AG.pendingNeed = Object.assign(AG.pendingNeed || {}, need);
  if (AG.refetchTimer) return;
  AG.refetchTimer = setTimeout(async () => {
    const n = AG.pendingNeed || {};
    AG.pendingNeed = null; AG.refetchTimer = null;
    if (n.conv) {
      await agLoadConv(AG.convId);
      if (AG.widgetConv && AG.widgetConv !== AG.convId) await agLoadWidgetConv();
      if ((TAB === 'agent' && AG.view === 'chat') || AG.widgetOpen) await agLoadConvs();   // a new title arrives after the answer
    }
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
  if (!soft || !root.querySelector('.ag-head')) {
    const label = { chat: agT('agent.v.chat', 'Chat'), auto: agT('agent.v.routines', 'Routines'), approvals: agT('agent.v.approvals', 'Approvals'),
      activity: agT('agent.v.activity', 'Activity'), settings: agT('agent.v.settings', 'Settings') };
    root.innerHTML = '<div class="ag-head"><h2>' + AG_ICON + agEsc(agT('agent.title_short', 'Agent')) + '</h2><div class="ag-seg" role="tablist">' + AG_VIEWS.map(v =>
      '<button type="button" role="tab" data-agv="' + v + '" aria-selected="' + (AG.view === v ? 'true' : 'false') + '" class="' + (AG.view === v ? 'on' : '') + '">' + agEsc(label[v]) +
      (v === 'approvals' ? '<span class="ag-badge" hidden></span>' : '') + '</button>').join('') + '</div><span class="ag-headnote" id="ag-headnote"></span></div><div id="ag-view"></div>';
    root.querySelectorAll('[data-agv]').forEach(b => b.onclick = () => { AG.view = b.dataset.agv; agRender(); });
    agPaintBadge();
  }
  root.querySelectorAll('[data-agv]').forEach(b => { const on = b.dataset.agv === AG.view; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false'); });
  const view = root.querySelector('#ag-view');
  if ((!st.password_set || !st.enabled) && AG.view !== 'settings') {
    view.innerHTML = agSetupCard();
    agWireSetupCard(view);
    return;
  }
  if (!soft) await agLoadView();
  const note = root.querySelector('#ag-headnote'); if (note) note.textContent = agHeadNote();
  ({ chat: agRenderChat, auto: agRenderAuto, approvals: agRenderApprovals, activity: agRenderActivity,
    settings: agRenderSettings })[AG.view](view);
}

async function agLoadView() {
  if (AG.view === 'chat') { await Promise.all([agLoadConvs(), agLoadApprovals(), agLoadModelChoices(), agLoadPolicy()]); if (AG.convId) await agLoadConv(AG.convId); }
  if (AG.view === 'auto') await Promise.all([agLoadTasks(), agLoadTemplates(), agLoadModelChoices(), agLoadPolicy()]);  // model choices load settings too
  if (AG.view === 'approvals') await Promise.all([agLoadApprovals(), agLoadPolicy()]);
  if (AG.view === 'activity') { await Promise.all([agLoadRuns(), agLoadPolicy(), agLoadTasks()]); if (AG.runSel) await agLoadRun(AG.runSel); }
  if (AG.view === 'settings') await Promise.all([agLoadPolicy(), agLoadModels(AG.setSec === 'models'), agLoadSettings()]);
}

// ── loaders ──────────────────────────────────────────────────────────────────

async function agLoadConvs() { const r = await agApi('/api/agent/conversations?limit=100'); if (r.ok) AG.convs = r.body.items || []; }
async function agLoadConv(id) {
  if (!id) { AG.detail = null; return; }
  const r = await agApi('/api/agent/conversations/' + encodeURIComponent(id));
  if (r.ok) AG.detail = r.body; else if (r.status === 404) { AG.convId = null; AG.detail = null; }
}
async function agLoadApprovals() { const r = await agApi('/api/agent/approvals?status=pending'); if (r.ok) AG.approvals = r.body.items || []; }
async function agLoadTasks() { const r = await agApi('/api/agent/tasks'); if (r.ok) { AG.tasks = r.body.items || []; AG.tasksLoaded = true; } }
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
  return AG.models.filter(m => prov[m.provider_id] && m.tools !== 'no').map(m => {
    const v = m.provider_id + '|' + m.model;
    return '<option value="' + agEsc(v) + '"' + (v === selected ? ' selected' : '') + '>' + agEsc(m.model + ' · ' + prov[m.provider_id].label) + '</option>';
  }).join('');
}

function agDefaultModel() {
  const s = AG.settings || {};
  return (s.default_provider_id && s.default_model) ? s.default_provider_id + '|' + s.default_model : '';
}

// ── chat ─────────────────────────────────────────────────────────────────────

function agRenderChat(view) {
  const hasModel = AG.models.some(m => m.tools !== 'no');
  const convs = AG.convs.filter(c => c.origin === 'chat');
  const tasks = AG.convs.filter(c => c.origin === 'task').slice(0, 12);
  const kept = agSaveInput('ag');
  const bucket = { today: agT('agent.today', 'Today'), yesterday: agT('agent.yesterday', 'Yesterday'), earlier: agT('agent.earlier', 'Earlier') };
  const row = c => '<button type="button" class="ag-conv' + (c.id === AG.convId ? ' on' : '') + '" data-cid="' + agEsc(c.id) + '" title="' + agEsc(agWhenFull(c.updated_at)) + '">' + agEsc(c.title || agT('agent.untitled', 'New chat')) + '</button>';
  view.innerHTML = '<div class="ag-chat' + (AG.listOpen ? ' show-list' : '') + '">' +
    '<aside class="ag-convs" aria-label="' + agEsc(agT('agent.chats_aria', 'Conversations')) + '"><button type="button" class="ag-btn ag-newchat" id="ag-new">' + AG_PLUS + agEsc(agT('agent.new_chat_btn', 'New chat')) + '</button>' +
    agGroupConvs(convs, Date.now()).map(([k, cs]) => '<div class="ag-glabel">' + agEsc(bucket[k]) + '</div>' + cs.map(row).join('')).join('') +
    (convs.length ? '' : '<p class="ag-mut ag-small">' + agEsc(agT('agent.no_chats', 'Your chats will show here.')) + '</p>') +
    (tasks.length ? '<div class="ag-glabel">' + agEsc(agT('agent.routine_runs', 'Routine runs')) + '</div>' + tasks.map(row).join('') : '') +
    '</aside>' +
    '<div class="ag-chatcol"><div class="ag-phonebar"><button type="button" class="ag-btn sm" id="ag-list">' + agEsc(agT('agent.chats', '☰ Chats')) + '</button><span class="ag-sp"></span>' +
    '<button type="button" class="ag-btn sm" id="ag-new2">' + AG_PLUS + agEsc(agT('agent.new_chat_btn', 'New chat')) + '</button></div>' +
    '<div class="ag-msgs" id="ag-msgs" aria-live="polite"></div>' + agComposer('ag', hasModel) + '</div></div>';
  const fresh = async () => { AG.convId = null; AG.detail = null; AG.listOpen = false; agRender(true); };
  view.querySelector('#ag-new').onclick = fresh;
  view.querySelector('#ag-new2').onclick = fresh;
  view.querySelector('#ag-list').onclick = () => { AG.listOpen = true; agRender(true); };
  view.querySelectorAll('[data-cid]').forEach(b => b.onclick = async () => { AG.convId = b.dataset.cid; AG.listOpen = false; await agLoadConv(AG.convId); agRender(true); });
  agPaintMessages(view.querySelector('#ag-msgs'), AG.detail, hasModel);
  agWireComposer(view, 'ag', () => AG.convId, id => { AG.convId = id; }, () => null);
  agRestoreInput('ag', kept);
}

function agModePill(mode) {
  const label = { off: agT('agent.mode.off', 'Off'), passive: agT('agent.mode.passive', 'Passive'), active: agT('agent.mode.active', 'Active') }[mode] || mode;
  return '<span class="ag-pill ' + (mode === 'active' ? 'ok' : mode === 'passive' ? 'info' : '') + '">' + agEsc(label) + '</span>';
}

function agComposer(prefix, hasModel) {
  return '<div class="ag-composer"><div class="ag-cbox">' +
    '<label class="ag-vh" for="' + prefix + '-ta">' + agEsc(agT('agent.message', 'Message')) + '</label>' +
    '<textarea id="' + prefix + '-ta" rows="2" placeholder="' + agEsc(agT('agent.ask_ph', 'Ask about your lab…')) + '"' + (hasModel ? '' : ' disabled') + '></textarea>' +
    '<div class="ag-crow"><label class="ag-modelpill"><span class="ag-vh">' + agEsc(agT('agent.model', 'Model')) + '</span>' +
    '<select id="' + prefix + '-model">' + agModelOptions(agDefaultModel()) + '</select></label><span class="ag-sp"></span>' +
    '<button type="button" class="ag-btn" id="' + prefix + '-stop" hidden>' + agEsc(agT('agent.stop', 'Stop')) + '</button>' +
    '<button type="button" class="ag-btn pri" id="' + prefix + '-send"' + (hasModel ? '' : ' disabled') + '>' + agEsc(agT('agent.send', 'Send')) + '</button></div>' +
    '<div class="ag-err" id="' + prefix + '-err" role="alert"></div></div></div>';
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
      { text, provider_id: pid || undefined, model: model || undefined, context: Object.assign({}, getContext() || {}, { lang: agUiLang() }) });
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
  return { text: (ok === null ? '… ' : ok ? '✓ ' : '✕ ') + agToolLabel(c.name) + (host ? ' · ' + host : ''), bad: ok === false };
}

function agPaintMessages(box, detail, hasModel) {
  if (!box) return;
  if (!hasModel) {
    box.innerHTML = '<div class="ag-empty"><h3>' + agEsc(agT('agent.no_model_h', 'Pick a model first')) + '</h3><p>' + agEsc(agT('agent.no_model', 'No model is set up yet. The agent can use a local model (Ollama, vLLM, llama.cpp, LM Studio) or a cloud one with your own key.')) +
      '</p><button class="ag-btn pri" data-agsetup="1">' + agEsc(agT('agent.setup_go', 'Set it up')) + '</button></div>';
    box.querySelector('[data-agsetup]').onclick = () => agSetupOpen();
    return;
  }
  if (!detail || !(detail.messages || []).length) {
    box.innerHTML = '<div class="ag-empty ag-hello">' + AG_ICON + '<h3>' + agEsc(agT('agent.hello', 'Ask your lab')) + '</h3><p>' +
      agEsc(agT('agent.hello_p', 'Ask about your machines in plain words. It looks before it answers, and only acts where you allow it.')) + '</p>' +
      '<div class="ag-sugs">' + agSuggestChips() + '</div></div>';
    agWireSuggestions(box);
    return;
  }
  const runs = {};
  (detail.runs || []).forEach(r => { runs[r.id] = r; });
  const groups = agRunGroups(detail);
  const lastRun = groups.length ? groups[groups.length - 1].run_id : null;
  let html = '';
  for (const g of groups) {
    const r = runs[g.run_id] || null;
    if (g.user != null) html += '<div class="ag-msg u">' + agEsc(g.user) + '</div>';
    let a = '';
    if (g.chips.length) {
      const open = !!AG.runOpen[g.run_id];
      a += '<button type="button" class="ag-work" data-work="' + agEsc(g.run_id || '') + '" aria-expanded="' + (open ? 'true' : 'false') + '">' + AG_ICON_SM +
        '<span>' + agEsc(agWorkLine(r, g.chips.length)) + '</span><span class="ag-caret" aria-hidden="true">›</span></button>';
      if (open) {
        a += '<div class="ag-steps">' + g.chips.map(c => {
          const l = agChipLabel(c);
          return '<button type="button" class="ag-chip' + (AG.chipOpen[c.id] ? ' open' : '') + (l.bad ? ' bad' : '') + '" data-chip="' + agEsc(c.id) + '">' + agEsc(l.text) + '</button>' +
            (AG.chipOpen[c.id] ? '<div class="ag-chipbody">' + agEsc(agPretty(c)) + '</div>' : '');
        }).join('') + '</div>';
      }
    }
    for (const t of g.texts) a += '<div class="ag-md">' + agMarkdown(t) + '</div>';
    for (const c of g.chips) if (c.card) a += agApprovalCard(c.card, false);
    const live = r && (r.status === 'running' || r.status === 'queued');
    if (live && AG.streaming[r.id]) a += '<div class="ag-md" data-stream="' + agEsc(r.id) + '">' + agMarkdown(AG.streaming[r.id]) + '</div>';
    if (live) a += '<div class="ag-typing" data-typing="' + agEsc(r.id) + '"><span class="ag-dots" aria-hidden="true"></span>' + agEsc(agT('agent.thinking', 'Working…')) + '</div>';
    if (r && r.status === 'waiting_approval') a += '<div class="ag-typing">' + agEsc(agT('agent.waiting', 'Waiting for your approval above.')) + '</div>';
    if (r && !live && r.status !== 'waiting_approval') {
      const ups = g.run_id === lastRun ? (r.followups || []) : [];
      a += '<div class="ag-foot">' + agFactsChip(r) + '<span class="ag-sp"></span>' +
        ups.map(q => '<button type="button" class="ag-sug sm" data-sug="' + agEsc(q) + '">' + agEsc(q) + '</button>').join('') + '</div>';
    }
    if (a) html += '<article class="ag-msg a">' + a + '</article>';
  }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.innerHTML = html;
  box.querySelectorAll('[data-work]').forEach(b => b.onclick = () => { AG.runOpen[b.dataset.work] = !AG.runOpen[b.dataset.work]; agRepaint(); });
  box.querySelectorAll('[data-chip]').forEach(b => b.onclick = () => { AG.chipOpen[b.dataset.chip] = !AG.chipOpen[b.dataset.chip]; agRepaint(); });
  box.querySelectorAll('[data-ag-link]').forEach(a => a.onclick = e => { e.preventDefault(); agFollowLink(a.dataset.agLink); });
  agWireSuggestions(box);
  agWireApprovalButtons(box);
  if (atBottom || agActiveRun(detail)) box.scrollTop = box.scrollHeight;
}

/** What the run really did with actions, from its audit (alpha pass 2, N1). Never the model's words. */
function agRunFacts(run) {
  if (!run || !Array.isArray(run.actions)) return '';
  if (!run.actions.length) return '<div class="ag-facts none">' + agEsc(agT('agent.facts.none', 'No actions were proposed in this run.')) + '</div>';
  const cls = { pending: 'warn', approved: 'info', ran: 'ok', failed: 'crit', refused: 'crit', rejected: '', expired: '', cancelled: '' };
  const label = { pending: agT('agent.facts.pending', 'Waiting for your approval'), approved: agT('agent.facts.approved', 'Approved'),
    rejected: agT('agent.facts.rejected', 'Rejected'), expired: agT('agent.facts.expired', 'Expired, not run'), refused: agT('agent.facts.refused', 'Refused'),
    cancelled: agT('agent.facts.cancelled', 'Cancelled'), ran: agT('agent.facts.ran', 'Ran'), failed: agT('agent.facts.failed', 'Failed') };
  return '<div class="ag-facts"><b>' + agEsc(agT('agent.facts.title', 'Actions in this run')) + '</b><ul>' + run.actions.map(f =>
    '<li><span class="ag-pill ' + (cls[f.outcome] || '') + '">' + agEsc(label[f.outcome] || f.outcome) + '</span> ' +
    agEsc(agT('agent.act.' + f.action, f.title)) + ' · ' + agEsc(f.host || '?') + (f.target ? ' → ' + agEsc(f.target) : '') +
    (f.outcome === 'pending' ? ' · <a href="#agent/approvals">' + agEsc(agT('agent.facts.open', 'Open approvals')) + '</a>' : '') +
    ((f.outcome === 'refused' || f.outcome === 'failed') && f.detail ? '<div class="ag-mut">' + agEsc(f.detail.slice(0, 200)) + '</div>' : '') +
    '</li>').join('') + '</ul></div>';
}

function agPretty(c) {
  let txt = c.result ? (c.result.content || '') : agT('agent.no_result', '(no result yet)');
  try { const j = JSON.parse(txt); txt = JSON.stringify(j.untrusted_data !== undefined ? j.untrusted_data : j, null, 1); } catch (e) { /* plain text result */ }
  const args = Object.keys(c.args).length ? JSON.stringify(c.args) + '\n\n' : '';
  return (args + txt).slice(0, 6000);
}

function agPaintStreaming(runId) {
  const el = document.querySelector('[data-stream="' + runId + '"]');
  if (el) { el.innerHTML = agMarkdown(AG.streaming[runId] || ''); const box = el.closest('.ag-msgs'); if (box) box.scrollTop = box.scrollHeight; return; }
  const typing = document.querySelector('[data-typing="' + runId + '"]');
  if (typing) {
    const d = document.createElement('div');
    d.className = 'ag-md'; d.dataset.stream = runId; d.innerHTML = agMarkdown(AG.streaming[runId] || '');
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
  let html = '<div class="ag-appr' + cls + '" role="group" aria-label="' + agEsc(agT('agent.ap.needed', 'Approval needed')) + '" data-appr="' + agEsc(a.id) + '">';
  html += '<div class="ag-appr-main"><div class="t">' + agEsc(agT('agent.act.' + a.action, a.title)) + (target ? ' <span class="ag-mono">' + agEsc(target) + '</span>' : '') +
    ' <span class="ag-mut">·</span> <span class="ag-mono">' + agEsc(a.host) + '</span></div>';
  html += '<div class="s">' + agEsc({ low: agT('agent.risk.low', 'low risk'), medium: agT('agent.risk.medium', 'medium risk'), high: agT('agent.risk.high', 'high risk') }[a.risk] || a.risk || '') +
    (a.seen_count > 1 ? ' · ' + agEsc(agTf('agent.seen', 'proposed {n}×', { n: a.seen_count })) : '') +
    (compact || st !== 'pending' ? '' : ' · ' + agEsc(agT('agent.nothing_runs', 'Nothing runs until you approve.'))) + '</div>';
  if (!compact) {
    if (a.rationale) html += '<div class="ag-why">' + agEsc(a.rationale) + '</div>';
    if (a.command_preview) html += '<details class="ag-cmd"><summary>' + agEsc(agT('agent.ap.command', 'Exact command')) + '</summary><code>' + agEsc(a.command_preview) + '</code></details>';
  }
  html += '</div>';
  if (st === 'pending') {
    html += '<div class="ag-appr-btns"><button type="button" class="ag-btn" data-rj="' + agEsc(a.id) + '">' + agEsc(agT('agent.reject', 'Reject')) + '</button>' +
      '<button type="button" class="ag-btn pri" data-ap="' + agEsc(a.id) + '">' + agEsc(agT('agent.approve', 'Approve')) + '</button></div>';
  } else {
    const who = agActorLabel(a.decided_by);
    html += '<div class="ag-appr-out">' + agEsc(agApprovalOutcome(a)) + (who ? ' · ' + agEsc(agTf('agent.by', 'by {who}', { who })) : '') + '</div>';
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
    const a = r.body.approval;
    if (r.ok && a && (a.status === 'approved' || a.status === 'executing')) agToastWhenDone(a);
  });
}

/** An approval still running when the decision returned: say how it ended once it has. */
async function agToastWhenDone(a) {
  for (let i = 0; i < 20; i++) {
    await new Promise(res => setTimeout(res, 1500));
    const r = await agApi('/api/agent/runs/' + encodeURIComponent(a.run_id));
    const cur = r.ok ? (r.body.approvals || []).find(x => x.id === a.id) : null;
    if (cur && cur.status !== 'approved' && cur.status !== 'executing') {
      agToast(cur.status === 'done' ? 'ok' : 'warning', agApprovalOutcome(cur));
      agScheduleRefetch({ conv: true, runs: true, approvals: true });
      return;
    }
  }
}

function agRenderApprovals(view) {
  const p = AG.approvals;
  view.innerHTML = p.length ? '<div class="ag-list">' + p.map(a => '<div>' + agApprovalCard(a, false) +
    '<div class="ag-mut ag-small ag-under">' + agEsc(agTf('agent.expires', 'expires {when}', { when: agWhen(a.expires_at) })) +
    ' · <a href="#agent/run/' + agEsc(a.run_id) + '">' + agEsc(agT('agent.see_run', 'see the run')) + '</a></div></div>').join('') + '</div>'
    : agEmpty(agT('agent.no_pending_h', 'Nothing waits for you'), agT('agent.no_pending', 'When the agent proposes an action that needs your OK, it shows here and on the Overview.'));
  agWireApprovalButtons(view);
}

// ── automations ──────────────────────────────────────────────────────────────

function agRenderAuto(view) {
  const sub = AG.autoView;
  const head = '<div class="ag-row" style="margin-bottom:10px">' +
    ['templates', 'mine'].map(v => '<button class="ag-btn sm' + (sub === v ? ' pri' : '') + '" data-autov="' + v + '">' +
      agEsc(v === 'templates' ? agT('agent.auto.templates', 'Templates') : agTf('agent.auto.mine_r', 'My routines ({n})', { n: AG.tasks.length })) + '</button>').join('') +
    '<button class="ag-btn sm" id="ag-custom">' + agEsc(agT('agent.auto.custom', '+ New from a prompt')) + '</button><span class="ag-sp"></span>' +
    '<button class="ag-btn sm" id="ag-pause">' + agEsc(AG.status && AG.status.paused ? agT('agent.resume_all_r', 'Resume routines') : agT('agent.pause_all_r', 'Pause all routines')) + '</button></div>' +
    (AG.status && AG.status.paused ? '<div class="ag-banner warn">' + agEsc(agT('agent.paused_note_r', 'All routines are paused. Nothing scheduled runs until you resume.')) + '</div>' : '');
  let body = '';
  if (sub === 'templates') {
    const active = {};
    AG.tasks.forEach(t => { if (t.template_id) active[t.template_id] = t; });
    body = '<div class="ag-grid">' + AG.templates.map(t => {
      const on = active[t.id];
      const meta = [agCronLabel(t.default_cron), t.est_tokens ? agTf('agent.est_short', '~{n}k tokens', { n: Math.round(t.est_tokens / 1000) }) : '',
        t.actions.length === 1 ? agT('agent.may_act_1', 'may propose 1 action') : t.actions.length ? agTf('agent.may_act_n', 'may propose {n} actions', { n: t.actions.length }) : agT('agent.report_only2', 'reports only'),
        t.eligible_hosts.length ? '' : agT('agent.no_hosts', 'no matching host')].filter(Boolean).join(' · ');
      return '<div class="ag-tpl' + (on ? ' on' : '') + '"><div class="ag-row"><h4 class="ag-grow">' + agEsc(agT('agent.tpl.' + t.id + '.name', t.name)) + '</h4>' +
        (on ? '<span class="ag-stat ok">' + agEsc(agT('agent.active', 'Active')) + '</span>' : '') + '</div><p>' + agEsc(agT('agent.tpl.' + t.id + '.desc', t.desc)) + '</p>' +
        '<div class="ag-meta" title="' + agEsc(agT('agent.est_t', 'Estimated tokens per run, on your default model')) + '">' + agEsc(meta) + '</div>' +
        '<div class="ag-row"><button type="button" class="ag-btn sm" data-act="' + agEsc(t.id) + '"' + (t.eligible_hosts.length ? '' : ' disabled') + '>' +
        agEsc(on ? agT('agent.add_another', 'Add another') : agT('agent.activate', 'Activate')) + '</button></div></div>';
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
      '</tbody></table></div></div>' : '<div class="ag-empty">' + agEsc(agT('agent.no_tasks_r', 'No routines yet. Activate a template to start.')) + '</div>';
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
    '<select class="ag-modelsel" id="agf-host"' + (AG.policy ? '' : ' disabled') + ' aria-label="' + agEsc(agT('agent.f.host', 'Host')) + '"><option value="">' + agEsc(agT('agent.all_hosts', 'All hosts')) + '</option>' + hosts.map(h => '<option' + (f.host === h ? ' selected' : '') + '>' + agEsc(h) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-status" aria-label="' + agEsc(agT('agent.f.outcome', 'Outcome')) + '"><option value="">' + agEsc(agT('agent.all_outcomes', 'All outcomes')) + '</option>' +
    ['done', 'waiting_approval', 'failed', 'refused', 'cancelled', 'interrupted', 'budget_exhausted'].map(s => '<option value="' + s + '"' + (f.status === s ? ' selected' : '') + '>' + agEsc(agStatusLabel(s)) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-trigger" aria-label="' + agEsc(agT('agent.f.trigger', 'Trigger')) + '"><option value="">' + agEsc(agT('agent.all_triggers', 'Chat and automations')) + '</option>' +
    ['chat', 'schedule', 'manual', 'dry_run'].map(s => '<option value="' + s + '"' + (f.trigger === s ? ' selected' : '') + '>' + agEsc(agTriggerLabel(s)) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-task"' + (AG.tasksLoaded ? '' : ' disabled') + ' aria-label="' + agEsc(agT('agent.f.task', 'Automation')) + '"><option value="">' + agEsc(agT('agent.all_tasks', 'All automations')) + '</option>' +
    AG.tasks.map(t => '<option value="' + agEsc(t.id) + '"' + (f.task_id === t.id ? ' selected' : '') + '>' + agEsc(t.name) + '</option>').join('') + '</select>' +
    '<select class="ag-modelsel" id="agf-action"' + (AG.policy ? '' : ' disabled') + ' aria-label="' + agEsc(agT('agent.pol.action', 'Action')) + '"><option value="">' + agEsc(agT('agent.all_actions', 'All actions')) + '</option>' +
    ((AG.policy && AG.policy.actions) || []).map(a => '<option value="' + agEsc(a.id) + '"' + (f.action === a.id ? ' selected' : '') + '>' + agEsc(agT('agent.act.' + a.id, a.title)) + '</option>').join('') + '</select>' +
    '<label class="ag-flabel" for="agf-since">' + agEsc(agT('agent.f.since', 'Since')) + '</label><input type="date" class="ag-modelsel" id="agf-since" value="' + agEsc(f.sinceDate || '') + '">' +
    '<input class="ag-modelsel" id="agf-q" placeholder="' + agEsc(agT('agent.f.search', 'Search titles')) + '" value="' + agEsc(f.q || '') + '">' +
    '<span class="ag-sp"></span><a class="ag-btn sm" href="/api/agent/audit/export?format=json' + (qs ? '&' + agEsc(qs) : '') + '" download>' + agEsc(agT('agent.export_json', 'Export JSON')) + '</a>' +
    '<a class="ag-btn sm" href="/api/agent/audit/export?format=csv' + (qs ? '&' + agEsc(qs) : '') + '" download>' + agEsc(agT('agent.export_csv', 'Export CSV')) + '</a></div>';
  html += '<div class="ag-card" style="padding:4px 6px"><div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.col.when', 'When')) + '</th><th>' + agEsc(agT('agent.col.run', 'Run')) + '</th><th>' +
    agEsc(agT('agent.col.trigger', 'Trigger')) + '</th><th>' + agEsc(agT('agent.model', 'Model')) + '</th><th>' + agEsc(agT('agent.col.tokens', 'Tokens')) + '</th><th>' + agEsc(agT('agent.col.outcome', 'Outcome')) + '</th></tr></thead><tbody>' +
    (AG.runs.length ? AG.runs.map(r => '<tr class="clk' + (AG.runSel === r.id ? ' sel' : '') + '" data-run="' + agEsc(r.id) + '"><td class="ag-mono">' + agEsc(agWhenFull(r.created_at)) + '</td><td>' + agEsc(r.label || r.title || r.id.slice(0, 8)) + '</td><td class="ag-mut">' +
      agEsc(agTriggerLabel(r.trigger)) + (agActorLabel(r.actor) ? ' · ' + agEsc(agActorLabel(r.actor)) : '') + '</td><td class="ag-mono ag-mut">' + agEsc(r.model || '—') + '</td><td class="ag-mut">' + agEsc(agTokens((r.prompt_tokens || 0) + (r.completion_tokens || 0))) + (r.cost ? ' · ' + agEsc(r.cost.toFixed(4)) : '') + '</td><td>' + agStatusPill(r.status) + '</td></tr>').join('')
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
  const tool = s.kind === 'model' ? (s.tool || '') : agToolLabel(s.tool);
  const where = s.host ? ' · ' + s.host + (s.target ? ' → ' + s.target : '') : '';
  switch (s.kind) {
    case 'model': return { tag: agT('agent.k.model', 'model'), text: (s.tokens_in || 0) + '→' + (s.tokens_out || 0) + ' tok' + (s.excerpt ? ' · ' + s.excerpt.replace(/[*`#|]+/g, '').replace(/\s+/g, ' ').trim().slice(0, 160) : (s.args && s.args.tool_calls ? ' · ' + s.args.tool_calls.map(agToolLabel).join(', ') : '')) };
    case 'meta': return { tag: agT('agent.k.label', 'label'), text: [(s.args || {}).title, ((s.args || {}).followups || []).join(' · ')].filter(Boolean).join(' — ') };
    case 'tool': return { tag: agT('agent.k.read', 'read'), text: tool + where + (s.ok ? '' : ' ✕') };
    case 'probe_intent': return { tag: agT('agent.k.check', 'check'), text: tool + where };
    case 'probe_result': return { tag: agT('agent.k.check', 'check'), text: tool + where + (s.ok ? ' ✓' : ' ✕') + (s.excerpt ? ' · ' + s.excerpt.slice(0, 160) : '') };
    case 'action_intent': return { tag: agT('agent.k.act', 'act'), text: tool + where + ' · ' + agT('agent.k.policy', 'policy') + ' ' + (s.decision_reason || '') + (s.approval_id ? ' · ' + agT('agent.k.approval', 'approval') + ' ' + s.approval_id.slice(0, 8) : '') };
    case 'action_result': return { tag: agT('agent.k.act', 'act'), text: tool + where + (s.ok ? ' ✓ ' : ' ✕ ') + (s.excerpt || '').slice(0, 200) };
    case 'policy': return { tag: agT('agent.k.policy', 'policy'), text: tool + where + ' · ' + (s.decision || '') + ' (' + (s.decision_reason || '') + ')' + (s.excerpt ? ' · ' + s.excerpt.slice(0, 200) : '') };
    case 'approval': return { tag: agT('agent.k.approval', 'approval'), text: tool + where + ' · ' + agT('agent.k.parked', 'waiting for approval') + (s.excerpt ? ' · "' + s.excerpt.slice(0, 160) + '"' : '') };
    default: return { tag: agT('agent.k.note', 'note'), text: (s.excerpt || tool || '').slice(0, 300) };
  }
}

/** A step's arguments, folded, as readable lines (not raw JSON). */
function agArgs(s) {
  const a = s.args;
  if (!a || typeof a !== 'object' || s.kind === 'model' || s.kind === 'meta' || !Object.keys(a).length) return '';
  const lines = Object.entries(a).map(([k, v]) => k + ': ' + (typeof v === 'object' ? JSON.stringify(v, null, 1).replace(/\s*\n\s*/g, ' ') : String(v)));
  return '<details class="ag-args"><summary>' + agEsc(agT('agent.k.details', 'details')) + '</summary><div class="ag-mono">' + lines.map(agEsc).join('<br>') + '</div></details>';
}

function agPaintRunDetail(box, d) {
  const r = d.run;
  const dur = r.started_at && r.ended_at ? (r.ended_at - r.started_at) + ' s' : '—';
  box.innerHTML = '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.run_detail', 'Run detail')) + ' · ' + agEsc(r.title || r.id.slice(0, 8)) + ' ' + agStatusPill(r.status) + '</div>' +
    '<dl class="ag-kv"><dt>' + agEsc(agT('agent.col.trigger', 'Trigger')) + '</dt><dd>' + agEsc(agTriggerLabel(r.trigger) + (agActorLabel(r.actor) ? ' · ' + agActorLabel(r.actor) : '') + (r.attended ? '' : ' · ' + agT('agent.unattended', 'unattended'))) + '</dd>' +
    '<dt>' + agEsc(agT('agent.model', 'Model')) + '</dt><dd class="ag-mono">' + agEsc((r.model || '—') + ' · ' + agTokens((r.prompt_tokens || 0) + (r.completion_tokens || 0)) + (r.cost ? ' · ' + r.cost.toFixed(4) : '')) + '</dd>' +
    '<dt>' + agEsc(agT('agent.col.mode', 'Mode')) + '</dt><dd>' + agEsc(agModeLabel(r.mode)) + '</dd>' +
    '<dt>' + agEsc(agT('agent.duration', 'Duration')) + '</dt><dd>' + agEsc(dur + ' · ' + (r.steps || 0) + ' ' + agT('agent.steps', 'steps')) + '</dd>' +
    (r.status_reason ? '<dt>' + agEsc(agT('agent.why_ended', 'Why it ended')) + '</dt><dd>' + agEsc(r.status_reason) + '</dd>' : '') + '</dl>' +
    agRunFacts(r) +
    '<div style="margin-top:10px">' + d.steps.map(s => { const l = agStepLine(s); return '<div class="ag-step"><span class="ag-mono ag-mut">' + agEsc(new Date(s.ts * 1000).toLocaleTimeString()) + '</span><span><b>' + agEsc(l.tag) + '</b> ' + agEsc(l.text) + agArgs(s) + '</span></div>'; }).join('') + '</div>' +
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
  const rules = (p.effective_rules || p.rules || {})[H.host] || {};
  const pnames = { look: agT('agent.preset.look', 'Look only'), ask: agT('agent.preset.ask', 'Ask me first'), small: agT('agent.preset.small', 'Handle the small stuff') };
  let html = '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.preset', 'For every machine')) + '</div><div class="ag-row"><div class="ag-seg" role="radiogroup" aria-label="' + agEsc(agT('agent.pol.preset', 'For every machine')) + '">' +
    Object.keys(pnames).map(k => '<button type="button" role="radio" aria-checked="' + (p.preset === k ? 'true' : 'false') + '" class="' + (p.preset === k ? 'on' : '') + '" data-preset="' + k + '">' + agEsc(pnames[k]) + '</button>').join('') + '</div>' +
    '<span class="ag-mut ag-small">' + agEsc(p.preset ? agT('agent.pol.preset_p', 'Machines you add later follow it too. The tables below change one machine or one action.') : agT('agent.pol.preset_none', 'Custom: set per machine below. Picking one resets every machine to it.')) + '</span></div></div>';
  html += p.controls_enabled
    ? '<div class="ag-banner">' + agEsc(agT('agent.pol.controls_on', 'Controls are on (ENABLE_CONTROLS=1). Turning them off makes every action Deny, whatever is set here. The manual buttons on the Containers and Services tabs follow ENABLE_CONTROLS only, not this page.')) + '</div>'
    : '<div class="ag-banner warn">' + agEsc(agT('agent.pol.controls_off', 'Controls are off (ENABLE_CONTROLS=0): the agent can read and run read-only checks, but every action is Deny.')) + '</div>';
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.per_box', 'Per box')) + '</div><div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.pol.host', 'Host')) + '</th><th>' +
    agEsc(agT('agent.pol.access', 'Agent access')) + '</th><th>' + agEsc(agT('agent.pol.meaning', 'Meaning')) + '</th></tr></thead><tbody>' +
    p.hosts.map(h => '<tr><td><span class="ag-mono">' + agEsc(h.host) + '</span><div class="ag-mut" style="font-size:12px">' + agEsc(h.os + (h.capabilities.length ? ' · ' + h.capabilities.join(', ') : '')) + '</div></td><td><div class="ag-tri" data-host="' + agEsc(h.host) + '">' +
      [['off', 'n', agT('agent.mode.off', 'Off')], ['passive', 'p', agT('agent.mode.passive', 'Passive')], ['active', 'y', agT('agent.mode.active', 'Active')]].map(([m, c, l]) => '<button data-mode="' + m + '" class="' + (h.mode === m ? 'on ' + c : '') + '">' + agEsc(l) + '</button>').join('') +
      '</div></td><td class="ag-mut" style="font-size:12px">' + agEsc({ off: agT('agent.pol.off_m', 'The agent cannot see this box'), passive: agT('agent.pol.passive_m', 'Read only'), active: agT('agent.pol.active_m', 'Can act, within the actions you allow') }[h.mode]) + '</td></tr>').join('') +
    '</tbody></table></div><p class="ag-mut" style="font-size:12px;margin:8px 0 0">' + agEsc(agT('agent.pol.mcp_note2', 'Off applies to this in-app agent only; the read-only MCP server still sees the whole fleet.')) + '</p></div>';
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.pol.actions_on', 'Allowed actions on')) + ' <select class="ag-modelsel" id="agp-host">' + p.hosts.map(h => '<option' + (h.host === H.host ? ' selected' : '') + '>' + agEsc(h.host) + '</option>').join('') + '</select></div>' +
    (H.mode !== 'active' ? '<div class="ag-banner warn">' + agEsc(agTf('agent.pol.not_active', '{host} is not Active, so none of these run there until you set it Active.', { host: H.host })) + '</div>' : '') +
    '<div class="ag-scrollhint">' + agEsc(agT('agent.swipe', '← swipe the table to see the rules →')) + '</div>' +
    '<div class="ag-tw"><table><thead><tr><th>' + agEsc(agT('agent.pol.action', 'Action')) + '</th><th>' + agEsc(agT('agent.pol.risk', 'Risk')) + '</th><th class="ag-wide">' + agEsc(agT('agent.pol.runs', 'Runs')) + '</th><th>' +
    agEsc(agT('agent.pol.in_chat', 'In chat')) + '</th><th>' + agEsc(agT('agent.pol.unattended', 'Unattended')) + '</th></tr></thead><tbody>' +
    p.actions.filter(a => !a.probe).map(a => {
      const cur = rules[a.id] || {};
      const rule = cur.rule || 'deny';
      const un = agUnattended(a, rule, cur.unattended_unlocked);
      const key = H.os === 'windows' ? 'windows' : H.host === 'local' ? 'local' : 'linux';
      const supported = a.targets.indexOf(key) >= 0 && (a.requires || []).every(r => H.capabilities.indexOf(r) >= 0);
      return '<tr' + (supported ? '' : ' style="opacity:.5"') + '><td>' + agEsc(agT('agent.act.' + a.id, a.title)) + (a.sudo ? ' <span class="ag-pill">root</span>' : '') + '</td><td>' + agRiskPill(a.risk) + '</td><td class="ag-mono ag-mut ag-wide" style="font-size:12px">' + agEsc(a.preview[key] || agT('agent.pol.na', 'not available here')) + '</td>' +
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
  view.querySelectorAll('[data-preset]').forEach(b => b.onclick = async () => {
    if (b.dataset.preset === p.preset) return;
    if (!confirm(agTf('agent.pol.preset_confirm', 'Set every machine to “{p}”? Changes you made per machine or per action are replaced.', { p: pnames[b.dataset.preset] }))) return;
    const r = await agApi('/api/agent/policy/preset', 'PUT', { preset: b.dataset.preset });
    if (!r.ok) agToast('critical', r.body.message || 'Failed');
    reload();
  });
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
  if (t.ok) return agModelsN((t.models || []).length || parseInt(t.detail, 10) || 0);
  const kinds = { auth: agT('agent.err.auth', 'The provider rejected the key (401/403).'), unreachable: agT('agent.err.unreachable', 'The provider can’t be reached.'),
    rate_limited: agT('agent.err.rate_limited', 'The provider is rate-limiting (429).'), server: agT('agent.err.server', 'The provider had an internal error (5xx).'),
    bad_request: agT('agent.err.bad_request', 'The provider refused the request — check the URL.'), timeout: agT('agent.err.timeout', 'The provider took too long to answer.'),
    protocol: agT('agent.err.protocol', 'The answer wasn’t what a model server sends — check the URL.') };
  return kinds[t.error_kind] || t.detail || agT('agent.failed', 'Something went wrong.');
}

function agModelsN(n) { return n === 1 ? agT('agent.m.models_1', '1 model') : agTf('agent.m.models_n', '{n} models', { n }); }

/** Tokens the way people read them: 950 tokens, 86.5k tokens. */
function agTokens(n) {
  n = Number(n) || 0;
  return n < 1000 ? agTf('agent.tokens', '{n} tokens', { n }) : agTf('agent.tokens', '{n} tokens', { n: (n / 1000).toFixed(n < 100000 ? 1 : 0).replace(/\.0$/, '') + 'k' });
}

/** A tool or catalogue action by its name in words. */
function agToolLabel(name) {
  const id = String(name || '').replace(/__/g, '.');
  const a = ((AG.policy && AG.policy.actions) || []).find(x => x.id === id);
  if (a) return agT('agent.act.' + id, a.title);
  const plain = id.replace(/[._]+/g, ' ').trim();
  return agT('agent.tool.' + id, plain.charAt(0).toUpperCase() + plain.slice(1));
}

/** A tool-probe result in the page's language. */
function agProbeText(r) {
  if (r.tools === 'yes' && r.multi_step === 'yes') return agT('agent.probe.good', 'Calls tools and follows up on their results.');
  if (r.tools === 'yes') return agT('agent.probe.single', 'Calls tools, but didn’t follow up on a tool result — fine for questions, weak for multi-step tasks.');
  if (r.tools === 'no') return agT('agent.probe.no', 'This model answered in text instead of calling the tool, so it can’t drive the agent.');
  return agTf('agent.probe.unknown', 'Couldn’t test it: {why}', { why: agTestText({ ok: false, error_kind: null, detail: r.detail }) });
}

function agUseBtn(attr, value, m) {
  if (m && m.tools === 'no') {
    return '<span class="ag-stat" title="' + agEsc(agT('agent.m.use_no', 'This model answered without calling tools in the test, so the agent can’t look at your lab with it. Pick one marked “tool calls: works”.')) + '">' +
      agEsc(agT('agent.m.cant_use', 'can’t be used')) + '</span>';
  }
  return '<button class="ag-btn sm' + (attr === 'data-use' ? ' pri' : '') + '" ' + attr + '="' + agEsc(value) + '">' + agEsc(agT('agent.m.use', 'Use')) + '</button>';
}

function agBadge(m) {
  if (m.tools === 'yes') return '<span class="ag-stat ok">' + agEsc(agT('agent.wz.works', 'Works with the agent')) + '</span>';
  if (m.tools === 'no') return '<span class="ag-stat no">' + agEsc(agT('agent.wz.cant', 'Can\'t be used')) + '</span>';
  return '<span class="ag-stat">' + agEsc(agT('agent.badge.untested', 'untested')) + '</span>';
}

function agRenderModels(view) {
  const st = AG.status || {};
  const s = AG.settings || {};
  const def = agDefaultModel();
  const byProv = {};
  AG.models.forEach(m => { (byProv[m.provider_id] = byProv[m.provider_id] || []).push(m); });
  let html = '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.m.general', 'General')) + '</div><div class="ag-form">' +
    '<label class="ag-switch"><input type="checkbox" id="agm-on"' + (st.enabled ? ' checked' : '') + '> ' + agEsc(agT('agent.m.enabled', 'Agent enabled')) + '</label>' +
    '<div class="ag-fld"><label for="agm-tz">' + agEsc(agT('agent.m.tz', 'Time zone')) + '</label><input id="agm-tz" value="' + agEsc(s.tz || '') + '" placeholder="' + agEsc(s.tz_effective || 'UTC') + '"></div>' +
    '<div class="ag-fld"><label for="agm-cap">' + agEsc(agTf('agent.m.cap_day_c', 'Cloud spend cap per day ({c})', { c: s.currency || '$' })) + '</label><input id="agm-cap" inputmode="decimal" value="' + agEsc(s.cost_cap_day || '') + '"></div>' +
    '<button type="button" class="ag-btn" id="agm-save">' + agEsc(agT('agent.save', 'Save')) + '</button></div></div>';
  html += '<div class="ag-card"><div class="ag-h">' + agEsc(agT('agent.m.found', 'Found on your lab')) + '</div>' +
    (AG.discovered.some(d => !d.provider_id) ? '<div class="ag-tw"><table class="ag-found"><tbody>' + AG.discovered.map((d, i) => d.provider_id ? '' : '<tr><td class="ag-mono">' + agEsc(d.server || d.provider_key) + '</td><td class="ag-mut">' + agEsc(d.host ? agTf('agent.m.on_host', 'on {h}', { h: d.host }) : agTf('agent.m.at_addr', 'at {a}', { a: String(d.base_url || '').replace(/^https?:\/\//, '') })) + '</td><td>' +
      (d.reachable ? '<span class="ag-stat ok">' + agEsc(agModelsN(d.models.length)) + '</span>' : '<span class="ag-stat">' + agEsc(agT('agent.m.unreachable', 'seen, but not reachable from the hub')) + '</span>') + '</td><td>' +
      (d.provider_id ? '<span class="ag-stat">' + agEsc(agT('agent.m.added', 'added')) + '</span>' : '<button class="ag-btn sm pri" data-disc="' + i + '"' + (d.reachable ? '' : ' disabled') + '>' + agEsc(agT('agent.m.add', 'Add')) + '</button>') + '</td></tr>' +
      (d.reachable && d.models.length ? '<tr><td colspan="4"><div class="ag-row">' + d.models.slice(0, 12).map(m => {
        const known = d.provider_id ? AG.models.find(x => x.provider_id === d.provider_id && x.model === m) : null;
        return '<span class="ag-row" style="gap:4px"><span class="ag-mono">' + agEsc(m) + '</span>' + (known ? agBadge(known) : '') +
          agUseBtn('data-disc-use', i + '|' + m, known) + '</span>';
      }).join('') + '</div></td></tr>' : '')).join('') + '</tbody></table></div>'
      : '<p class="ag-mut">' + agEsc(AG.discovered.length ? agT('agent.m.all_added', 'Every model server found on your lab is added below.') : agT('agent.m.none_found', 'No model servers found on your lab. Add Ollama, vLLM, llama.cpp or LM Studio as a custom AI server in Settings → General, or add one by URL below.')) + '</p>') + '</div>';
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
        (v === def ? '<span class="ag-stat on">' + agEsc(agT('agent.m.in_use', 'In use')) + '</span>' : agUseBtn('data-use', v, m)) + '</td></tr>';
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
  document.body.classList.toggle('ag-fab-on', show);
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
  panel.innerHTML = '<div class="ag-ph">' + AG_ICON_SM + '<b>' + agEsc(agT('agent.hello', 'Ask your lab')) + '</b>' +
    (ctx.tab ? '<span class="ag-ctx" title="' + agEsc(agT('agent.context_t', 'The question is asked about this page')) + '">' + agEsc(agLinkLabel('#' + ctx.tab) + (ctx.host ? ' · ' + ctx.host : '')) + '</span>' : '') +
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

// ── UAT-1: Overview card, setup wizard ─────────────────────────────────────

function agDayBucket(ts, nowMs) {
  const d = new Date(ts * 1000), n = new Date(nowMs);
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(n) - day(d)) / 86400000);
  return diff <= 0 ? 'today' : diff === 1 ? 'yesterday' : 'earlier';
}

function agGroupConvs(convs, nowMs) {
  const order = ['today', 'yesterday', 'earlier'];
  const g = { today: [], yesterday: [], earlier: [] };
  (convs || []).forEach(c => g[agDayBucket(c.updated_at || c.created_at || 0, nowMs)].push(c));
  return order.filter(k => g[k].length).map(k => [k, g[k]]);
}

function agWorkLine(run, nCalls) {
  const w = (run && run.work) || null;
  const m = w ? w.machines : null, n = w ? w.lookups : nCalls, s = w ? w.seconds : null;
  const parts = [];
  if (m) parts.push(m === 1 ? agT('agent.work.machine1', '1 machine') : agTf('agent.work.machines', '{n} machines', { n: m }));
  if (n) parts.push(n === 1 ? agT('agent.work.lookup1', '1 look-up') : agTf('agent.work.lookups', '{n} look-ups', { n }));
  if (s != null && s >= 0) parts.push(agTf('agent.work.secs', '{n} s', { n: s }));
  return agTf('agent.work.checked', 'Checked {what}', { what: parts.join(' · ') || agT('agent.work.nothing', 'nothing yet') });
}

function agFactsChip(run) {
  if (!run || !Array.isArray(run.actions)) return '';
  const a = run.actions;
  if (!a.length) return '<span class="ag-fchip">' + agEsc(agT('agent.facts.chip_none', 'No actions proposed')) + '</span>';
  const pending = a.filter(f => f.outcome === 'pending').length;
  const ran = a.filter(f => f.outcome === 'ran').length;
  const bad = a.filter(f => f.outcome === 'failed' || f.outcome === 'refused').length;
  const parts = [];
  if (pending) parts.push(pending === 1 ? agT('agent.facts.chip_wait1', '1 action waiting for your approval') : agTf('agent.facts.chip_wait', '{n} actions waiting for your approval', { n: pending }));
  if (ran) parts.push(ran === 1 ? agT('agent.facts.chip_ran1', '1 action ran') : agTf('agent.facts.chip_ran', '{n} actions ran', { n: ran }));
  if (bad) parts.push(bad === 1 ? agT('agent.facts.chip_bad1', '1 refused or failed') : agTf('agent.facts.chip_bad', '{n} refused or failed', { n: bad }));
  if (!parts.length) parts.push(agTf('agent.facts.chip_other', '{n} actions, already decided', { n: a.length }));
  const title = a.map(f => f.outcome + ': ' + f.title + ' · ' + (f.host || '?') + (f.target ? ' → ' + f.target : '')).join('\n');
  return (pending ? '<a class="ag-fchip warn" href="#agent/approvals" title="' + agEsc(title) + '">' : '<span class="ag-fchip' + (bad ? ' bad' : '') + '" title="' + agEsc(title) + '">') +
    agEsc(parts.join(' · ')) + (pending ? '</a>' : '</span>');
}

function agRunGroups(detail) {
  const groups = [], byRun = {};
  for (const it of agChatItems(detail)) {
    const key = it.run_id || ('_' + groups.length);
    let g = byRun[key];
    if (!g || (it.kind === 'user' && g.user != null)) { g = { run_id: it.run_id, user: null, chips: [], texts: [] }; byRun[key] = g; groups.push(g); }
    if (it.kind === 'user') g.user = it.text;
    else { g.chips = g.chips.concat(it.chips); if (it.text) g.texts.push(it.text); }
  }
  return groups;
}

/** What the lab has, from the dashboard's own data: a reporting GPU, Docker. */
function agLabHas(what) {
  const hosts = (typeof FLEET !== 'undefined' && FLEET && FLEET.hosts) || [];
  const hh = typeof HH !== 'undefined' ? HH : null;
  if (what === 'gpu') return !!(hh && hh.now && hh.now.gpu && hh.now.gpu.available) || hosts.some(h => ((h.host && h.host.gpus) || []).length);
  if (what === 'docker') return !!(hh && hh.docker && (hh.docker.containers || []).length) || hosts.some(h => ((h.host && h.host.docker && h.host.docker.containers) || []).length);
  return true;
}

function agSuggestChips() {
  return AG_SUGGEST.filter(x => !x[2] || agLabHas(x[2])).slice(0, 4).map(([k, f]) => { const q = agT(k, f); return '<button type="button" class="ag-sug" data-sug="' + agEsc(q) + '">' + agEsc(q) + '</button>'; }).join('');
}

function agWireSuggestions(root) {
  root.querySelectorAll('[data-sug]').forEach(b => b.onclick = () => {
    const panel = b.closest('.ag-panel');
    const prefix = panel ? 'agw' : 'ag';
    const ta = document.getElementById(prefix + '-ta');
    const send = document.getElementById(prefix + '-send');
    if (!ta || !send) return;
    ta.value = b.dataset.sug;
    send.click();
  });
}

function agHeadNote() {
  const p = AG.policy;
  if (!p) return '';
  const on = p.hosts.filter(h => h.mode === 'active').length;
  const label = { look: agT('agent.preset.look', 'Look only'), ask: agT('agent.preset.ask', 'Ask me first'), small: agT('agent.preset.small', 'Handle the small stuff') }[p.preset] || agT('agent.preset.custom', 'Custom permissions');
  return label + ' · ' + (p.preset === 'look' || !on ? agTf('agent.on_n_read', 'reads {n} machines', { n: p.hosts.filter(h => h.mode !== 'off').length }) : agTf('agent.on_n', 'on {n} machines', { n: on }));
}

function agSetupCard() {
  return '<div class="ag-card ag-setupcard">' + agInviteHtml(true) + '</div>';
}

function agWireSetupCard(root) {
  root.querySelectorAll('[data-agsetup]').forEach(b => b.onclick = () => agSetupOpen());
}

function agRenderSettings(view) {
  const names = { models: agT('agent.v.models', 'Models & keys'), policy: agT('agent.v.permissions', 'Permissions') };
  view.innerHTML = '<div class="ag-subnav" role="tablist">' + AG_SETTINGS.map(s => '<button type="button" role="tab" data-setsec="' + s + '" aria-selected="' + (AG.setSec === s ? 'true' : 'false') + '" class="' + (AG.setSec === s ? 'on' : '') + '">' + agEsc(names[s]) + '</button>').join('') +
    '<span class="ag-sp"></span><button type="button" class="ag-btn sm" data-agsetup="1">' + agEsc(agT('agent.setup_again', 'Run the setup again')) + '</button></div><div id="ag-setbody"></div>';
  view.querySelectorAll('[data-setsec]').forEach(b => b.onclick = async () => { AG.setSec = b.dataset.setsec; await agLoadView(); agRenderSettings(view); });
  agWireSetupCard(view);
  const body = view.querySelector('#ag-setbody');
  if (AG.setSec === 'policy') agRenderPolicy(body); else agRenderModels(body);
}

function agEmpty(h, p, btn) {
  return '<div class="ag-card ag-empty"><h3>' + agEsc(h) + '</h3>' + (p ? '<p>' + agEsc(p) + '</p>' : '') + (btn || '') + '</div>';
}

function agHomeSoon() {
  clearTimeout(AG.homeTimer);
  AG.homeTimer = setTimeout(agHomeRefresh, 600);
}

function agHomeMount() {
  let el = document.getElementById('ag-home');
  if (el) return el;
  const bar = document.getElementById('mc-fleetbar');
  if (!bar) return null;
  el = document.createElement('section');
  el.id = 'ag-home';
  el.className = 'ag-root ag-home';
  el.setAttribute('aria-label', agT('agent.title_short', 'Agent'));
  bar.after(el);
  return el;
}

/** The dashboard holds "What's new" back while the invitation is up; show it now. */
function agWhatsNewLater() {
  try { if (typeof WN_DONE !== 'undefined' && typeof maybeWhatsNew === 'function') { WN_DONE = false; maybeWhatsNew(AG.status && AG.status.version); } } catch (e) { /* the notes are a nicety */ }
}

function agHomePref(v) {
  try {
    if (v === undefined) return localStorage.getItem('hlm.agent.home') || '';
    localStorage.setItem('hlm.agent.home', v);
  } catch (e) { /* private window: the card just shows again */ }
  return v || '';
}

async function agHomeRefresh() {
  const s = await agApi('/api/agent/setup' + (AG.setup && AG.setup.done ? '' : '?found=1'));
  if (!s.ok) return;
  AG.setup = s.body;
  if (AG.setup.done) {
    const [o, m] = await Promise.all([agApi('/api/agent/overview'), agLoadModelChoices()]);
    if (o.ok) AG.overview = o.body;
  }
  agRenderHome();
  agPaintBadge();
}

function agHomeState(setup, pref) {
  if (!setup || !setup.allowed) return 'none';
  if (setup.done) return 'ask';
  if (pref === 'hidden') return 'none';
  if (setup.dismissed || pref === 'folded') return 'strip';
  return 'invite';
}

function agInviteHtml(inTab) {
  const s = AG.setup || {};
  const step = (n, done, label, extra) => '<li class="' + (done ? 'done' : '') + '"><span class="ag-num">' + (done ? AG_CHECK : n) + '</span>' + agEsc(label) + (extra || '') + '</li>';
  const found = s.found ? '<span class="ag-found">' + agEsc(s.found === 1 ? agT('agent.found1', '1 found on your network') : agTf('agent.found_n', '{n} found on your network', { n: s.found })) + '</span>' : '';
  return '<div class="ag-invite"><div class="ag-inv-l"><div class="ag-titlerow">' + AG_ICON + '<h2>' + agEsc(agT('agent.invite_h', 'Your lab can answer you now')) + '</h2></div>' +
    '<p>' + agEsc(agT('agent.invite_p', 'Ask about your machines in plain words, or let routine clean-ups run on a schedule. It only does what you allow, per machine, and every step is logged.')) + '</p>' +
    '<ol class="ag-steps3">' + step(1, s.password_set, agT('agent.step.pw', 'Set a password')) + step(2, s.model_ready, agT('agent.step.model', 'Pick a model'), found) +
    step(3, s.enabled, agT('agent.step.perm', 'Choose what it may do')) + '</ol>' +
    '<div class="ag-row"><button type="button" class="ag-btn pri lg" data-agsetup="1">' + agEsc(agT('agent.setup_cta', 'Set it up · about a minute')) + '</button>' +
    (inTab ? '' : '<button type="button" class="ag-btn ghost" id="agh-later">' + agEsc(agT('agent.not_now', 'Not now')) + '</button>') + '</div></div>' +
    agExampleHtml() + '</div>';
}

/** The invitation's example: built from this fleet's own data, so it is never
 *  mistaken for someone else's lab; a plainly labelled sample when there's no data. */
function agExampleFromFleet(fleet) {
  const hosts = (fleet && fleet.hosts) || [];
  if (!hosts.length) return null;
  const on = hosts.filter(h => h.online || h.is_local), off = hosts.filter(h => !h.online && !h.is_local);
  let top = null;
  on.forEach(h => ((h.host && h.host.disks) || []).forEach(d => { if (!top || (d.pct || 0) > top.pct) top = { host: h.label || h.name, mount: d.mount, pct: d.pct || 0, free: d.total && d.used != null ? Math.round(d.total - d.used) : null }; }));
  if (!top) return null;
  let a = (top.pct >= 90 ? agT('agent.ex.full', 'One disk needs a clean-up.') : agT('agent.ex.ok', 'Nothing is close to full.')) + ' ' +
    agTf(top.free != null ? 'agent.ex.top_free' : 'agent.ex.top', 'The fullest is **{h} {m}** at **{p}%**' + (top.free != null ? ' ({f} GB free).' : '.'), { h: top.host, m: top.mount, p: top.pct, f: top.free });
  if (off.length) a += ' ' + (off.length === 1 ? agTf('agent.ex.off1', '{h} is offline, so I couldn\'t check it.', { h: off[0].label || off[0].name }) : agTf('agent.ex.offn', '{n} machines are offline, so I couldn\'t check them.', { n: off.length }));
  return { a, w: (on.length === 1 ? agT('agent.work.machine1', '1 machine') : agTf('agent.work.machines', '{n} machines', { n: on.length })) };
}

function agExampleHtml() {
  const ex = agExampleFromFleet(typeof FLEET !== 'undefined' ? FLEET : null);
  return '<div class="ag-inv-r" aria-label="' + agEsc(agT('agent.example', 'Example')) + '"><div class="ag-kicker">' +
    agEsc(ex ? agT('agent.example_yours', 'Example, from your fleet\'s data') : agT('agent.example_sample', 'Example (a sample lab)')) + '</div>' +
    '<div class="ag-msg u">' + agEsc(agT('agent.example_q', 'How are my disks?')) + '</div>' +
    '<div class="ag-exa">' + agMarkdown(ex ? ex.a : agT('agent.example_a', 'Everything is healthy. The fullest is **vader /** at **67%** (291 GB free). ardi **/data** is at 72% and growing about 1 GB a day, so it is the one to watch. Two machines are offline, so I couldn\'t check them.')) + '</div>' +
    '<div class="ag-work static">' + AG_ICON_SM + '<span>' + agEsc(ex ? agTf('agent.work.checked', 'Checked {what}', { what: ex.w + ' · ' + agT('agent.work.lookup1', '1 look-up') }) : agT('agent.example_w', 'Checked 4 machines · 1 look-up · 6 s')) + '</span></div></div>';
}

function agRenderHome() {
  const el = agHomeMount();
  if (!el) return;
  const state = agHomeState(AG.setup, agHomePref());
  el.dataset.state = state;
  if (state === 'none') { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  if (state === 'invite') {
    el.innerHTML = '<div class="ag-homecard">' + agInviteHtml(false) + '</div>';
    el.querySelector('#agh-later').onclick = async () => {
      if (AG.setup.password_set) { const r = await agApi('/api/agent/setup/dismiss', 'POST', { dismissed: true }); if (r.ok) AG.setup = r.body; }
      agHomePref('folded');
      agRenderHome();
      agWhatsNewLater();
    };
  } else if (state === 'strip') {
    el.innerHTML = '<div class="ag-strip">' + AG_ICON_SM + '<span>' + agEsc(agT('agent.strip', 'Your lab can answer questions — set up the agent in about a minute.')) + '</span><span class="ag-sp"></span>' +
      '<button type="button" class="ag-btn sm" data-agsetup="1">' + agEsc(agT('agent.setup_go', 'Set it up')) + '</button>' +
      '<button type="button" class="ag-x" id="agh-hide" aria-label="' + agEsc(agT('agent.hide', 'Hide')) + '">✕</button></div>';
    el.querySelector('#agh-hide').onclick = () => { agHomePref('hidden'); agRenderHome(); };
  } else {
    el.innerHTML = agAskCardHtml();
    agWireAskCard(el);
  }
  el.querySelectorAll('[data-agsetup]').forEach(b => b.onclick = () => agSetupOpen());
}

function agModelLine(o) {
  const m = (o && o.model) || {};
  if (!m.model) return '';
  return m.model + (m.provider ? ' · ' + m.provider : '') + ' · ' + (m.local ? agT('agent.local_free', 'local · free') : agT('agent.cloud', 'cloud'));
}

function agAskCardHtml() {
  const o = AG.overview || { waiting: [], lately: [], pending_count: 0 };
  const dot = st => st === 'done' ? 'ok' : st === 'waiting_approval' ? 'warn' : st === 'failed' || st === 'refused' ? 'crit' : 'info';
  const waiting = o.waiting.length ? o.waiting.map(a => '<div class="ag-wait"><div class="ag-grow"><div class="t">' + agEsc(agT('agent.act.' + (a.action || ''), a.title)) +
      (a.params && Object.keys(a.params).length ? ' ' + agEsc(Object.values(a.params).join(' ')) : '') + ' · ' + agEsc(a.host) + '</div>' +
      '<div class="s">' + agEsc((a.task ? agTf('agent.from_task', 'From “{t}”', { t: a.task }) + ' · ' : '') + agWhen(a.created_at) + ' · ' + ({ low: agT('agent.risk.low', 'low risk'), medium: agT('agent.risk.medium', 'medium risk'), high: agT('agent.risk.high', 'high risk') }[a.risk] || a.risk)) + '</div></div>' +
      '<a class="ag-btn sm outline" href="#agent/approvals">' + agEsc(agT('agent.review', 'Review')) + '</a></div>').join('') +
      (o.pending_count > o.waiting.length ? '<a class="ag-small" href="#agent/approvals">' + agEsc(agTf('agent.more_waiting', '+{n} more', { n: o.pending_count - o.waiting.length })) + '</a>' : '')
    : '<p class="ag-mut ag-small">' + agEsc(agT('agent.nothing_waits', 'Nothing waits for you.')) + '</p>';
  const lately = o.lately.length ? o.lately.map(r => '<a class="ag-late" href="#agent/run/' + agEsc(r.run_id) + '"><span class="ag-dot ' + dot(r.status) + '"></span><span class="ag-grow">' +
      (r.task ? '<b>' + agEsc(r.task) + ':</b> ' : '') + agEsc(r.line || agStatusLabel(r.status)) + '</span><span class="ag-mono ag-mut">' + agEsc(agWhen(r.at)) + '</span></a>').join('')
    : '<p class="ag-mut ag-small">' + agEsc(o.next && o.next.length ? agTf('agent.next_first', 'No routine has run yet. Next: {t}, {when}.', { t: o.next[0].task, when: agWhen(o.next[0].at) })
      : o.routines ? agT('agent.no_lately', 'No routine ran in the last day and a half.') : agT('agent.no_routines', 'No routines yet — switch one on under Routines.')) + '</p>';
  return '<div class="ag-homecard ag-ask"><div class="ag-ask-l"><div class="ag-titlerow">' + AG_ICON + '<h2>' + agEsc(agT('agent.hello', 'Ask your lab')) + '</h2>' +
    '<span class="ag-mut ag-small">' + agEsc(agModelLine(o)) + '</span></div>' +
    '<form class="ag-askform" id="agh-form"><label class="ag-vh" for="agh-q">' + agEsc(agT('agent.hello', 'Ask your lab')) + '</label>' +
    '<input id="agh-q" autocomplete="off" placeholder="' + agEsc(agT('agent.home_ph', 'How are my disks? What is using the GPU? Is anything wrong?')) + '">' +
    '<button class="ag-btn pri lg" type="submit">' + agEsc(agT('agent.ask', 'Ask')) + '</button></form>' +
    '<div class="ag-sugs">' + agSuggestChips() + '</div></div>' +
    '<div class="ag-ask-r"><div class="ag-kicker">' + agEsc(agT('agent.waiting_you', 'Waiting for you')) + '</div>' + waiting +
    '<div class="ag-kicker" style="margin-top:8px">' + agEsc(agT('agent.lately', 'Lately')) + '</div>' + lately +
    '<a class="ag-small" href="#agent">' + agEsc(agT('agent.open_agent', 'Open the agent →')) + '</a></div></div>';
}

function agWireAskCard(el) {
  const form = el.querySelector('#agh-form');
  form.onsubmit = e => { e.preventDefault(); const q = el.querySelector('#agh-q').value.trim(); if (q) agAskInWidget(q); };
  el.querySelectorAll('[data-sug]').forEach(b => b.onclick = () => agAskInWidget(b.dataset.sug));
}

async function agAskInWidget(text) {
  AG.widgetConv = null; AG.widgetDetail = null;
  await agOpenWidget();
  const ta = document.getElementById('agw-ta'), send = document.getElementById('agw-send');
  if (!ta || !send) return;
  ta.value = text;
  send.click();
}

function agPwScore(pw) {
  pw = String(pw || '');
  if (pw.length < 10) return pw.length ? 1 : 0;
  // "aaaaaaaaaa" is long but guessable: few different characters weigh it down
  if (new Set(pw.toLowerCase()).size < 6) return 1;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter(r => r.test(pw)).length;
  return Math.min(4, 2 + (pw.length >= 14 ? 1 : 0) + (classes >= 3 ? 1 : 0));
}

function agPwLabel(score) {
  return [agT('agent.pw.empty', 'At least 10 characters'), agT('agent.pw.weak', 'Too easy to guess — at least 10 characters, with some variety'),
    agT('agent.pw.ok', 'Good enough'), agT('agent.pw.good', 'Strong enough'), agT('agent.pw.great', 'Strong')][score];
}

function agPickModel(models, current) {
  const ok = (models || []).filter(m => m.tools === 'yes');
  if (current && ok.some(m => m.provider_id + '|' + m.model === current)) return current;
  const rec = ok.find(m => m.recommended) || ok[0];
  return rec ? rec.provider_id + '|' + rec.model : '';
}

async function agSetupOpen() {
  const s = await agApi('/api/agent/setup');
  AG.setup = s.ok ? s.body : (AG.setup || { password_set: false });
  AG.wizFocused = false;
  const W = AG.wiz = { step: !AG.setup.password_set ? 1 : !AG.setup.model_ready ? 2 : 3, models: null, probing: {}, choice: '',
    preset: AG.setup.preset || 'ask', starters: null, picked: null, busy: false, err: '' };
  if (AG.setup.model_ready && AG.status && AG.status.default) W.choice = AG.status.default.provider_id + '|' + AG.status.default.model;
  let scrim = document.querySelector('.ag-wizard');
  if (!scrim) {
    scrim = document.createElement('div');
    scrim.className = 'ag-scrim ag-root ag-wizard';
    document.body.appendChild(scrim);
    scrim.addEventListener('keydown', e => { if (e.key === 'Escape') agSetupClose(); });
  }
  agSetupRender();
  if (W.step === 2) agSetupModels();
  if (W.step === 3) agSetupStarters();
}

function agSetupClose() {
  const s = document.querySelector('.ag-wizard'); if (s) s.remove();
  AG.wiz = null;
  agHomeRefresh();
  if (TAB === 'agent') agRender();
}

function agSetupRender() {
  const scrim = document.querySelector('.ag-wizard'), W = AG.wiz;
  if (!scrim || !W) return;
  const names = [agT('agent.wz.secure', 'Secure'), agT('agent.wz.model', 'Model'), agT('agent.wz.perm', 'Permissions')];
  const stepper = '<div class="ag-stepper">' + names.map((n, i) => '<span class="' + (W.step > i + 1 ? 'done' : W.step === i + 1 ? 'cur' : '') + '"><span class="ag-num">' + (W.step > i + 1 ? AG_CHECK : i + 1) + '</span>' + agEsc(n) + '</span>').join('<span class="ag-line"></span>') + '</div>';
  let body = '', foot = '';
  const err = '<div class="ag-err" role="alert">' + agEsc(W.err || '') + '</div>';
  if (W.step === 1) {
    body = '<h2>' + agEsc(agT('agent.wz.pw_h', 'First, protect the dashboard')) + '</h2><p class="ag-lead">' + agEsc(agT('agent.wz.pw_p', 'The agent can change things on your machines, so the dashboard needs a password before it switches on. You\'ll be asked for it once per browser.')) + '</p>' +
      '<div class="ag-fld"><label for="agz-pw">' + agEsc(agT('agent.wz.pw', 'Password')) + '</label><input id="agz-pw" type="password" autocomplete="new-password">' +
      '<div class="ag-meter" aria-hidden="true"><span></span><span></span><span></span><span></span></div><span class="ag-small ag-mut" id="agz-pwlabel" aria-live="polite">' + agEsc(agPwLabel(0)) + '</span></div>' +
      '<div class="ag-fld"><label for="agz-pw2">' + agEsc(agT('agent.wz.pw2', 'Type it again')) + '</label><input id="agz-pw2" type="password" autocomplete="new-password"></div>' + err;
    foot = '<span class="ag-small ag-mut ag-grow">' + agEsc(agT('agent.wz.forgot', 'Forgot it later? One command resets it.')) + ' <a href="https://sikamikanikobg.github.io/homelab-monitor/security/" target="_blank" rel="noopener">' + agEsc(agT('agent.wz.how', 'How')) + '</a></span>' +
      '<button type="button" class="ag-btn ghost" data-wz="cancel">' + agEsc(agT('agent.cancel', 'Cancel')) + '</button><button type="button" class="ag-btn pri" data-wz="pw">' + agEsc(agT('agent.wz.continue', 'Continue')) + '</button>';
  } else if (W.step === 2) {
    const ms = W.models;
    let list;
    if (ms === null) list = '<div class="ag-loading">' + agEsc(agT('agent.wz.looking', 'Looking for models on your network…')) + '</div>';
    else if (!ms.length) list = '<div class="ag-card ag-empty"><h3>' + agEsc(agT('agent.wz.none_h', 'No model server found')) + '</h3><p>' + agEsc(agT('agent.wz.none_p', 'Start Ollama, vLLM, llama.cpp or LM Studio on a machine the hub watches, or use a cloud model with your own key below.')) + '</p></div>';
    else list = '<div class="ag-radios" role="radiogroup" aria-label="' + agEsc(agT('agent.model', 'Model')) + '">' + ms.slice(0, 12).map(m => {
      const v = m.provider_id + '|' + m.model, testing = W.probing[v] === 'run' || (m.tools === 'unknown' && W.probing[v] !== 'done');
      const ok = m.tools === 'yes', no = !testing && !ok;
      const win = m.context_window ? agTf('agent.wz.ctx', '{n}k context', { n: Math.round(m.context_window / 1024) }) : '';
      const sub = [m.provider, win, ok ? (m.multi_step === 'yes' ? agT('agent.wz.multi', 'handles multi-step questions') : agT('agent.wz.single', 'fine for single questions')) : no ? agT('agent.wz.cant_p', 'it answered without calling tools, so it can\'t look at your lab') : ''].filter(Boolean).join(' · ');
      return '<label class="ag-radio' + (W.choice === v ? ' on' : '') + (no ? ' off' : '') + '"><input type="radio" name="agz-m" value="' + agEsc(v) + '"' + (W.choice === v ? ' checked' : '') + (ok ? '' : ' disabled') + '>' +
        '<span class="ag-grow"><span class="ag-rt"><b>' + agEsc(m.model) + '</b>' + (m.recommended ? '<span class="ag-tag">' + agEsc(agT('agent.wz.recommended', 'RECOMMENDED')) + '</span>' : '') + '</span><span class="ag-rs">' + agEsc(sub) + '</span></span>' +
        '<span class="ag-state ' + (testing ? '' : ok ? 'ok' : 'no') + '">' + agEsc(testing ? agT('agent.m.testing', 'Testing…') : ok ? agT('agent.wz.works', 'Works with the agent') : agT('agent.wz.cant', 'Can\'t be used')) + '</span></label>';
    }).join('') + '</div>';
    const cloud = (AG.presets || []).filter(p => !p.local);
    body = '<h2>' + agEsc(agT('agent.wz.m_h', 'Pick the model that does the thinking')) + '</h2><p class="ag-lead">' + agEsc(agT('agent.wz.m_p', 'Found on your network and tested just now. Local models are free and nothing leaves your lab.')) + '</p>' + list +
      '<details class="ag-cloud"' + (ms && !ms.length ? ' open' : '') + '><summary>' + agEsc(agT('agent.wz.cloud', 'Use a cloud model with your own key instead')) + '</summary>' +
      '<p class="ag-small ag-mut">' + agEsc(agT('agent.wz.cloud_p', 'OpenAI, Anthropic, Groq, Mistral, OpenRouter and more. Your key stays on the server.')) + '</p>' +
      '<div class="ag-row"><label class="ag-vh" for="agz-cp">' + agEsc(agT('agent.wz.provider', 'Provider')) + '</label><select id="agz-cp">' + cloud.map(p => '<option value="' + agEsc(p.id) + '">' + agEsc(p.label) + '</option>').join('') + '</select>' +
      '<label class="ag-vh" for="agz-ck">' + agEsc(agT('agent.m.paste_key', 'Paste API key')) + '</label><input id="agz-ck" type="password" autocomplete="off" placeholder="' + agEsc(agT('agent.m.paste_key', 'Paste API key')) + '">' +
      '<button type="button" class="ag-btn" data-wz="cloud">' + agEsc(agT('agent.m.add', 'Add')) + '</button></div></details>' +
      '<details class="ag-byurl"><summary>' + agEsc(agT('agent.wz.byurl', 'My model server isn\'t listed')) + '</summary>' +
      '<p class="ag-small ag-mut">' + agEsc(agT('agent.wz.byurl_p', 'Give its address, like http://192.168.1.20:11434 for Ollama or http://192.168.1.20:8000/v1 for vLLM.')) + '</p>' +
      '<div class="ag-row"><label class="ag-vh" for="agz-lp">' + agEsc(agT('agent.wz.server', 'Server')) + '</label><select id="agz-lp">' + (AG.presets || []).filter(p => p.local).map(p => '<option value="' + agEsc(p.id) + '">' + agEsc(p.label) + '</option>').join('') + '</select>' +
      '<label class="ag-vh" for="agz-lu">' + agEsc(agT('agent.wz.address', 'Address')) + '</label><input id="agz-lu" autocomplete="off" placeholder="http://host:port/v1">' +
      '<button type="button" class="ag-btn" data-wz="byurl">' + agEsc(agT('agent.m.add', 'Add')) + '</button></div></details>' + err;
    foot = '<button type="button" class="ag-btn ghost" data-wz="cancel">' + agEsc(agT('agent.cancel', 'Cancel')) + '</button><span class="ag-sp"></span>' +
      '<button type="button" class="ag-btn pri" data-wz="model"' + (W.choice ? '' : ' disabled') + '>' + agEsc(agT('agent.wz.continue', 'Continue')) + '</button>';
  } else {
    const n = (AG.policy && AG.policy.hosts) ? AG.policy.hosts.length : null;
    const presets = [['look', agT('agent.preset.look', 'Look only'), agT('agent.preset.look_p', 'Answers questions and writes reports. Never changes anything.')],
      ['ask', agT('agent.preset.ask', 'Ask me first'), agT('agent.preset.ask_p', 'It can suggest restarts and clean-ups. Nothing runs until you tap Approve.')],
      ['small', agT('agent.preset.small', 'Handle the small stuff'), agT('agent.preset.small_p', 'Safe tidy-ups, like removing unused images or restarting a crashed container, run by themselves, at most 3 times a day per target. Anything bigger still waits for you.')]];
    const st = W.starters;
    body = '<h2>' + agEsc(agT('agent.wz.p_h', 'How much may it do on its own?')) + '</h2><p class="ag-lead">' + agEsc(n ? agTf('agent.wz.p_p', 'Applies to all {n} machines, and to machines you add later. You can change it per machine and per action in Settings.', { n }) : agT('agent.wz.p_p0', 'Applies to every machine, and to machines you add later. You can change it per machine and per action in Settings.')) + '</p>' +
      '<div class="ag-radios" role="radiogroup" aria-label="' + agEsc(agT('agent.v.permissions', 'Permissions')) + '">' + presets.map(([id, t, d]) =>
        '<label class="ag-radio' + (W.preset === id ? ' on' : '') + '"><input type="radio" name="agz-p" value="' + id + '"' + (W.preset === id ? ' checked' : '') + '><span class="ag-grow"><span class="ag-rt"><b>' + agEsc(t) + '</b>' +
        (id === 'ask' ? '<span class="ag-tag">' + agEsc(agT('agent.wz.recommended', 'RECOMMENDED')) + '</span>' : '') + '</span><span class="ag-rs">' + agEsc(d) + '</span></span></label>').join('') + '</div>' +
      (st && st.length ? '<div class="ag-fld"><span class="lbl">' + agEsc(agT('agent.wz.routines', 'Switch on a ready-made routine')) + ' <span class="ag-mut">' + agEsc(agT('agent.wz.optional', '(optional)')) + '</span></span><div class="ag-checks">' +
        st.map(t => '<label><input type="checkbox" data-starter="' + agEsc(t.id) + '"' + (W.picked.indexOf(t.id) >= 0 ? ' checked' : '') + '> ' + agEsc(agT('agent.tpl.' + t.id + '.name', t.name)) + ' · ' + agEsc(agCronLabel(t.default_cron)) + '</label>').join('') + '</div></div>' : '') + err;
    foot = '<button type="button" class="ag-btn ghost" data-wz="back">' + agEsc(agT('agent.wz.back', 'Back')) + '</button><span class="ag-sp"></span>' +
      '<button type="button" class="ag-btn pri" data-wz="finish"' + (W.busy ? ' disabled' : '') + '>' + agEsc(W.busy ? agT('agent.wz.finishing', 'Switching on…') : agT('agent.wz.finish', 'Finish and ask something')) + '</button>';
  }
  // A repaint (a test result arriving) must not lose what is being typed or opened.
  const focusId = document.activeElement && scrim.contains(document.activeElement) ? document.activeElement.id : null;
  const kept = {};
  scrim.querySelectorAll('input[id],select[id]').forEach(i => { if (i.type !== 'radio' && i.type !== 'checkbox') kept[i.id] = i.value; });
  const openDetails = [...scrim.querySelectorAll('details[open]')].map(d => d.className);
  scrim.innerHTML = '<div class="ag-dialog ag-wiz" role="dialog" aria-modal="true" aria-label="' + agEsc(agT('agent.wz.aria', 'Set up the agent')) + '">' + stepper + '<div class="ag-wbody">' + body + '</div><div class="ag-wfoot">' + foot + '</div></div>';
  Object.entries(kept).forEach(([id, v]) => { const i = scrim.querySelector('#' + id); if (i && v) i.value = v; });
  openDetails.forEach(c => { const d = c && scrim.querySelector('details.' + c.split(' ')[0]); if (d) d.open = true; });
  agSetupWire(scrim);
  const f = focusId && scrim.querySelector('#' + focusId);
  if (f) f.focus(); else if (!AG.wizFocused) { const first = scrim.querySelector('input:not([disabled]),button.pri'); if (first) first.focus(); }
  AG.wizFocused = true;
}

function agSetupWire(scrim) {
  const W = AG.wiz;
  const $ = s => scrim.querySelector(s);
  scrim.querySelectorAll('[data-wz="cancel"]').forEach(b => b.onclick = agSetupClose);
  const pw = $('#agz-pw');
  if (pw) {
    const meter = () => {
      const sc = agPwScore(pw.value);
      scrim.querySelectorAll('.ag-meter span').forEach((s, i) => s.className = i < sc ? 'on s' + sc : '');
      $('#agz-pwlabel').textContent = agPwLabel(sc);
    };
    const clearErr = () => { if (W.err) { W.err = ''; const e = scrim.querySelector('.ag-err'); if (e) e.textContent = ''; } };
    pw.oninput = () => { meter(); clearErr(); };
    $('#agz-pw2').oninput = clearErr;
    meter();
    const go = $('[data-wz="pw"]');
    const submit = async () => {
      const a = pw.value, b = $('#agz-pw2').value;
      if (agPwScore(a) < 2) { W.err = agT('agent.pw.weak', 'Too easy to guess — at least 10 characters, with some variety'); agSetupRender(); return; }
      if (a !== b) { W.err = agT('agent.wz.mismatch', 'The two passwords are different.'); agSetupRender(); return; }
      go.disabled = true;
      const r = await agApi('/api/auth/setup', 'POST', { password: a });
      if (!r.ok) { W.err = r.body.message || agT('agent.failed', 'Something went wrong.'); go.disabled = false; agSetupRender(); return; }
      if (typeof HLM_AUTH !== 'undefined') { HLM_AUTH.forget(); await HLM_AUTH.status(true); }
      await agRefreshStatus();
      W.err = ''; W.step = 2; AG.wizFocused = false;
      scrim.querySelectorAll('input[type=password]').forEach(i => { i.value = ''; });
      agSetupRender(); agSetupModels();
    };
    go.onclick = submit;
    $('#agz-pw2').onkeydown = e => { if (e.key === 'Enter') submit(); };
  }
  scrim.querySelectorAll('input[name="agz-m"]').forEach(r => r.onchange = () => { W.choice = r.value; agSetupRender(); });
  const cloudBtn = $('[data-wz="cloud"]');
  if (cloudBtn) cloudBtn.onclick = async () => {
    const key = $('#agz-ck').value.trim();
    if (!key) { W.err = agT('agent.wz.need_key', 'Paste the key first.'); agSetupRender(); return; }
    cloudBtn.disabled = true;
    const r = await agApi('/api/agent/providers', 'POST', { preset: $('#agz-cp').value, api_key: key });
    if (!r.ok) { W.err = r.body.message || agT('agent.failed', 'Something went wrong.'); agSetupRender(); return; }
    const t = await agApi('/api/agent/providers/' + r.body.provider.id + '/test', 'POST', {});
    W.err = t.body.ok ? '' : agTestText(t.body);
    agSetupModels(true);
  };
  const byUrl = $('[data-wz="byurl"]');
  if (byUrl) byUrl.onclick = async () => {
    const url = $('#agz-lu').value.trim();
    if (!url) { W.err = agT('agent.wz.need_url', 'Type the server\'s address first.'); agSetupRender(); return; }
    byUrl.disabled = true;
    const r = await agApi('/api/agent/providers', 'POST', { preset: $('#agz-lp').value, base_url: url });
    if (!r.ok) { W.err = r.body.message || agT('agent.failed', 'Something went wrong.'); agSetupRender(); return; }
    const t = await agApi('/api/agent/providers/' + r.body.provider.id + '/test', 'POST', {});
    W.err = t.body.ok ? '' : agTestText(t.body);
    $('#agz-lu').value = '';
    agSetupModels(true);
  };
  const next = $('[data-wz="model"]');
  if (next) next.onclick = () => { W.step = 3; W.err = ''; AG.wizFocused = false; agSetupRender(); agSetupStarters(); };
  scrim.querySelectorAll('input[name="agz-p"]').forEach(r => r.onchange = () => { W.preset = r.value; agSetupRender(); });
  scrim.querySelectorAll('[data-starter]').forEach(c => c.onchange = () => {
    W.picked = W.picked.filter(x => x !== c.dataset.starter); if (c.checked) W.picked.push(c.dataset.starter);
  });
  const back = $('[data-wz="back"]');
  if (back) back.onclick = () => { W.step = 2; W.err = ''; agSetupRender(); if (W.models === null) agSetupModels(); };
  const fin = $('[data-wz="finish"]');
  if (fin) fin.onclick = async () => {
    const [pid, model] = String(W.choice || '').split('|');
    W.busy = true; agSetupRender();
    const r = await agApi('/api/agent/setup/finish', 'POST', { provider_id: pid || undefined, model: model || undefined, preset: W.preset, templates: W.picked });
    W.busy = false;
    if (!r.ok) { W.err = r.body.message || agT('agent.failed', 'Something went wrong.'); if (r.body.error === 'model_not_ready') W.step = 2; agSetupRender(); return; }
    agSetupClose();
    await agRefreshStatus();
    agWhatsNewLater();
    AG.view = 'chat'; AG.convId = null; AG.detail = null;
    if (TAB !== 'agent') showTab('agent'); else agRender();
  };
}

/** Step 2: find model servers, add them, then test every untested model in the
 *  background — one at a time per server, so a model server never loads several. */
async function agSetupModels(again) {
  const W = AG.wiz;
  if (!W) return;
  const pr = await agApi('/api/agent/presets');
  if (pr.ok) AG.presets = pr.body.presets || [];
  const r = await agApi('/api/agent/setup/models', again ? 'GET' : 'POST', again ? undefined : {});
  if (AG.wiz !== W) return;
  W.models = r.ok ? r.body.models || [] : [];
  W.choice = agPickModel(W.models, W.choice);
  agSetupRender();
  const byProv = {};
  W.models.filter(m => m.tools === 'unknown' && !m.probed).slice(0, 10).forEach(m => { (byProv[m.provider_id] = byProv[m.provider_id] || []).push(m); });
  await Promise.all(Object.values(byProv).map(async list => {
    for (const m of list) {
      const v = m.provider_id + '|' + m.model;
      W.probing[v] = 'run'; agSetupRender();
      const p = await agApi('/api/agent/models/probe', 'POST', { provider_id: m.provider_id, model: m.model });
      if (AG.wiz !== W) return;
      W.probing[v] = 'done';
      if (p.ok) { m.tools = p.body.tools; m.multi_step = p.body.multi_step; }
      agSetupRender();
    }
  }));
  if (AG.wiz !== W) return;
  const again2 = await agApi('/api/agent/setup/models');
  if (again2.ok) { W.models = again2.body.models || []; W.choice = agPickModel(W.models, W.choice); }
  agSetupRender();
}

async function agSetupStarters() {
  const W = AG.wiz;
  const [t] = await Promise.all([agApi('/api/agent/templates'), agLoadPolicy()]);
  if (AG.wiz !== W) return;
  W.starters = t.ok ? (t.body.templates || []).filter(x => x.starter && (x.eligible_hosts || []).length) : [];
  if (W.picked === null) W.picked = W.starters.slice(0, 1).map(x => x.id);
  agSetupRender();
}

agBoot();
