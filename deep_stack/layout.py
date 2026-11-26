"""Relative bounds for screenshots of the desktop table."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path


_REFERENCE_WIDTH = 3024
_REFERENCE_HEIGHT = 1470
_REFERENCE_TOP = 242
_REFERENCE_RATIO = _REFERENCE_WIDTH / _REFERENCE_HEIGHT
_NARROW_HEIGHT = 1482
_NARROW_RATIO = 1512 / _NARROW_HEIGHT
_CALIBRATION_MARGIN = 0.03

# Wide and split-screen seat centers at the two measured sizes. The last
# value assigns small changes beyond either measured edge to the near side.
_HORIZONTAL_ANCHORS = {
    "left": (314, 182, 0.0),
    "center": (1512, 756, 0.5),
    "right": (2708, 1330, 1.0),
}

# Seat ID, horizontal center, and vertical centers in the reference screenshot.
_SEATS = (
    ("top_left", 1400, 354, 423, 497, (1417, 535)),
    ("top_right", 1624, 354, 423, 497, None),
    ("right_upper", 2708, 459, 528, 603, (2600, 528)),
    ("right_middle", 2708, 722, 792, 867, (2600, 792)),
    ("right_lower", 2708, 1004, 1075, 1147, (2600, 1075)),
    ("hero", 1512, 1312, 1390, 1648, (1632, 1354)),
    ("left_lower", 314, 1004, 1075, 1147, (422, 1075)),
    ("left_middle", 314, 722, 792, 867, (422, 792)),
    ("left_upper", 314, 459, 528, 603, (422, 528)),
)
SEAT_IDS = tuple(seat[0] for seat in _SEATS)
EXTRA_SEAT_IDS = ("top_center", "bottom_center")
SEAT_LABELS = {
    "top_left": "上左", "top_center": "上中", "top_right": "上右",
    "right_upper": "右上", "right_middle": "右中", "right_lower": "右下",
    "hero": "底中（本人）", "bottom_center": "底中", "left_lower": "左下",
    "left_middle": "左中", "left_upper": "左上",
}
HERO_CARD_FIELDS = ("hero_card_0", "hero_card_1")
BOARD_CARD_FIELDS = tuple(f"board_card_{index}" for index in range(5))
HERO_BUTTON_FIELDS = ("hero_fold_button", "hero_passive_button", "hero_aggressive_button")
SPECTATOR_FIELDS = ("spectator_hint_left", "spectator_hint_right")
POPUP_TEXT_FIELDS = ("player_popup", "player_popup_id", "player_popup_id_label", "player_popup_name")
POPUP_NUMBER_FIELDS = (
    "player_popup_id_value", "player_popup_hands", "player_popup_win_rate",
    "player_popup_pot_entry_rate", "player_popup_showdown_win_rate",
)

_REGION_KINDS = {
    **{f"{seat}.{field}": kind for seat in SEAT_IDS + EXTRA_SEAT_IDS for field, kind in (
        ("label", "label"), ("avatar", "label"), ("stack", "number"), ("wager", "number"),
        ("status", "label"), ("cardback", "marker"), ("portrait", "marker"),
        ("dealer_left", "label"), ("dealer_right", "label"), ("blind_label", "label"),
    )},
    **dict.fromkeys(("pot", "center_pot"), "number"),
    **dict.fromkeys(HERO_CARD_FIELDS + BOARD_CARD_FIELDS, "card"),
    **dict.fromkeys(HERO_BUTTON_FIELDS + SPECTATOR_FIELDS + POPUP_TEXT_FIELDS + ("hero_waiting_banner",), "label"),
    **dict.fromkeys(POPUP_NUMBER_FIELDS, "number"),
    **dict.fromkeys(("player_popup_portrait", "player_popup_panel"), "marker"),
    "table_stakes": "label",
}
_COORDINATES = ("x", "y", "width", "height")


def clockwise_seats(known):
    """Return the clockwise physical order of a complete supported seat layout."""
    seats = SEAT_IDS
    if "top_center" in known:
        seats = ("top_center", *seats[2:])
    if "bottom_center" in known:
        seats = tuple("bottom_center" if seat == "hero" else seat for seat in seats)
    return seats if set(seats) == set(known) else None


@dataclass(frozen=True)
class LayoutProfile:
    regions: dict[str, dict]
    reference_aspect_ratio: float


def default_profile() -> LayoutProfile:
    """Return regions for the observed nine-seat desktop layout."""
    regions = {}

    def add(key, x, y, width, height):
        regions[key] = {
            "x": x / _REFERENCE_WIDTH,
            "y": (y - _REFERENCE_TOP) / _REFERENCE_HEIGHT,
            "width": width / _REFERENCE_WIDTH,
            "height": height / _REFERENCE_HEIGHT,
            "kind": _REGION_KINDS[key],
        }

    for seat, x, name, avatar, stack, wager in _SEATS:
        add(f"{seat}.label", x - 88, name - 22, 176, 44)
        add(f"{seat}.avatar", x - 46, avatar - 20, 92, 40)
        add(f"{seat}.stack", x - 72, stack - 24, 144, 48)
        if wager is not None:
            wager_x, wager_y = wager
            add(f"{seat}.wager", wager_x - 37, wager_y - 20, 74, 40)

    add("pot", 1407, 589, 210, 43)
    add("center_pot", 1440, 633, 172, 40)
    for index, key in enumerate(HERO_CARD_FIELDS):
        add(key, 1430 + index * 82, 1471, 78, 115)
    for index, key in enumerate(BOARD_CARD_FIELDS):
        add(key, 1309 + index * 82, 857, 78, 113)
    return LayoutProfile(regions, _REFERENCE_RATIO)


def profile_template(
    profile: LayoutProfile | None = None, *, include_unconfigured: bool = True,
) -> dict:
    """Export a profile that can be edited and loaded again."""
    default = default_profile()
    profile = profile if profile is not None else default
    return {
        "version": 1,
        "reference_aspect_ratio": profile.reference_aspect_ratio,
        "regions": {
            key: profile.regions[key].copy() if key in profile.regions else None
            for key in _REGION_KINDS
            if include_unconfigured or key in profile.regions or key in default.regions
        },
    }


def _unique_object(items):
    obj = {}
    for key, value in items:
        if key in obj:
            raise ValueError(f"布局 JSON 重复键：{key}")
        obj[key] = value
    return obj


def load_profile(path: str | Path) -> LayoutProfile:
    """Read normalized region overrides from a version-1 JSON file."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except ValueError as error:
        raise ValueError(f"布局文件 {path} 无法读取：{error}") from error
    return parse_profile(data)


