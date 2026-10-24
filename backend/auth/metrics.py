"""Login counter for /metrics. Created on the first login attempt, which can
only happen once a password is set — so installs without a login keep a
byte-identical /metrics page."""
import threading

_lock = threading.Lock()
_counter = {}


def login(result):
    with _lock:
        if "c" not in _counter:
            try:
                from prometheus_client import Counter
                _counter["c"] = Counter("homelab_auth_logins", "Dashboard login attempts", ["result"])
            except Exception as e:                 # prometheus_client missing or name taken
                print("auth metrics unavailable: %s" % e, flush=True)
                _counter["c"] = None
        c = _counter["c"]
    if c is not None:
        c.labels(result=result).inc()
