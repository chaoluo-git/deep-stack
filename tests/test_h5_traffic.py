"""Verify anonymous periodic-exchange detection with synthetic frame summaries."""

import json
import unittest

from deep_stack.h5_traffic import TrafficAnalyzer, summarize_traffic


_CAPTURE = "acb38089-4f60-49cf-be4d-e74eff408c09"
_OTHER_CAPTURE = "fb21e196-3000-46d9-a82a-8654fc202bd7"


def frame(offset, direction, size, *, connection="ws-1", opcode=2):
    return {
        "connection": connection, "offset_ms": offset,
        "direction": direction, "size_bytes": size,
        "opcode": opcode, "encoding": "binary",
    }


def exchanges(count=6, *, start=0, delays=(121, 122, 119), connection="ws-1"):
    frames = []
    for index in range(count):
        sent_time = start + index * 5000
        frames.extend((
            frame(sent_time, "sent", 81 + index % 3, connection=connection),
            frame(sent_time + delays[index % len(delays)], "received", 60 + index % 4, connection=connection),
            frame(sent_time + 180, "received", 195, connection=connection),
        ))
    return frames


def batch(frames, capture=_CAPTURE):
    return {"type": "frames", "capture_id": capture, "batch_id": "private-batch-id", "frames": frames}


class TrafficTests(unittest.TestCase):
    def test_detects_only_after_repeated_same_connection_pairs_and_omits_identifiers(self):
        analyzer = TrafficAnalyzer()
        samples = exchanges()
        self.assertEqual(analyzer.update(batch(samples[:15])), [])
        result = analyzer.update(batch(samples[15:]))
        self.assertEqual(result, [{
            "type": "periodic_exchange",
            "change": "detected",
            "capture_index": 1,
            "connection": "ws-1",
            "message": "推测周期性往返｜ws-1｜约 5 秒（可能是保活或轮询）",
            "period_ms": 5000,
            "sent_bytes": [81, 83],
            "received_bytes": [60, 63],
            "latency_ms": {"min": 119, "median": 121, "max": 122},
            "samples": {"sent": 6, "paired": 6, "regular_gaps": 5, "gaps": 5},
        }])
        as_json = json.dumps(result)
        self.assertNotIn(_CAPTURE, as_json)
        self.assertNotIn("private-batch-id", as_json)
        self.assertEqual(analyzer.update(batch(samples[15:])), [])
        self.assertEqual(analyzer.update(batch(exchanges(2, start=30000))), [])

    def test_offline_distinguishes_irregular_incidental_and_cross_connection_traffic(self):
        misaligned = [
            frame(5000 * index, "sent", 81, connection="ws-1")
            for index in range(6)
        ] + [
            frame(5000 * index + 125, "received", 61, connection="ws-2")
            for index in range(6)
        ]
        self.assertEqual(summarize_traffic(misaligned), [])
        self.assertEqual(summarize_traffic(exchanges(delays=(0, 110, 245))), [])
        self.assertEqual(summarize_traffic(exchanges(delays=(300,))), [])
        nonperiodic = [frame(row["offset_ms"] * 2, row["direction"], row["size_bytes"])
                       for row in exchanges()]
        self.assertEqual(summarize_traffic(nonperiodic), [])

    def test_offline_and_live_detect_same_capture_without_other_traffic(self):
        frames = exchanges(connection="ws-2")
        frames += exchanges(connection="ws-1")
        expected = TrafficAnalyzer().update(batch(frames))
        self.assertEqual(summarize_traffic(frames), sorted(expected, key=lambda row: row["connection"]))

    def test_separates_captures_and_reports_sustained_latency_change(self):
        analyzer = TrafficAnalyzer()
        old = exchanges()
        self.assertEqual(analyzer.update(batch(old))[0]["capture_index"], 1)
        another = analyzer.update(batch(old, capture=_OTHER_CAPTURE))[0]
        self.assertEqual(another["capture_index"], 2)

        changed = []
        for index in range(65):
            changed += analyzer.update(batch(exchanges(1, start=30000 + 5000 * index, delays=(201,))))
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["change"], "changed")
        self.assertEqual(changed[0]["capture_index"], 1)
        self.assertEqual(changed[0]["latency_ms"]["median"], 201)
        self.assertLessEqual(changed[0]["samples"]["sent"], 64)

    def test_one_byte_shift_does_not_report_a_new_pattern(self):
        analyzer = TrafficAnalyzer()
        first = [dict(row, size_bytes=81 if row["direction"] == "sent" else 61)
                 for row in exchanges(delays=(121,)) if row["size_bytes"] != 195]
        self.assertEqual(len(analyzer.update(batch(first))), 1)
        for index in range(64):
            shifted = [
                frame(30000 + index * 5000, "sent", 82),
                frame(30121 + index * 5000, "received", 62),
            ]
            self.assertEqual(analyzer.update(batch(shifted)), [])

    def test_ignores_malformed_or_non_binary_summaries(self):
        analyzer = TrafficAnalyzer()
        self.assertEqual(analyzer.update(None), [])
        self.assertEqual(analyzer.update({"type": "status", "frames": exchanges()}), [])
        self.assertEqual(analyzer.update(batch(exchanges(), capture="secret-capture-id")), [])
        invalid_frames = [dict(row, connection="player-identity") for row in exchanges()]
        invalid_frames += [dict(row, opcode=1) for row in exchanges()]
        self.assertEqual(analyzer.update(batch(invalid_frames)), [])
        self.assertEqual(analyzer.update(batch(exchanges(60))), [])
