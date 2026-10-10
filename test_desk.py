"""The game desk serves HTML, not Gradio. No crew run, no image credits."""

import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class DeskPageTest(unittest.TestCase):
    def setUp(self):
        from desk import bind

        self.httpd = bind(0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as res:
            return res.read()

    def _post(self, path: str) -> bytes:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=5) as res:
            return res.read()

    def test_page_is_a_board_not_gradio(self):
        page = self._get("/").decode("utf-8")
        self.assertNotIn("gradio", page.lower())
        self.assertIn('id="stage"', page)
        self.assertIn('id="return-rail"', page)
        self.assertIn('id="token"', page)
        self.assertIn('id="open-folder"', page)
        self.assertIn('id="clear-log"', page)
        self.assertIn('id="new-session"', page)
        self.assertNotIn("import gradio", Path("run.py").read_text(encoding="utf-8"))

    def test_status_has_no_key_and_no_image_paths(self):
        status = json.loads(self._get("/api/status").decode("utf-8"))
        self.assertIn("station", status)
        self.assertIn("note", status)
        self.assertIn("log", status)
        blob = json.dumps(status)
        key = os.environ.get("KIE_API_KEY", "")
        if key:
            self.assertNotIn(key, blob)
        for image in status["images"]:
            self.assertNotIn("path", image)

    def test_events_push_a_snapshot(self):
        sock = socket.create_connection(("127.0.0.1", self.port), 2)
        try:
            sock.sendall(b"GET /api/events HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            sock.settimeout(2)
            data = b""
            while b"\n\n" not in data:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertIn(b"text/event-stream", data)
        self.assertIn(b'"station"', data)

    def test_media_id_is_not_a_path(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/media?id=../.env", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_tunnel_log_url(self):
        from desk import public_url_from_tunnel_log

        log = (
            "Welcome to localhost.run!\n"
            "69e20590eedd2c.lhr.life tunneled with tls termination, "
            "https://69e20590eedd2c.lhr.life\n"
        )
        self.assertEqual(public_url_from_tunnel_log(log), "https://69e20590eedd2c.lhr.life")
        self.assertIsNone(public_url_from_tunnel_log("still waiting"))

    def test_ipv4_wildcard_is_in_the_listen_table(self):
        self.assertEqual(self.httpd.server_address[0], "0.0.0.0")
        needle = f"00000000:{self.port:04X}"
        with open("/proc/net/tcp", encoding="ascii") as handle:
            rows = handle.readlines()[1:]
        listening = any(row.split()[1] == needle and row.split()[3] == "0A" for row in rows)
        self.assertTrue(listening, f"{needle} not listening in /proc/net/tcp")

    def test_reachable_off_loopback(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            host = probe.getsockname()[0]
        finally:
            probe.close()
        if host.startswith("127."):
            self.skipTest("no routable address")
        with urllib.request.urlopen(f"http://{host}:{self.port}/", timeout=5) as res:
            page = res.read()
        self.assertIn(b'id="stage"', page)

    def test_library_rejects_escape(self):
        from desk import library_file

        self.assertIsNone(library_file("../.env"))
        self.assertIsNone(library_file("/etc/passwd"))
        self.assertIsNone(library_file("foo/../../.env"))

    def test_clear_log_keeps_the_session_and_new_session_resets_it(self):
        from board import STATUS_PATH

        original = STATUS_PATH.read_bytes() if STATUS_PATH.is_file() else None
        try:
            before = json.loads(self._get("/api/status").decode("utf-8"))
            cleared = json.loads(self._post("/api/log/clear").decode("utf-8"))
            self.assertTrue(cleared["ok"])
            after_log = json.loads(self._get("/api/status").decode("utf-8"))
            self.assertEqual(after_log["log"], [])
            self.assertEqual(after_log["brief"], before["brief"])
            self.assertEqual(after_log["folder"], before["folder"])
            self.assertEqual(len(after_log["images"]), len(before["images"]))
            reset = json.loads(self._post("/api/session/clear").decode("utf-8"))
            self.assertTrue(reset["ok"])
            fresh = json.loads(self._get("/api/status").decode("utf-8"))
            self.assertEqual(fresh["note"], "Waiting for a brief.")
            self.assertEqual(fresh["log"], [])
            self.assertEqual(fresh["images"], [])
            self.assertEqual(fresh["brief"], "")
            self.assertEqual(fresh["done"], [])
            self.assertFalse(fresh["running"])
        finally:
            if original is None:
                STATUS_PATH.unlink(missing_ok=True)
            else:
                STATUS_PATH.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
