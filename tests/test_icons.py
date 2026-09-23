"""App-icon resolution and the hub's icon cache (backend/icons.py).

The resolver is pure, so the interesting cases — a container called
`immich_server` running `ghcr.io/immich-app/immich-server:release`, a snap unit,
compose's `-1` replica suffix — are all table-driven here. The store is exercised
with a fake opener: no test in this file touches the network.
"""
import json
import os
import time

import pytest

from backend.icons import IconIndex, IconStore, candidates, SLUG_RE

ROWS = [
    {"Name": "Immich",        "Reference": "immich",        "SVG": "Yes"},
    {"Name": "Actual Budget", "Reference": "actual-budget", "SVG": "Yes"},
    {"Name": "Navidrome",     "Reference": "navidrome",     "SVG": "Yes"},
    {"Name": "Plex",          "Reference": "plex",          "SVG": "Yes"},
    {"Name": "Home Assistant", "Reference": "home-assistant", "SVG": "Yes"},
    {"Name": "PostgreSQL",    "Reference": "postgresql",   "SVG": "Yes"},
    {"Name": "MagicMirror2",  "Reference": "magicmirror2", "SVG": "Yes"},
    {"Name": "Whisper Money", "Reference": "whispermoney", "SVG": "Yes"},
    {"Name": "Registry Console", "Reference": "registryconsole", "SVG": "Yes"},
    {"Name": "PNG Only",      "Reference": "png-only",      "SVG": "No"},
    {"Name": "Bad Slug",      "Reference": "../etc/passwd", "SVG": "Yes"},
]


@pytest.fixture
def index():
    ix = IconIndex()
    ix.load(ROWS)
    return ix


# ── candidate generation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name,extra,expected", [
    # the plain case, and the name that needs its noise word peeled off
    ("immich", "", "immich"),
    ("immich_server", "", "immich"),
    # compose's replica suffix, then the noise word underneath it
    ("immich_server_1", "", "immich"),
    # nothing in the name, everything in the image
    ("web", "ghcr.io/immich-app/immich-server:release", "immich"),
    # the org segment carries the name when the image itself doesn't
    ("app", "docker.io/plexinc/plex:latest", "plex"),
    # display name with a space, asked for as one word and as two
    ("actualbudget", "", "actual-budget"),
    ("actual-budget", "", "actual-budget"),
    # a systemd unit, and a snap-wrapped one whose name is three words glued
    # together — "plexmediaserver" is recognised by the app it starts with
    ("navidrome.service", "", "navidrome"),
    ("snap.plexmediaserver.plexmediaserver.service", "", "plex"),
    # the catalogue spells it with a suffix: postgres -> postgresql, and the
    # version digit in magicmirror2
    ("langfuse-stack-postgres-1", "docker.io/postgres:17", "postgresql"),
    ("magicmirror", "karsten13/magicmirror:latest", "magicmirror2"),
    # a registry with a port must not be mistaken for a tag
    ("x", "registry.local:5000/navidrome:1.2", "navidrome"),
])
def test_resolves(index, name, extra, expected):
    assert index.resolve(name, extra) == expected


@pytest.mark.parametrize("name,extra", [
    ("totally-made-up-thing", ""),
    # "Whisper Money" and "Registry Console" are different apps that merely start
    # the same way — a wrong logo is worse than none, so these stay blank
    ("whisper", ""),
    ("registry", "registry:3"),
    ("", ""),
    ("png-only", ""),          # in the catalogue, but has no SVG
])
def test_no_match_is_none(index, name, extra):
    assert index.resolve(name, extra) is None


def test_candidates_are_bounded_and_unique():
    c = candidates("a-b-c-d-e-f-g-h-i-j-server-1", "ghcr.io/org/a-b-c-d:tag")
    assert len(c) <= 8
    assert len(c) == len(set(c))


def test_last_word_is_never_peeled_away():
    # "server" is noise, but a container literally called that still resolves to
    # something rather than to the empty string.
    assert candidates("server")[0] == "server"


def test_ambiguous_extension_is_refused():
    """Two catalogue entries extend the same name: we show nothing rather than
    pick one. (pg -> pgadmin / pgbackweb: there is no right answer here.)"""
    ix = IconIndex()
    ix.load([{"Name": "PGAdmin", "Reference": "pgadmin", "SVG": "Yes"},
             {"Name": "PGBackWeb", "Reference": "pgbackweb", "SVG": "Yes"}])
    assert ix.resolve("pgad") is None


def test_index_rejects_unsafe_slugs(index):
    # '../etc/passwd' is in ROWS; it must not have survived into the catalogue.
    assert index.count == 9
    for slug in index._slugs:
        assert SLUG_RE.match(slug)


def test_empty_index_resolves_nothing():
    assert IconIndex().resolve("immich", "") is None


# ── the store ─────────────────────────────────────────────────────────────────

class FakeNet:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def __call__(self, url, limit=None):
        self.calls.append(url)
        if url in self.files:
            return self.files[url]
        raise OSError("404")


