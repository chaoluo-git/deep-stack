"""Short-lived visual evidence linking a visible profile popup to a seat."""

from collections.abc import Iterable, Mapping, Sequence
from math import sqrt


_SIZE = 24
_RADIUS = 9.5
_CENTER = (_SIZE - 1) / 2
_CORE = tuple(
    y * _SIZE + x
    for y in range(_SIZE)
    for x in range(_SIZE)
    if (x - _CENTER) ** 2 + (y - _CENTER) ** 2 <= _RADIUS**2
)
# The edge is excluded because one view may use a different corner mask.
# A timer or fold label can cover the middle, leaving both outer bands intact.
_TOP = tuple(index for index in _CORE if index // _SIZE <= 7)
_BOTTOM = tuple(index for index in _CORE if index // _SIZE >= 16)
_OFFSETS = tuple(y * _SIZE + x for y in (0, -1, 1) for x in (0, -1, 1))
_MINIMUM_SCORE = 0.90
_MINIMUM_LEAD = 0.08

AvatarSignature = tuple[int, ...]


def _has_variation(pixels, indices, minimum_variance):
    size = len(indices)
    total = sum(pixels[index] for index in indices)
    squared = sum(pixels[index] ** 2 for index in indices)
    return size * squared - total * total >= minimum_variance * size * size


def avatar_signature(image) -> AvatarSignature | None:
    """Reduce a tightly cropped avatar to luminance samples for this session."""
    from PIL import Image

    width, height = image.size
    if min(width, height) < 12 or not 0.72 <= width / height <= 1.4:
        return None
    pixels = tuple(image.convert("L").resize((_SIZE, _SIZE), Image.Resampling.BILINEAR).get_flattened_data())
    return pixels if _has_variation(pixels, _CORE, 25) else None


def _correlation(popup, candidate, indices, offset):
    # Zero-mean correlation tolerates the dialog scrim darkening the table.
    first = second = first_squared = second_squared = products = 0
    for i in indices:
        a, b = popup[i], candidate[i + offset]
        first += a
        second += b
        first_squared += a * a
        second_squared += b * b
        products += a * b
    size = len(indices)
    first_variance = size * first_squared - first * first
    second_variance = size * second_squared - second * second
    if min(first_variance, second_variance) < size * size * 9:
        return -1.0
    return (size * products - first * second) / sqrt(first_variance * second_variance)


def _similarity(popup, candidate):
    best = -1.0
    for offset in _OFFSETS:
        best = max(best, _correlation(popup, candidate, _CORE, offset))
        top = _correlation(popup, candidate, _TOP, offset)
        bottom = _correlation(popup, candidate, _BOTTOM, offset)
        if min(top, bottom) >= 0.80:
            # Demand agreement on both exposed bands for the occluded path.
            best = max(best, ((top + bottom) / 2) * 0.98)
    return best


def match_avatar(
    popup: Sequence[int] | None,
    candidates: Mapping[str, Iterable[Sequence[int] | None]],
) -> str | None:
    """Return a uniquely strong seat match among its recent visible avatars."""
    size = _SIZE * _SIZE
    if popup is None or len(popup) != size:
        return None

    scores = []
    for seat, views in candidates.items():
        score = max(
            (_similarity(popup, view) for view in views if view is not None and len(view) == size),
            default=None,
        )
        if score is not None:
            scores.append((score, seat))
    scores.sort(reverse=True)
    if len(scores) < 2 or scores[0][0] < _MINIMUM_SCORE or scores[0][0] - scores[1][0] < _MINIMUM_LEAD:
        return None
    return scores[0][1]


def avatars_conflict(first: Sequence[int] | None, second: Sequence[int] | None) -> bool:
    """Detect an unmistakable portrait change despite a central action overlay."""
    size = _SIZE * _SIZE
    if first is None or second is None or len(first) != size or len(second) != size:
        return False
    for band in (_TOP, _BOTTOM):
        if not all(_has_variation(image, band, 100) for image in (first, second)):
            return False
        if any(_correlation(first, second, band, offset) >= 0.45 for offset in _OFFSETS):
            return False
    return True
