"""US-11 activity filters + export (JSON/CSV) + no secrets in export/logs."""
import hlm, json, csv, io
c = hlm.Client(); c.login()
r = c.call("GET", "/api/agent/runs")[1]
print("runs keys:", list(r)[:10], "count:", len(r.get("items") or []))
for q in ("?host=lab1", "?status=failed", "?outcome=failed", "?trigger=schedule", "?task_id=x", "?action=container.restart", "?since=1792849000", "?from=2026-10-24", "?limit=5", "?offset=5&limit=5", "?q=restart"):
    s, d = c.call("GET", "/api/agent/runs" + q)
    items = d.get("items") if isinstance(d, dict) else None
    print(f"runs{q:30s} {s} n={len(items) if items is not None else d}")
s, j = c.call("GET", "/api/agent/audit/export")
print("export json keys:", list(j), "runs", len(j.get("runs", [])), "steps", len(j.get("steps", [])) if "steps" in j else "-")
st = json.dumps(j)
print("json has excerpts:", "untrusted_data" in st, "size", len(st))
s, txt = c.call("GET", "/api/agent/audit/export?format=csv", raw=True)
rows = list(csv.reader(io.StringIO(txt)))
print("csv header:", rows[0], "rows", len(rows))
for q in ("?host=lab1", "?format=csv&host=lab2", "?since=1792849000"):
    s, d = c.call("GET", "/api/agent/audit/export" + q, raw=True)
    print("export", q, s, len(d))
# CSV injection
bad = [r for r in rows[1:] if any(cell.startswith(("=", "+", "@")) for cell in r)]
print("csv cells starting with = + @:", bad[:3])
# secrets in logs
logs = hlm.ardi("docker logs hlm-agents-alpha 2>&1 | grep -c -E 'sk-alpha|NEVER-SHOWN|BACKUP-LEAK|alpha-agents-2026|hlm_[A-Za-z0-9_-]{20}'")
print("secret-looking strings in hub logs:", logs.strip())
print("admin-log:", json.dumps(c.call("GET", "/api/agent/admin-log")[1])[:600])
