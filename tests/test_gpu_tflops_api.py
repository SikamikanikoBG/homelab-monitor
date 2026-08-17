"""TFLOPS as the API publishes it — /api/gpu/history and /api/data.

The spec table itself is covered in test_gpuspec.py. What is pinned here is the
wiring: that the figure reaches every host through the one shared endpoint, that
the derived series follows the clock history already in the database, and above
all that an unrecognised card produces *absence* rather than a confident zero —
the same rule the cockpit applies to every other optional metric.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _client():
    import app
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _wipe():
    import app
    for t in ("gpu_samples", "gpu_samples_1h", "proc"):
        app.DB.execute(f"DELETE FROM {t}")
    app.DB.commit()


def _card(idx=0, name="NVIDIA GeForce RTX 3090", **kw):
    g = {"idx": idx, "name": name, "vendor": "nvidia", "util": 50,
         "mem_used": 12000, "mem_total": 24576, "power": 200, "temp": 70,
         "clk_sm": 1695}
    g.update(kw)
    return g


def _seed(host, cards_at):
    import app
    from backend.db.repos import gpu_samples as repo
    for ts, cards in cards_at:
        repo.record(app.DB, ts, host, cards, interval=10)
    app.DB.commit()


def _live(host, cards):
    import app
    return mock.patch.dict(
        app.HOST_DATA,
        {host: {"data": {"host": {"gpus": cards}}, "at": int(time.time()), "fails": 0}},
        clear=True)


class TestPerCardCompute(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_a_recognised_card_publishes_its_rated_peak(self):
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0)]) for t in range(60, 0, -10)])
        with _live("vader", [_card(0)]):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        k = d["cards"][0]["compute"]
        self.assertEqual(k["fp32"], 35.6)
        self.assertEqual(k["fp16"], 71.0)
        self.assertEqual(k["cores"], 10496)
        self.assertTrue(d["cards"][0]["supports"]["tflops"])

    def test_an_unrecognised_card_publishes_absence_not_zero(self):
        """The whole failure mode this guards: a blank column is a fact, a 0.0
        TFLOPS column is a lie about hardware that works fine."""
        now = int(time.time())
        odd = _card(0, name="Whizzo Graphics 9000")
        _seed("vader", [(now - t, [odd]) for t in range(60, 0, -10)])
        with _live("vader", [odd]):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        card = d["cards"][0]
        self.assertIsNone(card["compute"])
        self.assertFalse(card["supports"]["tflops"])
        self.assertTrue(all(v is None for v in card["series"]["tflops"]))

    def test_a_driver_with_no_clock_reports_no_tflops_series(self):
        """clk_sm is what the series is derived from. Without it there is a peak
        but no history, and claiming otherwise would draw the boost figure as a
        flat line across an idle night."""
        now = int(time.time())
        noclk = _card(0)
        noclk.pop("clk_sm")
        _seed("vader", [(now - t, [noclk]) for t in range(60, 0, -10)])
        with _live("vader", [noclk]):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        card = d["cards"][0]
        self.assertFalse(card["supports"]["tflops"])
        self.assertNotIn("fp32_now", card["compute"])
        self.assertEqual(card["compute"]["fp32"], 35.6)


class TestDerivedSeries(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_the_series_tracks_the_clock_history(self):
        """Idle at 210 MHz then boosting to 1695 must show up as a ~8× step —
        it is derived from clk_sm, which the database already had."""
        now = int(time.time())
        _seed("vader", [(now - t, [_card(0, clk_sm=210)]) for t in range(600, 300, -10)])
        _seed("vader", [(now - t, [_card(0, clk_sm=1695)]) for t in range(300, 0, -10)])
        with _live("vader", [_card(0)]):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        vals = [v for v in d["cards"][0]["series"]["tflops"] if v is not None]
        self.assertGreater(len(vals), 1)
        self.assertLess(min(vals), 6)
        self.assertGreater(max(vals), 30)

    def test_combined_sums_the_cards_rather_than_averaging_them(self):
        """A box's FLOP/s ceiling is what all its cards do at once. Averaging
        would report a 3×3090 rig as a single 3090."""
        now = int(time.time())
        cards = [_card(0), _card(1), _card(2)]
        _seed("vader", [(now - t, cards) for t in range(60, 0, -10)])
        with _live("vader", cards):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        vals = [v for v in d["combined"]["tflops"] if v is not None]
        self.assertTrue(vals)
        self.assertAlmostEqual(max(vals), 106.8, delta=1.0)

    def test_sub_tflops_values_are_not_rounded_away(self):
        """A Quadro P2000 idling at 139 MHz is 0.28 TFLOPS. Integer rounding
        would draw that as a flat zero line for the whole night."""
        now = int(time.time())
        p2000 = _card(0, name="Quadro P2000", mem_total=5120, clk_sm=139)
        _seed("local", [(now - t, [p2000]) for t in range(60, 0, -10)])
        import app
        with mock.patch.dict(app.LATEST, {"gpus": [p2000]}):
            d = self.c.get("/api/gpu/history?host=local&range=1h").get_json()
        vals = [v for v in d["cards"][0]["series"]["tflops"] if v is not None]
        self.assertTrue(vals)
        self.assertGreater(max(vals), 0)
        self.assertLess(max(vals), 1)


class TestPooled(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        _wipe()

    def test_the_box_figure_is_the_sum_of_its_cards(self):
        now = int(time.time())
        cards = [_card(0), _card(1), _card(2)]
        _seed("vader", [(now - t, cards) for t in range(60, 0, -10)])
        with _live("vader", cards):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        k = d["now_pooled"]["compute"]
        self.assertAlmostEqual(k["fp32"], 106.8, places=1)
        self.assertAlmostEqual(k["fp16"], 213.0, places=1)
        self.assertEqual(k["cards_known"], 3)
        self.assertEqual(k["cards_total"], 3)

    def test_a_box_with_no_recognised_card_has_no_compute_block(self):
        now = int(time.time())
        odd = _card(0, name="Whizzo Graphics 9000")
        _seed("vader", [(now - t, [odd]) for t in range(60, 0, -10)])
        with _live("vader", [odd]):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        self.assertNotIn("compute", d["now_pooled"])

    def test_a_mixed_box_says_how_many_cards_it_recognised(self):
        now = int(time.time())
        cards = [_card(0), _card(1, name="Whizzo Graphics 9000")]
        _seed("vader", [(now - t, cards) for t in range(60, 0, -10)])
        with _live("vader", cards):
            d = self.c.get("/api/gpu/history?host=vader&range=1h").get_json()
        k = d["now_pooled"]["compute"]
        self.assertEqual((k["cards_known"], k["cards_total"]), (1, 2))
        self.assertAlmostEqual(k["fp32"], 35.6, places=1)


class TestLivePayload(unittest.TestCase):
    """/api/data is what the AI Models tab reads — its cards must carry compute
    too, or the Compute column is blank on the one tab that most wants it."""

    def test_api_data_cards_carry_compute(self):
        import app
        c = _client()
        with mock.patch.dict(app.LATEST, {"gpus": [_card(0), _card(1, name="GPU 1")]}):
            d = c.get("/api/data?range=1h").get_json()
        gpus = d["now"]["gpus"]
        self.assertEqual(gpus[0]["compute"]["fp32"], 35.6)
        self.assertNotIn("compute", gpus[1])

    def test_a_gpu_less_hub_still_serves_api_data(self):
        import app
        c = _client()
        with mock.patch.dict(app.LATEST, {"gpus": []}):
            self.assertEqual(c.get("/api/data?range=1h").status_code, 200)


if __name__ == "__main__":
    unittest.main()
