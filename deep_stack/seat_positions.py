"""Derive preflop position labels from the observed clockwise hand roster."""

from .layout import clockwise_seats
from .observations import STREETS


_ACTIONS = {"fold", "check", "call", "bet", "raise", "all_in"}
_STATUSES = {"occupied", "vacant", "inactive", "unknown", "conflict"}
_ROLE_KEYS = ("dealer_seat", "small_blind_seat", "big_blind_seat")
_ROLE_LABELS = ("BTN", "SB", "BB")
# After the blinds, keep CO/HJ/LJ relative to the button. At six and seven
# handed, use UTG for the first player to act where that early label is used.
_EARLY_TO_LATE = {
    3: (),
    4: ("CO",),
    5: ("HJ", "CO"),
    6: ("UTG", "HJ", "CO"),
    7: ("UTG", "LJ", "HJ", "CO"),
    8: ("UTG", "UTG+1", "LJ", "HJ", "CO"),
    9: ("UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO"),
}


def _deal_evidence(frame, rows, blind_seats, ante):
    dealt = set(frame.get("cardbacks", ())) | blind_seats
    if frame.get("hero_state") in {"playing", "folded"}:
        dealt.add("hero")
    preflop = frame.get("street") == "preflop"
    for seat, row in rows.items():
        wager = row.get("wager")
        posted = type(wager) in (int, float) and 0 < wager <= 1e12
        if row.get("action") in _ACTIONS or (posted and (not preflop or wager != ante)):
            dealt.add(seat)
    return dealt


def _status(seat, row, observed, vacant, inactive, dealt, portraits):
    status = observed.get(seat)
    if status in _STATUSES and status != "unknown":
        return "conflict" if seat in dealt and status in {"inactive", "vacant"} else status
    if seat in vacant or seat in inactive:
        if seat in dealt or (seat in vacant and seat in inactive):
            return "conflict"
        return "vacant" if seat in vacant else "inactive"
    if seat in dealt:
        return "occupied"
    if status == "unknown":
        return status
    stack = row.get("stack")
    if type(stack) in (int, float) and 0 <= stack <= 1e12 and (row.get("name") or seat in portraits):
        return "occupied"
    return "unknown"


def _roles(positions, statuses):
    dealer, small, big = (positions.get(key) for key in _ROLE_KEYS)
    visible = (dealer, small, big)
    if any(seat is not None and statuses.get(seat) not in {"occupied", "unknown"} for seat in visible):
        return None
    if big is not None and big in (dealer, small):
        return None
    roles = {seat: role for seat, role in zip(visible, _ROLE_LABELS) if seat is not None}
    if dealer is not None and dealer == small:
        roles[dealer] = "BTN/SB"
    return roles


def read_seat_positions(frame, *, positions=None):
    """Return physical seat statuses and any supportable poker positions.

    Complete mappings need a known layout and occupied dealer, a recognized
    street, resolved seat statuses, and signs of participation on two seats.
    If only some occupied seats show in-hand evidence, the mapping is inferred
    from the occupied roster. A folded player still belongs to that roster.
    """
    seats = frame.get("seats", ())
    rows = {row["seat"]: row for row in seats}
    observed = frame.get("seat_status") or {}
    vacant = set(frame.get("vacant_seats", ()))
    inactive = set(frame.get("inactive_seats", ()))
    physical = rows.keys() | observed.keys() | vacant | inactive
    order = clockwise_seats(physical)
    listing = order if order is not None else (*rows, *sorted(physical - rows.keys()))
    if positions is None:
        positions = frame.get("positions") or {}
    dealer, small, big = (positions.get(key) for key in _ROLE_KEYS)
    blind_seats = {seat for seat in (small, big) if seat is not None}
    dealt = _deal_evidence(frame, rows, blind_seats, positions.get("ante"))
    portraits = frame.get("seat_avatars", {})
    statuses = {seat: _status(seat, rows.get(seat, {}), observed, vacant, inactive, dealt, portraits)
                for seat in listing}
    result = {"position_by_seat": {}, "seat_statuses": statuses, "position_status": "waiting"}
    if len(rows) != len(seats) or "conflict" in statuses.values():
        return {**result, "position_status": "conflict"}
    if order is None or not frame.get("seat_layout_confirmed"):
        return result

    occupied = [seat for seat in order if statuses[seat] == "occupied"]
    visible_roles = _roles(positions, statuses)
    if visible_roles is None or (dealer is not None and dealer == small and len(dealt & physical) > 2):
        return {**result, "position_status": "conflict"}
    if (dealer not in occupied or "unknown" in statuses.values()
            or frame.get("street") not in STREETS.values() or len(dealt.intersection(occupied)) < 2):
        return {**result, "position_by_seat": visible_roles,
                "position_status": "partial" if visible_roles else "waiting"}

    index = occupied.index(dealer)
    dealt_order = occupied[index:] + occupied[:index]
    labels = ("BTN/SB", "BB") if len(occupied) == 2 else (*_ROLE_LABELS, *_EARLY_TO_LATE[len(occupied)])
    by_seat = dict(zip(dealt_order, labels))
    if any(by_seat[seat] != role and role not in by_seat[seat].split("/") for seat, role in visible_roles.items()):
        return {**result, "position_status": "conflict"}
    return {**result, "position_by_seat": by_seat,
            "position_status": "confirmed" if dealt.issuperset(occupied) else "inferred"}
