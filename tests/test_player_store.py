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

    def test_reconcile_accepts_explicit_vacancy_and_visual_change_without_a_hand(self):
        self.store.bind("A", "right_upper", "1")
        self.store.bind("A", "left_upper", "2")
        self.assertEqual(self.store.reconcile_seats("A", frame(street=None)), [])
        initial = frame(street=None)
        initial["vacant_seats"] = ["right_upper"]
        self.assertEqual(self.store.reconcile_seats("A", initial, changed_seats=["left_upper"]),
                         ["left_upper", "right_upper"])
        self.assertEqual(self.store.reconcile_seats("A", initial, changed_seats=["left_upper"]), [])
        self.assertTrue(all(item["paused"] for item in self.store.bindings("A")))

    def test_weak_ocr_conflict_does_not_pause_or_attribute_the_action(self):
        self.store.bind("A", "right_upper", "1", name="原昵称")
        conflicting = frame(reading(name="不同", action="bet", wager=15), street=None)
        observation = {"text": "不同", "confidence": 0.7}
        conflicting["evidence"] = {"right_upper.label": {"observations": [observation]}}
        self.assertEqual(self.store.reconcile_seats("A", conflicting), [])
        self.assertEqual(self.store.record_frame("A", "hand", conflicting)["added"], 0)
        self.assertFalse(self.store.bindings("A")[0]["paused"])

        observation["confidence"] = 0.95
        self.assertEqual(self.store.reconcile_seats("A", conflicting), ["right_upper"])
        self.assertTrue(self.store.bindings("A")[0]["paused"])

    def test_confirmed_id_moves_from_paused_seat_and_keeps_old_actions(self):
        self.store.bind("A", "right_upper", "123", name="玩家")
        old_hand = frame(reading(name="玩家", action="check"))
        self.store.record_frame("A", "old", old_hand)
        old_seat = frame(street=None)
        old_seat["vacant_seats"] = ["right_upper"]
        self.store.reconcile_seats("A", old_seat)
        self.store.bind("A", "left_upper", "123", name="玩家")
        new_hand = frame(reading("left_upper", name="玩家", action="bet", wager=25))
        self.assertEqual(self.store.record_frame("A", "old", new_hand)["added"], 0)
        self.assertEqual(self.store.record_frame("A", "new", new_hand)["added"], 1)
        self.assertEqual(self.store.bindings("A"), [
            {"seat": "left_upper", "player_id": "123", "name": "玩家", "paused": False},
        ])
        self.assertEqual([(event["seat"], event["action"]) for event in self.store.events("123")],
                         [("left_upper", "bet"), ("right_upper", "check")])

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

    def test_club_profile_is_deduplicated_and_kept_separate_from_observations(self):
        player_id = "00123"
        club = {"hands": 100, "win_rate": 0.25, "pot_entry_rate": 0.42, "showdown_win_rate": 0.56}
        self.store.bind("A", "right_upper", player_id, name="牌桌昵称")
        self.store.record_frame("A", "hand1", frame(reading(action="fold")))
        self.assertIsNone(self.store.stats(player_id)["club_profile"])
        self.assertTrue(self.store.save_profile(player_id, name="俱乐部昵称", stats=club))
        self.assertFalse(self.store.save_profile(player_id, name=" 俱乐部昵称 ", stats=club))
        newer = {**club, "hands": 101}
        self.assertTrue(self.store.save_profile(player_id, name="俱乐部昵称", stats=newer))

        with PlayerStore(self.path) as reopened:
            profiles = reopened.profiles(player_id, limit=2)
            self.assertEqual([item["stats"] for item in profiles], [newer, club])
            self.assertEqual(reopened.profiles(player_id, limit=1), profiles[:1])
            self.assertTrue(profiles[0]["recorded_at"])
            stats = reopened.stats(player_id)
            self.assertEqual(stats["club_profile"], newer)
            self.assertEqual(stats["actions"]["fold"], 1)
            self.assertIsNone(stats["vpip"])
            self.assertIsNone(stats["pfr"])
            self.assertEqual(reopened.bindings("A")[0]["name"], "牌桌昵称")

    def test_club_profile_requires_all_four_well_formed_counts_and_rates(self):
        base = {"hands": 0, "win_rate": 0, "pot_entry_rate": 0, "showdown_win_rate": 0}
        for invalid in ({}, {**base, "hands": False}, {**base, "hands": -1}, {**base, "hands": 2**63},
                        {**base, "pot_entry_rate": "42%"}, {**base, "win_rate": 1.1},
                        {**base, "showdown_win_rate": float("nan")}):
            with self.subTest(stats=invalid), self.assertRaises(ValueError):
                self.store.save_profile("1", name=None, stats=invalid)
        self.assertFalse(self.store.stats("1")["registered"])
        self.assertEqual(self.store.profiles("1"), [])
        self.assertTrue(self.store.save_profile("1", name=None, stats=base))
        self.assertTrue(self.store.stats("1")["registered"])
