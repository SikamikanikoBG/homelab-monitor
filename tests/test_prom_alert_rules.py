"""Guards the checked-in Prometheus rule pack (docs/grafana/alerts.rules.yml).

The point of this test is ACCURACY: catch the day someone adds an alert on a
metric HomeLab Monitor doesn't actually export (an "aspirational" metric). It
derives the allow-list of legal ``homelab_*`` metric names straight from the
exporter source (``app.py``) — both the ``prometheus_client`` Gauge families and
the pure-stdlib ``_prom_metric(...)`` families — so the rules can never drift
ahead of the real exporter without this test going red.

It runs with OR without PyYAML: if PyYAML is importable we parse structurally,
otherwise we fall back to a tiny line scanner over the (deliberately simple,
2-space-indented) rule file. No new runtime/test dependency is introduced.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RULES_PATH = os.path.join(ROOT, "docs", "grafana", "alerts.rules.yml")
APP_PATH = os.path.join(ROOT, "app.py")

# Any promql/label token that is NOT a homelab metric — matched as an identifier
# in an expr but legitimately not one of our exported series.
_NON_METRIC_IDENTS = {
    # PromQL functions used in the exprs.
    "on", "group_left", "group_right", "by", "without",
    "rate", "increase", "sum", "count", "max", "min", "avg", "vector",
    # The standard Prometheus per-target liveness sample (not exported by us).
    "up",
    # Label names / literal label values appearing inside {...} selectors.
    "gpu", "vendor", "name", "state", "job", "mountpoint", "check",
    "series", "direction", "currency", "unit", "host", "server", "model",
    "instance", "homelab_monitor",
    "exited", "dead", "restarting", "failed", "active", "inactive",
}


def _exported_metric_names():
    """Allow-list of homelab_* metric names, parsed from app.py.

    Covers both exposition paths: ``Gauge("homelab_...", ...)`` /
    ``Counter(...)`` from prometheus_client, and the pure-stdlib
    ``_prom_metric(out, "homelab_...", ...)`` families.
    """
    src = open(APP_PATH, encoding="utf-8").read()
    names = set(re.findall(r'"(homelab_[a-z0-9_]+)"', src))
    # Recording-rule outputs live in *our* namespace but are produced by the rule
    # pack itself, so allow the colon-style recorded series names too.
    return names


def _rules_document():
    """Return the parsed rule doc as {groups: [...]}, PyYAML or fallback."""
    text = open(RULES_PATH, encoding="utf-8").read()
    try:
        import yaml  # noqa: WPS433 (optional dep — handled below)
    except Exception:
        return _fallback_parse(text), "fallback"
    return list(yaml.safe_load_all(text))[0], "pyyaml"


def _fallback_parse(text):
    """Dependency-free structural read of the (simple) rule file.

    Only understands the shape this repo actually writes: a top-level
    ``groups:`` list, each with ``name`` + a ``rules:`` list whose items carry
    ``alert``/``record``/``expr``/``labels.severity``/``annotations.{summary,
    description}``. Good enough to assert the invariants below without PyYAML.
    """
    groups = []
    cur_group = None
    cur_rule = None
    section = None  # "labels" | "annotations" | None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        m = re.match(r"-\s+name:\s*(.+)$", line)
        if indent <= 2 and m:
            cur_group = {"name": m.group(1).strip(), "rules": []}
            groups.append(cur_group)
            cur_rule = None
            section = None
            continue

        m = re.match(r"-\s+(alert|record):\s*(.+)$", line)
        if m:
            cur_rule = {"labels": {}, "annotations": {}}
            cur_rule[m.group(1)] = m.group(2).strip()
            cur_group["rules"].append(cur_rule)
            section = None
            continue

        if cur_rule is None:
            continue

        m = re.match(r"(alert|record):\s*(.+)$", line)
        if m:
            cur_rule[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r"expr:\s*(.+)$", line)
        if m:
            cur_rule["expr"] = m.group(1).strip()
            continue
        m = re.match(r"for:\s*(.+)$", line)
        if m:
            cur_rule["for"] = m.group(1).strip()
            continue
        if line in ("labels:",):
            section = "labels"
            continue
        if line in ("annotations:",):
            section = "annotations"
            continue
        m = re.match(r"([A-Za-z_]+):\s*(.*)$", line)
        if m and section in ("labels", "annotations"):
            key, val = m.group(1), m.group(2).strip()
            cur_rule[section][key] = val.strip().strip('"')
            continue
    return {"groups": groups}


def _iter_rules(doc):
    for g in doc["groups"]:
        for r in g["rules"]:
            yield g, r


def test_rules_file_exists():
    assert os.path.isfile(RULES_PATH), RULES_PATH


def test_rules_parse_and_have_groups():
    doc, how = _rules_document()
    assert doc["groups"], "no rule groups parsed (%s)" % how
    names = {g["name"] for g in doc["groups"]}
    # The pack ships an alerting group; recording group is a bonus we also ship.
    assert "homelab_alerts" in names, names


def test_every_alert_is_well_formed():
    doc, _ = _rules_document()
    alerts = [r for _, r in _iter_rules(doc) if "alert" in r]
    assert len(alerts) >= 10, "expected a substantial alert pack, got %d" % len(alerts)
    for a in alerts:
        assert a.get("expr"), "alert missing expr: %r" % a
        assert a["labels"].get("severity") in {"info", "warning", "critical"}, a
        assert a["annotations"].get("summary"), "alert %s missing summary" % a.get("alert")
        assert a["annotations"].get("description"), "alert %s missing description" % a.get("alert")


def test_recording_rules_well_formed():
    doc, _ = _rules_document()
    recs = [r for _, r in _iter_rules(doc) if "record" in r]
    for r in recs:
        assert r.get("expr"), "record missing expr: %r" % r
        # Our recorded series follow the name:metric:unit convention.
        assert ":" in r["record"], r["record"]


def test_no_aspirational_metrics():
    """Every homelab_* identifier referenced in a rule expr must be a real
    exported metric (from app.py) — this is the accuracy guard."""
    allowed = _exported_metric_names()
    doc, _ = _rules_document()
    referenced = set()
    for _, r in _iter_rules(doc):
        expr = r.get("expr", "")
        for ident in re.findall(r"homelab_[a-z0-9_]+", expr):
            # Recorded (colon) series never match this homelab_ regex, so any
            # homelab_ token here is a claimed EXPORTED metric.
            referenced.add(ident)
    assert referenced, "no metrics referenced — parser likely broke"
    missing = sorted(referenced - allowed)
    assert not missing, "rules reference metrics not exported by app.py: %s" % missing


def test_no_unknown_bare_identifiers():
    """Belt-and-braces: any non-homelab identifier used at metric position in an
    expr must be a known PromQL keyword / label token — catches typos like a
    stray ``upp`` or a renamed standard series."""
    doc, _ = _rules_document()
    allowed = _exported_metric_names()
    for _, r in _iter_rules(doc):
        expr = r.get("expr", "")
        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr):
            if ident.startswith("homelab_"):
                assert ident in allowed, "unexported metric %s in %r" % (ident, r)
            else:
                assert ident in _NON_METRIC_IDENTS, (
                    "unexpected identifier %r in expr %r — add it to the "
                    "allow-list if legitimate" % (ident, expr)
                )
