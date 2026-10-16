"""The game desk serves HTML, not Gradio. No crew run, no image credits."""

import io
import json
import shutil
import os
import re
import socket
import subprocess
import threading
import tempfile
from unittest import mock
import unittest
from screenwriter_studio.web import library
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
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
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
        self.assertEqual(page.count('aria-label="What is this?"'), 8)
        self.assertEqual(page.count(">?</button>"), 8)
        # Saved model choices sit in one closed panel, apart from this film's choices.
        panel = page[page.index('id="models-panel"'):page.index("      </details>\n    </div>", page.index('id="models-panel"'))]
        self.assertNotIn('id="models-panel" open', page)
        for part in ('id="settings-images"', 'id="settings-video"', 'id="settings-writing"', 'id="settings-reset"', 'id="show-effort"'):
            self.assertIn(part, panel)
        self.assertIn('<dialog id="reference-dialog"', page)
        actions = page[page.index('class="brief-actions"'):]
        self.assertLess(actions.index('id="idea-cost"'), actions.index('id="run"'))
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

    def test_settings_route_saves_validates_and_resets(self):
        from screenwriter_studio import settings
        body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/settings", timeout=5).read())
        self.assertEqual(body["values"], settings.DEFAULTS)
        self.assertIn("kling", [m["id"] for m in body["schema"]["video"]["models"]])
        chosen = json.loads(json.dumps(settings.DEFAULTS))
        chosen["video"].update(selection="kling", mode="4K", parallel=2)
        saved = json.loads(self._post_json("/api/settings", {"values": chosen}))
        self.assertEqual(saved["values"]["video"]["mode"], "4K")
        self.assertEqual(settings.load()["video"]["parallel"], 2)
        chosen["video"].update(selection="seedance")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post_json("/api/settings", {"values": chosen})
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(settings.load()["video"]["selection"], "kling")
        json.loads(self._post_json("/api/settings", {"reset": True}))
        self.assertEqual(settings.load(), settings.DEFAULTS)

    def test_run_passes_and_validates_length_and_format(self):
        from unittest import mock
        with mock.patch("screenwriter_studio.web.server.start_crew") as start:
            self._post_json("/api/run", {"idea": "test", "runtime_seconds": 30, "aspect_ratio": "9:16", "max_shots": 4})
            start.assert_called_once_with("test", False, False, 4, "manual", runtime_seconds=30, aspect_ratio="9:16")
        bad = ({"runtime_seconds": 5}, {"runtime_seconds": "30"}, {"runtime_seconds": 721},
               {"aspect_ratio": "4:3"}, {"runtime_seconds": 30, "max_shots": 20}, {"runtime_seconds": 60, "max_shots": 1})
        for extra in bad:
            with mock.patch("screenwriter_studio.web.server.start_crew") as start:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json("/api/run", {"idea": "test", **extra})
                self.assertEqual(caught.exception.code, 400, extra)
                start.assert_not_called()

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
            with mock.patch.object(library, "PRODUCTIONS", root / "productions"), mock.patch.object(desk, "WORKSPACE_ROOT", root), mock.patch.object(library, "WORKSPACE_ROOT", root):
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


def _reference_picker_js() -> str:
    script = (STATIC / "desk.js").read_text(encoding="utf-8")
    return script[script.index("const REFERENCE_ROLES = ["):script.index("function renderReferenceTray(")]


