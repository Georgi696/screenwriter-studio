"""The game desk serves HTML, not Gradio. No crew run, no image credits."""

import json
import os
import re
import socket
import subprocess
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
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
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
        self.assertIn('id="suggest"', page)
        self.assertIn("Suggest a story", page)
        self.assertIn("Strengthen this", page)
        self.assertIn("/api/brief/suggest", page)
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
            sock.sendall(f"GET /api/events HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
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

    def test_only_loopback_is_bound(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_untrusted_host_and_origin_are_rejected(self):
        for headers in [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Sec-Fetch-Site": "cross-site"},
        ]:
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/status", headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)

    def test_cross_origin_post_cannot_start_paid_run(self):
        from unittest import mock
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/run", data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
        )
        with mock.patch("desk.start_crew") as start:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            start.assert_not_called()

    def test_run_passes_custom_shot_limit(self):
        from unittest import mock
        with mock.patch("desk.start_crew") as start:
            self._post_json("/api/run", {"idea": "test", "max_shots": 6})
            start.assert_called_once_with("test", False, False, 6)

    def test_invalid_shot_limit_does_not_start_run(self):
        from unittest import mock
        for value in [True, 0, 241, 1.5, "6"]:
            with mock.patch("desk.start_crew") as start:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json("/api/run", {"max_shots": value})
                self.assertEqual(caught.exception.code, 400)
                start.assert_not_called()

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


    def _post_json(self, path: str, payload: dict) -> bytes:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as res:
            return res.read()

    def test_suggest_route_returns_the_brief_and_does_not_start_the_crew(self):
        from unittest import mock

        with mock.patch("desk.suggest_brief", return_value="A 30-second 9:16 film about Nia.") as call:
            body = json.loads(self._post_json("/api/brief/suggest", {"idea": "nia and a ferry"}).decode("utf-8"))
        self.assertEqual(body["brief"], "A 30-second 9:16 film about Nia.")
        call.assert_called_once_with("nia and a ferry")
        status = json.loads(self._get("/api/status").decode("utf-8"))
        self.assertFalse(status["running"])

    def test_suggest_route_reports_a_missing_key(self):
        from unittest import mock

        from brief_suggest import BriefSuggestError

        message = "KIE_API_KEY is not set. Add it to `.env` in the repo root."
        with mock.patch("desk.suggest_brief", side_effect=BriefSuggestError(message)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post_json("/api/brief/suggest", {"idea": ""})
        self.assertEqual(caught.exception.code, 503)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("KIE_API_KEY is not set", payload["error"])

    def test_restore_marks_abandoned_run_interrupted(self):
        from unittest import mock
        import desk
        original = desk._board
        try:
            with mock.patch("desk.load_status", return_value={"running": True, "active": "writer"}):
                desk._restore()
            self.assertFalse(desk._board.running)
            self.assertIsNone(desk._board.active)
            self.assertIn("interrupted", desk._board.note)
        finally:
            desk._board = original
            desk.hub.revision = original.revision

    def test_suggest_route_rejects_bad_json(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/brief/suggest",
            data=b"not-json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)


def _reader_renderer_js() -> str:
    html = Path("desk.html").read_text(encoding="utf-8")
    start = html.index("function escapeText(")
    end = html.index("function highlightJson(")
    return html[start:end]


class MarkdownPreviewTest(unittest.TestCase):
    def test_shot_sheet_table_renders_and_suffix_case_is_markdown(self):
        fixture = "\n".join(
            [
                "| # | Dur | Framing | Action | Audio | Notes |",
                "|---|---|---|---|---|---|",
                "| 1 | 4s | Vertical wide shot of the night platform | Rain on the glass | Lukas, under his breath | Hold |",
                "| 2 | 6s | Close on Lukas | He whispers stay \\| with me through a very long action line that must stay inside this cell | Quiet | Rescue |",
                "| 3 | 2s | <script>alert(1)</script> | ok | — | — |",
                "",
                "- The repeated phrase Stay with me is planted as Lukas's immediate rescue command.",
            ]
        )
        prose = "\n".join(
            [
                "# Title",
                "",
                "A **bold** and *italic* [link](https://example.com).",
                "",
                "- one",
                "",
                "```",
                "code",
                "```",
            ]
        )
        aligned = "| Left | Right |\n|:---|---:|\n| a | b |\n"
        driver = _reader_renderer_js() + "\n" + (
            "const fixture = " + json.dumps(fixture) + ";\n"
            "const prose = " + json.dumps(prose) + ";\n"
            "const aligned = " + json.dumps(aligned) + ";\n"
            "process.stdout.write(JSON.stringify({\n"
            "  html: renderMarkdown(fixture),\n"
            "  prose: renderMarkdown(prose),\n"
            "  aligned: renderMarkdown(aligned),\n"
            "  formats: {\n"
            "    upper: readerFormatFor('productions/demo/02_SHOTS.MD'),\n"
            "    lower: readerFormatFor('02_shots.md'),\n"
            "    mixed: readerFormatFor('Notes.Md'),\n"
            "    markdown: readerFormatFor('README.MARKDOWN'),\n"
            "    fountain: readerFormatFor('scene.fountain'),\n"
            "    text: readerFormatFor('notes.TXT')\n"
            "  }\n"
            "}));\n"
        )
        proc = subprocess.run(["node", "-"], input=driver, text=True, capture_output=True, check=True)
        result = json.loads(proc.stdout)
        html = result["html"]
        rows = re.findall(r"<tr>(.*?)</tr>", html)
        self.assertGreaterEqual(len(rows), 4)
        header = re.findall(r"<th[^>]*>(.*?)</th>", rows[0])
        self.assertEqual(header, ["#", "Dur", "Framing", "Action", "Audio", "Notes"])
        self.assertIn("<table>", html)
        self.assertNotIn("---", html)
        self.assertNotIn("|---|", html)
        self.assertNotIn("|# Dur", html)
        self.assertNotRegex(html, r"<p>[^<]*\|")
        action = re.findall(r"<td[^>]*>(.*?)</td>", rows[2])
        self.assertEqual(len(action), 6)
        self.assertIn("stay | with me", action[3])
        self.assertIn("must stay inside this cell", action[3])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script", html.lower())
        self.assertLess(html.index("</table>"), html.index("<li>"))
        self.assertIn(
            "<li>The repeated phrase Stay with me is planted as Lukas's immediate rescue command.</li>",
            html,
        )
        prose_html = result["prose"]
        self.assertIn("<h1>Title</h1>", prose_html)
        self.assertIn("<strong>bold</strong>", prose_html)
        self.assertIn("<em>italic</em>", prose_html)
        self.assertIn('href="https://example.com"', prose_html)
        self.assertIn("<li>one</li>", prose_html)
        self.assertIn("<pre", prose_html)
        self.assertIn("code", prose_html)
        aligned_html = result["aligned"]
        aligned_header = re.findall(r"<th\b([^>]*)>", aligned_html)
        self.assertIn("text-align:left", aligned_header[0])
        self.assertIn("text-align:right", aligned_header[1])
        self.assertEqual(
            result["formats"],
            {
                "upper": "md",
                "lower": "md",
                "mixed": "md",
                "markdown": "md",
                "fountain": "",
                "text": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
