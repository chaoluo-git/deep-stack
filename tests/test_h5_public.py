from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import unittest

from deep_stack.h5_public import PublicCapture, validate_snapshot
from deep_stack.player_store import PlayerStore


def seat(slot=1, *, player_id=None, action=None, wager=None, stack=None):
    return {"slot": slot, "player_id": player_id, "action": action, "wager": wager, "stack": stack}


def snapshot(offset, *, table_ref="29", hand_ref="72", street="preflop", board=(), seats=(), table_details=None):
    result = {
        "offset_ms": offset, "table_ref": table_ref, "hand_ref": hand_ref, "street": street,
        "board": list(board), "seats": list(seats),
    }
    if table_details is not None:
        result["table_details"] = table_details
    return result


def table_details(*, small_blind=None, big_blind=None, ante=None, third_blind=None, dealer_slot=None):
    return {
        "small_blind": small_blind, "big_blind": big_blind, "ante": ante,
        "third_blind": third_blind, "dealer_slot": dealer_slot,
    }


def select(events, kind):
    return [event for event in events if event["type"] == "public_" + kind]


class H5PublicTests(unittest.TestCase):
    def setUp(self):
        self.capture = PublicCapture()
        self.capture_id = str(uuid4())

    def accept(self, *samples):
        return self.capture.accept(self.capture_id, list(samples))

    def test_player_history_follows_stable_id_across_hands_and_tables(self):
        events = self.accept(
            snapshot(0, seats=[seat(5, player_id="101", action="call", wager=4)]),
            snapshot(1, seats=[seat(5, player_id="101", action="call", wager=24)]),
            snapshot(2, seats=[seat(5, player_id="202")]),
            snapshot(3, seats=[seat(5, player_id="202", action="raise")]),
            snapshot(4, hand_ref="73", seats=[seat(9, player_id="101", action="raise"), seat(5, player_id="202")]),
            snapshot(5, table_ref="33", seats=[seat(9, player_id="101", action="fold")]),
            snapshot(6, table_ref=None, seats=[seat(1, player_id="303", action="check")]),
        )
        with TemporaryDirectory() as folder, PlayerStore(Path(folder) / "players.sqlite3") as store:
            store.record_h5_public(events)
            store.record_h5_public(events)
            stats = store.stats("101")
            self.assertEqual((stats["action_count"], stats["observed_hands"], stats["observed_wager"]["count"]), (3, 3, 0))
            self.assertEqual([(row["hand_id"], row["seat"], row["action"]) for row in store.events("101")], [
                ("72", "slot-9", "fold"), ("73", "slot-9", "raise"), ("72", "slot-5", "call"),
            ])
            for uncertain in ("202", "303"):
                self.assertEqual(store.stats(uncertain)["action_count"], 0)
                self.assertTrue(select(store.h5_public_events(player_id=uncertain), "action"))
            observed = select(store.h5_public_events(player_id="101"), "action")
            self.assertEqual([(event["action"], event["wager"], event["wager_update"]) for event in observed], [
                ("fold", None, False), ("raise", None, False), ("call", 24, True), ("call", 4, False),
            ])

    def test_anonymous_action_is_not_backfilled_by_an_amount_update(self):
        events = self.accept(
            snapshot(0, seats=[seat(2, action="call")]),
            snapshot(1, seats=[seat(2, action="call", player_id="101", wager=12)]),
        )
        with TemporaryDirectory() as folder, PlayerStore(Path(folder) / "players.sqlite3") as store:
            store.record_h5_public(events)
            self.assertEqual(store.stats("101")["action_count"], 0)
            observed = select(store.h5_public_events(), "action")
            self.assertEqual([(event["player_id"], event["wager_update"]) for event in observed], [
                ("101", True), (None, False),
            ])

    def test_validate_clones_and_limits_snapshots_to_public_fields(self):
        original = snapshot(0, street="flop", board=("As", "2h"), seats=[
            seat(9, player_id="0001", action="all_in", wager=0, stack=12.25), seat(2),
        ])
        normalized = validate_snapshot(original)
        self.assertEqual([item["slot"] for item in normalized["seats"]], [2, 9])
        self.assertEqual(normalized["seats"][1]["player_id"], "0001")
        original["board"].clear()
        original["seats"][0]["player_id"] = None
        self.assertEqual(normalized["board"], ["As", "2h"])
        self.assertEqual(normalized["seats"][1]["player_id"], "0001")

        for bad in (
            {**normalized, "hole_cards": ["As", "Kh"]},
            {**normalized, "offset_ms": True}, {**normalized, "offset_ms": 2**53},
            {**normalized, "table_ref": "token-secret"}, {**normalized, "hand_ref": "1" * 25},
            {**normalized, "street": "turning"}, {**normalized, "board": ["As", "As"]},
            {**normalized, "board": ["as"]}, {**normalized, "board": ["As", None]},
            snapshot(1, seats=[seat(1), seat(1)]), snapshot(1, seats=[seat(True)]),
            snapshot(1, seats=[seat(2, player_id=78)]), snapshot(1, seats=[seat(2, player_id="0")]),
            snapshot(1, seats=[seat(2, action=[])]), snapshot(1, seats=[seat(2, action="big_blind")]),
            snapshot(1, seats=[seat(2, wager=float("inf"))]),
            snapshot(1, seats=[seat(2, stack=float("nan"))]), snapshot(1, seats=[seat(2, stack=True)]),
            snapshot(1, seats=[{**seat(), "name": "private-nickname"}]),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_snapshot(bad)

    def test_batches_are_atomic_ordered_and_independent(self):
        initial = snapshot(10)
        middle = snapshot(11, seats=[seat(action="call")])
        self.accept(initial)
        with self.assertRaisesRegex(ValueError, "时间顺序"):
            self.accept(middle, snapshot(1))
        with self.assertRaisesRegex(ValueError, "公共牌"):
            self.accept(middle, snapshot(12, board=("XX",)))
        self.assertEqual(select(self.accept(middle), "action")[0]["action"], "call")
        self.assertEqual(self.accept(deepcopy(middle)), [])
        with self.assertRaisesRegex(ValueError, "时间顺序"):
            self.accept(snapshot(11, hand_ref="73"))
        self.assertEqual(self.capture.accept(str(uuid4()), [initial])[0]["type"], "public_table")

    def test_optional_table_details_validate_complete_allowlist_and_clear_unobserved_dealer(self):
        inputs = snapshot(0, seats=[seat(3)], table_details=table_details(
            small_blind=0.5, big_blind=1, ante=0.1, third_blind=2, dealer_slot=3,
        ))
        validated = validate_snapshot(inputs)
        self.assertEqual(validated, inputs)
        inputs["table_details"]["dealer_slot"] = 7
        missing_dealer = validate_snapshot(inputs)
        self.assertEqual(validated["table_details"]["dealer_slot"], 3)
        self.assertIsNone(missing_dealer["table_details"]["dealer_slot"])
        self.assertEqual(missing_dealer["table_details"]["big_blind"], 1)
        self.assertNotIn("table_details", validate_snapshot(snapshot(0)))

        for bad in (
            None, {**table_details(), "small_blind_slot": 2}, {"dealer_slot": 3},
            table_details(small_blind=2), table_details(big_blind=2),
            table_details(small_blind=2, big_blind=1), table_details(ante=0),
            table_details(ante=True), table_details(ante="1"), table_details(ante=1e12 + 1),
            table_details(third_blind=float("inf")), table_details(dealer_slot=0),
            table_details(dealer_slot=True), table_details(dealer_slot=10),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_snapshot({**snapshot(0), "table_details": bad})

    def test_table_detail_changes_and_clear_are_observed_without_replaying_actions(self):
        both = [seat(2, action="call", wager=2), seat(4)]
        first = table_details(small_blind=1, big_blind=2, ante=0.5, dealer_slot=2)
        changed = {**first, "dealer_slot": 4}
        events = self.accept(
            snapshot(0, seats=both),
            snapshot(1, seats=both, table_details=first),
            snapshot(2, seats=both, table_details=dict(first)),
            snapshot(3, seats=both, table_details=changed),
            snapshot(4, seats=both[:1], table_details=changed),
            snapshot(5, seats=both[:1], table_details=table_details()),
            snapshot(6, seats=both[:1]),
        )
        tables = select(events, "table")
        self.assertEqual([(item["offset_ms"], item.get("table_details")) for item in tables], [
            (0, None), (1, first), (3, changed), (4, {**changed, "dealer_slot": None}),
            (5, table_details()), (6, None),
        ])
        self.assertIsNot(tables[1]["table_details"], first)
        self.assertEqual([event["offset_ms"] for event in select(events, "action")], [0])
        self.assertTrue(all("table_details" not in event for event in select(events, "action")))

    def test_observes_complete_boards_and_ignores_incomplete_or_nonprogressive_rendering(self):
        flop = ("As", "Td", "4c")
        turn = (*flop, "2s")
        river = (*turn, "9h")
        events = self.accept(
            snapshot(0, hand_ref=None),
            snapshot(1, street="flop", board=flop[:2]),
            snapshot(2, street="flop", board=flop),
            snapshot(3, street="turn", board=flop),
            snapshot(4, street="turn", board=turn),
            snapshot(5, street="river", board=("Ah", *turn[1:], "9h")),
            snapshot(6, street="river", board=river),
            snapshot(7, street="turn", board=turn),
            snapshot(8, street="settled", board=river),
            snapshot(9, street="flop", board=flop, hand_ref="73"),
            snapshot(10, hand_ref="74"),
            snapshot(11, street="flop", board=flop, hand_ref="74"),
        )
        boards = select(events, "board")
        self.assertEqual(
            [(item["offset_ms"], item["board"], item["confirmed_hand"]) for item in boards],
            [(0, [], False), (2, list(flop), True), (4, list(turn), True), (6, list(river), True),
             (10, [], True), (11, list(flop), True)],
        )

    def test_retained_labels_and_animation_amounts_are_not_new_actions(self):
        flop = ("As", "Td", "4c")
        events = self.accept(
            snapshot(0, seats=[seat(action="call")]),
            snapshot(1, seats=[seat(action="call", wager=20, stack=60)]),
            snapshot(2, seats=[seat(action="call", wager=20, stack=60)]),
            snapshot(3, seats=[seat(action="call", wager=0, stack=60)]),
            snapshot(4, street="flop", board=flop, seats=[seat(action="call", wager=20, stack=60)]),
            snapshot(5, street="flop", board=flop, seats=[seat(action="call", wager=40, stack=60)]),
            snapshot(6, street="flop", board=flop, seats=[seat(wager=0, stack=60)]),
            snapshot(7, street="flop", board=flop, seats=[seat(action="call", wager=40, stack=20)]),
            snapshot(8, street="settled", board=flop, seats=[seat(action="bet", wager=60, stack=0)]),
        )
        self.assertEqual(
            [(item["offset_ms"], item["wager"], item["wager_update"]) for item in select(events, "action")],
            [(0, None, False), (1, 20, True), (7, 40, False)],
        )
        self.assertEqual([item["offset_ms"] for item in select(events, "chips")], [1, 3, 4, 5, 6, 7, 8])

    def test_actor_association_is_same_frame_and_requires_one_matching_slot(self):
        events = self.accept(
            snapshot(0, seats=[seat(1, action="call", player_id="111", wager=7)]),
            snapshot(1, seats=[seat(1, action="raise", wager=13)]),
            snapshot(2, seats=[seat(1, action="raise", player_id="222", wager=13)]),
            snapshot(3, seats=[seat(1, action="fold", player_id="222"), seat(2, player_id="222")]),
            snapshot(4, hand_ref=None, seats=[seat(2, action="bet", player_id="222", wager=4)]),
        )
        actions = select(events, "action")
        self.assertEqual(
            [(item["offset_ms"], item["slot"], item["player_id"], item["hand_ref"]) for item in actions],
            [(0, 1, "111", "72"), (1, 1, None, "72"), (3, 1, None, "72"), (4, 2, "222", None)],
        )
        self.assertFalse(actions[-1]["confirmed_hand"])
        self.assertEqual(
            [(item["offset_ms"], item["slot"], item["player_id"]) for item in select(events, "identity")],
            [(0, 1, "111"), (1, 1, None), (2, 1, "222"), (3, 1, None), (4, 2, "222")],
        )

    def test_terminal_label_is_not_replayed_across_streets_and_true_hand_edges_reset_it(self):
        flop = ("As", "Td", "4c")
        events = self.accept(
            snapshot(0, seats=[seat(action="fold")]),
            snapshot(1, street="flop", board=flop),
            snapshot(2, street="flop", board=flop, seats=[seat(action="fold")]),
            snapshot(3, street="flop", board=flop, seats=[seat(action="check")]),
            snapshot(4, hand_ref="73", seats=[seat(action="check")]),
            snapshot(5, hand_ref="73", seats=[seat()]),
            snapshot(6, hand_ref="73", seats=[seat(action="fold")]),
        )
        self.assertEqual(
            [(item["offset_ms"], item["action"], item["hand_ref"]) for item in select(events, "action")],
            [(0, "fold", "72"), (6, "fold", "73")],
        )

    def test_unknown_hand_never_inherits_confirmed_identity_or_infers_new_hand(self):
        flop = ("As", "Td", "4c")
        events = self.accept(
            snapshot(0, street="flop", board=flop, seats=[seat(action="bet", player_id="111")]),
            snapshot(1, hand_ref=None, seats=[seat(action="bet")]),
            snapshot(2, hand_ref=None, seats=[seat(action="check")]),
            snapshot(3, hand_ref=None, street="flop", board=flop),
        )
        self.assertEqual(
            [(item["offset_ms"], item["hand_ref"], item["player_id"]) for item in select(events, "action")],
            [(0, "72", "111"), (2, None, None)],
        )
        self.assertEqual([item["offset_ms"] for item in select(events, "board")], [0, 1])

    def test_unknown_hands_can_observe_new_fold_after_visible_settlement_and_label_clear(self):
        events = self.accept(
            snapshot(0, hand_ref=None, seats=[seat(action="fold")]),
            snapshot(1, hand_ref=None, street="settled", seats=[seat(action="fold")]),
            snapshot(2, hand_ref=None, seats=[seat(action="fold")]),
            snapshot(3, hand_ref=None, seats=[seat()]),
            snapshot(4, hand_ref=None, seats=[seat(action="fold")]),
        )
        self.assertEqual(
            [(event["offset_ms"], event["confirmed_hand"]) for event in select(events, "action")],
            [(0, False), (4, False)],
        )


if __name__ == "__main__":
    unittest.main()
