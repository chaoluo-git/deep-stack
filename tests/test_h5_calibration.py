from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from deep_stack.h5_calibration import CalibrationRecorder
from deep_stack.live_actions import ActionLog
from deep_stack.live_board import BoardLog


_HEADER = {"type": "calibration_start", "version": 1}
_FRAME = {
    "connection": "ws-1", "offset_ms": 19, "direction": "received",
    "opcode": 2, "size_bytes": 63, "encoding": "binary",
}
_ACTION = {
    "type": "action", "street": "flop", "seat": "right_upper", "action": "raise",
    "wager": 2.5, "hand_id": "private-screen-id", "confirmed_hand": True,
}
_BOARD = {
    "type": "board", "street": "flop", "cards": ["As", None, "6h"],
    "complete": False, "hand_id": _ACTION["hand_id"],
}


def h5_event(*frames):
    return {"type": "frames", "capture_id": str(uuid4()), "batch_id": str(uuid4()), "frames": list(frames)}


class CalibrationRecorderTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "journal.jsonl"

    def rows(self, path=None):
        return [json.loads(line) for line in (path or self.path).read_text(encoding="utf-8").splitlines()]

    def test_timing_and_allowlists_keep_only_anonymous_evidence(self):
        clock = iter((100, 102, 104)).__next__
        candidate = h5_event({**_FRAME, "payloadData": "secret-payload"})
        candidate["token"] = "secret-bearer"
        candidate["url"] = "https://secret.invalid"
        with CalibrationRecorder(self.path, clock=clock) as journal:
            journal.h5(candidate)
            journal.screen({**_ACTION, "player_id": "secret-stable-id", "name": "secret-nickname",
                            "message": "secret-action-message", "position": "BB", "wager_update": True},
                           captured_at=101.25, sample_started_at=101)
            journal.screen({**_BOARD, "hero_cards": ["Jc", "9h"], "evidence": "secret-ocr-evidence"})

        start, h5, action, board = self.rows()
        self.assertEqual(start, _HEADER)
        self.assertEqual(h5, {"type": "h5_batch", "arrival_ms": 2000,
                              "capture_id": candidate["capture_id"], "batch_id": candidate["batch_id"],
                              "frames": [_FRAME]})
        alias = action["hand_id"]
        self.assertRegex(alias, r"^[a-f0-9]{24}$")
        self.assertEqual(action, {
            "type": "screen_action", "street": "flop", "at_ms": 1250, "sample_started_ms": 1000,
            "hand_id": alias, "seat": "right_upper", "action": "raise", "wager": 2.5,
            "confirmed_hand": True, "position": "BB", "wager_update": True,
        })
        self.assertEqual(board, {
            "type": "screen_board", "street": "flop", "at_ms": 4000, "hand_id": alias,
            "cards": ["As", None, "6h"], "complete": False,
        })
        serialized = self.path.read_text(encoding="utf-8")
        for value in ("secret-", "private-screen-id", "hero_cards", "player_id",
                      '"message"', '"name"', '"url"', '"token"', "payloadData"):
            self.assertNotIn(value, serialized)

    def test_accepts_action_log_and_board_log_without_keeping_player_identity(self):
        hand_id = uuid4().hex
        frame = {
            "street": "preflop", "board": [],
            "seats": [{"seat": "hero", "action": "call", "wager": 12, "name": "private-user"}],
        }
        bindings = [{"seat": "hero", "paused": False, "player_id": "6543210123", "name": "private-user"}]
        actions = ActionLog().update(frame, hand_id, bindings, positions={"hero": "BTN/SB"})
        board = BoardLog().update(frame, hand_id)
        self.assertEqual(actions[0]["player_id"], "6543210123")
        with CalibrationRecorder(self.path, clock=lambda: 10) as journal:
            journal.screen(actions[0])
            journal.screen(board, captured_at=11)
            unknown = {**_ACTION, "hand_id": None, "street": None, "confirmed_hand": False}
            journal.screen(unknown, captured_at=12)
        _, action, public_board, unconfirmed = self.rows()
        self.assertEqual((action["position"], action["hand_id"]), ("BTN/SB", public_board["hand_id"]))
        self.assertEqual((public_board["cards"], public_board["complete"]), ([], True))
        self.assertEqual((unconfirmed["hand_id"], unconfirmed["confirmed_hand"]), (None, False))
        self.assertNotIn("6543210123", self.path.read_text(encoding="utf-8"))

    def test_exclusive_open_and_idempotent_close_never_overwrite_records(self):
        self.path.write_bytes(b"existing user file")
        with self.assertRaisesRegex(ValueError, "已存在"), CalibrationRecorder(self.path):
            self.fail("An existing file must not be replaced")
        self.assertEqual(self.path.read_bytes(), b"existing user file")
        self.path.unlink()
        journal = CalibrationRecorder(self.path, clock=lambda: 20)
        journal.close()
        with self.assertRaisesRegex(RuntimeError, "未开启"):
            journal.screen(_BOARD)
        with journal:
            if os.name == "posix":
                self.assertEqual(self.path.stat().st_mode & 0o077, 0)
            with self.assertRaisesRegex(RuntimeError, "已开启"):
                journal.__enter__()
            journal.screen(_BOARD)
            journal.close()
            journal.close()
            before = self.path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "已经关闭"):
                journal.h5(h5_event(_FRAME))
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.rows()), 2)

    def test_hand_ids_are_stable_within_each_file_only(self):
        other = self.path.with_name("second.jsonl")
        for path in (self.path, other):
            with CalibrationRecorder(path, clock=lambda: 10) as journal:
                journal.screen(_ACTION)
                journal.screen(_BOARD)
        first_alias = self.rows()[1]["hand_id"]
        other_alias = self.rows(other)[1]["hand_id"]
        self.assertEqual(first_alias, self.rows()[2]["hand_id"])
        self.assertEqual(other_alias, self.rows(other)[2]["hand_id"])
        self.assertNotEqual(first_alias, other_alias)

    def test_concurrent_producers_do_not_interleave_or_drop_lines(self):
        count = 80
        with CalibrationRecorder(self.path, clock=lambda: 40) as journal:
            def write_pair(index):
                journal.h5(h5_event({**_FRAME, "offset_ms": index}))
                journal.screen(_ACTION, captured_at=40 + index / 1000)

            with ThreadPoolExecutor(max_workers=8) as threads:
                list(threads.map(write_pair, range(count)))
        header, *records = self.rows()
        self.assertEqual(header, _HEADER)
        self.assertEqual(len(records), 2 * count)
        self.assertEqual({row["frames"][0]["offset_ms"] for row in records if row["type"] == "h5_batch"},
                         set(range(count)))
        self.assertEqual({row["at_ms"] for row in records if row["type"] == "screen_action"}, set(range(count)))

    def test_rejects_invalid_values_and_leaves_no_partial_record(self):
        with CalibrationRecorder(self.path, clock=lambda: 10) as journal:
            for frame in ({**_FRAME, "connection": "https://private.invalid"}, {**_FRAME, "opcode": True},
                          {**_FRAME, "size_bytes": -2}, {**_FRAME, "encoding": "private"}):
                with self.assertRaisesRegex(ValueError, "H5 校准帧字段"):
                    journal.h5(h5_event(frame))
            for event in ({**_ACTION, "seat": "invalid-seat"}, {**_ACTION, "action": "screen-label"},
                          {**_ACTION, "wager": float("nan")}, {**_ACTION, "confirmed_hand": False},
                          {**_ACTION, "position": "私人昵称"}, {**_ACTION, "wager_update": "yes"},
                          {**_ACTION, "street": "unrelated"}, {**_ACTION, "hand_id": []},
                          {**_BOARD, "cards": ["not-public", None, "7h"]},
                          {**_BOARD, "cards": ["As", "As", "7h"]},
                          {**_BOARD, "complete": True},
                          {**_BOARD, "cards": ["As", "7h"]}):
                with self.assertRaises(ValueError):
                    journal.screen(event)
            for kwargs in ({"captured_at": 9}, {"captured_at": float("inf")},
                           {"captured_at": 12, "sample_started_at": 13},
                           {"captured_at": 12, "sample_started_at": 9}):
                with self.assertRaises(ValueError):
                    journal.screen(_BOARD, **kwargs)
            self.assertEqual(self.rows(), [_HEADER])
            journal.screen(_BOARD)
        self.assertEqual(len(self.rows()), 2)

    def test_input_size_and_serialized_line_limits(self):
        with CalibrationRecorder(self.path, clock=lambda: 10) as journal:
            with self.assertRaisesRegex(ValueError, "帧数量"):
                journal.h5(h5_event(*([_FRAME] * 129)))
            with patch("deep_stack.h5_calibration._MAX_LINE_BYTES", 40):
                with self.assertRaisesRegex(ValueError, "单条校准记录"):
                    journal.h5(h5_event(_FRAME))
            self.assertEqual(self.rows(), [_HEADER])
            journal.h5(h5_event(*([_FRAME] * 128)))
        self.assertEqual(len(self.rows()[1]["frames"]), 128)
