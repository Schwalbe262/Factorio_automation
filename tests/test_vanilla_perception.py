import tempfile
import unittest
from pathlib import Path

from factorio_ai.vanilla_gui import encode_bgra_bmp
from factorio_ai.vanilla_perception import classify_bmp_file


class VanillaPerceptionTests(unittest.TestCase):
    def test_classifies_settings_menu_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.bmp"
            path.write_bytes(_bmp(640, 480, _settings_pixel))
            state = classify_bmp_file(path)
        self.assertEqual(state.kind, "settings_menu")
        self.assertGreater(state.confidence, 0.8)

    def test_classifies_gameplay_terrain_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "gameplay.bmp"
            path.write_bytes(_bmp(640, 480, _gameplay_pixel))
            state = classify_bmp_file(path)
        self.assertEqual(state.kind, "gameplay")
        self.assertGreater(state.features["terrain_center_ratio"], 0.34)

    def test_classifies_minimized_titlebar_capture_as_not_gameplay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "minimized.bmp"
            path.write_bytes(_bmp(160, 28, lambda _x, _y: (32, 32, 32)))
            state = classify_bmp_file(path)
        self.assertEqual(state.kind, "not_gameplay_capture")

    def test_classifies_bright_occlusion_as_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "occluded.bmp"
            path.write_bytes(_bmp(640, 480, lambda _x, _y: (245, 245, 245)))
            state = classify_bmp_file(path)
        self.assertEqual(state.kind, "unknown_or_occluded")


def _bmp(width, height, painter):
    rows = []
    for y in range(height - 1, -1, -1):
        for x in range(width):
            b, g, r = painter(x, y)
            rows.append(bytes((b, g, r, 255)))
    return encode_bgra_bmp(width, height, b"".join(rows))


def _settings_pixel(x, y):
    if x < 150:
        if 24 <= x <= 130 and 30 <= y <= 58:
            return (240, 145, 20)
        return (42, 46, 53)
    if 170 <= x <= 610 and 90 <= y <= 122:
        return (37, 41, 48)
    return (18, 23, 30)


def _gameplay_pixel(x, y):
    if y > 410:
        return (30, 32, 36)
    if x > 560 and y < 140:
        return (55, 60, 45)
    if (x // 22 + y // 18) % 3 == 0:
        return (76, 118, 148)
    if (x // 31 + y // 29) % 5 == 0:
        return (64, 112, 68)
    return (78, 132, 166)


if __name__ == "__main__":
    unittest.main()
