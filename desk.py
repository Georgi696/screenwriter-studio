"""Game desk: one HTML file, the stdlib HTTP server, and server-sent events.

Gradio rebuilt the whole board, and inlined each still as base64, on a timer.
This process pushes a small JSON snapshot when the revision changes. The page
patches the token, the station states, and the image slots, so motion is not
restarted on every chunk.

Chosen over Gradio, Phaser, Three, and a vendored UI kit because it adds no
dependency and no build. The shape is the one in
https://workwarrior.org/2026/04/22/the-browser-ui-no-npm-required/
(ThreadingHTTPServer, a static page, SSE). The client only touches nodes that
changed, which is the point of https://thryft.dev/ (patch the DOM, do not swap
the document, or CSS animations restart). https://github.com/eumemic/morph is
the same SSE-and-stable-id idea with View Transitions, and it is a runtime
this one board does not need.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from board import (
    Board,
    apply_chunk,
    board_from,
    client_payload,
    load_status,
    save_status,
    scrub,
)
from models import (
    ART_MODEL,
    DEVELOPMENT_MODEL,
    EDIT_MODEL,
    EDITOR_MODEL,
    GPT_IMAGE_MODEL,
    IMAGE_MODEL_BY_KIND,
    NANO_BANANA_MODEL,
    SEEDREAM_MODEL,
    WRITER_MODEL,
)
from studio import REPO_ROOT, ScreenwriterStudio

PAGE = Path(__file__).resolve().parent / "desk.html"
PRODUCTIONS = REPO_ROOT / "productions"
DOCUMENT_SUFFIXES = {".json", ".md", ".fountain", ".txt"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DEFAULT_IDEA = "A 30-second vertical film about someone who keeps a seat on the last bus."
_MAX_BODY = 100_000
_MAX_TEXT = 200_000
_MAX_IMAGE = 8_000_000

_lock = threading.Lock()
_board = Board()
_run_serial = 0
_future: asyncio.Future | None = None
_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()


class Hub:
    """Wakes SSE clients when the board revision changes. No HTML is built here."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self.revision = 0

    def publish(self, revision: int) -> None:
        with self._cond:
            self.revision = revision
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        with self._cond:
            if self.revision == seen:
                self._cond.wait(timeout=timeout)
            return self.revision


hub = Hub()


def _restore() -> None:
    global _board
    data = load_status()
    _board = board_from(data) if data else Board()
    hub.revision = _board.revision


_restore()


def _secret() -> str:
    return os.environ.get("KIE_API_KEY", "")


def _image_agents() -> list[tuple[str, str]]:
    grouped: dict[str, list[str]] = {}
    for kind, model in IMAGE_MODEL_BY_KIND.items():
        grouped.setdefault(model, []).append(kind)
    order = [NANO_BANANA_MODEL, GPT_IMAGE_MODEL, SEEDREAM_MODEL, EDIT_MODEL]
    for model in grouped:
        if model not in order:
            order.append(model)
    return [(model, ", ".join(grouped[model])) for model in order if model in grouped]


def _replace(board: Board) -> None:
    """Caller holds _lock. Persists, then wakes the page."""
    global _board
    _board = board
    save_status(board, _secret())
    hub.publish(board.revision)


def public_view() -> dict:
    with _lock:
        payload = client_payload(_board, _secret())
        images = []
        for image, raw in zip(payload["images"], _board.images):
            item = dict(image)
            path = str(raw.get("path") or "")
            if raw.get("state") == "done" and _safe_image(path, _board.folder) is not None:
                item["src"] = "/media?id=" + urllib.parse.quote(str(raw.get("id") or ""), safe="")
            else:
                item["src"] = ""
            images.append(item)
        payload["images"] = images
        payload["crew"] = [
            {"id": "development", "name": "Development", "model": DEVELOPMENT_MODEL},
            {"id": "writer", "name": "Screenwriter", "model": WRITER_MODEL},
            {"id": "editor", "name": "Script editor", "model": EDITOR_MODEL},
            {"id": "art", "name": "Art director", "model": ART_MODEL},
        ]
        payload["image_agents"] = [
            {"model": model, "kinds": kinds} for model, kinds in _image_agents()
        ]
        return payload


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _production_roots(folder: str) -> list[Path]:
    roots: list[Path] = []
    if PRODUCTIONS.is_dir():
        roots.append(PRODUCTIONS.resolve())
    if folder:
        candidate = Path(folder)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / folder
        if candidate.is_dir():
            roots.append(candidate.resolve())
    return roots


def _safe_image(path: str, folder: str) -> Path | None:
    if not path:
        return None
    file = Path(path)
    try:
        if not file.is_file():
            return None
        resolved = file.resolve()
    except OSError:
        return None
    if resolved.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    if resolved.stat().st_size > _MAX_IMAGE:
        return None
    if any(_under(resolved, root) for root in _production_roots(folder)):
        return resolved
    return None


