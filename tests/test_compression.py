"""HTTP compression stays transparent and leaves the live event stream alone."""
import gzip
import json
import unittest
from unittest.mock import patch

import brotli

import app


class TestCompression(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_dashboard_html_is_brotli_compressed(self):
        plain = self.client.get("/")
        compressed = self.client.get("/", headers={"Accept-Encoding": "br"})

        self.assertEqual(compressed.status_code, 200)
        self.assertEqual(compressed.mimetype, "text/html")
        self.assertEqual(compressed.headers.get("Content-Encoding"), "br")
        self.assertEqual(brotli.decompress(compressed.data), plain.data)

    def test_javascript_response_is_gzip_compressed(self):
        source = "const sample = 'homelab';\n" * 100
        with app.app.test_request_context(
            "/static/test.js", headers={"Accept-Encoding": "gzip"}
        ):
            response = app.app.response_class(
                source, mimetype="application/javascript"
            )
            compressed = app.app.process_response(response)

        self.assertEqual(compressed.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(compressed.get_data()), source.encode())

    def test_large_json_response_is_gzip_compressed(self):
        payload = {"items": ["sample-value"] * 200}
        with patch.object(app, "live_payload", return_value=payload):
            response = self.client.get(
                "/api/now", headers={"Accept-Encoding": "gzip"}
            )

        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(response.data)), payload)

    def test_event_stream_is_not_compressed(self):
        response = self.client.get(
            "/api/stream",
            headers={"Accept-Encoding": "br, gzip, deflate, zstd"},
            buffered=False,
        )
        try:
            self.assertEqual(response.mimetype, "text/event-stream")
            self.assertIsNone(response.headers.get("Content-Encoding"))
            self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")
        finally:
            response.close()
