"""The game desk serves HTML, not Gradio. No crew run, no image credits."""

import io
import json
import os
import re
import socket
import subprocess
import threading
import tempfile
from unittest import mock
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from screenwriter_studio.paths import PROJECT_ROOT
from screenwriter_studio.web.server import STATIC


class DeskDisconnectTest(unittest.TestCase):
    def test_reset_while_reading_request_is_quiet(self):
        from screenwriter_studio.web.server import DeskHandler

        request = mock.Mock()
        request.makefile.return_value.readline.side_effect = ConnectionResetError(54, "Connection reset by peer")
        handler = DeskHandler(request, ("127.0.0.1", 12345), mock.Mock())
        self.assertTrue(handler.close_connection)
        request.makefile.return_value.close.assert_called_once()

    def test_disconnect_while_writing_response_is_quiet(self):
        from screenwriter_studio.web.server import DeskHandler

        for error in (BrokenPipeError, ConnectionResetError):
            with self.subTest(error=error):
                request = mock.Mock()
                request.makefile.return_value = io.BytesIO(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                request.sendall.side_effect = error()
                server = mock.Mock(server_address=("127.0.0.1", 80))
                handler = DeskHandler(request, ("127.0.0.1", 12345), server)
                self.assertTrue(handler.close_connection)
                request.sendall.assert_called_once()

    def test_unexpected_errors_still_propagate(self):
        from screenwriter_studio.web.server import DeskHandler

        for error in (RuntimeError, OSError):
            with self.subTest(error=error):
                request = mock.Mock()
                request.makefile.return_value.readline.side_effect = error("unexpected failure")
                with self.assertRaises(error):
                    DeskHandler(request, ("127.0.0.1", 12345), mock.Mock())


class DeskPageTest(unittest.TestCase):
    def setUp(self):
        from screenwriter_studio.web.server import bind, Hub
        from screenwriter_studio.web.board import Board

        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for replacement in [
            mock.patch("screenwriter_studio.web.board.STATUS_PATH", Path(state.name) / "desk_status.json"),
            mock.patch("screenwriter_studio.web.server._board", Board()),
            mock.patch("screenwriter_studio.web.server.hub", Hub()),
        ]:
            replacement.start()
            self.addCleanup(replacement.stop)

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
        self.assertIn('id="proceed"', page)
        self.assertIn("Proceed with this draft", page)
        self.assertIn('id="stop"', page)
        self.assertIn('class="stop"', page)
        self.assertIn(">Stop</button>", page)
        self.assertNotIn('id="stop" class="ghost"', page)
        self.assertEqual(page.count('aria-label="What is this?"'), 3)
        self.assertEqual(page.count(">?</button>"), 3)
        self.assertNotIn(">What is this?</button>", page)
        self.assertIn("Suggest a story", page)
        self.assertIn("Strengthen this", self._get("/static/desk.js").decode())
        script = self._get("/static/desk.js").decode()
        self.assertIn("/api/brief/suggest", script)
        self.assertIn("/api/proceed", script)
        self.assertIn("/api/stop", script)
        self.assertIn("stop.hidden = !state.running", script)
        css = self._get("/static/desk.css").decode()
        self.assertIn("button.stop", css)
        self.assertIn("background: #a3261b", css)
        self.assertIn("color: #fff", css)
        self.assertNotIn("import gradio", (PROJECT_ROOT / "run.py").read_text(encoding="utf-8"))

    def test_static_assets_are_served_and_private_paths_are_not(self):
        for name, mime in [("desk.css", "text/css"), ("desk.js", "text/javascript")]:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/static/{name}", timeout=5) as response:
                self.assertIn(mime, response.headers["Content-Type"])
                self.assertEqual(response.read(), (STATIC / name).read_bytes())
        for path in ["/static/../server.py", "/static/../../.state/desk_status.json"]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._get(path)
            self.assertEqual(caught.exception.code, 404)

    def test_status_has_no_key_and_no_image_paths(self):
        status = json.loads(self._get("/api/status").decode("utf-8"))
        self.assertIn("station", status)
        self.assertIn("note", status)
        self.assertIn("log", status)
        self.assertIn("can_proceed", status)
        self.assertFalse(status["can_proceed"])
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

    def test_stop_rejects_cross_origin_and_invokes_cancel(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/stop",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
        )
        with mock.patch("screenwriter_studio.web.server.stop_run") as cancel:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            cancel.assert_not_called()
        with mock.patch("screenwriter_studio.web.server.stop_run") as cancel:
            body = json.loads(self._post("/api/stop").decode("utf-8"))
        self.assertTrue(body["ok"])
        cancel.assert_called_once_with()

    def test_stop_run_sets_the_batch_event_and_cancels_the_crew(self):
        import screenwriter_studio.web.server as desk
        from screenwriter_studio.events import StudioEvent

        event = threading.Event()
        future = mock.Mock()
        future.done.return_value = False
        previous_cancel = desk._cancel
        previous_future = desk._future
        previous_serial = desk._run_serial
        desk._cancel = event
        desk._future = future
        desk._board.running = True
        desk._board.active = "writer"
        desk._board.images = [{"id": "s01", "state": "running"}]
        try:
            desk.stop_run()
            self.assertTrue(event.is_set())
            future.cancel.assert_called_once_with()
            self.assertFalse(desk._board.running)
            self.assertIsNone(desk._board.active)
            self.assertEqual(desk._board.note, "Generation stopped. Saved images can be resumed.")
            self.assertEqual(desk._board.images[0]["state"], "failed")
            self.assertEqual(desk._board.images[0]["error"], "Generation stopped. Saved images can be resumed.")
            desk._apply(previous_serial, StudioEvent("writing", "**Screenwriter** is writing.\n\n"))
            self.assertFalse(desk._board.running)
            self.assertEqual(desk._board.note, "Generation stopped. Saved images can be resumed.")
        finally:
            desk._cancel = previous_cancel
            desk._future = previous_future
            desk._run_serial = previous_serial

    def test_cross_origin_post_cannot_start_paid_run(self):
        from unittest import mock
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/run", data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
        )
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            start.assert_not_called()

    def test_run_passes_custom_shot_limit(self):
        from unittest import mock
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            self._post_json("/api/run", {"idea": "test", "max_shots": 6})
            start.assert_called_once_with("test", False, False, 6, "manual")

    def test_inspection_mode_and_review_route(self):
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            self._post_json("/api/run", {"idea": "test", "inspection_mode": "auto"})
            start.assert_called_once_with("test", False, False, None, "auto")
        for mode in ("off", None, [], True):
            with mock.patch("screenwriter_studio.web.server.start_crew") as start:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json("/api/run", {"inspection_mode": mode})
                self.assertEqual(caught.exception.code, 400)
                start.assert_not_called()
        decision = {"id": "s01", "review_id": "version-token", "action": "repair"}
        with mock.patch("screenwriter_studio.web.server.resume_images") as resume:
            self._post_json("/api/images/review", decision)
            resume.assert_called_once_with(decision)
        with mock.patch("screenwriter_studio.web.server.resume_images", side_effect=ValueError("Stale review")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post_json("/api/images/review", decision)
            self.assertEqual(caught.exception.code, 409)

    def test_proceed_route_continues_saved_pages_and_rejects_a_blank_desk(self):
        with mock.patch("screenwriter_studio.web.server.proceed_saved") as proceed:
            self._post_json("/api/proceed", {"dry_run": True, "inspection_mode": "auto"})
            proceed.assert_called_once_with(True, "auto")
        for mode in ("off", None, [], True):
            with mock.patch("screenwriter_studio.web.server.proceed_saved") as proceed:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json("/api/proceed", {"inspection_mode": mode})
                self.assertEqual(caught.exception.code, 400)
                proceed.assert_not_called()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/proceed",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.example"},
        )
        with mock.patch("screenwriter_studio.web.server.proceed_saved") as proceed:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            proceed.assert_not_called()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post_json("/api/proceed", {})
        self.assertEqual(caught.exception.code, 409)

    def test_saved_pages_can_proceed_until_image_jobs_exist(self):
        import screenwriter_studio.web.server as desk
        from screenwriter_studio.schemas import ScriptVerdict
        from tests.fixtures import _piece

        development, screenplay = _piece()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "productions" / "hall"
            folder.mkdir(parents=True)
            (folder / "00_development.json").write_text(development.model_dump_json(), encoding="utf-8")
            (folder / "01_screenplay.json").write_text(screenplay.model_dump_json(), encoding="utf-8")
            (folder / "verdict.json").write_text(
                ScriptVerdict(passed=False, issues=["Shot 1 has two actions."]).model_dump_json(),
                encoding="utf-8",
            )
            with mock.patch.object(desk, "PRODUCTIONS", root / "productions"), mock.patch.object(desk, "WORKSPACE_ROOT", root):
                self.assertTrue(desk.can_proceed("productions/hall"))
                self.assertFalse(desk.can_proceed("../.env"))
                images = folder / "images"
                images.mkdir()
                (images / "jobs.json").write_text("{}", encoding="utf-8")
                self.assertFalse(desk.can_proceed("productions/hall"))

    def test_invalid_shot_limit_does_not_start_run(self):
        from unittest import mock
        for value in [True, 0, 241, 1.5, "6"]:
            with mock.patch("screenwriter_studio.web.server.start_crew") as start:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json("/api/run", {"max_shots": value})
                self.assertEqual(caught.exception.code, 400)
                start.assert_not_called()

    def test_library_rejects_escape(self):
        from screenwriter_studio.web.server import library_file

        self.assertIsNone(library_file("../.env"))
        self.assertIsNone(library_file("/etc/passwd"))
        self.assertIsNone(library_file("foo/../../.env"))

    def test_clear_log_keeps_the_session_and_new_session_resets_it(self):
        from screenwriter_studio.web.board import STATUS_PATH

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

        with mock.patch("screenwriter_studio.web.server.suggest_brief", return_value="A 30-second 9:16 film about Nia.") as call:
            body = json.loads(self._post_json("/api/brief/suggest", {"idea": "nia and a ferry"}).decode("utf-8"))
        self.assertEqual(body["brief"], "A 30-second 9:16 film about Nia.")
        call.assert_called_once_with("nia and a ferry")
        status = json.loads(self._get("/api/status").decode("utf-8"))
        self.assertFalse(status["running"])

    def test_suggest_route_reports_a_missing_key(self):
        from unittest import mock

        from screenwriter_studio.web.brief import BriefSuggestError

        message = "KIE_API_KEY is not set. Add it to `.env` in the repo root."
        with mock.patch("screenwriter_studio.web.server.suggest_brief", side_effect=BriefSuggestError(message)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post_json("/api/brief/suggest", {"idea": ""})
        self.assertEqual(caught.exception.code, 503)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("KIE_API_KEY is not set", payload["error"])

    def test_restore_marks_abandoned_run_interrupted(self):
        from unittest import mock
        import screenwriter_studio.web.server as desk
        original = desk._board
        try:
            with mock.patch("screenwriter_studio.web.server.load_status", return_value={"running": True, "active": "writer"}):
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
    html = (STATIC / "desk.js").read_text(encoding="utf-8")
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


class ReviewCardAbortTest(unittest.TestCase):
    def test_abort_sits_beside_approve_and_posts_keep(self):
        script = (STATIC / "desk.js").read_text(encoding="utf-8")
        css = (STATIC / "desk.css").read_text(encoding="utf-8")
        self.assertNotIn("Keep this image", script)
        self.assertNotRegex(css, r"\.model-col\.has-jobs \.ghost\s*\{")
        self.assertRegex(css, r"\.model-col\.has-jobs \.frame\.ghost\s*\{[^}]*display:\s*none")
        self.assertRegex(css, r"\.image-review \.review-actions \{[^}]*display:\s*flex;")
        actions_rule = re.search(r"\.image-review \.review-actions button \{([^}]*)\}", css)
        self.assertIsNotNone(actions_rule)
        actions_body = actions_rule.group(1)
        self.assertRegex(actions_body, r"display:\s*inline-flex")
        self.assertNotRegex(actions_body, r"display\s*:\s*none")
        self.assertIn("cursor: pointer", actions_body)
        self.assertRegex(actions_body, r"padding:\s*[^;]+")
        self.assertRegex(actions_body, r"width:\s*auto")
        ghost_rule = re.search(r"\.image-review \.review-actions button\.ghost \{([^}]*)\}", css)
        self.assertIsNotNone(ghost_rule)
        self.assertRegex(ghost_rule.group(1), r"border:\s*1px solid")
        self.assertNotRegex(ghost_rule.group(1), r"display\s*:\s*none")
        self.assertNotRegex(ghost_rule.group(1), r"visibility\s*:\s*hidden")
        for match in re.finditer(r"([^{}]+)\{([^{}]+)\}", css):
            selector, decls = match.group(1), match.group(2)
            if not re.search(r"display\s*:\s*none", decls):
                continue
            self.assertNotIn(".image-review", selector)
            if ".ghost" in selector:
                self.assertIn(".frame.ghost", re.sub(r"\s+", "", selector))
        start = script.index("function renderInspection(")
        end = script.index("\nfunction syncProceed(")
        driver = script[start:end] + r"""
const posted = [];
let last = null;
function reviewImage(data) { posted.push(data); return Promise.resolve(); }
function fetch() { return Promise.resolve({ json() { return Promise.resolve({}); } }); }
function apply() {}
function showReaderImage() {}
function setView() {}
class El {
  constructor(tag) {
    this.tag = tag; this.className = ""; this.children = []; this.parent = null;
    this.dataset = {}; this.disabled = false; this.listeners = {}; this._text = "";
  }
  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
  }
  addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (selector.startsWith(".") ? child.className.split(/\s+/).includes(selector.slice(1)) : child.tag === selector) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}
const document = { createElement(tag) { return new El(tag); } };
function paragraphs(frame) {
  const out = [];
  const walk = (node) => { if (node.tag === "p") out.push(node.textContent); node.children.forEach(walk); };
  walk(frame);
  return out;
}
function row(frame) {
  const actions = frame.querySelector(".review-actions");
  if (!actions) return [];
  return actions.children.map((button) => ({ text: button.textContent, className: button.className, action: button.dataset.action, disabled: button.disabled }));
}
function buttons(frame) {
  return frame.querySelectorAll("button").map((button) => ({
    tag: button.tag,
    type: button.type || "",
    text: button.textContent,
    className: button.className,
    action: button.dataset.action || "",
  }));
}
async function click(frame, action) {
  const button = frame.querySelectorAll("button").find((item) => item.dataset.action === action);
  posted.length = 0;
  for (const fn of button.listeners.click) await fn();
  return posted.slice();
}
function marked(frame) {
  return frame.querySelectorAll("button").filter((button) => button.dataset.action).map((button) => ({
    text: button.textContent, action: button.dataset.action, disabled: button.disabled
  }));
}
async function render(image, running) {
  const frame = new El("figure");
  renderInspection(frame, image, running);
  return { row: row(frame), paragraphs: paragraphs(frame), buttons: buttons(frame), marked: marked(frame), frame };
}
const proposed = {
  id: "actor", review_id: "version-token", state: "review", src: "/media?id=actor",
  repair_prompt: "Edit the coat.", repair_model: "gpt-image-2-5-sunburst-image-to-image", repairs: 0,
  inspection: { summary: "Wrong wardrobe", findings: ["Blue coat"], changes: ["Make it red"], preserve: ["Face"] }
};
const declined = { ...proposed, repair_prompt: "", repair_unavailable: "No executable repair is available." };
const kept = { ...proposed, state: "done", decision: "human accepted" };
const waiting = {
  id: "plate", review_id: "version-token", state: "review", src: "/media?id=plate", repairs: 0,
  inspection: { summary: "Inspection unavailable. Human review required.", findings: [], changes: [], preserve: [], uncertainty: "1 validation error" }
};
(async () => {
  const proposedFrame = await render(proposed, false);
  const declinedFrame = await render(declined, false);
  const keptFrame = await render(kept, false);
  const busy = await render(proposed, true);
  const waitingFrame = await render(waiting, true);
  process.stdout.write(JSON.stringify({
    proposed: { row: proposedFrame.row, paragraphs: proposedFrame.paragraphs, buttons: proposedFrame.buttons, abort: await click(proposedFrame.frame, "keep"), approve: await click(proposedFrame.frame, "repair") },
    declined: { row: declinedFrame.row, paragraphs: declinedFrame.paragraphs, buttons: declinedFrame.buttons, abort: await click(declinedFrame.frame, "keep") },
    kept: keptFrame.row,
    busy: busy.row,
    waiting: { marked: waitingFrame.marked, abort: await click(waitingFrame.frame, "keep") },
  }));
})();
"""
        proc = subprocess.run(["node", "-"], input=driver, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(
            result["proposed"]["row"],
            [
                {"text": "Approve these changes (uses credits)", "className": "go", "action": "repair", "disabled": False},
                {"text": "Abort", "className": "ghost", "action": "keep", "disabled": False},
            ],
        )
        self.assertIn("Approval spends credits on one repair and its inspection.", " ".join(result["proposed"]["paragraphs"]))
        helper = "Abort keeps the current image and spends no credits."
        for label in ("proposed", "declined"):
            card = result[label]
            self.assertIn(helper, card["paragraphs"])
            abort = [button for button in card["buttons"] if button["text"] == "Abort"]
            self.assertEqual(
                abort,
                [{"tag": "button", "type": "button", "text": "Abort", "className": "ghost", "action": "keep"}],
                f"{label} rendered the helper sentence without an Abort button",
            )
        self.assertEqual(result["proposed"]["abort"], [{"id": "actor", "review_id": "version-token", "action": "keep"}])
        self.assertEqual(result["proposed"]["approve"], [{"id": "actor", "review_id": "version-token", "action": "repair"}])
        self.assertEqual(result["declined"]["row"], [{"text": "Abort", "className": "ghost", "action": "keep", "disabled": False}])
        self.assertNotIn("Approval spends credits", " ".join(result["declined"]["paragraphs"]))
        self.assertIn("Abort keeps the current image and spends no credits.", result["declined"]["paragraphs"])
        self.assertEqual(result["declined"]["abort"], [{"id": "actor", "review_id": "version-token", "action": "keep"}])
        self.assertEqual(result["kept"], [])
        self.assertEqual(
            result["busy"],
            [
                {"text": "Approve these changes (uses credits)", "className": "go", "action": "repair", "disabled": True},
                {"text": "Abort", "className": "ghost", "action": "keep", "disabled": False},
            ],
        )
        self.assertEqual(
            result["waiting"]["marked"],
            [
                {"text": "Abort", "action": "keep", "disabled": False},
                {"text": "Inspect again (uses credits)", "action": "inspect", "disabled": True},
            ],
        )
        self.assertEqual(result["waiting"]["abort"], [{"id": "plate", "review_id": "version-token", "action": "keep"}])


class KeepWhileRunningTest(unittest.TestCase):
    def test_abort_keeps_saved_review_without_a_provider_call(self):
        import screenwriter_studio.web.server as desk
        from screenwriter_studio.web.board import Board

        png = b"\x89PNG\r\n\x1a\n" + b"saved"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = root / "productions" / "hall" / "images"
            images.mkdir(parents=True)
            plate = images / "plate.png"
            plate.write_bytes(png)
            spec = {
                "inspection": {"mode": "manual"},
                "aspect_ratio": "16:9",
                "resolution": "1K",
                "jobs": [
                    {"id": "plate", "kind": "location", "prompt": "A street"},
                    {"id": "shot", "kind": "keyframe", "prompt": "The street", "references": ["plate"]},
                ],
            }
            (images / "jobs.json").write_text(json.dumps(spec), encoding="utf-8")
            manifest = {
                "images": [
                    {
                        "id": "plate",
                        "kind": "location",
                        "state": "review",
                        "review_id": "version-token",
                        "path": str(plate),
                        "url": "https://example.test/plate.png",
                        "inspection": {
                            "verdict": "review",
                            "summary": "Inspection unavailable. Human review required.",
                            "findings": [],
                            "changes": [],
                            "preserve": [],
                            "uncertainty": "1 validation error for Inspection. Uncertain images or proposed changes require review.",
                        },
                    },
                    {"id": "shot", "kind": "keyframe", "state": "blocked", "blocked_by": ["plate"]},
                ]
            }
            (images / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            board = Board()
            board.running = True
            board.folder = "productions/hall"
            board.note = "Continuing saved image work."
            board.images = [dict(image) for image in manifest["images"]]
            with tempfile.TemporaryDirectory() as state, mock.patch.object(desk, "_board", board), mock.patch.object(desk, "PRODUCTIONS", root / "productions"), mock.patch.object(desk, "WORKSPACE_ROOT", root), mock.patch("screenwriter_studio.web.board.STATUS_PATH", Path(state) / "desk_status.json"), mock.patch("screenwriter_studio.web.server.run_batch") as batch:
                desk.resume_images({"id": "plate", "review_id": "version-token", "action": "keep"})
                batch.assert_not_called()
            saved = json.loads((images / "manifest.json").read_text(encoding="utf-8"))
            kept = saved["images"][0]
            self.assertEqual(kept["state"], "done")
            self.assertEqual(kept["decision"], "human accepted")
            self.assertEqual(plate.read_bytes(), png)
            self.assertEqual(saved["images"][1]["state"], "planned")
            self.assertNotIn("blocked_by", saved["images"][1])
            self.assertFalse(board.running)
            self.assertEqual(board.images[0]["state"], "done")


if __name__ == "__main__":
    unittest.main()
