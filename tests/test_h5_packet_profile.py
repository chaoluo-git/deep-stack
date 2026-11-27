from base64 import b64encode
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deep_stack.h5_packet_profile import profile_packets


def cdp(body, index, *, direction="Received", request="private-connection-id", opcode=2):
    payload = b64encode(body).decode("ascii") if isinstance(body, bytes) else body
    return {"method": f"Network.webSocketFrame{direction}", "sessionId": "private-session-id",
            "params": {"requestId": request, "timestamp": index, "response": {"opcode": opcode,
                                                                               "payloadData": payload}}}


def sized_body(size, *, offset=2, width=2, order="big", delta=4):
    body = bytearray(b"\xa5" * size)
    body[offset:offset + width] = (size - delta).to_bytes(width, order)
    return bytes(body)


class H5PacketProfileTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "capture.json"

    def profile(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return profile_packets(self.path)

    def test_finds_supported_length_relationships_by_direction(self):
        sizes = (310, 352, 412, 510, 601, 725)
        received = [cdp(sized_body(size), index) for index, size in enumerate(sizes)]
        sent = [cdp(sized_body(size, offset=6, width=4, order="little", delta=-2), index, direction="Sent")
                for index, size in enumerate(sizes[:-1])]
        sent.append(cdp(b"\xa5" * sizes[-1], len(sent), direction="Sent"))
        report = self.profile(received + sent)
        by_direction = {item["direction"]: item for item in report["directions"]}
        self.assertEqual(report["profiled_connection_count"], 1)
        self.assertEqual(report["binary_packet_count"], 12)
        self.assertIn({"offset_bytes": 2, "width_bytes": 2, "byte_order": "big",
                       "packet_size_minus_field_bytes": 4, "match_count": 6, "eligible_packet_count": 6,
                       "distinct_matching_sizes": 6, "observed_coverage": 1.0},
                      by_direction["received"]["length_prefix"]["candidates"])
        self.assertIn({"offset_bytes": 6, "width_bytes": 4, "byte_order": "little",
                       "packet_size_minus_field_bytes": -2, "match_count": 5, "eligible_packet_count": 6,
                       "distinct_matching_sizes": 5, "observed_coverage": 0.8333},
                      by_direction["sent"]["length_prefix"]["candidates"])

    def test_requires_a_relationship_across_different_packet_sizes(self):
        same_size = [cdp(sized_body(310), index) for index in range(8)]
        first, = self.profile(same_size)["directions"]
        self.assertEqual(first["length_prefix"]["candidate_count"], 0)
        self.assertEqual(first["length_prefix"]["max_support_across_multiple_sizes"], 0)
        two_sizes = [cdp(sized_body(310 + index % 2), index) for index in range(8)]
        first, = self.profile(two_sizes)["directions"]
        self.assertEqual(first["length_prefix"]["candidate_count"], 0)
        self.assertEqual(first["length_prefix"]["max_support_across_multiple_sizes"], 8)

    def test_anonymous_aggregate_windows_and_entropy_do_not_export_bytes(self):
        secret = b"private-body-string"
        ordinary = b"\xaa" * 16 + secret + b"\xbb" * 16
        short = b"\xaa\xaa\xaa"
        report = self.profile([cdp(ordinary, 0), cdp(ordinary, 1), cdp(short, 2),
                               cdp("private-text", 3, opcode=1), cdp(b"private-ping", 4, opcode=9)])
        group, = report["directions"]
        self.assertEqual(group["connection"], "ws-1")
        self.assertEqual(group["packet_count"], 3)
        self.assertEqual(group["size_bytes"]["min"], 3)
        self.assertEqual(group["size_bytes"]["max"], len(ordinary))
        for side in ("first", "last"):
            self.assertEqual(group[side]["four_byte_windows"], {
                "eligible_packet_count": 2, "distinct_count": 1, "duplicate_occurrences": 1,
                "repeated_value_count": 1,
            })
            self.assertEqual(group[side]["sixteen_byte_entropy"], {
                "eligible_packet_count": 2, "sampled_byte_count": 32, "pooled_bits_per_byte": 0,
            })
        encoded = json.dumps(report)
        self.assertNotIn("private-", encoded)
        self.assertNotIn(b64encode(ordinary).decode("ascii"), encoded)
        self.assertNotIn("payload", encoded)

    def test_short_empty_and_har_binary_records_are_supported(self):
        har = {"log": {"entries": [{"request": {"url": "https://private.invalid"}, "_webSocketMessages": [
            {"type": "receive", "time": 1, "opcode": 2, "data": ""},
            {"type": "receive", "time": 2, "opcode": 2, "data": b64encode(b"x").decode("ascii")},
        ]}]}}
        report = self.profile(har)
        group, = report["directions"]
        self.assertEqual(report["binary_packet_count"], 2)
        self.assertEqual(group["size_bytes"]["distinct_count"], 2)
        self.assertEqual(group["first"]["four_byte_windows"]["eligible_packet_count"], 0)
        self.assertIsNone(group["last"]["sixteen_byte_entropy"]["pooled_bits_per_byte"])
        self.assertEqual(self.profile([])["directions"], [])

    def test_sampling_and_connection_limits_leave_explicit_counts(self):
        events = [cdp(b"sample constant data", index) for index in range(6)]
        events.extend((cdp(b"separate stream", 6, request="private-other-stream"),
                       cdp(b"other direction", 7, direction="Sent")))
        with (patch("deep_stack.h5_packet_profile._MAX_CONNECTIONS", 1),
              patch("deep_stack.h5_packet_profile._MAX_SAMPLES_PER_DIRECTION", 3)):
            report = self.profile(events)
        self.assertEqual(report["binary_packet_count"], 8)
        self.assertEqual(report["unprofiled_connection_packet_count"], 1)
        received, sent = report["directions"]
        self.assertEqual((received["direction"], received["packet_count"], received["inspected_packet_count"]),
                         ("received", 6, 3))
        self.assertEqual((sent["direction"], sent["packet_count"], sent["inspected_packet_count"]), ("sent", 1, 1))
