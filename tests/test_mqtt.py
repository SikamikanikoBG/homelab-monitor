"""Tests for the Home Assistant / MQTT auto-discovery publisher (E4).

Covers, with ALL sockets mocked (no real broker in CI):
  • CONNECT / PUBLISH packet byte-encoding for known inputs, incl. the
    remaining-length varint and the 2-byte-prefixed UTF-8 string framing.
  • HA discovery payload shape: topics, unique_id, state_topic, shared device.
  • state payload reflects the same metric source values the dashboard uses.
  • mqtt_pass is a SECRET: masked in /api/settings + keep/CLEAR round-trip.
  • OFF / no-host => the publisher config is None (thread makes NO socket).
  • unreachable broker => caught, no raise, status records a safe last_error.
  • /api/mqtt/test returns a clean ok/error (never 500) and never enables.
  • no broker credentials leak into error text.
"""
import os
import sys
import json
import socket
import struct
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class TestMqttPacketEncoding(unittest.TestCase):
    def test_remaining_length_varint(self):
        self.assertEqual(app._mqtt_remaining_length(0), b"\x00")
        self.assertEqual(app._mqtt_remaining_length(127), b"\x7f")
        self.assertEqual(app._mqtt_remaining_length(128), b"\x80\x01")
        self.assertEqual(app._mqtt_remaining_length(16383), b"\xff\x7f")
        self.assertEqual(app._mqtt_remaining_length(16384), b"\x80\x80\x01")
        with self.assertRaises(ValueError):
            app._mqtt_remaining_length(268435456)

    def test_string_framing(self):
        self.assertEqual(app._mqtt_str("MQTT"), b"\x00\x04MQTT")
        self.assertEqual(app._mqtt_str(""), b"\x00\x00")
        # multibyte UTF-8 length is in BYTES not chars
        self.assertEqual(app._mqtt_str("é"), b"\x00\x02" + "é".encode("utf-8"))

    def test_connect_packet_no_auth(self):
        pkt = app._mqtt_connect_packet("cid", keepalive=60)
        self.assertEqual(pkt[0], 0x10)                       # CONNECT type
        # remaining length then body
        body = pkt[2:]
        self.assertTrue(body.startswith(b"\x00\x04MQTT\x04"))  # name + level 4
        flags = body[7]                                     # after 6-byte name + 1 level
        self.assertEqual(flags & 0x02, 0x02)                # clean session
        self.assertEqual(flags & 0x80, 0)                   # no username flag
        self.assertEqual(body[8:10], struct.pack(">H", 60))  # keepalive
        self.assertEqual(body[10:], app._mqtt_str("cid"))

    def test_connect_packet_with_auth_sets_flags_and_payload(self):
        pkt = app._mqtt_connect_packet("cid", "alice", "s3cret", keepalive=30)
        body = pkt[2:]
        flags = body[7]
        self.assertEqual(flags & 0x80, 0x80)                # username flag
        self.assertEqual(flags & 0x40, 0x40)                # password flag
        payload = body[10:]
        self.assertEqual(payload,
                         app._mqtt_str("cid") + app._mqtt_str("alice") + app._mqtt_str("s3cret"))

    def test_publish_packet_retain_and_framing(self):
        pkt = app._mqtt_publish_packet("a/b", "x", retain=True)
        self.assertEqual(pkt[0], 0x31)                      # PUBLISH | retain
        body = pkt[2:]
        self.assertEqual(body, app._mqtt_str("a/b") + b"x")
        # without retain
        pkt2 = app._mqtt_publish_packet("a/b", "x", retain=False)
        self.assertEqual(pkt2[0], 0x30)

    def test_publish_rejects_qos(self):
        with self.assertRaises(ValueError):
            app._mqtt_publish_packet("t", "p", qos=1)


