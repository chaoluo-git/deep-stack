"""Action reporting checks for consecutive recognized frames."""

import unittest

from deep_stack.live_actions import ActionLog


def reading(seat, action, wager=None, *, name=None):
    return {"seat": seat, "action": action, "wager": wager, "name": name}


def frame(street, *seats):
    return {"street": street, "seats": list(seats)}


def binding(seat, player_id, *, name=None, paused=False):
    return {"seat": seat, "player_id": player_id, "name": name, "paused": paused}


class ActionLogTests(unittest.TestCase):
    def test_repeated_frames_emit_once_with_the_matching_active_binding(self):
        log = ActionLog()
        observe = frame("preflop", reading("right_upper", "call", 20, name="ＡLICE"))
        bindings = [binding("right_upper", "00123", name="Alice")]
        events = log.update(observe, "hand-a", bindings)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["type"], event["seat"], event["player_id"]), ("action", "right_upper", "00123"))
        self.assertEqual((event["action"], event["wager"], event["street"]), ("call", 20, "preflop"))
        self.assertTrue(event["confirmed_hand"])
        self.assertEqual(log.update(observe, "hand-a", bindings), [])

    def test_reports_a_visible_action_without_claiming_unconfirmed_street_or_archival(self):
        log = ActionLog()
        action = reading("top_center", "raise", 25)
        unknown = frame(None, action)
        event, = log.update(unknown, None, [])
        self.assertIsNone(event["street"])
        self.assertFalse(event["confirmed_hand"])
        self.assertIn("街次待确认", event["message"])
        self.assertEqual(log.update(unknown, None, []), [])
        visible = frame("preflop", action)
        events = log.update(visible, None, [])
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["hand_id"])
        self.assertFalse(events[0]["confirmed_hand"])
        self.assertIn("手牌待确认", events[0]["message"])
        self.assertEqual(log.update(visible, None, []), [])
        self.assertEqual(log.update(visible, "confirmed", []), [])

    def test_same_street_raise_can_change_amount_and_reports_newly_read_amount(self):
        log = ActionLog()
        events = []
        for wager in (None, 35, 35, 75, 35, None):
            events.extend(log.update(frame("flop", reading("left_lower", "raise", wager)), "hand", []))
        self.assertEqual([event["wager"] for event in events], [None, 35, 75])
        self.assertEqual([event["wager_update"] for event in events], [False, True, False])

    def test_fold_and_all_in_persist_visually_across_streets(self):
        log = ActionLog()
        first = reading("top_right", "fold")
        second = reading("left_upper", "all_in", 150)
        self.assertEqual(len(log.update(frame("preflop", first, second), "hand-a", [])), 2)
        self.assertEqual(log.update(frame("flop", first, second), "hand-a", []), [])
        self.assertEqual(log.update(frame("turn", first, second), "hand-a", []), [])
        self.assertEqual(len(log.update(frame("preflop", first, second), "hand-b", [])), 2)

    def test_only_unpaused_nonconflicting_bindings_supply_an_id(self):
        log = ActionLog()
        observed = frame(
            "river",
            reading("left_upper", "check", name="新昵称"),
            reading("left_middle", "bet", 70),
            reading("top_left", "fold"),
            reading("top_right", "call", 70),
        )
        observed["vacant_seats"] = ["top_left"]
        bindings = [
            binding("left_upper", "1", name="旧昵称"),
            binding("left_middle", "2", paused=True),
            binding("top_left", "3"),
            binding("top_right", "4"),
        ]
        events = log.update(observed, "hand", bindings)
        self.assertEqual([(event["seat"], event["player_id"]) for event in events], [
            ("left_upper", None), ("left_middle", None), ("top_right", "4"),
        ])
