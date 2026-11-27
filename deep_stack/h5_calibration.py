"""Record time-aligned, anonymous H5 and public screen evidence."""

from hmac import digest as hmac_digest
import json
from math import isfinite
import os
from pathlib import Path
from secrets import token_bytes
from threading import Lock
from time import monotonic

from .ev import RANKS, SUITS
from .h5_live import _CONNECTION, _ENCODINGS, _MAX_FRAMES, _MAX_SAFE_INTEGER, _bounded_int, _uuid
from .layout import EXTRA_SEAT_IDS, SEAT_IDS
from .live_actions import _ACTION_LABELS
from .observations import STREETS
from .seat_positions import _EARLY_TO_LATE, _ROLE_LABELS


_MAX_LINE_BYTES = 32 * 1024
_CARD_COUNTS = {street: count for count, street in STREETS.items()}
_SEATS = SEAT_IDS + EXTRA_SEAT_IDS
_POSITIONS = (*_ROLE_LABELS, "BTN/SB", *(position for group in _EARLY_TO_LATE.values() for position in group))


def _instant(value):
    if type(value) not in (int, float):
        raise ValueError("校准时间必须是有限数值")
    try:
        stamp = float(value)
    except OverflowError:
        raise ValueError("校准时间必须是有限数值") from None
    if not isfinite(stamp):
        raise ValueError("校准时间必须是有限数值")
    return stamp


def _relative_ms(start, stamp):
    delta_ms = (stamp - start) * 1000
    if not isfinite(delta_ms) or not 0 <= delta_ms <= _MAX_SAFE_INTEGER:
        raise ValueError("校准时间超出本次记录范围")
    return round(delta_ms)


def _wire_frame(frame):
    if not isinstance(frame, dict):
        raise ValueError("H5 校准帧格式无效")
    connection = frame.get("connection")
    offset_ms = frame.get("offset_ms")
    direction = frame.get("direction")
    opcode = frame.get("opcode")
    size_bytes = frame.get("size_bytes")
    encoding = frame.get("encoding")
    if not (
        isinstance(connection, str) and _CONNECTION.fullmatch(connection)
        and _bounded_int(offset_ms, _MAX_SAFE_INTEGER)
        and direction in ("received", "sent")
        and _bounded_int(opcode, 15)
        and _bounded_int(size_bytes, _MAX_SAFE_INTEGER)
        and encoding in _ENCODINGS
    ):
        raise ValueError("H5 校准帧字段无效")
    return {
        "connection": connection, "offset_ms": offset_ms, "direction": direction,
        "opcode": opcode, "size_bytes": size_bytes, "encoding": encoding,
    }


def _action_fields(event, hand_id):
    seat = event.get("seat")
    action = event.get("action")
    wager = event.get("wager")
    confirmed = event.get("confirmed_hand")
    if seat not in _SEATS or not isinstance(action, str) or action not in _ACTION_LABELS:
        raise ValueError("屏幕动作的座位或类型无效")
    if wager is not None and (type(wager) not in (int, float) or not 0 <= wager <= 1e12):
        raise ValueError("屏幕动作的金额无效")
    if type(confirmed) is not bool or confirmed != (hand_id is not None):
        raise ValueError("屏幕动作的手牌确认状态无效")
    result = {"seat": seat, "action": action, "wager": wager, "confirmed_hand": confirmed}
    position = event.get("position")
    if position is not None:
        if position not in _POSITIONS:
            raise ValueError("屏幕动作的位置无效")
        result["position"] = position
    if "wager_update" in event:
        if type(event["wager_update"]) is not bool:
            raise ValueError("屏幕动作的金额补读状态无效")
        result["wager_update"] = event["wager_update"]
    return result


def _board_fields(event, street):
    raw = event.get("cards")
    complete = event.get("complete")
    if not isinstance(raw, (list, tuple)) or len(raw) > 5 or type(complete) is not bool:
        raise ValueError("屏幕公共牌的格式无效")
    cards = list(raw)
    known = [card for card in cards if card is not None]
    if any(not isinstance(card, str) or len(card) != 2 or card[0] not in RANKS or card[1] not in SUITS
           for card in known):
        raise ValueError("屏幕公共牌的牌面无效")
    if len(set(known)) != len(known):
        raise ValueError("屏幕公共牌包含重复牌面")
    if ((street is not None and len(cards) != _CARD_COUNTS[street])
            or complete and (street is None or len(known) != len(cards))):
        raise ValueError("屏幕公共牌与街次或完成状态不一致")
    return {"cards": cards, "complete": complete}


