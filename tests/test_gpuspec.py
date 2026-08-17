"""The TFLOPS spec table and the lookup that reads it.

The whole feature rests on matching a card name to the right row. A wrong match
is worse than no match at all — it puts a confident, plausible number on the
dashboard for hardware the user doesn't own — so most of what is tested here is
the *refusal* to match: mobile parts, prefixes of longer model names, and cards
that simply aren't in the table.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import gpuspec


class TestNameMatching(unittest.TestCase):
    def test_the_fleet_this_was_built_on(self):
        """The two cards actually in the author's rack must resolve."""
        self.assertEqual(gpuspec.lookup("NVIDIA GeForce RTX 3090")["model"], "rtx 3090")
        self.assertEqual(gpuspec.lookup("Quadro P2000")["model"], "quadro p2000")

    def test_vendor_words_are_noise(self):
        for name in ("NVIDIA GeForce RTX 4090", "GeForce RTX 4090", "RTX 4090",
                     "NVIDIA GeForce RTX 4090 24GB"):
            self.assertEqual(gpuspec.lookup(name)["model"], "rtx 4090", name)

    def test_a_longer_model_never_answers_as_its_prefix(self):
        """'RTX 4080 SUPER' contains 'RTX 4080'. The table is scanned longest
        key first precisely so the SUPER row wins; getting this backwards would
        under-report a card by ~7%."""
        self.assertEqual(gpuspec.lookup("NVIDIA GeForce RTX 4080 SUPER")["model"],
                         "rtx 4080 super")
        self.assertEqual(gpuspec.lookup("NVIDIA GeForce RTX 4080")["model"], "rtx 4080")
        self.assertEqual(gpuspec.lookup("NVIDIA GeForce RTX 3090 Ti")["model"], "rtx 3090 ti")
        self.assertEqual(gpuspec.lookup("NVIDIA RTX 5000 Ada Generation")["model"],
                         "rtx 5000 ada")

    def test_short_keys_need_word_boundaries(self):
        """'l4' must not be found inside 'L40S', and 'a100' must not be found
        inside a name that merely contains the digits."""
        self.assertEqual(gpuspec.lookup("NVIDIA L40S")["model"], "l40s")
        self.assertEqual(gpuspec.lookup("NVIDIA L4")["model"], "l4")
        self.assertEqual(gpuspec.lookup("NVIDIA A100-SXM4-80GB")["model"], "a100")
        self.assertEqual(gpuspec.lookup("NVIDIA H100 PCIe")["model"], "h100 pcie")
        self.assertEqual(gpuspec.lookup("NVIDIA H100 80GB HBM3")["model"], "h100")
        # A10G is a different card from an A10 and is not in the table.
        self.assertIsNone(gpuspec.lookup("NVIDIA A10G"))

    def test_mobile_parts_are_refused_not_guessed(self):
        """An 'RTX 4090 Laptop GPU' is 9728 cores, not 16384. Answering it with
        the desktop row would overstate the machine by 70%."""
        for name in ("NVIDIA GeForce RTX 4090 Laptop GPU",
                     "NVIDIA GeForce RTX 3080 Ti Laptop GPU",
                     "NVIDIA GeForce RTX 3060 Mobile",
                     "NVIDIA RTX A4000 Max-Q"):
            self.assertIsNone(gpuspec.lookup(name), name)

    def test_unknown_and_placeholder_names_return_nothing(self):
        for name in (None, "", "GPU 1", "AMD GPU 0", "Some Future Card 9000",
                     "llvmpipe (LLVM 15.0.7, 256 bits)"):
            self.assertIsNone(gpuspec.lookup(name), repr(name))

    def test_amd_and_intel_resolve_too(self):
        self.assertEqual(gpuspec.lookup("AMD Radeon RX 7900 XTX")["model"], "rx 7900 xtx")
        self.assertEqual(gpuspec.lookup("AMD Instinct MI300X")["model"], "instinct mi300x")
        self.assertEqual(gpuspec.lookup("Intel Arc A770 Graphics")["model"], "arc a770")


class TestTableSanity(unittest.TestCase):
    def test_every_row_is_well_formed(self):
        for key, (cores, boost, fp32, fp16) in gpuspec.SPECS.items():
            self.assertEqual(key, gpuspec._norm(key), f"{key} is not in normalised form")
            self.assertGreater(cores, 0, key)
            self.assertGreater(boost, 0, key)
            self.assertGreater(fp32, 0, key)
            if fp16 is not None:
                self.assertGreaterEqual(fp16, fp32, f"{key}: FP16 tensor below FP32 vector")

    def test_fp32_matches_cores_times_clock_where_the_formula_applies(self):
        """2 × cores × clock is the definition, and it must reproduce the quoted
        figure for every architecture that isn't dual-issue. RDNA 3 and CDNA are
        exempt — AMD quotes the dual-issue rate, which is exactly why the table
        stores published TFLOPS instead of deriving them.
        """
        dual_issue = ("rx 9070", "rx 9070 xt", "rx 7900 xtx", "rx 7900 xt", "rx 7900 gre",
                      "rx 7800 xt", "rx 7700 xt", "rx 7600",
                      "instinct mi100", "instinct mi210", "instinct mi250x",
                      "instinct mi300x", "arc a770", "arc a750", "arc b580")
        for key, (cores, boost, fp32, _fp16) in gpuspec.SPECS.items():
            if key in dual_issue:
                continue
            derived = 2 * cores * boost / 1e6
            self.assertAlmostEqual(
                derived, fp32, delta=max(0.35, fp32 * 0.02),
                msg=f"{key}: 2×{cores}×{boost}MHz = {derived:.1f}, table says {fp32}")


