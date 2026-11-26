from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deep_stack.ev import analyze
from deep_stack.live import HandTracker, PopupTracker, _state, scan_live


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


class LiveDecisionTests(unittest.TestCase):
    def test_waits_without_hero_timer_even_when_buttons_are_visible(self):
        candidate = frame(own_timer=False)
        state, reason, seconds = _state(candidate, {})
        self.assertIsNone(state)
        self.assertIsNone(seconds)
        self.assertIn("轮到自己", reason)

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
