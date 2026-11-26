"""Exercise the automatic profile planner with a fake window click."""

import unittest
from unittest.mock import call, patch

from deep_stack.avatar_scan import AvatarScanner
from deep_stack.window_input import WindowInputError


class AvatarScannerTests(unittest.TestCase):
    def setUp(self):
        mock = patch("deep_stack.avatar_scan.click_window_point")
        self.click = mock.start()
        self.addCleanup(mock.stop)
        self.scanner = AvatarScanner()

    def step(self, now, *, frame=None, bindings=(), popup_event=None, **options):
        scene = {
            "spectating": True,
            "popup_open": False,
            "avatar_points": {"top_center": (400, 200)},
            "dismiss_popup_point": None,
        }
        if frame:
            scene.update(frame)
        defaults = {"window_id": 17, "image_size": (1600, 1000), "captured_at": now - 0.1}
        return self.scanner.step(scene, now=now, bindings=bindings, popup_event=popup_event,
                                 **(defaults | options))

    def test_quietly_ignores_a_manually_opened_popup(self):
        event = self.step(10, frame={"popup_open": True, "dismiss_popup_point": (800, 600)},
                          popup_event={"type": "player_bound", "seat": "top_center"})
        self.assertIsNone(event)
        self.click.assert_not_called()

    def test_scan_requires_a_fresh_frame_and_a_verified_spectator_without_hero(self):
        for frame, options in (
            ({}, {"window_id": None}),
            ({"spectating": False}, {}),
            ({"spectating": None}, {}),
            ({"layout_mismatch": True}, {}),
            ({"hero_evidence": True}, {}),
            ({"hero_cards": ["Ad", "Ks"]}, {}),
            ({"seats": [{"seat": "hero"}]}, {}),
            ({}, {"captured_at": 7.0}),
            ({}, {"captured_at": 11.0}),
        ):
            with self.subTest(frame=frame, options=options):
                self.assertIsNone(self.step(10, frame=frame, **options))
        self.click.assert_not_called()

    def test_skips_vacant_and_confirmed_seats_but_can_recheck_a_paused_binding(self):
        points = {"hero": (200, 200), "top_center": (400, 200),
                  "left_upper": (200, 400), "right_upper": (1300, 400)}
        event = self.step(10, frame={"avatar_points": points, "vacant_seats": ["left_upper"]}, bindings=[
            {"seat": "top_center", "player_id": "123", "paused": False},
            {"seat": "right_upper", "player_id": "456", "paused": True},
        ])
        self.assertEqual((event["type"], event["stage"], event["seat"]),
                         ("avatar_scan", "opening", "right_upper"))
        self.click.assert_called_once_with(17, (1600, 1000), (1300, 400))

    def test_waits_for_a_confirmed_same_seat_and_a_verified_close_point(self):
        self.step(10)
        modal = {"popup_open": True, "dismiss_popup_point": (800, 600)}
        self.assertIsNone(self.step(11.0, frame=modal, popup_event={"type": "waiting"}))
        self.assertIsNone(self.step(11.5, frame=modal, popup_event={"type": "player_profile", "seat": None}))
        self.assertIsNone(self.step(12.0, frame={"popup_open": True},
                                    popup_event={"type": "player_bound", "seat": "top_center"}))
        self.click.assert_called_once()
        event = self.step(12.5, frame=modal, popup_event={"type": "player_bound", "seat": "top_center"})
        self.assertEqual((event["stage"], event["seat"]), ("closing", "top_center"))
        self.assertIsNone(self.step(13.0, frame=modal,
                                    popup_event={"type": "player_bound", "seat": "top_center"}))
        self.assertEqual(self.click.call_args_list, [
            call(17, (1600, 1000), (400, 200)),
            call(17, (1600, 1000), (800, 600)),
        ])

    def test_conflicting_popup_stays_open_and_releases_scanner_claim(self):
        self.step(10)
        modal = {"popup_open": True, "dismiss_popup_point": (800, 600)}
        event = self.step(12, frame=modal, popup_event={"type": "player_bound", "seat": "bottom_center"})
        self.assertEqual((event["type"], event["stage"]), ("waiting", "different_seat"))
        self.assertIsNone(self.step(13, frame=modal,
                                    popup_event={"type": "player_bound", "seat": "top_center"}))
        self.click.assert_called_once()

    def test_expired_open_attempt_waits_for_cooldown_then_can_retry(self):
        self.step(10)
        self.assertIsNone(self.step(17.0))
        self.assertEqual(self.step(18.0)["stage"], "open_timeout")
        self.assertIsNone(self.step(24.0))
        self.assertEqual(self.step(25.0)["stage"], "opening")
        self.assertEqual(self.click.call_count, 2)

    def test_unresolved_popup_is_left_for_manual_action_at_timeout(self):
        self.step(10)
        modal = {"popup_open": True, "dismiss_popup_point": (800, 600)}
        result = self.step(18.0, frame=modal, popup_event={"type": "player_profile", "seat": None})
        self.assertEqual(result["stage"], "open_timeout")
        self.assertIsNone(self.step(19.0, frame=modal,
                                    popup_event={"type": "player_bound", "seat": "top_center"}))
        self.click.assert_called_once()

    def test_resize_switch_or_loss_of_spectator_proof_abandons_the_popup(self):
        for options in ({"window_id": 19}, {"image_size": (1300, 1000)}, {"spectating": False}):
            with self.subTest(options=options):
                self.scanner = AvatarScanner()
                self.click.reset_mock()
                self.step(10)
                modal = {"popup_open": True, "dismiss_popup_point": (800, 600)}
                outcome = self.step(12.0, frame=modal | {
                    "spectating": options.get("spectating", True)
                }, popup_event={"type": "player_bound", "seat": "top_center"},
                    **{k: v for k, v in options.items() if k != "spectating"})
                self.assertIsNone(outcome)
                self.click.assert_called_once()

    def test_does_not_start_another_open_until_verified_close(self):
        self.step(10)
        event = self.step(12, frame={"popup_open": True, "dismiss_popup_point": (800, 600)},
                          popup_event={"type": "player_profile", "seat": "top_center"})
        self.assertEqual(event["stage"], "closing")
        next_seat = {"avatar_points": {"bottom_center": (300, 500)}}
        self.assertIsNone(self.step(13.0, frame=next_seat | {"popup_open": True}))
        self.assertIsNone(self.step(13.5, frame=next_seat))
        event = self.step(14.0, frame=next_seat)
        self.assertEqual((event["stage"], event["seat"]), ("opening", "bottom_center"))
        self.assertEqual(len(self.click.call_args_list), 3)

    def test_window_input_error_disables_scanner_and_uses_waiting_event(self):
        self.click.side_effect = WindowInputError("需授权")
        event = self.step(10)
        self.assertEqual((event["type"], event["stage"]), ("waiting", "disabled"))
        self.assertIn("需授权", event["message"])
        self.assertIsNone(self.step(25, frame={"avatar_points": {"left_upper": (100, 200)}}))
        self.click.assert_called_once()
