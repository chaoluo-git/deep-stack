from importlib.util import find_spec
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deep_stack.layout import default_profile, pixel_regions
from deep_stack.windows_ocr import WindowsOCRError, _generic_suit, read_fields


_HAS_OCR = all(find_spec(name) is not None for name in ("rapidocr", "onnxruntime", "PIL"))
_TEXT = {
    "hero.label": "Alice",
    "hero.stack": "800",
    "right_upper.label": "Call",
    "right_upper.stack": "420",
    "right_upper.wager": "20",
    "pot": "240",
    "center_pot": "220",
}
_CARDS = {
    "hero_card_0": "As",
    "hero_card_1": "Kh",
    "board_card_0": "7d",
    "board_card_1": "2c",
    "board_card_2": "Qc",
}

def _font(paths, size):
    from PIL import ImageFont

    for path in paths:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return None


def _standard_font(size):
    from PIL import ImageFont

    return ImageFont.load_default(size)


def _chinese_font(size):
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return _font((
        windows / "msyh.ttc", windows / "simsun.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
    ), size)


def write_ocr_fixture(path, *, size=(1512, 735), header=0, chinese=False):
    """Create an original synthetic screenshot for OCR and frozen CLI smoke tests."""
    from PIL import Image, ImageDraw

    width, height = size
    screenshot = Image.new("RGB", (width, height + header), "#d3d8df")
    draw = ImageDraw.Draw(screenshot)
    draw.rectangle((0, header, width - 1, height + header - 1), fill=(10, 95, 65))
    regions = pixel_regions(default_profile(), (0, header, width, height))
    labels = dict(_TEXT)
    if chinese:
        labels["left_upper.label"] = "加注"
    for name, text in labels.items():
        x, y, w, h = regions[name]
        font_size = max(12, round(h * 0.62))
        font = _chinese_font(font_size) if name == "left_upper.label" else _standard_font(font_size)
        draw.text((x + w // 2, y + h // 2), text, font=font, fill="white", anchor="mm")

    for name, card in _CARDS.items():
        x, y, w, h = regions[name]
        draw.rectangle((x, y, x + w - 1, y + h - 1), fill="#fcfcfc")
        rank, suit = card
        color = "#cc1538" if suit in {"d", "h"} else "#151515"
        font = _standard_font(max(12, round(h * 0.32)))
        left, top, right, bottom = draw.textbbox((0, 0), rank, font=font)
        draw.text((x + w * 0.24 - (left + right) / 2, y + h * 0.16 - (top + bottom) / 2),
                  rank, font=font, fill=color)
        size = min(round(w * 0.43), round(h * 0.30))
        shape = _generic_suit(suit, (size, size)).point([0] * 128 + [255] * 128)
        screenshot.paste(color, (round(x + w * 0.24 - size / 2), round(y + h * 0.44 - size / 2)), shape)
    screenshot.save(path)


@unittest.skipUnless(_HAS_OCR, "requires optional Portable OCR dependencies")
class PortableOCRTests(unittest.TestCase):
    def test_real_cpu_reads_two_screenshot_sizes_and_header(self):
        from deep_stack.recognition import _table_state

        for size, header in [((1512, 735), 0), ((1320, 680), 54)]:
            with (
                self.subTest(size=size), TemporaryDirectory() as directory,
                patch("requests.sessions.Session.request", side_effect=AssertionError("OCR must run offline")),
            ):
                path = Path(directory) / "牌桌.png"
                write_ocr_fixture(path, size=size, header=header)
                fields = read_fields(path, viewport=(0, header, *size), regions=default_profile().regions)
                table = _table_state(fields)
                seats = {seat["seat"]: seat for seat in table["seats"]}
                self.assertEqual(seats["hero"]["stack"], 800)
                self.assertEqual(seats["hero"]["name"], "Alice")
                self.assertEqual(seats["right_upper"]["action"], "call")
                self.assertEqual(seats["right_upper"]["stack"], 420)
                self.assertEqual(seats["right_upper"]["wager"], 20)
                self.assertEqual((table["pot"], table["center_pot"]), (240, 220))
                self.assertTrue(fields["board_card_4"]["empty_felt"])
                self.assertEqual(table["street"], "flop")
                cards = {key: value for key, value in fields.items() if "_card_" in key}
                self.assertEqual(table["hero_cards"], ["As", "Kh"], cards)
                self.assertEqual(table["board"], ["7d", "2c", "Qc"], cards)

    def test_reads_chinese_with_cpu_model(self):
        from deep_stack.recognition import _table_state

        if _chinese_font(16) is None:
            self.skipTest("CJK screenshot fixture needs an installed Chinese font")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "牌桌.png"
            write_ocr_fixture(path, chinese=True)
            table = _table_state(read_fields(path, regions=default_profile().regions))
        seats = {seat["seat"]: seat for seat in table["seats"]}
        self.assertEqual(seats["left_upper"]["action"], "raise")

    def test_wide_action_region_distinguishes_the_button_from_a_checkbox(self):
        from PIL import Image, ImageDraw

        from deep_stack.live import _control

        image = Image.new("RGB", (828, 66), (22, 110, 68))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((530, 8, 752, 58), radius=6, fill="#14614e")
        font = _standard_font(29)
        draw.text((202, 33), "Auto Check", font=font, fill="white", anchor="mm")
        draw.text((639, 33), "Call 50", font=font, fill="white", anchor="mm")
        key = "hero_passive_button"
        regions = {key: {"x": 0, "y": 0, "width": 1, "height": 1, "kind": "label"}}
        evidence = read_fields(image, regions=regions)
        self.assertEqual(len(evidence[key]["observations"]), 2)
        self.assertEqual(_control({"evidence": evidence}, key, ("check", "call")), ("call", 50))

    def test_viewport_outside_or_corrupt_image_is_explained(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "牌桌.png"
            write_ocr_fixture(path)
            with self.assertRaisesRegex(WindowsOCRError, "超出图片边界"):
                read_fields(path, viewport=(0, 0, 2000, 1000), regions={})
            path.write_bytes(b"broken")
            with self.assertRaisesRegex(WindowsOCRError, "无法解码截图"):
                read_fields(path, regions={})
