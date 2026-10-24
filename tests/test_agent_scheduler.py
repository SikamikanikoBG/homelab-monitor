"""Cron parsing and DST, once-per-slot firing, catch-up, overlap, pause,
GPU courtesy, cloud limits, templates and delivery (US-8/9/12, EC-10/11/19/23).

DST cases use Europe/Sofia: summer time ends 2026-10-25 (04:00 → 03:00) and
starts 2026-03-29 (03:00 → 04:00).
"""
import json
import time
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_fakes import AgentOn, FakeFleet, ScriptedAdapter, app
from backend.agent import cron, providers, scheduler, state, store, templates

try:
    SOFIA = ZoneInfo("Europe/Sofia")
except ZoneInfoNotFoundError:            # Windows without tzdata; CI and the image have it
    SOFIA = None


def utc(y, mo, d, h, mi, tz):
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())


class TestCronParse(unittest.TestCase):
    def test_fields(self):
        c = cron.Cron("*/15 1-5/2 * jan,mar MON-fri")
        self.assertEqual(sorted(c.minutes), [0, 15, 30, 45])
        self.assertEqual(sorted(c.hours), [1, 3, 5])
        self.assertEqual(sorted(c.months), [1, 3])
        self.assertEqual(sorted(c.dow), [1, 2, 3, 4, 5])
        self.assertEqual(cron.Cron("@daily").expr, "0 0 * * *")
        self.assertEqual(sorted(cron.Cron("0 0 * * 7").dow), [0])

    def test_errors_name_the_field(self):
        for expr, field in (("61 * * * *", "minute"), ("* 24 * * *", "hour"), ("* * 0 * *", "day of month"),
                            ("* * * 13 *", "month"), ("* * * * 8", "day of week"), ("* * *", "expression"),
                            ("*/0 * * * *", "minute"), ("5-1 * * * *", "minute"), ("x * * * *", "minute")):
            with self.assertRaises(cron.CronError) as cm:
                cron.Cron(expr)
            self.assertEqual(cm.exception.field, field, expr)

    def test_vixie_dom_dow_or(self):
        c = cron.Cron("0 0 13 * 5")       # the 13th OR any Friday
        tz = cron.zone("UTC")
        t = utc(2026, 11, 1, 0, 0, tz)
        days = []
        for _ in range(6):
            t, _l = cron.next_after(c, "UTC", t)
            days.append(datetime.utcfromtimestamp(t).day)
        self.assertEqual(days, [6, 13, 20, 27, 4, 11])

    def test_feb_29(self):
        t, _ = cron.next_after("0 12 29 2 *", "UTC", utc(2026, 1, 1, 0, 0, cron.zone("UTC")))
        self.assertEqual(datetime.utcfromtimestamp(t).year, 2028)

    def test_min_interval(self):
        self.assertEqual(cron.Cron("*/15 * * * *").min_interval_s(), 900)
        self.assertEqual(cron.Cron("0 3 * * *").min_interval_s(), 86400)
        self.assertEqual(cron.Cron("0 */6 * * *").min_interval_s(), 6 * 3600)

    def test_monotonic(self):
        t = 1_790_000_000
        for _ in range(50):
            n, _l = cron.next_after("7 */3 * * *", "UTC", t)
            self.assertGreater(n, t)
            t = n


@unittest.skipIf(SOFIA is None, "no tz database on this machine")
class TestCronDST(unittest.TestCase):
    def _slots(self, expr, start, n=4):
        return [loc for _, loc in cron.preview(expr, "Europe/Sofia", start, n)]

    def test_fall_back_daily_fires_once(self):
        start = utc(2026, 10, 24, 12, 0, SOFIA)
        out = cron.preview("30 3 * * *", "Europe/Sofia", start, 3)
        self.assertEqual([loc for _, loc in out], ["2026-10-25T03:30", "2026-10-26T03:30", "2026-10-27T03:30"])
        self.assertEqual(len({u for u, _ in out}), 3)
        # first occurrence: 03:30 EEST = 00:30 UTC
        self.assertEqual(datetime.utcfromtimestamp(out[0][0]).strftime("%H:%M"), "00:30")

    def test_fall_back_hourly_no_duplicate(self):
        out = cron.preview("0 * * * *", "Europe/Sofia", utc(2026, 10, 25, 1, 30, SOFIA), 5)
        utcs = [u for u, _ in out]
        self.assertEqual(len(utcs), len(set(utcs)))
        self.assertEqual([l for _, l in out][:3], ["2026-10-25T02:00", "2026-10-25T03:00", "2026-10-25T04:00"])

    def test_every_15_no_duplicate_utc(self):
        out = cron.preview("*/15 * * * *", "Europe/Sofia", utc(2026, 10, 25, 2, 50, SOFIA), 12)
        utcs = [u for u, _ in out]
        self.assertEqual(len(utcs), len(set(utcs)))
        self.assertEqual(utcs, sorted(utcs))

    def test_spring_forward_gap(self):
        out = cron.preview("30 3 * * *", "Europe/Sofia", utc(2026, 3, 28, 12, 0, SOFIA), 2)
        # 03:30 doesn't exist on 2026-03-29: fires once at 04:00 local
        first = datetime.fromtimestamp(out[0][0], SOFIA)
        self.assertEqual(first.strftime("%Y-%m-%d %H:%M"), "2026-03-29 04:00")
        self.assertEqual(datetime.fromtimestamp(out[1][0], SOFIA).strftime("%Y-%m-%d %H:%M"), "2026-03-30 03:30")