class ReferencePickerTest(unittest.TestCase):
    """Known images start in their role and name; nameless subjects are flagged; choices survive a reload."""

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the desk script")
    def test_roles_names_warnings_and_restore(self):
        driver = (
            "const store = {};\n"
            "const localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };\n"
            "const selectedRefs = new Map();\n"
            "let uploadedRefs = [{ id: '.references/up.png', name: 'up.png', src: '/x' }];\n"
            "const SUBJECT_ROLES = new Set(['character', 'location', 'object']);\n"
            "const REFERENCE_STORE = 'studio.references';\n"
            "const deskFolder = 'productions/water';\n"
            "const productionSlug = (folder) => folder.split('/').pop();\n"
            "let libraryProjects = [{ slug: 'water', references: [\n"
            "  { path: 'water/references/character-01.png', mode: 'character', name: 'Lena', scope: 'auto', shots: [], wardrobe: false },\n"
            "  { path: 'water/references/location-01.png', mode: 'location', name: 'the lake', scope: 'shots', shots: [1, 3] }] }];\n"
            "const markReferenceSelection = () => {};\n"
        ) + _reference_picker_js() + (
            "const out = {};\n"
            "out.known = newReference({ role: 'character', subject: 'Lena', wardrobe: false });\n"
            "out.legacy = newReference({ role: 'lock', subject: 'x' }).mode;\n"
            "out.plain = newReference();\n"
            "selectedRefs.set('a.png', newReference({ role: 'location', subject: '' }));\n"
            "selectedRefs.set('b.png', newReference());\n"
            "out.summary = referenceSummary();\n"
            "selectedRefs.clear();\n"
            "restoreReferences();\n"
            "out.restored = [...selectedRefs];\n"
            "saveReferences(); selectedRefs.clear(); uploadedRefs = [];\n"
            "loadReferences();\n"
            "out.reloaded = [[...selectedRefs.keys()], uploadedRefs.map((u) => u.id)];\n"
            "process.stdout.write(JSON.stringify(out));\n"
        )
        result = json.loads(subprocess.run(["node", "-"], input=driver, text=True, capture_output=True, check=True).stdout)
        self.assertEqual((result["known"]["mode"], result["known"]["name"], result["known"]["face"], result["known"]["wardrobe"]),
                         ("character", "Lena", True, False))
        self.assertEqual(result["legacy"], "style")
        self.assertEqual((result["plain"]["mode"], result["plain"]["name"]), ("style", ""))
        self.assertEqual(result["summary"], "1 style · 1 location · 1 to name")
        restored = dict(result["restored"])
        self.assertEqual((restored["water/references/character-01.png"]["mode"],
                          restored["water/references/character-01.png"]["wardrobe"]), ("character", False))
        self.assertEqual((restored["water/references/location-01.png"]["scope"],
                          restored["water/references/location-01.png"]["shotsText"]), ("shots", "1, 3"))
        self.assertEqual(result["reloaded"], [list(restored), [".references/up.png"]])


class ReferenceCardTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the desk script")
    def test_card_preview_matches_what_the_image_model_is_told(self):
        from screenwriter_studio.references import reference_direction
        script = (STATIC / "desk.js").read_text(encoding="utf-8")
        helpers = script[script.index("function referenceDirection("):script.index("const DIRECTION_EXAMPLES")]
        cases = [{"mode": mode} for mode in ("style", "location", "object", "composition", "lock")] + [
            {"mode": "character", "face": True, "wardrobe": True},
            {"mode": "character", "face": True, "wardrobe": False, "instruction": "  Her hair is short now. "},
            {"mode": "location", "name": "the avenue", "instruction": "keep the burning skyscrapers"},
        ]
        driver = helpers + "process.stdout.write(JSON.stringify({ told: CASES.map(referenceDirection), scope: CASES.map(automaticScope) }));"
        driver = "const CASES = " + json.dumps(cases) + ";\n" + driver
        result = json.loads(subprocess.run(["node", "-"], input=driver, text=True, capture_output=True, check=True).stdout)
        expected = [reference_direction({**case, "instruction": case.get("instruction", "").strip()}) for case in cases]
        self.assertEqual(result["told"], expected)
        self.assertEqual(result["scope"][0], "All shots")
        self.assertEqual(result["scope"][-1], "Shots set in the avenue")
        self.assertEqual(result["scope"][5], "Shots with this character")