def parse_profile(data: object) -> LayoutProfile:
    if (
        not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
        or data.keys() - {"version", "reference_aspect_ratio", "regions"}
    ):
        raise ValueError("布局需要版本 1 的 JSON 对象和 regions 字段")
    overrides = data.get("regions")
    if not isinstance(overrides, dict):
        raise ValueError("布局的 regions 必须是对象")

    ratio = data.get("reference_aspect_ratio", _REFERENCE_RATIO)
    if type(ratio) not in (int, float) or not 0 < ratio <= 100 or not math.isfinite(ratio):
        raise ValueError("布局的 reference_aspect_ratio 必须为正的有限宽高比")

    regions = default_profile().regions
    for key, bounds in overrides.items():
        kind = _REGION_KINDS.get(key)
        if kind is None:
            raise ValueError(f"未知的布局区域：{key}")
        if bounds is None:
            regions.pop(key, None)
            continue
        if (
            not isinstance(bounds, dict)
            or bounds.keys() - {*_COORDINATES, "kind"}
            or bounds.get("kind", kind) != kind
        ):
            raise ValueError(f"布局区域 {key} 的格式或 kind 不符")
        values = tuple(bounds.get(coordinate) for coordinate in _COORDINATES)
        if any(type(value) not in (int, float) or not 0 <= value <= 1 for value in values):
            raise ValueError(f"布局区域 {key} 的坐标必须是 0 到 1 之间的有限数")
        x, y, width, height = values
        if width <= 0 or height <= 0 or x + width > 1 + 1e-12 or y + height > 1 + 1e-12:
            raise ValueError(f"布局区域 {key} 必须完全位于牌桌内，且宽高为正数")
        regions[key] = dict(zip(_COORDINATES, values), kind=kind)
    if not regions:
        raise ValueError("布局须至少保留一个识别区域")
    return LayoutProfile(regions, float(ratio))


def validate_viewport(
    viewport: tuple[int, int, int, int] | list[int] | None,
    image_size: tuple[int, int] | None = None,
) -> tuple[int, int, int, int] | None:
    """Validate screenshot-pixel bounds using the optional decoded size."""
    if viewport is None:
        return None
    if (
        not isinstance(viewport, (tuple, list)) or len(viewport) != 4
        or any(type(value) is not int for value in viewport)
    ):
        raise ValueError("viewport 需要整数 X,Y,W,H")
    x, y, width, height = viewport
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError("viewport 左上角坐标须非负，且宽高为正数")
    if image_size is not None and (x + width > image_size[0] or y + height > image_size[1]):
        raise ValueError("viewport 超出截图边界")
    return x, y, width, height


