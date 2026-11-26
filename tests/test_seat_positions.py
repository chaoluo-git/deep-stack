import unittest

from deep_stack.layout import SEAT_IDS
from deep_stack.seat_positions import read_seat_positions


def observation(occupied, *, dealer, order=SEAT_IDS, street="preflop"):
    return {
        "seats": [
            {"seat": seat, "name": seat if seat in occupied else None,
             "stack": 100 if seat in occupied else None, "action": None, "wager": None}
            for seat in order
        ],
        "seat_status": {seat: "occupied" if seat in occupied else "vacant" for seat in order},
        "cardbacks": list(occupied),
        "seat_layout_confirmed": True,
        "street": street,
        "positions": {"dealer_seat": dealer},
    }


class SeatPositionTests(unittest.TestCase):
    def test_position_names_for_every_supported_hand_size(self):
        sequences = (
            ("BTN/SB", "BB"),
            ("BTN", "SB", "BB"),
            ("BTN", "SB", "BB", "CO"),
            ("BTN", "SB", "BB", "HJ", "CO"),
            ("BTN", "SB", "BB", "UTG", "HJ", "CO"),
            ("BTN", "SB", "BB", "UTG", "LJ", "HJ", "CO"),
            ("BTN", "SB", "BB", "UTG", "UTG+1", "LJ", "HJ", "CO"),
            ("BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"),
        )
        for count, expected in enumerate(sequences, start=2):
            with self.subTest(players=count):
                occupied = SEAT_IDS[:count]
                frame = observation(occupied, dealer=occupied[0])
                result = read_seat_positions(frame)
                self.assertEqual(result["position_status"], "confirmed")
                self.assertEqual(result["position_by_seat"], dict(zip(occupied, expected)))

    def test_eight_seat_spectator_geometry_skips_empty_and_waiting_players(self):
        order = ("top_center", *SEAT_IDS[2:5], "bottom_center", *SEAT_IDS[6:])
        occupied = ("top_center", "right_upper", "right_lower", "bottom_center", "left_lower", "left_upper")
        frame = observation(occupied, dealer="left_lower", order=order, street="flop")
        frame["seat_status"]["right_lower"] = "inactive"
        frame["cardbacks"].remove("right_lower")
        result = read_seat_positions(frame)
        self.assertEqual(result["position_status"], "confirmed")
        self.assertEqual(result["position_by_seat"], {
            "left_lower": "BTN", "left_upper": "SB", "top_center": "BB",
            "right_upper": "HJ", "bottom_center": "CO",
        })
        self.assertEqual(result["seat_statuses"]["right_lower"], "inactive")
        self.assertEqual(result["seat_statuses"]["left_middle"], "vacant")

    def test_fold_remains_in_hand_roster_and_missing_cardback_can_be_inferred(self):
        frame = observation(SEAT_IDS, dealer=SEAT_IDS[0], street="turn")
        frame["cardbacks"].remove("right_middle")
        next(row for row in frame["seats"] if row["seat"] == "right_middle")["action"] = "fold"
        self.assertEqual(read_seat_positions(frame)["position_status"], "confirmed")
        frame["cardbacks"].remove("left_middle")
        result = read_seat_positions(frame)
        self.assertEqual(result["position_status"], "inferred")
        self.assertEqual(result["position_by_seat"]["right_middle"], "UTG")
        self.assertEqual(result["position_by_seat"]["left_middle"], "HJ")

    def test_unreadable_physical_seat_keeps_only_explicit_roles(self):
        frame = observation(SEAT_IDS[:5], dealer="top_left")
        frame["seat_status"]["left_middle"] = "unknown"
        frame["positions"].update(small_blind_seat="top_right", big_blind_seat="right_upper")
        result = read_seat_positions(frame)
        self.assertEqual(result["position_status"], "partial")
        self.assertEqual(result["position_by_seat"], {"top_left": "BTN", "top_right": "SB", "right_upper": "BB"})
        self.assertEqual(result["seat_statuses"]["left_middle"], "unknown")

    def test_ante_and_occupied_seats_do_not_confirm_a_hand(self):
        frame = observation(SEAT_IDS, dealer="top_left")
        frame["positions"]["ante"] = 5
        frame["cardbacks"] = []
        for row in frame["seats"]:
            row["wager"] = 5
        result = read_seat_positions(frame)
        self.assertEqual(result["position_status"], "partial")
        self.assertEqual(result["position_by_seat"], {"top_left": "BTN"})

    def test_conflicting_blinds_or_occupancy_suppress_the_mapping(self):
        frame = observation(SEAT_IDS[:5], dealer="top_left")
        frame["positions"]["small_blind_seat"] = "right_upper"
        result = read_seat_positions(frame)
        self.assertEqual(result["position_status"], "conflict")
        self.assertEqual(result["position_by_seat"], {})
        frame["positions"].pop("small_blind_seat")
        frame["seat_status"]["top_right"] = "vacant"
        result = read_seat_positions(frame)
        self.assertEqual(result["position_status"], "conflict")
        self.assertEqual(result["seat_statuses"]["top_right"], "conflict")

    def test_legacy_seat_fields_and_missing_rows_with_explicit_vacancy(self):
        frame = observation(SEAT_IDS[:4], dealer="top_left")
        frame.pop("seat_status")
        frame["vacant_seats"] = [*SEAT_IDS[5:]]
        frame["inactive_seats"] = [SEAT_IDS[4]]
        frame["seats"] = frame["seats"][:4]
        self.assertEqual(read_seat_positions(frame)["position_by_seat"], {
            "top_left": "BTN", "top_right": "SB", "right_upper": "BB", "right_middle": "CO",
        })
        frame["seat_layout_confirmed"] = False
        self.assertEqual(read_seat_positions(frame)["position_by_seat"], {})
