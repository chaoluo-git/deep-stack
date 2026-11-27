"""Validate and track snapshots of the public H5 table display."""

from collections import Counter
from dataclasses import dataclass, field
import re
from threading import Lock
from uuid import UUID

from .ev import KINDS, RANKS, SUITS


_MAX_SAFE_INTEGER = 2**53 - 1
MAX_PUBLIC_SNAPSHOTS = 32
PUBLIC_SOURCE = "h5_client_display"
_MAX_CAPTURES = 128
_REF = re.compile(r"[0-9]{1,24}")
_FIELDS = {"offset_ms", "table_ref", "hand_ref", "street", "board", "seats"}
_SEAT_FIELDS = {"slot", "player_id", "action", "wager", "stack"}
_TABLE_AMOUNTS = ("small_blind", "big_blind", "ante", "third_blind")
_TABLE_DETAILS_FIELDS = {*_TABLE_AMOUNTS, "dealer_slot"}
_TABLE_KEYS = ("table_ref", "hand_ref", "street", "table_details")
_BOARD_COUNTS = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
_STREETS = (*_BOARD_COUNTS, "settled")
_STREET_ORDER = {street: index for index, street in enumerate(_STREETS)}
_TERMINAL = {"fold", "all_in"}


def _valid_ref(value):
    return value is None or (
        isinstance(value, str) and _REF.fullmatch(value) is not None and value.strip("0") != ""
    )


def _valid_chips(value):
    return value is None or (type(value) in (int, float) and 0 <= value <= 1e12)


def _table_details(details, slots):
    if not isinstance(details, dict) or details.keys() != _TABLE_DETAILS_FIELDS:
        raise ValueError("公开牌桌桌况字段无效")
    amounts = {name: details[name] for name in _TABLE_AMOUNTS}
    if any(not _valid_chips(value) or value == 0 for value in amounts.values()):
        raise ValueError("公开牌桌盲注或前注金额无效")
    small, big = amounts["small_blind"], amounts["big_blind"]
    if (small is None) != (big is None) or (small is not None and small > big):
        raise ValueError("公开牌桌大小盲注不一致")
    third = amounts["third_blind"]
    if third is not None and (big is None or third <= big):
        raise ValueError("公开牌桌第三盲注无效")
    dealer = details["dealer_slot"]
    if dealer is not None and (type(dealer) is not int or not 1 <= dealer <= 9):
        raise ValueError("公开牌桌庄位编号无效")
    return {**amounts, "dealer_slot": dealer if dealer in slots else None}


def validate_snapshot(snapshot):
    """Return an independent allowlisted snapshot, or raise ValueError."""
    if not isinstance(snapshot, dict) or not _FIELDS <= snapshot.keys() <= _FIELDS | {"table_details"}:
        raise ValueError("公开牌桌快照字段无效")
    offset = snapshot["offset_ms"]
    if type(offset) is not int or not 0 <= offset <= _MAX_SAFE_INTEGER:
        raise ValueError("公开牌桌快照时间无效")
    if not _valid_ref(snapshot["table_ref"]) or not _valid_ref(snapshot["hand_ref"]):
        raise ValueError("公开牌桌标识无效")
    if snapshot["street"] is not None and snapshot["street"] not in _STREETS:
        raise ValueError("公开牌桌街次无效")
    board = snapshot["board"]
    if (
        not isinstance(board, list) or len(board) > 5
        or any(not isinstance(card, str) or len(card) != 2 or card[0] not in RANKS or card[1] not in SUITS
               for card in board)
        or len(set(board)) != len(board)
    ):
        raise ValueError("公开牌桌公共牌无效")

    seats = snapshot["seats"]
    if not isinstance(seats, list) or len(seats) > 9:
        raise ValueError("公开牌桌座位数无效")
    slots = set()
    for seat in seats:
        if not isinstance(seat, dict) or seat.keys() != _SEAT_FIELDS:
            raise ValueError("公开牌桌座位字段无效")
        slot = seat["slot"]
        action = seat["action"]
        if type(slot) is not int or not 1 <= slot <= 9 or slot in slots:
            raise ValueError("公开牌桌座位编号无效")
        if not _valid_ref(seat["player_id"]):
            raise ValueError("公开牌桌玩家标识无效")
        if action is not None and (not isinstance(action, str) or action not in KINDS):
            raise ValueError("公开牌桌动作无效")
        if not _valid_chips(seat["wager"]) or not _valid_chips(seat["stack"]):
            raise ValueError("公开牌桌筹码读数无效")
        slots.add(slot)

    validated = {
        **snapshot, "board": list(board),
        "seats": [dict(seat) for seat in sorted(seats, key=lambda seat: seat["slot"])],
    }
    if "table_details" in snapshot:
        validated["table_details"] = _table_details(snapshot["table_details"], slots)
    return validated


