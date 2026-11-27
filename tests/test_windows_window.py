"""Test Windows window verification without capturing or clicking a desktop."""

from contextlib import nullcontext
import ctypes
from dataclasses import replace
import unittest
from unittest.mock import Mock, call, patch

from deep_stack import windows_window as windows


class WindowsWindowTests(unittest.TestCase):
    def setUp(self):
        self.capture_token = windows._last_capture.set(None)
        self.addCleanup(windows._last_capture.reset, self.capture_token)
        self.target = windows._Target(17, 50, "WePoker-H5 - Google Chrome", 144, (-800, -60, 0, 540))
        self.native = Mock()
        self.native.foreground.return_value = 17
        self.native.physical_pixels.side_effect = nullcontext
        self.native.current_target.return_value = self.target
        self.native.unobscured.return_value = True
        self.native.input_idle.return_value = True
        self.native.move_cursor.return_value = True
        self.native.cursor_is_at.return_value = True
        self.native.send_mouse.return_value = 2
        loader = patch.object(windows, "_native", return_value=self.native)
        loader.start()
        self.addCleanup(loader.stop)
        grabber = patch("PIL.ImageGrab.grab")
        self.grab = grabber.start()
        self.addCleanup(grabber.stop)
        self.image = Mock(size=(800, 600))
        self.grab.return_value = self.image

    def test_discovers_only_the_current_verified_browser(self):
        self.assertEqual(windows.find_wpk_window(), 17)
        self.native.current_target.return_value = None
        self.assertIsNone(windows.find_wpk_window())
        self.native.foreground.return_value = None
        self.assertIsNone(windows.find_wpk_window())
        self.grab.assert_not_called()

    def test_captures_then_posts_one_click_in_negative_monitor_coordinates(self):
        self.assertIs(windows.capture_window(17), self.image)
        self.grab.assert_called_once_with(window=17)
        windows.click_window_point(17, (800, 600), (300.25, 200))
        self.native.move_cursor.assert_called_once_with(-500, 140)
        self.native.cursor_is_at.assert_called_once_with((-500, 140))
        self.assertEqual(self.native.unobscured.call_args_list, [call(self.target, (-500, 140))] * 2)
        self.native.send_mouse.assert_called_once_with(windows._LEFT_DOWN, windows._LEFT_UP)
        self.image.close.assert_not_called()
        with self.assertRaises(windows.WindowInputError):
            windows.click_window_point(17, (800, 600), (300.25, 200))
        self.assertEqual(self.native.send_mouse.call_count, 1)

    def test_rejects_wrong_size_and_closes_discarded_capture(self):
        self.image.size = (790, 600)
        with self.assertRaises(windows.WindowCaptureError):
            windows.capture_window(17)
        self.image.close.assert_called_once_with()
        with self.assertRaises(windows.WindowInputError):
            windows.click_window_point(17, (800, 600), (300, 200))
        self.native.send_mouse.assert_not_called()

    def test_rejects_window_change_even_if_size_stays_the_same(self):
        for update in ({"client": (-799, -60, 1, 540)}, {"dpi": 96}, {"pid": 51}, {"title": "WPK Demo"}):
            with self.subTest(update=update):
                self.native.current_target.side_effect = [self.target, self.target, replace(self.target, **update)]
                windows.capture_window(17)
                with self.assertRaises(windows.WindowInputError):
                    windows.click_window_point(17, (800, 600), (300, 200))
        self.native.move_cursor.assert_not_called()
        self.native.send_mouse.assert_not_called()

    def test_refuses_click_when_obscured_or_another_input_is_active(self):
        for check in (self.native.unobscured, self.native.input_idle):
            with self.subTest(check=check):
                windows.capture_window(17)
                check.return_value = False
                with self.assertRaises(windows.WindowInputError):
                    windows.click_window_point(17, (800, 600), (300, 200))
                check.return_value = True
        self.native.move_cursor.assert_not_called()
        self.native.send_mouse.assert_not_called()

    def test_rechecks_foreground_after_moving_the_pointer(self):
        windows.capture_window(17)
        self.native.foreground.return_value = 99
        with self.assertRaises(windows.WindowInputError):
            windows.click_window_point(17, (800, 600), (300, 200))
        self.native.move_cursor.assert_called_once()
        self.native.send_mouse.assert_not_called()

    def test_rechecks_cursor_after_user_interaction(self):
        windows.capture_window(17)
        self.native.cursor_is_at.return_value = False
        with self.assertRaises(windows.WindowInputError):
            windows.click_window_point(17, (800, 600), (300, 200))
        self.native.send_mouse.assert_not_called()

    def test_releases_button_if_only_the_down_event_was_inserted(self):
        windows.capture_window(17)
        self.native.send_mouse.side_effect = [1, 1]
        with self.assertRaises(windows.WindowInputError):
            windows.click_window_point(17, (800, 600), (300, 200))
        self.assertEqual(self.native.send_mouse.call_args_list, [
            call(windows._LEFT_DOWN, windows._LEFT_UP), call(windows._LEFT_UP),
        ])


class NativeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.native = object.__new__(windows._Win32)
        self.target = windows._Target(17, 50, "WePoker-H5", 144, (0, 0, 800, 600))
        self.native.point_monitor = Mock(return_value=1)
        self.native.point_window = Mock(return_value=18)
        self.native.ancestor = Mock(return_value=17)
        self.native.pid = Mock(return_value=50)
        self.native.window_above = Mock(return_value=None)
        self.native.is_visible = Mock(return_value=True)
        self.native.is_minimized = Mock(return_value=False)
        self.native.dwm_attribute = Mock(return_value=0)
        self.native.window_rect = Mock()

    def test_checks_monitor_hit_root_and_pid(self):
        self.assertTrue(self.native.unobscured(self.target, (40, 30)))
        for check, foreign in [(self.native.ancestor, 99), (self.native.pid, 51), (self.native.point_monitor, None)]:
            with self.subTest(check=check):
                original = check.return_value
                check.return_value = foreign
                self.assertFalse(self.native.unobscured(self.target, (40, 30)))
                check.return_value = original

    def test_extension_collector_is_rejected_as_a_foreground_browser_target(self):
        self.native.foreground = Mock(return_value=17)
        self.native.is_enabled = Mock(return_value=True)
        self.native.window_class = Mock()
        for title in ("WPK 帧采集", "WPK 帧采集 - Google Chrome", "WPK 帧采集 — Microsoft Edge"):
            with self.subTest(title=title):
                def write_title(_hwnd, output, _capacity):
                    output.value = title
                    return len(title)

                self.native.window_text = Mock(side_effect=write_title)
                self.assertIsNone(self.native.current_target(17))
        self.native.window_class.assert_not_called()

    def test_checks_overlapping_z_order_even_if_the_native_hit_ignores_an_overlay(self):
        self.native.window_above.side_effect = [99, None]

        def write_rect(_hwnd, output):
            output._obj.right, output._obj.bottom = (50, 50)
            return True

        self.native.window_rect.side_effect = write_rect
        self.assertFalse(self.native.unobscured(self.target, (40, 30)))

    def test_rejects_pointer_capture_and_native_menus(self):
        self.native.async_key = Mock(return_value=0)
        self.native.gui_thread_info = Mock()
        for active, captured, flags, idle in ((17, None, 0, True), (99, None, 0, False),
                                                (17, 99, 0, False), (17, None, 4, False)):
            with self.subTest(active=active, captured=captured, flags=flags):
                def fill_thread_info(thread, output):
                    self.assertEqual(thread, 0)
                    self.assertEqual(output._obj.size, ctypes.sizeof(windows._GuiThreadInfo))
                    output._obj.active = active
                    output._obj.capture = captured
                    output._obj.flags = flags
                    return True

                self.native.gui_thread_info.side_effect = fill_thread_info
                self.assertEqual(self.native.input_idle(17), idle)

    def test_restores_thread_dpi_context_on_error(self):
        self.native.set_dpi_context = Mock(side_effect=[123, 456])
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with self.native.physical_pixels():
                raise RuntimeError("failed")
        requested, restored = self.native.set_dpi_context.call_args_list
        self.assertEqual(requested.args[0].value, ctypes.c_void_p(-4).value)
        self.assertEqual(restored, call(123))

    def test_closes_process_handle_when_executable_query_fails(self):
        self.native.open_process = Mock(return_value=100)
        self.native.process_image = Mock(return_value=False)
        self.native.close_handle = Mock()
        self.assertIsNone(self.native.process_executable(10))
        self.native.close_handle.assert_called_once_with(100)

    def test_mouse_input_representation_matches_the_windows_abi(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(windows._Input), expected)


if __name__ == "__main__":
    unittest.main()