def _store(tmp_path, files, enabled=True):
    net = FakeNet(files)
    st = IconStore(str(tmp_path / "icons"), enabled=lambda: enabled, opener=net)
    return st, net


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
IMMICH = "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/immich.svg"
INDEX = "https://cdn.jsdelivr.net/gh/selfhst/icons/index.json"


def test_get_fetches_once_then_serves_from_disk(tmp_path):
    st, net = _store(tmp_path, {IMMICH: SVG})
    assert st.get("immich") == SVG
    assert st.get("immich") == SVG
    assert len(net.calls) == 1                      # second read came off disk
    assert (tmp_path / "icons" / "immich.svg").read_bytes() == SVG


def test_a_miss_is_remembered(tmp_path):
    st, net = _store(tmp_path, {})
    assert st.get("nope") is None
    assert st.get("nope") is None
    assert len(net.calls) == 1                      # not re-asked within the TTL


def test_miss_is_retried_once_the_ttl_lapses(tmp_path):
    st, net = _store(tmp_path, {})
    assert st.get("immich") is None
    old = time.time() - 86400 - 60
    os.utime(tmp_path / "icons" / "immich.miss", (old, old))
    net.files[IMMICH] = SVG
    assert st.get("immich") == SVG


def test_junk_response_is_not_cached_as_an_icon(tmp_path):
    st, _ = _store(tmp_path, {IMMICH: b"<html>404 not found</html>"})
    assert st.get("immich") is None
    assert not (tmp_path / "icons" / "immich.svg").exists()


def test_oversized_response_is_refused(tmp_path):
    st, _ = _store(tmp_path, {IMMICH: b"<svg" + b"x" * (256 * 1024)})
    assert st.get("immich") is None


@pytest.mark.parametrize("slug", ["../etc/passwd", "a/b", "UPPER", "", "x" * 80, "immich.svg"])
def test_unsafe_slugs_never_reach_the_disk_or_the_network(tmp_path, slug):
    st, net = _store(tmp_path, {IMMICH: SVG})
    assert st.get(slug) is None
    assert net.calls == []


def test_disabled_store_does_not_fetch(tmp_path):
    st, net = _store(tmp_path, {IMMICH: SVG}, enabled=False)
    assert st.get("immich") is None
    assert net.calls == []


def test_disabled_store_still_serves_what_it_already_has(tmp_path):
    st, _ = _store(tmp_path, {IMMICH: SVG})
    st.get("immich")
    off, net = _store(tmp_path, {}, enabled=False)
    assert off.get("immich") == SVG                 # cached icons keep working
    assert net.calls == []


def test_index_is_cached_and_reloads_without_network(tmp_path):
    st, net = _store(tmp_path, {INDEX: json.dumps(ROWS).encode()})
    assert st.refresh_index(force=True) == 9
    cold, cold_net = _store(tmp_path, {})
    assert cold.load_cached_index() == 9
    assert cold.index.resolve("immich_server") == "immich"
    assert cold_net.calls == []


def test_index_refresh_is_skipped_while_fresh(tmp_path):
    st, net = _store(tmp_path, {INDEX: json.dumps(ROWS).encode()})
    st.refresh_index(force=True)
    st.refresh_index()
    assert len(net.calls) == 1


def test_a_failed_refresh_keeps_the_previous_catalogue(tmp_path):
    st, net = _store(tmp_path, {INDEX: json.dumps(ROWS).encode()})
    st.refresh_index(force=True)
    net.files.clear()
    st.index.fetched_at = 0
    assert st.refresh_index() == 9
    assert st.index.resolve("plex") == "plex"


def test_index_is_not_truncated_by_the_svg_size_cap(tmp_path):
    """One shared read cap turned the 862 KB catalogue into 256 KB of broken
    JSON — every icon silently missing. The index gets its own, larger limit."""
    big = json.dumps(ROWS + [{"Name": "Pad %d" % i, "Reference": "pad-%d" % i,
                              "SVG": "Yes"} for i in range(9000)]).encode()
    assert len(big) > 256 * 1024
    st, _ = _store(tmp_path, {INDEX: big})
    assert st.refresh_index(force=True) == 9009


def test_garbage_index_is_ignored(tmp_path):
    st, _ = _store(tmp_path, {INDEX: b"not json"})
    assert st.refresh_index(force=True) == 0
    assert st.index.resolve("immich") is None


def test_stats_report_what_is_held(tmp_path):
    st, _ = _store(tmp_path, {INDEX: json.dumps(ROWS).encode(), IMMICH: SVG})
    st.refresh_index(force=True)
    st.get("immich")
    s = st.stats()
    assert s["known"] == 9 and s["cached"] == 1 and s["fetched_at"]


def test_read_only_cache_dir_still_serves(tmp_path, monkeypatch):
    st, _ = _store(tmp_path, {IMMICH: SVG})
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert st.get("immich") == SVG                  # fetched and served, just not kept
