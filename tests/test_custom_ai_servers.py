"""Unit tests for the custom AI-servers registry: parse_custom_servers
(setting value → clean descriptors) and probe_custom_server (a user-registered
host:port → model rows). http.client is faked per (ip,port,path), so these pin
the port-aware dispatch — the whole point of the feature — without touching the
network."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import probes


def _fake_http(routes):
    """An http.client.HTTPConnection stand-in serving canned JSON per
    (port, path). `routes` maps (port, path) -> body (None = 404)."""
    class FakeResp:
        def __init__(self, body, status=200):
            self._b = json.dumps(body).encode() if body is not None else b""
            self.status = status if body is not None else 404
        def read(self):
            return self._b
    class FakeConn:
        def __init__(self, ip, port, timeout=None):
            self.ip, self.port = ip, port
            self._path = None
        def request(self, method, path):
            self._path = path
        def getresponse(self):
            return FakeResp(routes.get((self.port, self._path)))
        def close(self):
            pass
    return FakeConn


class TestParseCustomServers(unittest.TestCase):
    def test_blank_is_empty_not_error(self):
        self.assertEqual(probes.parse_custom_servers(""), ([], None))
        self.assertEqual(probes.parse_custom_servers("   "), ([], None))
        self.assertEqual(probes.parse_custom_servers(None), ([], None))

    def test_valid_list(self):
        raw = json.dumps([{"name": "vllm", "host": "vader", "port": 8010,
                           "provider": "vllm"},
                          {"name": "oll", "host": "192.168.1.50", "port": 11434,
                           "provider": "ollama"}])
        out, err = probes.parse_custom_servers(raw)
        self.assertIsNone(err)
        self.assertEqual(len(out), 2)
        # Pre-fleet_host entries parse with fleet_host="" (= the hub).
        self.assertEqual(out[0], {"name": "vllm", "host": "vader", "port": 8010,
                                  "provider": "vllm", "fleet_host": ""})
        self.assertIsInstance(out[0]["port"], int)

    def test_fleet_host_is_kept(self):
        raw = json.dumps([{"name": "v", "host": "100.76.27.18", "port": 8010,
                           "provider": "vllm", "fleet_host": "vader"}])
        out, err = probes.parse_custom_servers(raw)
        self.assertIsNone(err)
        self.assertEqual(out[0]["fleet_host"], "vader")

    def test_fleet_host_missing_is_hub(self):
        out, err = probes.parse_custom_servers(json.dumps(
            [{"name": "v", "host": "h", "port": 80, "provider": "vllm"}]))
        self.assertIsNone(err)
        self.assertEqual(out[0]["fleet_host"], "")

    def test_fleet_host_stripped_and_capped(self):
        out, err = probes.parse_custom_servers(json.dumps(
            [{"name": "v", "host": "h", "port": 80, "provider": "vllm",
              "fleet_host": "  vader  "}]))
        self.assertIsNone(err)
        self.assertEqual(out[0]["fleet_host"], "vader")
        out, err = probes.parse_custom_servers(json.dumps(
            [{"name": "v", "host": "h", "port": 80, "provider": "vllm",
              "fleet_host": "x" * 41}]))
        self.assertIsNone(out)
        self.assertIn("40", err)

    def test_fleet_host_non_string_rejected(self):
        out, err = probes.parse_custom_servers(json.dumps(
            [{"name": "v", "host": "h", "port": 80, "provider": "vllm",
              "fleet_host": 42}]))
        # A number is not a fleet name — reject rather than silently coerce.
        self.assertIsNone(out)
        self.assertIsNotNone(err)

    def test_not_json(self):
        out, err = probes.parse_custom_servers("not json")
        self.assertIsNone(out)
        self.assertIsNotNone(err)

    def test_not_a_list(self):
        out, err = probes.parse_custom_servers('{"name": "x"}')
        self.assertIsNone(out)
        self.assertIsNotNone(err)

    def test_item_not_an_object(self):
        out, err = probes.parse_custom_servers('[ "just a string" ]')
        self.assertIsNone(out)
        self.assertIsNotNone(err)

    def test_bad_port(self):
        for port in (0, -1, 99999, "abc", None):
            out, err = probes.parse_custom_servers(json.dumps(
                [{"name": "x", "host": "h", "port": port, "provider": "vllm"}]))
            self.assertIsNone(out, f"port={port!r} should be rejected")
            self.assertIsNotNone(err)

    def test_unknown_provider_rejected(self):
        out, err = probes.parse_custom_servers(json.dumps(
            [{"name": "x", "host": "h", "port": 80, "provider": "nope"}]))
        self.assertIsNone(out)
        self.assertIsNotNone(err)

    def test_missing_fields_rejected(self):
        for missing in ("name", "host", "provider"):
            item = {"name": "x", "host": "h", "port": 80, "provider": "vllm"}
            del item[missing]
            out, err = probes.parse_custom_servers(json.dumps([item]))
            self.assertIsNone(out, f"missing {missing} should be rejected")

    def test_extra_fields_are_ignored(self):
        raw = json.dumps([{"name": "x", "host": "h", "port": 80, "provider": "vllm",
                           "note": "keep me out"}])
        out, err = probes.parse_custom_servers(raw)
        self.assertIsNone(err)
        self.assertNotIn("note", out[0])

    def test_duplicate_entry_rejected(self):
        item = {"name": "x", "host": "h", "port": 80, "provider": "vllm"}
        out, err = probes.parse_custom_servers(json.dumps([item, dict(item)]))
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        self.assertIn("twice", err)
        # Same name on a different port is fine.
        other = dict(item, port=81)
        out, err = probes.parse_custom_servers(json.dumps([item, other]))
        self.assertIsNone(err)
        self.assertEqual(len(out), 2)

    def test_oversized_list_rejected(self):
        item = {"name": "x", "host": "h", "port": 80, "provider": "vllm"}
        out, err = probes.parse_custom_servers(json.dumps([dict(item, port=80 + i) for i in range(21)]))
        self.assertIsNone(out)
        self.assertIn("20", err)

    def test_non_string_raw_is_coerced(self):
        # A direct API client may hand over the list itself; it is serialized
        # exactly the way the settings route stores it, then parsed.
        item = {"name": "x", "host": "h", "port": 80, "provider": "vllm"}
        out, err = probes.parse_custom_servers([item])
        self.assertIsNone(err)
        self.assertEqual(out, [{"name": "x", "host": "h", "port": 80,
                                "provider": "vllm", "fleet_host": ""}])


class TestValidateCustomServers(unittest.TestCase):
    """Door validation for the settings POST: error string or None, never an
    exception — a bad value must be rejected, not crash the route."""
    def _one(self, **kw):
        e = {"name": "x", "host": "h", "port": 80, "provider": "vllm"}
        e.update(kw)
        return e

    def test_blank_and_absent_are_fine(self):
        for raw in (None, "", "   "):
            self.assertIsNone(probes.validate_custom_servers(raw), f"raw={raw!r}")

    def test_valid_string_and_list(self):
        self.assertIsNone(probes.validate_custom_servers(json.dumps([self._one()])))
        self.assertIsNone(probes.validate_custom_servers([self._one()]))  # coerced

    def test_not_json(self):
        self.assertIn("JSON array", probes.validate_custom_servers("not json"))

    def test_not_a_list(self):
        self.assertIn("JSON array", probes.validate_custom_servers('{"name": "x"}'))

    def test_bad_entry_reports_the_reason(self):
        self.assertIn("port", probes.validate_custom_servers(
            json.dumps([self._one(port=99999)])))
        self.assertIn("provider", probes.validate_custom_servers(
            json.dumps([self._one(provider="nope")])))

    def test_duplicate_and_oversized_rejected(self):
        self.assertIn("twice", probes.validate_custom_servers(
            json.dumps([self._one(), self._one()])))
        self.assertIn("20", probes.validate_custom_servers(
            json.dumps([self._one(port=80 + i) for i in range(21)])))


class TestProbeCustomServer(unittest.TestCase):
    OPENAI = {"data": [{"id": "qwen3.8-27b"}, {"id": "glm-air"}]}

    def test_openai_provider_hits_the_user_port(self):
        # The whole point: vLLM is probed on the user's 8010, not a guessed port.
        with mock.patch.object(probes.http.client, "HTTPConnection",
                               _fake_http({(8010, "/v1/models"): self.OPENAI})):
            rows = probes.probe_custom_server({"name": "v", "host": "vader",
                                               "port": 8010, "provider": "vllm"})
        self.assertEqual([r[0] for r in rows], ["qwen3.8-27b", "glm-air"])
        # /v1/models lists what the server CAN serve → Idle rows (no live VRAM).
        self.assertTrue(all(r[1] is None for r in rows))

    def test_ollama_provider_uses_api_and_custom_port(self):
        ps = {"models": [{"name": "qwen3:8b", "size_vram": 1048576000, "size": 1048576000}]}
        with mock.patch.object(probes.http.client, "HTTPConnection",
                               _fake_http({(12345, "/api/ps"): ps})):
            rows = probes.probe_custom_server({"name": "o", "host": "127.0.0.1",
                                               "port": 12345, "provider": "ollama"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "qwen3:8b")
        self.assertIsNotNone(rows[0][1])     # ollama reports live VRAM → Loaded

    def test_sampler_descriptor_shape_uses_ip_not_host(self):
        # The sampler builds {"name","ip","port","provider"} (container-descriptor
        # shape), not "host". A probe that only read "host" silently returned []
        # on every sample: Test (which sends "host") passed, the AI tab never
        # showed the server.
        with mock.patch.object(probes.http.client, "HTTPConnection",
                               _fake_http({(8010, "/v1/models"): self.OPENAI})):
            rows = probes.probe_custom_server({"name": "v", "ip": "10.0.0.5",
                                               "port": 8010, "provider": "vllm"})
        self.assertEqual([r[0] for r in rows], ["qwen3.8-27b", "glm-air"])

    def test_bad_descriptor_returns_empty(self):
        for desc in ({"name": "x", "host": "", "port": 80, "provider": "vllm"},
                     {"name": "x", "host": "h", "port": 0, "provider": "vllm"},
                     {"name": "x", "host": "h", "port": 99999, "provider": "vllm"},
                     {"name": "x", "host": "h", "port": 80, "provider": ""}):
            self.assertEqual(probes.probe_custom_server(desc), [], f"desc={desc}")

    def test_unknown_provider_returns_empty(self):
        self.assertEqual(probes.probe_custom_server(
            {"name": "x", "host": "h", "port": 80, "provider": "nope"}), [])

    def test_non_numeric_port_degrades_not_raises(self):
        # The function's own contract: a malformed descriptor returns [], never
        # raises — a bad port in the stored setting must not take down a sample.
        self.assertEqual(probes.probe_custom_server(
            {"name": "x", "host": "h", "port": "abc", "provider": "vllm"}), [])
        self.assertEqual(probes.probe_custom_server(
            {"name": "x", "host": "h", "port": None, "provider": "vllm"}), [])

    def test_down_server_degrades_to_empty(self):
        with mock.patch.object(probes.http.client, "HTTPConnection",
                               _fake_http({})):   # nothing answers
            self.assertEqual(probes.probe_custom_server(
                {"name": "x", "host": "127.0.0.1", "port": 8010, "provider": "vllm"}), [])

    def test_oversized_idle_catalogue_is_collapsed(self):
        many = {"data": [{"id": f"m{i}"} for i in range(50)]}
        with mock.patch.object(probes.http.client, "HTTPConnection",
                               _fake_http({(8010, "/v1/models"): many})):
            rows = probes.probe_custom_server({"name": "x", "host": "h",
                                               "port": 8010, "provider": "vllm"})
        # CATALOG_MAX=15 → 50 idle models collapse to a single summary row.
        self.assertEqual(len(rows), 1)
        self.assertIn("models available", rows[0][0])


class TestOpenaiKeyDerivation(unittest.TestCase):
    def test_openai_keys_match_the_factory(self):
        # vllm is an OpenAI provider → port-addressable; whisper-asr is not
        # (it owns its own port ladder + /openapi.json check).
        self.assertIn("vllm", probes._OPENAI_KEYS)
        self.assertIn("llama.cpp", probes._OPENAI_KEYS)
        self.assertNotIn("whisper-asr-webservice", probes._OPENAI_KEYS)
        self.assertNotIn("triton", probes._OPENAI_KEYS)

    def test_every_openai_provider_is_in_probable_table(self):
        probable = {k for k, _ in probes.PROBES}
        self.assertTrue(probes._OPENAI_KEYS <= probable)


if __name__ == "__main__":
    unittest.main()
