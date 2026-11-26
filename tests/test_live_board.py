"""Checks for live public-card events and temporary recognition gaps."""

import unittest

from deep_stack.live_board import BoardLog


def frame(street, board=None, observed=None, *, popup=False):
    return {"street": street, "board": board, "observed_board_cards": observed,
            "hero_cards": ["2d", "4h"], "popup_open": popup}


class BoardLogTests(unittest.TestCase):
    def test_emits_full_public_cards_once_per_street(self):
        log = BoardLog()
        board = ["As", "Td", "7c", "Kh", "9d"]
        cases = (("flop", 3, "翻牌"), ("turn", 4, "转牌"), ("river", 5, "河牌"))
        for street, count, label in cases:
            with self.subTest(street=street):
                picture = frame(street, board[:count])
                event = log.update(picture, "hand-a")
                self.assertEqual((event["type"], event["street"], event["cards"], event["hand_id"]),
                                 ("board", street, board[:count], "hand-a"))
                self.assertEqual((event["known_count"], event["card_count"]), (count, count))
                self.assertTrue(event["complete"])
                self.assertIn(f"{label} A♠ 10♦ 7♣", event["message"])
                self.assertNotIn("2♦", event["message"])
                self.assertNotIn("4♥", event["message"])
                self.assertIsNone(log.update(picture, "hand-a"))

    def test_reports_partial_slots_then_full_read_without_ocr_flicker(self):
        log = BoardLog()
        partial = frame("flop", observed=["As", None, "7c", None, None])
        event = log.update(partial, None)
        self.assertEqual(event["cards"], ["As", None, "7c"])
        self.assertEqual((event["known_count"], event["card_count"]), (2, 3))
        self.assertFalse(event["complete"])
        self.assertIn("A♠ ? 7♣（识别 2/3）", event["message"])
        self.assertIsNone(log.update(frame("flop", observed=["As", None, None]), None))

        full = frame("flop", ["As", "Td", "7c"])
        self.assertTrue(log.update(full, "hand-a")["complete"])
        self.assertIsNone(log.update(partial, None))
        self.assertIsNone(log.update(frame("flop", observed=["Ah", "Td", "7c"]), None))
        self.assertIsNone(log.update(full, "hand-a"))

    def test_does_not_guess_a_street_or_publish_duplicate_cards(self):
        log = BoardLog()
        reading = frame(None, observed=["As", None, "As", "7c", None])
        event = log.update(reading, None)
        self.assertIsNone(event["street"])
        self.assertIsNone(event["card_count"])
        self.assertEqual(event["cards"], [None, None, None, "7c"])
        self.assertIn("街次待确认", event["message"])
        self.assertIsNone(log.update(reading, None))
        self.assertIsNone(log.update(frame(None, observed=[None] * 5), None))

    def test_resets_after_a_known_empty_preflop_or_a_new_hand(self):
        log = BoardLog()
        flop = frame("flop", ["As", "Td", "7c"])
        self.assertIsNotNone(log.update(flop, "hand-a"))
        pending = frame("preflop", observed=[None] * 5)
        self.assertIsNone(log.update(pending, None))
        preflop = frame("preflop", [], [None] * 5)
        event = log.update(preflop, None)
        self.assertEqual(event["cards"], [])
        self.assertIn("等待发牌", event["message"])
        self.assertIsNone(log.update(preflop, "hand-b"))
        self.assertIsNotNone(log.update(flop, "hand-b"))
        self.assertIsNotNone(log.update(flop, "hand-c"))

    def test_changed_reading_needs_repeat_when_the_hand_is_uncertain(self):
        log = BoardLog()
        first = frame("flop", ["As", "Td", "7c"])
        conflict = frame("flop", ["Ad", "Td", "7c"])
        self.assertIsNotNone(log.update(first, "hand-a"))
        self.assertIsNone(log.update(conflict, None))
        self.assertIsNone(log.update(first, "hand-a"))
        self.assertIsNone(log.update(conflict, None))
        self.assertIsNone(log.update(frame(None, popup=True), None))
        self.assertIsNone(log.update(conflict, None))
        event = log.update(conflict, None)
        self.assertEqual(event["cards"], ["Ad", "Td", "7c"])
        self.assertIsNone(event["hand_id"])
