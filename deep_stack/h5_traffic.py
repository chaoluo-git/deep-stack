"""Detect the recurring exchange seen in WPK using WebSocket metadata only."""

from collections import OrderedDict
from dataclasses import dataclass, field
import re
from statistics import median


_BATCH_FRAMES = 128
_HISTORY_FRAMES = 128
_MAX_CAPTURES = 8
_MAX_CONNECTIONS = 16
_MAX_OFFLINE_FRAMES = 100_000
_MAX_OFFSET = 2**53 - 1
_CAPTURE_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)
_CONNECTION = re.compile(r"ws-[1-9][0-9]{0,8}")
_SENT_BYTES = range(81, 84)
_RECEIVED_BYTES = range(60, 64)
_PERIOD_MS = 5000
_PERIOD_TOLERANCE_MS = 100
_MAX_REPLY_MS = 250
_LATENCY_TOLERANCE_MS = 50


@dataclass
class _Connection:
    history: list[tuple[int, str, int]] = field(default_factory=list)
    reported: tuple | None = None


@dataclass
class _Capture:
    index: int
    connections: OrderedDict[str, _Connection] = field(default_factory=OrderedDict)


def _qualified_frames(frames):
    """Retain only the dimensions needed for identifying the observed cohort."""
    groups = {}
    for row in frames:
        if not isinstance(row, dict) or type(row.get("opcode")) is not int or row["opcode"] != 2:
            continue
        connection = row.get("connection")
        offset = row.get("offset_ms")
        size = row.get("size_bytes")
        direction = row.get("direction")
        if (
            not isinstance(connection, str) or not _CONNECTION.fullmatch(connection)
            or type(offset) is not int or not 0 <= offset <= _MAX_OFFSET
            or type(size) is not int
        ):
            continue
        if (direction == "sent" and size in _SENT_BYTES
                or direction == "received" and size in _RECEIVED_BYTES):
            groups.setdefault(connection, []).append((offset, direction, size))
    return groups


def _paired_replies(sent, received):
    """Pair each candidate send with at most one subsequent matched-size receipt."""
    next_reply = 0
    for send_time, _ in sent:
        while next_reply < len(received) and received[next_reply][0] < send_time:
            next_reply += 1
        if next_reply == len(received):
            break
        reply_time, reply_size = received[next_reply]
        delay = reply_time - send_time
        if delay <= _MAX_REPLY_MS:
            yield delay, reply_size
            next_reply += 1


def _pattern(history):
    sent = [(time, size) for time, direction, size in history if direction == "sent"]
    if len(sent) < 6:
        return None
    gaps = [right[0] - left[0] for left, right in zip(sent, sent[1:])]
    regular = sum(abs(gap - _PERIOD_MS) <= _PERIOD_TOLERANCE_MS for gap in gaps)
    if regular * 5 < len(gaps) * 4:
        return None

    received = [(time, size) for time, direction, size in history if direction == "received"]
    pairs = list(_paired_replies(sent, received))
    if len(pairs) < 5 or len(pairs) * 5 < len(sent) * 4:
        return None
    latencies, reply_sizes = zip(*pairs)
    typical_latency = round(median(latencies))
    consistent = sum(abs(delay - typical_latency) <= _LATENCY_TOLERANCE_MS for delay in latencies)
    if consistent * 4 < len(latencies) * 3:
        return None

    send_sizes = [size for _, size in sent]
    return {
        "period_ms": round(median(gaps)),
        "sent_bytes": [min(send_sizes), max(send_sizes)],
        "received_bytes": [min(reply_sizes), max(reply_sizes)],
        "latency_ms": {"min": min(latencies), "median": typical_latency, "max": max(latencies)},
        "samples": {"sent": len(sent), "paired": len(pairs), "regular_gaps": regular, "gaps": len(gaps)},
    }


def _signature(pattern):
    return (
        pattern["latency_ms"]["median"],
        tuple(pattern["sent_bytes"]),
        tuple(pattern["received_bytes"]),
    )


def _changed(previous, current):
    # A new frame or the observed one-byte size jitter is expected. Report a
    # sustained latency shift or a change to a separated message-size family.
    if abs(previous[0] - current[0]) >= 50:
        return True
    return any(
        max(right[0] - left[1], left[0] - right[1]) >= 2
        for left, right in zip(previous[1:], current[1:])
    )


def _event(capture_index, connection, pattern, change):
    seconds = f"{pattern['period_ms'] / 1000:.1f}".rstrip("0").rstrip(".")
    label = "周期性往返有变化" if change == "changed" else "周期性往返"
    return {
        "type": "periodic_exchange",
        "change": change,
        "capture_index": capture_index,
        "connection": connection,
        "message": f"推测{label}｜{connection}｜约 {seconds} 秒（可能是保活或轮询）",
        **pattern,
    }


class TrafficAnalyzer:
    """Track a small rolling window per anonymous connection for live reports.

    The caller supplies summaries from the local receiver. Only the observed
    WPK binary 5-second cohort is eligible. Timing supports a periodic exchange
    but gives no application message name or business meaning.
    """

    def __init__(self):
        self._captures = OrderedDict()
        self._next_capture = 1

    def update(self, event):
        """Return newly detected or materially changed patterns for this batch."""
        if not isinstance(event, dict) or event.get("type") != "frames":
            return []
        capture_id = event.get("capture_id")
        frames = event.get("frames")
        if (not isinstance(capture_id, str) or not _CAPTURE_ID.fullmatch(capture_id)
                or not isinstance(frames, list) or len(frames) > _BATCH_FRAMES):
            return []
        return self._update(capture_id.lower(), frames)

    def _update(self, capture_id, frames):
        groups = _qualified_frames(frames)
        if not groups:
            return []
        capture = self._captures.get(capture_id)
        if capture is None:
            capture = self._captures[capture_id] = _Capture(self._next_capture)
            self._next_capture += 1
            if len(self._captures) > _MAX_CAPTURES:
                self._captures.popitem(last=False)
        self._captures.move_to_end(capture_id)

        output = []
        for connection, samples in groups.items():
            state = capture.connections.get(connection)
            if state is None:
                state = capture.connections[connection] = _Connection()
                if len(capture.connections) > _MAX_CONNECTIONS:
                    capture.connections.popitem(last=False)
            capture.connections.move_to_end(connection)
            state.history = sorted(set(state.history).union(samples))[-_HISTORY_FRAMES:]
            pattern = _pattern(state.history)
            if pattern is None:
                continue
            previous = state.reported
            signature = _signature(pattern)
            if previous is None or _changed(previous, signature):
                state.reported = signature
                output.append(_event(capture.index, connection, pattern, "detected" if previous is None else "changed"))
        return output


def summarize_traffic(frames):
    """Return patterns from the latest bounded sample of each offline stream."""
    if not isinstance(frames, list) or len(frames) > _MAX_OFFLINE_FRAMES:
        raise ValueError("流量摘要需要最多 100000 条匿名消息记录")
    analyzer = TrafficAnalyzer()
    offline = "offline"
    for start in range(0, len(frames), _BATCH_FRAMES):
        analyzer._update(offline, frames[start:start + _BATCH_FRAMES])
    capture = analyzer._captures.get(offline)
    if capture is None:
        return []
    return [
        _event(capture.index, connection, pattern, "detected")
        for connection, state in sorted(capture.connections.items(), key=lambda item: int(item[0][3:]))
        if (pattern := _pattern(state.history)) is not None
    ]
