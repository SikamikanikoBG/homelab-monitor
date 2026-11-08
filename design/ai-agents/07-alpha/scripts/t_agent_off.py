import hlm, json, urllib.request
c = hlm.Client(); print("login", c.login()[0])
for path in ("/api/agent/status", "/api/agent/conversations", "/api/agent/policy", "/api/agent/runs", "/api/agent/templates", "/api/agent/nope"):
    print(path, c.call("GET", path))
print("POST enable", c.call("POST", "/api/agent/enable", {"enabled": True}))
s, html = c.call("GET", "/", raw=True)
print("index mentions ag-fab:", "ag-fab" in html, "agent.js:", "agent.js" in html, "data-t=\"agent\"" in html)
# stream frames
req = urllib.request.Request(c.base + "/api/stream")
r = c.op.open(req, timeout=15)
buf = b""
import time; t0 = time.time()
while time.time() - t0 < 8:
    chunk = r.read1(65536) if hasattr(r, "read1") else r.read(4096)
    if not chunk: break
    buf += chunk
print("stream bytes", len(buf), "agent in stream:", b"agent" in buf)
import re
print("html agent refs:", sorted(set(re.findall(r'.{40}agent.{40}', html)))[:12])
print("stream agent refs:", sorted(set(re.findall(rb'.{30}agent.{30}', buf)))[:8])