def library_catalog() -> tuple[list[dict], str]:
    """Documents and images under productions/<slug>/, plus a change signature."""
    root = PRODUCTIONS.resolve() if PRODUCTIONS.is_dir() else PRODUCTIONS
    projects: list[dict] = []
    signature: list[str] = []
    if not root.is_dir():
        return projects, ""
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        documents: list[dict] = []
        images: list[dict] = []
        for file in sorted(folder.rglob("*")):
            if not file.is_file() or file.name.startswith("."):
                continue
            resolved = file.resolve()
            if not _under(resolved, root):
                continue
            suffix = resolved.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                bucket = images
            elif suffix in DOCUMENT_SUFFIXES:
                bucket = documents
            else:
                continue
            relative = resolved.relative_to(root).as_posix()
            stat = resolved.stat()
            bucket.append({"path": relative, "name": resolved.relative_to(folder.resolve()).as_posix()})
            signature.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
        projects.append({"slug": folder.name, "documents": documents, "images": images})
    return projects, "|".join(signature)


def library_file(path: str) -> Path | None:
    raw = (path or "").strip()
    if not raw or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        return None
    if not PRODUCTIONS.is_dir():
        return None
    root = PRODUCTIONS.resolve()
    file = (root / raw).resolve()
    if not file.is_file() or not _under(file, root):
        return None
    if file.suffix.lower() not in DOCUMENT_SUFFIXES | IMAGE_SUFFIXES:
        return None
    return file


def library_text(file: Path) -> str:
    text = file.read_text(encoding="utf-8", errors="replace")
    if file.suffix.lower() == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            pass
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n\n… truncated"
    return scrub(text, _secret())


def _media_for(image_id: str) -> Path | None:
    secret = _secret()
    with _lock:
        folder = _board.folder
        images = [dict(image) for image in _board.images]
    for image in images:
        raw_id = str(image.get("id") or "")
        if raw_id != image_id and scrub(raw_id, secret) != image_id:
            continue
        return _safe_image(str(image.get("path") or ""), folder)
    return None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is not None:
        return _loop
    with _lock:
        if _loop is not None:
            return _loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            _loop_ready.set()
            loop.run_forever()

        threading.Thread(target=_run, name="desk-loop", daemon=True).start()
        _loop_ready.wait(5)
        _loop = loop
        return loop


def _current(serial: int) -> bool:
    with _lock:
        return serial == _run_serial


def _apply(serial: int, chunk: str) -> None:
    with _lock:
        if serial != _run_serial:
            return
        apply_chunk(_board, chunk)
        _board.log.append(chunk.strip())
        _board.running = True
        _replace(_board)


def _finish(serial: int, note: str | None) -> None:
    with _lock:
        if serial != _run_serial:
            return
        if note:
            _board.note = note
            _board.active = None
        _board.running = False
        _replace(_board)


async def _drive(serial: int, idea: str, pages_only: bool, dry_run: bool) -> None:
    studio = ScreenwriterStudio()
    try:
        async for chunk in studio.run(idea, pages_only=pages_only, dry_run=dry_run):
            if not _current(serial):
                return
            _apply(serial, chunk)
    except asyncio.CancelledError:
        _finish(serial, "Run cancelled.")
        return
    except Exception as exc:
        message = str(exc)
        secret = _secret()
        if not secret or secret not in message:
            traceback.print_exc()
        _finish(serial, message)
        return
    _finish(serial, None)


def start_crew(idea: str, pages_only: bool, dry_run: bool) -> None:
    global _future, _run_serial
    loop = _ensure_loop()
    with _lock:
        _run_serial += 1
        serial = _run_serial
        previous = load_status()
        board = Board()
        board.revision = int((previous or {}).get("revision") or _board.revision or 0)
        board.brief = idea
        board.edge = "brief-dev"
        board.note = "The brief is on its way to Development."
        board.running = True
        _replace(board)
    if _future is not None and not _future.done():
        _future.cancel()
    _future = asyncio.run_coroutine_threadsafe(_drive(serial, idea, pages_only, dry_run), loop)


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"


class DeskHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ScreenwriterDesk"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if route in {"/", "/index.html"}:
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if route == "/api/status":
            self._json(200, public_view())
            return
        if route == "/api/events":
            self._events()
            return
        if route == "/api/library":
            projects, signature = library_catalog()
            self._json(200, {"signature": signature, "projects": projects})
            return
        if route == "/api/file":
            self._file(query.get("path", [""])[0])
            return
        if route == "/media":
            self._media(query.get("id", [""])[0])
            return
        if route == "/library-media":
            self._library_media(query.get("path", [""])[0])
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/run":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"error": "expected JSON"})
            return
        if length < 0 or length > _MAX_BODY:
            self._json(413, {"error": "brief is too long"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "expected JSON"})
            return
        if not isinstance(data, dict):
            self._json(400, {"error": "expected JSON"})
            return
        idea = data.get("idea") if isinstance(data.get("idea"), str) else ""
        start_crew(idea.strip() or DEFAULT_IDEA, bool(data.get("pages_only")), bool(data.get("dry_run")))
        self._json(202, {"ok": True})

    def _file(self, path: str) -> None:
        file = library_file(path)
        if file is None:
            self._json(404, {"error": "That file is not in a production folder."})
            return
        label = file.relative_to(PRODUCTIONS.resolve()).as_posix()
        if file.suffix.lower() in IMAGE_SUFFIXES:
            self._json(
                200,
                {
                    "kind": "image",
                    "name": label,
                    "src": "/library-media?path=" + urllib.parse.quote(path, safe=""),
                },
            )
            return
        self._json(200, {"kind": "text", "name": label, "text": library_text(file)})

    def _media(self, image_id: str) -> None:
        file = _media_for(image_id)
        if file is None:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send(200, file.read_bytes(), _content_type(file))

    def _library_media(self, path: str) -> None:
        file = library_file(path)
        if file is None or file.suffix.lower() not in IMAGE_SUFFIXES:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        self._send(200, file.read_bytes(), _content_type(file))

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen = -1
        try:
            while True:
                revision = hub.wait(seen, 12)
                if revision == seen:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                seen = revision
                payload = json.dumps(public_view(), separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class DeskServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def bind(port: int) -> DeskServer:
    """Listen on 0.0.0.0.

    Cursor's preview scans the IPv4 listen table and dials the machine address.
    A 127.0.0.1 socket refuses that dial. An IPv6 socket never shows up in the
    scan, so the preview browser has nothing on 127.0.0.1 and reports connection
    refused. An IPv4 wildcard socket is visible to the scan and accepts the dial.
    """
    return DeskServer(("0.0.0.0", port), DeskHandler)


_TUNNEL_URL = re.compile(r"https://[a-z0-9]+\.lhr\.life")


def public_url_from_tunnel_log(text: str) -> str | None:
    match = _TUNNEL_URL.search(text)
    return match.group(0) if match else None


def _browsers_are_remote() -> bool:
    """True when the person opening the link is not on this computer.

    Cursor's preview and a browser on their laptop both dial their own
    127.0.0.1. Nothing is listening there, so every one of them reports
    connection refused while this process is fine.
    """
    return bool(os.environ.get("CURSOR_AGENT") or os.environ.get("CURSOR_AGENT_SOCKET"))


def _start_public_tunnel(port: int, timeout: float = 20) -> tuple[subprocess.Popen | None, str | None]:
    """Publish the desk through localhost.run. Returns the ssh process and the https URL."""
    if shutil.which("ssh") is None:
        return None, None
    proc = subprocess.Popen(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-R", f"80:127.0.0.1:{port}",
            "nokey@localhost.run",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    buf = ""
    deadline = time.monotonic() + timeout

    def drain() -> None:
        nonlocal buf
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                return
            if not chunk:
                return
            buf += chunk.decode("utf-8", "replace")

    url = None
    while time.monotonic() < deadline and proc.poll() is None:
        drain()
        url = public_url_from_tunnel_log(buf)
        if url:
            break
        time.sleep(0.1)
    if url is None:
        proc.terminate()
        return None, None

    def keep_draining() -> None:
        while proc.poll() is None:
            drain()
            time.sleep(0.2)

    threading.Thread(target=keep_draining, name="desk-tunnel", daemon=True).start()
    return proc, url


def _stop_tunnel(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, 15)
    except OSError:
        proc.terminate()


def _open_browser(url: str) -> None:
    """Open a window on this display. xdg-open hangs on the xfce helper and never shows the page."""
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if chrome and os.environ.get("DISPLAY"):
        subprocess.Popen(
            [chrome, "--new-window", url, "--no-sandbox", "--disable-dev-shm-usage"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    webbrowser.open(url)


def serve(port: int = 7860, open_browser: bool = True) -> None:
    httpd = None
    last_error: OSError | None = None
    for candidate in range(port, port + 10):
        try:
            httpd = bind(candidate)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        raise SystemExit(f"Could not open the desk: {last_error}")
    bound = httpd.server_address[1]
    local = f"http://127.0.0.1:{bound}"
    tunnel: subprocess.Popen | None = None
    public = None
    if _browsers_are_remote():
        print("Publishing a link browsers outside this computer can open...", flush=True)
        tunnel, public = _start_public_tunnel(bound)
    if public:
        print(f"Open this in any browser:\n{public}", flush=True)
        print(f"{local} is only this computer. Other browsers refuse it.", flush=True)
        url = public
    else:
        print(f"Screenwriter desk at {local}", flush=True)
        if _browsers_are_remote():
            print(
                "No public link. A browser on another computer will say "
                f"{local} refused to connect.",
                flush=True,
            )
        url = local
    if open_browser:
        _open_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDesk closed.")
    finally:
        _stop_tunnel(tunnel)
        httpd.server_close()
