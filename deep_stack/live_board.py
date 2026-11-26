"""Report public card readings without printing private cards or OCR noise."""

from collections import Counter

from .ev import RANKS, SUITS
from .observations import STREETS


_CARD_COUNTS = {street: count for count, street in STREETS.items()}
_STREET_LABELS = {"preflop": "翻前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
_SUIT_SYMBOLS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}


def _read_slots(values):
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(value if (
        isinstance(value, str) and len(value) == 2 and value[0] in RANKS and value[1] in SUITS
    ) else None for value in values[:5])


def _snapshot(frame):
    street = frame.get("street")
    count = _CARD_COUNTS.get(street)
    full = frame.get("board")
    observed = _read_slots(frame.get("observed_board_cards"))
    if count == 0:
        return (street, (), True) if full in ([], ()) and not any(observed) else None
    if count is not None:
        cards = _read_slots(full)
        if len(cards) == count and all(cards) and len(set(cards)) == count:
            return street, cards, True
        padded = (observed + (None,) * count)[:count]
    else:
        if not any(observed):
            return None
        street = None
        last_read = max(index for index, card in enumerate(observed) if card is not None)
        padded = observed[:last_read + 1]

    occurrences = Counter(padded)
    return street, tuple(card if occurrences[card] == 1 else None for card in padded), False


def _event(snapshot, hand_id):
    street, cards, complete = snapshot
    known = sum(card is not None for card in cards)
    count = _CARD_COUNTS.get(street)
    label = _STREET_LABELS.get(street, "街次待确认")
    if street == "preflop":
        message = "公共牌｜翻前，等待发牌"
    else:
        faces = " ".join(f"{card[0].replace('T', '10')}{_SUIT_SYMBOLS[card[1]]}" if card else "?" for card in cards)
        message = f"公共牌｜{label} {faces}"
        if not complete and count is not None:
            status = f"识别 {known}/{count}" if known < count else "待复核"
            message += f"（{status}）"
    return {"type": "board", "street": street, "cards": list(cards), "complete": complete,
            "known_count": known, "card_count": count, "hand_id": hand_id, "message": message}


class BoardLog:
    """Emit stage changes and improved public card readings once."""

    def __init__(self):
        self._hand_id = None
        self._reported = None
        self._pending = None

    def update(self, frame, hand_id):
        snapshot = None if frame.get("popup_open") else _snapshot(frame)
        if snapshot is None:
            self._pending = None
            return None

        if hand_id is not None and self._hand_id is not None and hand_id != self._hand_id:
            self._reported = self._pending = None
        if hand_id is not None:
            self._hand_id = hand_id

        if self._reported is not None:
            if snapshot == self._reported:
                self._pending = None
                return None
            if snapshot[0] != "preflop" and not self._has_update(snapshot):
                return None

        if snapshot[0] == "preflop" and hand_id is None:
            self._hand_id = None
        self._reported = snapshot
        self._pending = None
        return _event(snapshot, hand_id)

    def _has_update(self, snapshot):
        street, cards, complete = snapshot
        previous_street, previous_cards, previously_complete = self._reported
        if street == previous_street and previously_complete and not complete:
            self._pending = None
            return False
        overlap = tuple(zip(cards, previous_cards))
        if any(current and previous and current != previous for current, previous in overlap):
            # A single changed card may be a recognition error during an animation.
            if self._pending != snapshot:
                self._pending = snapshot
                return False
            return True

        self._pending = None
        old_count = _CARD_COUNTS.get(previous_street)
        new_count = _CARD_COUNTS.get(street)
        if old_count is not None and new_count is not None and new_count < old_count:
            return False
        return (street is not None and street != previous_street
                or complete and not previously_complete
                or any(current and not previous for current, previous in overlap)
                or any(cards[len(previous_cards):]))
