"""Inspect a local Chrome capture without exporting its message contents."""

from base64 import b64decode
from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path
from stat import S_ISREG


_MAX_TRACE_BYTES = 32 * 1024 * 1024
_MAX_MESSAGES = 100_000
_ENCODINGS = ("json", "utf8", "binary", "gzip", "control")
_HAR_DIRECTIONS = {"send": "sent", "receive": "received"}
_CDP_DIRECTIONS = {
    "Network.webSocketFrameSent": "sent",
    "Network.webSocketFrameReceived": "received",
}
_CDP_ERROR = "Network.webSocketFrameError"
_EXPORT_HINT = (
    "请在 Chrome 开发者工具的 Network 面板导出 HAR (sanitized)，"
    "或保存 Protocol Monitor JSON，再传入导出文件的实际路径。"
)


@dataclass(frozen=True)
class _Frame:
    stream: tuple
    time: float
    direction: str
    opcode: int
    size_bytes: int
    encoding: str


@dataclass(frozen=True)
class BinaryPacket:
    stream: tuple
    timestamp: float
    direction: str
    body: bytes


def _reject_constant(_value):
    raise ValueError("JSON 只能使用有限数值")


def _read_document(path):
    path = Path(path)
    try:
        path = path.expanduser().absolute()
    except (OSError, RuntimeError):
        raise ValueError(f"无法解析捕获文件路径「{path}」。{_EXPORT_HINT}") from None
    invalid_path = f"捕获文件路径「{path}」必须指向普通文件。{_EXPORT_HINT}"
    try:
        data = None
        if S_ISREG(path.stat().st_mode):
            with path.open("rb") as source:
                data = source.read(_MAX_TRACE_BYTES + 1)
    except FileNotFoundError:
        raise ValueError(f"找不到本地 Chrome 捕获文件「{path}」。{_EXPORT_HINT}") from None
    except (IsADirectoryError, NotADirectoryError, ValueError):
        raise ValueError(invalid_path) from None
    except OSError:
        raise ValueError(f"无法读取本地 Chrome 捕获文件「{path}」。请检查文件读取权限。{_EXPORT_HINT}") from None
    if data is None:
        raise ValueError(invalid_path)
    if len(data) > _MAX_TRACE_BYTES:
        raise ValueError("捕获文件超过 32 MiB")
    try:
        return json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ValueError("捕获文件必须是有效的 UTF-8 JSON") from None


def _time(value):
    if type(value) not in (int, float):
        raise ValueError("WebSocket 消息时间无效")
    try:
        time = float(value)
    except OverflowError as exc:
        raise ValueError("WebSocket 消息时间无效") from exc
    if not isfinite(time) or time < 0:
        raise ValueError("WebSocket 消息时间无效")
    return time


def _contains_json(payload):
    # A text number or an ordinary short string is not useful evidence of JSON framing.
    if isinstance(payload, str):
        starts = ("{", "[")
    else:
        starts = (b"{", b"[")
    if not payload.lstrip().startswith(starts):
        return False
    try:
        return isinstance(json.loads(payload, parse_constant=_reject_constant), (dict, list))
    except (ValueError, RecursionError):
        return False


def _decode_payload(opcode, payload):
    if type(opcode) is not int or not 0 <= opcode <= 15:
        raise ValueError("WebSocket 消息操作码无效")
    if not isinstance(payload, str):
        raise ValueError("WebSocket 消息缺少有效的字符串负载")
    if opcode == 1:
        try:
            return payload.encode("utf-8")
        except UnicodeError:
            raise ValueError("WebSocket 文本负载不是有效的 UTF-8") from None
    try:
        return b64decode(payload, validate=True)
    except ValueError:
        raise ValueError("WebSocket 二进制消息的 Base64 编码无效") from None


def _frame(stream, timestamp, direction, opcode, payload):
    decoded = _decode_payload(opcode, payload)
    size = len(decoded)
    if opcode == 1:
        encoding = "json" if _contains_json(payload) else "utf8"
    else:
        if opcode >= 8:
            encoding = "control"
        elif size >= 10 and decoded.startswith(b"\x1f\x8b\x08") and not decoded[3] & 0xe0:
            # The tag recognizes a gzip header only. No payload is decompressed.
            encoding = "gzip"
        elif _contains_json(decoded):
            encoding = "json"
        else:
            encoding = "binary"
    return _Frame(stream, _time(timestamp), direction, opcode, size, encoding)


def _read_binary_packet(stream, timestamp, direction, opcode, payload):
    timestamp = _time(timestamp)
    decoded = _decode_payload(opcode, payload)
    if opcode == 2:
        return BinaryPacket(stream, timestamp, direction, decoded)
    return None


