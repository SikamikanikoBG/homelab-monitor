"""Backup must carry no secrets: provider keys, password hash, sessions, api keys, channel tokens."""
import hlm, json, sqlite3, urllib.request, os, tempfile
c = hlm.Client(); c.login()
KEY = "sk-alpha-BACKUP-LEAK-5555"
s, d = c.call("POST", "/api/agent/providers", {"preset": "openai", "api_key": KEY, "label": "backup leak"})
pid = d["provider"]["id"]
req = urllib.request.Request(c.base + "/api/backup")
data = c.op.open(req).read()
p = os.path.join(tempfile.gettempdir(), "hlm_backup.db"); open(p, "wb").write(data)
print("backup bytes", len(data), "raw contains key:", KEY.encode() in data, "last4 present:", b"5555" in data)
db = sqlite3.connect(p)
tabs = [r[0] for r in db.execute("select name from sqlite_master where type='table'")]
print("tables:", tabs)
for t in tabs:
    cols = [r[1] for r in db.execute("pragma table_info(%s)" % t)]
    sus = [col for col in cols if any(x in col.lower() for x in ("key", "secret", "token", "hash", "password", "webhook"))]
    if sus:
        rows = db.execute("select %s from %s limit 5" % (",".join(sus), t)).fetchall()
        print(t, sus, rows)
# settings table secrets
for t in tabs:
    if "setting" in t:
        for r in db.execute("select * from %s" % t):
            if any(x in str(r[0]).lower() for x in ("pass", "token", "key", "secret", "webhook", "hash")):
                print("  ", t, r)
c.call("DELETE", "/api/agent/providers/" + pid)
