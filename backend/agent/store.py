"""All agent SQL, in one place. Each function opens `ports.db()` (the
dashboard's connection under its lock) for just its own statements — never
across a model call or an SSH command.

Provider keys: the `api_key` column is read by exactly one function,
`provider_secret()`. Every other provider query names its columns and leaves
the key out, so no serializer can leak it by accident.
"""
import contextlib
import hashlib
import json
import time
import uuid

from backend.agent import ports

TERMINAL = ("done", "failed", "cancelled", "interrupted", "budget_exhausted", "refused")
ACTIVE = ("queued", "running", "waiting_approval")


def new_id():
    return uuid.uuid4().hex


def now():
    return int(time.time())


def _dumps(v):
    return None if v is None else json.dumps(v, sort_keys=True, ensure_ascii=False)


def _loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def params_hash(action, host, params):
    raw = json.dumps([action, host, params], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _row(cur):
    rows = _rows(cur)
    return rows[0] if rows else None


# ── admin log ────────────────────────────────────────────────────────────────

def admin_log(kind, subject, detail, actor=None, conn=None):
    with _on(conn) as c:
        c.execute("INSERT INTO agent_admin_log(ts,actor,kind,subject,detail_json) VALUES(?,?,?,?,?)",
                  (now(), actor, kind, subject, _dumps(detail)))


def admin_log_list(limit=200):
    with ports.db() as c:
        return _rows(c.execute("SELECT id,ts,actor,kind,subject,detail_json FROM agent_admin_log "
                               "ORDER BY id DESC LIMIT ?", (limit,)))


# ── counts for the nav badge / status ────────────────────────────────────────

def counts():
    with ports.db() as c:
        pending = c.execute("SELECT COUNT(*) FROM agent_approvals WHERE status='pending' AND expires_at>?",
                            (now(),)).fetchone()[0]
        running = c.execute("SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running')").fetchone()[0]
    return {"pending_approvals": pending, "running": running}


# ── providers & models ───────────────────────────────────────────────────────

_PROVIDER_COLS = ("id,preset,adapter,label,base_url,key_ref,key_last4,extra_json,source,enabled,"
                  "last_test_at,last_test_json,created_at,updated_at,"
                  "(api_key IS NOT NULL AND api_key<>'') AS key_stored")


def provider_list():
    with ports.db() as c:
        return _rows(c.execute("SELECT %s FROM agent_providers ORDER BY created_at" % _PROVIDER_COLS))


def provider_get(pid):
    with ports.db() as c:
        return _row(c.execute("SELECT %s FROM agent_providers WHERE id=?" % _PROVIDER_COLS, (pid,)))


def provider_secret(pid):
    """The stored key, for the adapter only. Never returned by the API."""
    with ports.db() as c:
        r = c.execute("SELECT api_key FROM agent_providers WHERE id=?", (pid,)).fetchone()
    return r[0] if r else None


def provider_insert(row):
    t = now()
    with ports.db() as c:
        c.execute("INSERT INTO agent_providers(id,preset,adapter,label,base_url,api_key,key_ref,key_last4,"
                  "extra_json,source,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (row["id"], row["preset"], row["adapter"], row["label"], row["base_url"],
                   row.get("api_key"), row.get("key_ref"), row.get("key_last4"),
                   _dumps(row.get("extra") or {}), row.get("source") or "manual", 1, t, t))
        c.commit()


def provider_update(pid, fields):
    allowed = ("label", "base_url", "api_key", "key_ref", "key_last4", "extra_json", "enabled",
               "last_test_at", "last_test_json")
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return
    with ports.db() as c:
        c.execute("UPDATE agent_providers SET %s, updated_at=? WHERE id=?"
                  % ", ".join("%s=?" % k for k, _ in sets), [v for _, v in sets] + [now(), pid])
        c.commit()