class TestMqttDiscovery(unittest.TestCase):
    def test_discovery_topics_and_device(self):
        msgs = app._mqtt_discovery_messages("homeassistant")
        self.assertEqual(len(msgs), len(app._MQTT_SENSORS))
        topics = [t for t, _, _ in msgs]
        self.assertIn("homeassistant/sensor/homelab_gpu_util/config", topics)
        self.assertIn("homeassistant/sensor/homelab_power_total/config", topics)
        for topic, payload, retain in msgs:
            self.assertTrue(retain, "discovery configs must be retained")
            cfg = json.loads(payload)
            self.assertTrue(cfg["unique_id"].startswith("homelab_"))
            self.assertEqual(cfg["state_topic"], "homelab/monitor/state")
            # shared device block groups every sensor under one HA device
            self.assertEqual(cfg["device"]["identifiers"], ["homelab_monitor"])
            self.assertEqual(cfg["device"]["name"], "HomeLab Monitor")

    def test_discovery_prefix_sanitised(self):
        msgs = app._mqtt_discovery_messages("/custom/")
        self.assertTrue(all(t.startswith("custom/sensor/") for t, _, _ in msgs))
        msgs2 = app._mqtt_discovery_messages("")
        self.assertTrue(all(t.startswith("homeassistant/") for t, _, _ in msgs2))


class TestMqttStateReflectsMetrics(unittest.TestCase):
    def test_state_uses_latest_snapshot(self):
        with mock.patch.dict(app.LATEST, {"util": 73.4, "mem_used": 1234,
                                          "power": 200.0, "cpu_power": 50.0,
                                          "dram_power": 10.0, "temp": 61.2},
                             clear=False):
            st = app._mqtt_collect_state()
        self.assertEqual(st["gpu_util"], 73.4)
        self.assertEqual(st["gpu_vram_used"], 1234)
        self.assertEqual(st["gpu_power"], 200.0)
        self.assertEqual(st["gpu_temp"], 61.2)
        self.assertEqual(st["power_total"], 260.0)   # 200+50+10
        # uptime keys always present (safe defaults)
        self.assertIn("uptime_total", st)
        self.assertIn("uptime_up", st)


class TestMqttSecretHandling(unittest.TestCase):
    def test_mqtt_pass_is_a_secret(self):
        self.assertIn("mqtt_pass", app.SETTING_SECRETS)

    def test_public_settings_masks_password(self):
        app.save_settings({"mqtt_pass": "topsecret"})
        try:
            pub = app._public_settings()
            self.assertNotIn("mqtt_pass", pub)
            self.assertTrue(pub.get("mqtt_pass_set"))
        finally:
            app.save_settings({"mqtt_pass": ""})

    def test_keep_and_clear_round_trip(self):
        app.save_settings({"mqtt_pass": "abc123"})
        try:
            # absent key => unchanged (keep)
            app.save_settings({"mqtt_host": "broker.lan"})
            self.assertEqual(app.get_settings()["mqtt_pass"], "abc123")
            # empty string => CLEAR
            app.save_settings({"mqtt_pass": ""})
            self.assertEqual(app.get_settings()["mqtt_pass"], "")
        finally:
            app.save_settings({"mqtt_pass": "", "mqtt_host": ""})


class TestMqttInert(unittest.TestCase):
    def test_cfg_none_when_disabled(self):
        self.assertIsNone(app._mqtt_cfg_from_settings(
            {"mqtt_enabled": "0", "mqtt_host": "broker.lan"}))

    def test_cfg_none_when_no_host(self):
        self.assertIsNone(app._mqtt_cfg_from_settings(
            {"mqtt_enabled": "1", "mqtt_host": ""}))

    def test_cfg_clamps_interval(self):
        cfg = app._mqtt_cfg_from_settings(
            {"mqtt_enabled": "1", "mqtt_host": "h", "mqtt_interval_sec": "3"})
        self.assertEqual(cfg["interval"], 10)   # min ~10

    def test_disabled_makes_no_socket(self):
        """A disabled/unconfigured publisher must never open a socket."""
        with mock.patch.object(app.socket, "create_connection") as cc:
            cfg = app._mqtt_cfg_from_settings({"mqtt_enabled": "0", "mqtt_host": "h"})
            self.assertIsNone(cfg)
            cc.assert_not_called()


