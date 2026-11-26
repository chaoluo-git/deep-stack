"""Read visible dealer, blinds and ante from an observed table."""

import re
import unicodedata
from decimal import Decimal
from math import isfinite

from .layout import clockwise_seats


_NUMBER = r"[0-9]{1,9}(?:\.[0-9]{1,4})?"
_STAKES = re.compile(
    rf"(?<![0-9.,/])(?P<small>{_NUMBER})\s*/\s*(?P<big>{_NUMBER})"
    rf"(?:\s*\(\s*(?P<ante>{_NUMBER})\s*\))?(?![0-9.,/]|\s*\()"
)
_ANTE = re.compile(rf"\(\s*({_NUMBER})\s*\)")
_BLIND_LABELS = {
    "sb": "small_blind_seat", "smallblind": "small_blind_seat",
    "小盲": "small_blind_seat", "小盲注": "small_blind_seat",
    "bb": "big_blind_seat", "bigblind": "big_blind_seat",
    "大盲": "big_blind_seat", "大盲注": "big_blind_seat",
}


def _texts(fields, key):
    return [
        unicodedata.normalize("NFKC", reading["text"]).strip().casefold()
        for reading in fields.get(key, {}).get("observations", ())
        if isinstance(reading.get("text"), str)
        and type(reading.get("confidence")) in (int, float)
        and 0.80 <= reading["confidence"] <= 1
    ]


def _stakes(fields):
    texts = _texts(fields, "table_stakes")
    readings = set()
    for text in texts:
        for match in _STAKES.finditer(text):
            small, big = Decimal(match["small"]), Decimal(match["big"])
            if 0 < small <= big:
                ante = Decimal(match["ante"]) if match["ante"] else None
                readings.add((small, big, ante))
    pairs = {(small, big) for small, big, _ in readings}
    if len(pairs) != 1:
        return None, None, None
    antes = {value for _, _, value in readings if value is not None}
    # The dedicated single-row crop may split the ante into a second OCR block.
    if len(texts) == 2 and _STAKES.fullmatch(texts[0]):
        if match := _ANTE.fullmatch(texts[1]):
            antes.add(Decimal(match[1]))
    small, big = pairs.pop()
    return small, big, next(iter(antes)) if len(antes) == 1 else None


def _amount_matches(value, expected):
    return type(value) in (int, float) and isfinite(value) and Decimal(str(value)) == expected


def _infer_blinds(rows, vacant, inactive, dealer, small, big):
    if dealer is None or small is None or big is None:
        return None
    order = clockwise_seats(rows.keys() | vacant)
    if order is None:
        return None
    occupied = []
    for seat in order:
        row = rows.get(seat, {})
        stack, wager = row.get("stack"), row.get("wager")
        if seat in vacant:
            if row.get("name") or row.get("action") or stack is not None or wager not in (None, 0):
                return None
            continue
        if type(stack) not in (int, float) or not isfinite(stack) or stack <= 0:
            return None
        if seat in inactive:
            if row.get("action") or wager not in (None, 0):
                return None
            continue
        occupied.append(seat)
    if len(occupied) < 2 or dealer not in occupied:
        return None
    index = occupied.index(dealer)
    small_offset, big_offset = (0, 1) if len(occupied) == 2 else (1, 2)
    small_seat = occupied[(index + small_offset) % len(occupied)]
    big_seat = occupied[(index + big_offset) % len(occupied)]
    if rows[small_seat].get("action") not in (None, "fold") or rows[big_seat].get("action") not in (None, "fold", "check"):
        return None
    if not _amount_matches(rows[small_seat].get("wager"), small):
        return None
    if not _amount_matches(rows[big_seat].get("wager"), big):
        return None
    return small_seat, big_seat


def _number(value):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def read_table_positions(fields, seats, vacant_seats, street, *, inactive_seats=(), conflicting_seats=()):
    """Return only visible roles, or corroborated preflop seat-order inference."""
    rows = {row["seat"]: row for row in seats}
    vacant = set(vacant_seats)
    inactive = set(inactive_seats)
    markers = [
        seat for seat in rows for side in ("left", "right")
        if "d" in _texts(fields, f"{seat}.dealer_{side}")
    ]
    labels = {"small_blind_seat": set(), "big_blind_seat": set()}
    for seat in rows.keys() - vacant - inactive:
        for text in _texts(fields, f"{seat}.blind_label"):
            if role := _BLIND_LABELS.get("".join(text.split())):
                labels[role].add(seat)
    dealer = markers[0] if len(markers) == 1 and markers[0] not in vacant else None
    small, big, ante = _stakes(fields)
    positions = {
        "dealer_seat": dealer,
        "small_blind_seat": None, "big_blind_seat": None,
        "small_blind": _number(small), "big_blind": _number(big), "ante": _number(ante),
    }
    sources = {key: "table_stakes" for key in ("small_blind", "big_blind", "ante") if positions[key] is not None}
    if dealer is not None:
        sources["dealer_seat"] = "dealer_marker"
    if labels["small_blind_seat"] & labels["big_blind_seat"]:
        return {**positions, "sources": sources}
    for key, candidates in labels.items():
        if len(candidates) == 1:
            positions[key] = next(iter(candidates))
            sources[key] = "blind_label"
    if (street == "preflop" and not conflicting_seats and len(rows) == len(seats)
            and all(len(candidates) <= 1 for candidates in labels.values())):
        inferred = _infer_blinds(rows, vacant, inactive, dealer, small, big)
        if inferred and all(not labels[key] or seat in labels[key] for key, seat in zip(labels, inferred)):
            for key, seat in zip(labels, inferred):
                if positions[key] is None:
                    positions[key] = seat
                    sources[key] = "preflop_order_and_wager"
    return {**positions, "sources": sources}
