"""Saved references reach KIE as hosted https URLs, never inline data. No network."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screenwriter_studio.images import generator

PNG = b"\x89PNG\r\n\x1a\n" + b"reference"


class ReferenceUploadTest(unittest.TestCase):
    def setUp(self):
        generator._uploads.clear()
        self.addCleanup(generator._uploads.clear)
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ref = self.temp / "ref-01.png"
        self.ref.write_bytes(PNG)
        self.calls = []

        def request(url, api_key, payload=None, query=None):
            self.calls.append((url, payload))
            return {"code": 200, "msg": "ok", "data": {
                "downloadUrl": "https://tempfile.redpandaai.co/kieai/1/" + payload["fileName"]}}

        self.enterContext(patch.object(generator, "_request", side_effect=request))
        self.sent = []

        def generate(api_key, payload, **kwargs):
            self.sent.append(payload)
            return {"task_id": f"t{len(self.sent)}", "url": "https://result.example/still.png"}

        self.enterContext(patch.object(generator, "generate_one", side_effect=generate))
        self.enterContext(patch.object(generator, "download", side_effect=lambda url, dest: dest.write_bytes(PNG)))

    def run_still(self, job_id):
        job = {"id": job_id, "kind": "keyframe", "prompt": "Mara on the platform.", "references": ["ref-01"]}
        return generator.run_job("key", job, "16:9", "1K", self.temp, {}, False, locked={"ref-01": self.ref})

    def test_locked_reference_is_uploaded_once_and_sent_as_https(self):
        first = self.run_still("s01")
        second = self.run_still("s02")

        self.assertEqual(first["state"], "done")
        self.assertEqual(second["state"], "done")
        self.assertEqual(len(self.calls), 1, "the same file is reused inside the upload window")
        url, body = self.calls[0]
        self.assertEqual(url, generator.UPLOAD_URL)
        self.assertTrue(body["base64Data"].startswith("data:image/png;base64,"))
        self.assertTrue(body["fileName"].endswith(".png"))
        for payload in self.sent:
            images = payload["input"]["image_input"]
            self.assertEqual(len(images), 1)
            self.assertTrue(images[0].startswith("https://tempfile.redpandaai.co/"))
        self.assertEqual(first["references"], [str(self.ref)])

    def test_rejected_upload_fails_the_still_without_submitting(self):
        with patch.object(generator, "_request", return_value={"code": 500, "msg": "storage busy"}):
            record = self.run_still("s01")

        self.assertIn("reference ref-01", record["error"])
        self.assertIn("storage busy", record["error"])
        self.assertEqual(self.sent, [])

    def test_expired_upload_is_sent_again(self):
        self.run_still("s01")
        digest = next(iter(generator._uploads))
        url, _ = generator._uploads[digest]
        generator._uploads[digest] = (url, 0.0)
        self.run_still("s02")

        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
