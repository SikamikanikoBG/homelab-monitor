"""Tables for the AI agent. Created on first use (never at boot of a disabled
install) with CREATE IF NOT EXISTS, so they are additive: an older image that
doesn't know them simply ignores them.

Audit design (04-plan.md): `agent_steps` is append-only, enforced by triggers.
UPDATE is always refused; DELETE only below the cutoff the retention job writes
into `agent_retention`. Every action is also printed to stdout as one JSON line
(see audit.py), so `docker logs` holds a copy outside the database.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_settings(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS agent_providers(
  id TEXT PRIMARY KEY, preset TEXT NOT NULL, adapter TEXT NOT NULL, label TEXT NOT NULL,
  base_url TEXT NOT NULL,
  api_key TEXT,               -- never selected by any API serializer; scrubbed from backups
  key_ref TEXT,               -- 'env:NAME' or 'file:/run/secrets/x'; only the reference is stored
  key_last4 TEXT,
  extra_json TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'manual',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_test_at INTEGER, last_test_json TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS agent_models(
  provider_id TEXT NOT NULL, model TEXT NOT NULL, context_window INTEGER,
  tools TEXT NOT NULL DEFAULT 'unknown', multi_step TEXT NOT NULL DEFAULT 'unknown',
  probe_detail TEXT, probed_at INTEGER,
  price_in REAL NOT NULL DEFAULT 0, price_out REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(provider_id, model));

CREATE TABLE IF NOT EXISTS agent_conversations(
  id TEXT PRIMARY KEY, title TEXT, origin TEXT NOT NULL, task_id TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS agent_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, run_id TEXT,
  role TEXT NOT NULL, content TEXT, tool_calls_json TEXT, tool_call_id TEXT, tool_name TEXT,
  provider_meta_json TEXT, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_agent_msg_conv ON agent_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS agent_runs(
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, trigger TEXT NOT NULL, task_id TEXT,
  scheduled_for INTEGER, attended INTEGER NOT NULL, mode TEXT NOT NULL,
  provider_id TEXT, model TEXT, status TEXT NOT NULL, status_reason TEXT,
  steps INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0, cost_micros INTEGER NOT NULL DEFAULT 0,
  context_json TEXT, hosts_json TEXT, actor TEXT,
  created_at INTEGER NOT NULL, started_at INTEGER, ended_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);

CREATE TABLE IF NOT EXISTS agent_steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,         -- model | tool | probe | action_intent | action_result | policy | approval | note
  tool TEXT, host TEXT, target TEXT, args_json TEXT, args_hash TEXT,
  ok INTEGER, result_bytes INTEGER, result_excerpt TEXT, result_hash TEXT,
  decision TEXT, decision_reason TEXT, approval_id TEXT, actor TEXT,
  duration_ms INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  UNIQUE(run_id, seq));
CREATE INDEX IF NOT EXISTS idx_agent_steps_host ON agent_steps(host, ts);
CREATE INDEX IF NOT EXISTS idx_agent_steps_kind ON agent_steps(kind, ts);

CREATE TABLE IF NOT EXISTS agent_retention(id INTEGER PRIMARY KEY CHECK(id=1), cutoff INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS agent_steps_no_update BEFORE UPDATE ON agent_steps
  BEGIN SELECT RAISE(ABORT, 'agent_steps is append-only'); END;
CREATE TRIGGER IF NOT EXISTS agent_steps_no_delete BEFORE DELETE ON agent_steps
  WHEN OLD.ts >= COALESCE((SELECT cutoff FROM agent_retention WHERE id=1), 0)
  BEGIN SELECT RAISE(ABORT, 'agent_steps is append-only'); END;

CREATE TABLE IF NOT EXISTS agent_admin_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor TEXT, kind TEXT NOT NULL,
  subject TEXT, detail_json TEXT);

CREATE TABLE IF NOT EXISTS agent_approvals(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, call_id TEXT,
  action TEXT NOT NULL, host TEXT NOT NULL, params_json TEXT NOT NULL, params_hash TEXT NOT NULL,
  risk TEXT NOT NULL, attended INTEGER NOT NULL DEFAULT 0,
  rationale TEXT,             -- model-written, always shown labelled as the agent's reasoning
  bind_json TEXT,             -- facts the approval is bound to (e.g. a model digest)
  catalogue_version INTEGER NOT NULL,
  status TEXT NOT NULL,       -- pending|approved|rejected|expired|executing|done|failed|refused
  seen_count INTEGER NOT NULL DEFAULT 1, last_proposed_at INTEGER,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  decided_at INTEGER, decided_by TEXT, executed_at INTEGER, result_excerpt TEXT);
CREATE INDEX IF NOT EXISTS idx_agent_appr_status ON agent_approvals(status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_appr_pending
  ON agent_approvals(host, action, params_hash) WHERE status='pending';

CREATE TABLE IF NOT EXISTS agent_host_policy(host TEXT PRIMARY KEY, mode TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS agent_action_rules(
  host TEXT NOT NULL, action TEXT NOT NULL, rule TEXT NOT NULL,
  unattended_unlocked INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
  PRIMARY KEY(host, action));
CREATE TABLE IF NOT EXISTS agent_protected(
  host TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, reason TEXT,
  created_at INTEGER NOT NULL, PRIMARY KEY(host, kind, name));

CREATE TABLE IF NOT EXISTS agent_tasks(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, template_id TEXT, template_version INTEGER,
  prompt TEXT NOT NULL, tools_json TEXT NOT NULL, actions_json TEXT NOT NULL,
  hosts_json TEXT NOT NULL, cron TEXT NOT NULL, tz TEXT NOT NULL, mode TEXT NOT NULL,
  provider_id TEXT, model TEXT, delivery_json TEXT NOT NULL, budget_json TEXT NOT NULL,
  catch_up TEXT NOT NULL DEFAULT 'skip', enabled INTEGER NOT NULL DEFAULT 1,
  next_fire INTEGER, last_slot_local TEXT, warning TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS agent_task_fires(
  task_id TEXT NOT NULL, scheduled_for INTEGER NOT NULL, run_id TEXT, outcome TEXT,
  created_at INTEGER NOT NULL, PRIMARY KEY(task_id, scheduled_for));
"""

# id -> the connection itself. Holding the object (not just its id) means a
# closed handle's id can't be reused by a new connection after a restore.
_ready = {}


def ensure(conn):
    if _ready.get(id(conn)) is conn:
        return
    conn.executescript(SCHEMA)
    conn.commit()
    _ready[id(conn)] = conn


def reset():
    _ready.clear()


def tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%' ORDER BY name")]


def exists(conn):
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='agent_settings'").fetchone())
    except sqlite3.Error:
        return False
