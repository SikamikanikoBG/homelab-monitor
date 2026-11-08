"""Pass 2 #4/#8/#18: rename sequence, orphans, stuck run."""
import hlm, time, json
from t_policy import setp, c
def pol():
    p = c.call("GET", "/api/agent/policy")[1]; return [(h["host"], h["mode"]) for h in p["hosts"]], {k: list(v) for k, v in p["rules"].items()}
print("start:", pol())
print("db rows:", hlm.ardi("docker exec hlm-agents-alpha python -c \"import sqlite3;db=sqlite3.connect('/data/gpu.db');print(list(db.execute('select host,mode from agent_host_policy')));print(list(db.execute('select host,action,rule from agent_action_rules')))\"").strip())
setp("lab2", "active", "container.restart", "ask")
cid = c.new_conv()
s, d = c.ask(cid, hlm.stub([{"tool_calls": [{"name": "container__restart", "arguments": {"host": "lab2", "name": "worker"}}]}, {"text": "x"}]))
ap = [a for a in d["approvals"] if a["status"] == "pending"]
print("card:", [a["status"] for a in d["approvals"]], d["run"]["status"])
print("1 lab2->lab2x:", c.call("PATCH", "/api/hosts/lab2", {"name": "lab2x"})); time.sleep(2); print("  ", pol())
time.sleep(2)
r = c.call("GET", "/api/agent/runs/" + d["run"]["id"])[1]
print("  run after rename:", r["run"]["status"], r["run"]["status_reason"], [(a["status"], a.get("result")) for a in r["approvals"]])
print("  new message in conv:", c.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": "hello"})[0])
print("2 lab2x->lab2:", c.call("PATCH", "/api/hosts/lab2x", {"name": "lab2"})); time.sleep(2); print("  ", pol())
setp("lab2", "passive")
print("3 lab2->lab2x:", c.call("PATCH", "/api/hosts/lab2", {"name": "lab2x"})); time.sleep(2); print("  ", pol())
print("4 lab2x->lab2:", c.call("PATCH", "/api/hosts/lab2x", {"name": "lab2"})); time.sleep(2); print("  ", pol())
print("hosts:", [h["name"] for h in c.call("GET", "/api/hosts")[1]["hosts"]])
print("db rows:", hlm.ardi("docker exec hlm-agents-alpha python -c \"import sqlite3;db=sqlite3.connect('/data/gpu.db');print(list(db.execute('select host,mode from agent_host_policy')));print(list(db.execute('select host,action,rule from agent_action_rules')))\"").strip())
print("500s in log:", hlm.ardi("docker logs hlm-agents-alpha 2>&1 | grep -c 'PATCH /api/hosts.* 500'").strip())
