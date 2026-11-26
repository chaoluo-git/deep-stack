"""Offline CPU OCR for the same normalized crops used by the Vision backend."""

from functools import lru_cache
from math import ceil, floor, isfinite
import os
from pathlib import Path
from threading import Lock


class WindowsOCRError(ValueError):
    """The screenshot or the Portable OCR installation could not be read."""


_RECOGNITION_MODEL = "PP-OCRv6_rec_small.onnx"
_DETECTION_MODEL = "PP-OCRv6_det_small.onnx"
_OCR_LOCK = Lock()
_SUIT_SYMBOLS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}


@lru_cache(maxsize=1)
def _engine():
    try:
        import onnxruntime  # The CPU runtime must be installed independently of RapidOCR.
        import rapidocr
    except (ImportError, OSError) as exc:
        raise WindowsOCRError(
            "截图识别需要 RapidOCR 和 ONNX Runtime。请重新解压 Windows 发行包或按 README 安装依赖。"
        ) from exc

    models = Path(rapidocr.__file__).resolve().parent / "models"
    recognition = models / _RECOGNITION_MODEL
    detection = models / _DETECTION_MODEL
    if not recognition.is_file() or not detection.is_file():
        raise WindowsOCRError("缺少内置 RapidOCR 模型。请重新解压完整发行包或重新安装识别依赖。")
    if "CPUExecutionProvider" not in onnxruntime.get_available_providers():
        raise WindowsOCRError("ONNX Runtime 未提供 CPU 执行后端。")

    return rapidocr.RapidOCR(params={
        # Explicit on-disk model paths keep recognition independent of any model server.
        "Rec.model_path": str(recognition),
        "Det.model_path": str(detection),
        "Det.limit_side_len": 320,
        "Global.log_level": "error",
        "Global.use_preprocess_img": False,
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })


def _crop(image, viewport, region):
    left, top, width, height = viewport
    x = region["x"]
    y = region["y"]
    box = (
        max(left, left + floor(width * x)),
        max(top, top + floor(height * y)),
        min(left + width, left + ceil(width * (x + region["width"]))),
        min(top + height, top + ceil(height * (y + region["height"]))),
    )
    return image.crop(box) if box[2] > box[0] and box[3] > box[1] else None


def _surface(image):
    """Classify an empty green slot or neutral card surface when evidence is strong."""
    import numpy as np

    color = np.asarray(image.resize((32, 32)), dtype=np.int16)
    red, green, blue = color.transpose(2, 0, 1)
    if ((green >= red + 10) & (green >= blue + 6)).mean() >= 0.97:
        return "felt"
    neutral = (color.min(axis=2) >= 96) & (np.ptp(color, axis=2) <= 25)
    # The top-right card corner usually remains clear even on illustrated face cards.
    if neutral.mean() >= 0.83 or (
        neutral.mean() >= 0.55 and neutral[:8, 19:].mean() >= 0.90
    ):
        return "card"
    return None


def _appears_blank(image):
    import numpy as np

    # Limit downsampling so small one-digit wagers survive the check.
    gray = np.asarray(image.convert("L").resize((64, 24)), dtype=np.int16)
    low, high = np.percentile(gray, (1, 99))
    return high - low < 20


def _ink_mask(mask):
    import numpy as np
    from PIL import Image

    ys, xs = np.nonzero(mask)
    if len(xs) < 12:
        return None
    left, right = int(xs.min()), int(xs.max()) + 1
    top, bottom = int(ys.min()), int(ys.max()) + 1
    if left == 0 or top == 0 or right == mask.shape[1] or bottom == mask.shape[0]:
        return None
    selection = mask[top:bottom, left:right]
    height, width = selection.shape
    if min(height, width) < 5 or not 0.65 <= width / height <= 1.65 or not 0.30 <= selection.mean() <= 0.89:
        return None
    shape = Image.fromarray(selection).resize((24, 24), Image.Resampling.NEAREST)
    return np.asarray(shape, dtype=bool)


