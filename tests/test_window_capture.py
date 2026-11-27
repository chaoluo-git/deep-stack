"""Window selection and screenshot command tests without reading the desktop."""

from importlib.util import find_spec
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from deep_stack.window_capture import WindowCaptureError, capture_window, find_wpk_window


def window(identifier, owner="Google Chrome", title="WePoker-H5", **values):
    return {
        "kCGWindowOwnerName": owner,
        "kCGWindowName": title,
        "kCGWindowNumber": identifier,
        "kCGWindowIsOnscreen": True,
        "kCGWindowLayer": 0,
        "kCGWindowBounds": {"Width": 1400, "Height": 800},
        **values,
    }


class WindowSelectionTests(unittest.TestCase):
    def find(self, *entries):
        with patch("deep_stack.window_capture.sys.platform", "darwin"), \
             patch("deep_stack.window_capture._onscreen_windows", return_value=list(entries)):
            return find_wpk_window()

    def test_filters_chrome_devtools_unrelated_layers_and_other_apps(self):
        match = self.find(
            window(1, title="DevTools – https://h5.wpk.com"),
            window(2, owner="Terminal"),
            window(3, kCGWindowIsOnscreen=False),
            window(4, kCGWindowLayer=25),
            window(5, title="h5.wpk.com - Google Chrome"),
            window(6, title="Unrelated tab"),
            window(7, kCGWindowBounds={"Width": 0, "Height": 800}),
        )
        self.assertEqual(match, 5)

    def test_rejects_multiple_matching_windows(self):
        with self.assertRaisesRegex(WindowCaptureError, "多个 WePoker 窗口"):
            self.find(window(1), window(2))

    def test_extension_collector_does_not_hide_or_duplicate_a_game_window(self):
        collector = window(1, title="WPK 帧采集")
        suffixed = window(2, title="WPK 帧采集 - Google Chrome")
        self.assertIsNone(self.find(collector, suffixed))
        self.assertEqual(self.find(collector, suffixed, window(3)), 3)
        with self.assertRaisesRegex(WindowCaptureError, "多个 WePoker 窗口"):
            self.find(collector, window(3), window(4, title="WPK 牌桌"))

    def test_missing_title_does_not_select_arbitrary_chrome_window(self):
        self.assertIsNone(self.find(window(1, title=None)))
        self.assertEqual(self.find(window(2, owner="WePoker", title=None)), 2)

    def test_unavailable_macos_metadata_leaves_window_unspecified(self):
        with patch("deep_stack.window_capture.sys.platform", "darwin"), \
             patch("deep_stack.window_capture._onscreen_windows", side_effect=OSError):
            self.assertIsNone(find_wpk_window())

    def test_windows_keeps_default_desktop_capture(self):
        with patch("deep_stack.window_capture.sys.platform", "win32"), \
             patch("deep_stack.window_capture._onscreen_windows") as list_windows:
            self.assertIsNone(find_wpk_window())
            list_windows.assert_not_called()


@unittest.skipUnless(find_spec("PIL"), "window capture fixtures require Pillow")
class WindowCaptureTests(unittest.TestCase):
    def test_macos_capture_uses_explicit_id_and_deletes_temporary_file(self):
        from PIL import Image

        captured = []

        def fake_capture(command, **kwargs):
            self.assertEqual(command[:-1], ["/usr/sbin/screencapture", "-l", "42", "-o", "-a", "-x"])
            self.assertTrue(kwargs["check"])
            self.assertTrue(kwargs["capture_output"])
            self.assertGreater(kwargs["timeout"], 0)
            path = Path(command[-1])
            captured.append(path)
            Image.new("RGB", (20, 10), "green").save(path)

        with patch("deep_stack.window_capture.sys.platform", "darwin"), \
             patch("deep_stack.window_capture.subprocess.run", side_effect=fake_capture):
            result = capture_window(42)

        self.assertEqual(result.size, (20, 10))
        self.assertEqual(result.getpixel((1, 1)), (0, 128, 0))
        self.assertFalse(captured[0].exists())

    def test_failed_capture_reports_window_permission_hint(self):
        with patch("deep_stack.window_capture.sys.platform", "darwin"), \
             patch("deep_stack.window_capture.subprocess.run", side_effect=subprocess.TimeoutExpired("capture", 5)):
            with self.assertRaisesRegex(WindowCaptureError, "授权录屏"):
                capture_window(42)

    def test_windows_passes_explicit_handle_to_pillow(self):
        from PIL import Image, ImageGrab

        screenshot = Image.new("RGB", (20, 10), "green")
        with patch("deep_stack.window_capture.sys.platform", "win32"), \
             patch.object(ImageGrab, "grab", return_value=screenshot) as grab:
            self.assertIs(capture_window(42), screenshot)
            grab.assert_called_once_with(window=42)
