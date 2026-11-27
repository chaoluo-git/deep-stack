from contextlib import contextmanager, redirect_stderr
from copy import deepcopy
from http.client import HTTPConnection, HTTPResponse
from io import StringIO
import json
import os
from pathlib import Path
from socket import create_connection
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from deep_stack.h5_live import live_server


_ORIGIN = "chrome-extension://" + "a" * 32
_FRAME = {
    "connection": "ws-1", "offset_ms": 23, "direction": "received",
    "opcode": 2, "size_bytes": 17, "encoding": "binary",
}


def payload(*frames):
    return {"capture_id": str(uuid4()), "batch_id": str(uuid4()), "frames": list(frames)}


class H5LiveTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "pairing.json"
        self.events = []

    @contextmanager
    def receiver(self, emit=None):
        with live_server(0, self.path, self.events.append if emit is None else emit) as server:
            self.server = server
            self.pairing = json.loads(self.path.read_text(encoding="utf-8"))
            self.auth = "Bearer " + self.pairing["token"]
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                yield
            finally:
                server.shutdown()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def request(self, body=b"", *, method="POST", path="/v1/frames", headers=None, authenticated=True):
        request_headers = {"Content-Type": "application/json"}
        if authenticated:
            request_headers["Authorization"] = self.auth
        request_headers.update(headers or {})
        serialized = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=serialized, headers=request_headers)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_private_pairing_lifecycle_has_no_token_in_events(self):
        with self.receiver():
            self.assertEqual(self.server.server_address[0], "127.0.0.1")
            self.assertEqual(self.pairing["version"], 1)
            self.assertEqual(self.pairing["url"], f"http://127.0.0.1:{self.server.server_port}/v1/frames")
            self.assertRegex(self.pairing["token"], r"^[A-Za-z0-9_-]{43}$")
            if os.name == "posix":
                self.assertEqual(self.path.stat().st_mode & 0o077, 0)
            self.assertEqual(self.events, [
                {"type": "listening", "url": self.pairing["url"], "pairing_file": str(self.path)},
            ])
            self.assertNotIn(self.pairing["token"], json.dumps(self.events))
            first_token = self.pairing["token"]
        self.assertFalse(self.path.exists())
        with self.receiver():
            self.assertNotEqual(self.pairing["token"], first_token)

    def test_existing_or_changed_pairing_file_is_preserved(self):
        self.path.write_text("existing file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "配对文件已存在"), live_server(0, self.path, self.events.append):
            self.fail("An existing path must never be overwritten")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "existing file")
        self.path.unlink()
        with self.receiver():
            self.path.write_text("changed during capture", encoding="utf-8")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "changed during capture")

    def test_valid_retry_is_idempotent_and_conflicting_retry_is_rejected(self):
        batch = payload(_FRAME)
        with self.receiver():
            self.assertEqual(self.request(batch)[::2], (204, b""))
            retry = {
                "batch_id": batch["batch_id"].upper(), "capture_id": batch["capture_id"],
                "frames": [dict(reversed(tuple(_FRAME.items())))],
            }
            self.assertEqual(self.request(retry)[0], 204)
            self.assertEqual(self.request({**batch, "frames": [{**_FRAME, "size_bytes": 8}]})[0], 409)
            self.assertEqual(self.events[1:], [{"type": "frames", **batch}])

    def test_only_extension_origins_can_make_preflight_requests(self):
        preflight = {
            "Origin": _ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization",
        }
        with self.receiver():
            status, headers, body = self.request(method="OPTIONS", headers=preflight, authenticated=False)
            self.assertEqual((status, body), (204, b""))
            self.assertEqual(headers["access-control-allow-origin"], _ORIGIN)
            self.assertEqual(headers["access-control-allow-methods"], "POST")
            for origins in ({"Origin": "null"}, {"Origin": "https://untrusted.invalid"}, {"Origin": ""}):
                status, headers, _ = self.request(
                    method="OPTIONS", headers={**preflight, **origins}, authenticated=False,
                )
                self.assertEqual(status, 403)
                self.assertNotIn("access-control-allow-origin", headers)
            disallowed_headers = {**preflight, "Access-Control-Request-Headers": "x-unknown"}
            self.assertEqual(self.request(method="OPTIONS", headers=disallowed_headers, authenticated=False)[0], 403)
            self.assertEqual(self.request(method="OPTIONS", authenticated=False)[0], 403)
            self.assertEqual(self.request(payload(_FRAME), headers={"Origin": "https://untrusted.invalid"})[0], 403)
            self.assertEqual(self.request(payload(_FRAME), headers={"Host": "rebind.invalid"})[0], 403)
            status, headers, _ = self.request(payload(_FRAME), headers={"Origin": _ORIGIN})
            self.assertEqual(status, 204)
            self.assertEqual(headers["access-control-allow-origin"], _ORIGIN)

    def test_rejections_do_not_log_or_echo_unknown_fields_or_request_paths(self):
        token_like = "private-unexpected-data"
        batch = payload(dict(_FRAME))
        raw = json.dumps(batch).replace('"size_bytes": 17', '"size_bytes": 17, "size_bytes": 18').encode("utf-8")
        logs = StringIO()
        with self.receiver(), redirect_stderr(logs):
            bad_requests = (
                (batch, {"authenticated": False}, 401),
                (batch, {"headers": {"Authorization": f"Bearer {token_like}"}}, 401),
                (batch, {"headers": {"Content-Type": "text/plain"}}, 415),
                (batch, {"path": f"/v1/frames?token={token_like}"}, 404),
                ({**batch, "raw_header": token_like}, {}, 400),
                ({**batch, "frames": [{**_FRAME, "body": token_like}]}, {}, 400),
                (raw, {}, 400),
                (token_like.encode("utf-8"), {}, 400),
            )
            for body, options, expected in bad_requests:
                with self.subTest(expected=expected):
                    status, response_headers, response_body = self.request(body, **options)
                    self.assertEqual((status, response_body), (expected, b""))
                    self.assertNotIn(token_like, str(response_headers))
            self.assertEqual(len(self.events), 1)
        self.assertEqual(logs.getvalue(), "")

    def test_early_rejection_is_sent_before_a_delayed_body_is_discarded(self):
        with patch("deep_stack.h5_live._REJECT_DRAIN_TIMEOUT", 2), self.receiver():
            body = b"rejected body must stay private"
            host = f"127.0.0.1:{self.server.server_port}"
            headers = (f"POST /rejected HTTP/1.1\r\nHost: {host}\r\n"
                       f"Content-Length: {len(body)}\r\n\r\n").encode("ascii")
            with create_connection(self.server.server_address, timeout=3) as connection:
                connection.sendall(headers)
                with HTTPResponse(connection) as response:
                    response.begin()
                    self.assertEqual((response.status, response.read()), (404, b""))
                connection.settimeout(0.05)
                with self.assertRaises(TimeoutError):
                    connection.recv(1)
                connection.settimeout(3)
                connection.sendall(body)
                self.assertEqual(connection.recv(1), b"")
            self.assertEqual(len(self.events), 1)
            self.assertEqual(self.request(payload(_FRAME))[0], 204)

    def test_invalid_metadata_and_size_limits(self):
        good = payload(_FRAME)
        invalid_frames = (
            {**_FRAME, "connection": "ws-0001"}, {**_FRAME, "connection": "private-id"},
            {**_FRAME, "offset_ms": -1}, {**_FRAME, "offset_ms": 2**53},
            {**_FRAME, "offset_ms": 1.0}, {**_FRAME, "direction": "other"},
            {**_FRAME, "opcode": True}, {**_FRAME, "opcode": 16},
            {**_FRAME, "size_bytes": -1}, {**_FRAME, "encoding": "unknown"},
        )
        with self.receiver():
            for frame in invalid_frames:
                with self.subTest(frame=frame):
                    self.assertEqual(self.request({**good, "frames": [frame]})[0], 400)
            self.assertEqual(self.request({**good, "capture_id": "external-id"})[0], 400)
            self.assertEqual(self.request({**good, "frames": []})[0], 400)
            self.assertEqual(self.request({**good, "frames": [_FRAME] * 129})[0], 400)
            self.assertEqual(self.request(headers={"Content-Length": "65537"})[0], 413)
            self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(len(self.events), 1)

    def test_body_timeout_does_not_break_a_following_batch(self):
        with patch("deep_stack.h5_live._IO_TIMEOUT", 0.1), self.receiver():
            status, _, body = self.request(b"{", headers={"Content-Length": "5"})
            self.assertEqual((status, body), (408, b""))
            self.assertEqual(self.request(payload(_FRAME))[0], 204)

    def test_dedup_cache_is_bounded(self):
        first, second, third = payload(_FRAME), payload(_FRAME), payload(_FRAME)
        with patch("deep_stack.h5_live._MAX_REMEMBERED_BATCHES", 2), self.receiver():
            for batch in (first, second, third, deepcopy(first)):
                self.assertEqual(self.request(batch)[0], 204)
            self.assertEqual([event["batch_id"] for event in self.events[1:]],
                             [item["batch_id"] for item in (first, second, third, first)])

    def test_failed_callback_retry_does_not_repeat_a_partial_side_effect(self):
        def emit(event):
            self.events.append(event)
            if event["type"] == "frames":
                raise ValueError("private callback error")

        batch = payload(_FRAME)
        with self.receiver(emit):
            self.assertEqual(self.request(batch)[::2], (500, b""))
            self.assertEqual(self.request(batch)[::2], (500, b""))
            self.assertEqual(len(self.events), 2)
