"""Tiny stdlib client for the alpha instance. import hlm; c = hlm.Client(); c.login()"""
import http.cookiejar, json, os, subprocess, time, urllib.error, urllib.request
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("ALPHA_URL", "http://100.97.120.53:9831")
PW = os.environ.get("ALPHA_PASSWORD", "alpha-agents-2026")


class Client:
    def __init__(self, base=BASE):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        self.headers = {}

    def call(self, method, path, body=None, headers=None, timeout=60, raw=False, csrf=True):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"}
        if csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(self.headers)
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.op.open(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
                st = r.status
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            st = e.code
        if raw:
            return st, txt
        try:
            return st, json.loads(txt)
        except ValueError:
            return st, txt

    def login(self, pw=PW):
        s, d = self.call("POST", "/api/auth/login", {"password": pw})
        if s == 200:
            self.csrf = d.get("csrf", "")
        return s, d

    def wait_run(self, rid, until=("done", "failed", "refused", "cancelled", "waiting_approval",
                                   "budget_exhausted", "interrupted"), t=240):
        end = time.time() + t
        d = None
        while time.time() < end:
            s, d = self.call("GET", "/api/agent/runs/" + rid)
            if s == 200 and d["run"]["status"] in until:
                return d
            time.sleep(1)
        return d

    def ask(self, cid, text, wait=True, **kw):
        s, d = self.call("POST", "/api/agent/conversations/%s/messages" % cid, {"text": text, **kw})
        if s != 202 or not wait:
            return s, d
        return s, self.wait_run(d["run_id"])

    def new_conv(self):
        return self.call("POST", "/api/agent/conversations")[1]["id"]


def ardi(cmd, timeout=60):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "ardi@100.97.120.53", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def started(box, name):
    return ardi("docker exec %s docker inspect -f '{{.State.StartedAt}} {{.State.Status}}' %s" % (box, name)).strip()


def stub(turns):
    return "#stub " + json.dumps(turns)


def stub2_provider(c):
    """A test-only stub that also finds '#stub' inside a task prompt (for scheduled runs)."""
    url = "http://hlm-alpha-stub2:18099/v1"
    for p in c.call("GET", "/api/agent/providers")[1]["providers"]:
        if p["base_url"] == url:
            return p["id"]
    s, d = c.call("POST", "/api/agent/providers", {"preset": "openai_compatible", "base_url": url, "label": "stub2 (alpha tester)"})
    pid = d["provider"]["id"]
    c.call("POST", "/api/agent/providers/%s/test" % pid)
    c.call("POST", "/api/agent/models/probe", {"provider_id": pid, "model": "stub-agent"})
    return pid
