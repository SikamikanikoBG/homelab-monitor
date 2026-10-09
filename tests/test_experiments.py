"""Unit tests for the Experiments tab: training-run detection (/proc cmdline) and
GPU activity-session reconstruction from power/util history."""
import os
import re
import sys
import time
import unittest
from io import BytesIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestClassifyTraining(unittest.TestCase):
    def test_launchers(self):
        self.assertEqual(app._classify_training(["torchrun", "--nproc", "2", "train.py"]), "torchrun")
        self.assertEqual(app._classify_training(["accelerate", "launch", "run.py"]), "accelerate")
        self.assertEqual(app._classify_training(["deepspeed", "x.py"]), "deepspeed")

    def test_train_scripts(self):
        self.assertEqual(app._classify_training(["python", "train.py"]), "train.py")
        self.assertEqual(app._classify_training(["python", "/work/finetune_lora.py", "--lr", "1e-4"]),
                         "finetune_lora.py")

    def test_ml_framework_module(self):
        self.assertEqual(app._classify_training(["python", "-m", "axolotl.cli.train"]), "training")

    def test_not_training(self):
        self.assertIsNone(app._classify_training(["python", "serve_api.py"]))
        self.assertIsNone(app._classify_training(["ollama", "serve"]))
        self.assertIsNone(app._classify_training([]))


class TestCollectTraining(unittest.TestCase):
    def _run(self, procs, gpu_pids, now):
        def fake_open(path, *a, **k):
            m = re.match(r"/proc/(\d+)/cmdline$", path)
            if m and m.group(1) in procs:
                return BytesIO(procs[m.group(1)])
            raise FileNotFoundError(path)
        with patch("app.os.listdir", return_value=list(procs.keys())), \
             patch("builtins.open", side_effect=fake_open):
            return app.collect_training(gpu_pids, now=now)

    def test_detects_and_annotates_vram(self):
        app._TRAIN_SEEN.clear()
        procs = {"100": b"torchrun\x00--nproc_per_node\x002\x00sft.py\x00",
                 "200": b"python\x00serve_api.py\x00",            # inference, not training
                 "300": b"python\x00finetune_lora.py\x00"}
        out = self._run(procs, {100: 5000}, now=1000)
        pids = {r["pid"] for r in out}
        self.assertIn(100, pids)
        self.assertIn(300, pids)
        self.assertNotIn(200, pids)                              # plain inference excluded
        t100 = next(r for r in out if r["pid"] == 100)
        self.assertEqual(t100["vram"], 5000)
        self.assertTrue(t100["on_gpu"])
        t300 = next(r for r in out if r["pid"] == 300)
        self.assertFalse(t300["on_gpu"])                        # not on the GPU

    def test_elapsed_from_first_seen(self):
        app._TRAIN_SEEN.clear()
        procs = {"100": b"torchrun\x00train.py\x00"}
        o1 = self._run(procs, {}, now=1000)
        o2 = self._run(procs, {}, now=1050)
        self.assertEqual(o1[0]["elapsed"], 0)
        self.assertEqual(o2[0]["elapsed"], 50)

    def test_dead_pid_forgotten(self):
        app._TRAIN_SEEN.clear()
        self._run({"100": b"torchrun\x00train.py\x00"}, {}, now=1000)
        self.assertIn("100", app._TRAIN_SEEN)
        self._run({"200": b"deepspeed\x00x.py\x00"}, {}, now=1100)   # 100 gone
        self.assertNotIn("100", app._TRAIN_SEEN)


class TestGpuSessions(unittest.TestCase):
    def test_single_session_metrics(self):
        rows = [(100, 50, 200, 8000, 10), (110, 60, 210, 8100, 10), (120, 55, 205, 8050, 10),
                (130, 5, 50, 100, 10), (140, 3, 40, 90, 10)]
        s = app._gpu_sessions(rows, 10, active_util=20, max_gap=1, min_len=2, price=0.5)
        self.assertEqual(len(s), 1)
        sess = s[0]
        self.assertEqual(sess["start"], 100)
        self.assertEqual(sess["end"], 120)
        self.assertEqual(sess["duration"], 30)                 # end-start+interval
        self.assertEqual(sess["peak_util"], 60)
        self.assertEqual(sess["peak_vram"], 8100)
        expect_kwh = (200 + 210 + 205) * 10 / 3_600_000.0
        self.assertAlmostEqual(sess["energy_kwh"], round(expect_kwh, 4), places=6)
        self.assertAlmostEqual(sess["cost"], round(expect_kwh * 0.5, 4), places=6)

    def test_gap_splits_sessions(self):
        # two bursts separated by 2 idle samples; max_gap=1 -> two sessions
        rows = [(100, 50, 100, 1, 10), (110, 50, 100, 1, 10),
                (120, 0, 0, 0, 10), (130, 0, 0, 0, 10),
                (140, 50, 100, 1, 10), (150, 50, 100, 1, 10)]
        s = app._gpu_sessions(rows, 10, active_util=20, max_gap=1, min_len=2)
        self.assertEqual(len(s), 2)

    def test_gap_within_tolerance_merges(self):
        rows = [(100, 50, 100, 1, 10), (110, 50, 100, 1, 10),
                (120, 0, 0, 0, 10),
                (130, 50, 100, 1, 10), (140, 50, 100, 1, 10)]
        s = app._gpu_sessions(rows, 10, active_util=20, max_gap=2, min_len=2)
        self.assertEqual(len(s), 1)                            # single idle tolerated

    def test_min_len_drops_blips(self):
        rows = [(100, 90, 300, 9000, 10), (110, 1, 0, 0, 10)]
        s = app._gpu_sessions(rows, 10, active_util=20, max_gap=1, min_len=2)
        self.assertEqual(s, [])                                # one-sample blip ignored


class TestApiSessions(unittest.TestCase):
    def test_endpoint(self):
        now = int(time.time())
        with app.LOCK:
            app.DB.execute("DELETE FROM samples")
            for i in range(6):
                app.DB.execute("INSERT OR REPLACE INTO samples(ts,util,mem_used,mem_total,power,temp) "
                               "VALUES(?,?,?,?,?,?)", (now - 100 + i * 10, 50, 8000, 24000, 200, 60))
            app.DB.commit()
        app.save_settings({"kwh_price": "0.30", "currency": "$", "tariff_mode": "single"})
        j = app.app.test_client().get("/api/sessions?range=1h").get_json()
        self.assertGreaterEqual(j["totals"]["count"], 1)
        self.assertIn("training", j)
        self.assertTrue(j["sessions"][0]["energy_kwh"] > 0)


if __name__ == "__main__":
    unittest.main()
