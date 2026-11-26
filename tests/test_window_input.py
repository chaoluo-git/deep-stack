"""Validate window clicks without touching the user's desktop."""

import unittest
from unittest.mock import Mock, call, patch

from deep_stack.window_input import WindowInputError, click_window_point


def window(identifier=17, *, left=100, top=50, width=800, height=600, **extra):
    return {
        "kCGWindowOwnerName": "Google Chrome",
        "kCGWindowName": "WePoker-H5",
        "kCGWindowOwnerPID": 500,
        "kCGWindowNumber": identifier,
        "kCGWindowIsOnscreen": True,
        "kCGWindowLayer": 0,
        "kCGWindowBounds": {"X": left, "Y": top, "Width": width, "Height": height},
        **extra,
    }


class WindowInputTests(unittest.TestCase):
    def setUp(self):
        platform = patch("deep_stack.window_input.sys.platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        listed = patch("deep_stack.window_input._window_list", return_value=[window()])
        self.listed = listed.start()
        self.addCleanup(listed.stop)
        self.preflight = Mock(return_value=True)
        self.button_state = Mock(return_value=False)
        self.create = Mock(side_effect=[123, 456])
        self.set_flags = Mock()
        self.set_field = Mock()
        self.post = Mock()
        self.release = Mock()
        native = patch("deep_stack.window_input._native_apis", return_value=(
            self.preflight, self.button_state, self.create, self.set_flags, self.set_field,
            self.post, self.release,
        ))
        self.native = native.start()
        self.addCleanup(native.stop)

    def test_dispatches_one_unmodified_retina_click_to_the_matched_pid(self):
        click_window_point(17, (1600, 1200), (300, 200))
        generated = [(kind, (point.x, point.y), button)
                     for (_, kind, point, button), _ in self.create.call_args_list]
        self.assertEqual(generated, [(1, (250, 150), 0), (2, (250, 150), 0)])
        self.assertEqual(self.set_flags.call_args_list, [call(123, 0), call(456, 0)])
        self.assertEqual(self.set_field.call_args_list, [call(123, 1, 1), call(456, 1, 1)])
        self.assertEqual(self.post.call_args_list, [call(500, 123), call(500, 456)])
        self.assertEqual(self.release.call_args_list, [call(123), call(456)])
        self.assertEqual(self.listed.call_count, 2)

    def test_handles_a_display_left_of_the_primary_without_losing_point_precision(self):
        self.listed.return_value = [window(left=-800, top=-60)]
        click_window_point(17, (800, 600), (300.25, 200))
        at = self.create.call_args_list[0].args[2]
        self.assertEqual((at.x, at.y), (-499.75, 140))

    def test_rejects_covered_point_even_when_cover_is_another_chrome_window(self):
        for owner in ("Terminal", "Google Chrome"):
            with self.subTest(owner=owner):
                self.listed.return_value = [window(99, kCGWindowOwnerName=owner), window()]
                with self.assertRaisesRegex(WindowInputError, "遮挡"):
                    click_window_point(17, (1600, 1200), (300, 200))
        self.native.assert_not_called()

    def test_ignores_transparent_and_behind_windows(self):
        self.listed.return_value = [
            window(99, kCGWindowOwnerName="Terminal", kCGWindowAlpha=0.0),
            window(17),
            window(98, kCGWindowOwnerName="Terminal"),
        ]
        click_window_point(17, (1600, 1200), (300, 200))
        self.post.assert_called()

    def test_rejects_a_different_tab_or_invalid_owner_pid(self):
        for extra in ({"kCGWindowName": "Other tab"}, {"kCGWindowOwnerPID": 0}):
            with self.subTest(extra=extra):
                self.listed.return_value = [window(**extra)]
                with self.assertRaisesRegex(WindowInputError, "不是可核实的 WePoker"):
                    click_window_point(17, (1600, 1200), (300, 200))
        self.native.assert_not_called()

    def test_rejects_malformed_coordinate_and_obviously_outdated_capture(self):
        for image_size, point in (
            ((1600, 1200), (1600, 5)),
            ((1600, 1200), (float("nan"), 5)),
            ((0, 2), (1, 1)),
            ((1600, 1200), None),
        ):
            with self.subTest(image_size=image_size, point=point):
                with self.assertRaisesRegex(WindowInputError, "坐标"):
                    click_window_point(17, image_size, point)
        with self.assertRaisesRegex(WindowInputError, "尺寸不一致"):
            click_window_point(17, (1600, 1300), (300, 200))
        self.native.assert_not_called()

    def test_revalidates_z_order_and_geometry_after_native_event_setup(self):
        final_frames = (
            [window(99, kCGWindowOwnerName="Terminal"), window(17)],
            [window(17, left=101)],
            [window(17, kCGWindowOwnerPID=501)],
        )
        for final in final_frames:
            with self.subTest(final=final):
                self.listed.side_effect = [[window()], final]
                self.create.side_effect = [123, 456]
                self.release.reset_mock()
                with self.assertRaises(WindowInputError):
                    click_window_point(17, (1600, 1200), (300, 200))
                self.assertEqual(self.release.call_args_list, [call(123), call(456)])
        self.post.assert_not_called()

    def test_requires_native_permission_and_idle_mouse_without_requesting_access(self):
        self.preflight.return_value = False
        with self.assertRaisesRegex(WindowInputError, "辅助功能"):
            click_window_point(17, (1600, 1200), (300, 200))
        self.preflight.return_value = True
        self.button_state.side_effect = [False, True]
        with self.assertRaisesRegex(WindowInputError, "鼠标按键"):
            click_window_point(17, (1600, 1200), (300, 200))
        self.create.assert_not_called()
        self.post.assert_not_called()

    def test_does_not_send_a_button_down_when_the_button_up_could_not_be_created(self):
        self.create.side_effect = [123, None]
        with self.assertRaisesRegex(WindowInputError, "未能创建"):
            click_window_point(17, (1600, 1200), (300, 200))
        self.post.assert_not_called()
        self.release.assert_called_once_with(123)

    def test_other_platform_stays_manual(self):
        with patch("deep_stack.window_input.sys.platform", "linux"):
            with self.assertRaisesRegex(WindowInputError, "仅支持 macOS"):
                click_window_point(17, (1600, 1200), (300, 200))
        self.listed.assert_not_called()
        self.native.assert_not_called()
