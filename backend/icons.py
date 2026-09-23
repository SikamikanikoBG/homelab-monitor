"""App icons for containers and services — the selfh.st icon set, cached on the hub.

The dashboard ships ~65 brand marks inline, which covers the usual suspects and
nothing else. https://selfh.st/icons has ~2,900, which covers very nearly every
container a homelab runs. This module borrows them.

**The browser never talks to the icon CDN.** It asks the hub for
`/api/icon/<slug>.svg`; the hub fetches that one file once, writes it under the
data volume and serves every later request off disk. That matters for three
reasons: the list of icons a dashboard requests is a precise inventory of what
you self-host, and it shouldn't leak to a third party; a homelab with no
internet still shows its icons after the first fetch; and if the CDN ever goes
away, what you already have keeps working.

Resolution (name/image -> slug) is pure and needs no network: it runs against
the index this module keeps in memory, so annotating a 50-container payload
costs nothing. Only a genuine cache miss on a real slug ever hits the network,
in the request that asked for it.

Everything degrades to "no icon": no index, no network, an unknown name and a
CDN 404 all end at the same blank, never at an error.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request

# The index (~2,900 rows: display name, slug, which formats exist) and the SVGs
# themselves. jsdelivr serves the repo straight from GitHub.
INDEX_URL = "https://cdn.jsdelivr.net/gh/selfhst/icons/index.json"
SVG_URL   = "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/{slug}.svg"

INDEX_TTL   = 7 * 86400   # re-fetch the catalogue weekly; new apps appear often
MISS_TTL    = 86400       # remember a 404 for a day before asking again
MAX_SVG     = 256 * 1024  # a logo that big is not a logo
MAX_INDEX   = 8 * 1024 * 1024   # the catalogue is ~900 KB and grows; this is slack
HTTP_TIMEOUT = 8

# A slug is a filename on someone else's CDN and a path segment here. Keep it
# boring: no dots (no traversal, no extension games), no slashes, no unicode.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_log = logging.getLogger(__name__)

# Words that say nothing about which app this is. Stripped when trimming a name
# down to its recognisable core: "immich_server" -> "immich".
NOISE = ("server", "app", "web", "api", "backend", "frontend", "ui", "db",
         "database", "core", "main", "service", "daemon", "worker", "client",
         "proxy", "gateway", "agent", "stack", "container", "docker", "oss",
         "ce", "fpm", "nginx", "alpine", "latest")


def _norm(s):
    """Lowercase, and everything that isn't a letter or digit becomes a hyphen."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _from_image(image):
    """The app's name as an image reference spells it.

    `ghcr.io/immich-app/immich-server:release` -> ['immich-server', 'immich-app'].
    Registry host, tag and digest are dropped; the last path segment is what the
    image is called, and the org before it is often the project's own name.
    """
    ref = (image or "").split("@", 1)[0]
    ref = re.sub(r":[^:/]+$", "", ref)          # tag, but not a registry port
    parts = [p for p in ref.split("/") if p]
    if not parts:
        return []
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]                        # drop the registry host
    out = [_norm(parts[-1])]
    if len(parts) > 1:
        out.append(_norm(parts[-2]))
    return [p for p in out if p]


def _trims(slug):
    """A name, then the same name with its noise words peeled off the ends.

    'immich-server' -> ['immich-server', 'immich']; 'chess-web-1' ->
    ['chess-web-1', 'chess-web', 'chess']. Compose's numeric replica suffix goes
    first, then noise words from either end, never the last word standing.
    """
    parts = [p for p in slug.split("-") if p]
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    out, seen = [], set()

    def add(ps):
        s = "-".join(ps)
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    add(parts)
    cur = list(parts)
    while len(cur) > 1 and cur[-1] in NOISE:
        cur = cur[:-1]
        add(cur)
    while len(cur) > 1 and cur[0] in NOISE:
        cur = cur[1:]
        add(cur)
    return out


