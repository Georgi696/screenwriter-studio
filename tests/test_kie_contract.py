"""The real HTTP path (httpx, retries, download) against a local fake of KIE. No internet."""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from screenwriter_studio import kie
from screenwriter_studio.images import generator

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeKie:
    """Scripted answers: each list is consumed per call; the last entry repeats."""

    def __init__(self, create, poll):
        self.create, self.poll = list(create), list(poll)
        self.calls = {"create": 0, "poll": 0, "download": 0}

    @staticmethod
    def take(script):
        return script.pop(0) if len(script) > 1 else script[0]


def serve(fake: FakeKie):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, headers=None, raw=False):
            data = body if raw else json.dumps(body).encode()
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            fake.calls["create"] += 1
            status, body, headers = fake.take(fake.create)
            self.reply(status, body, headers)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/img.png":
                fake.calls["download"] += 1
                self.reply(200, PNG, {"Content-Type": "image/png"}, raw=True)
                return
            assert parse_qs(urlparse(self.path).query)["taskId"] == ["task-1"]
            fake.calls["poll"] += 1
            status, body, headers = fake.take(fake.poll)
            self.reply(status, body, headers)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


CREATED = (200, {"code": 200, "data": {"taskId": "task-1"}}, {})


def finished(base):
    return (200, {"code": 200, "data": {"state": "success", "resultJson": json.dumps({"resultUrls": [base + "/img.png"]})}}, {})


class ContractTest(unittest.TestCase):
    def start(self, create, poll):
        self.fake = FakeKie(create, [])
        self.httpd = serve(self.fake)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.base = base
        # Poll answers may be written as functions of the fake's address (the result URL points back at it).
        self.fake.poll = [step(base) if callable(step) else step for step in poll]
        self.enterContext(patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}))
        self.enterContext(patch.object(kie, "_client", None))
        self.enterContext(patch.object(kie, "RETRY_BASE", 0))
        self.enterContext(patch.object(generator, "CREATE_URL", base + "/createTask"))
        self.enterContext(patch.object(generator, "POLL_URL", base + "/recordInfo"))
        self.enterContext(patch.object(generator, "POLL_INTERVAL", 0))
        self.enterContext(patch.object(generator, "load_api_key", return_value="test-key"))
        self.out = Path(tempfile.mkdtemp())
        self.spec = {"aspect_ratio": "16:9", "resolution": "1K", "jobs": [{"id": "s01", "kind": "keyframe", "prompt": "A hall."}]}

    def test_rate_limit_and_a_poll_blip_still_end_in_a_saved_image(self):
        self.start(
            create=[(429, {"code": 429, "msg": "slow down"}, {"Retry-After": "0"}), CREATED],
            poll=[(502, {"msg": "bad gateway"}, {}), finished],
        )
        manifest = generator.run_batch(self.spec, self.out)
        record = manifest["images"][0]
        self.assertEqual(record["state"], "done", record.get("error"))
        self.assertEqual(self.fake.calls["create"], 2, "the 429 was resent exactly once")
        self.assertEqual(self.fake.calls["poll"], 2)
        self.assertEqual((self.out / "s01.png").read_bytes(), PNG)

    def test_a_server_error_on_submit_is_not_resent(self):
        self.start(create=[(503, {"msg": "unavailable"}, {})], poll=[finished])
        manifest = generator.run_batch(self.spec, self.out)
        self.assertEqual(self.fake.calls["create"], 1)
        record = manifest["images"][0]
        self.assertEqual(record["state"], "submitting", "the outcome is unknown, so the job waits for a person")
        self.assertIn("503", record["error"])

    def test_out_of_credits_stops_the_batch(self):
        self.start(create=[(402, {"code": 402, "msg": "Insufficient credits"}, {})], poll=[finished])
        with self.assertRaises(kie.CreditsExhausted):
            generator.run_batch(self.spec, self.out)
        self.assertEqual(self.fake.calls["create"], 1)

    def test_large_files_stream_to_disk_and_respect_cancel(self):
        self.start(create=[CREATED], poll=[finished])
        target = self.out / "clip.part"
        self.assertEqual(kie.stream_to_file(self.base + "/img.png", target), len(PNG))
        self.assertEqual(target.read_bytes(), PNG)

        def stop(_cancel):
            raise RuntimeError("Generation cancelled.")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            kie.stream_to_file(self.base + "/img.png", target, check=stop)

    def test_a_dead_host_is_reported_as_not_sent(self):
        self.start(create=[CREATED], poll=[finished])
        self.httpd.shutdown()
        self.httpd.server_close()  # closing the listener makes connections refuse, not hang
        manifest = generator.run_batch(self.spec, self.out)
        record = manifest["images"][0]
        self.assertEqual(record["state"], "failed")
        self.assertIn("not sent", record["error"])


if __name__ == "__main__":
    unittest.main()