class LibraryHintsTest(unittest.TestCase):
    def test_library_knows_roles_names_and_earlier_versions(self):
        from screenwriter_studio.web import server

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            film = root / "water"
            for name in ("images/lena.png", "images/lena.repair-1.png", "images/s01.png", "images/s01.take-2.png",
                         "references/location-01.png"):
                (film / name).parent.mkdir(parents=True, exist_ok=True)
                (film / name).write_bytes(b"\x89PNG\r\n\x1a\n")
            images = film / "images"
            (film / "00_development.json").write_text(json.dumps({"characters": [{"name": "Lena", "description": "x"}]}))
            (film / "run_settings.json").write_text(json.dumps({"references": [
                {"id": "location-01", "mode": "location", "name": "the lake", "path": str(film / "references/location-01.png")},
                {"id": "ref-01", "mode": "lock", "name": "old", "path": str(film / "references/location-01.png")}]}))
            (images / "jobs.json").write_text(json.dumps({"jobs": [{"id": "lena", "kind": "character"},
                                                                    {"id": "s01", "kind": "keyframe"}]}))
            (images / "manifest.json").write_text(json.dumps({"images": [
                {"id": "lena", "path": str(images / "lena.repair-1.png"),
                 "history": [{"path": str(images / "lena.png"), "url": "https://host/a.png"}]},
                {"id": "s01", "path": str(images / "s01.take-2.png"),
                 "history": [{"path": str(images / "s01.png"), "url": "https://host/b.png"}]}]}))
            with mock.patch.object(library, "PRODUCTIONS", root):
                projects, _ = server.library_catalog()
        hints = {item["name"]: {k: v for k, v in item.items() if k not in ("path", "name")} for item in projects[0]["images"]}
        self.assertEqual(hints["images/lena.repair-1.png"], {"label": "Lena · character", "role": "character", "subject": "Lena"})
        self.assertTrue(hints["images/lena.png"]["earlier"])
        self.assertEqual(hints["images/s01.take-2.png"], {"label": "Shot 1"})
        self.assertEqual(hints["images/s01.png"], {"label": "Shot 1 · earlier version", "earlier": True})
        self.assertEqual(hints["references/location-01.png"]["label"], "the lake · location reference")
        self.assertEqual(projects[0]["references"], [{"path": "water/references/location-01.png", "mode": "location",
                                                      "name": "the lake", "instruction": "", "scope": "auto", "shots": []}])