def provider_delete(pid):
    with ports.db() as c:
        n = c.execute("DELETE FROM agent_providers WHERE id=?", (pid,)).rowcount
        c.execute("DELETE FROM agent_models WHERE provider_id=?", (pid,))
        c.execute("UPDATE agent_tasks SET warning='provider_removed' WHERE provider_id=?", (pid,))
        c.commit()
    return n


def model_upsert(pid, model, **fields):
    allowed = ("context_window", "tools", "multi_step", "probe_detail", "probed_at", "price_in", "price_out")
    with ports.db() as c:
        c.execute("INSERT OR IGNORE INTO agent_models(provider_id, model) VALUES(?,?)", (pid, model))
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if sets:
            c.execute("UPDATE agent_models SET %s WHERE provider_id=? AND model=?"
                      % ", ".join("%s=?" % k for k, _ in sets), [v for _, v in sets] + [pid, model])
        c.commit()


def model_list(pid=None):
    with ports.db() as c:
        if pid:
            return _rows(c.execute("SELECT * FROM agent_models WHERE provider_id=? ORDER BY model", (pid,)))
        return _rows(c.execute("SELECT * FROM agent_models ORDER BY provider_id, model"))


def model_get(pid, model):
    with ports.db() as c:
        return _row(c.execute("SELECT * FROM agent_models WHERE provider_id=? AND model=?", (pid, model)))


def provider_tasks_warn(pid, warning):
    with ports.db() as c:
        c.execute("UPDATE agent_tasks SET warning=? WHERE provider_id=?", (warning, pid))
        c.commit()


# ── conversations & messages ─────────────────────────────────────────────────

def conversation_create(title=None, origin="chat", task_id=None):
    cid, t = new_id(), now()
    with ports.db() as c:
        c.execute("INSERT INTO agent_conversations(id,title,origin,task_id,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?)", (cid, (title or "")[:200] or None, origin, task_id, t, t))
        c.commit()
    return cid


def conversation_get(cid):
    with ports.db() as c:
        return _row(c.execute("SELECT * FROM agent_conversations WHERE id=?", (cid,)))


def conversation_list(origin=None, limit=50, before=None, include_archived=False):
    q = ("SELECT c.*, (SELECT status FROM agent_runs r WHERE r.conversation_id=c.id "
         "ORDER BY r.created_at DESC LIMIT 1) AS last_status FROM agent_conversations c WHERE 1=1")
    args = []
    if origin:
        q += " AND c.origin=?"
        args.append(origin)
    if not include_archived:
        q += " AND c.archived=0"
    if before:
        q += " AND c.updated_at<?"
        args.append(int(before))
    q += " ORDER BY c.updated_at DESC LIMIT ?"
    args.append(int(limit))
    with ports.db() as c:
        return _rows(c.execute(q, args))


def conversation_update(cid, title=None, archived=None):
    with ports.db() as c:
        if title is not None:
            c.execute("UPDATE agent_conversations SET title=?, updated_at=? WHERE id=?",
                      (title[:200], now(), cid))
        if archived is not None:
            c.execute("UPDATE agent_conversations SET archived=? WHERE id=?", (1 if archived else 0, cid))
        c.commit()


def conversation_touch(cid, title_if_empty=None):
    with ports.db() as c:
        c.execute("UPDATE agent_conversations SET updated_at=?, title=COALESCE(title, ?) WHERE id=?",
                  (now(), (title_if_empty or "")[:80] or None, cid))
        c.commit()


def conversation_delete(cid):
    """Deletes the conversation and its messages. Runs and their audit steps stay
    (history is history); they keep pointing at the id."""
    with ports.db() as c:
        c.execute("DELETE FROM agent_messages WHERE conversation_id=?", (cid,))
        n = c.execute("DELETE FROM agent_conversations WHERE id=?", (cid,)).rowcount
        c.commit()
    return n


