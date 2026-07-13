"""Static guardrails for the 🔗 ?tour=<key> deep-link (next_ai).

Browser-free checks that keep the tour deep-link demo-safe:
  • every one of the 13 TOUR_STEPS carries a unique, stable `key` slug
  • the keys are exactly the expected slugs, in the expected step order
  • the deep-link resolver maps a known key -> its step index, and
    '1'/'start'/'' -> 0, and any unknown value -> -1 (no launch)
  • startTour accepts a start index and threads it into _tourGo
  • the load-time parse (maybeDeepLinkTour) is wired and strips ?tour via
    history.replaceState, and never DOM-injects the raw query value
  • the deep-link path does NOT introduce a new mutation of hl_tour_v1

The tour engine (resolve/skip/focus-trap/reduced-motion/mobile) is untouched
and untested here — these are pure markup/wiring invariants. The static-analysis
mirror of _tourIndexForDeepLink below is kept in lockstep with the JS by asserting
the JS source contains the same branch logic.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "static", "dashboard.html")

# Expected key -> index, matching the current 13-step TOUR_STEPS order.
EXPECTED_KEYS = [
    "hero", "fix", "copilot", "forecast", "incidents", "ribbon", "postmortem",
    "advisor", "slo", "diskio", "controls", "status", "integrations",
]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _tour_block(html):
    start = html.index("const TOUR_STEPS=[")
    end = html.index("];", start)
    return html[start:end + 2]


def _parse_keys(html):
    block = _tour_block(html)
    # keys appear as `key:'slug'` at the head of each step literal
    return re.findall(r"key:'([^']+)'", block)


def _index_for(html, raw):
    """Pure-Python mirror of the JS _tourIndexForDeepLink resolver."""
    keys = _parse_keys(html)
    if raw is None:
        return -1
    v = str(raw).strip().lower()
    if v == "" or v == "1" or v == "start":
        return 0
    for i, k in enumerate(keys):
        if k.lower() == v:
            return i
    return -1


class TestStepKeys(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)
        self.keys = _parse_keys(self.html)

    def test_thirteen_keys(self):
        self.assertEqual(len(self.keys), 13,
                         "expected 13 keyed tour steps, found %d" % len(self.keys))

    def test_keys_are_unique(self):
        self.assertEqual(len(self.keys), len(set(self.keys)),
                         "tour step keys must be unique: %r" % self.keys)

    def test_keys_match_expected_order(self):
        self.assertEqual(self.keys, EXPECTED_KEYS,
                         "tour keys drifted from the expected slug order")


class TestDeepLinkResolver(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)

    def test_known_key_resolves_to_correct_index(self):
        for i, key in enumerate(EXPECTED_KEYS):
            self.assertEqual(_index_for(self.html, key), i, key)
            # case-insensitive
            self.assertEqual(_index_for(self.html, key.upper()), i, key)

    def test_start_aliases_resolve_to_zero(self):
        for alias in ("1", "start", "START", "", "  ", " start "):
            self.assertEqual(_index_for(self.html, alias), 0, repr(alias))

    def test_unknown_values_do_not_launch(self):
        for bad in ("bogus", "42", "0", "hero;drop", "<script>", "-1", "ribbonx"):
            self.assertEqual(_index_for(self.html, bad), -1, repr(bad))

    def test_none_does_not_launch(self):
        self.assertEqual(_index_for(self.html, None), -1)


class TestDeepLinkWiring(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)

    def test_resolver_fn_present_with_same_branches(self):
        src = self.html[self.html.index("function _tourIndexForDeepLink("):]
        src = src[:src.index("\n}")]
        # start aliases
        self.assertIn("'1'", src)
        self.assertIn("'start'", src)
        # unknown -> -1
        self.assertIn("return -1", src)
        # matches against the step keys, case-insensitively
        self.assertIn("TOUR_STEPS[k].key", src)
        self.assertIn("toLowerCase()", src)

    def test_starttour_accepts_start_index(self):
        src = self.html[self.html.index("function startTour("):]
        src = src[:src.index("\n}")]
        self.assertIn("startAt", src)
        # clamps into range and defaults to 0
        self.assertIn("startAt<TOUR.steps.length", src)
        # threads the start into the go engine (reuses existing skip logic)
        self.assertIn("_tourGo(start, true)", src)

    def test_maybe_deeplink_wired_on_load(self):
        self.assertIn("maybeDeepLinkTour();", self.html)
        # runs before the first-visit offer
        i_dl = self.html.index("maybeDeepLinkTour();")
        i_off = self.html.rindex("maybeOfferTour();")
        self.assertLess(i_dl, i_off)

    def test_url_is_cleaned_via_replacestate(self):
        src = self.html[self.html.index("function maybeDeepLinkTour("):]
        src = src[:src.index("\n}")]
        self.assertIn("params.delete('tour')", src)
        self.assertIn("history.replaceState", src)
        # unknown value returns before launching
        self.assertIn("if(idx<0) return;", src)

    def test_raw_query_value_never_dom_injected(self):
        # The raw ?tour value flows only into the resolver (lookup) and delete().
        # Assert it is never concatenated into innerHTML/textContent/insertAdjacent.
        src = self.html[self.html.index("function maybeDeepLinkTour("):]
        src = src[:src.index("\n}")]
        for sink in ("innerHTML", "textContent", "insertAdjacent", "outerHTML"):
            self.assertNotIn(sink, src,
                             "deep-link parse must not touch DOM sink %s" % sink)

    def test_deeplink_adds_no_new_tour_key_mutation(self):
        # The deep-link launch must not itself set/clear hl_tour_v1 — it relies on
        # startTour's existing 'seen' arm. Neither helper writes TOUR_KEY directly.
        for fn in ("_tourIndexForDeepLink", "maybeDeepLinkTour"):
            src = self.html[self.html.index("function %s(" % fn):]
            src = src[:src.index("\n}")]
            self.assertNotIn("TOUR_KEY", src, fn)
            self.assertNotIn("hl_tour_v1", src, fn)


if __name__ == "__main__":
    unittest.main()