class SchedCase(unittest.TestCase):
    def setUp(self):
        self.on = AgentOn().start()
        self.fleet = FakeFleet().start()
        r = self.on.post("/api/agent/providers", {"preset": "vllm", "base_url": "http://model-host:8000/v1"})
        self.pid = r.get_json()["provider"]["id"]
        store.model_upsert(self.pid, "m1", tools="yes", context_window=32768)
        self.on.put("/api/agent/settings", {"default_provider_id": self.pid, "default_model": "m1", "tz": "UTC"})
        self.adapter = ScriptedAdapter([{"text": "report"}] * 50)
        p = patch.object(providers, "adapter_for", lambda row: self.adapter)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.fleet.stop()
        self.on.stop()

    def task(self, **kw):
        body = {"template_id": "disk-space-watch", "hosts": ["local"], "cron": "0 3 * * *", "tz": "UTC",
                "provider_id": self.pid, "model": "m1"}
        body.update(kw)
        r = self.on.post("/api/agent/tasks", body)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["task"]


class TestScheduler(SchedCase):
    def test_fires_once_per_slot(self):
        t = self.task()
        slot = t["next_fire"]
        out1 = scheduler.tick(slot + 5)
        out2 = scheduler.tick(slot + 6)
        self.assertEqual([o[2] for o in out1], ["fired"])
        self.assertEqual(out2, [])
        # a second tick racing on the same slot can't fire it either
        self.assertEqual(scheduler._try_fire(store.task_get(t["id"]), slot, "schedule"), "already_fired")
        self.assertEqual(len(store.run_list(task_id=t["id"])), 1)
        self.assertGreater(store.task_get(t["id"])["next_fire"], slot)

    def test_missed_slots_skip_by_default(self):
        t = self.task(cron="0 * * * *")
        later = t["next_fire"] + 10 * 3600 + 30
        out = scheduler.tick(later)
        self.assertEqual(out, [])
        fires = store.fire_list(t["id"])
        self.assertEqual([f["outcome"] for f in fires], ["skipped_missed"])
        self.assertGreater(store.task_get(t["id"])["next_fire"], later)

    def test_missed_slots_catch_up_once(self):
        t = self.task(cron="0 * * * *", catch_up="once")
        later = t["next_fire"] + 10 * 3600 + 30
        out = scheduler.tick(later)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], t["next_fire"] + 10 * 3600)      # the latest missed slot, not ten
        self.assertEqual(len(store.run_list(task_id=t["id"])), 1)

    def test_overlap_skipped(self):
        t = self.task(cron="*/15 * * * *")
        with patch("backend.agent.runs.Manager.submit", lambda *a, **k: None):
            scheduler.tick(t["next_fire"] + 1)
            nxt = store.task_get(t["id"])["next_fire"]
            out = scheduler.tick(nxt + 1)
        self.assertEqual(out[0][2], "skipped_overlap")

    def test_pause_all(self):
        t = self.task()
        self.assertEqual(self.on.post("/api/agent/pause", {"paused": True}).status_code, 200)
        out = scheduler.tick(t["next_fire"] + 1)
        self.assertEqual(out[0][2], "skipped_paused")
        self.assertEqual(store.run_list(task_id=t["id"]), [])

    def test_disabled_agent_fires_nothing(self):
        t = self.task()
        state.put({"enabled": "0"})
        self.assertEqual(scheduler.tick(t["next_fire"] + 1), [])

    def test_run_now_and_fires_list(self):
        t = self.task()
        r = self.on.post("/api/agent/tasks/%s/run" % t["id"])
        self.assertEqual(r.status_code, 202)
        fires = self.on.get("/api/agent/tasks/%s/fires" % t["id"]).get_json()["items"]
        self.assertEqual(fires[0]["run_id"], r.get_json()["run_id"])

    def test_cloud_task_limits(self):
        r = self.on.post("/api/agent/providers", {"preset": "openai", "api_key": "sk-test-0000"})
        cloud = r.get_json()["provider"]["id"]
        r = self.on.post("/api/agent/tasks", {"template_id": "disk-space-watch", "hosts": ["local"],
                                              "cron": "*/15 * * * *", "provider_id": cloud, "model": "gpt"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cron", r.get_json()["fields"])
        t = self.task(provider_id=cloud, model="gpt", cron="0 * * * *")
        rid = store.run_create("c", "schedule", False, "report", provider_id=cloud, model="gpt", task_id=t["id"])
        store.run_add_usage(rid, 0, 0, int(5 * 1e6))
        store.run_update(rid, status="done")
        out = scheduler.tick(t["next_fire"] + 1)
        self.assertEqual(out[0][2], "skipped_budget")

    def test_gpu_courtesy_skips_after_wait(self):
        t = self.task()
        state.put({"gpu_defer_max_min": "0"})
        with patch.object(scheduler, "_gpu_busy", return_value=True):
            scheduler.tick(t["next_fire"] + 1)
        run = store.run_list(task_id=t["id"])[0]
        self.assertEqual((run["status"], run["status_reason"]), ("cancelled", "skipped_gpu_busy"))
        self.assertEqual(store.fire_list(t["id"])[0]["outcome"], "skipped_gpu_busy")

    def test_gpu_busy_reads_hub_cards(self):
        with patch.object(app, "AI_SERVERS", [{"name": "ollama", "ip": "172.17.0.5", "provider": "ollama"}]), \
                patch.dict(app.LATEST, {"gpus": [{"util": 99, "mem_used": 23000, "mem_total": 24000}]}):
            self.assertTrue(scheduler._gpu_busy("http://172.17.0.5:11434"))
        with patch.object(app, "AI_SERVERS", [{"name": "ollama", "ip": "172.17.0.5", "provider": "ollama"}]), \
                patch.dict(app.LATEST, {"gpus": [{"util": 5, "mem_used": 1000, "mem_total": 24000}]}):
            self.assertFalse(scheduler._gpu_busy("http://172.17.0.5:11434"))
        self.assertIsNone(scheduler._gpu_busy("http://unknown-host:1"))

    def test_delivery_to_channels(self):
        app.save_settings({"ntfy_topic": "lab"})          # a channel must be set up to be picked
        self.addCleanup(app.save_settings, {"ntfy_topic": ""})
        t = self.task(delivery={"channels": ["inapp", "ntfy"]})
        with patch("backend.agent.ports.dispatch_to_channels") as d:
            self.on.post("/api/agent/tasks/%s/run" % t["id"])
        args = d.call_args[0]
        self.assertEqual(args[0], "info")
        self.assertIn("report", args[2])
        self.assertIn("/#agent/run/", args[2])
        self.assertEqual(args[3], ["ntfy"])

    def test_task_validation_and_update(self):
        r = self.on.post("/api/agent/tasks", {"name": "x", "prompt": "y", "cron": "99 * * * *", "mode": "act"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cron", r.get_json()["fields"])
        t = self.task()
        r = self.on.patch("/api/agent/tasks/" + t["id"], {"cron": "0 5 * * *", "enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["task"]["enabled"])
        self.assertEqual(r.get_json()["task"]["cron"], "0 5 * * *")
        self.assertEqual(self.on.delete("/api/agent/tasks/" + t["id"]).status_code, 200)

    def test_cron_preview_api(self):
        j = self.on.post("/api/agent/cron/preview", {"cron": "0 3 * * 0", "tz": "UTC"}).get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(len(j["next"]), 5)
        j = self.on.post("/api/agent/cron/preview", {"cron": "0 25 * * *"}).get_json()
        self.assertEqual((j["ok"], j["field"]), (False, "hour"))


class TestTemplates(SchedCase):
    def test_all_templates_valid(self):
        ts = templates.all_templates()
        self.assertEqual(len(ts), 10)
        for t in ts:
            self.assertEqual(templates.problems(t), [], t["id"])

    def test_gallery_and_eligible_hosts(self):
        j = self.on.get("/api/agent/templates").get_json()["templates"]
        docker = next(t for t in j if t["id"] == "docker-cleanup")
        self.assertIn("lab1", docker["eligible_hosts"])
        self.assertEqual(set(docker["runs_alone"]), {"docker.prune_dangling_images", "docker.prune_build_cache"})
        self.assertEqual(docker["waits_for_approval"], ["docker.prune_stopped_containers"])

    def test_empty_fleet_hub_alone_is_enough(self):
        self.fleet.stop()
        with patch.object(app, "HEALTH", {"docker": {"available": True, "containers": []}, "systemd": None}):
            j = self.on.get("/api/agent/templates").get_json()["templates"]
        self.assertEqual(next(t for t in j if t["id"] == "docker-cleanup")["eligible_hosts"], ["local"])
        self.fleet.start()

    def test_activation_snapshots_the_template(self):
        t = self.task(template_id="docker-cleanup", hosts=["lab1"])     # a host with Docker
        self.assertEqual(t["template_version"], 1)
        self.assertIn("docker.prune_build_cache", t["actions"])
        self.assertIn("docker.system_df", t["tools"])
        self.assertFalse(t["update_available"])


if __name__ == "__main__":
    unittest.main()
