from base64 import b64encode
from contextlib import redirect_stderr
from gzip import compress
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deep_stack.h5_trace import inspect_trace


_SYNTHETIC_URL = "https://capture.invalid/h5?token=do-not-export-this-token"


def har_frame(data, *, timestamp=10, direction="receive", opcode=1):
    return {"type": direction, "time": timestamp, "opcode": opcode, "data": data}


def cdp_frame(data, *, method="Received", timestamp=10, opcode=1, monitor=False, session=None):
    entry = {"method": f"Network.webSocketFrame{method}"}
    if session is not None:
        entry["sessionId"] = session
    entry["result" if monitor else "params"] = {
        "requestId": "private-request-id",
        "timestamp": timestamp,
        "response": {"opcode": opcode, "mask": False, "payloadData": data},
    }
    return entry


def wire(data):
    return b64encode(data).decode("ascii")


class H5TraceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "capture.json"

    def inspect(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
        return inspect_trace(self.path)

    def test_cli_writes_only_the_report_and_keeps_original_capture(self):
        from deep_stack.__main__ import main

        expected = self.inspect([cdp_frame("private message")])
        original = self.path.read_bytes()
        output = self.path.with_name("summary.json")
        self.assertEqual(main(["h5-inspect", str(self.path), "-o", str(output)]), 0)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main(["h5-inspect", str(self.path), "-o", str(self.path)]), 2)
        self.assertIn("不同文件", errors.getvalue())
        self.assertEqual(self.path.read_bytes(), original)

    def test_har_yields_only_anonymous_payload_metadata_in_time_order(self):
        secret = '{"private-player-key":"匿名甲987654321"}'
        gzipped = compress(b"private-gzip-body", mtime=0)
        raw_binary = b"\x00\xa1\xffprivate-binary-body"
        sample = {"log": {"entries": [
            {"request": {"url": _SYNTHETIC_URL}, "_webSocketMessages": [
                har_frame(secret, timestamp=1_700_000_000.125),
                har_frame(wire(gzipped), timestamp=1_700_000_000.250, opcode=2),
                har_frame("private-error-body", timestamp=1_700_000_001, direction="error", opcode=-1),
            ]},
            {"response": {"content": {"text": "private-http-response"}}},
            {"request": {"url": _SYNTHETIC_URL}, "_webSocketMessages": [
                har_frame(wire(raw_binary), timestamp=1_700_000_000, direction="send", opcode=2),
                har_frame(wire(b""), timestamp=1_700_000_000.375, opcode=9),
            ]},
        ]}}
        report = self.inspect(sample)
        self.assertEqual(report, {
            "format": "deep-stack-h5-trace-v1",
            "source_format": "chrome_har",
            "connection_count": 2,
            "frame_count": 4,
            "reported_errors": 1,
            "payload_encodings": {"json": 1, "utf8": 0, "binary": 1, "gzip": 1, "control": 1},
            "frames": [
                {"connection": "ws-1", "offset_ms": 0, "direction": "sent",
                 "opcode": 2, "size_bytes": len(raw_binary)},
                {"connection": "ws-2", "offset_ms": 125, "direction": "received",
                 "opcode": 1, "size_bytes": len(secret.encode("utf-8"))},
                {"connection": "ws-2", "offset_ms": 250, "direction": "received",
                 "opcode": 2, "size_bytes": len(gzipped)},
                {"connection": "ws-1", "offset_ms": 375, "direction": "received",
                 "opcode": 9, "size_bytes": 0},
            ],
        })
        serialized = json.dumps(report, ensure_ascii=False)
        for field in (_SYNTHETIC_URL, "do-not-export-this-token", "private-player-key", "匿名甲987654321",
                      wire(raw_binary), wire(gzipped), "private-gzip-body", "private-binary-body",
                      "private-error-body", "private-http-response"):
            self.assertNotIn(field, serialized)

    def test_protocol_monitor_and_raw_cdp_event_arrays_are_supported(self):
        error = {
            "method": "Network.webSocketFrameError",
            "result": {"requestId": "private-request-id", "timestamp": 51,
                       "errorMessage": "private-error-message"},
        }
        records = [
            {"id": 1, "result": {"cookies": ["private-cookie-value"]}},
            {"method": "Network.webSocketCreated", "result": {"url": _SYNTHETIC_URL}},
            cdp_frame("私有字符", timestamp=50, monitor=True, session="private-session-one"),
            cdp_frame(wire(b'{"private-field":123}'), timestamp=50.050, opcode=2,
                      method="Sent", session="private-session-two"),
            cdp_frame("12", timestamp=50.100, monitor=True, session="private-session-one"),
            error,
        ]
        report = self.inspect({"events": records})
        self.assertEqual(report, self.inspect(records))
        self.assertEqual(report["source_format"], "cdp_json")
        self.assertEqual((report["connection_count"], report["frame_count"], report["reported_errors"]), (2, 3, 1))
        self.assertEqual(report["payload_encodings"], {"json": 1, "utf8": 2, "binary": 0, "gzip": 0, "control": 0})
        self.assertEqual([row["offset_ms"] for row in report["frames"]], [0, 50, 100])
        self.assertEqual([row["connection"] for row in report["frames"]], ["ws-1", "ws-2", "ws-1"])
        self.assertEqual(report["frames"][0]["size_bytes"], 12)
        for secret in (_SYNTHETIC_URL, "私有字符", "private-field", "private-session-one",
                       "private-session-two", "private-request-id", "private-error-message", "private-cookie-value"):
            self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))

    def test_empty_capture_and_error_only_capture_return_zero_frames(self):
        for document in ([], {"log": {"entries": []}},
                         {"log": {"entries": [{"_webSocketMessages": []}]}},
                         [{"method": "Network.requestWillBeSent", "params": {"url": _SYNTHETIC_URL}}]):
            with self.subTest(document=document):
                output = self.inspect(document)
                self.assertEqual((output["connection_count"], output["frame_count"], output["reported_errors"]), (0, 0, 0))
                self.assertEqual(output["frames"], [])
        error = har_frame("opaque-error", direction="error", opcode=-1)
        output = self.inspect({"log": {"entries": [{"_webSocketMessages": [error]}]}})
        self.assertEqual((output["connection_count"], output["frame_count"], output["reported_errors"]), (0, 0, 1))

    def test_malformed_files_and_messages_report_safe_validation_errors(self):
        bad_samples = (
            ({"unexpected": _SYNTHETIC_URL}, "Chrome HAR 或 CDP"),
            ({"log": {"entries": None}}, "log.entries"),
            ([42], "CDP 事件必须是"),
            ([{"method": "Network.webSocketFrameReceived"}], "缺少参数"),
            ([cdp_frame("invalid!!private", opcode=2)], "Base64"),
            ([cdp_frame("private", opcode=True)], "操作码"),
            ([cdp_frame("private", timestamp=False)], "时间无效"),
            ([cdp_frame("private", timestamp=-2)], "时间无效"),
            ([cdp_frame("\ud800")], "UTF-8"),
            ({"log": {"entries": [{"_webSocketMessages": [har_frame("private", direction="error")]}]}},
             "错误事件的操作码"),
        )
        for document, message in bad_samples:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message) as error:
                self.inspect(document)
            self.assertNotIn("private", str(error.exception))
            self.assertNotIn(_SYNTHETIC_URL, str(error.exception))
        for payload in (b"{invalid-private-content", b"\xff\xfe"):
            self.path.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
                inspect_trace(self.path)

    def test_missing_capture_cli_prints_actual_path_and_export_hint(self):
        from deep_stack.__main__ import main

        source = self.path.with_name("missing capture.har")
        output = self.path.with_name("summary.json")
        errors = StringIO()
        with redirect_stderr(errors):
            self.assertEqual(main(["h5-inspect", str(source), "-o", str(output)]), 2)
        message = errors.getvalue()
        self.assertIn("找不到本地 Chrome 捕获文件", message)
        self.assertIn(str(source), message)
        self.assertIn("Network 面板导出 HAR", message)
        self.assertIn("Protocol Monitor JSON", message)
        self.assertFalse(output.exists())

    def test_directory_and_unreadable_capture_have_specific_path_diagnostics(self):
        directory = self.path.parent
        with self.assertRaisesRegex(ValueError, "必须指向普通文件") as error:
            inspect_trace(directory)
        self.assertIn(str(directory), str(error.exception))
        self.path.write_text("private raw capture", encoding="utf-8")
        with patch.object(Path, "open", side_effect=PermissionError("private system error")):
            with self.assertRaisesRegex(ValueError, "无法读取本地 Chrome 捕获文件") as error:
                inspect_trace(self.path)
        message = str(error.exception)
        self.assertIn(str(self.path), message)
        self.assertIn("文件读取权限", message)
        self.assertNotIn("private", message)

    def test_input_and_transport_record_limits_fail_before_serialization(self):
        self.path.write_bytes(b" " * 65)
        with patch("deep_stack.h5_trace._MAX_TRACE_BYTES", 64), self.assertRaisesRegex(ValueError, "32 MiB"):
            inspect_trace(self.path)
        with patch("deep_stack.h5_trace._MAX_MESSAGES", 1), self.assertRaisesRegex(ValueError, "100000 条"):
            self.inspect([cdp_frame("first"), cdp_frame("second")])

    def test_short_or_different_binary_magic_does_not_claim_gzip(self):
        frames = [
            cdp_frame(wire(b"\x1f\x8b\x08"), opcode=2),
            cdp_frame(wire(b"\x1f\x8b\x00\x00\x00\x00\x00\x00\x00\x00"), opcode=2),
            cdp_frame(wire(b"\x1f\x8b\x08\xe0\x00\x00\x00\x00\x00\x00"), opcode=2),
            cdp_frame(wire(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x00"), opcode=2),
            cdp_frame('{"number":NaN}'),
        ]
        report = self.inspect(frames)
        self.assertEqual(report["payload_encodings"], {"json": 0, "utf8": 1, "binary": 3, "gzip": 1, "control": 0})
