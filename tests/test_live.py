from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deep_stack.ev import analyze
from deep_stack.layout import SEAT_IDS
from deep_stack.live import HandTracker, LiveCapture, PopupTracker, _state, scan_live
from deep_stack.live_table import TableLog


_HERO = ["As", "Ks"]
_RIVER = ["Ts", "Js", "Qs", "2c", "7d"]


def seat(seat_id, *, action=None, stack=400, wager=None):
    return {"seat": seat_id, "name": seat_id, "action": action, "stack": stack, "wager": wager}


def readings(*texts):
    return {"observations": [{"text": text, "confidence": 0.99} for text in texts]}


def frame(*, own_timer=True, passive="跟注 50"):
    evidence = {}
    if own_timer:
        evidence["hero.avatar"] = readings("8s")
    if passive is not None:
        evidence["hero_passive_button"] = readings(passive)
    return {
        "hero_cards": _HERO.copy(),
        "board": _RIVER.copy(),
        "street": "river",
        "seats": [seat("hero"), seat("right_upper", action="bet", wager=50)],
        "pot": 150,
        "center_pot": 100,
        "evidence": evidence,
        "cardbacks": [],
    }


def eight_seat_frame(street="flop"):
    order = ("top_center", *("bottom_center" if key == "hero" else key for key in SEAT_IDS[2:]))
    vacancy = "right_upper"
    return {
        **frame(own_timer=False),
        "seats": [seat(key, action="call" if key == "left_lower" else None,
                       stack=None if key == vacancy else 400, wager=5 if key == "left_lower" else None)
                  for key in order],
        "seat_capacity": 8, "seat_layout_confirmed": True, "spectating": True, "hero_cards": None,
        "seat_status": {key: "vacant" if key == vacancy else "occupied" for key in order},
        "vacant_seats": [vacancy], "cardbacks": [key for key in order if key != vacancy],
        "street": street, "board": _RIVER[:{"flop": 3, "turn": 4, "river": 5}[street]],
        "positions": {"dealer_seat": "top_center", "small_blind_seat": "right_middle",
                      "big_blind_seat": "right_lower", "small_blind": 5, "big_blind": 10,
                      "sources": {"dealer_seat": "dealer_marker"}},
    }