def message_add(cid, role, content=None, run_id=None, tool_calls=None, tool_call_id=None,
                tool_name=None, provider_meta=None):
    with ports.db() as c:
        cur = c.execute("INSERT INTO agent_messages(conversation_id,run_id,role,content,tool_calls_json,"
                        "tool_call_id,tool_name,provider_meta_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid, run_id, role, content, _dumps(tool_calls) if tool_calls else None,
                         tool_call_id, tool_name, _dumps(provider_meta) if provider_meta else None, now()))
        c.commit()
        return cur.lastrowid


def message_list(cid):
    with ports.db() as c:
        rows = _rows(c.execute("SELECT * FROM agent_messages WHERE conversation_id=? ORDER BY id", (cid,)))
    for r in rows:
        r["tool_calls"] = _loads(r.pop("tool_calls_json"), None)
        r["provider_meta"] = _loads(r.pop("provider_meta_json"), None)
    return rows


# ── runs ─────────────────────────────────────────────────────────────────────

def run_create(conversation_id, trigger, attended, mode, provider_id=None, model=None, task_id=None,
               scheduled_for=None, context=None, hosts=None, actor=None, status="queued"):
    rid = new_id()
    with ports.db() as c:
        c.execute("INSERT INTO agent_runs(id,conversation_id,trigger,task_id,scheduled_for,attended,mode,"
                  "provider_id,model,status,context_json,hosts_json,actor,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, conversation_id, trigger, task_id, scheduled_for, 1 if attended else 0, mode,
                   provider_id, model, status, _dumps(context), _dumps(hosts), actor, now()))
        c.commit()
    return rid


def run_get(rid):
    with ports.db() as c:
        r = _row(c.execute("SELECT * FROM agent_runs WHERE id=?", (rid,)))
    if r:
        r["context"] = _loads(r.pop("context_json"), None)
        r["hosts"] = _loads(r.pop("hosts_json"), None)
    return r


def run_update(rid, **fields):
    allowed = ("status", "status_reason", "steps", "prompt_tokens", "completion_tokens", "cost_micros",
               "started_at", "ended_at", "provider_id", "model")
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return
    with ports.db() as c:
        c.execute("UPDATE agent_runs SET %s WHERE id=?" % ", ".join("%s=?" % k for k, _ in sets),
                  [v for _, v in sets] + [rid])
        c.commit()


def run_set_status_if(rid, from_statuses, to_status, reason=None):
    """Conditional status change; True when this caller won the transition."""
    marks = ",".join("?" * len(from_statuses))
    fields = "status=?, status_reason=COALESCE(?, status_reason)"
    args = [to_status, reason]
    if to_status in TERMINAL:
        fields += ", ended_at=?"
        args.append(now())
    with ports.db() as c:
        n = c.execute("UPDATE agent_runs SET %s WHERE id=? AND status IN (%s)" % (fields, marks),
                      args + [rid] + list(from_statuses)).rowcount
        c.commit()
    return n == 1


def run_add_usage(rid, prompt_tokens, completion_tokens, cost_micros):
    with ports.db() as c:
        c.execute("UPDATE agent_runs SET prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+?, "
                  "cost_micros=cost_micros+? WHERE id=?",
                  (int(prompt_tokens or 0), int(completion_tokens or 0), int(cost_micros or 0), rid))
        c.commit()