def pixel_regions(profile: LayoutProfile, viewport: tuple[int, int, int, int]) -> dict:
    """Map relative bounds to screenshot pixels for inspecting the active layout."""
    x, y, width, height = validate_viewport(viewport)
    regions = {}
    for key, bounds in profile.regions.items():
        left = x + math.floor(bounds["x"] * width)
        top = y + math.floor(bounds["y"] * height)
        right = min(x + width, x + math.ceil((bounds["x"] + bounds["width"]) * width))
        bottom = min(y + height, y + math.ceil((bounds["y"] + bounds["height"]) * height))
        regions[key] = (left, top, right - left, bottom - top)
    return regions


def responsive_profile(profile: LayoutProfile, viewport: tuple[int, int, int, int]) -> LayoutProfile | None:
    """Project the built-in wide layout onto a measured desktop table size."""
    _, _, width, height = validate_viewport(viewport)
    ratio = width / height
    if profile.reference_aspect_ratio != _REFERENCE_RATIO or not (
        _NARROW_RATIO * (1 - _CALIBRATION_MARGIN) <= ratio <= _REFERENCE_RATIO * (1 + _CALIBRATION_MARGIN)
    ):
        return None
    if ratio == _REFERENCE_RATIO:
        return profile

    measured = min(_REFERENCE_RATIO, max(_NARROW_RATIO, ratio))
    wide_share = (measured - _NARROW_RATIO) / (_REFERENCE_RATIO - _NARROW_RATIO)
    regions = {}
    for key, bounds in profile.regions.items():
        side = key.split("_", 1)[0]
        wide_x, narrow_x, edge = _HORIZONTAL_ANCHORS.get(side, _HORIZONTAL_ANCHORS["center"])
        reference_anchor = wide_x / _REFERENCE_HEIGHT
        anchor = narrow_x / _NARROW_HEIGHT * (1 - wide_share) + reference_anchor * wide_share
        anchor += (ratio - measured) * edge
        # Keep the card and text crops proportional to table height. Relocate
        # their seat group instead of compressing them into the smaller width.
        x = (anchor + bounds["x"] * _REFERENCE_RATIO - reference_anchor) / ratio
        region_width = bounds["width"] * _REFERENCE_RATIO / ratio
        region = bounds
        if key in HERO_CARD_FIELDS:
            # The split-screen cards are slightly narrower and spaced farther apart.
            narrow_pixels = (1 - wide_share) / _REFERENCE_HEIGHT
            x += (2 if key == HERO_CARD_FIELDS[0] else 6) * narrow_pixels / ratio
            region_width -= 3 * narrow_pixels / ratio
            region = {**bounds, "y": bounds["y"] + narrow_pixels, "height": bounds["height"] - 3 * narrow_pixels}
        right = min(1.0, x + region_width)
        x = max(0.0, x)
        if right > x:
            regions[key] = {**region, "x": x, "width": right - x}
    return LayoutProfile(regions, ratio)


def layout_warnings(profile: LayoutProfile, viewport: tuple[int, int, int, int]) -> list[str]:
    """Warn when a resize may have changed the game's responsive layout."""
    _, _, width, height = validate_viewport(viewport)
    ratio = width / height
    expected = profile.reference_aspect_ratio
    if max(ratio / expected, expected / ratio) <= 1.12:
        return []
    return [f"截图牌桌宽高比 {ratio:.2f} 偏离布局标定的 {expected:.2f}，"
            "请核对座位和牌面位置，必要时加载对应的布局文件。"]