class TestMqttRobustness(unittest.TestCase):
    def test_unreachable_broker_is_caught(self):
        """A refused connection must raise out of _mqtt_session_publish so the
        worker's try/except records it — and must NOT be a credential leak."""
        cfg = {"host": "broker.lan", "port": 1883, "tls": False,
               "user": "alice", "pass": "supersecret", "prefix": "homeassistant"}
        with mock.patch.object(app.socket, "create_connection",
                               side_effect=ConnectionRefusedError()):
            with self.assertRaises(Exception) as ctx:
                app._mqtt_session_publish(cfg)
            self.assertNotIn("supersecret", str(ctx.exception))

    def test_sanitize_err_no_cred_leak(self):
        msg = app._mqtt_sanitize_err(ConnectionRefusedError())
        self.assertEqual(msg, "connection refused")
        self.assertNotIn("secret", app._mqtt_sanitize_err(socket.timeout()))

    def test_session_publish_writes_expected_packets(self):
        """With a fully-mocked socket, a successful session sends CONNECT, the
        discovery configs, availability, and state — all on the wire."""
        sent = []

        class FakeSock:
            def settimeout(self, t): pass
            def sendall(self, b): sent.append(b)
            def recv(self, n): return b"\x20\x02\x00\x00"   # CONNACK accepted
            def close(self): pass

        cfg = {"host": "h", "port": 1883, "tls": False, "user": "", "pass": "",
               "prefix": "homeassistant"}
        with mock.patch.object(app.socket, "create_connection", return_value=FakeSock()):
            n = app._mqtt_session_publish(cfg, publish_discovery=True)
        # discovery (N sensors) + availability + state
        self.assertEqual(n, len(app._MQTT_SENSORS) + 2)
        self.assertEqual(sent[0][0], 0x10)               # first packet is CONNECT
        # state topic appears on the wire
        joined = b"".join(sent)
        self.assertIn(b"homelab/monitor/state", joined)
        self.assertIn(b"homelab/monitor/availability", joined)

    def test_connack_rejection_raises_safe(self):
        class FakeSock:
            def settimeout(self, t): pass
            def sendall(self, b): pass
            def recv(self, n): return b"\x20\x02\x00\x04"  # bad user/pass
            def close(self): pass
        cfg = {"host": "h", "port": 1883, "tls": False, "user": "u",
               "pass": "pw", "prefix": "homeassistant"}
        with mock.patch.object(app.socket, "create_connection", return_value=FakeSock()):
            with self.assertRaises(Exception) as ctx:
                app._mqtt_session_publish(cfg)
            self.assertNotIn("pw", str(ctx.exception))


class TestMqttTestEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_no_host_returns_clean_not_configured(self):
        app.save_settings({"mqtt_host": "", "mqtt_enabled": "0"})
        r = self.client.post("/api/mqtt/test", json={})
        self.assertEqual(r.status_code, 200)   # never a 500
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertIn("not configured", j["error"])
        # endpoint must NOT enable the integration
        self.assertEqual(app.get_settings()["mqtt_enabled"], "0")

    def test_unreachable_returns_clean_error(self):
        with mock.patch.object(app.socket, "create_connection",
                               side_effect=ConnectionRefusedError()):
            r = self.client.post("/api/mqtt/test", json={"mqtt_host": "broker.lan"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["error"], "connection refused")
        # still disabled
        self.assertEqual(app.get_settings()["mqtt_enabled"], "0")

    def test_success_does_not_enable(self):
        class FakeSock:
            def settimeout(self, t): pass
            def sendall(self, b): pass
            def recv(self, n): return b"\x20\x02\x00\x00"
            def close(self): pass
        with mock.patch.object(app.socket, "create_connection", return_value=FakeSock()):
            r = self.client.post("/api/mqtt/test", json={"mqtt_host": "broker.lan"})
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(app.get_settings()["mqtt_enabled"], "0")

    def tearDown(self):
        app.save_settings({"mqtt_host": "", "mqtt_enabled": "0", "mqtt_pass": ""})


if __name__ == "__main__":
    unittest.main()