class LiveDecisionTests(unittest.TestCase):
    def test_waiting_sitting_out_and_folded_hero_cannot_trigger_advice(self):
        for hero_state, message in (("waiting_for_hand", "等待下一手"), ("sitting_out", "留座"), ("folded", "已弃牌")):
            with self.subTest(hero_state=hero_state):
                candidate = frame()
                candidate["hero_state"] = hero_state
                state, reason, seconds = _state(candidate, {})
                self.assertIsNone(state)
                self.assertIsNone(seconds)
                self.assertIn(message, reason)

    def test_occupied_waiting_opponent_is_excluded_until_confirmed_active(self):
        candidate = frame()
        candidate["seats"].append(seat("left_upper"))
        candidate["inactive_seats"] = ["left_upper"]
        state, reason, _ = _state(candidate, {})
        self.assertIsNone(reason)
        self.assertEqual([row["seat"] for row in state["seats"]], ["hero", "right_upper"])
        candidate["cardbacks"] = ["left_upper"]
        state, reason, _ = _state(candidate, {})
        self.assertIsNone(state)
        self.assertIn("等待状态与本手参与读数冲突", reason)

    def test_waits_without_hero_timer_even_when_buttons_are_visible(self):
        candidate = frame(own_timer=False)
        state, reason, seconds = _state(candidate, {})
        self.assertIsNone(state)
        self.assertIsNone(seconds)
        self.assertIn("轮到自己", reason)

    def test_reports_new_stacks_and_retains_confirmed_blind_seats_only_for_the_same_hand(self):
        candidate = eight_seat_frame()
        table = TableLog()
        first = table.update(candidate, "first")
        self.assertEqual(first["dealt_players"], 7)
        self.assertIn("空位 右上", first["message"])
        self.assertEqual(first["position_by_seat"]["left_middle"], "HJ")
        candidate["seats"][0]["stack"] = 390
        candidate["positions"].update(small_blind_seat=None, big_blind_seat=None)
        second = table.update(candidate, "first")
        self.assertEqual(second["stacks"]["top_center"], 390)
        self.assertEqual(second["small_blind_seat"], "right_middle")
        self.assertEqual(second["message"], "余额 上中 390")
        self.assertIsNone(table.update(candidate, "first"))

        candidate["cardbacks"].remove("left_middle")
        candidate["seat_status"]["left_middle"] = "unknown"
        self.assertEqual(table.update(candidate, "first")["position_by_seat"]["left_middle"], "HJ")
        changed = table.update(candidate, "second")
        self.assertIsNone(changed["small_blind_seat"])
        self.assertNotIn("left_middle", changed["position_by_seat"])

    def test_does_not_repeat_same_table_description_when_hand_id_is_resolved(self):
        table = TableLog()
        candidate = eight_seat_frame()
        self.assertIsNotNone(table.update(candidate, None))
        self.assertIsNone(table.update(candidate, "confirmed"))

    def test_waits_if_another_seat_also_has_a_timer(self):
        candidate = frame()
        candidate["evidence"]["right_upper.status"] = readings("7秒")
        state, reason, seconds = _state(candidate, {})
        self.assertIsNone(state)
        self.assertEqual(seconds, 8)
        self.assertIn("轮到自己", reason)

    def test_auto_check_checkbox_does_not_count_as_a_button(self):
        state, reason, _ = _state(frame(passive="自动让牌"), {})
        self.assertIsNone(state)
        self.assertIn("按钮", reason)

    def test_observed_heads_up_call_can_be_evaluated(self):
        state, reason, seconds = _state(frame(), {})
        self.assertIsNone(reason)
        self.assertEqual(seconds, 8)
        self.assertEqual(state["actions"], [
            {"kind": "fold", "amount": 0}, {"kind": "call", "amount": 50},
        ])
        self.assertTrue(all("wager" not in player for player in state["seats"]))
        result = analyze(state)
        self.assertEqual(result["method"], "exact_river")
        self.assertEqual(result["recommendation"]["kind"], "call")
        self.assertAlmostEqual(result["recommendation"]["ev"], 150)

    def test_displayed_call_and_verified_bets_must_agree(self):
        state, reason, _ = _state(frame(passive="Call 40"), {"pot_includes_wagers": True})
        self.assertIsNone(state)
        self.assertIn("金额与本街投入不一致", reason)

    def test_reconciles_missing_zero_wagers_for_multiway_call(self):
        candidate = frame(passive=None)
        candidate["seats"].append(seat("left_upper"))
        candidate["cardbacks"].append("left_upper")
        state, reason, _ = _state(candidate, {"pot_includes_wagers": True})
        self.assertIsNone(reason)
        self.assertEqual({item["seat"]: item["wager"] for item in state["seats"]}, {
            "hero": 0, "right_upper": 50, "left_upper": 0,
        })
        self.assertEqual(state["actions"][-1], {"kind": "call", "amount": 50})
        self.assertEqual(analyze(state, samples=100)["recommendation"]["kind"], "call")

        candidate["center_pot"] = 95
        state, reason, _ = _state(candidate, {"pot_includes_wagers": True})
        self.assertIsNone(state)
        self.assertTrue(reason)

    def test_stream_suppresses_repeated_decisions_and_allows_a_new_window(self):
        current = frame()
        second = deepcopy(current)
        second["evidence"]["hero.avatar"] = readings("7s")
        no_timer = frame(own_timer=False)
        stream = StringIO()
        with (
            patch("deep_stack.recognition.recognize", side_effect=[current, second, no_timer, current]),
            redirect_stdout(stream),
        ):
            scan_live(
                frames=["frame1", "frame2", "frame3", "frame4"], viewport=None, profile=None,
                model={}, samples=100, interval=1, max_age=100, json_lines=True,
            )
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["recommendation", "waiting", "recommendation"])

    def test_stream_reports_the_board_and_positions_without_duplicate_table_headers(self):
        frames = [eight_seat_frame(street) for street in ("flop", "flop", "turn", "river")]
        stream = StringIO()
        with patch("deep_stack.recognition.recognize", side_effect=frames), redirect_stdout(stream):
            scan_live(frames=range(4), viewport=None, profile=None, model={}, samples=100,
                      interval=1, max_age=100, json_lines=True, show_actions=True, show_board=True)
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([event["cards"] for event in events if event["type"] == "board"],
                         [_RIVER[:3], _RIVER[:4], _RIVER])
        self.assertEqual(sum(event["type"] == "table" for event in events), 1)
        action = next(event for event in events if event["type"] == "action")
        self.assertEqual((action["seat"], action["seat_label"], action["position"]), ("left_lower", "左下", "LJ"))
        self.assertIn("LJ · 左下 跟注", action["message"])

    def test_stream_requires_two_confirmed_frames_before_switching_seat_layout(self):
        eight = eight_seat_frame()
        nine = deepcopy(eight)
        nine["seat_capacity"] = 9
        nine["seats"][:1] = [seat("top_left"), seat("top_right")]
        del nine["seat_status"]["top_center"]
        nine["seat_status"].update(top_left="occupied", top_right="occupied")
        nine["cardbacks"][:1] = ["top_left", "top_right"]
        nine["positions"].update(dealer_seat="top_left", small_blind_seat="top_right",
                                 big_blind_seat="right_middle")
        frames = [eight, nine, eight, nine, nine]
        stream = StringIO()
        with patch("deep_stack.recognition.recognize", side_effect=frames), redirect_stdout(stream):
            scan_live(frames=range(len(frames)), viewport=None, profile=None, model={}, samples=100,
                      interval=1, max_age=100, json_lines=True, show_actions=True)
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([event["seat_capacity"] for event in events if event["type"] == "table"], [8, 9])


