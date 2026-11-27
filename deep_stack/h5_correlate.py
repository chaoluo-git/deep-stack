"""Associate anonymous calibration observations by local receipt proximity."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import accumulate
import json
from pathlib import Path
import re
from stat import S_ISREG

from .h5_calibration import _action_fields, _board_fields
from .h5_live import _MAX_SAFE_INTEGER, _batch, _bounded_int, _without_duplicate_keys
from .observations import STREETS


_HEADER = {"type": "calibration_start", "version": 1}
_SCREEN_ACTION = {"type", "at_ms", "hand_id", "street", "seat", "action", "wager", "confirmed_hand"}
_SCREEN_BOARD = {"type", "at_ms", "hand_id", "street", "cards", "complete"}
_SAMPLE_START = {"sample_started_ms"}
_CAPTURE_BATCH = {"type", "arrival_ms", "capture_id", "batch_id", "frames"}
_HAND = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_BYTES = 32 * 1024 * 1024
_MAX_LINE = 96 * 1024
_MAX_FRAMES = 100_000
_MAX_MARKERS = 20_000
_MAX_LISTED_PER_MARKER = 32
_MAX_LISTED_TOTAL = 8192
_PARTIAL_LINE = object()


@dataclass(frozen=True)
class _Batch:
    index: int
    capture: int
    arrival: int
    offsets_max: int
    frames: list[dict]


def _timestamp(value):
    return _bounded_int(value, _MAX_SAFE_INTEGER)


def _reject_constant(_value):
    raise ValueError("JSON number is not finite")


def _records(path):
    try:
        path = Path(path).expanduser()
    except RuntimeError:
        raise ValueError("无法解析校准日志的本地路径") from None
    if not S_ISREG(path.stat().st_mode):
        raise ValueError("时间线需要本地 JSONL 普通文件")
    consumed = 0
    with path.open("rb") as file:
        line_number = 0
        while raw := file.readline(_MAX_LINE + 1):
            line_number += 1
            consumed += len(raw)
            if consumed > _MAX_BYTES:
                raise ValueError("校准日志超过 32 MiB")
            if len(raw) > _MAX_LINE:
                raise ValueError(f"校准日志第 {line_number} 行过长")
            try:
                text = raw.decode("utf-8-sig" if line_number == 1 else "utf-8").strip()
                record = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_without_duplicate_keys)
            except json.JSONDecodeError as error:
                if (line_number > 1 and not raw.endswith(b"\n") and text.startswith("{")
                        and (error.pos == len(text) or error.msg.startswith("Unterminated string"))):
                    yield line_number, _PARTIAL_LINE
                    return
                raise ValueError(f"校准日志第 {line_number} 行不是有效 JSON") from None
            except (ValueError, RecursionError):
                raise ValueError(f"校准日志第 {line_number} 行不是有效 UTF-8 JSON") from None
            yield line_number, record


def _marker(record, hands):
    kind = record["type"]
    at = record.get("at_ms")
    started = record.get("sample_started_ms", at)
    hand = record.get("hand_id")
    street = record.get("street")
    if (not _timestamp(at) or not _timestamp(started) or started > at
            or (hand is not None and (not isinstance(hand, str) or not _HAND.fullmatch(hand)))):
        raise ValueError("invalid marker time or hand alias")
    if street is not None and street not in STREETS.values():
        raise ValueError("invalid marker street")
    marker = {"type": kind, "at_ms": at, "sample_started_ms": started, "hand_index": None, "street": street}
    if kind == "screen_action":
        if not _SCREEN_ACTION <= record.keys() <= _SCREEN_ACTION | _SAMPLE_START | {"position", "wager_update"}:
            raise ValueError("invalid marker fields")
        marker.update(_action_fields(record, hand))
    elif kind == "screen_board":
        if not _SCREEN_BOARD <= record.keys() <= _SCREEN_BOARD | _SAMPLE_START:
            raise ValueError("invalid board marker fields")
        marker.update(_board_fields(record, street))
    else:
        raise ValueError("unsupported marker")
    if hand is not None:
        marker["hand_index"] = hands.setdefault(hand, len(hands) + 1)
    return marker


def _read_timeline(path):
    records = _records(path)
    header = next(records, (0, None))[1]
    if not isinstance(header, dict) or header != _HEADER or type(header["version"]) is not int:
        raise ValueError("校准日志开头需要第 1 版 calibration_start")
    captures, hands, batches, markers = {}, {}, {}, []
    frame_count = duplicate_count = 0
    truncated = False
    for line, record in records:
        if record is _PARTIAL_LINE:
            truncated = True
            break
        try:
            if not isinstance(record, dict):
                raise ValueError("invalid journal row")
            if record.get("type") == "h5_batch":
                if record.keys() != _CAPTURE_BATCH or not _timestamp(record["arrival_ms"]):
                    raise ValueError("invalid batch timestamp or shape")
                payload = {key: record[key] for key in ("capture_id", "batch_id", "frames")}
                capture_id, batch_id, frames = _batch(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                identity = (capture_id, batch_id)
                if identity in batches:
                    if batches[identity].frames != frames:
                        raise ValueError("conflicting retransmission")
                    duplicate_count += 1
                    continue
                capture = captures.setdefault(capture_id, len(captures) + 1)
                batches[identity] = _Batch(len(batches) + 1, capture, record["arrival_ms"],
                                           max(frame["offset_ms"] for frame in frames), frames)
                frame_count += len(frames)
            else:
                markers.append(_marker(record, hands))
        except (ValueError, KeyError, TypeError, RecursionError):
            raise ValueError(f"校准日志第 {line} 行的记录字段或数值无效") from None
        if frame_count > _MAX_FRAMES or len(markers) > _MAX_MARKERS:
            raise ValueError("校准日志最多支持 100000 个网络帧和 20000 个画面标记")
    source = {"batch_count": len(batches), "frame_count": frame_count, "capture_count": len(captures),
              "duplicate_batch_count": duplicate_count, "ignored_partial_final_line": truncated}
    return sorted(batches.values(), key=lambda item: (item.arrival, item.index)), markers, source


def _nearest_batches(batches, start, stop, middle, sampled_start, sampled_end):
    before, after = middle - 1, middle
    while before >= start or after < stop:
        if before >= start and (after >= stop or
                sampled_start - batches[before].arrival <= max(0, batches[after].arrival - sampled_end)):
            yield batches[before]
            before -= 1
        else:
            yield batches[after]
            after += 1


def _clock_limits(batches):
    limits = {}
    for batch in batches:
        # One millisecond covers the independently rounded browser and receiver values.
        limit = batch.arrival - batch.offsets_max + 1
        limits[batch.capture] = min(limits.get(batch.capture, limit), limit)
    return limits


def _candidate(batch, frame, index, at, limit):
    anchor = batch.arrival - batch.offsets_max + frame["offset_ms"]
    return {
        "capture_index": batch.capture, "batch_index": batch.index, "frame_index": index, **frame,
        "batch_arrival_ms": batch.arrival, "arrival_delta_ms": batch.arrival - at,
        "batch_offset_estimate_ms": anchor, "batch_offset_estimate_delta_ms": anchor - at,
        "feasible_at_ms": {"earliest": None, "latest": batch.arrival},
        "fixed_offset_latest_at_ms": min(batch.arrival, frame["offset_ms"] + limit),
    }


def correlate_timeline(path, window_ms=1500):
    """Report time-window candidates from a version-one anonymous JSONL journal.

    The screen timestamp is when visible evidence was sampled. Candidates are
    indexed only by whole-batch receipt. Browser elapsed offsets show a relative
    ordering and can supply a conditional upper bound, but never elect a match.
    """
    if type(window_ms) is not int or not 0 <= window_ms <= 60_000:
        raise ValueError("时间窗口必须是 0 到 60000 之间的整数毫秒")
    batches, markers, source = _read_timeline(path)
    times = [batch.arrival for batch in batches]
    prefix = list(accumulate((len(batch.frames) for batch in batches), initial=0))
    limits = _clock_limits(batches)
    summary = {kind: dict.fromkeys(("marker_count", "none", "single", "multiple"), 0)
               for kind in ("screen_action", "screen_board")}
    output = []
    listed_total = 0
    for marker in sorted(markers, key=lambda item: item["at_ms"]):
        started, at = marker["sample_started_ms"], marker["at_ms"]
        start, stop = bisect_left(times, started - window_ms), bisect_right(times, at + window_ms)
        count = prefix[stop] - prefix[start]
        status = "none" if count == 0 else "single" if count == 1 else "multiple"
        totals = summary[marker["type"]]
        totals["marker_count"] += 1
        totals[status] += 1
        allowance = min(_MAX_LISTED_PER_MARKER, _MAX_LISTED_TOTAL - listed_total)
        candidates = []
        for batch in _nearest_batches(batches, start, stop, bisect_left(times, started), started, at):
            if len(candidates) >= allowance:
                break
            frames = batch.frames[:allowance - len(candidates)]
            candidates.extend(_candidate(batch, frame, index, at, limits[batch.capture])
                              for index, frame in enumerate(frames, start=1))
        listed_total += len(candidates)
        output.append({**marker, "candidate_status": status, "candidate_batch_count": stop - start,
                       "candidate_frame_count": count, "omitted_candidate_frame_count": count - len(candidates),
                       "candidates": candidates})
    return {
        "format": "deep-stack-h5-correlation-v1",
        "method": {
            "association": "batch_arrival_proximity", "window_ms": window_ms,
            "receipt_window": "inclusive_from_screen_capture_start_minus_window_to_end_plus_window",
            "screen_timing": "local_screenshot_acquisition_bounds_not_game_event_time",
            "batch_offset_estimate": "anchor_largest_browser_offset_to_batch_arrival",
            "fixed_offset_bound": "conditional_same_rate_and_constant_shift_with_one_ms_rounding",
            "earlier_frame_bound": "unknown", "application_message_mapping": None,
        },
        "source": source,
        "capture_timing": [{"capture_index": capture, "fixed_clock_shift_ms": {"earliest": None, "latest": limit}}
                           for capture, limit in sorted(limits.items())],
        "summary": summary,
        "markers": output,
    }
