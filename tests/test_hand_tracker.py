import unittest

from deep_stack.live import HandTracker


_BLIND_SOURCES = {
    "small_blind_seat": "preflop_order_and_wager",
    "big_blind_seat": "blind_label",
}


def view(dealer=None, small=None, big=None, *, spectating=True, street="preflop", board=(), sources=None):
    positions = {
        "dealer_seat": dealer,
        "small_blind_seat": small,
        "big_blind_seat": big,
        "sources": {"dealer_seat": "dealer_marker", **_BLIND_SOURCES} if sources is None else sources,
    }
    return {
        "spectating": spectating,
        "street": street,
        "board": list(board) if board is not None else None,
        "hero_cards": None,
        "positions": positions,
    }


class PreflopHandTests(unittest.TestCase):
    def test_confirms_without_cards_for_both_spectator_and_waiting_player(self):
        for spectating in (True, False):
            with self.subTest(spectating=spectating):
                tracker = HandTracker()
                opening = view("top_center", "right_upper", "right_middle", spectating=spectating)
                self.assertIsNone(tracker.update(opening))
                hand = tracker.update(opening)
                self.assertIsNotNone(hand)
                # Action can replace the blind labels once the dealer is established.
                self.assertEqual(tracker.update(view("top_center", spectating=spectating)), hand)
                self.assertIsNone(tracker.update(view(spectating=spectating)))
                self.assertEqual(tracker.update(view(
                    "top_center", street="flop", board=("Ac", "4d", "2h"), spectating=spectating,
                )), hand)

    def test_needs_repeated_matching_verified_roles_and_an_empty_board(self):
        tracker = HandTracker()
        opening = view("top_center", "right_upper", "right_middle")
        for incomplete in (
            view("top_center", "right_upper"),
            view("top_center", "right_upper", "right_upper"),
            view("top_center", "right_upper", "right_middle", board=None),
            view("top_center", "right_upper", "right_middle", sources={"dealer_seat": "dealer_marker"}),
        ):
            self.assertIsNone(tracker.update(incomplete))
            self.assertIsNone(tracker.update(incomplete))
        self.assertIsNone(tracker.update(opening))
        self.assertIsNone(tracker.update(view("top_center", street=None, board=None)))
        self.assertIsNone(tracker.update(opening))
        self.assertIsNotNone(tracker.update(opening))

    def test_changed_dealer_suppresses_old_attribution_until_anchor_repeats(self):
        tracker = HandTracker()
        first = view("top_center", "right_upper", "right_middle")
        tracker.update(first)
        previous = tracker.update(first)
        self.assertIsNotNone(previous)
        self.assertIsNone(tracker.update(view("right_upper", street=None, board=None)))
        self.assertIsNone(tracker.update(view(street="flop", board=("Ac", "4d", "2h"))))
        self.assertIsNone(tracker.update(view("right_upper")))
        second = view("right_upper", "right_middle", "left_upper")
        self.assertIsNone(tracker.update(second))
        current = tracker.update(second)
        self.assertIsNotNone(current)
        self.assertNotEqual(current, previous)
        self.assertEqual(tracker.update(view("right_upper")), current)

    def test_moved_dealer_and_unreadable_street_never_reuse_previous_hand(self):
        tracker = HandTracker()
        first = view("top_center", "right_upper", "right_middle")
        tracker.update(first)
        previous = tracker.update(first)
        self.assertIsNotNone(previous)
        second = view("right_upper", "right_middle", "left_upper")
        self.assertIsNone(tracker.update(second))
        self.assertIsNone(tracker.update(view(street=None, board=None)))
        self.assertIsNone(tracker.update(view(street="flop", board=("Ac", "4d", "2h"))))
        self.assertIsNone(tracker.update(second))
        current = tracker.update(second)
        self.assertIsNotNone(current)
        self.assertNotEqual(current, previous)

    def test_changed_dealer_with_same_visible_postflop_cards_cannot_start_hand(self):
        tracker = HandTracker()
        public_cards = ("Ac", "4d", "2h")
        previous = tracker.update(view("top_center", street="flop", board=public_cards))
        self.assertIsNotNone(previous)
        conflicting = view("right_upper", street="flop", board=public_cards)
        self.assertIsNone(tracker.update(conflicting))
        self.assertIsNone(tracker.update(conflicting))
        self.assertIsNone(tracker.update(view(street="flop", board=public_cards)))
        self.assertEqual(tracker.update(view("top_center", street="flop", board=public_cards)), previous)

    def test_heads_up_allows_dealer_to_be_small_blind(self):
        tracker = HandTracker()
        opening = view("hero", "hero", "right_upper")
        self.assertIsNone(tracker.update(opening))
        self.assertIsNotNone(tracker.update(opening))
