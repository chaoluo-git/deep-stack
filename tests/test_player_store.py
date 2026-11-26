"""Regression tests for identity attribution and observed action counts."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deep_stack.player_store import PlayerStore


def reading(seat="right_upper", *, name=None, action=None, wager=None, stack=None):
    return {"seat": seat, "name": name, "action": action, "wager": wager, "stack": stack}


def frame(*seats, street="preflop"):
    return {"street": street, "seats": list(seats)}


class PlayerStoreTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "players.sqlite3"
        self.store = PlayerStore(self.path)
        self.addCleanup(self.store.close)

    def test_keeps_leading_zeros_and_never_merges_same_nickname(self):
        self.store.bind("A", "right_upper", "00123", name="相同")
        self.store.bind("A", "hero", "123", name="相同")
        self.store.record_frame("A", "hand1", frame(
            reading(action="raise", wager=20), reading("hero", action="fold"),
        ))
        self.assertEqual(self.store.stats("00123")["actions"]["raise"], 1)
        self.assertEqual(self.store.stats("123")["actions"]["fold"], 1)
        self.assertEqual(self.store.events("00123")[0]["seat"], "right_upper")
        for invalid in (123, "000", "１２３", "-123", "abc", ""):
            with self.subTest(id=invalid), self.assertRaisesRegex(ValueError, "正数字符串"):
                self.store.stats(invalid)

    def test_pauses_mismatched_name_and_resumes_only_after_explicit_rebind(self):
        self.store.bind("A", "right_upper", "123", name="旧名")
        self.store.record_frame("A", "hand1", frame(reading(name="旧名", action="call", wager=10)))
        mismatch = frame(reading(name="新名", action="raise", wager=20))
        self.assertEqual(self.store.record_frame("A", "hand1", mismatch)["paused"], ["right_upper"])
        self.assertTrue(self.store.bindings("A")[0]["paused"])
        self.assertEqual(self.store.record_frame("A", "hand1", frame(reading(action="fold")))["added"], 0)
        self.store.bind("A", "right_upper", "123")
        self.assertEqual(self.store.record_frame("A", "hand1", mismatch)["added"], 1)
        self.assertEqual(self.store.bindings("A"), [
            {"seat": "right_upper", "player_id": "123", "name": "新名", "paused": False},
        ])

    def test_seat_changes_do_not_reassign_already_seen_hands(self):
        self.store.bind("A", "right_upper", "1")
        self.store.bind("B", "right_upper", "2")
        screenshot = frame(reading(action="call", wager=10))
        self.store.record_frame("A", "hand1", screenshot)
        self.store.record_frame("B", "hand1", screenshot)
        self.store.bind("A", "right_upper", "2")
        self.assertEqual(self.store.record_frame("A", "hand1", screenshot)["added"], 0)
        self.assertEqual(self.store.record_frame("A", "hand2", screenshot)["added"], 1)
        self.store.unbind("A", "right_upper")
        self.assertEqual(self.store.record_frame("A", "hand3", screenshot)["added"], 0)
        self.assertEqual(self.store.stats("1")["observed_hands"], 1)
        self.assertEqual(self.store.stats("2")["observed_hands"], 2)
        self.assertEqual(self.store.bindings("A"), [])
        self.assertEqual(len(self.store.bindings("B")), 1)

    def test_vacated_seat_stops_recording_before_the_next_name_appears(self):
        self.store.bind("A", "right_upper", "123")
        vacant = frame(reading())
        vacant["vacant_seats"] = ["right_upper"]
        self.assertEqual(self.store.record_frame("A", "hand1", vacant)["paused"], ["right_upper"])
        incoming_action = frame(reading(action="raise", wager=20))
        self.assertEqual(self.store.record_frame("A", "hand2", incoming_action)["added"], 0)
        self.assertEqual(self.store.stats("123")["action_count"], 0)

    def test_deduplicates_and_keeps_more_than_one_raise_per_street(self):
        self.store.bind("A", "right_upper", "123")
        for action, wager, baseline in [
            ("raise", None, (1, 0)), ("raise", 15, (0, 1)), ("raise", 15, (0, 0)),
            ("raise", 40, (1, 0)), ("raise", 15, (0, 0)), ("raise", None, (0, 0)),
            ("call", 60, (1, 0)), ("fold", None, (1, 0)),
        ]:
            result = self.store.record_frame("A", "hand1", frame(reading(action=action, wager=wager)))
            self.assertEqual((result["added"], result["updated"]), baseline)
        self.assertEqual(self.store.record_frame(
            "A", "hand1", frame(reading(action="fold"), street="flop"),
        )["added"], 0)
        with PlayerStore(self.path) as reopened:
            self.assertEqual(reopened.record_frame(
                "A", "hand1", frame(reading(action="raise", wager=40)),
            )["added"], 0)
            stats = reopened.stats("123")
            self.assertEqual(stats["action_count"], 4)
            self.assertEqual(stats["by_street"]["preflop"]["raise"], 2)
            self.assertEqual(stats["observed_wager"], {"count": 3, "min": 15, "max": 60, "mean": 115 / 3})
            self.assertIsNone(stats["vpip"])

    def test_unknown_street_does_not_duplicate_an_ongoing_label(self):
        self.store.bind("A", "right_upper", "123")
        first = self.store.record_frame("A", "hand1", frame(reading(action="bet"), street=None))
        second = self.store.record_frame(
            "A", "hand1", frame(reading(action="bet", wager=15), street="flop"),
        )
        third = self.store.record_frame("A", "hand1", frame(reading(action="check"), street="turn"))
        self.assertEqual((first["added"], second["added"], third["added"]), (1, 0, 1))
        self.assertEqual(self.store.stats("123")["by_street"]["unknown"]["bet"], 1)

    def test_betting_rates_only_use_explicitly_verified_preflop(self):
        self.store.bind("A", "right_upper", "123")
        self.store.record_frame("A", "hand1", frame(reading(action="raise", wager=30)))
        self.store.record_frame("A", "hand2", frame(reading(name="玩家", stack=300)))
        self.store.record_frame("A", "hand3", frame(reading(action="fold")))
        self.assertIsNone(self.store.stats("123")["pfr"])
        with self.assertRaisesRegex(ValueError, "稳定 ID"):
            self.store.confirm_preflop("A", "unknown", "right_upper", vpip=True, pfr=True)
        with self.assertRaisesRegex(ValueError, "自愿入池"):
            self.store.confirm_preflop("A", "hand1", "right_upper", vpip=False, pfr=True)
        self.store.confirm_preflop("A", "hand1", "right_upper", vpip=True, pfr=True)
        self.store.confirm_preflop("A", "hand1", "right_upper", vpip=True, pfr=True)
        self.store.confirm_preflop("A", "hand2", "right_upper", vpip=False, pfr=False)
        stats = self.store.stats("123")
        self.assertEqual((stats["confirmed_preflop_hands"], stats["vpip"], stats["pfr"]), (2, 0.5, 0.5))

    def test_frame_failure_rolls_back_all_actions(self):
        self.store.bind("A", "right_upper", "1")
        self.store.bind("A", "hero", "2")
        bad_frame = frame(reading(action="fold"), reading("hero", action="raise", wager=float("nan")))
        with self.assertRaisesRegex(ValueError, "有限数字"):
            self.store.record_frame("A", "hand1", bad_frame)
        self.assertEqual(self.store.stats("1")["action_count"], 0)
        self.assertEqual(self.store.stats("2")["action_count"], 0)

    def test_duplicate_id_cannot_follow_two_seats_in_the_same_session(self):
        self.store.bind("A", "right_upper", "1")
        with self.assertRaisesRegex(ValueError, "先解除"):
            self.store.bind("A", "hero", "1")
        self.store.unbind("A", "right_upper")
        self.store.bind("A", "hero", "1")
        self.assertEqual(self.store.bindings("A")[0]["seat"], "hero")