def run_list(trigger=None, task_id=None, host=None, action=None, status=None, since=None, until=None,
             before=None, limit=50):
    q = "SELECT r.*, c.title AS title FROM agent_runs r LEFT JOIN agent_conversations c ON c.id=r.conversation_id WHERE 1=1"
    args = []
    for col, val in (("r.trigger", trigger), ("r.task_id", task_id), ("r.status", status)):
        if val:
            q += " AND %s=?" % col
            args.append(val)
    if host:
        q += " AND r.id IN (SELECT run_id FROM agent_steps WHERE host=?)"
        args.append(host)
    if action:
        q += " AND r.id IN (SELECT run_id FROM agent_steps WHERE tool=? AND kind IN ('action_intent','policy'))"
        args.append(action)
    if since:
        q += " AND r.created_at>=?"
        args.append(int(since))
    if until:
        q += " AND r.created_at<?"
        args.append(int(until))
    if before:
        q += " AND r.created_at<?"
        args.append(int(before))
    q += " ORDER BY r.created_at DESC, r.id LIMIT ?"
    args.append(int(limit))
    with ports.db() as c:
        rows = _rows(c.execute(q, args))
    for r in rows:
        r["context"] = _loads(r.pop("context_json"), None)
        r["hosts"] = _loads(r.pop("hosts_json"), None)
    return rows


def runs_with_status(statuses):
    marks = ",".join("?" * len(statuses))
    with ports.db() as c:
        return _rows(c.execute("SELECT id, conversation_id, trigger, task_id, attended, status FROM agent_runs "
                               "WHERE status IN (%s) ORDER BY created_at" % marks, list(statuses)))


