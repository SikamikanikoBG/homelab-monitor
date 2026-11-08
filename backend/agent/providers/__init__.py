"""Model providers: presets (data), adapters (three code paths), keys.

Keys are write-only. They live in `agent_providers.api_key` (blanked in every
backup) or, better, outside the database: `key_ref` = `env:NAME` or
`file:/run/secrets/name`, where only the reference is stored.
"""
import json
import os
import posixpath
import re
import threading
import urllib.parse

from backend.agent.providers.anthropic import AnthropicAdapter
from backend.agent.providers.base import ProviderError
from backend.agent.providers.ollama import OllamaAdapter
from backend.agent.providers.openai_compat import OpenAICompatAdapter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTERS = {"openai_compat": OpenAICompatAdapter, "ollama": OllamaAdapter, "anthropic": AnthropicAdapter}
_presets = {}
_sems = {}
_sem_lock = threading.Lock()


def presets():
    if not _presets:
        with open(os.path.join(_HERE, "presets.json"), encoding="utf-8") as f:
            for p in json.load(f)["presets"]:
                _presets[p["id"]] = p
    return list(_presets.values())


def preset(pid):
    presets()
    return _presets.get(pid)


def public_presets():
    return [{"id": p["id"], "label": p["label"], "adapter": p["adapter"], "base_url": p.get("base_url") or "",
             "needs_key": bool(p.get("needs_key")), "local": bool(p.get("local")),
             "experimental": bool(p.get("experimental")), "extra_fields": p.get("extra_fields") or []}
            for p in presets()]


def check_base_url(url):
    """A user-facing problem with a provider URL, or None."""
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return "That URL can't be read."
    if u.scheme not in ("http", "https") or not u.netloc:
        return "Use an http:// or https:// URL."
    if u.username or u.password:
        return "Put the key in the key field, not in the URL."
    return None


def url_host(url):
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def key_dir():
    """Where file: key references may point (Docker/compose secrets by default)."""
    return (os.environ.get("AGENT_KEY_DIR") or "/run/secrets").rstrip("/") + "/"


def split_key(api_key, key_ref):
    """(key, ref): a key typed as env:NAME or file:/path is a reference, not a
    literal key (alpha #22)."""
    k = (api_key or "").strip()
    if k.startswith(("env:", "file:")):
        return None, k
    return (k or None), (key_ref or None)


def check_key_ref(ref):
    if not ref:
        return None
    if ref.startswith("env:") and re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]{0,127}", ref):
        return None
    if ref.startswith("file:"):
        path = posixpath.normpath(ref[5:])
        if path.startswith(key_dir()) and ".." not in ref:
            return None
        return "A file key must live under %s (set AGENT_KEY_DIR to change it)." % key_dir()
    return "A key reference looks like env:NAME or file:%sname." % key_dir()


def resolve_key(row):
    """The key for a provider row, from its reference or the database."""
    ref = (row or {}).get("key_ref") or ""
    if ref.startswith("env:"):
        return os.environ.get(ref[4:]) or None
    if ref.startswith("file:"):
        if check_key_ref(ref):
            return None
        try:
            with open(ref[5:], encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    from backend.agent import store
    return store.provider_secret(row["id"]) if row and row.get("id") else None


def is_local(row):
    p = preset((row or {}).get("preset")) or {}
    return bool(p.get("local"))


def adapter_for(row):
    p = preset(row["preset"]) or {"adapter": row.get("adapter", "openai_compat")}
    cls = _ADAPTERS.get(p.get("adapter") or row.get("adapter"))
    if cls is None:
        raise ProviderError("bad_request", "unknown adapter")
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else json.loads(row.get("extra_json") or "{}")
    timeout = 180 if p.get("local") else 60
    return cls(row["base_url"], api_key=resolve_key(row), preset=p, extra=extra, timeout=timeout)


def semaphore(row):
    """Per-endpoint concurrency: one request at a time to a local model server
    (it has one GPU), four to a cloud API."""
    key = row.get("base_url") or row.get("id")
    with _sem_lock:
        if key not in _sems:
            _sems[key] = threading.BoundedSemaphore(1 if is_local(row) else 4)
        return _sems[key]


def serialize(row):
    """A provider row for the browser. Never includes the key."""
    p = preset(row.get("preset")) or {}
    try:
        last = json.loads(row.get("last_test_json") or "null")
    except ValueError:
        last = None
    return {"id": row["id"], "preset": row["preset"], "label": row["label"], "adapter": row["adapter"],
            "base_url": row["base_url"], "key_set": bool(row.get("key_stored") or row.get("key_ref")),
            "key_last4": row.get("key_last4"), "key_ref": row.get("key_ref"), "enabled": bool(row.get("enabled")),
            "source": row.get("source"), "local": bool(p.get("local")), "experimental": bool(p.get("experimental")),
            "extra": json.loads(row.get("extra_json") or "{}"), "last_test_at": row.get("last_test_at"),
            "last_test": last}
