import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from deep_stack.h5_correlate import correlate_timeline


_HEADER = {"type": "calibration_start", "version": 1}
_FRAME = {"connection": "ws-1", "offset_ms": 0, "direction": "received", "opcode": 2,
          "size_bytes": 17, "encoding": "binary"}


def batch(index, arrival, *offsets, capture=1):
    return {"type": "h5_batch", "capture_id": str(UUID(int=capture)), "batch_id": str(UUID(int=index)),
            "arrival_ms": arrival, "frames": [{**_FRAME, "offset_ms": offset} for offset in offsets]}


def action(at, **overrides):
    return {"type": "screen_action", "at_ms": at, "hand_id": "local-hand-alias", "street": "flop",
            "seat": "right_lower", "action": "bet", "wager": 12.5, "confirmed_hand": True,
            "position": "HJ", **overrides}


def board(at, **overrides):
    return {"type": "screen_board", "at_ms": at, "hand_id": "local-hand-alias", "street": "flop",
            "cards": ["As", "Kc", "6d"], "complete": True, **overrides}


class H5CorrelateTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "table.jsonl"

    def write(self, *records, tail=b"", header=_HEADER):
        content = "".join(f"{json.dumps(row)}\n" for row in (header, *records))
        self.path.write_bytes(content.encode("utf-8") + tail)

    def test_preserves_multiple_candidates_and_reports_offsets_as_timing_only(self):
        self.write(board(2000), batch(1, 1050, 20, 50), action(1100), batch(2, 1090, 100),
                   batch(3, 1150, 100, capture=2), batch(4, 2008, 120, capture=2), action(3000))
        report = correlate_timeline(self.path, 55)
        event, public, none = report["markers"]
        self.assertEqual((event["seat"], event["position"], event["wager"], event["hand_index"]),
                         ("right_lower", "HJ", 12.5, 1))
        self.assertEqual((event["candidate_status"], event["candidate_batch_count"], event["candidate_frame_count"]),
                         ("multiple", 3, 4))
        self.assertEqual([item["arrival_delta_ms"] for item in event["candidates"]], [-10, -50, -50, 50])
        first_batch_frame = event["candidates"][1]
        self.assertEqual(first_batch_frame["batch_offset_estimate_ms"], 1020)
        self.assertEqual(first_batch_frame["batch_offset_estimate_delta_ms"], -80)
        self.assertEqual(first_batch_frame["fixed_offset_latest_at_ms"], 1011)
        self.assertEqual(first_batch_frame["feasible_at_ms"], {"earliest": None, "latest": 1050})
        self.assertEqual(public["candidate_status"], "single")
        self.assertEqual(public["cards"], ["As", "Kc", "6d"])
        self.assertEqual(public["hand_index"], 1)
        self.assertEqual((none["candidate_status"], none["candidates"]), ("none", []))
        self.assertEqual(report["summary"]["screen_action"],
                         {"marker_count": 2, "none": 1, "single": 0, "multiple": 1})
        self.assertIsNone(report["method"]["application_message_mapping"])
        self.assertNotIn("local-hand-alias", json.dumps(report))
        self.assertNotIn(str(UUID(int=1)), json.dumps(report))

    def test_window_covers_capture_interval_and_includes_exact_boundaries(self):
        self.write(batch(1, 939, 0), batch(2, 940, 1), batch(3, 1000, 2), batch(4, 1001, 3),
                   action(1000, sample_started_ms=940), board(1000))
        event, point = correlate_timeline(self.path, 0)["markers"]
        self.assertEqual([item["batch_arrival_ms"] for item in event["candidates"]], [940, 1000])
        self.assertEqual(point["candidate_frame_count"], 1)
        self.assertEqual(point["sample_started_ms"], 1000)

    def test_partial_final_json_is_reported_but_invalid_complete_rows_fail(self):
        self.write(action(200), tail=b'{"type":"h5_batch","frames":[')
        result = correlate_timeline(self.path)
        self.assertTrue(result["source"]["ignored_partial_final_line"])
        self.assertEqual(len(result["markers"]), 1)
        for tail in (b'{"type":"h5_batch","frames":[\n', b'{incorrect}', b'null', b'{"x":1,"x":2}'):
            with self.subTest(tail=tail):
                self.write(action(200), tail=tail)
                with self.assertRaisesRegex(ValueError, "第 3 行"):
                    correlate_timeline(self.path)

    def test_valid_final_row_without_newline_is_not_truncated(self):
        self.write(tail=json.dumps(board(200)).encode("utf-8"))
        result = correlate_timeline(self.path)
        self.assertFalse(result["source"]["ignored_partial_final_line"])
        self.assertEqual(result["markers"][0]["type"], "screen_board")

    def test_identical_batch_retransmission_does_not_add_candidates(self):
        original = batch(1, 200, 0)
        self.write(original, {**original, "arrival_ms": 225}, action(220))
        result = correlate_timeline(self.path)
        self.assertEqual(result["source"]["duplicate_batch_count"], 1)
        self.assertEqual(result["markers"][0]["candidate_frame_count"], 1)
        self.assertEqual(result["markers"][0]["candidates"][0]["batch_arrival_ms"], 200)
        self.write(original, batch(1, 200, 1))
        with self.assertRaisesRegex(ValueError, "第 3 行"):
            correlate_timeline(self.path)

    def test_candidate_caps_preserve_exact_counts_and_ambiguity(self):
        self.write(batch(1, 200, 1, 2, 3, 4), action(200), board(201))
        with (patch("deep_stack.h5_correlate._MAX_LISTED_PER_MARKER", 2),
              patch("deep_stack.h5_correlate._MAX_LISTED_TOTAL", 3)):
            markers = correlate_timeline(self.path)["markers"]
        self.assertEqual([item["candidate_status"] for item in markers], ["multiple", "multiple"])
        self.assertEqual([item["candidate_frame_count"] for item in markers], [4, 4])
        self.assertEqual([item["omitted_candidate_frame_count"] for item in markers], [2, 3])

    def test_rejects_invalid_record_shapes_and_clock_bounds(self):
        invalid = (
            action(200, at_ms=True), action(200, sample_started_ms=201), action(200, seat="player-id"),
            action(200, confirmed_hand=True, hand_id=None), action(200, wager=float("inf")),
            board(200, cards=["As", "As", "6d"]), board(200, cards=["As", None, "6d"]),
            {**batch(1, 200, 0), "frames": [{**_FRAME, "payload": "private"}]},
            batch(1, 2**53, 0), {"type": "listening"},
        )
        for row in invalid:
            with self.subTest(row=row):
                self.write(row)
                with self.assertRaisesRegex(ValueError, "第 2 行"):
                    correlate_timeline(self.path)
        self.write(header={**_HEADER, "version": True})
        with self.assertRaisesRegex(ValueError, "calibration_start"):
            correlate_timeline(self.path)
        for window in (True, -1, 60_001, 0.5):
            with self.subTest(window=window), self.assertRaisesRegex(ValueError, "时间窗口"):
                correlate_timeline(self.path, window)

    def test_caps_line_bytes_and_input_frame_count(self):
        self.write(batch(1, 0, 1, 2))
        with patch("deep_stack.h5_correlate._MAX_FRAMES", 1), self.assertRaisesRegex(ValueError, "最多支持"):
            correlate_timeline(self.path)
        with patch("deep_stack.h5_correlate._MAX_LINE", 120), self.assertRaisesRegex(ValueError, "第 2 行过长"):
            correlate_timeline(self.path)
