import unittest

from deep_stack.ev import analyze


class ExpectedValueTests(unittest.TestCase):
    def test_river_counts_all_unblocked_combinations_and_split_pot(self):
        state = {
            "hero_cards": ["2c", "3d"],
            "board": ["As", "Ks", "Qs", "Js", "Ts"],
            "pot": 100,
            "seats": [
                {"seat": "hero", "stack": 200, "in_hand": True},
                {"seat": "villain", "stack": 200, "in_hand": True},
            ],
            "actions": [{"kind": "fold", "amount": 0}, {"kind": "call", "amount": 20}],
        }
        result = analyze(state)
        self.assertEqual(result["method"], "exact_river")
        self.assertEqual(result["samples"], 990)
        self.assertAlmostEqual(result["actions"][1]["ev"], 40)
        self.assertEqual(result["recommendation"]["kind"], "call")
        self.assertEqual(result["actions"][1]["std_error"], 0)

    def test_river_respects_weights_and_known_card_blockers(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 100,
            "seats": [
                {"seat": "hero", "stack": 200, "in_hand": True},
                {
                    "seat": "villain",
                    "stack": 200,
                    "in_hand": True,
                    "range": [
                        {"cards": ["As", "Ad"], "weight": 1},
                        {"cards": ["Qs", "Qd"], "weight": 3},
                        {"cards": ["Ks", "Qh"], "weight": 1000},
                    ],
                },
            ],
            "actions": [{"kind": "fold", "amount": 0}, {"kind": "call", "amount": 25}],
        }
        result = analyze(state)
        self.assertEqual(result["samples"], 2)
        self.assertAlmostEqual(result["recommendation"]["ev"], 68.75)
        self.assertEqual(result["recommendation"]["ci95"], [68.75, 68.75])

    def test_bet_values_calls_and_folds_and_rejects_missing_response(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 100,
            "seats": [
                {"seat": "hero", "stack": 200, "in_hand": True, "wager": 0},
                {"seat": "opponent", "stack": 200, "in_hand": True, "wager": 0, "range": [{"cards": ["As", "Ad"]}]},
            ],
            "actions": [{"kind": "check", "amount": 0}, {"kind": "bet", "amount": 30, "responses": {"opponent": 0.5}}],
        }
        result = analyze(state)
        self.assertEqual(result["recommendation"]["kind"], "bet")
        self.assertEqual(result["recommendation"]["ev"], 35)
        del state["actions"][1]["responses"]
        with self.assertRaisesRegex(ValueError, "缺少座位"):
            analyze(state)

    def test_hero_short_call_cannot_win_the_unmatched_wager(self):
        state = {
            "hero_cards": ["As", "Ks"],
            "board": ["Qs", "Js", "Ts", "2c", "3d"],
            "pot": 450,
            "seats": [
                {"seat": "hero", "stack": 50, "in_hand": True, "wager": 0, "committed": 100},
                {"seat": "villain", "stack": 200, "in_hand": True, "wager": 100, "committed": 200},
                {"seat": "folded", "stack": 50, "in_hand": False, "wager": 0, "committed": 150},
            ],
            "actions": [{"kind": "fold", "amount": 0}, {"kind": "call", "amount": 50}],
        }
        self.assertEqual(analyze(state)["recommendation"]["ev"], 400)
        for seat in state["seats"]:
            del seat["committed"]
        with self.assertRaisesRegex(ValueError, "超额筹码"):
            analyze(state)

    def test_three_way_side_pot_and_all_in_opponent(self):
        state = {
            "hero_cards": ["Kh", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 130,
            "seats": [
                {"seat": "hero", "stack": 100, "in_hand": True, "wager": 0, "committed": 40},
                {
                    "seat": "short",
                    "stack": 0,
                    "in_hand": True,
                    "wager": 10,
                    "committed": 10,
                    "range": [{"cards": ["As", "Ad"]}],
                },
                {
                    "seat": "deep",
                    "stack": 80,
                    "in_hand": True,
                    "wager": 40,
                    "committed": 80,
                    "range": [{"cards": ["Qs", "Qd"]}],
                },
            ],
            "actions": [{"kind": "fold", "amount": 0}, {"kind": "call", "amount": 40}],
        }
        result = analyze(state, samples=100)
        self.assertEqual(result["method"], "monte_carlo")
        self.assertEqual(result["recommendation"]["ev"], 100)
        self.assertEqual(result["recommendation"]["ci95"], [100, 100])

    def test_joint_weighted_draws_reject_conflicting_hands_symmetrically(self):
        state = {
            "hero_cards": ["Kh", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 90,
            "seats": [
                {"seat": "hero", "stack": 100, "in_hand": True, "wager": 0},
                {
                    "seat": "first",
                    "stack": 100,
                    "in_hand": True,
                    "wager": 0,
                    "range": [{"cards": ["As", "Ad"]}, {"cards": ["Qs", "Qd"]}],
                },
                {
                    "seat": "second",
                    "stack": 100,
                    "in_hand": True,
                    "wager": 0,
                    "range": [{"cards": ["As", "Ac"]}, {"cards": ["Ts", "Td"]}],
                },
            ],
            "actions": [{"kind": "check", "amount": 0}],
        }
        # Two of three equally weighted legal pairs contain aces.
        result = analyze(state, samples=4500, seed=29)
        self.assertAlmostEqual(result["recommendation"]["ev"], 30, delta=2)
        self.assertGreater(result["recommendation"]["std_error"], 0)
        self.assertEqual(result, analyze(state, samples=4500, seed=29))
        state["seats"][1]["range"].pop()
        state["seats"][2]["range"].pop()
        with self.assertRaisesRegex(ValueError, "有限抽样"):
            analyze(state, samples=100)

    def test_preflop_multiway_reproducible_and_duplicate_cards_rejected(self):
        state = {
            "hero_cards": ["Ah", "Ad"],
            "board": [],
            "pot": 100,
            "seats": [
                {"seat": "hero", "stack": 200, "in_hand": True, "wager": 0},
                {"seat": "first", "stack": 200, "in_hand": True, "wager": 20},
                {"seat": "second", "stack": 200, "in_hand": True, "wager": 0},
            ],
            "actions": [{"kind": "fold", "amount": 0}, {"kind": "call", "amount": 20}],
        }
        first = analyze(state, samples=150, seed=1)
        self.assertEqual(first, analyze(state, samples=150, seed=1))
        self.assertNotEqual(first["recommendation"]["ev"], analyze(state, samples=150, seed=2)["recommendation"]["ev"])
        self.assertTrue(any("默认补筹码" in note for note in first["assumptions"]))
        state["board"] = ["Ad", "Ts", "9d"]
        with self.assertRaisesRegex(ValueError, "不能重复"):
            analyze(state)

    def test_incorrect_bet_target_and_mismatching_commitments_are_rejected(self):
        state = {
            "hero_cards": ["Ah", "Ad"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 50,
            "seats": [
                {"seat": "hero", "stack": 100, "in_hand": True, "wager": 10, "committed": 10},
                {"seat": "villain", "stack": 80, "in_hand": True, "wager": 30, "committed": 30},
            ],
            "actions": [{"kind": "call", "amount": 30}],
        }
        with self.assertRaisesRegex(ValueError, "committed"):
            analyze(state)
        state["pot"] = 40
        with self.assertRaisesRegex(ValueError, "跟注 amount"):
            analyze(state)
        state["actions"] = [{"kind": "raise", "amount": 20}]
        with self.assertRaisesRegex(ValueError, "加注必须"):
            analyze(state)


    def test_hand_dependent_responses_change_exact_bet_ev(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 100,
            "seats": [
                {"seat": "hero", "stack": 150, "in_hand": True, "wager": 0},
                {
                    "seat": "villain",
                    "stack": 150,
                    "in_hand": True,
                    "wager": 0,
                    "range": [{"cards": ["As", "Ad"]}, {"cards": ["Qs", "Qd"]}],
                },
            ],
            "actions": [
                {"kind": "check", "amount": 0},
                {
                    "kind": "bet",
                    "amount": 30,
                    "responses": {"villain": {"default": 0, "by_hand": [{"cards": ["Ad", "As"], "probability": 1}]}},
                },
            ],
        }
        result = analyze(state)
        self.assertEqual(result["actions"][1]["ev"], 35)
        self.assertEqual(result["recommendation"]["kind"], "check")
        state["actions"][1]["responses"]["villain"] = 0.5
        result = analyze(state)
        self.assertEqual(result["actions"][1]["ev"], 75)
        self.assertEqual(result["recommendation"]["kind"], "bet")

    def test_simulation_applies_hand_specific_fold_even_for_short_opponent(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s"],
            "pot": 100,
            "seats": [
                {"seat": "hero", "stack": 150, "in_hand": True, "wager": 0},
                {
                    "seat": "villain",
                    "stack": 15,
                    "in_hand": True,
                    "wager": 0,
                    "range": [{"cards": ["As", "Ad"]}],
                },
            ],
            "actions": [
                {
                    "kind": "bet",
                    "amount": 30,
                    "responses": {"villain": {"default": 1, "by_hand": [{"cards": ["Ad", "As"], "probability": 0}]}},
                },
            ],
        }
        result = analyze(state, samples=100, seed=17)
        self.assertEqual(result["method"], "monte_carlo")
        self.assertEqual(result["actions"][0]["ev"], 100)
        state["actions"][0]["responses"]["villain"] = 1
        with self.assertRaisesRegex(ValueError, "对手无法全额跟注"):
            analyze(state, samples=100)

    def test_main_pot_dead_money_is_earned_and_not_refunded(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 50,
            "dead_money": 10,
            "seats": [
                {"seat": "hero", "stack": 100, "in_hand": True, "wager": 0, "committed": 20},
                {
                    "seat": "villain",
                    "stack": 100,
                    "in_hand": True,
                    "wager": 0,
                    "committed": 20,
                    "range": [{"cards": ["As", "Ad"]}],
                },
            ],
            "actions": [{"kind": "check", "amount": 0}],
        }
        self.assertEqual(analyze(state)["actions"][0]["ev"], 0)
        state["seats"][1]["range"] = [{"cards": ["Qs", "Qd"]}]
        self.assertEqual(analyze(state)["actions"][0]["ev"], 50)
        state["seats"][1]["stack"] = 0
        for seat in state["seats"]:
            del seat["wager"]
        with self.assertRaisesRegex(ValueError, "全下对手.*wager"):
            analyze(state)

    def test_decimal_chips_preserve_the_last_cent_at_large_values(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s", "Jc"],
            "pot": 200000000000.21,
            "seats": [
                {"seat": "hero", "stack": 100, "in_hand": True, "wager": 100000000000.1, "committed": 100000000000.1},
                {
                    "seat": "villain",
                    "stack": 100,
                    "in_hand": True,
                    "wager": 100000000000.11,
                    "committed": 100000000000.11,
                    "range": [{"cards": ["As", "Ad"]}],
                },
            ],
            "actions": [{"kind": "call", "amount": 0.01}],
        }
        self.assertEqual(analyze(state)["actions"][0]["ev"], -0.01)

    def test_reachable_side_pot_without_eligible_player_is_rejected_before_sampling(self):
        state = {
            "hero_cards": ["Ks", "Kd"],
            "board": ["2c", "3d", "7h", "9s"],
            "pot": 80,
            "seats": [
                {"seat": "hero", "stack": 100, "in_hand": True, "wager": 0, "committed": 10},
                {"seat": "villain", "stack": 100, "in_hand": True, "wager": 0, "committed": 10},
                {"seat": "folded_first", "stack": 100, "in_hand": False, "wager": 0, "committed": 30},
                {"seat": "folded_second", "stack": 100, "in_hand": False, "wager": 0, "committed": 30},
            ],
            "actions": [{"kind": "check", "amount": 0}],
        }
        with self.assertRaisesRegex(ValueError, "分池没有可领取"):
            analyze(state, samples=100)


if __name__ == "__main__":
    unittest.main()
