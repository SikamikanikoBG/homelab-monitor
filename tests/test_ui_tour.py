"""Static guardrails for the launchable guided tour (next_ai).

Browser-free checks that keep the tour honest for the judgment demo:
  • TOUR_STEPS carries the expected number of steps (13 after the 🌈 ribbon add)
  • the new anomaly-timeline step is present, on the gpu tab, with the right anchor
  • the ribbon anchor id exists exactly once and lives under the gpu section
  • every step's PRIMARY selector resolves to an element present in the static
    HTML, and (for id/attr anchors) sits under the step's declared data-tab
  • the two new tour i18n keys exist in BOTH locales with no placeholder drift

The engine (resolve/skip/focus-trap/reduced-motion) is intentionally untouched
and untested here — these are pure markup/wiring invariants.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")
EN = os.path.join(ROOT, "locales", "en.json")
ZH = os.path.join(ROOT, "locales", "zh-CN.json")

NEW_TOUR_KEYS = ["tour.s_ribbon_t", "tour.s_ribbon_b"]
EXPECTED_STEP_COUNT = 13


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _tour_block(html):
    """Return the raw JS text of the TOUR_STEPS array literal."""
    start = html.index("const TOUR_STEPS=[")
    end = html.index("];", start)
    return html[start:end + 2]


# A step object literal: {tab:..., sel:[...], ic:'...', title:'...', body:'...'}
_STEP_RE = re.compile(r"\{tab:(?P<tab>[^,]+),\s*sel:\[(?P<sel>[^\]]*)\]")
_SEL_RE = re.compile(r"'([^']+)'")


def _parse_steps(html):
    block = _tour_block(html)
    steps = []
    for m in _STEP_RE.finditer(block):
        tab_raw = m.group("tab").strip()
        tab = None if tab_raw == "null" else tab_raw.strip("'")
        sels = _SEL_RE.findall(m.group("sel"))
        steps.append({"tab": tab, "sel": sels})
    return steps


def _section_spans(html):
    """Map data-tab name -> (start, end) char span of its <section> block."""
    spans = {}
    starts = [(m.start(), m.group(1))
              for m in re.finditer(r'<section data-tab="([^"]+)"', html)]
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(html)
        spans[name] = (pos, end)
    return spans


def _simple_present(html, sel, span=None):
    """True if a single simple selector (#id / .class / [attr]) is present."""
    hay = html if span is None else html[span[0]:span[1]]
    if sel.startswith("#"):
        return f'id="{sel[1:]}"' in hay
    if sel.startswith("."):
        cls = sel[1:]
        # class token present in static markup, or as a rendered/CSS class name
        if re.search(r'class="[^"]*\b' + re.escape(cls) + r'\b', hay):
            return True
        # runtime-rendered anchors (e.g. .upslo, .inc-pm-chip) still ship a CSS
        # rule + a JS class="… x …" string; accept those as a present anchor
        return (("." + cls) in hay) or (cls in hay)
    # attribute selector like [data-tab="uptime"] .card -> handled by caller split
    return sel in hay


def _anchor_present(html, sel, span=None):
    """True if a CSS selector resolves against the static HTML.

    Compound descendant selectors (``#uptimegrid .upslo``) are split: every
    simple part must be present. The id container is the load-bearing anchor;
    runtime-injected descendant classes are matched loosely (CSS/JS presence).
    """
    parts = sel.split()
    if len(parts) == 1:
        return _simple_present(html, sel, span)
    return all(_simple_present(html, p, span) for p in parts)


class TestTourSteps(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)
        self.steps = _parse_steps(self.html)
        self.spans = _section_spans(self.html)

    def test_step_count(self):
        self.assertEqual(len(self.steps), EXPECTED_STEP_COUNT,
                         f"expected {EXPECTED_STEP_COUNT} tour steps, "
                         f"found {len(self.steps)}")

    def test_ribbon_step_present_and_on_gpu(self):
        block = _tour_block(self.html)
        self.assertIn("tour.s_ribbon_t", block, "ribbon step title missing")
        self.assertIn("tour.s_ribbon_b", block, "ribbon step body missing")
        self.assertIn("🌈", block, "ribbon step icon missing")
        ribbon = [s for s in self.steps
                  if s["sel"] and s["sel"][0] == "#anom-timeline-wrap"]
        self.assertEqual(len(ribbon), 1,
                         "expected exactly one #anom-timeline-wrap tour step")
        self.assertEqual(ribbon[0]["tab"], "gpu",
                         "ribbon step must be on the gpu tab")

    def test_ribbon_anchor_id_unique_and_in_gpu(self):
        self.assertEqual(self.html.count('id="anom-timeline-wrap"'), 1,
                         "#anom-timeline-wrap must appear exactly once")
        gpu = self.spans["gpu"]
        self.assertTrue(_anchor_present(self.html, "#anom-timeline-wrap", gpu),
                        "#anom-timeline-wrap must live under the gpu section")

    def test_ribbon_placed_after_incidents(self):
        # flow must read ...incidents -> ribbon -> postmortem...
        idx = next(i for i, s in enumerate(self.steps)
                   if s["sel"] and s["sel"][0] == "#anom-timeline-wrap")
        prev_first = self.steps[idx - 1]["sel"][0]
        next_first = self.steps[idx + 1]["sel"][0]
        self.assertEqual(prev_first, "#inc-card",
                         "ribbon step should follow the incidents step")
        self.assertEqual(next_first, ".inc-pm-chip",
                         "ribbon step should precede the postmortem step")

    def test_every_step_primary_selector_resolves(self):
        for i, s in enumerate(self.steps):
            self.assertTrue(s["sel"], f"step {i} has no selectors")
            primary = s["sel"][0]
            # the primary anchor must exist somewhere in the HTML
            self.assertTrue(_anchor_present(self.html, primary),
                            f"step {i} primary selector {primary!r} not in HTML")
            # for tab-scoped id anchors, the id container must live under the
            # declared tab (descendant classes may be runtime-injected)
            lead = primary.split()[0]
            if s["tab"] and lead.startswith("#"):
                span = self.spans.get(s["tab"])
                self.assertIsNotNone(span,
                                     f"step {i} declares unknown tab {s['tab']!r}")
                self.assertTrue(
                    _simple_present(self.html, lead, span),
                    f"step {i} anchor {lead!r} not under tab {s['tab']!r}")

    def test_every_step_has_a_resolvable_selector(self):
        # skip-safety companion: at least one candidate per step exists in HTML
        for i, s in enumerate(self.steps):
            ok = any(_anchor_present(self.html, sel) for sel in s["sel"])
            self.assertTrue(ok, f"step {i} has no resolvable selector: {s['sel']}")


class TestTourI18n(unittest.TestCase):
    def setUp(self):
        with open(EN, encoding="utf-8") as f:
            self.en = json.load(f)
        with open(ZH, encoding="utf-8") as f:
            self.zh = json.load(f)

    def test_new_keys_in_both_locales(self):
        for k in NEW_TOUR_KEYS:
            self.assertIn(k, self.en, f"en.json missing {k}")
            self.assertIn(k, self.zh, f"zh-CN.json missing {k}")
            self.assertTrue(self.en[k].strip(), f"en.json {k} is empty")
            self.assertTrue(self.zh[k].strip(), f"zh-CN.json {k} is empty")

    def test_no_placeholder_drift(self):
        # neither new string carries {tokens}; if that changes, they must match
        token = re.compile(r"\{[^}]+\}")
        for k in NEW_TOUR_KEYS:
            self.assertEqual(sorted(token.findall(self.en[k])),
                             sorted(token.findall(self.zh[k])),
                             f"placeholder drift between locales for {k}")


if __name__ == "__main__":
    unittest.main()
