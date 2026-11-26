import unittest

from deep_stack.layout import SEAT_IDS
from deep_stack.table_positions import read_table_positions


def field(*texts):
    return {"observations": [{"text": word, "confidence": 0.95} for word in texts]}


def table(occupied, wagers, *, order=SEAT_IDS):
    return [
        {"seat": seat, "name": seat if seat in occupied else None,
         "stack": 100 if seat in occupied else None, "wager": wagers.get(seat), "action": None}
        for seat in order
    ], set(order) - set(occupied)


class TablePositionTests(unittest.TestCase):
    def test_reads_stakes_marker_and_explicit_labels_after_preflop(self):
        rows, vacant = table({"top_left", "right_upper", "left_lower"}, {})
        result = read_table_positions({
            "table_stakes": field("５／１０（５）"),
            "top_left.dealer_left": field("D"),
            "right_upper.blind_label": field("小盲"),
            "left_lower.blind_label": field("ＢＢ"),
        }, rows, vacant, "turn")
        self.assertEqual(result, {
            "dealer_seat": "top_left", "small_blind_seat": "right_upper", "big_blind_seat": "left_lower",
            "small_blind": 5, "big_blind": 10, "ante": 5, "sources": {
                "dealer_seat": "dealer_marker", "small_blind_seat": "blind_label", "big_blind_seat": "blind_label",
                "small_blind": "table_stakes", "big_blind": "table_stakes", "ante": "table_stakes",
            },
        })

    def test_retains_only_unambiguous_stakes_and_does_not_guess_missing_ante(self):
        for readings, wanted in (
            (("0.5 / 1",), (0.5, 1, None)),
            (("2/4", "(2)"), (2, 4, 2)),
            (("2/4(0)",), (2, 4, 0)),
            (("2/4(2)", "2/4(5)"), (2, 4, None)),
            (("2/4(2)", "5/10"), (None, None, None)),
            (("5/10(5",), (None, None, None)),
        ):
            with self.subTest(readings=readings):
                result = read_table_positions({"table_stakes": field(*readings)}, [], [], "preflop")
                self.assertEqual(tuple(result[key] for key in ("small_blind", "big_blind", "ante")), wanted)

    def test_rejects_conflicting_dealer_and_blind_labels(self):
        rows, vacant = table({"top_left", "right_upper", "left_lower"}, {})
        result = read_table_positions({
            "top_left.dealer_right": field("D"),
            "right_upper.dealer_right": field("Ｄ"),
            "right_upper.blind_label": field("SB", "BB"),
        }, rows, vacant, "preflop")
        self.assertIsNone(result["dealer_seat"])
        self.assertIsNone(result["small_blind_seat"])
        self.assertIsNone(result["big_blind_seat"])

    def test_infers_wrapped_eight_seat_order_from_exact_posted_amounts(self):
        order = ("top_center", *(seat if seat != "hero" else "bottom_center" for seat in SEAT_IDS[2:]))
        rows, vacant = table({"left_upper", "top_center", "bottom_center"},
                             {"top_center": 2, "bottom_center": 4}, order=order)
        result = read_table_positions({
            "table_stakes": field("2/4(2)"), "left_upper.dealer_left": field("D"),
            "bottom_center.blind_label": field("big blind"),
        }, rows, vacant, "preflop")
        self.assertEqual(result["small_blind_seat"], "top_center")
        self.assertEqual(result["big_blind_seat"], "bottom_center")
        self.assertEqual(result["sources"]["small_blind_seat"], "preflop_order_and_wager")
        self.assertEqual(result["sources"]["big_blind_seat"], "blind_label")

    def test_headsup_dealer_posts_small_blind(self):
        rows, vacant = table({"top_left", "right_upper"}, {"top_left": 2, "right_upper": 4})
        result = read_table_positions({"table_stakes": field("2/4"), "top_left.dealer_left": field("D")},
                                      rows, vacant, "preflop")
        self.assertEqual((result["small_blind_seat"], result["big_blind_seat"]), ("top_left", "right_upper"))

    def test_preserves_blind_inference_after_another_seat_acts(self):
        rows, vacant = table({"top_left", "right_upper", "left_lower"},
                             {"top_left": 20, "right_upper": 5, "left_lower": 10})
        next(row for row in rows if row["seat"] == "top_left")["action"] = "raise"
        result = read_table_positions({"table_stakes": field("5/10"), "top_left.dealer_right": field("D")},
                                      rows, vacant, "preflop")
        self.assertEqual((result["small_blind_seat"], result["big_blind_seat"]), ("right_upper", "left_lower"))

    def test_can_read_matching_posted_blinds_after_both_players_fold(self):
        rows, vacant = table({"top_left", "right_upper", "left_lower"}, {"right_upper": 5, "left_lower": 10})
        for row in rows:
            if row["seat"] in {"right_upper", "left_lower"}:
                row["action"] = "fold"
        fields = {"table_stakes": field("5/10(5)"), "top_left.dealer_right": field("D")}
        positions = read_table_positions(fields, rows, vacant, "preflop")
        self.assertEqual((positions["small_blind_seat"], positions["big_blind_seat"]), ("right_upper", "left_lower"))
        conflict = read_table_positions(fields, rows, vacant, "preflop", conflicting_seats={"right_upper"})
        self.assertIsNone(conflict["small_blind_seat"])

    def test_does_not_infer_when_amount_occupancy_action_or_street_is_uncertain(self):
        fields = {"table_stakes": field("5/10(5)"), "top_left.dealer_right": field("D")}
        variants = (
            ("preflop", {"right_upper": 5, "left_lower": 20}, None, None),
            ("preflop", {"right_upper": 5, "left_lower": 10}, "wager", None),
            ("preflop", {"right_upper": 5, "left_lower": 10}, "action", "call"),
            ("preflop", {"right_upper": 5, "left_lower": 10}, "stack", None),
            ("flop", {"right_upper": 5, "left_lower": 10}, None, None),
        )
        for street, wagers, field_name, value in variants:
            with self.subTest(street=street, wagers=wagers, field=field_name):
                rows, vacant = table({"top_left", "right_upper", "left_lower"}, wagers)
                if field_name:
                    next(row for row in rows if row["seat"] == "right_upper")[field_name] = value
                result = read_table_positions(fields, rows, vacant, street)
                self.assertIsNone(result["small_blind_seat"])
                self.assertIsNone(result["big_blind_seat"])

    def test_no_move_to_inferred_role_when_label_conflicts(self):
        rows, vacant = table({"top_left", "right_upper", "left_lower"}, {"right_upper": 5, "left_lower": 10})
        result = read_table_positions({
            "table_stakes": field("5/10"), "top_left.dealer_right": field("D"),
            "left_lower.blind_label": field("小盲"),
        }, rows, vacant, "preflop")
        self.assertEqual(result["small_blind_seat"], "left_lower")
        self.assertIsNone(result["big_blind_seat"])