def active_run_in_conversation(cid):
    with ports.db() as c:
        r = c.execute("SELECT id FROM agent_runs WHERE conversation_id=? AND status IN ('queued','running',"
                      "'waiting_approval') ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
    return r[0] if r else None


def task_active_run(task_id):
    with ports.db() as c:
        r = c.execute("SELECT id FROM agent_runs WHERE task_id=? AND status IN ('queued','running',"
                      "'waiting_approval') LIMIT 1", (task_id,)).fetchone()
    return r[0] if r else None


def cost_since(since, task_id=None, cloud_only_provider_ids=None):
    q = "SELECT COALESCE(SUM(cost_micros),0) FROM agent_runs WHERE created_at>=?"
    args = [since]
    if task_id:
        q += " AND task_id=?"
        args.append(task_id)
    with ports.db() as c:
        return c.execute(q, args).fetchone()[0]


# ── steps (append-only) ──────────────────────────────────────────────────────

STEP_FIELDS = ("tool", "host", "target", "args_json", "args_hash", "ok", "result_bytes", "result_excerpt",
               "result_hash", "decision", "decision_reason", "approval_id", "actor", "duration_ms",
               "tokens_in", "tokens_out")


def step_append(run_id, kind, **fields):
    vals = {k: fields.get(k) for k in STEP_FIELDS}
    if isinstance(vals["args_json"], (dict, list)):
        vals["args_json"] = _dumps(vals["args_json"])
    if isinstance(vals["ok"], bool):
        vals["ok"] = 1 if vals["ok"] else 0
    with ports.db() as c:
        seq = (c.execute("SELECT COALESCE(MAX(seq),0) FROM agent_steps WHERE run_id=?",
                         (run_id,)).fetchone()[0] or 0) + 1
        c.execute("INSERT INTO agent_steps(run_id,seq,ts,kind,%s) VALUES(?,?,?,?,%s)"
                  % (",".join(STEP_FIELDS), ",".join("?" * len(STEP_FIELDS))),
                  [run_id, seq, now(), kind] + [vals[k] for k in STEP_FIELDS])
        c.execute("UPDATE agent_runs SET steps=? WHERE id=?", (seq, run_id))
        c.commit()
    return seq


def step_list(run_id, after_seq=0):
    with ports.db() as c:
        rows = _rows(c.execute("SELECT * FROM agent_steps WHERE run_id=? AND seq>? ORDER BY seq",
                               (run_id, int(after_seq or 0))))
    for r in rows:
        r["args"] = _loads(r.pop("args_json"), None)
    return rows


def action_count(host, action, target, since):
    """Successful runs of one action on one target since `since` (rate limit)."""
    with ports.db() as c:
        return c.execute("SELECT COUNT(*) FROM agent_steps WHERE kind='action_result' AND ok=1 AND host=? "
                         "AND tool=? AND COALESCE(target,'')=? AND ts>=?",
                         (host, action, target or "", since)).fetchone()[0]


def steps_for_export(since=None, until=None, host=None, run_ids=None):
    q = "SELECT * FROM agent_steps WHERE 1=1"
    args = []
    if since:
        q += " AND ts>=?"
        args.append(int(since))
    if until:
        q += " AND ts<?"
        args.append(int(until))
    if host:
        q += " AND host=?"
        args.append(host)
    if run_ids is not None:
        if not run_ids:
            return []
        q += " AND run_id IN (%s)" % ",".join("?" * len(run_ids))
        args.extend(run_ids)
    q += " ORDER BY ts, run_id, seq"
    with ports.db() as c:
        return _rows(c.execute(q, args))


# ── approvals ────────────────────────────────────────────────────────────────

def approval_propose(run_id, task_id, call_id, action, host, params, risk, rationale, attended,
                     catalogue_version, ttl_s, bind=None):
    """Create a pending approval, or — when the same action/host/params is
    already pending (another night's run proposed it too) — count it on that
    one instead. Returns (approval_id, created_new)."""
    ph = params_hash(action, host, params)
    t = now()
    with ports.db() as c:
        existing = c.execute("SELECT id FROM agent_approvals WHERE host=? AND action=? AND params_hash=? "
                             "AND status='pending'", (host, action, ph)).fetchone()
        if existing and not attended:
            c.execute("UPDATE agent_approvals SET seen_count=seen_count+1, last_proposed_at=?, "
                      "rationale=COALESCE(?, rationale), run_id=?, expires_at=MAX(expires_at, ?) WHERE id=?",
                      (t, rationale, run_id, t + ttl_s, existing[0]))
            c.commit()
            return existing[0], False
        if existing:
            # An attended chat card always gets its own card; park the old one.
            c.execute("UPDATE agent_approvals SET status='expired', decided_at=?, decided_by='superseded' "
                      "WHERE id=?", (t, existing[0]))
        aid = new_id()
        c.execute("INSERT INTO agent_approvals(id,run_id,task_id,call_id,action,host,params_json,params_hash,risk,"
                  "attended,rationale,bind_json,catalogue_version,status,seen_count,last_proposed_at,created_at,"
                  "expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?)",
                  (aid, run_id, task_id, call_id, action, host, _dumps(params), ph, risk, 1 if attended else 0,
                   rationale, _dumps(bind), catalogue_version, t, t, t + ttl_s))
        c.commit()
    return aid, True


def approval_get(aid):
    with ports.db() as c:
        r = _row(c.execute("SELECT * FROM agent_approvals WHERE id=?", (aid,)))
    if r:
        r["params"] = _loads(r.pop("params_json"), {})
        r["bind"] = _loads(r.pop("bind_json"), None)
    return r


def approval_list(status=None, run_id=None, limit=200):
    q = "SELECT * FROM agent_approvals WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"
        args.append(status)
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with ports.db() as c:
        rows = _rows(c.execute(q, args))
    for r in rows:
        r["params"] = _loads(r.pop("params_json"), {})
        r["bind"] = _loads(r.pop("bind_json"), None)
    return rows


def approval_transition(aid, from_status, to_status, actor=None, result=None, now_ts=None):
    """Exactly-once state change: only the caller whose UPDATE matched wins.
    Deciding also requires the approval not to have expired."""
    t = now_ts or now()
    with ports.db() as c:
        if from_status == "pending":
            n = c.execute("UPDATE agent_approvals SET status=?, decided_at=?, decided_by=? "
                          "WHERE id=? AND status='pending' AND expires_at>?",
                          (to_status, t, actor, aid, t)).rowcount
        elif to_status == "executing":
            n = c.execute("UPDATE agent_approvals SET status='executing', executed_at=? WHERE id=? AND status=?",
                          (t, aid, from_status)).rowcount
        else:
            n = c.execute("UPDATE agent_approvals SET status=?, result_excerpt=COALESCE(?, result_excerpt) "
                          "WHERE id=? AND status=?", (to_status, result, aid, from_status)).rowcount
        c.commit()
    return n == 1


def approvals_expire(now_ts=None):
    t = now_ts or now()
    with ports.db() as c:
        rows = _rows(c.execute("SELECT id, run_id, attended FROM agent_approvals WHERE status='pending' "
                               "AND expires_at<=?", (t,)))
        c.execute("UPDATE agent_approvals SET status='expired', decided_at=?, decided_by='expiry' "
                  "WHERE status='pending' AND expires_at<=?", (t, t))
        c.commit()
    return rows


def approvals_expire_all(reason):
    t = now()
    with ports.db() as c:
        rows = _rows(c.execute("SELECT id, run_id FROM agent_approvals WHERE status='pending'"))
        c.execute("UPDATE agent_approvals SET status='expired', decided_at=?, decided_by=? WHERE status='pending'",
                  (t, reason))
        c.commit()
    return rows


def approvals_expire_for_run(run_id, reason):
    """A stopped chat run's own confirm cards go with it."""
    with ports.db() as c:
        c.execute("UPDATE agent_approvals SET status='expired', decided_at=?, decided_by=? "
                  "WHERE run_id=? AND status='pending' AND attended=1", (now(), reason, run_id))
        c.commit()


def approvals_executing():
    with ports.db() as c:
        return c.execute("SELECT COUNT(*) FROM agent_approvals WHERE status='executing'").fetchone()[0]


# ── policy tables ────────────────────────────────────────────────────────────

def host_modes():
    with ports.db() as c:
        return dict(c.execute("SELECT host, mode FROM agent_host_policy").fetchall())


def set_host_mode(host, mode):
    with ports.db() as c:
        c.execute("INSERT INTO agent_host_policy(host,mode,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(host) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
                  (host, mode, now()))
        c.commit()


def action_rules():
    with ports.db() as c:
        rows = c.execute("SELECT host, action, rule, unattended_unlocked FROM agent_action_rules").fetchall()
    out = {}
    for host, action, rule, unlocked in rows:
        out.setdefault(host, {})[action] = {"rule": rule, "unattended_unlocked": bool(unlocked)}
    return out


def set_action_rule(host, action, rule, unlocked):
    with ports.db() as c:
        c.execute("INSERT INTO agent_action_rules(host,action,rule,unattended_unlocked,updated_at) "
                  "VALUES(?,?,?,?,?) ON CONFLICT(host,action) DO UPDATE SET rule=excluded.rule, "
                  "unattended_unlocked=excluded.unattended_unlocked, updated_at=excluded.updated_at",
                  (host, action, rule, 1 if unlocked else 0, now()))
        c.commit()


def protected_list():
    with ports.db() as c:
        return _rows(c.execute("SELECT host, kind, name, reason, created_at FROM agent_protected "
                               "ORDER BY host, kind, name"))


def set_protected(host, kind, name, on, reason=None):
    with ports.db() as c:
        if on:
            c.execute("INSERT OR REPLACE INTO agent_protected(host,kind,name,reason,created_at) VALUES(?,?,?,?,?)",
                      (host, kind, name, reason, now()))
        else:
            c.execute("DELETE FROM agent_protected WHERE host=? AND kind=? AND name=?", (host, kind, name))
        c.commit()


@contextlib.contextmanager
def _on(conn):
    """The caller's connection inside its open transaction (no commit here),
    or our own under the dashboard lock, committed on the way out."""
    if conn is not None:
        yield conn
        return
    with ports.db() as c:
        yield c
        c.commit()


def _refuse_pending(c, host, by, why):
    """Refuse a host's pending approvals; cancel chat runs parked on them so the
    conversation isn't stuck waiting for a card that can never be decided."""
    pend = [r[0] for r in c.execute("SELECT id FROM agent_approvals WHERE host=? AND status='pending'", (host,))]
    runs = [r[0] for r in c.execute("SELECT DISTINCT run_id FROM agent_approvals WHERE host=? AND status='pending' "
                                    "AND attended=1", (host,))]
    c.execute("UPDATE agent_approvals SET status='refused', decided_at=?, decided_by=?, result_excerpt=? "
              "WHERE host=? AND status='pending'", (now(), by, why, host))
    for rid in runs:
        c.execute("UPDATE agent_runs SET status='cancelled', status_reason=?, ended_at=? "
                  "WHERE id=? AND status='waiting_approval'", (by, now(), rid))
    return pend


def host_renamed(old, new, conn=None):
    with _on(conn) as c:
        for table in ("agent_host_policy", "agent_action_rules", "agent_protected"):
            c.execute("UPDATE OR REPLACE %s SET host=? WHERE host=?" % table, (new, old))
        pend = _refuse_pending(c, old, "host_renamed", "The host was renamed; the agent must propose this again.")
        for tid, hosts_json in c.execute("SELECT id, hosts_json FROM agent_tasks").fetchall():
            hosts = _loads(hosts_json, [])
            if old in hosts:
                c.execute("UPDATE agent_tasks SET hosts_json=? WHERE id=?",
                          (_dumps([new if h == old else h for h in hosts]), tid))
    return pend


def host_deleted(name, conn=None):
    with _on(conn) as c:
        for table in ("agent_host_policy", "agent_action_rules", "agent_protected"):
            c.execute("DELETE FROM %s WHERE host=?" % table, (name,))
        pend = _refuse_pending(c, name, "host_deleted", "The host was removed.")
        for tid, hosts_json in c.execute("SELECT id, hosts_json FROM agent_tasks").fetchall():
            hosts = _loads(hosts_json, [])
            if name in hosts:
                c.execute("UPDATE agent_tasks SET hosts_json=? WHERE id=?",
                          (_dumps([h for h in hosts if h != name]), tid))
    return pend


def prune_policy_orphans(known_hosts):
    """Remove policy rows whose host isn't registered any more."""
    marks = ",".join("?" * len(known_hosts))
    n = 0
    with ports.db() as c:
        for table in ("agent_host_policy", "agent_action_rules", "agent_protected"):
            n += c.execute("DELETE FROM %s WHERE host NOT IN (%s)" % (table, marks), list(known_hosts)).rowcount
        c.commit()
    return n


# ── tasks & fires ────────────────────────────────────────────────────────────

_TASK_JSON = ("tools_json", "actions_json", "hosts_json", "delivery_json", "budget_json")


def _task_out(r):
    if not r:
        return r
    for k in _TASK_JSON:
        r[k[:-5]] = _loads(r.pop(k), [] if k != "delivery_json" and k != "budget_json" else {})
    r["enabled"] = bool(r["enabled"])
    return r


def task_insert(t):
    tid, ts = new_id(), now()
    with ports.db() as c:
        c.execute("INSERT INTO agent_tasks(id,name,template_id,template_version,prompt,tools_json,actions_json,"
                  "hosts_json,cron,tz,mode,provider_id,model,delivery_json,budget_json,catch_up,enabled,next_fire,"
                  "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, t["name"], t.get("template_id"), t.get("template_version"), t["prompt"],
                   _dumps(t.get("tools") or []), _dumps(t.get("actions") or []), _dumps(t.get("hosts") or []),
                   t["cron"], t["tz"], t["mode"], t.get("provider_id"), t.get("model"),
                   _dumps(t.get("delivery") or {}), _dumps(t.get("budget") or {}), t.get("catch_up") or "skip",
                   1 if t.get("enabled", True) else 0, t.get("next_fire"), ts, ts))
        c.commit()
    return tid