class CalibrationRecorder:
    """Write an exclusive, append-only JSON lines journal on a monotonic timebase."""

    def __init__(self, path, clock=monotonic):
        self.path = Path(path).expanduser().absolute()
        self._clock = clock
        self._lock = Lock()
        self._file = None

    def __enter__(self):
        with self._lock:
            if self._file is not None:
                raise RuntimeError("校准记录已开启")
            self._start = _instant(self._clock())
            self._hand_key = token_bytes(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_BINARY", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                raise ValueError(f"校准记录文件已存在，请换用其他路径「{self.path}」") from None
            try:
                self._file = os.fdopen(descriptor, "ab", buffering=0)
            except BaseException:
                os.close(descriptor)
                raise
            try:
                self._append({"type": "calibration_start", "version": 1})
            except BaseException:
                self._close()
                raise
        return self

    def __exit__(self, *_exc_info):
        self.close()

    def close(self):
        """Close the active journal once while allowing concurrent producers to finish."""
        with self._lock:
            self._close()

    def _close(self):
        file, self._file = self._file, None
        if file is not None:
            file.close()

    def _require_open(self):
        if self._file is None:
            raise RuntimeError("校准记录未开启或已经关闭")

    def _append(self, record):
        data = (json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > _MAX_LINE_BYTES:
            raise ValueError("单条校准记录超过 32 KiB")
        try:
            if self._file.write(data) != len(data):
                raise OSError("校准记录写入不完整")
        except OSError:
            self._close()
            raise

    def _anonymous_hand(self, hand_id):
        if hand_id is None:
            return None
        if not isinstance(hand_id, str) or not 1 <= len(hand_id) <= 64:
            raise ValueError("屏幕手牌标识无效")
        try:
            raw = hand_id.encode("utf-8")
        except UnicodeError:
            raise ValueError("屏幕手牌标识无效") from None
        return hmac_digest(self._hand_key, raw, "sha256").hex()[:24]

    def h5(self, event):
        """Record the arrival time and an allowlisted copy of an authenticated batch."""
        with self._lock:
            self._require_open()
            arrival_ms = _relative_ms(self._start, _instant(self._clock()))
            if not isinstance(event, dict) or event.get("type") != "frames":
                raise ValueError("校准记录需要 H5 帧批次")
            frames = event.get("frames")
            if not isinstance(frames, list) or not 1 <= len(frames) <= _MAX_FRAMES:
                raise ValueError("H5 校准帧数量无效")
            self._append({
                "type": "h5_batch", "arrival_ms": arrival_ms,
                "capture_id": _uuid(event.get("capture_id")), "batch_id": _uuid(event.get("batch_id")),
                "frames": [_wire_frame(frame) for frame in frames],
            })

    def screen(self, event, captured_at=None, *, sample_started_at=None):
        """Record an ActionLog or BoardLog event at the screenshot capture time."""
        with self._lock:
            self._require_open()
            captured = _instant(self._clock() if captured_at is None else captured_at)
            at_ms = _relative_ms(self._start, captured)
            if not isinstance(event, dict) or event.get("type") not in ("action", "board"):
                raise ValueError("校准记录需要屏幕动作或公共牌事件")
            street = event.get("street")
            if street is not None and street not in STREETS.values():
                raise ValueError("屏幕事件的街次无效")
            hand_id = self._anonymous_hand(event.get("hand_id"))
            record = {"type": f"screen_{event['type']}", "at_ms": at_ms, "hand_id": hand_id, "street": street}
            if sample_started_at is not None:
                started = _instant(sample_started_at)
                if started > captured:
                    raise ValueError("截图采样开始时间晚于完成时间")
                record["sample_started_ms"] = _relative_ms(self._start, started)
            fields = _action_fields(event, hand_id) if event["type"] == "action" else _board_fields(event, street)
            self._append({**record, **fields})
