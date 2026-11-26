"""Read the visible player ID and match a profile popup to one current seat."""

from dataclasses import dataclass, field
from decimal import Decimal
import re
import unicodedata

from .avatar_match import match_avatar


_ID_PREFIX = r"(?:玩家|用户|用戶|user|player)?id[:#]*"
_ID_LINE = re.compile(_ID_PREFIX + r"(?P<id>[0-9]{4,24})")
_ID_LABEL = re.compile(_ID_PREFIX)
_ID_VALUE = re.compile(r"[0-9]{4,24}")
_BADGE_ROW = re.compile(r"(?:1d|ld|d)?(?P<id>[0-9]{4,24})")
_NAME_LINE = re.compile(r"(?:玩家昵称|玩家暱稱|昵称|暱稱|用户名|用戶名|nickname|username|name)\s*:\s*(.+)",
                        re.IGNORECASE)
_HANDS = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,12})")
_PERCENT = re.compile(r"([0-9]{1,3}(?:\.[0-9]{1,4})?)\s*%")
_STATS = ("hands", "win_rate", "pot_entry_rate", "showdown_win_rate")
POPUP_REGIONS = (
    "player_popup",
    "player_popup_id",
    "player_popup_id_label",
    "player_popup_id_value",
    "player_popup_name",
    *(f"player_popup_{key}" for key in _STATS),
)
_MIN_CONFIDENCE = 0.75


@dataclass(frozen=True)
class PlayerPopup:
    player_id: str
    name: str | None = None
    stats: dict[str, int | float] = field(default_factory=dict)


def _normalized(text):
    return unicodedata.normalize("NFKC", text).strip()


def _id_text(text):
    return "".join(_normalized(text).casefold().split())


def _readings(fields, key):
    return [
        reading for reading in fields.get(key, {}).get("observations", ())
        if isinstance(reading.get("text"), str) and reading["text"].strip()
        and type(reading.get("confidence")) in (float, int)
        and _MIN_CONFIDENCE <= reading["confidence"] <= 1
    ]


def _reading_id(text):
    match = _ID_VALUE.fullmatch(text)
    return text if match and any(digit != "0" for digit in text) else None


def _same_row(label, value, tolerance):
    coords = tuple(reading.get(axis) for reading in (label, value) for axis in ("center_x", "center_y"))
    if any(type(coord) not in (float, int) or not 0 <= coord <= 1 for coord in coords):
        return False
    label_x, label_y, value_x, value_y = coords
    return label_x < value_x and abs(label_y - value_y) <= tolerance


def _ids_in_region(readings, *, tight=False):
    texts = [(reading, _id_text(reading["text"])) for reading in readings]
    ids = set()
    labels = [reading for reading, text in texts if _ID_LABEL.fullmatch(text)]
    values = [(reading, value) for reading, text in texts if (value := _reading_id(text))]
    for _, text in texts:
        match = _ID_LINE.fullmatch(text)
        if match and (player_id := _reading_id(match["id"])):
            ids.add(player_id)
    for reading, value in values:
        if any(_same_row(label, reading, 0.16 if tight else 0.035) for label in labels):
            ids.add(value)
        elif tight and len(readings) == len(labels) + len(values) == 2:
            # An isolated row can return exactly the ID label and value without coordinates.
            if len(labels) == 1 and len(values) == 1 and all("center_y" not in item for item in readings):
                ids.add(value)
    return ids


def _popup_name(text):
    name = _normalized(text)
    if name and len(name) <= 64 and all(unicodedata.category(char) != "Cc" for char in name):
        return name
    return None


def _names(fields, general):
    names = set()
    for reading in general:
        if match := _NAME_LINE.fullmatch(_normalized(reading["text"])):
            if name := _popup_name(match[1]):
                names.add(name)
    for reading in _readings(fields, "player_popup_name"):
        text = _normalized(reading["text"])
        match = _NAME_LINE.fullmatch(text)
        compact = _id_text(text)
        if _ID_LINE.fullmatch(compact) or _ID_LABEL.fullmatch(compact):
            continue
        if name := _popup_name(match[1] if match else text):
            names.add(name)
    return names