class HandTrackerTests(unittest.TestCase):
    def test_observing_another_table_can_track_visible_postflop_actions(self):
        tracker = HandTracker()
        flop = frame()
        flop["street"], flop["board"], flop["hero_cards"] = "flop", _RIVER[:3], None
        hand = tracker.update(flop)
        self.assertIsNotNone(hand)
        turn = frame()
        turn["street"], turn["board"], turn["hero_cards"] = "turn", _RIVER[:4], None
        self.assertEqual(tracker.update(turn), hand)

    def test_keeps_recording_after_hero_cards_become_unreadable(self):
        tracker = HandTracker()
        flop = frame()
        flop["street"], flop["board"] = "flop", _RIVER[:3]
        hand = tracker.update(flop)
        turn = frame()
        turn["street"], turn["board"], turn["hero_cards"] = "turn", _RIVER[:4], None
        self.assertEqual(tracker.update(turn), hand)
        reset = frame()
        reset["street"], reset["board"], reset["hero_cards"] = "preflop", [], None
        self.assertIsNone(tracker.update(reset))

    def test_preserves_same_hand_across_streets_and_rejects_conflicting_board(self):
        tracker = HandTracker()
        first = frame()
        first["street"] = "flop"
        first["board"] = _RIVER[:3]
        hand = tracker.update(first)
        self.assertIsNotNone(hand)
        self.assertEqual(tracker.update(first), hand)

        conflicting = frame()
        conflicting["board"][0] = "Tc"
        self.assertIsNone(tracker.update(conflicting))
        self.assertEqual(tracker.update(frame()), hand)

    def test_new_hand_boundary_requires_repeat_even_when_hero_cards_repeat(self):
        tracker = HandTracker()
        old_hand = tracker.update(frame())
        new = frame()
        new["street"], new["board"] = "preflop", []
        self.assertIsNone(tracker.update(new))
        new_hand = tracker.update(new)
        self.assertIsNotNone(new_hand)
        self.assertNotEqual(new_hand, old_hand)
        self.assertEqual(tracker.update(new), new_hand)

        unreadable = deepcopy(new)
        unreadable["hero_cards"] = None
        self.assertIsNone(tracker.update(unreadable))
        self.assertEqual(tracker.update(new), new_hand)


