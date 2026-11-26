"""Read public, visible table information from a saved WePoker screenshot."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from importlib.util import find_spec
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unicodedata

from .layout import (
    BOARD_CARD_FIELDS as _BOARD_FIELDS,
    HERO_CARD_FIELDS as _HERO_FIELDS,
    SEAT_IDS,
    default_profile,
    detect_viewport,
    layout_warnings,
    pixel_regions,
    validate_viewport,
)
from .player_popup import POPUP_REGIONS, parse_popup


class RecognitionError(ValueError):
    """A screenshot could not be decoded or passed to the OCR backend."""


_MIN_CONFIDENCE = 0.50

_ACTIONS = {
    "弃牌": "fold", "棄牌": "fold", "已弃牌": "fold", "已棄牌": "fold",
    "蓋牌": "fold", "fold": "fold",
    "让牌": "check", "讓牌": "check", "过牌": "check", "過牌": "check", "check": "check",
    "跟注": "call", "跟註": "call", "call": "call",
    "下注": "bet", "下註": "bet", "bet": "bet",
    "加注": "raise", "加註": "raise", "加注到": "raise", "加註到": "raise",
    "raise": "raise", "raiseto": "raise",
    "全下": "all_in", "全押": "all_in", "梭哈": "all_in", "allin": "all_in",
}
_SEAT_LABELS = frozenset({"空位", "坐下", "留座", "等待加入", "等待中", "离座", "離座"})
_VACANT_LABELS = frozenset({"空位", "坐下", "等待加入", "离座", "離座", "empty"})
_AMOUNT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?([kmb万萬亿億])?", re.ASCII)
_AMOUNT_PREFIX = re.compile(r"^(?:(?:总|總)?\s*底\s*池|total\s*pot|pot|筹码|籌碼)\s*:?\s*")
_ACTION_WITH_AMOUNT = re.compile(r"([^0-9]+?)([0-9].*)")
_MULTIPLIERS = {
    "k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "万": 10_000, "萬": 10_000,
    "亿": 100_000_000, "億": 100_000_000,
}
_SUITS = {"♠": "s", "♤": "s", "♥": "h", "♡": "h", "♦": "d", "♢": "d", "♣": "c", "♧": "c"}
_CARD_CODE = re.compile(r"(10|[2-9tjqka])([shdc♠♤♥♡♦♢♣♧])", re.IGNORECASE)
_RANK = re.compile(r"10|[2-9tjqka]", re.IGNORECASE)
# Vision can return Cyrillic lookalikes even when the OCR language is English.
_CARD_HOMOGLYPHS = str.maketrans("зсактнј", "3cakthj")
_STREETS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}


def live_profile():
    """Add the observed cardback anchors and a search area for manual controls."""
    profile = default_profile()
    regions = profile.regions
    for seat in SEAT_IDS:
        if seat == "hero":
            continue
        avatar = regions[f"{seat}.avatar"]
        center_x = avatar["x"] + avatar["width"] / 2
        center_y = avatar["y"] + avatar["height"] / 2
        offset = -56 if seat in {"top_left", "left_upper", "left_middle", "left_lower"} else 16
        regions[f"{seat}.cardback"] = {
            "x": center_x + offset / 3024, "y": center_y + 9 / 1470,
            "width": 42 / 3024, "height": 41 / 1470, "kind": "marker",
        }
    def add(key, x, y, width, height, kind):
        regions[key] = {"x": x / 3024, "y": (y - 242) / 1470, "width": width / 3024,
                        "height": height / 1470, "kind": kind}

    add("hero_passive_button", 1089, 1388, 1663, 220, "label")
    add("player_popup_id", 1384, 568, 235, 39, "label")
    add("player_popup_id_label", 1387, 569, 37, 37, "label")
    add("player_popup_id_value", 1425, 568, 194, 39, "number")
    add("player_popup_name", 1422, 517, 370, 51, "label")
    for key, x in (("hands", 1284), ("win_rate", 1437), ("pot_entry_rate", 1589),
                   ("showdown_win_rate", 1740)):
        add(f"player_popup_{key}", x - 56, 875, 112, 45, "number")
    return profile


def _normalized(text):
    return unicodedata.normalize("NFKC", text).strip()


def _compact(text):
    return "".join(_normalized(text).split()).casefold()


def _amount_match(text):
    value = _AMOUNT_PREFIX.sub("", _normalized(text).casefold())
    value = re.sub(r"\s+([kmb万萬亿億])$", r"\1", value)
    return _AMOUNT.fullmatch(value)


def _amount(text):
    match = _amount_match(text)
    if match is None:
        return None
    whole, fraction, suffix = match.groups()
    if len(whole.replace(",", "")) > 12 or fraction is not None and len(fraction) > 7:
        return None
    number = Decimal(whole.replace(",", "") + (fraction or ""))
    number *= _MULTIPLIERS.get(suffix, 1)
    return int(number) if number == number.to_integral_value() else float(number)


def _action_key(text):
    label = _compact(text)
    return "allin" if label in {"all-in", "all_in"} else label


def _action(text):
    action = _ACTIONS.get(_action_key(text))
    if action is not None:
        return action
    match = _ACTION_WITH_AMOUNT.fullmatch(_normalized(text))
    if match is None or _amount(match.group(2)) is None:
        return None
    return _ACTIONS.get(_action_key(match.group(1).rstrip().removesuffix(":")))


def _countable_amount(text):
    # A display suffix abbreviates the value and can hide several chip units.
    match = _amount_match(text)
    return match is not None and match.group(3) is None


def _texts(fields, key):
    return tuple(
        _normalized(item["text"])
        for item in fields.get(key, {}).get("observations", [])
        if item["confidence"] >= _MIN_CONFIDENCE and item["text"].strip()
    )


def _single(values):
    distinct = set(values)
    return next(iter(distinct)) if len(distinct) == 1 else None


def _read_amount(fields, key, warnings):
    texts = _texts(fields, key)
    amounts = {amount for text in texts if (amount := _amount(text)) is not None}
    if len(amounts) != 1:
        if len(amounts) > 1:
            warnings.append(f"{key} 区域出现多个不同读数，已保留未知。")
        return None
    amount = amounts.pop()
    if not any(_amount(text) == amount and _countable_amount(text) for text in texts):
        warnings.append(f"{key} 使用缩写单位，显示的换算数值可能经过取整。")
    return amount


def _card_text(text):
    return _compact(text).translate(_CARD_HOMOGLYPHS).replace("\ufe0e", "").replace("\ufe0f", "")


def _card(fields, key):
    texts = tuple(_card_text(text) for text in _texts(fields, key))
    matches = [match for text in texts if (match := _CARD_CODE.fullmatch(text))]
    cards = {_encode_card(*match.groups()) for match in matches}

    # OCR can emit the rank and a Unicode suit as two separate lines.
    rank = _single(text for text in texts if _RANK.fullmatch(text))
    suit = _single(_SUITS[text] for text in texts if text in _SUITS)
    if rank is not None and suit is not None:
        cards.add(_encode_card(rank, suit))

    # A visual suit is useful only when a separate crop confirms the upper-left rank.
    corner = fields.get(key, {}).get("corner_card")
    if corner is not None and corner["suit"] in {"s", "h", "d", "c"}:
        ranks = (
            _card_text(reading["text"]) for reading in corner["rank_observations"]
            if reading["confidence"] >= _MIN_CONFIDENCE
        )
        corner_rank = _single(rank for text in ranks if (rank := _rank_token(text)) is not None)
        if corner_rank is not None:
            cards.add(_encode_card(corner_rank, corner["suit"]))
    return _single(cards)


def _rank_token(text):
    if _RANK.fullmatch(text):
        return text
    # The OCR backend can repeat a crop to make an isolated rank legible.
    for rank in ("10", "2", "3", "4", "5", "6", "7", "8", "9", "t", "j", "q", "k", "a"):
        if text in (rank * 2, rank * 3):
            return rank
    return None


def _encode_card(rank, suit):
    return ("T" if rank == "10" else rank.upper()) + _SUITS.get(suit, suit.lower())


def _seat(fields, seat, warnings):
    labels = _texts(fields, f"{seat}.label")
    status = _texts(fields, f"{seat}.status")
    actions = {action for text in labels + status if (action := _action(text)) is not None}
    # Any OCR text in an avatar could be part of the image. Only an explicit
    # folded label is expected to be displayed there by the table.
    if any(_action(text) == "fold" for text in _texts(fields, f"{seat}.avatar")):
        actions.add("fold")
    if len(actions) > 1:
        warnings.append(f"{seat} 同时出现不同动作标签，已保留未知。")
    names = [text for text in labels if _action(text) is None and _compact(text) not in _SEAT_LABELS]
    return {
        "seat": seat,
        "name": _single(names),
        "stack": _read_amount(fields, f"{seat}.stack", warnings),
        "action": _single(actions),
        "wager": _read_amount(fields, f"{seat}.wager", warnings),
    }


def _table_state(fields):
    warnings = []
    seats = [_seat(fields, seat, warnings) for seat in SEAT_IDS]
    pot = _read_amount(fields, "pot", warnings)
    center_pot = _read_amount(fields, "center_pot", warnings)
    if pot is not None or center_pot is not None:
        warnings.append("pot 为顶部可见读数，center_pot 为下一行读数；入池口径和本街投入请结合画面核对。")

    observed_hero = [_card(fields, key) for key in _HERO_FIELDS]
    observed_board = [_card(fields, key) for key in _BOARD_FIELDS]
    hero_cards = observed_hero if all(observed_hero) and len(set(observed_hero)) == 2 else None
    card_slots = [
        card is not None or fields.get(key, {}).get("card_surface") is True
        for key, card in zip(_BOARD_FIELDS, observed_board)
    ]
    board_count = sum(card_slots)
    street = None
    if (
        board_count in _STREETS and all(card_slots[:board_count])
        and all(fields.get(key, {}).get("empty_felt") is True for key in _BOARD_FIELDS[board_count:])
    ):
        street = _STREETS[board_count]
    prefix = observed_board[:board_count]
    board = prefix if street is not None and all(prefix) and len(set(prefix)) == board_count else None
    if hero_cards is not None and board is not None and set(hero_cards).intersection(board):
        warnings.append("手牌和公共牌出现重复牌，请手动核对。")
        hero_cards = board = None
    if hero_cards is None:
        warnings.append("手牌无法完整确认，请核对牌点和花色。")
    if board is None:
        warnings.append("公共牌无法完整确认，请核对牌点、花色和公共牌数量。")
    if not any(player["action"] or player["stack"] is not None for player in seats):
        warnings.append("未读到座位动作或筹码，请检查截图裁切和牌桌布局。")

    return {
        "seats": seats,
        "vacant_seats": [
            seat for seat in SEAT_IDS if any(
                _compact(text) in _VACANT_LABELS
                for text in _texts(fields, f"{seat}.label") + _texts(fields, f"{seat}.status")
            )
        ],
        "hero_cards": hero_cards,
        "board": board,
        "street": street,
        "observed_hero_cards": observed_hero,
        "observed_board_cards": observed_board,
        "pot": pot,
        "center_pot": center_pot,
        "evidence": {
            key: observation
            for key, observation in fields.items()
            if observation.get("observations") or observation.get("empty_felt") or observation.get("card_surface")
            or observation.get("corner_card")
        },
        "warnings": warnings,
    }


def _read_vision(path, viewport, regions):
    swift = shutil.which("swift")
    if swift is None:
        raise RecognitionError("未找到 swift，截图识别需要安装 Xcode Command Line Tools。")

    script = Path(__file__).with_name("ocr.swift")
    request = {"path": str(path.resolve()), "viewport": viewport, "regions": regions}
    try:
        result = subprocess.run(
            [swift, str(script)], input=json.dumps(request), capture_output=True, text=True,
            encoding="utf-8", timeout=90, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RecognitionError("截图识别超时。") from error
    except OSError as error:
        raise RecognitionError(f"无法启动截图识别：{error}") from error

    if result.returncode != 0:
        context = result.stderr.strip() or f"退出状态 {result.returncode}"
        raise RecognitionError(f"截图识别失败：{context}")
    try:
        fields = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RecognitionError("截图识别程序返回了无效 JSON。") from error
    if not isinstance(fields, dict):
        raise RecognitionError("截图识别程序返回了无效数据。")
    return fields


def _open_image(source):
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RecognitionError("自动定位或屏幕抓取需要 Pillow，请安装 .[live] 或 .[ocr]") from exc
    if isinstance(source, Image.Image):
        return source.convert("RGB")
    with Image.open(source) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _fields(source, viewport, regions, backend=None):
    portable = backend == "portable" or (
        backend is None and (sys.platform != "darwin" or getattr(sys, "frozen", False))
    )
    if portable:
        from .windows_ocr import read_fields

        return read_fields(source, viewport=viewport, regions=regions)
    if isinstance(source, Path):
        return _read_vision(source, viewport, regions)
    with TemporaryDirectory(prefix="deep-stack-ocr-") as folder:
        path = Path(folder) / "frame.png"
        source.save(path, compress_level=1)
        return _read_vision(path, viewport, regions)


def screen_image():
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RecognitionError("屏幕抓取需要 Pillow，请安装 .[live] 或 .[ocr]") from exc
    return ImageGrab.grab()


def _has_cardback(image):
    """Require a red face with a bright edge in the seat's cardback crop."""
    image = image.resize((20, 20))
    pixels = image.load()
    red_rows = white = 0
    for y in range(20):
        red_count = 0
        for x in range(20):
            red, green, blue = pixels[x, y]
            red_count += red >= 90 and red >= green + 38 and red >= blue + 32
            white += min(red, green, blue) >= 175 and max(red, green, blue) - min(red, green, blue) <= 70
        red_rows += red_count >= 9
    return red_rows >= 9 and white >= 12


