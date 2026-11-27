"""Receive authenticated H5 WebSocket metadata over a local HTTP socket."""

from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
from time import monotonic
from uuid import UUID


_HOST = "127.0.0.1"
_ROUTE = "/v1/frames"
_MAX_BATCH_BYTES = 64 * 1024
_MAX_FRAMES = 128
_MAX_REMEMBERED_BATCHES = 4096
_IO_TIMEOUT = 3
_MAX_SAFE_INTEGER = 2**53 - 1
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}")
_CONNECTION = re.compile(r"ws-[1-9][0-9]{0,8}")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,10}")
_BATCH_FIELDS = {"capture_id", "batch_id", "frames"}
_FRAME_FIELDS = {"connection", "offset_ms", "direction", "opcode", "size_bytes", "encoding"}
_ENCODINGS = ("json", "utf8", "binary", "gzip", "control")
_CORS_HEADERS = {"authorization", "content-type"}


def _without_duplicate_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid batch identity")
    standard = str(UUID(value))
    if value.lower() != standard:
        raise ValueError("invalid batch identity")
    return standard


def _bounded_int(value, limit):
    return type(value) is int and 0 <= value <= limit


def _batch(body):
    payload = json.loads(body.decode("utf-8"), object_pairs_hook=_without_duplicate_keys)
    if not isinstance(payload, dict) or payload.keys() != _BATCH_FIELDS:
        raise ValueError("invalid batch fields")
    frames = payload["frames"]
    if not isinstance(frames, list) or not 1 <= len(frames) <= _MAX_FRAMES:
        raise ValueError("invalid frame count")
    for frame in frames:
        if not isinstance(frame, dict) or frame.keys() != _FRAME_FIELDS:
            raise ValueError("invalid frame fields")
        connection = frame["connection"]
        if not (
            isinstance(connection, str) and _CONNECTION.fullmatch(connection)
            and _bounded_int(frame["offset_ms"], _MAX_SAFE_INTEGER)
            and frame["direction"] in ("received", "sent")
            and _bounded_int(frame["opcode"], 15)
            and _bounded_int(frame["size_bytes"], _MAX_SAFE_INTEGER)
            and frame["encoding"] in _ENCODINGS
        ):
            raise ValueError("invalid frame values")
    return _uuid(payload["capture_id"]), _uuid(payload["batch_id"]), frames


class _Receiver(HTTPServer):
    def __init__(self, port, token, emit):
        self.authorization = f"Bearer {token}".encode("ascii")
        self.emit = emit
        self.batches = OrderedDict()
        super().__init__((_HOST, port), _Handler)

    def accept(self, capture_id, batch_id, frames):
        identity = (capture_id, batch_id)
        fingerprint = sha256(json.dumps(frames, sort_keys=True, separators=(",", ":")).encode()).digest()
        previous = self.batches.get(identity)
        if previous is not None:
            self.batches.move_to_end(identity)
            old_fingerprint, status = previous
            return status if compare_digest(old_fingerprint, fingerprint) else 409
        try:
            self.emit({"type": "frames", "capture_id": capture_id, "batch_id": batch_id, "frames": frames})
        except Exception:
            # A callback can fail after an observable side effect. Retain the
            # failed outcome for an identical retry instead of calling it twice.
            status = 500
        else:
            status = 204
        self.batches[identity] = (fingerprint, status)
        if len(self.batches) > _MAX_REMEMBERED_BATCHES:
            self.batches.popitem(last=False)
        return status


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(_IO_TIMEOUT)
        self.origin = None
        super().setup()

    def log_message(self, *_args):
        # The standard logger includes the request target and can echo secrets.
        pass

    def send_error(self, code, message=None, explain=None):
        self._respond(code)

    def _respond(self, status, *, preflight=False):
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Connection", "close")
        if status != 204:
            self.send_header("Content-Length", "0")
        if self.origin is not None:
            self.send_header("Access-Control-Allow-Origin", self.origin)
            self.send_header("Vary", "Origin")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        try:
            self.end_headers()
        except OSError:
            pass  # The peer may have closed before reading the response.

    def _check_endpoint(self):
        if self.path != _ROUTE:
            self._respond(404)
            return False
        port = self.server.server_port
        hosts = self.headers.get_all("Host", [])
        if hosts != [f"{_HOST}:{port}"] and not (port == 80 and hosts == [_HOST]):
            self._respond(403)
            return False
        origins = self.headers.get_all("Origin", [])
        if not origins:
            return True
        if len(origins) != 1 or not _EXTENSION_ORIGIN.fullmatch(origins[0]):
            self._respond(403)
            return False
        self.origin = origins[0]
        return True

    def do_OPTIONS(self):
        if not self._check_endpoint():
            return
        methods = self.headers.get_all("Access-Control-Request-Method", [])
        requested_headers = {
            item.strip().lower()
            for header in self.headers.get_all("Access-Control-Request-Headers", [])
            for item in header.split(",")
        }
        if self.origin is None or methods != ["POST"] or requested_headers - _CORS_HEADERS:
            self._respond(403)
            return
        self._respond(204, preflight=True)

    def do_POST(self):
        if not self._check_endpoint():
            return
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not compare_digest(auth[0].encode("utf-8"), self.server.authorization):
            self._respond(401)
            return
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._respond(415)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not _CONTENT_LENGTH.fullmatch(lengths[0]):
            self._respond(400)
            return
        length = int(lengths[0])
        if length > _MAX_BATCH_BYTES:
            self._respond(413)
            return
        try:
            capture_id, batch_id, frames = _batch(self._read_body(length))
        except TimeoutError:
            self._respond(408)
            return
        except OSError:
            self._respond(400)
            return
        except (ValueError, RecursionError):
            self._respond(400)
            return
        self._respond(self.server.accept(capture_id, batch_id, frames))

    def _read_body(self, length):
        deadline = monotonic() + _IO_TIMEOUT
        body = bytearray()
        while len(body) < length:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(length - len(body))
            if not chunk:
                raise ValueError("incomplete body")
            body.extend(chunk)
        return body


def _same_file(path, created):
    current = path.lstat()
    return (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)


@contextmanager
def _pairing(path, url, token):
    data = (json.dumps({"version": 1, "url": url, "token": token}) + "\n").encode("utf-8")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise ValueError(f"配对文件已存在，请换用其他路径「{path}」") from None
    created = None
    complete = False
    try:
        with os.fdopen(descriptor, "wb") as file:
            created = os.fstat(file.fileno())
            file.write(data)
        complete = True
        yield
    finally:
        try:
            if created is not None and _same_file(path, created):
                with path.open("rb") as file:
                    unchanged = file.read(len(data) + 1) == data
                if (unchanged or not complete) and _same_file(path, created):
                    path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def live_server(port, pairing_file, emit):
    """Bind, pair, announce, and yield the HTTP server without starting its loop."""
    if not _bounded_int(port, 65535):
        raise ValueError("监听端口必须是 0 到 65535 之间的整数")
    path = Path(pairing_file).expanduser().absolute()
    token = token_urlsafe(32)
    with _Receiver(port, token, emit) as server:
        url = f"http://{_HOST}:{server.server_port}{_ROUTE}"
        with _pairing(path, url, token):
            emit({"type": "listening", "url": url, "pairing_file": str(path)})
            yield server


def serve_live(port, pairing_file, emit):
    """Run the localhost receiver until interrupted by the caller."""
    with live_server(port, pairing_file, emit) as server:
        server.serve_forever()
