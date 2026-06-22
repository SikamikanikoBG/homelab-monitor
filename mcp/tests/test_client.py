"""Unit tests for homelab_client.py against a stub monitor.

Pure stdlib so it runs on the same Python 3.8+ as the client module (the `mcp`
SDK / server.py are not exercised here — that layer is a thin pass-through and
needs 3.10+). Run directly:

    python mcp/tests/test_client.py

Exits non-zero on the first failure.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# import the module under test (sibling-of-parent dir)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import homelab_client as hc  # noqa: E402


# ── canned payloads shaped like the real endpoints ───────────────────────────

def _host(cpu, ram_used, ram_total, disks, name="testbox"):
    return {
        "cpu": cpu, "cores": 8, "ram_used": ram_used, "ram_total": ram_total,
        "ram_kernel": 512, "load1": 1.2, "uptime": 86400, "ctemp": 47,
        "disks": disks,
        "os": {"kernel": "6.8.0", "arch": "x86_64", "pretty": "openSUSE Leap 16.1", "id": "opensuse-leap"},
        "net": {"hostname": name}, "sec": {"firewall": "firewalld"},
    }


FLEET = {
    "interval": 10,
    "hosts": [
        {"name": "local", "label": "ardi (this hub)", "ssh_target": None, "is_local": True,
         "online": True, "at": 1000, "last_check": {"summary": {"overall": "ok"}},
         "host": _host(12, 64000, 128000,
                       [{"mount": "/", "pct": 41}, {"mount": "/backup", "pct": 88}])},
        {"name": "cloudy", "label": "cloudy", "ssh_target": "anakin@cloudy", "is_local": False,
         "online": False, "at": 500, "last_check": {"summary": {"overall": "warn"}},
         "error": "ssh timeout", "host": None},
    ],
}

HOST_LOCAL = {"name": "local", "online": True, "at": 1000,
              "host": _host(9, 30000, 64000, [{"mount": "/", "pct": 55}])}
HOST_GHOST = {"name": "ghost", "online": False, "error": "no data yet", "at": None, "host": None}

HEALTH = {
    "version": "0.13.1", "updated": 1700,
    "now": {"gpu": {"util": 73, "mem_used": 9000, "mem_total": 24576, "available": True},
            "host": _host(15, 40000, 128000, [{"mount": "/", "pct": 60}])},
    "docker": {"available": True, "reason": None,
               "summary": {"total": 12, "running": 11, "problems": 1},
               "containers": [
                   {"name": "immich", "state": "running", "health": "healthy"},
                   {"name": "n8n", "state": "exited", "health": None},
                   {"name": "searxng", "state": "running", "health": "unhealthy"},
               ]},
    "systemd": {"available": True, "reason": None, "summary": {"failed": 1},
                "services": [
                    {"name": "sshd", "active": "active", "status": "ok"},
                    {"name": "borgbackup", "active": "failed", "status": "bad"},
                    # completed oneshot — inactive but NOT a failure; must be ignored
                    {"name": "nvidia-cdi-refresh", "active": "inactive", "status": "info"},
                    # flagged bad by the monitor even though ActiveState != failed
                    {"name": "weird", "active": "active", "status": "bad"},
                ]},
    "update": {"available": True, "current": "0.13.1"},
    "os_updates": {"count": 3},
    "diagnostics": {"checks": [{"name": "docker", "ok": True}], "summary": "ok"},
    "overview": [{"key": "gpu", "label": "GPU", "status": "ok"}],
}

DATA = {
    "version": "0.13.1", "range": "6h", "bucket_sec": 60,
    "mem_total": 24576, "peak_mem": 9000, "pressure_free_mb": 2048,
    "labels": [1000, 1060, 1120],
    "total": {"util": [10, 20, 0], "mem": [8000, 9000, 1000], "power": [200, 210, 90],
              "temp": [60, 62, 35], "cpu": [5, 7, 1], "ram_used": [40000, 41000, 39000],
              "ram_total": [128000, 128000, 128000], "load1": [0.9, 1.1, 0.8], "ctemp": [45, 46, 40]},
    "now": {
        "gpu_avail": True, "util": 73, "mem_used": 9000, "mem_total": 24576,
        "power": 210, "temp": 62, "ts": 1120,
        "host": _host(15, 40000, 128000, [{"mount": "/", "pct": 60}]),
        "models": [{"service": "ollama", "model": "llama3:70b", "vram": 8200, "state": "loaded"}],
        "procs": [{"service": "ollama", "mem": 8200}, {"service": "immich_ml", "mem": 1400}],
    },
    "summary": [{"service": "ollama", "peak": 8200, "avg": 6100, "present": 100},
                {"service": "immich_ml", "peak": 1500, "avg": 900, "present": 80}],
    "model_summary": [{"service": "ollama", "model": "llama3:70b", "peak": 8200, "avg": 6100}],
    "callers": [{"caller": "open-webui", "server": "ollama", "seconds": 3600, "samples": 360}],
    "events": [{"ts": 1650, "service": "immich_ml", "kind": "oom", "detail": "killed",
                "blame": "immich_ml lost to ollama"}],
    "insights": ["GPU VRAM peaked at 8.2 GB driven by open-webui"],
}

COSTS = {
    "enabled": True, "range": "7d", "bucket_sec": 600, "currency": "BGN",
    "rapl_available": True,
    "tariff": {"mode": "dual", "price_day": 0.28, "price_night": 0.14,
               "night_start": "23:00", "night_end": "06:00"},
    "machines": [{
        "name": "local",
        "now_w": {"gpu": 210, "cpu": 45, "dram": 8, "total": 263},
        "energy_kwh": {"gpu": 12.4, "cpu": 3.1, "dram": 0.6, "total": 16.1},
        "cost": {"today": 0.92, "d7": 4.21, "d30": 18.7},
        "cost_range": 4.21,
        "measured": ["gpu", "cpu", "dram"], "estimated": [],
    }],
    # the stacked-area chart the client deliberately drops
    "components": {"labels": [1000, 1600], "gpu": [200, 210], "cpu": [40, 45], "dram": [7, 8]},
    "breakdown": [
        {"kind": "model", "name": "llama3:70b", "energy_kwh": 8.2, "cost": 2.1, "avg_w": 180},
        {"kind": "container", "name": "immich_ml", "energy_kwh": 1.4, "cost": 0.35, "avg_w": 30},
    ],
}

COSTS_ENTITY = {
    "name": "llama3:70b", "kind": "model", "range": "7d", "bucket_sec": 600,
    "currency": "BGN", "energy_kwh": 8.2, "cost": 2.1, "avg_w": 180, "peak_w": 320,
    # the per-bucket cost curve the client deliberately drops
    "series": {"labels": [1000, 1600], "watts": [170, 190], "cost_cum": [0.4, 0.9]},
    "resources": {"gpu_vram_peak_mb": 8200},
}

RUNS = {
    "range": "7d", "currency": "BGN", "tariff_mode": "dual",
    "runs": [
        {"id": "r1", "name": "qwen-sft", "source": "sdk", "status": "finished",
         "started_at": 1000, "ended_at": 4600, "duration": 3600, "host": "ardi",
         "params": {"lr": 0.0002, "epochs": 3}, "tags": ["sft"], "notes": "",
         "key_id": 1, "key_name": "laptop",
         "metrics_latest": {"loss": 0.42, "accuracy": 0.91},
         "energy_kwh": 1.8, "cost": 0.46, "avg_w": 180, "peak_util": 99},
        {"id": "r2", "name": "eval-sweep", "source": "mlflow", "status": "running",
         "started_at": 4000, "ended_at": None, "duration": 600, "host": "ardi",
         "params": {}, "tags": [], "notes": "", "key_id": 1, "key_name": "laptop",
         "metrics_latest": {"loss": 0.55}, "energy_kwh": 0.3, "cost": 0.08,
         "avg_w": 160, "peak_util": 95},
    ],
}

RUN_ONE = {
    "id": "r1", "name": "qwen-sft", "source": "sdk", "status": "finished",
    "started_at": 1000, "ended_at": 4600, "duration": 3600, "host": "ardi",
    "params": {"lr": 0.0002, "epochs": 3}, "tags": ["sft"], "notes": "",
    "metrics": {"loss": {"steps": [0, 1, 2], "ts": [1000, 2800, 4600], "values": [1.2, 0.7, 0.42]}},
    "resource": {"labels": [1000, 4600], "power_w": [170, 190], "util_pct": [98, 99], "bucket_sec": 600},
    "energy_kwh": 1.8, "cost": 0.46, "avg_w": 180, "peak_util": 99,
    "currency": "BGN", "tariff_mode": "dual",
}

DISK_DONE = {
    "path": "/", "state": "done", "total": 355218889101, "free": 82583969792,
    "entries": [{"name": "var", "path": "/var", "bytes": 172911017873,
                 "children": [{"name": "lib", "path": "/var/lib", "bytes": 170963548531}]}],
    "error": None,
}
DISK_SCANNING = {"path": "/slow", "state": "scanning"}

INCIDENTS = {
    "now": 9000,
    "summary": {"open": 1, "top": {"id": "inc1", "severity": "critical", "open": 1}},
    "incidents": [
        {"id": "inc1", "state": "open", "severity": "critical",
         "opened_at": 8000, "updated_at": 9000, "cleared_at": None,
         "member_count": 3, "active_count": 3,
         "members": [
             {"series": "gpu_power", "direction": "spike", "peak_z": 7.2, "unit": "W",
              "peak_value": 320, "baseline": 200, "first_seen": 8000, "last_seen": 9000, "active": True},
             {"series": "gpu_temp", "direction": "spike", "peak_z": 4.1, "unit": "C",
              "peak_value": 85, "baseline": 60, "first_seen": 8020, "last_seen": 9000, "active": True},
         ]},
        {"id": "inc0", "state": "cleared", "severity": "warning",
         "opened_at": 1000, "updated_at": 2000, "cleared_at": 2000,
         "member_count": 1, "active_count": 0, "members": []},
    ],
}
INCIDENT_ONE = {
    "now": 9000,
    "incident": {"id": "inc1", "state": "open", "severity": "critical",
                 "opened_at": 8000, "updated_at": 9000, "cleared_at": None,
                 "member_count": 2, "active_count": 2,
                 "members": INCIDENTS["incidents"][0]["members"],
                 "timeline": [
                     {"at": 8000, "event": "opened", "detail": "incident opened", "series": None},
                     {"at": 8000, "event": "member_joined", "series": "gpu_power", "detail": "gpu_power ▲ (peak σ=7.2)"},
                     {"at": 8020, "event": "member_joined", "series": "gpu_temp", "detail": "gpu_temp ▲ (peak σ=4.1)"},
                 ]},
}

METRICS = "# HELP gpu_util GPU utilization\ngpu_util{gpu=\"gpu0\"} 73\n"
HEALTHZ = {"status": "ok", "version": "0.13.1"}

# /api/recommendations deterministic (brief=1, LLM-free) body. `priority` is None
# and llm_used False because the brief path never calls the LLM.
RECOS = {
    "now": 9000, "generated_at": 9000, "model": "qwen2.5:7b", "enabled": True,
    "llm_used": False, "priority": None, "llm_status": "skipped",
    "count": 3, "counts": {"crit": 1, "warn": 1, "total": 3},
    "items": [
        {"id": "disk-/backup", "severity": "crit", "title": "/backup fills in 4 days",
         "detail": "88% used, +3%/day", "action": "Free space or expand /backup",
         "source": "forecast", "link": "#disks", "ts": 8900},
        {"id": "oom-immich_ml", "severity": "warn", "title": "immich_ml OOM-killed 3x",
         "detail": "3 kills in 7 days", "action": "Cap immich_ml memory", "source": "oom"},
        {"id": "cost-month", "severity": "info", "title": "On track for 18.70 BGN this month",
         "detail": "vs 16.10 last", "action": "Review the Costs tab", "source": "cost"},
    ],
}

# /api/copilot/ask responses, keyed by whether the LLM "answered".
ASK_LLM = {
    "now": 9000, "model": "qwen2.5:7b", "question": "why is the gpu busy?",
    "facts": ["GPU util 73%", "ollama serving llama3:70b"],
    "sources": ["gpu", "models"], "routing": "live", "enabled": True,
    "answer": "The GPU is busy because ollama is serving llama3:70b at 73% util.",
    "source": "llm", "llm_status": "ok",
}
ASK_NOLLM = {
    "now": 9000, "model": "qwen2.5:7b", "question": "down llm",
    "facts": ["GPU util 73%"], "sources": ["gpu"], "routing": "live",
    "enabled": True, "answer": "", "facts_summary": "GPU util 73%",
    "source": "facts", "llm_status": "ollama unreachable",
}

ROUTES = {
    "/api/fleet": FLEET,
    "/api/host_data/local": HOST_LOCAL,
    "/api/host_data/ghost": HOST_GHOST,
    "/api/health": HEALTH,
    "/api/data": DATA,
    "/api/costs": COSTS,
    "/api/costs/entity": COSTS_ENTITY,
    "/api/runs": RUNS,
    "/api/incidents": INCIDENTS,
    "/healthz": HEALTHZ,
}

# Records the raw query/body each handler saw, so tests can assert the client used
# the LLM-free path and posted the question.
_SEEN = {"reco_query": None, "ask_body": None}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if path == "/api/copilot/ask":
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
            _SEEN["ask_body"] = body
            q = (body.get("question") or "")
            self._json(ASK_NOLLM if "down llm" in q else ASK_LLM)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"error":"not found"}')

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/recommendations":
            _SEEN["reco_query"] = self.path.split("?", 1)[1] if "?" in self.path else ""
            self._json(RECOS)
            return
        if path == "/api/disk_scan":
            # crude query parse: /slow always scanning, everything else done.
            scanning = "%2Fslow" in self.path or "/slow" in self.path
            self._json(DISK_SCANNING if scanning else DISK_DONE)
            return
        if path == "/metrics":
            body = METRICS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/runs/"):
            rid = path.rsplit("/", 1)[-1]
            if rid == "r1":
                self._json(RUN_ONE)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"unknown run"}')
            return
        if path.startswith("/api/incidents/"):
            iid = path.rsplit("/", 1)[-1]
            if iid == "inc1":
                self._json(INCIDENT_ONE)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"unknown incident"}')
            return
        if path in ROUTES:
            body = json.dumps(ROUTES[path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'{"error":"not found"}')


# ── tiny test harness ────────────────────────────────────────────────────────

_FAILS = []


def check(cond, msg):
    if cond:
        print("  ok  -", msg)
    else:
        print("  FAIL-", msg)
        _FAILS.append(msg)


def run():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    os.environ["HOMELAB_MONITOR_URL"] = "http://127.0.0.1:%d" % srv.server_address[1]
    os.environ["HOMELAB_HTTP_TIMEOUT"] = "5"
    try:
        print("list_hosts")
        r = hc.list_hosts()
        check(r["count"] == 2, "counts both hosts")
        check(r["sample_interval_sec"] == 10, "passes through interval")
        loc = r["hosts"][0]
        check(loc["name"] == "local" and loc["is_local"] is True, "local first, flagged")
        check(loc["overall"] == "ok", "pulls overall from last_check")
        check(loc["vitals"]["ram_pct"] == 50.0, "computes ram_pct (64000/128000)")
        check(loc["vitals"]["fullest_disk"]["mount"] == "/backup", "picks fullest disk (88%)")
        check(loc["vitals"]["os"] == "openSUSE Leap 16.1", "os pretty name")
        offl = r["hosts"][1]
        check(offl["online"] is False and offl["error"] == "ssh timeout", "offline host carries error")
        check(offl["vitals"] is None, "no vitals when host snapshot missing")

        print("get_host (online)")
        r = hc.get_host("local")
        check(r["online"] is True and r["host"]["os"]["arch"] == "x86_64", "returns full host inventory")

        print("get_host (no data yet)")
        r = hc.get_host("ghost")
        check(r["online"] is False and r["error"] == "no data yet", "waiting state surfaced")

        print("get_snapshot")
        r = hc.get_snapshot()
        check(r["version"] == "0.13.1", "version")
        check(r["gpu"]["util"] == 73, "gpu vitals")
        check(r["host"]["ram_pct"] is not None, "host summarized")
        names = {c["name"] for c in r["docker"]["problem_containers"]}
        check(names == {"n8n", "searxng"}, "problem containers = exited + unhealthy only")
        failed = {s["name"] for s in r["systemd"]["failed_services"]}
        check(failed == {"borgbackup", "weird"}, "failed = active==failed or status==bad")
        check("nvidia-cdi-refresh" not in failed, "completed oneshot (inactive) not flagged")
        check(r["update_available"] is True, "update flag")

        print("get_containers")
        r = hc.get_containers()
        check(len(r["containers"]) == 3, "full container list (not just problems)")
        check(r["summary"]["total"] == 12, "container summary passed through")

        print("get_services")
        r = hc.get_services()
        check(len(r["services"]) == 4, "full service list (not just failed)")

        print("get_memory")
        r = hc.get_memory("24h")
        check(r["ram_total_mb"] == 128000, "system RAM total from host (not GPU VRAM)")
        check(r["ram_used_mb"] == 40000, "system RAM used from host")
        check(r["ram_kernel_mb"] == 512, "kernel memory from now.host")
        svcs = {s["service"] for s in r["per_service"]}
        check(svcs == {"ollama", "immich_ml"}, "per-service RAM breakdown")
        check({p["service"] for p in r["current_procs"]} == {"ollama", "immich_ml"}, "current procs")

        print("get_gpu")
        r = hc.get_gpu("24h")
        check(r["util_pct"] == 73 and r["vram_used_mb"] == 9000, "gpu now vitals")
        check(r["power_w"] == 210 and r["temp_c"] == 62, "gpu power/temp")
        check(r["models_vram"][0]["peak"] == 8200, "per-model vram")
        check(r["callers"][0]["server"] == "ollama", "gpu caller attribution")

        print("get_history")
        r = hc.get_history("24h")
        check(r["labels"] == [1000, 1060, 1120], "history timestamps")
        check(r["series"]["util"] == [10, 20, 0], "history series aligned")
        check(r["bucket_sec"] == 60, "bucket width")

        print("scan_disk")
        r = hc.scan_disk("/")
        check(r["state"] == "done" and r["total_bytes"] == 355218889101, "disk scan completes")
        check(r["entries"][0]["children"][0]["name"] == "lib", "nested folder tree")
        r = hc.scan_disk("/slow", max_wait=0)
        check(r["state"] == "scanning" and "note" in r, "scanning path returns poll-again note")

        print("get_ai_models")
        r = hc.get_ai_models("24h")
        check(r["loaded"][0]["model"] == "llama3:70b", "loaded models")
        check(r["callers"][0]["caller"] == "open-webui", "caller attribution")
        check(r["vram_summary"][0]["peak"] == 8200, "vram summary")

        print("get_events / get_alerts")
        r = hc.get_events()
        check(r["events"][0]["kind"] == "oom", "events surfaced")
        check("blame" in r["events"][0], "blame preserved")
        check(len(r["insights"]) == 1, "insights surfaced")
        check(hc.get_alerts()["events"] == r["events"], "get_alerts aliases get_events")

        print("get_costs")
        r = hc.get_costs("7d")
        check(r["enabled"] is True and r["currency"] == "BGN", "cost summary basics")
        check(r["machine"]["cost"]["today"] == 0.92, "machine cost windows (today/7d/30d)")
        check(r["machine"]["energy_kwh"]["total"] == 16.1, "machine energy total")
        check(r["machine"]["cost_range"] == 4.21, "cost over the selected range")
        check(r["tariff"]["mode"] == "dual", "tariff passed through")
        check(r["breakdown"][0]["name"] == "llama3:70b", "ranked breakdown, biggest first")
        check("components" not in r, "per-bucket stacked chart trimmed out")

        print("get_entity_cost")
        r = hc.get_entity_cost("llama3:70b", "model", "7d")
        check(r["cost"] == 2.1 and r["energy_kwh"] == 8.2, "entity cost + energy")
        check(r["peak_w"] == 320, "entity peak watts")
        check(r["resources"]["gpu_vram_peak_mb"] == 8200, "entity resource use")
        check("series" not in r, "entity cost curve trimmed out")

        print("get_experiments")
        r = hc.get_experiments("7d")
        check(r["count"] == 2, "counts runs")
        check(r["runs"][0]["metrics_latest"]["loss"] == 0.42, "latest metrics surfaced")
        check(r["runs"][0]["cost"] == 0.46, "run priced by GPU energy")

        print("get_experiment")
        r = hc.get_experiment("r1")
        check(r["id"] == "r1" and r["status"] == "finished", "single run detail")
        check(r["metrics"]["loss"]["values"][-1] == 0.42, "loss-curve series")
        check(r["resource"]["util_pct"] == [98, 99], "gpu series over the run")
        try:
            hc.get_experiment("nope")
            check(False, "unknown run id raises MonitorError")
        except hc.MonitorError as e:
            check("404" in str(e), "unknown run id raises MonitorError (404)")

        print("get_incidents (list)")
        r = hc.get_incidents()
        check(r["count"] == 2, "counts incidents")
        check(r["summary"]["open"] == 1, "summary open count surfaced")
        check(r["incidents"][0]["id"] == "inc1", "open-first ordering preserved")
        check(r["incidents"][0]["severity"] == "critical", "severity surfaced")
        check(r["incidents"][0]["members"][0]["series"] == "gpu_power", "member series surfaced")
        check(r["incidents"][0]["members"][0]["peak_z"] == 7.2, "member peak z surfaced")

        print("get_incidents (detail)")
        r = hc.get_incidents(incident_id="inc1")
        check(r["id"] == "inc1" and r["state"] == "open", "single incident detail")
        check(r["member_count"] == 2, "member count on detail")
        check([e["event"] for e in r["timeline"]][0] == "opened", "timeline starts at opened")
        check(any(e["event"] == "member_joined" for e in r["timeline"]), "timeline has member joins")
        try:
            hc.get_incidents(incident_id="nope")
            check(False, "unknown incident id raises MonitorError")
        except hc.MonitorError as e:
            check("404" in str(e), "unknown incident id raises MonitorError (404)")

        print("get_recommendations (deterministic / LLM-free)")
        r = hc.get_recommendations()
        check("brief=1" in (_SEEN["reco_query"] or ""), "uses the LLM-free brief=1 path")
        check(r["count"] == 3, "counts items")
        check(r["counts"]["crit"] == 1 and r["counts"]["warn"] == 1, "severity counts surfaced")
        top = r["items"][0]
        check(top["severity"] == "crit", "ranked crit first")
        check(top["title"] == "/backup fills in 4 days", "item title")
        check(top["action"] == "Free space or expand /backup", "actionable suggestion")
        check(top["source"] == "forecast" and top["link"] == "#disks", "source + link")
        r2 = hc.get_recommendations(limit=1)
        check(r2["count"] == 1 and len(r2["items"]) == 1, "limit caps returned items")

        print("ask_lab (LLM answered)")
        r = hc.ask_lab("why is the gpu busy?")
        check(_SEEN["ask_body"] == {"question": "why is the gpu busy?"}, "posts the question body")
        check(r["answer"].startswith("The GPU is busy"), "returns the LLM answer")
        check(r["sources"] == ["gpu", "models"], "names the sources")
        check(r["routing"] == "live", "routing surfaced")
        check(r["llm_status"] == "ok", "llm_status ok")

        print("ask_lab (LLM unreachable -> routed facts)")
        r = hc.ask_lab("down llm")
        check(r["answer"] == "", "no answer when LLM unreachable")
        check(r["facts_summary"] == "GPU util 73%", "degrades to routed facts summary")
        check(r["llm_status"] == "ollama unreachable", "carries the degrade reason")

        print("resources")
        check("gpu_util" in hc.get_metrics(), "metrics text")
        check(hc.get_version()["status"] == "ok", "healthz")

        print("error handling")
        os.environ["HOMELAB_MONITOR_URL"] = "http://127.0.0.1:1"  # refused
        try:
            hc.list_hosts()
            check(False, "unreachable monitor raises MonitorError")
        except hc.MonitorError as e:
            check("cannot reach monitor" in str(e), "unreachable monitor raises MonitorError")
        try:
            hc.get_recommendations()
            check(False, "unreachable monitor raises MonitorError (get_recommendations)")
        except hc.MonitorError as e:
            check("cannot reach monitor" in str(e), "get_recommendations errors like the rest")
        try:
            hc.ask_lab("anything")
            check(False, "unreachable monitor raises MonitorError (ask_lab POST)")
        except hc.MonitorError as e:
            check("cannot reach monitor" in str(e), "ask_lab POST errors like the rest")
    finally:
        srv.shutdown()

    print()
    if _FAILS:
        print("%d FAILURE(S)" % len(_FAILS))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    run()