class DesignSystemTest(unittest.TestCase):
    """Phase 1 design rules: one type scale, text-safe colours, visible controls, calm motion, one status voice."""

    css = (STATIC / "desk.css").read_text(encoding="utf-8")
    page = (STATIC / "desk.html").read_text(encoding="utf-8")

    def test_type_uses_the_six_step_scale(self):
        sizes = {float(size) for size in re.findall(r"font-size:\s*([0-9.]+)px", self.css)}
        self.assertLessEqual(sizes, {12, 13, 14, 16, 20, 28})

    def test_orange_text_and_badges_use_the_text_safe_shade(self):
        self.assertNotRegex(self.css, r"(?<![-\w])color:\s*var\(--accent\)")
        self.assertNotRegex(self.css, r"background:\s*var\(--accent\);\s*color:\s*#fff")

    def test_form_controls_have_a_visible_border(self):
        for selector in (r"input\.limit", r"\.model-field select", r'\.ref-field select, \.ref-field input\[type="text"\], \.ref-field textarea'):
            rule = re.search(r"(?m)^\s*" + selector + r"\s*\{([^}]*)\}", self.css)
            self.assertIsNotNone(rule, selector)
            self.assertIn("var(--control-line)", rule.group(1))

    def test_nothing_lifts_under_the_pointer(self):
        for match in re.finditer(r"([^{}]+):hover[^{]*\{([^}]*)\}", self.css):
            self.assertNotRegex(match.group(2), r"translateY\(-", match.group(1).strip())

    def test_hidden_always_hides_and_status_text_stays_on_story(self):
        self.assertRegex(self.css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}")
        for part in ("#hud .kicker", "#note", "#plaque"):
            self.assertIn('body:not([data-view="pipeline"]) ' + part, self.css)

    def test_one_region_announces_progress(self):
        live = re.findall(r'id="([^"]+)"[^>]*aria-live="polite"', self.page)
        self.assertEqual(sorted(live), ["library-search-status", "next-step", "toast"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the desk script")
    def test_images_group_by_role_and_filter_by_what_needs_you(self):
        script = (STATIC / "desk.js").read_text(encoding="utf-8")
        status = script[script.index("function imageStatus("):script.index("function applyStillFilter(")]
        groups = script[script.index("const IMAGE_GROUPS = ["):script.index("function ensureGroup(")]
        label = script[script.index("function imageLabel("):script.index("function syncImages(")]
        driver = (
            "const shotLabel = (state, id) => 'Shot ' + Number(id.slice(1));\n" + status + groups + label +
            "process.stdout.write(JSON.stringify({\n"
            "  groups: ['character', 'Location', 'keyframe', 'scene', 'poster', undefined].map(imageGroup),\n"
            "  status: ['review', 'failed', 'done', 'running', 'blocked', 'planned', ''].map(imageStatus),\n"
            "  labels: [imageLabel({}, { id: 's03', kind: 'keyframe' }), imageLabel({}, { id: 'lena', kind: 'character' })]\n"
            "}));\n"
        )
        result = json.loads(subprocess.run(["node", "-"], input=driver, text=True, capture_output=True, check=True).stdout)
        self.assertEqual(result["groups"], ["cast", "cast", "shots", "shots", "other", "other"])
        self.assertEqual(result["status"], ["needs", "needs", "done", "progress", "progress", "progress", "progress"])
        self.assertEqual(result["labels"], ["Shot 3", "lena · character"])


class AppShellTest(unittest.TestCase):
    """Phase 2: a productions rail, steps for the open production, and a drawer for activity and files."""

    page = (STATIC / "desk.html").read_text(encoding="utf-8")
    script = (STATIC / "desk.js").read_text(encoding="utf-8")

    def section(self, element_id):
        start = self.page.index('id="' + element_id + '"')
        return self.page[start:self.page.index("</section>\n</section>", start) if element_id == "drawer" else None]

    def test_steps_are_navigation_not_tabs(self):
        self.assertNotIn('role="tab"', self.page)
        self.assertIn('<nav class="tablist" aria-label="Production">', self.page)
        self.assertIn('id="new-session" data-tab="brief" aria-current="page"', self.page)
        self.assertIn('setAttribute("aria-current", "page")', self.script)

    def test_log_files_and_viewer_live_in_the_drawer(self):
        drawer = self.section("drawer")
        for part in ('id="activity-pane"', 'id="files-pane"', 'id="library"', 'id="open-folder"', 'id="reader-pane"', 'id="clear-log"'):
            self.assertIn(part, drawer)
        rail = self.page[self.page.index('id="rail"'):self.page.index("</aside>")]
        self.assertIn('id="production-list"', rail)
        self.assertNotIn('id="library"', rail)
        self.assertIn('if (name === "activity" || name === "reader") {', self.script)

    def test_spent_estimate_counts_every_version_and_take(self):
        from screenwriter_studio.pricing import image_rate, spent_estimate, video_rate
        spec = {"resolution": "2K"}
        manifest = {"images": [
            {"id": "s01", "model": "nano-banana-2-1", "state": "done", "history": [{"url": "https://host/a.png"}]},
            {"id": "s02", "model": "nano-banana-2-1", "state": "review"},
            {"id": "s03", "model": "nano-banana-2-1", "state": "running"},
            {"id": "s04", "model": "nano-banana-2-1", "state": "done", "dry_run": True}]}
        plan = {"mode": "pro", "sound": True, "model": "kling-3.0/video", "shots": [{"id": "s01", "duration_seconds": 4}]}
        clips = [{"id": "s01", "state": "done", "history": [{"file": "s01_v1.mp4"}]}]
        expected = 3 * image_rate("nano-banana-2-1", "2K") + 2 * 4 * video_rate("kling-3.0/video", "pro", True)
        self.assertEqual(spent_estimate(spec, manifest, plan, clips)["credits"], expected)
        self.assertEqual(spent_estimate(spec, {"images": []})["credits"], 0)

    def test_rail_summary_says_how_far_and_what_waits(self):
        from screenwriter_studio.web import server
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            film = root / "water"
            (film / "images").mkdir(parents=True)
            (film / "videos").mkdir()
            (film / "images/s01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (film / "00_development.json").write_text(json.dumps({"title": "What the Water Takes"}))
            (film / "01_screenplay.json").write_text("{}")
            (film / "images/manifest.json").write_text(json.dumps({"images": [
                {"id": "lena", "state": "done", "path": str(film / "images/missing.png")},
                {"id": "s01", "state": "review", "path": str(film / "images/s01.png")}]}))
            (film / "videos/manifest.json").write_text(json.dumps({"clips": [{"id": "s01", "state": "done"}, {"id": "s02"}]}))
            summary = library._project_summary(film, root)
            view = server.production_view(film)
        self.assertEqual(summary, {"title": "What the Water Takes", "stage": "clips", "needs": 1,
                                   "images": [1, 2], "clips": [1, 2], "thumb": "water/images/s01.png"})
        self.assertEqual((view["title"], view["slug"], view["spent"]), ("What the Water Takes", "water", None))


class WorkflowLayoutTest(unittest.TestCase):
    """Phase 3: approval as one card, one review panel, clips as a strip, references in a dialog."""

    page = (STATIC / "desk.html").read_text(encoding="utf-8")
    script = (STATIC / "desk.js").read_text(encoding="utf-8")

    def between(self, start, end):
        at = self.page.index(start)
        return self.page[at:self.page.index(end, at)]

    def test_approval_controls_live_in_one_card_beside_the_steps(self):
        card = self.between('id="approval-card"', "</section>")
        for part in ('id="proceed"', 'id="abort-draft"', 'id="cost-choices"', 'id="proceed-cost"', 'id="proceed-note"'):
            self.assertIn(part, card)
        self.assertNotIn('id="proceed"', self.between('id="hud"', 'id="pips"'))
        layout = self.between('id="story-layout"', 'id="shot-board"')
        self.assertLess(layout.index('id="track"'), layout.index('id="stage-detail"'))

    def test_review_waits_in_one_panel_and_cards_point_to_it(self):
        panel = self.between('id="review-panel"', "</section>")
        for part in ('id="review-image"', 'id="review-refs"', 'id="review-decision"', 'id="review-prev"', 'id="review-next"'):
            self.assertIn(part, panel)
        self.assertIn('if (image.state === "review") renderReviewStub(frame, image);', self.script)
        self.assertIn('renderInspection(document.getElementById("review-decision"), image, state.running);', self.script)

    def test_reference_dialog_holds_the_browser_and_enter_cannot_start_a_run(self):
        dialog = self.between('<dialog id="reference-dialog"', "</dialog>")
        for part in ('id="reference-current"', 'id="reference-other"', 'id="reference-upload"', 'id="reference-search"'):
            self.assertIn(part, dialog)
        self.assertNotIn('type="submit"', dialog)
        self.assertIn('if (event.key === "Enter") event.preventDefault();', self.script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the desk script")
    def test_clip_times_and_review_queue(self):
        clock = self.script[self.script.index("function clock("):self.script.index("let sequence = [];")]
        queue = self.script[self.script.index("function reviewQueue("):self.script.index("function openStillsReview(")]
        driver = clock + queue + (
            "process.stdout.write(JSON.stringify({ times: [0, 4, 65, 600].map(clock),\n"
            "  queue: reviewQueue({ images: [{ id: 'a', state: 'done' }, { id: 'b', state: 'review' }, { id: 'c', state: 'failed' }] }).map((i) => i.id),\n"
            "  empty: reviewQueue(null).length }));\n")
        result = json.loads(subprocess.run(["node", "-"], input=driver, text=True, capture_output=True, check=True).stdout)
        self.assertEqual(result, {"times": ["0:00", "0:04", "1:05", "10:00"], "queue": ["b"], "empty": 0})

    def test_each_image_lists_the_references_it_should_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref = root / "water/references/character-01.png"
            ref.parent.mkdir(parents=True)
            ref.write_bytes(b"\x89PNG\r\n\x1a\n")
            spec = {"locked_references": [{"id": "character-01", "mode": "character", "name": "Lena", "path": str(ref)}],
                    "jobs": [{"id": "lake", "kind": "location"},
                             {"id": "s01", "kind": "keyframe", "references": ["character-01", "lake", "https://elsewhere/x.png"]}]}
            with mock.patch.object(library, "PRODUCTIONS", root):
                found = library.image_references(spec)
        self.assertEqual(found, {"s01": [
            {"label": "Lena · character", "role": "character", "src": "/library-media?path=water%2Freferences%2Fcharacter-01.png"},
            {"label": "lake · location", "role": "location", "src": "/media?id=lake"}]})


class MarkdownPreviewTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the desk script")
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
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to run the desk script")
    def test_abort_sits_beside_approve_and_posts_keep(self):
        script = (STATIC / "desk.js").read_text(encoding="utf-8")
        css = (STATIC / "desk.css").read_text(encoding="utf-8")
        self.assertNotIn("Keep this image", script)
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
let settingsSchema = null;
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
            with tempfile.TemporaryDirectory() as state, mock.patch.object(desk, "_board", board), mock.patch.object(library, "PRODUCTIONS", root / "productions"), mock.patch.object(desk, "WORKSPACE_ROOT", root), mock.patch.object(library, "WORKSPACE_ROOT", root), mock.patch("screenwriter_studio.web.board.STATUS_PATH", Path(state) / "desk_status.json"), mock.patch("screenwriter_studio.web.server.run_batch") as batch:
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