def candidates(name, extra=""):
    """Every slug worth trying for this container/unit, best guess first.

    `name` is the container or unit name, `extra` its image or description.
    A systemd unit's `.service` suffix and a snap's `snap.x.y` wrapper are
    stripped, because neither is part of the app's name.
    """
    raw = (name or "").strip()
    raw = re.sub(r"\.(service|socket|timer|target|mount|scope)$", "", raw)
    if raw.startswith("snap."):
        bits = raw.split(".")
        raw = bits[1] if len(bits) > 1 else raw
    out, seen = [], set()
    for src in _trims(_norm(raw)) + [t for c in _from_image(extra) for t in _trims(c)]:
        if src and src not in seen:
            seen.add(src)
            out.append(src)
    return out[:8]


class IconIndex:
    """The catalogue: which slugs exist, and what they're called.

    Two lookups — the slug itself, and the display name flattened to letters and
    digits, so "Actual Budget" is found by `actualbudget` as well as
    `actual-budget`. Empty until a refresh succeeds, and an empty index simply
    resolves everything to None.
    """

    # A concatenated name ("plexmediaserver") is matched by the longest catalogue
    # entry it starts with. Short keys are excluded, and enough of the name has
    # to be left over, so this recognises a real app with words glued on rather
    # than letting "plex" claim every name beginning with those four letters.
    PREFIX_MIN = 4
    PREFIX_REST_MIN = 3
    # The catalogue sometimes spells a name with a version number or a couple of
    # trailing letters: postgres -> postgresql, magicmirror -> magicmirror2. That
    # much is the same app. Anything longer is a different one — "whisper" is not
    # "Whisper Money", "registry" is not "Registry Console" — so the leftover has
    # to be tiny, and the extension has to be the only one, or we show nothing.
    SUFFIX_MAX = 2

    def __init__(self):
        self._slugs = set()
        self._alias = {}
        self._prefix = []
        self._memo = {}
        self.count = 0
        self.fetched_at = 0

    def load(self, rows):
        slugs, alias = set(), {}
        for r in rows or []:
            slug = (r.get("Reference") or "").strip().lower()
            if not SLUG_RE.match(slug) or (r.get("SVG") or "").lower() != "yes":
                continue
            slugs.add(slug)
            alias.setdefault(_alnum(slug), slug)
            alias.setdefault(_alnum(r.get("Name")), slug)
        alias.pop("", None)
        self._slugs, self._alias, self.count = slugs, alias, len(slugs)
        self._prefix = sorted((k for k in alias if len(k) >= self.PREFIX_MIN),
                              key=len, reverse=True)
        self._memo = {}
        return self.count

    def resolve(self, name, extra=""):
        """First candidate the catalogue actually has, or None. No network."""
        if not self._slugs:
            return None
        for c in candidates(name, extra):
            if c in self._slugs:
                return c
            hit = self._alias.get(_alnum(c))
            if hit:
                return hit
        for c in candidates(name, extra):
            if "-" in c:
                continue        # already word-separated: _trims had its chance
            key = _alnum(c)
            hit = self._longest_prefix(key) or self._versioned(key)
            if hit:
                return hit
        return None

    def _versioned(self, key):
        """The catalogue's spelling of this same name, when it differs only by a
        version number or a two-letter tail — and only when it is unambiguous."""
        if len(key) < self.PREFIX_MIN:
            return None
        found = None
        for k in self._alias:
            if not k.startswith(key) or k == key:
                continue
            rest = k[len(key):]
            if len(rest) > self.SUFFIX_MAX and not rest.isdigit():
                continue
            if found is not None:
                return None      # two candidates: refuse to guess
            found = k
        return self._alias[found] if found else None

    def _longest_prefix(self, key):
        if key in self._memo:
            return self._memo[key]
        out = None
        for k in self._prefix:
            if len(key) - len(k) >= self.PREFIX_REST_MIN and key.startswith(k):
                out = self._alias[k]
                break
        self._memo[key] = out
        return out


