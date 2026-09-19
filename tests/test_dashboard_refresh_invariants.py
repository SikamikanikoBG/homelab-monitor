"""CI gate: structural invariants of the dashboard's refresh loop.

The client is a single 745 KB HTML file with no build step and no module
boundaries, so these are source-level assertions rather than unit tests. The
behavioural coverage lives in tests/js/test_refresh_loop.js, which runs the real
functions against a fake clock; this file is the cheap guard that runs inside the
existing Python suite and fails loudly if one of the fixes is edited away.

Each invariant below is a bug that shipped in v0.30.0 and made the charts stop
refreshing while the rest of the page kept moving.
"""
import re
from pathlib import Path

DASHBOARD = Path(__file__).parent.parent / "static" / "dashboard.html"


def _source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    """Slice a top-level `function name(...)` out of the page.

    Top-level declarations start at column 0 and end at the next `}` in column 0,
    which is the whole structure this needs.
    """
    start = src.find(f"\nfunction {name}(")
    assert start >= 0, f"function {name}() not found in static/dashboard.html"
    end = src.find("\n}", start)
    assert end >= 0, f"could not find the end of {name}()"
    return src[start:end + 2]


def test_schedule_history_reschedules_in_finally():
    """A throw out of a synchronous renderer must not kill the refresh chain.

    scheduleHistory() used to call refreshHistory() and then reschedule on the
    next line. renderDiskIo() and renderLocalOnlyNotice() are synchronous, so one
    exception from either skipped the reschedule and the page never refreshed
    again — silently, because the header shows a clock time, not an age.
    """
    body = _function(_source(), "scheduleHistory")
    assert "finally" in body, (
        "scheduleHistory() must reschedule inside a finally block so a throwing "
        "tick cannot end the refresh loop"
    )
    finally_idx = body.index("finally")
    assert "scheduleHistory()" in body[finally_idx:], (
        "the reschedule must be the finally clause, not merely present somewhere"
    )


def test_history_period_consults_the_liveness_watchdog():
    """LIVE_ON tracks connection events, so it can stay true on a dead stream.

    While stuck true the poll stayed on the slow cadence and refreshHistory()'s
    `if(!LIVE_ON) loadFleet()` meant the fleet was never polled at all.
    """
    src = _source()
    assert "\nfunction liveStale(" in src, "the stream-liveness watchdog is missing"
    period = _function(src, "historyPeriod")
    assert "liveStale()" in period, (
        "historyPeriod() must treat a stream that stopped delivering as not live"
    )
    refresh = _function(src, "refreshHistory")
    assert "liveStale()" in refresh, (
        "refreshHistory() must demote LIVE_ON on a stale stream so the fleet poll resumes"
    )


def test_apply_live_merges_into_the_charted_series():
    """The 2s stream must move the charts, not only the live tiles.

    applyLive() used to assign D.now and nothing else, so every chart on the page
    moved only at the history cadence — 60s on the default 6h range, against
    tiles that moved every 2s.
    """
    body = _function(_source(), "applyLive")
    assert "mergeLiveTail(" in body, (
        "applyLive() must fold the live frame into the newest chart bucket"
    )
    assert "mergeGpuTail(" in body, (
        "applyLive() must fold the live frame into the GPU tab's series too"
    )
    assert "paintLiveCharts(" in body, (
        "applyLive() must repaint the visible tab's charts after merging"
    )


def test_vram_tail_is_not_averaged():
    """VRAM is a step metric: averaging the open bucket hides a model load."""
    body = _function(_source(), "mergeLiveTail")
    assert re.search(r"set\('mem',\s*n\.mem_used,\s*'last'\)", body), (
        "the VRAM tail must carry the latest value, not a running mean"
    )
    gpu = _function(_source(), "mergeGpuTail")
    assert "'last'" in gpu, "the GPU VRAM tail must carry the latest value"


def test_mk_reassigns_inline_plugins():
    """Inline plugins close over the payload they were built from.

    The GPU throttle bands close over d.cards, the threshold line over d.hot_c and
    the VRAM capacity line over d.capacity_mb. mk()'s in-place branch reassigned
    labels, datasets and options but never plugins, so those stayed frozen at
    page-load state for the life of the tab while the lines underneath moved.
    """
    body = _function(_source(), "mk")
    assert "plugins" in body, "mk() must account for inline plugins"
    assert re.search(r"newP\.forEach\(\(p,\s*i\)\s*=>\s*Object\.assign\(curP\[i\],\s*p\)\)", body), (
        "mk()'s in-place update must copy the fresh plugin hooks onto the objects "
        "the chart already holds"
    )
    assert "samePlugins" in body, (
        "a changed plugin set must count as a shape change and force a rebuild"
    )


def test_overview_has_no_compute_history_chart():
    """The Overview's compute-history chart was removed in 0.35.0 — it duplicated
    the GPU tab one click away, and the front page is deliberately short now.
    This test replaces the old signature invariant (which existed because that
    chart's labels-only cache key froze it for a whole bucket at a time): if the
    chart ever comes back, it comes back with that invariant re-tested, not by
    accident.
    """
    src = _source()
    assert "\nfunction renderMcChart(" not in src, (
        "renderMcChart() is back on the Overview — restore its value-based cache "
        "signature test (see git history) along with it"
    )
    assert 'id="mc-chartpanel"' not in src
