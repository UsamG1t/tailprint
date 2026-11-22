import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from tailprint.backends import DemoBackend
from tailprint.network import Network, peer_catalog
from tailprint.server import Application, Server
from tailprint.store import Store


class HTTPTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        net = Network({"Self": {"HostName": "desk", "TailscaleIPs": []}}, 8787)
        self.app = Application(Store(path), DemoBackend(), net, path)
        self.server = Server("127.0.0.1", 0, self.app)
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.headers = {"Origin": self.origin, "X-Tailprint": "1"}
        net.known["127.0.0.1"] = "laptop"
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, body, headers or {})
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def test_web_assets_are_packaged(self):
        for path, marker in [
            ("/", b"TailPrint"),
            ("/app.js", b"/api/jobs"),
            ("/style.css", b".wordmark"),
        ]:
            with self.subTest(path=path):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                try:
                    conn.request("GET", path)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(marker, response.read())
                finally:
                    conn.close()

    def test_registration_and_catalog(self):
        self.assertEqual(self.request("GET", "/api/local")[0], 404)
        self.assertEqual(
            self.request("POST", "/api/publish", b"{}", self.headers)[0], 404
        )
        self.server.admin = True
        self.assertEqual(
            self.request(
                "POST",
                "/api/publish",
                b'{"name":"Studio Laser","enabled":true}',
                self.headers,
            )[0],
            200,
        )
        self.assertEqual(peer_catalog("127.0.0.1", self.port), ["Studio Laser"])

    def test_request_controls(self):
        self.assertEqual(
            self.request("GET", "/api/jobs", headers={"Host": "outside.example"})[0],
            403,
        )
        self.assertEqual(
            self.request(
                "POST", "/api/jobs", b"doc", {"Origin": "https://other.example"}
            )[0],
            403,
        )
        self.app.net.known.clear()
        self.assertEqual(self.request("GET", "/api/jobs")[0], 403)

    def test_submission_and_retry(self):
        self.app.store.publish("Laser", True)
        query = urlencode(
            dict(id=str(uuid4()), printer="Laser", copies=1, name="a.pdf")
        )
        headers = {**self.headers, "Content-Type": "application/octet-stream"}
        for _ in range(2):
            self.assertEqual(
                self.request("POST", "/api/jobs?" + query, b"%PDF-1.7\n%%EOF", headers)[
                    0
                ],
                202,
            )
        self.assertEqual(len(self.request("GET", "/api/jobs")[1]), 1)
        self.assertEqual(
            self.request("POST", "/api/jobs?" + query, b"bad", headers)[0], 415
        )
