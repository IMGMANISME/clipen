import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from clipen.analysis.image_similarity import fingerprint_png


def _chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _png(color, *, accent=None):
    width = height = 16
    rows = []
    for y in range(height):
        pixels = []
        for x in range(width):
            pixels.append(accent if accent and x < 2 and y < 2 else color)
        rows.append(b"\0" + b"".join(bytes(pixel) for pixel in pixels))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + _chunk(b"IEND", b"")
    )


class ImageSimilarityTests(unittest.TestCase):
    def test_small_local_change_is_similar_but_different_scene_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            red = root / "red.png"
            near_red = root / "near-red.png"
            blue = root / "blue.png"
            red.write_bytes(_png((220, 30, 30)))
            near_red.write_bytes(_png((220, 30, 30), accent=(210, 35, 35)))
            blue.write_bytes(_png((20, 40, 220)))
            original = fingerprint_png(red)
            self.assertTrue(original.is_similar_to(fingerprint_png(near_red)))
            self.assertFalse(original.is_similar_to(fingerprint_png(blue)))

    def test_invalid_png_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.png"
            path.write_bytes(b"not an image")
            with self.assertRaises(ValueError):
                fingerprint_png(path)


if __name__ == "__main__":
    unittest.main()