class IconStore:
    """Index + on-disk SVG cache, under `<data dir>/icons`.

    Refreshes happen on a background thread so nothing in a request path ever
    waits on the network, except the one fetch that a cache miss needs.
    """

    def __init__(self, cache_dir, enabled=lambda: True, opener=None):
        self.dir = cache_dir
        self.index = IconIndex()
        self._enabled = enabled
        self._open = opener or self._http_get
        self._lock = threading.Lock()
        self._refreshing = False

    # ── plumbing ──────────────────────────────────────────────────────────────
    @staticmethod
    def _http_get(url, limit=MAX_SVG):
        """Read at most `limit`+1 bytes, so an oversized body is detected rather
        than silently truncated — which is what a single shared cap did to the
        catalogue: 256 KB of an 862 KB JSON document parses as nothing at all."""
        req = urllib.request.Request(url, headers={"User-Agent": "homelab-monitor"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read(limit + 1)

    def _path(self, slug, suffix=".svg"):
        return os.path.join(self.dir, slug + suffix)

    def _ensure_dir(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            return True
        except OSError:
            return False        # read-only volume: serve whatever is already there

    # ── index ─────────────────────────────────────────────────────────────────
    def load_cached_index(self):
        """Read the catalogue written by an earlier run. This is what makes the
        hub work offline: no network, still every icon it knew about yesterday."""
        try:
            with open(self._path("_index", ".json"), "rb") as f:
                rows = json.loads(f.read().decode("utf-8"))
            self.index.load(rows)
            self.index.fetched_at = os.path.getmtime(self._path("_index", ".json"))
        except FileNotFoundError:
            pass                # first run on this hub; the refresh will fetch it
        except Exception as e:
            _log.debug("icon index cache unreadable: %s", e)
        return self.index.count

    def index_stale(self):
        return (time.time() - self.index.fetched_at) > INDEX_TTL

    def refresh_index(self, force=False):
        """Fetch the catalogue if it's missing or a week old. Returns the count."""
        if not self._enabled() or (not force and not self.index_stale()):
            return self.index.count
        try:
            raw = self._open(INDEX_URL, MAX_INDEX)
            rows = json.loads(raw.decode("utf-8"))
            if len(raw) > MAX_INDEX or not isinstance(rows, list) or not rows:
                return self.index.count
            self.index.load(rows)
            self.index.fetched_at = time.time()
            if self._ensure_dir():
                tmp = self._path("_index", ".json.tmp")
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.replace(tmp, self._path("_index", ".json"))
        except Exception as e:
            # Keep whatever we had; the UI just shows fewer icons. An offline hub
            # hits this on every refresh, so it is debug, not a warning.
            _log.debug("icon index refresh failed: %s", e)
        return self.index.count

    def refresh_index_async(self):
        """Refresh in the background, one at a time, never blocking a caller."""
        with self._lock:
            if self._refreshing or not self._enabled() or not self.index_stale():
                return
            self._refreshing = True

        def run():
            try:
                self.refresh_index()
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=run, name="icon-index", daemon=True).start()

    # ── the icons themselves ──────────────────────────────────────────────────
    def get(self, slug):
        """The SVG for this slug as bytes, or None.

        Disk first, then one fetch. A 404 is remembered for a day so a container
        whose name merely looks like a slug can't turn every page load into an
        outbound request.
        """
        if not SLUG_RE.match(slug or ""):
            return None
        try:
            with open(self._path(slug), "rb") as f:
                return f.read()
        except OSError:
            pass
        if not self._enabled():
            return None
        try:
            if time.time() - os.path.getmtime(self._path(slug, ".miss")) < MISS_TTL:
                return None
        except OSError:
            pass
        try:
            body = self._open(SVG_URL.format(slug=slug), MAX_SVG)
        except Exception as e:
            _log.debug("icon fetch failed for %s: %s", slug, e)
            self._mark_miss(slug)
            return None
        if not body or len(body) > MAX_SVG or b"<svg" not in body[:512].lower():
            self._mark_miss(slug)
            return None
        if self._ensure_dir():
            try:
                tmp = self._path(slug, ".svg.tmp")
                with open(tmp, "wb") as f:
                    f.write(body)
                os.replace(tmp, self._path(slug))
            except OSError:
                pass
        return body

    def _mark_miss(self, slug):
        if not self._ensure_dir():
            return
        try:
            with open(self._path(slug, ".miss"), "wb") as f:
                f.write(b"")
        except OSError:
            pass

    def stats(self):
        cached = 0
        try:
            cached = sum(1 for n in os.listdir(self.dir) if n.endswith(".svg"))
        except OSError:
            pass
        return {"known": self.index.count, "cached": cached,
                "fetched_at": int(self.index.fetched_at) or None}
