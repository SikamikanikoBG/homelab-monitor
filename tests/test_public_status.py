import os, pytest
os.environ.setdefault("DATABASE_URL", ":memory:")

import app as _app

@pytest.fixture
def client():
    os.environ["PUBLIC_STATUS"] = "1"
    _app.app.config["TESTING"] = True
    with _app.app.test_client() as c:
        yield c
    os.environ.pop("PUBLIC_STATUS", None)

@pytest.fixture
def client_off():
    os.environ.pop("PUBLIC_STATUS", None)
    _app.app.config["TESTING"] = True
    with _app.app.test_client() as c:
        yield c

def test_off_by_default_api(client_off):
    assert client_off.get("/api/public-status").status_code == 404

def test_off_by_default_page(client_off):
    assert client_off.get("/public").status_code == 404

def test_enabled_api_returns_200(client):
    assert client.get("/api/public-status").status_code == 200

def test_enabled_page_returns_200(client):
    assert client.get("/public").status_code == 200

def test_no_sensitive_keys(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    blocked = {"containers", "services", "processes", "os_updates", "diagnostics",
               "discord_webhook_url", "telegram_token", "email_password",
               "slack_webhook_url", "webhook_url", "api_key"}
    assert not blocked & set(data.keys())

def test_overview_cards_safe(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    allowed = {"key", "label", "status", "metric", "detail"}
    for card in data.get("overview", []):
        assert set(card.keys()) <= allowed

def test_status_field_valid(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    assert data["status"] in ("ok", "warn", "crit")

def test_lab_branding(client):
    import json
    data = json.loads(client.get("/api/public-status").data)
    assert "lab_name" in data
    assert "lab_emoji" in data

def test_no_private_paths_in_body(client):
    body = client.get("/api/public-status").data.decode()
    assert "/var/lib" not in body
    assert "image" not in body.lower() or "lab_emoji" in body


# ── Public monitors: per-check opt-in + per-service detail (status pages) ──────
import time as _time

def _mk_check(public, label="Immich", target="https://immich.example.net/health"):
    cid, err = _app.create_uptime_check(
        {"label": label, "type": "http", "target": target, "public": public})
    assert err is None, err
    return cid

def _seed(cid, rows):
    with _app.LOCK:
        _app.DB.executemany(
            "INSERT INTO uptime_results(check_id,ts,up,latency_ms,code,err) VALUES(?,?,?,?,?,?)",
            [(cid, ts, up, lat, None, (None if up else "boom")) for ts, up, lat in rows])
        _app.DB.commit()

def _wipe():
    with _app.LOCK:
        _app.DB.execute("DELETE FROM uptime_checks")
        _app.DB.execute("DELETE FROM uptime_results")
        _app.DB.commit()

def test_public_monitor_listed_only_when_public(client):
    _wipe()
    pub = _mk_check(True, "Immich")
    _mk_check(False, "Private NAS", "https://nas.example.net")
    data = client.get("/api/public-status").get_json()
    labels = [m["label"] for m in data.get("monitors", [])]
    assert "Immich" in labels
    assert "Private NAS" not in labels
    assert data["monitors_summary"]["total"] == 1
    _wipe()

def test_public_detail_requires_public(client):
    _wipe()
    pub = _mk_check(True)
    priv = _mk_check(False, "Private")
    assert client.get(f"/api/public-status/{pub}").status_code == 200
    assert client.get(f"/api/public-status/{priv}").status_code == 404
    assert client.get("/api/public-status/does-not-exist").status_code == 404
    _wipe()

def test_public_detail_off_when_disabled(client_off):
    # No PUBLIC_STATUS env -> even a known id 404s.
    assert client_off.get("/api/public-status/anything").status_code == 404

def test_public_detail_shape_and_windows(client):
    _wipe()
    now = int(_time.time())
    cid = _mk_check(True)
    _seed(cid, [(now - 3600, 1, 120.0), (now - 1800, 0, 0.0), (now - 60, 1, 130.0)])
    d = client.get(f"/api/public-status/{cid}").get_json()
    assert set(("uptime", "daily", "response_series", "incidents", "host")) <= set(d.keys())
    assert set(("24h", "7d", "30d", "90d")) <= set(d["uptime"].keys())
    assert any(i["duration_s"] for i in d["incidents"])   # the down->up blip
    _wipe()

def test_public_detail_no_raw_target_or_creds(client):
    _wipe()
    cid = _mk_check(True, "Immich", "https://user:secret@immich.example.net/health?token=abc")
    d = client.get(f"/api/public-status/{cid}").get_json()
    blob = str(d)
    assert "secret" not in blob and "token=abc" not in blob and "/health" not in blob
    assert d["host"] == "immich.example.net"
    _wipe()

def test_monitors_key_is_not_the_blocked_services_key(client):
    _wipe()
    data = client.get("/api/public-status").get_json()
    assert "services" not in data            # the blocked private key
    assert "monitors" in data                 # our public uptime summary
    _wipe()


def test_public_monitor_shows_in_maintenance(client):
    _wipe()
    _app.create_maintenance_window(
        label="test window", kind="uptime", pattern="*",
        start_ts=int(_time.time()) - 60, end_ts=int(_time.time()) + 3600
    )
    cid = _mk_check(True)
    data = client.get("/api/public-status").get_json()
    mon = next(m for m in data["monitors"] if m["id"] == cid)
    assert mon["in_maintenance"] is True
    assert data["status"] == "maintenance"
    detail = client.get(f"/api/public-status/{cid}").get_json()
    assert detail["in_maintenance"] is True
    _app.DB.execute("DELETE FROM maintenance_windows")
    _app.DB.commit()
    _wipe()