def _felt(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return green >= 48 and green >= red + 14 and green >= blue + 5


def _green_candidates(image) -> list[tuple[int, int, int, int]]:
    width, height = image.size
    pixels = image.load()
    mask = bytearray(_felt(pixels[x, y]) for y in range(height) for x in range(width))
    remaining = mask[:]
    candidates = []
    for start, present in enumerate(remaining):
        if not present:
            continue
        remaining[start] = 0
        stack = [start]
        left = right = start % width
        top = bottom = start // width
        count = 0
        while stack:
            index = stack.pop()
            y, x = divmod(index, width)
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            count += 1
            neighbors = (
                index - 1 if x else index,
                index + 1 if x < width - 1 else index,
                index - width if y else index,
                index + width if y < height - 1 else index,
            )
            for neighbor in neighbors:
                if remaining[neighbor]:
                    remaining[neighbor] = 0
                    stack.append(neighbor)

        span, depth = right - left + 1, bottom - top + 1
        if span < max(16, width // 7) or depth < max(16, height // 7):
            continue
        if count * 100 < span * depth * 65:
            continue
        # The upper two-thirds contain the table felt except for controls and
        # cards. Reject full rows covered by an intrusive window or panel.
        upper = [sum(mask[y * width + left:y * width + right + 1])
                 for y in range(top, top + depth * 2 // 3)]
        narrow = sum(hits * 100 < span * 72 for hits in upper)
        if narrow * 20 <= len(upper):
            candidates.append((left, top, right + 1, bottom + 1))
    return candidates


def _step_samples(start: int, end: int, count: int) -> list[int]:
    return [start + (end - start) * (2 * index + 1) // (2 * count) for index in range(count)]


def _window_bottom(image, left: int, top: int, right: int, felt_bottom: int) -> int | None:
    width, height = image.size
    depth = felt_bottom - top
    limit = min(height, felt_bottom + max(3, depth // 5))
    if left == 0 and right == width:
        return height if height <= limit else None
    if felt_bottom >= height - 1:
        return height

    pixels = image.load()
    # The lower controls can be darker than the felt. Across the window edge
    # their color still changes against the surrounding desktop.
    edges = []
    if left > 1:
        edges.append((left + 2, left - 2))
    if right < width - 1:
        edges.append((right - 3, right + 1))
    if not edges:
        return None

    for row in range(max(top, felt_bottom - 1), limit):
        distinct = sum(
            max(abs(a - b) for a, b in zip(pixels[inside, row], pixels[outside, row])) >= 22
            for inside, outside in edges
        )
        if not distinct:
            return row if row >= felt_bottom + max(2, depth // 100) else None
    return height if limit == height else None


def detect_viewport(image, *, window: bool = False) -> tuple[int, int, int, int] | None:
    """Find one coherent green table including its bottom control area.

    A single-window capture supplies the lower boundary directly. Its alpha
    channel may contain a translucent window shadow, which is outside the
    browser. Coordinates are always relative to the original Pillow image.
    """
    if not window:
        return _detect_viewport(image, window=False)

    left, top = 0, 0
    if "A" in image.getbands():
        opaque = image.getchannel("A").point(lambda alpha: 255 if alpha >= 248 else 0)
        bounds = opaque.getbbox()
        if bounds is None:
            return None
        left, top, _, _ = bounds
        if bounds != (0, 0, *image.size):
            image = image.crop(bounds)

    viewport = _detect_viewport(image, window=True)
    if viewport is None:
        return None
    x, y, width, height = viewport
    return left + x, top + y, width, height


def _detect_viewport(image, *, window: bool) -> tuple[int, int, int, int] | None:
    width, height = image.size
    if width < 100 or height < 80:
        return None

    columns, rows = min(width, 200), min(height, 200)
    thumbnail = image.resize((columns, rows), resample=0).convert("RGB")
    candidates = _green_candidates(thumbnail)
    if len(candidates) != 1:
        return None

    rough_left, rough_top, rough_right, rough_bottom = candidates[0]
    image = image if image.mode == "RGB" else image.convert("RGB")
    pixels = image.load()
    step_x = math.ceil(width / columns)
    step_y = math.ceil(height / rows)
    left, right = rough_left * width / columns, rough_right * width / columns
    top, bottom = rough_top * height / rows, rough_bottom * height / rows

    def near(value, step, limit):
        return range(max(0, math.floor(value) - 2 * step), min(limit, math.ceil(value) + 2 * step + 1))

    region_rows = _step_samples(int(top + (bottom - top) * 0.08), int(top + (bottom - top) * 0.40), 30)
    left = next((x for x in near(left, step_x, width)
                 if sum(_felt(pixels[x, y]) for y in region_rows) >= 24), None)
    right_edge = next((x for x in reversed(near(right, step_x, width))
                       if sum(_felt(pixels[x, y]) for y in region_rows) >= 24), None)
    if left is None or right_edge is None or right_edge - left < 98:
        return None
    right = right_edge + 1

    region_columns = _step_samples(left, right, 50)
    top = next((y for y in near(top, step_y, height)
                if sum(_felt(pixels[x, y]) for x in region_columns) >= 40), None)
    if top is None:
        return None
    if window:
        # Curved table edges can lose most green columns near the lowest point.
        # Look up into the last sixth of the window's content, which bounds the
        # non-green footer to one fifth of the felt height.
        first_row = max(top + 60, (5 * height + top + 5) // 6 - 1)
        last_row = min(height - 1, math.ceil(bottom) + 2 * step_y)
        if not any(
            sum(_felt(pixels[x, y]) for x in region_columns) >= 16
            for y in range(last_row, first_row - 1, -1)
        ):
            return None
        bottom = height
    else:
        center_columns = _step_samples(left + (right - left) * 2 // 5, left + (right - left) * 3 // 5, 30)
        felt_edge = next((y for y in reversed(near(bottom, step_y, height))
                          if sum(_felt(pixels[x, y]) for x in center_columns) >= 24), None)
        if felt_edge is None or felt_edge - top < 60:
            return None
        bottom = _window_bottom(image, left, top, right, felt_edge + 1)
        if bottom is None:
            return None
    return left, top, right - left, bottom - top