def _capture_ref(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("公开牌桌采集编号无效")
    try:
        result = str(UUID(value))
    except ValueError:
        raise ValueError("公开牌桌采集编号无效") from None
    if value.lower() != result:
        raise ValueError("公开牌桌采集编号无效")
    return result


def _public_seats(snapshot):
    if snapshot is None:
        return {}
    seats = snapshot["seats"]
    counts = Counter(seat["player_id"] for seat in seats)
    return {
        seat["slot"]: {**seat, "player_id": seat["player_id"] if counts[seat["player_id"]] == 1 else None}
        for seat in seats
    }


@dataclass
class _SeatAction:
    player_id: str | None = None
    label: str | None = None
    blocked_label: str | None = None
    terminal: str | None = None
    amounts: dict = field(default_factory=dict)

    def reset_round(self, *, scope_changed, reset_terminals):
        self.amounts.clear()
        self.blocked_label = self.label
        if scope_changed:
            self.player_id = None
        if scope_changed or reset_terminals:
            self.terminal = None

    def observe(self, seat, *, actions_visible):
        actor = seat["player_id"]
        label = seat["action"]
        wager = seat["wager"]
        if actor is not None:
            if self.player_id not in (None, actor):
                self.reset_round(scope_changed=True, reset_terminals=True)
            self.player_id = actor
        self.label = label
        if label != self.blocked_label:
            self.blocked_label = None
        if not actions_visible or label is None or label == self.blocked_label:
            return None
        if self.terminal is not None and (label != self.terminal or label not in self.amounts):
            return None

        repeated = label in self.amounts
        previous = self.amounts.get(label)
        if repeated and (wager is None or previous is not None and wager <= previous):
            return None
        self.amounts[label] = wager
        if label in _TERMINAL:
            self.terminal = label
        return repeated


@dataclass
class _CaptureState:
    latest: dict | None = None
    table_ref: str | None = None
    hand_ref: str | None = None
    street: str | None = None
    board: tuple | None = None
    blocked_board: tuple = ()
    seats: dict = field(default_factory=dict)

    def board_event(self, snapshot, scope_changed):
        if scope_changed:
            self.blocked_board = self.board or self.blocked_board
            self.board = None
        board = tuple(snapshot["board"])
        street = snapshot["street"]
        count = _BOARD_COUNTS.get(street)
        if count is not None:
            if len(board) != count:
                return False
        elif len(board) not in (3, 4, 5):
            return False
        if not board:
            if self.board:
                # Without a known new hand, protect against a temporary render reset.
                self.blocked_board = self.board
            elif snapshot["hand_ref"] is not None:
                # A visible empty board establishes a fresh display in this known hand.
                self.blocked_board = ()
            changed = self.board != ()
            self.board = ()
            return changed
        if self.blocked_board:
            shared = min(len(board), len(self.blocked_board))
            if board[:shared] == self.blocked_board[:shared]:
                return False
            self.blocked_board = ()
        if self.board is not None and (board == self.board or board[:len(self.board)] != self.board):
            return False
        self.board = board
        return True

    def observe(self, capture_id, snapshot):
        table_ref, hand_ref, street = (snapshot[key] for key in ("table_ref", "hand_ref", "street"))
        table_changed = table_ref is not None and self.table_ref not in (None, table_ref)
        hand_changed = hand_ref is not None and self.hand_ref not in (None, hand_ref)
        scope_changed = table_changed or hand_changed
        street_regressed = (
            not scope_changed and hand_ref is not None and hand_ref == self.hand_ref
            and street is not None and self.street is not None
            and _STREET_ORDER[street] < _STREET_ORDER[self.street]
        )
        round_changed = not street_regressed and street is not None and self.street not in (None, street)
        reset_terminals = (
            hand_ref is None and street == "preflop" and not snapshot["board"]
            and (bool(self.board) or self.street == "settled")
        )
        event = {
            "source": PUBLIC_SOURCE, "capture_id": capture_id, "offset_ms": snapshot["offset_ms"],
            "table_ref": table_ref, "hand_ref": hand_ref, "street": street,
            "confirmed_hand": hand_ref is not None,
        }
        events = []
        if self.latest is None or any(snapshot.get(key) != self.latest.get(key) for key in _TABLE_KEYS):
            table_event = {"type": "public_table", **event}
            if "table_details" in snapshot:
                table_event["table_details"] = dict(snapshot["table_details"])
            events.append(table_event)
        if not street_regressed and self.board_event(snapshot, scope_changed):
            events.append({"type": "public_board", **event, "board": list(self.board)})

        if round_changed or scope_changed or reset_terminals:
            for action_state in self.seats.values():
                action_state.reset_round(scope_changed=scope_changed, reset_terminals=reset_terminals)
        previous_seats = _public_seats(self.latest)
        seats = _public_seats(snapshot)
        for slot in sorted(previous_seats.keys() | seats.keys()):
            seat = seats.get(slot)
            previous = previous_seats.get(slot, {})
            actor = seat["player_id"] if seat else None
            seat_event = {**event, "slot": slot, "player_id": actor}
            if actor != previous.get("player_id") or (scope_changed and actor is not None):
                events.append({"type": "public_identity", **seat_event})
            if seat is None:
                continue
            amounts = {"wager": seat["wager"], "stack": seat["stack"]}
            if any(amount != previous.get(key) for key, amount in amounts.items()) or (
                scope_changed and any(amount is not None for amount in amounts.values())
            ):
                events.append({"type": "public_chips", **seat_event, **amounts})
            if street_regressed:
                continue
            observed = self.seats.setdefault(slot, _SeatAction()).observe(seat, actions_visible=street != "settled")
            if observed is not None:
                events.append({
                    "type": "public_action", **seat_event, **amounts,
                    "action": seat["action"], "wager_update": observed,
                })
        if table_ref is not None:
            self.table_ref = table_ref
        if hand_ref is not None or table_changed:
            self.hand_ref = hand_ref
        if not street_regressed and (street is not None or scope_changed):
            self.street = street
        self.latest = snapshot
        return events


class PublicCapture:
    """Track bounded display observations by the collector's capture session."""

    def __init__(self):
        self._captures = {}
        self._lock = Lock()

    def accept(self, capture_id, snapshots):
        """Validate a batch before applying it, and return append-only observations."""
        capture_id = _capture_ref(capture_id)
        if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= MAX_PUBLIC_SNAPSHOTS:
            raise ValueError("公开牌桌批次数量无效")
        validated = [validate_snapshot(snapshot) for snapshot in snapshots]
        with self._lock:
            state = self._captures.get(capture_id)
            if state is None:
                if len(self._captures) >= _MAX_CAPTURES:
                    raise ValueError("公开牌桌采集会话已到上限")
                state = _CaptureState()

            latest = state.latest
            accepted = []
            for snapshot in validated:
                if latest is not None and snapshot["offset_ms"] <= latest["offset_ms"]:
                    if snapshot == latest:
                        continue
                    raise ValueError("公开牌桌快照时间顺序冲突")
                accepted.append(snapshot)
                latest = snapshot
            events = []
            for snapshot in accepted:
                events.extend(state.observe(capture_id, snapshot))
            self._captures[capture_id] = state
            return events
