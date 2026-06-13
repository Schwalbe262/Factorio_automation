import struct
import unittest

from factorio_ai.vanilla_gui import (
    AchievementPolicyError,
    GuiAutomationError,
    _capture_method,
    _virtual_key,
    encode_bgra_bmp,
    is_factorio_game_window_title,
    validate_achievement_safe_args,
)


class VanillaGuiTests(unittest.TestCase):
    def test_allows_window_size_argument(self):
        args = validate_achievement_safe_args(["--window-size", "1600x900"])
        self.assertEqual(args, ["--window-size", "1600x900"])

    def test_allows_inline_window_size_argument(self):
        args = validate_achievement_safe_args(["--window-size=1600x900"])
        self.assertEqual(args, ["--window-size=1600x900"])

    def test_rejects_mod_directory(self):
        with self.assertRaises(AchievementPolicyError):
            validate_achievement_safe_args(["--mod-directory", "runtime/mods"])

    def test_rejects_rcon(self):
        with self.assertRaises(AchievementPolicyError):
            validate_achievement_safe_args(["--rcon-port", "27015"])

    def test_rejects_unknown_custom_argument(self):
        with self.assertRaises(AchievementPolicyError):
            validate_achievement_safe_args(["--some-new-unsafe-flag"])

    def test_rejects_bare_save_argument(self):
        with self.assertRaises(AchievementPolicyError):
            validate_achievement_safe_args(["my-save.zip"])

    def test_factorio_window_title_filter_rejects_browser_and_explorer(self):
        self.assertTrue(is_factorio_game_window_title("Factorio"))
        self.assertTrue(is_factorio_game_window_title("Factorio: Space Age 2.0.76"))
        self.assertFalse(is_factorio_game_window_title("Factorio Prints - Chrome"))
        self.assertFalse(is_factorio_game_window_title("Factorio - File Explorer"))

    def test_capture_method_auto_uses_window_only_when_minimized(self):
        self.assertEqual(_capture_method("auto", minimized=False), "window")
        self.assertEqual(_capture_method("auto", minimized=True), "window")
        self.assertEqual(_capture_method("screen", minimized=True), "screen")
        with self.assertRaises(GuiAutomationError):
            _capture_method("unsafe", minimized=False)

    def test_bmp_encoder_writes_32_bit_bmp_header(self):
        bmp = encode_bgra_bmp(1, 1, b"\x01\x02\x03\x04")
        self.assertEqual(bmp[:2], b"BM")
        file_size = struct.unpack("<I", bmp[2:6])[0]
        pixel_offset = struct.unpack("<I", bmp[10:14])[0]
        width = struct.unpack("<i", bmp[18:22])[0]
        height = struct.unpack("<i", bmp[22:26])[0]
        bit_count = struct.unpack("<H", bmp[28:30])[0]
        self.assertEqual(file_size, len(bmp))
        self.assertEqual(pixel_offset, 54)
        self.assertEqual(width, 1)
        self.assertEqual(height, 1)
        self.assertEqual(bit_count, 32)

    def test_bmp_encoder_rejects_wrong_pixel_length(self):
        with self.assertRaises(ValueError):
            encode_bgra_bmp(2, 1, b"\x00" * 4)

    def test_probe_report_marks_minimized_unverified_by_default(self):
        from factorio_ai.vanilla_gui import VanillaProbeReport

        report = VanillaProbeReport(
            window_found=True,
            title="Factorio",
            minimized=False,
            visible_capture="visible.bmp",
            minimized_capture="minimized.bmp",
            minimized_capture_ok=False,
            background_key_posted=True,
            background_input_verified=False,
            can_run_minimized=False,
            notes=[],
        )
        payload = report.to_dict()
        self.assertFalse(payload["can_run_minimized"])
        self.assertFalse(payload["background_input_verified"])

    def test_virtual_key_mapping_rejects_unsupported_keys(self):
        self.assertEqual(_virtual_key("w"), 0x57)
        self.assertEqual(_virtual_key("escape"), 0x1B)
        with self.assertRaises(GuiAutomationError):
            _virtual_key("f13")


if __name__ == "__main__":
    unittest.main()
