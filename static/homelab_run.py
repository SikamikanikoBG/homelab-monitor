"""homelab_run.py — tiny client for HomeLab Monitor's run-tracking API.

Push training/session metadata + metrics to your self-hosted HomeLab Monitor and
pull it back, with real GPU energy/cost attached by the hub. Stdlib-only (urllib);
uses `requests` automatically if it's installed. Copy this one file anywhere — or
download it from your hub at  http://<hub>:9800/static/homelab_run.py

Quickstart (Jupyter / Colab / Kaggle):

    import homelab_run as homelab
    homelab.configure(url="http://homelab.lan:9800", key="hlm_xxx")  # or env vars

    with homelab.run("sft-llama3-lora", params={"lr": 2e-4, "bs": 8}) as r:
        for step, loss in enumerate(train()):
            r.log_metric("loss", loss, step=step)
            r.log_metric("tokens_per_sec", batch_tokens / step_seconds, step=step)  # tracked on Experiments
        r.log_params({"final_eval": 0.81})

    # pull it back (e.g. from another notebook), with cost attached by the hub:
    print(homelab.pull(r.id)["cost"], homelab.list_runs(range="7d"))

Colab/Kaggle note: those run in the cloud, so the hub URL must be reachable from
the internet — expose it via Tailscale (recommended) or an ngrok/Cloudflare tunnel
and pass that https URL. On your LAN, plain http://host:9800 works.
"""
import os, sys, json, time, uuid, socket, threading, urllib.request, urllib.error

try:
    import requests as _rq           # optional; nicer, but never required
except Exception:
    _rq = None

_CFG = {"url": os.environ.get("HOMELAB_URL", "http://localhost:9800"),
        "key": os.environ.get("HOMELAB_KEY", ""), "timeout": 10}


def configure(url=None, key=None, timeout=None):
    if url is not None:     _CFG["url"] = url.rstrip("/")
    if key is not None:     _CFG["key"] = key
    if timeout is not None: _CFG["timeout"] = timeout


def _headers():
    h = {"Content-Type": "application/json"}
    if _CFG["key"]:
        h["Authorization"] = "Bearer " + _CFG["key"]
        h["X-API-Key"] = _CFG["key"]
    return h


def _request(method, path, payload=None):
    url = _CFG["url"].rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if _rq is not None:
        resp = _rq.request(method, url, data=data, headers=_headers(), timeout=_CFG["timeout"])
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=_CFG["timeout"]) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")


def _detect_source():
    if "google.colab" in sys.modules:            return "colab"
    if os.environ.get("KAGGLE_KERNEL_RUN_TYPE"): return "kaggle"
    try:
        import IPython
        if IPython.get_ipython() is not None:    return "jupyter"
    except Exception:
        pass
    return "cli"


class Run:
    """One tracked run. Use via homelab.run(...) (context manager) or manually:
        r = homelab.run("name").start(); r.log_metric(...); r.finish()
    """
    def __init__(self, name, params=None, tags=None, notes=None, source=None,
                 heartbeat=30, buffer=True):
        self.id = uuid.uuid4().hex
        self.name = name
        self.params = dict(params or {})
        self.tags = tags or []
        self.notes = notes or ""
        self.source = source or _detect_source()
        self.host = socket.gethostname()
        self._hb_interval = heartbeat
        self._buffer = buffer
        self._buf = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False

    def start(self):
        _request("POST", "/api/runs", {
            "id": self.id, "name": self.name, "source": self.source, "host": self.host,
            "params": self.params, "tags": self.tags, "notes": self.notes,
            "started_at": int(time.time())})
        self._started = True
        if self._hb_interval:
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        return self

    def _heartbeat_loop(self):
        while not self._stop.wait(self._hb_interval):
            try:
                self.flush()
                _request("PATCH", f"/api/runs/{self.id}", {"status": "running"})
            except Exception:
                pass                          # never let telemetry crash training

    def log_metric(self, key, value, step=None, ts=None):
        pt = {"key": key, "value": float(value),
              "step": int(step) if step is not None else 0,
              "ts": int(ts) if ts is not None else int(time.time())}
        if self._buffer:
            with self._lock:
                self._buf.append(pt)
                flush = len(self._buf) >= 100
            if flush:
                self.flush()
        else:
            _request("POST", f"/api/runs/{self.id}/metrics", {"metrics": [pt]})
        return self

    def log_metrics(self, d, step=None, ts=None):
        for k, v in (d or {}).items():
            self.log_metric(k, v, step=step, ts=ts)
        return self

    def flush(self):
        with self._lock:
            pts, self._buf = self._buf, []
        if pts:
            _request("POST", f"/api/runs/{self.id}/metrics", {"metrics": pts})
        return self

    def log_params(self, d):
        self.params.update(d or {})
        _request("PATCH", f"/api/runs/{self.id}", {"params": self.params})
        return self

    def set_notes(self, text):
        self.notes = text
        _request("PATCH", f"/api/runs/{self.id}", {"notes": text})
        return self

    def _terminate(self, status):
        self._stop.set()
        try:
            self.flush()
        except Exception:
            pass
        _request("POST", f"/api/runs/{self.id}/finish",
                 {"status": status, "ended_at": int(time.time())})

    def finish(self):
        self._terminate("finished"); return self

    def fail(self):
        self._terminate("failed"); return self

    def pull(self):
        return pull(self.id)

    def __enter__(self):
        if not self._started:
            self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._terminate("failed" if exc_type else "finished")
        return False                          # don't suppress exceptions


def run(name, **kw):
    """Create a Run. `with homelab.run('x') as r: ...` starts/finishes it for you."""
    return Run(name, **kw)


def pull(run_id):
    """Full run + metrics + GPU power/util/cost series (dict)."""
    return _request("GET", f"/api/runs/{run_id}")


def list_runs(range="7d", status=None):
    path = f"/api/runs?range={range}" + (f"&status={status}" if status else "")
    return _request("GET", path)["runs"]