def task_get(tid):
    with ports.db() as c:
        return _task_out(_row(c.execute("SELECT * FROM agent_tasks WHERE id=?", (tid,))))


def task_list():
    with ports.db() as c:
        return [_task_out(r) for r in _rows(c.execute("SELECT * FROM agent_tasks ORDER BY created_at"))]


def task_update(tid, **fields):
    plain = ("name", "prompt", "cron", "tz", "mode", "provider_id", "model", "catch_up", "enabled",
             "next_fire", "last_slot_local", "warning", "template_version")
    sets = []
    for k, v in fields.items():
        if k in plain:
            sets.append((k, (1 if v else 0) if k == "enabled" else v))
        elif k + "_json" in _TASK_JSON:
            sets.append((k + "_json", _dumps(v)))
    if not sets:
        return
    with ports.db() as c:
        c.execute("UPDATE agent_tasks SET %s, updated_at=? WHERE id=?" % ", ".join("%s=?" % k for k, _ in sets),
                  [v for _, v in sets] + [now(), tid])
        c.commit()


def task_delete(tid):
    with ports.db() as c:
        n = c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,)).rowcount
        c.commit()
    return n


def fire_claim(task_id, scheduled_for, outcome="fired"):
    """Insert the fire row for this slot; True only for the first caller (once per slot)."""
    with ports.db() as c:
        n = c.execute("INSERT OR IGNORE INTO agent_task_fires(task_id,scheduled_for,outcome,created_at) "
                      "VALUES(?,?,?,?)", (task_id, int(scheduled_for), outcome, now())).rowcount
        c.commit()
    return n == 1


