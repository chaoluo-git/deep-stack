"""Report visible action labels without claiming a complete hand history."""

from math import isfinite

from .layout import SEAT_LABELS
from .observations import STREETS
from .player_store import _name_key


_STREET_LABELS = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
_ACTION_LABELS = {"fold": "弃牌", "check": "过牌", "call": "跟注", "bet": "下注",
                  "raise": "加注", "all_in": "全下"}
_STREET_ORDER = tuple(STREETS.values())
_TERMINAL = {"fold", "all_in"}


class ActionLog:
    """Deduplicate consecutive readings while keeping confirmed hands separate."""

    def __init__(self):
        self._hand_id = None
        self._street = None
        self._amounts = {}
        self._unknown_amounts = {}
        self._terminal_streets = {}

    def update(self, frame, hand_id, bindings, *, positions=None):
        street = frame.get("street")
        if frame.get("popup_open"):
            return []
        if street not in _STREET_ORDER:
            street = hand_id = None

        hand_changed = hand_id is not None and self._hand_id is not None and hand_id != self._hand_id
        unconfirmed_reset = (
            hand_id is None and street is not None and self._street is not None
            and _STREET_ORDER.index(street) < _STREET_ORDER.index(self._street)
        )
        if hand_changed or unconfirmed_reset:
            self._amounts.clear()
            self._terminal_streets.clear()
            self._hand_id = None
        if hand_id is not None:
            self._hand_id = hand_id
        if street is not None:
            self._street = street
            self._unknown_amounts.clear()
        amounts = self._amounts if street is not None else self._unknown_amounts

        active = {item["seat"]: item for item in bindings if not item["paused"]}
        inactive = set(frame.get("vacant_seats", ())) | set(frame.get("inactive_seats", ()))
        events = []
        for reading in frame.get("seats", ()):
            seat = reading["seat"]
            action = reading.get("action")
            if action not in _ACTION_LABELS or seat in inactive:
                continue
            if action in _TERMINAL:
                prior_street = self._terminal_streets.get((seat, action))
                if prior_street is not None and prior_street != street:
                    continue
                if street is not None:
                    self._terminal_streets[(seat, action)] = street
            wager = reading.get("wager")
            if type(wager) not in (int, float) or not 0 <= wager <= 1e12 or not isfinite(wager):
                wager = None
            values = amounts.setdefault((street, seat, action), set())
            if wager in values or (
                values and (wager is None or action in _TERMINAL and values != {None})
            ):
                continue
            wager_update = values == {None}
            values.add(wager)

            binding = active.get(seat)
            name = reading.get("name")
            if binding and name and binding.get("name") and _name_key(name) != _name_key(binding["name"]):
                binding = None
            player_id = binding["player_id"] if binding else None
            position = positions.get(seat) if positions is not None else None
            events.append(_action_event(street, seat, action, wager, hand_id, player_id, wager_update, position))
        return events


def _action_event(street, seat, action, wager, hand_id, player_id, wager_update, position):
    seat_label = SEAT_LABELS.get(seat, seat)
    identity = f"{position} · {seat_label}" if position else seat_label
    if player_id is not None:
        identity += f" ID {player_id}"
    amount = f"，可见金额 {wager:g}" if wager is not None else ""
    pending = "（手牌待确认）" if hand_id is None else ""
    label = _ACTION_LABELS[action]
    if wager_update:
        label += "金额补读"
    return {
        "type": "action",
        "street": street,
        "seat": seat,
        "seat_label": seat_label,
        "position": position,
        "action": action,
        "wager": wager,
        "hand_id": hand_id,
        "player_id": player_id,
        "confirmed_hand": hand_id is not None,
        "wager_update": wager_update,
        "message": f"{_STREET_LABELS.get(street, '街次待确认')} {identity} {label}{amount}{pending}",
    }
