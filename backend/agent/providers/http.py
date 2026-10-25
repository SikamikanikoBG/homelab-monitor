"""urllib plumbing every adapter shares: one POST that streams lines back, one
GET for JSON, the error mapping, retries, and the Stop button.

- No redirects: a redirect would carry the API key to wherever it points.
- Retries only before the first byte (a retry after that would duplicate text
  or tool calls), only for unreachable / 429 / 5xx / timeout, 3 tries, backoff
  1/3/9 s honouring Retry-After.
- Error bodies are trimmed and scrubbed of the key before anyone sees them.
- Stop: `Call.abort()` closes the live response from another thread; the
  reading thread then gets a `cancelled` error.
"""
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request

from backend.agent.providers.base import ProviderError, RETRYABLE

BACKOFF = (1, 3, 9)
MAX_STREAM_BYTES = 16 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused (%s)" % newurl[:80], headers, fp)


_opener = urllib.request.build_opener(_NoRedirect)


class Call:
    """Holds the live response so Stop can close it."""

    def __init__(self, cancel=None):
        self.cancel = cancel or threading.Event()
        self._resp = None
        self._lock = threading.Lock()

    def attach(self, resp):
        with self._lock:
            self._resp = resp
        if self.cancel.is_set():
            self.abort()

    def abort(self):
        self.cancel.set()
        self.close()

    def close(self):
        """Close the live response without cancelling the run."""
        with self._lock:
            r = self._resp
        if r is not None:
            try:
                r.close()
            except Exception as e:                    # closing a half-read socket can raise; we're stopping anyway
                print("agent http: close on abort: %s" % e, flush=True)


def _scrub(text, secret):
    text = (text or "")[:600]
    if secret:
        text = text.replace(secret, "••••")
    return text.strip()


def _error_from_http(e, secret):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    msg = body
    try:
        j = json.loads(body)
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or json.dumps(err)
        elif isinstance(err, str):
            msg = err
        elif isinstance(j, dict) and j.get("message"):
            msg = j["message"]
    except ValueError:
        pass
    ctype = (e.headers.get("Content-Type") if e.headers else "") or ""
    if "html" in ctype.lower():
        msg = "HTTP %s %s" % (e.code, e.reason or "")      # a web page, not an API answer: no raw HTML (alpha #36)
    msg = _scrub(msg or str(e.reason), secret)
    code = e.code
    retry_after = None
    try:
        retry_after = float(e.headers.get("Retry-After")) if e.headers and e.headers.get("Retry-After") else None
    except (TypeError, ValueError):
        retry_after = None
    if code in (401, 403):
        kind = "auth"
    elif code == 429:
        kind = "rate_limited"
    elif code >= 500:
        kind = "server"
    elif 300 <= code < 400:
        kind = "protocol"
    else:
        kind = "bad_request"
    return ProviderError(kind, msg, status=code, retry_after=retry_after)


def _open(req, timeout, secret):
    try:
        return _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise _error_from_http(e, secret)
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.timeout):
            raise ProviderError("timeout", "no answer within %ss" % timeout)
        raise ProviderError("unreachable", _scrub(str(e.reason), secret))
    except socket.timeout:
        raise ProviderError("timeout", "no answer within %ss" % timeout)
    except (ConnectionError, OSError) as e:
        raise ProviderError("unreachable", _scrub(str(e), secret))


def _with_retries(fn, cancel, sleep=time.sleep):
    last = None
    for attempt in range(len(BACKOFF) + 1):
        if cancel is not None and cancel.is_set():
            raise ProviderError("cancelled")
        try:
            return fn()
        except ProviderError as e:
            last = e
            if e.kind not in RETRYABLE or attempt == len(BACKOFF):
                raise
            wait = min(e.retry_after or BACKOFF[attempt], 30)
            if cancel is not None:
                if cancel.wait(wait):
                    raise ProviderError("cancelled")
            else:
                sleep(wait)
    raise last


def stream_lines(url, payload, headers, call, timeout=180, secret=None):
    """POST JSON and yield decoded, non-empty lines (SSE or NDJSON).

    The connection and the reads run on a helper thread that hands lines over
    a queue; this side polls the queue every 0.2 s and checks the Stop event, so
    Stop takes effect at once even while the model is still thinking about its
    first byte (alpha #5). A stopped helper is abandoned: its socket is closed
    and whatever it still receives is thrown away."""
    data = json.dumps(payload).encode("utf-8")
    hdrs = dict({"Content-Type": "application/json", "Accept": "text/event-stream, application/x-ndjson, application/json",
                 "User-Agent": "homelab-monitor-agent"}, **(headers or {}))
    q = queue.Queue()
    done = threading.Event()

    def connect():
        return _open(urllib.request.Request(url, data=data, headers=hdrs, method="POST"), timeout, secret)

    def reader():
        resp = None
        try:
            resp = _with_retries(connect, call.cancel)
            call.attach(resp)
            total = 0
            while not done.is_set():
                try:
                    raw = resp.readline()
                except socket.timeout:
                    raise ProviderError("timeout", "the stream stalled for %ss" % timeout)
                except (ValueError, OSError, AttributeError) as e:
                    raise ProviderError("cancelled" if call.cancel.is_set() else "protocol", "stream broke: %s" % e)
                if not raw:
                    break
                total += len(raw)
                if total > MAX_STREAM_BYTES:
                    raise ProviderError("protocol", "answer larger than %d MB" % (MAX_STREAM_BYTES // (1024 * 1024)))
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    q.put(("line", line))
            q.put(("end", None))
        except ProviderError as e:
            q.put(("err", e))
        except Exception as e:                       # anything else the socket throws: report, don't kill the thread silently
            q.put(("err", ProviderError("cancelled" if call.cancel.is_set() else "protocol", str(e)[:200])))
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception as e:
                    print("agent http: close: %s" % e, flush=True)

    threading.Thread(target=reader, name="agent-http", daemon=True).start()
    try:
        while True:
            if call.cancel.is_set():
                raise ProviderError("cancelled")
            try:
                kind, val = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "line":
                yield val
            elif kind == "end":
                return
            else:
                raise val
    finally:
        done.set()                                   # stop reading (normal end, break, or Stop)
        call.close()


def get_json(url, headers=None, timeout=10, secret=None, cancel=None):
    hdrs = dict({"Accept": "application/json", "User-Agent": "homelab-monitor-agent"}, **(headers or {}))

    def once():
        resp = _open(urllib.request.Request(url, headers=hdrs), timeout, secret)
        try:
            raw = resp.read(MAX_STREAM_BYTES)
        finally:
            resp.close()
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ProviderError("protocol", "not JSON")

    return _with_retries(once, cancel) if cancel is not None else once()


def post_json(url, payload, headers=None, timeout=30, secret=None):
    hdrs = dict({"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "homelab-monitor-agent"}, **(headers or {}))
    resp = _open(urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs,
                                        method="POST"), timeout, secret)
    try:
        raw = resp.read(MAX_STREAM_BYTES)
    finally:
        resp.close()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ProviderError("protocol", "not JSON")