def _stat(text, key):
    text = _normalized(text)
    if key == "hands":
        if _HANDS.fullmatch(text):
            value = text.replace(",", "")
            return int(value) if len(value) <= 12 else None
        return None
    if match := _PERCENT.fullmatch(text):
        value = Decimal(match[1])
        if 0 <= value <= 100:
            return float(value / 100)
    return None


def parse_popup(fields, *, badge=False):
    """Return a unique confidently shown ID, optional name, and visible stats."""
    general = _readings(fields, "player_popup")
    ids = _ids_in_region(general)
    row = _readings(fields, "player_popup_id")
    ids.update(_ids_in_region(row, tight=True))
    number_reads = {
        value for reading in _readings(fields, "player_popup_id_value")
        if (value := _reading_id(_id_text(reading["text"])))
    }
    label = _readings(fields, "player_popup_id_label")
    if label and all(_ID_LABEL.fullmatch(_id_text(reading["text"])) for reading in label):
        ids.update(number_reads)
    if badge and len(number_reads) == 1:
        # The colored badge supplies context when OCR drops its narrow "I".
        for reading in row:
            match = _BADGE_ROW.fullmatch(_id_text(reading["text"]))
            if match and match["id"] in number_reads:
                ids.add(match["id"])
    names = _names(fields, general)
    if len(ids) != 1 or len(names) > 1:
        return None
    stats = {}
    for key in _STATS:
        values = {
            value for reading in _readings(fields, f"player_popup_{key}")
            if (value := _stat(reading["text"], key)) is not None
        }
        if len(values) == 1:
            stats[key] = values.pop()
    return PlayerPopup(ids.pop(), next(iter(names), None), stats)


def nickname_key(value):
    """Normalize spacing from OCR without dropping meaningful characters."""
    return "".join(_normalized(value).split()) if isinstance(value, str) else ""


def match_popup(popup, seats, *, previous_seats=(), popup_avatar=None, seat_avatars=None):
    """Resolve one seat from independent, non-conflicting nickname and avatar evidence."""
    seat, _ = resolve_popup_seat(popup, seats, previous_seats=previous_seats, popup_avatar=popup_avatar,
                                 seat_avatars=seat_avatars)
    return seat


def resolve_popup_seat(popup, seats, *, previous_seats=(), popup_avatar=None, seat_avatars=None):
    """Return the matched seat or the reason no safe binding can be made."""
    if popup is None:
        return None, "profile_unread"

    popup_name = nickname_key(popup.name)
    current = {seat["seat"]: seat for seat in seats}
    matches = set()
    names = {}
    for seat in (*current.values(), *previous_seats):
        name = nickname_key(seat.get("name"))
        if not name or seat.get("vacant"):
            continue
        seat_id = seat["seat"]
        names.setdefault(seat_id, set()).add(name)
        if name == popup_name:
            matches.add(seat_id)

    avatar_seat = match_avatar(popup_avatar, {
        seat_id: views for seat_id, views in (seat_avatars or {}).items()
        if seat_id in current and not current[seat_id].get("vacant")
    })
    if avatar_seat is not None:
        if matches and avatar_seat not in matches:
            return None, "avatar_nickname_conflict"
        return avatar_seat, None

    if len(matches) > 1:
        return None, "duplicate_nickname"
    if not matches:
        return None, "no_matching_seat" if popup_name else "popup_nickname_unavailable"
    seat = matches.pop()
    visible_seat = current.get(seat)
    if not visible_seat or visible_seat.get("vacant"):
        return None, "seat_unavailable"
    if names[seat] != {popup_name}:
        return None, "nickname_changed"
    return seat, None