class PopupTrackerTests(unittest.TestCase):
    def test_keeps_leading_zero_and_binds_after_two_consistent_readings(self):
        from deep_stack.player_store import PlayerStore

        tracker = PopupTracker()
        visible = {"seats": [{"seat": "right_upper", "name": "Study Player"}], "viewport": [0, 0, 1512, 735]}
        popup_frame = {
            **visible,
            "seats": [{"seat": "right_upper", "name": None}],
            "popup_open": True,
            "player_popup": {
                "player_id": "001234", "name": "Study Player",
                "stats": {"hands": 300, "win_rate": 0.2, "pot_entry_rate": 0.42, "showdown_win_rate": 0.5},
            },
        }
        with TemporaryDirectory() as folder, PlayerStore(Path(folder) / "players.sqlite3") as store:
            self.assertIsNone(tracker.update(visible, store, "study", 1))
            self.assertEqual(tracker.update(popup_frame, store, "study", 2)["type"], "waiting")
            self.assertEqual(store.bindings("study"), [])
            event = tracker.update(popup_frame, store, "study", 3)
            self.assertEqual((event["type"], event["seat"], event["player_id"]),
                             ("player_bound", "right_upper", "001234"))
            self.assertEqual(store.stats("001234")["club_profile"]["pot_entry_rate"], 0.42)
            self.assertEqual(tracker.update(popup_frame, store, "study", 4), event)
            self.assertEqual(len(store.profiles("001234")), 1)


class WindowLiveTests(unittest.TestCase):
    def test_windows_avatar_capture_requires_a_verified_foreground_window(self):
        from deep_stack.window_capture import WindowCaptureError

        with (
            patch("deep_stack.live.sys.platform", "win32"),
            patch("deep_stack.windows_window.find_wpk_window", side_effect=[123, None]),
            patch("deep_stack.windows_window.capture_window", return_value="client") as read_window,
            patch("deep_stack.recognition.screen_image") as read_screen,
        ):
            capture = LiveCapture(require_window=True)
            self.assertEqual(capture.read(), ("client", 123))
            with self.assertRaisesRegex(WindowCaptureError, "等待 WePoker-H5"):
                capture.read()
        read_window.assert_called_once_with(123)
        read_screen.assert_not_called()

    def test_mac_prefers_wpk_and_does_not_switch_to_desktop_if_it_disappears(self):
        from deep_stack.window_capture import WindowCaptureError

        capture = LiveCapture()
        capture.prefer_window = True
        with (
            patch("deep_stack.window_capture.find_wpk_window", side_effect=[123, None]),
            patch("deep_stack.window_capture.capture_window", return_value="window") as read_window,
            patch("deep_stack.recognition.screen_image") as read_screen,
        ):
            image, window_id = capture.read()
            self.assertEqual((image, window_id), ("window", 123))
            capture.previous_window = window_id
            with self.assertRaisesRegex(WindowCaptureError, "等待 WePoker-H5"):
                capture.read()
        read_window.assert_called_once_with(123)
        read_screen.assert_not_called()

    def test_temporary_capture_failure_can_recover_in_the_same_session(self):
        visible = frame(own_timer=False)
        visible["viewport"] = [0, 242, 3024, 1470]
        stream = StringIO()
        with (
            patch.object(LiveCapture, "read", side_effect=[OSError("临时失败"), ("window", 123), KeyboardInterrupt]),
            patch("deep_stack.recognition.recognize", return_value=visible) as recognize,
            patch("deep_stack.live.sleep"),
            redirect_stdout(stream),
            self.assertRaises(KeyboardInterrupt),
        ):
            scan_live(
                frames=None, viewport=None, profile=None, model={}, samples=100,
                interval=1, max_age=3, json_lines=True,
            )
        self.assertTrue(recognize.call_args.kwargs["window_capture"])
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([event["type"] for event in events], ["waiting", "viewport", "waiting"])
        self.assertEqual(events[1]["viewport"], [0, 242, 3024, 1470])