def _region_image(image, bounds):
    x, y, width, height = bounds
    return image.crop((x, y, x + width, y + height))


def _popup_badge(image, profile, viewport):
    bounds = pixel_regions(profile, viewport).get("player_popup_id_label")
    if bounds is None:
        return False
    pixels = list(_region_image(image, bounds).resize((16, 16)).get_flattened_data())
    amber = sum(r >= 125 and 18 <= r - g <= 115 and 18 <= g - b <= 110 for r, g, b in pixels)
    return amber >= 32


def _vacancy_marker(image):
    """Require the thin gray cross centered on the empty green seat."""
    pixels = image.resize((40, 20)).load()

    def bright(x, y):
        pixel = pixels[x, y]
        return min(pixel) >= 100 and max(pixel) - min(pixel) <= 55

    horizontal = max(sum(bright(x, y) for x in range(14, 26)) for y in range(8, 13))
    vertical = max(sum(bright(x, y) for y in range(3, 17)) for x in range(18, 23))
    corners = sum(bright(x, y) for x in (13, 16, 24, 27) for y in (3, 6, 14, 17))
    return horizontal >= 10 and vertical >= 12 and corners <= 2


def recognize(image, *, viewport=None, profile=None, auto_viewport=False, live=False, backend=None,
              popup_fallback=None):
    """Read visible table fields from a saved screenshot or a Pillow image."""
    try:
        viewport = validate_viewport(viewport)
    except ValueError as exc:
        raise RecognitionError(str(exc)) from exc
    if isinstance(image, (str, Path)):
        image = Path(image).expanduser()
        if not image.is_file():
            raise RecognitionError(f"找不到截图文件：{image}")
    profile = profile or (live_profile() if live else default_profile())
    has_pixels = not isinstance(image, Path) or auto_viewport or live
    if has_pixels:
        image = _open_image(image)
        if auto_viewport and viewport is None:
            viewport = detect_viewport(image)
            if viewport is None and live and popup_fallback:
                try:
                    fallback = validate_viewport(popup_fallback, image.size)
                except ValueError:
                    fallback = None
                if fallback and _popup_badge(image, profile, fallback):
                    viewport = fallback
            if viewport is None:
                raise RecognitionError("未定位到单一横向绿色牌桌，请核对可见窗口或指定 --viewport")
        try:
            viewport = validate_viewport(viewport, image.size) or (0, 0, *image.size)
        except ValueError as exc:
            raise RecognitionError(str(exc)) from exc

    warnings = layout_warnings(profile, viewport) if viewport else []
    if live and warnings:
        raise RecognitionError(warnings[0])
    if live and backend is None and find_spec("rapidocr") and find_spec("onnxruntime"):
        backend = "portable"
    popup_badge = live and _popup_badge(image, profile, viewport)
    regions = {
        key: bounds for key, bounds in profile.regions.items()
        if bounds["kind"] != "marker" and (key not in POPUP_REGIONS or popup_badge)
    }
    fields = _fields(image, viewport, regions, backend)
    result = _table_state(fields)
    if viewport is not None:
        result["viewport"] = list(viewport)
        result["warnings"].extend(warnings)
        result["layout_mismatch"] = bool(warnings)
    if live:
        result["cardbacks"] = []
        for key, bounds in pixel_regions(profile, viewport).items():
            seat, _, field = key.partition(".")
            if field == "cardback" and _has_cardback(_region_image(image, bounds)):
                result["cardbacks"].append(seat)
            if field == "avatar" and _vacancy_marker(_region_image(image, bounds)):
                if seat not in result["vacant_seats"]:
                    result["vacant_seats"].append(seat)
        result["popup_open"] = bool(popup_badge)
        popup = parse_popup(fields, badge=True) if popup_badge else None
        result["player_popup"] = asdict(popup) if popup is not None else None
    return result


def read_player_id(path, *, viewport=None):
    """Read a stable ID from a tight screenshot of the visible profile ID row."""
    path = Path(path).expanduser()
    viewport = validate_viewport(viewport)
    fields = _fields(path, viewport, {
        "player_id": {"x": 0, "y": 0, "width": 1, "height": 1, "kind": "label"},
    })
    texts = tuple(_compact(text) for text in _texts(fields, "player_id"))
    ids = {
        match[1] for text in texts
        if (match := re.fullmatch(r"(?:id|玩家id|用户id)[:：#]?([0-9]{4,24})", text.strip("()[]")))
    }
    if not ids and any(text.rstrip(":：#") in {"id", "玩家id", "用户id"} for text in texts):
        ids = {text for text in texts if re.fullmatch(r"[0-9]{4,24}", text)}
    if len(ids) != 1:
        raise RecognitionError("未读到唯一的 ID，请裁到头像弹窗的 ID 整行，或手工输入")
    return next(iter(ids))
