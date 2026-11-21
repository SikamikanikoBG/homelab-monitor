"""EC-34: restore while a run is busy must be refused (409)."""
import hlm, json, time, uuid, urllib.request, os, tempfile
c = hlm.Client(); c.login()
data = c.op.open(urllib.request.Request(c.base + "/api/backup")).read()
slow = [p for p in c.call("GET", "/api/agent/providers")[1]["providers"] if "/slow/" in p["base_url"]][0]
s, r = c.call("POST", "/api/agent/conversations/%s/messages" % c.new_conv(), {"text": "slow", "provider_id": slow["id"], "model": "stub-agent"})
time.sleep(3)
print("running:", c.call("GET", "/api/agent/status")[1]["running"])
b = uuid.uuid4().hex
body = (b"--" + b.encode() + b"\r\nContent-Disposition: form-data; name=\"backup\"; filename=\"backup.db\"\r\nContent-Type: application/octet-stream\r\n\r\n" + data + b"\r\n--" + b.encode() + b"--\r\n")
req = urllib.request.Request(c.base + "/api/backup/restore", data=body, method="POST",
                             headers={"Content-Type": "multipart/form-data; boundary=" + b, "X-CSRF-Token": c.csrf})
try:
    with c.op.open(req, timeout=60) as resp: print("restore:", resp.status, resp.read()[:300])
except urllib.error.HTTPError as e: print("restore:", e.code, e.read()[:300])
c.call("POST", "/api/agent/runs/%s/cancel" % r["run_id"])
