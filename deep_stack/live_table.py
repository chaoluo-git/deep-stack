"""Keep the current hand's poker positions and report visible seat changes."""

from .layout import SEAT_LABELS
from .seat_positions import read_seat_positions


_POSITION_ORDER = ("BTN/SB", "BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO")
_ROLES = {
    "dealer_seat": {"BTN", "BTN/SB"}, "small_blind_seat": {"SB", "BTN/SB"}, "big_blind_seat": {"BB"},
}
_COMPLETE = {"confirmed", "inferred"}


def _seat_names(statuses, *selected):
    return "、".join(SEAT_LABELS.get(seat, seat) for seat, state in statuses.items() if state in selected)


def _stack_line(stacks):
    return "余额 " + "、".join(f"{SEAT_LABELS.get(seat, seat)} {amount:g}" for seat, amount in stacks.items())


def _summary_text(details):
    capacity = f"{details['seat_capacity']} 座" if details["seat_layout_confirmed"] else "座位数待核对"
    small, big, ante = (details.get(key) for key in ("small_blind", "big_blind", "ante"))
    stakes = f"{small if small is not None else '?'} / {big if big is not None else '?'}"
    headline = f"牌桌 {capacity}｜盲注 {stakes}｜ante {ante if ante is not None else '待核对'}"
    if details["dealt_players"] is not None:
        headline += f"｜本手 {details['dealt_players']} 人"
    lines = [headline]

    labels = {position: SEAT_LABELS.get(seat, seat) for seat, position in details["position_by_seat"].items()}
    status = details["position_status"]
    position_text = "｜".join(f"{key} {labels[key]}" for key in _POSITION_ORDER if key in labels)
    if position_text:
        qualifier = {"inferred": "（按座次推断）", "partial": "（部分确认）", "conflict": "（读数冲突）"}.get(status, "")
        lines.append(f"位置{qualifier} {position_text}")
    else:
        lines.append("位置读数冲突，待核对" if status == "conflict" else "位置待核对")

    statuses = details["seat_statuses"]
    seat_changes = []
    vacant = _seat_names(statuses, "vacant")
    if vacant:
        seat_changes.append(f"空位 {vacant}")
    elif details["seat_layout_confirmed"] and statuses and all(
        state in {"occupied", "inactive"} for state in statuses.values()
    ):
        seat_changes.append("空位 0")
    for name, states in (("等待/留座", ("inactive",)), ("待确认", ("unknown", "conflict"))):
        if names := _seat_names(statuses, *states):
            seat_changes.append(f"{name} {names}")
    if seat_changes:
        lines.append("｜".join(seat_changes))
    if details["stacks"]:
        lines.append(_stack_line(details["stacks"]))
    return "\n".join(lines)


class TableLog:
    """Report topology and use the same positions throughout a known hand."""

    def __init__(self):
        self.position_by_seat = {}
        self._previous = None
        self._hand = None
        self._hand_roles = {}
        self._hand_positions = None

    def _resolve(self, result, hand, raw_roles):
        assignments = result["position_by_seat"]
        status = result["position_status"]
        roles = {**raw_roles}
        for key, labels in _ROLES.items():
            if not roles[key]:
                roles[key] = next((seat for seat, position in assignments.items() if position in labels), None)
        if hand is None:
            return assignments, status, roles
        if self._hand != hand:
            self._hand = hand
            self._hand_roles = {}
            self._hand_positions = None
        if status == "conflict" or any(
            seat and self._hand_roles.get(key) and seat != self._hand_roles[key] for key, seat in raw_roles.items()
        ):
            self._hand_roles = {}
            self._hand_positions = None
            return assignments if status == "conflict" else {}, "conflict", raw_roles
        if status in _COMPLETE and (self._hand_positions is None or (
            status == "confirmed" and self._hand_positions[1] == "inferred"
        )):
            self._hand_positions = (assignments, status)
        if self._hand_positions is not None:
            assignments, status = self._hand_positions
            roles = {key: next((seat for seat, position in assignments.items() if position in labels), None)
                     for key, labels in _ROLES.items()}
        self._hand_roles.update({key: seat for key, seat in roles.items() if seat is not None})
        return assignments, status, {key: self._hand_roles.get(key) for key in _ROLES}

    def update(self, frame, hand):
        positions = frame.get("positions") or {}
        roster = read_seat_positions(frame)
        layout_confirmed = bool(frame.get("seat_layout_confirmed"))
        if not layout_confirmed and not any(positions.values()) and not any(
            status in {"vacant", "inactive"} for status in roster["seat_statuses"].values()
        ):
            self.position_by_seat = {}
            return None

        raw_roles = {key: positions.get(key) for key in _ROLES}
        assignments, status, roles = self._resolve(roster, hand, raw_roles)
        self.position_by_seat = assignments
        stacks = {row["seat"]: row["stack"] for row in frame["seats"] if row["stack"] is not None}
        details = {
            "seat_capacity": frame.get("seat_capacity"), "seat_layout_confirmed": layout_confirmed,
            "spectating": frame.get("spectating", False), "hero_state": frame.get("hero_state"),
            **{key: value for key, value in positions.items() if key != "sources"}, **roles,
            "position_by_seat": assignments, "position_status": status, "seat_statuses": roster["seat_statuses"],
            "dealt_players": len(assignments) if status in _COMPLETE else None,
        }
        previous = self._previous
        self._previous = (details, stacks)
        if previous is not None and details == previous[0]:
            changes = {seat: amount for seat, amount in stacks.items() if previous[1].get(seat) != amount}
            if not changes:
                return None
            message = _stack_line(changes)
        else:
            message = _summary_text({**details, "stacks": stacks})
        return {"type": "table", **details, "hand_id": hand, "stacks": stacks, "message": message}
