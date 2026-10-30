"""Webhook server: bad requests get an answer, the token guards /report and stays out of the log."""
from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path

from bounce_triage.server import ServerConfig, TriageState, make_handler

ROOT = Path(__file__).resolve().parent.parent


class WebhookServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        cfg = ServerConfig("127.0.0.1", 0, ROOT / "samples", tmp / "state", tmp / "out", token="s3cret")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(TriageState(cfg)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.log = io.StringIO()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        with redirect_stderr(self.log):
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            status = resp.status
            resp.read()
        conn.close()
        return status

    def test_bad_requests_get_an_answer(self):
        self.assertEqual(self.request("POST", "/webhook?token=s3cret", b"\xff\xfe"), 400)
        self.assertEqual(self.request("POST", "/webhook?token=s3cret", b"", {"Content-Length": "abc"}), 400)
        self.assertEqual(self.request("POST", "/webhook?token=s3cret",
                                      json.dumps({"data": {"lead": {"custom_variables": 5}}}).encode()), 200)

    def test_token_guards_webhook_and_report_and_is_not_logged(self):
        self.assertEqual(self.request("POST", "/webhook"), 401)  # no body: a 401 answers before reading one
        self.assertEqual(self.request("GET", "/report"), 401)
        self.assertEqual(self.request("GET", "/report?token=s3cret"), 404)  # no bounce posted yet
        self.assertNotIn("s3cret", self.log.getvalue())


if __name__ == "__main__":
    unittest.main()