def _har_frames(document, decode):
    log = document.get("log")
    if not isinstance(log, dict) or not isinstance(log.get("entries"), list):
        raise ValueError("HAR 缺少 log.entries 列表")
    for stream, entry in enumerate(log["entries"]):
        if not isinstance(entry, dict):
            raise ValueError("HAR 请求项必须是 JSON 对象")
        if "_webSocketMessages" not in entry:
            continue
        messages = entry["_webSocketMessages"]
        if not isinstance(messages, list):
            raise ValueError("HAR WebSocket 消息列表无效")
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                raise ValueError("HAR WebSocket 消息必须包含方向")
            direction = message["type"]
            if direction == "error":
                if type(message.get("opcode")) is not int or message["opcode"] != -1:
                    raise ValueError("HAR WebSocket 错误事件的操作码无效")
                _time(message.get("time"))
                yield None
                continue
            if direction not in _HAR_DIRECTIONS:
                raise ValueError("HAR WebSocket 消息方向无效")
            yield decode(
                (stream,), message.get("time"), _HAR_DIRECTIONS[direction],
                message.get("opcode"), message.get("data"),
            )


def _cdp_frames(entries, decode):
    if not isinstance(entries, list):
        raise ValueError("CDP 捕获需要 JSON 事件列表")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("CDP 事件必须是 JSON 对象")
        method = entry.get("method")
        if method is None and type(entry.get("id")) is int:
            continue  # Raw CDP responses use a command ID rather than a method.
        if not isinstance(method, str) or not method:
            raise ValueError("CDP 事件缺少 method")
        if method != _CDP_ERROR and method not in _CDP_DIRECTIONS:
            continue
        params = entry.get("params")
        if params is None:
            params = entry.get("result")  # Protocol Monitor places notification params in result.
        if not isinstance(params, dict):
            raise ValueError("CDP WebSocket 事件缺少参数")
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("CDP WebSocket 事件缺少请求标识")
        context = (entry.get("sessionId"), entry.get("target"))
        if any(value is not None and not isinstance(value, str) for value in context):
            raise ValueError("CDP WebSocket 事件的会话标识无效")
        if method == _CDP_ERROR:
            _time(params.get("timestamp"))
            yield None
            continue
        response = params.get("response")
        if not isinstance(response, dict):
            raise ValueError("CDP WebSocket 事件缺少消息内容")
        yield decode(
            (*context, request_id), params.get("timestamp"), _CDP_DIRECTIONS[method],
            response.get("opcode"), response.get("payloadData"),
        )


def _trace_records(path, decode):
    document = _read_document(path)
    if isinstance(document, dict) and "log" in document:
        return "chrome_har", _har_frames(document, decode)
    if isinstance(document, list) or isinstance(document, dict) and "events" in document:
        return "cdp_json", _cdp_frames(document if isinstance(document, list) else document["events"], decode)
    raise ValueError("捕获文件须为 Chrome HAR 或 CDP JSON 事件列表")


def _bounded_records(records):
    for count, record in enumerate(records, start=1):
        if count > _MAX_MESSAGES:
            raise ValueError("捕获文件含超过 100000 条 WebSocket 消息或错误")
        yield record


def read_binary_packets(path):
    """Read opcode-2 packet bodies for local analysis without serializing payloads."""
    _, records = _trace_records(path, _read_binary_packet)
    yield from (packet for packet in _bounded_records(records) if packet is not None)


def inspect_trace(path):
    """Inspect a Chrome HAR, Protocol Monitor export, or array of raw CDP events.

    Connections count streams with retained messages. Chromium error events
    are counted separately. Payload tags are observations about encoding
    only and do not identify the application's message schema.
    """
    source_format, records = _trace_records(path, _frame)
    frames = []
    errors = 0
    for record in _bounded_records(records):
        if record is None:
            errors += 1
        else:
            frames.append(record)
    frames.sort(key=lambda item: item.time)

    connections = {}
    encodings = dict.fromkeys(_ENCODINGS, 0)
    messages = []
    start = frames[0].time if frames else 0
    for record in frames:
        offset_ms = (record.time - start) * 1000
        if not isfinite(offset_ms):
            raise ValueError("WebSocket 消息时间跨度无效")
        connection = connections.setdefault(record.stream, f"ws-{len(connections) + 1}")
        messages.append({
            "connection": connection,
            "offset_ms": round(offset_ms),
            "direction": record.direction,
            "opcode": record.opcode,
            "size_bytes": record.size_bytes,
        })
        encodings[record.encoding] += 1
    return {
        "format": "deep-stack-h5-trace-v1",
        "source_format": source_format,
        "connection_count": len(connections),
        "frame_count": len(messages),
        "reported_errors": errors,
        "payload_encodings": encodings,
        "frames": messages,
    }