class TestComputeBlock(unittest.TestCase):
    def test_peak_and_live_are_different_numbers(self):
        """An idle 3090 parked in P8 at 210 MHz is nowhere near its 35.6 TFLOPS
        box figure, and saying so is the entire point of publishing both."""
        c = gpuspec.compute_for("NVIDIA GeForce RTX 3090", clk_sm=210)
        self.assertEqual(c["fp32"], 35.6)
        self.assertEqual(c["fp16"], 71.0)
        self.assertAlmostEqual(c["fp32_now"], 35.6 * 210 / 1695, places=1)
        self.assertLess(c["fp32_now"], 5)

    def test_at_boost_the_live_figure_is_the_rated_figure(self):
        c = gpuspec.compute_for("NVIDIA GeForce RTX 3090", clk_sm=1695)
        self.assertAlmostEqual(c["fp32_now"], 35.6, places=1)

    def test_no_clock_means_no_live_figure_not_the_boost_one(self):
        c = gpuspec.compute_for("NVIDIA GeForce RTX 3090")
        self.assertNotIn("fp32_now", c)
        self.assertEqual(c["fp32"], 35.6)

    def test_a_card_without_tensor_cores_omits_fp16(self):
        """Pascal has no tensor cores. An FP16 figure here would invent hardware."""
        c = gpuspec.compute_for("Quadro P2000", clk_sm=1480)
        self.assertNotIn("fp16", c)
        self.assertAlmostEqual(c["fp32_now"], 3.0, places=1)

    def test_unknown_card_gets_nothing(self):
        self.assertIsNone(gpuspec.compute_for("Some Future Card 9000", clk_sm=2000))


class TestAttach(unittest.TestCase):
    def test_only_recognised_cards_gain_the_key(self):
        cards = [{"idx": 0, "name": "NVIDIA GeForce RTX 3090", "clk_sm": 1695},
                 {"idx": 1, "name": "GPU 1"}]
        gpuspec.attach(cards)
        self.assertIn("compute", cards[0])
        self.assertNotIn("compute", cards[1])

    def test_the_live_figure_follows_the_clock_on_re_attach(self):
        """The fast lane mutates the same card dicts every couple of seconds. A
        block cached on first sight would freeze fp32_now at the idle clock."""
        cards = [{"idx": 0, "name": "NVIDIA GeForce RTX 3090", "clk_sm": 210}]
        gpuspec.attach(cards)
        idle = cards[0]["compute"]["fp32_now"]
        cards[0]["clk_sm"] = 1695
        gpuspec.attach(cards)
        self.assertGreater(cards[0]["compute"]["fp32_now"], idle * 5)

    def test_a_card_that_stops_being_recognised_loses_the_block(self):
        cards = [{"idx": 0, "name": "NVIDIA GeForce RTX 3090", "clk_sm": 1695}]
        gpuspec.attach(cards)
        cards[0]["name"] = "GPU 0"
        gpuspec.attach(cards)
        self.assertNotIn("compute", cards[0])

    def test_survives_junk(self):
        self.assertEqual(gpuspec.attach(None), None)
        self.assertEqual(gpuspec.attach([None, "x"]), [None, "x"])


class TestPooled(unittest.TestCase):
    def _cards(self, *names, clk=None):
        cards = [{"idx": i, "name": n, **({"clk_sm": clk} if clk else {})}
                 for i, n in enumerate(names)]
        return gpuspec.attach(cards)

    def test_three_of_the_same_card_sum(self):
        p = gpuspec.pooled(self._cards(*["NVIDIA GeForce RTX 3090"] * 3, clk=1695))
        self.assertAlmostEqual(p["fp32"], 106.8, places=1)
        self.assertAlmostEqual(p["fp16"], 213.0, places=1)
        self.assertEqual(p["cards_known"], 3)
        self.assertEqual(p["cards_total"], 3)
        self.assertEqual(p["cores"], 31488)

    def test_an_unrecognised_card_is_counted_but_not_summed(self):
        """Reporting a partial sum as the whole machine is the failure mode; the
        counts are what let the UI say 'of the cards we recognise'."""
        p = gpuspec.pooled(self._cards("NVIDIA GeForce RTX 3090", "Some Future Card", clk=1695))
        self.assertAlmostEqual(p["fp32"], 35.6, places=1)
        self.assertEqual(p["cards_known"], 1)
        self.assertEqual(p["cards_total"], 2)

    def test_a_mixed_pool_publishes_no_fp16(self):
        """A 3090 next to a Pascal Quadro: the box has no single FP16 tensor
        figure, and the 3090's alone would credit the whole machine with it."""
        p = gpuspec.pooled(self._cards("NVIDIA GeForce RTX 3090", "Quadro P2000", clk=1000))
        self.assertNotIn("fp16", p)
        self.assertGreater(p["fp32"], 0)

    def test_a_card_with_no_clock_suppresses_the_pooled_live_figure(self):
        cards = [{"idx": 0, "name": "NVIDIA GeForce RTX 3090", "clk_sm": 1695},
                 {"idx": 1, "name": "NVIDIA GeForce RTX 3090"}]
        gpuspec.attach(cards)
        p = gpuspec.pooled(cards)
        self.assertNotIn("fp32_now", p)
        self.assertAlmostEqual(p["fp32"], 71.2, places=1)

    def test_no_recognised_cards_pools_to_nothing(self):
        self.assertIsNone(gpuspec.pooled([{"idx": 0, "name": "GPU 0"}]))
        self.assertIsNone(gpuspec.pooled([]))
        self.assertIsNone(gpuspec.pooled(None))


if __name__ == "__main__":
    unittest.main()
