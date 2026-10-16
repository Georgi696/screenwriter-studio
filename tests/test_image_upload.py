"""Oversized stills: kept on disk unchanged, shrunk only for the bytes sent to KIE."""

import base64
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from screenwriter_studio.images import generator
from screenwriter_studio.images.upload import fit_image
from screenwriter_studio.videos.plan import build_plan
from tests.test_video import production


def encode(image, fmt="PNG", **options):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **options)
    return buffer.getvalue()


def gradient(mode="RGBA", size=(320, 180)):
    image = Image.new(mode, size)
    image.putdata([(x % 256, y % 256, (x + y) % 256, 255)[:len(mode)] for y in range(size[1]) for x in range(size[0])])
    return image


def noise(size=(320, 180)):
    rng = random.Random(7)
    return Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))


class FitImageTest(unittest.TestCase):
    def test_image_within_the_cap_is_sent_unchanged(self):
        data = encode(gradient())
        self.assertEqual(fit_image(data, len(data)), (data, ".png"))

    def test_opaque_alpha_is_dropped_without_changing_a_pixel(self):
        source = gradient("RGBA")
        data = encode(source, compress_level=0)
        fitted, kind = fit_image(data, len(data) // 2)
        self.assertEqual(kind, ".png")
        self.assertLessEqual(len(fitted), len(data) // 2)
        with Image.open(io.BytesIO(fitted)) as result:
            self.assertEqual(result.mode, "RGB")
            self.assertEqual(result.tobytes(), source.convert("RGB").tobytes())

    def test_transparency_is_flattened_onto_white(self):
        source = gradient("RGBA")
        source.putalpha(0)
        data = encode(source, compress_level=0)
        fitted, _ = fit_image(data, len(data) // 4)
        with Image.open(io.BytesIO(fitted)) as result:
            self.assertGreater(min(result.convert("L").getextrema()), 245)

    def test_photographic_detail_falls_back_to_jpeg_and_then_a_smaller_size(self):
        data = encode(noise((2048, 1152)))
        limit = 600_000
        fitted, kind = fit_image(data, limit)
        self.assertEqual(kind, ".jpg")
        self.assertLessEqual(len(fitted), limit)
        with Image.open(io.BytesIO(fitted)) as result:
            self.assertLess(result.width, 2048)
            self.assertAlmostEqual(result.width / result.height, 2048 / 1152, places=2)

    def test_thumbnail_is_a_small_jpeg_with_the_same_shape(self):
        from screenwriter_studio.images.upload import thumbnail
        data = thumbnail(encode(noise((1376, 768))))
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        with Image.open(io.BytesIO(data)) as result:
            self.assertEqual(max(result.size), 320)
            self.assertAlmostEqual(result.width / result.height, 1376 / 768, places=1)

    def test_unknown_bytes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "PNG, JPEG, or WebP"):
            fit_image(b"not an image" * 10, 5)


class OversizedFileTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_upload_of_a_still_over_the_cap_sends_a_smaller_copy_and_keeps_the_file(self):
        path = self.root / "s01.png"
        data = encode(gradient("RGBA"), compress_level=0)
        path.write_bytes(data)
        limit = len(data) // 2
        with mock.patch.object(generator, "UPLOAD_LIMIT", limit):
            url = generator.reference_data_url(path)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertLessEqual(len(base64.b64decode(url.split(",", 1)[1])), limit)
        self.assertEqual(path.read_bytes(), data)

    def test_video_plan_accepts_a_keyframe_over_eight_megabytes(self):
        folder = production(self.root)
        (folder / "images/s01.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 9_000_000)
        plan = build_plan(folder)
        self.assertEqual(plan["shots"][0]["image"], "images/s01.png")


if __name__ == "__main__":
    unittest.main()