def fire_set(task_id, scheduled_for, run_id=None, outcome=None):
    with ports.db() as c:
        c.execute("UPDATE agent_task_fires SET run_id=COALESCE(?, run_id), outcome=COALESCE(?, outcome) "
                  "WHERE task_id=? AND scheduled_for=?", (run_id, outcome, task_id, int(scheduled_for)))
        c.commit()


def fire_list(task_id, limit=20):
    with ports.db() as c:
        return _rows(c.execute("SELECT scheduled_for, outcome, run_id FROM agent_task_fires WHERE task_id=? "
                               "ORDER BY scheduled_for DESC LIMIT ?", (task_id, limit)))


def last_fire(task_id, outcome="fired"):
    with ports.db() as c:
        r = c.execute("SELECT MAX(scheduled_for) FROM agent_task_fires WHERE task_id=? AND outcome=?",
                      (task_id, outcome)).fetchone()
    return r[0] if r else None


# ── retention ────────────────────────────────────────────────────────────────

def prune(older_than):
    """Delete finished runs (and their steps/messages) created before
    `older_than`, decided approvals, and empty conversations. Pending approvals
    and runs waiting for one are never pruned."""
    terms = ",".join("?" * len(TERMINAL))
    with ports.db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM agent_runs WHERE created_at<? AND status IN (%s)" % terms,
                                       [older_than] + list(TERMINAL))]
        c.execute("INSERT INTO agent_retention(id,cutoff) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET "
                  "cutoff=MAX(cutoff, excluded.cutoff)", (older_than,))
        for rid in ids:
            c.execute("DELETE FROM agent_steps WHERE run_id=? AND ts<?", (rid, older_than))
            c.execute("DELETE FROM agent_messages WHERE run_id=?", (rid,))
            c.execute("DELETE FROM agent_runs WHERE id=?", (rid,))
        c.execute("DELETE FROM agent_approvals WHERE status NOT IN ('pending','approved','executing') "
                  "AND created_at<?", (older_than,))
        c.execute("DELETE FROM agent_conversations WHERE updated_at<? AND id NOT IN "
                  "(SELECT conversation_id FROM agent_runs)", (older_than,))
        c.execute("DELETE FROM agent_task_fires WHERE created_at<?", (older_than,))
        c.commit()
    return len(ids)