class PopupSeatEvidenceTests(unittest.TestCase):
    def test_binds_top_center_using_pinned_avatar_when_popup_hides_seat(self):
        from deep_stack.player_store import PlayerStore

        tracker = PopupTracker()
        visible = {
            "seats": [{"seat": "top_center", "name": "样本玩家"}, {"seat": "left_upper", "name": "其他玩家"}],
            "seat_avatars": {"top_center": (1,), "left_upper": (2,)},
            "viewport": [0, 242, 1512, 1482],
        }
        dialog = {
            "seats": [{"seat": "top_left", "name": None}, {"seat": "left_upper", "name": "其他玩家"}],
            "popup_seat_candidates": [{"seat": "top_center", "name": None}],
            "seat_avatars": {"left_upper": (3,)},
            "viewport": [0, 242, 1512, 1478],
            "popup_open": True,
            "popup_avatar": (1,),
            "player_popup": {"player_id": "001234", "name": "样本烷家"},
        }
        with (
            TemporaryDirectory() as folder,
            PlayerStore(Path(folder) / "test.db") as store,
            patch("deep_stack.player_popup.match_avatar", return_value="top_center") as avatar_match,
        ):
            tracker.update(visible, store, "study", 0)
            tracker.update(visible, store, "study", 1)
            self.assertEqual(tracker.update(dialog, store, "study", 8)["type"], "waiting")
            dialog["player_popup"] = {"player_id": "001234", "name": None}
            event = tracker.update(dialog, store, "study", 10)
            self.assertEqual((event["type"], event["seat"]), ("player_bound", "top_center"))
            self.assertEqual(avatar_match.call_args.args[1]["top_center"], [None, (1,)])
            self.assertEqual(store.bindings("study")[0]["name"], "样本玩家")
            saved_frame = {**visible, "street": "flop"}
            self.assertEqual(store.record_frame("study", "hand", saved_frame)["paused"], [])

    def test_rejects_avatar_that_conflicts_with_an_exact_nickname(self):
        from deep_stack.player_store import PlayerStore

        tracker = PopupTracker()
        dialog = {
            "seats": [{"seat": "left_upper", "name": "样本玩家"}, {"seat": "right_upper", "name": "其他玩家"}],
            "seat_avatars": {"left_upper": (1,), "right_upper": (2,)},
            "popup_open": True,
            "popup_avatar": (2,),
            "player_popup": {"player_id": "001234", "name": "样本玩家"},
        }
        with (
            TemporaryDirectory() as folder,
            PlayerStore(Path(folder) / "test.db") as store,
            patch("deep_stack.player_popup.match_avatar", return_value="right_upper"),
        ):
            self.assertEqual(tracker.update(dialog, store, "study", 1)["type"], "waiting")
            event = tracker.update(dialog, store, "study", 2)
            self.assertEqual((event["type"], event["seat"]), ("player_profile", None))
            self.assertEqual(store.bindings("study"), [])

    def test_one_physical_bottom_seat_uses_unobscured_spectator_role(self):
        tracker = PopupTracker()
        visible = {
            "seats": [{"seat": "bottom_center", "name": "样本玩家"}, {"seat": "left_upper", "name": None}],
            "seat_avatars": {"bottom_center": (1,), "left_upper": (2,)},
        }
        dialog = {
            **visible,
            "seats": [{"seat": "hero", "name": None}, {"seat": "left_upper", "name": None}],
            "popup_seat_candidates": [{"seat": "bottom_center", "name": None}],
            "seat_avatars": {"hero": (1,), "left_upper": (2,)},
            "popup_open": True,
            "popup_avatar": (1,),
            "player_popup": {"player_id": "001234", "name": "样本玩家"},
        }
        with patch("deep_stack.player_popup.match_avatar", return_value="bottom_center") as avatar_match:
            tracker.update(visible, None, "study", 1)
            tracker.update(dialog, None, "study", 2)
            self.assertEqual(tracker.update(dialog, None, "study", 3)["seat"], "bottom_center")
            self.assertEqual(avatar_match.call_args.args[1]["bottom_center"], [(1,), (1,)])
            self.assertNotIn("hero", avatar_match.call_args.args[1])