def _generic_suit(suit, size):
    """Draw a generic playing-card pip, independent of installed fonts."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (64, 64))
    draw = ImageDraw.Draw(image)
    heart = [(8, 23), (9, 16), (14, 10), (21, 8), (27, 10), (32, 16),
             (37, 10), (43, 8), (50, 10), (55, 16), (56, 23), (52, 32),
             (43, 43), (32, 55), (21, 43), (12, 32)]
    stem = [(29, 35), (35, 35), (35, 47), (38, 52), (43, 56),
            (21, 56), (26, 52), (29, 47)]
    if suit == "h":
        draw.polygon(heart, fill=255)
    elif suit == "d":
        draw.polygon([(32, 5), (54, 32), (32, 59), (10, 32)], fill=255)
    elif suit == "s":
        draw.polygon(stem, fill=255)
        draw.polygon([(x, 59 - y) for x, y in heart], fill=255)
    elif suit == "c":
        draw.polygon(stem, fill=255)
        for box in ((21, 6, 43, 29), (8, 22, 32, 45), (32, 22, 56, 45)):
            draw.ellipse(box, fill=255)
    return image.resize(size, Image.Resampling.LANCZOS)


@lru_cache(maxsize=1)
def _suit_masks():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = (
        windows_fonts / "seguisym.ttf",
        Path("/System/Library/Fonts/Apple Symbols.ttf"),
        "DejaVuSans.ttf",
    )
    sizes = (12, 14, 18, 20, 22, 24, 26, 32, 40, 48, 64)
    masks = {suit: [] for suit in _SUIT_SYMBOLS}
    for suit, variants in masks.items():
        for size in range(9, 33):
            shape = _ink_mask(np.asarray(_generic_suit(suit, (size, size))) >= 128)
            if shape is not None:
                variants.append(shape)
    for name in candidates:
        font_masks = {suit: [] for suit in _SUIT_SYMBOLS}
        try:
            for size in sizes:
                font = ImageFont.truetype(str(name), size)
                for suit, symbol in _SUIT_SYMBOLS.items():
                    image = Image.new("L", (100, 100))
                    ImageDraw.Draw(image).text((50, 50), symbol, font=font, fill=255, anchor="mm")
                    mask = _ink_mask(np.asarray(image) >= 128)
                    if mask is not None:
                        font_masks[suit].append(mask)
        except OSError:
            continue
        if all(font_masks.values()):
            for suit, variants in font_masks.items():
                masks[suit].extend(variants)
            break
    return {suit: np.stack(variants) for suit, variants in masks.items()}


def _match_suit(image):
    import cv2
    import numpy as np

    color = np.asarray(image, dtype=np.int16)
    red, green, blue = color.transpose(2, 0, 1)
    references = _suit_masks()
    if not references:
        return None

    matches = []
    inks = (
        ((red >= green + 48) & (red >= blue + 32), ("h", "d"), 0.86, 0.07),
        (color.max(axis=2) <= 105, ("s", "c"), 0.87, 0.04),
    )
    # An edge of the rank or the illustration can enter this crop. Match only
    # separated ink components whose centers could belong to the suit symbol.
    for ink, options, minimum, margin in inks:
        count, labels, stats, centers = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
        for component in range(1, count):
            x, y = centers[component]
            _, top, width, height, area = stats[component]
            if (
                area < 12
                or not 0.16 <= x / image.width <= 0.73
                or not 0.22 <= y / image.height <= 0.67
            ):
                continue
            # At small scales the stem of a club can be split by antialiasing.
            stem = [
                part for part in range(1, count)
                if part != component and stats[part, cv2.CC_STAT_AREA] < area * 0.22
                and abs(centers[part, 0] - x) <= max(1, width * 0.22)
                and y < centers[part, 1] <= top + height + 2
                and stats[part, cv2.CC_STAT_TOP] >= top + height * 0.70
            ]
            mask = _ink_mask(np.isin(labels, [component, *stem]))
            if mask is None:
                continue
            scores = []
            for suit in options:
                variants = references[suit]
                overlap = 2 * (mask & variants).sum(axis=(1, 2)) / (mask.sum() + variants.sum(axis=(1, 2)))
                scores.append((float(overlap.max()), suit))
            scores.sort(reverse=True)
            (overlap, suit), (second, _) = scores
            if overlap >= minimum and overlap - second >= margin:
                matches.append((suit, overlap))
    return matches[0] if len(matches) == 1 else None


def _ocr(image, *, detect=False):
    import numpy as np

    # RapidOCR expects BGR arrays. Use a contiguous copy for inference.
    bgr = np.asarray(image)[:, :, ::-1].copy()
    with _OCR_LOCK:
        output = _engine()(bgr, use_det=detect, use_cls=False, use_rec=True)
    texts, scores = output.txts, output.scores
    if texts is None or scores is None:
        return []

    lines = []
    boxes = output.boxes if detect else None
    for index, (text, score) in enumerate(zip(texts, scores)):
        text = text.strip()
        confidence = float(score)
        if not text or not isfinite(confidence) or confidence < 0.60:
            continue
        line = {"text": text, "confidence": confidence}
        if boxes is not None:
            x, y = np.asarray(boxes[index]).mean(axis=0)
            line["center_x"] = float(x / image.width)
            line["center_y"] = float(y / image.height)
        lines.append(line)
    return lines


def _card_fields(image):
    surface = _surface(image)
    result = {"observations": [], "empty_felt": surface == "felt", "card_surface": surface == "card"}
    if surface == "felt" or min(image.size) < 12 or _appears_blank(image):
        return result

    width, height = image.size
    if surface == "card":
        suit_image = image.crop((0, floor(height * 0.28),
                                 ceil(width * 0.48), ceil(height * 0.66)))
        matched = _match_suit(suit_image)
        if matched is not None:
            suit, overlap = matched
            rank_image = image.crop((floor(width * 0.015), floor(height * 0.015),
                                     ceil(width * 0.56), ceil(height * 0.33)))
            rank = _ocr(rank_image)
            if rank:
                result["corner_card"] = {"suit": suit, "overlap": overlap, "rank_observations": rank}
                return result
    result["observations"] = _ocr(image, detect=True)
    return result


def read_fields(path, *, viewport=None, regions):
    """Read named normalized crop regions from an image file or Pillow image."""
    try:
        from PIL import Image, ImageOps
    except (ImportError, OSError) as exc:
        raise WindowsOCRError("截图识别需要 Pillow。请重新解压 Windows 发行包或按 README 安装依赖。") from exc

    try:
        if isinstance(path, Image.Image):
            screenshot = ImageOps.exif_transpose(path).convert("RGB")
        else:
            with Image.open(path) as source:
                screenshot = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise WindowsOCRError(f"无法解码截图：{exc}") from exc

    viewport = viewport if viewport is not None else (0, 0, *screenshot.size)
    if (
        not isinstance(viewport, (list, tuple)) or len(viewport) != 4
        or any(type(value) is not int for value in viewport)
    ):
        raise WindowsOCRError("截图裁切区域需要四个整数。")
    left, top, width, height = viewport
    if (
        min(left, top) < 0 or min(width, height) <= 0
        or left + width > screenshot.width or top + height > screenshot.height
    ):
        raise WindowsOCRError("截图裁切区域超出图片边界。")

    fields = {}
    try:
        for name, region in regions.items():
            image = _crop(screenshot, viewport, region)
            if image is None:
                continue
            if region["kind"] == "card":
                field = _card_fields(image)
            else:
                # A control crop may contain multiple buttons or adjacent checkboxes.
                detect = name.startswith("hero_") and name.endswith("_button")
                field = {
                    "observations": [] if _appears_blank(image) else _ocr(image, detect=detect),
                    "empty_felt": False,
                    "card_surface": False,
                }
            fields[name] = field
    except WindowsOCRError:
        raise
    except Exception as exc:
        raise WindowsOCRError(f"RapidOCR 识别失败：{exc}") from exc
    return fields
