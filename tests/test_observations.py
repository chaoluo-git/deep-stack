import unittest

from deep_stack.observations import build_observation, make_draft


def seat(seat_id, *, name=None, stack=None, action=None, wager=None):
    return {"seat": seat_id, "name": name, "stack": stack, "action": action, "wager": wager}


def frame(*seats, board=None, hero=None, pot=None, street=None):
    return {"hero_cards": hero, "board": board, "pot": pot, "seats": seats, "street": street}


class ObservationTests(unittest.TestCase):
    def test_keeps_frame_provenance_and_deduplicates_same_action(self):
        flop = ["2c", "7h", "9s"]
        observations = build_observation([
            frame(seat("hero", action="bet"), seat("left_upper", action="fold"), board=flop),
            frame(seat("hero"), board=flop),
            frame(seat("hero", action="bet", wager=20), board=flop),
            frame(seat("hero", action="bet", wager=20), seat("left_upper", action="fold"),
                  board=flop + ["As"]),
        ])
        events = observations["events"]
        self.assertEqual(
            [(event["frame"], event["street"], event["action"]) for event in events],
            [(1, "flop", "bet"), (1, "flop", "fold"), (4, "turn", "bet")],
        )
        self.assertEqual((events[0]["wager"], events[0]["wager_frame"]), (20, 3))

    def test_unreadable_board_does_not_assign_previous_street(self):
        observations = build_observation([
            frame(seat("hero", action="check"), board=["2c", "7h", "9s"]),
            frame(seat("hero", action="check")),
        ])
        self.assertEqual([event["street"] for event in observations["events"]], ["flop", None])

    def test_explicit_turn_with_partial_cards_still_tracks_street(self):
        observations = build_observation([frame(seat("hero", action="bet"), street="turn")])
        self.assertEqual(observations["events"][0]["street"], "turn")
        self.assertIsNone(make_draft(observations)["board"])

    def test_flags_mixed_hands_or_out_of_order_frames(self):
        flop = ["2c", "7h", "9s"]
        with self.assertRaisesRegex(ValueError, "公共牌冲突"):
            build_observation([frame(board=flop), frame(board=[])])
        with self.assertRaisesRegex(ValueError, "手牌不同"):
            build_observation([frame(hero=["As", "Ad"]), frame(hero=["Ac", "Ad"])])
        with self.assertRaisesRegex(ValueError, "街次"):
            build_observation([frame(street="turn"), frame(street="flop")])

    def test_draft_reuses_names_and_hole_cards_without_stale_wagers_or_pot(self):
        observations = build_observation([
            frame(seat("right_upper", name="测试席", wager=30), hero=["Ad", "As"], pot=200),
            frame(seat("right_upper", stack=170, action="bet"),
                  seat("left_upper", stack=80, action="fold")),
        ])
        state = make_draft(observations)
        self.assertEqual(state["hero_cards"], ["Ad", "As"])
        self.assertEqual(state["seats"][0]["name"], "测试席")
        self.assertIsNone(state["seats"][0]["wager"])
        self.assertIsNone(state["seats"][0]["in_hand"])
        self.assertIs(state["seats"][1]["in_hand"], False)
        self.assertIsNone(state["pot"])
        self.assertIsNone(state["board"])
        self.assertEqual(state["actions"], [])
