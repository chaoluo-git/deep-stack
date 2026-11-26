"""Calibration and image-boundary tests for desktop screenshots."""

from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deep_stack.layout import (
    default_profile,
    detect_viewport,
    layout_warnings,
    load_profile,
    parse_profile,
    pixel_regions,
    profile_template,
    validate_viewport,
)


class LayoutTests(unittest.TestCase):
    def test_scales_existing_layout_inside_selected_window(self):
        profile = default_profile()
        baseline = pixel_regions(profile, (0, 242, 3024, 1470))
        self.assertEqual(baseline["board_card_0"], (1309, 857, 78, 113))
        half = pixel_regions(profile, (12, 61, 1512, 735))
        self.assertEqual(half["board_card_0"], (666, 368, 40, 57))
        self.assertEqual(layout_warnings(profile, (12, 61, 1512, 735)), [])
        self.assertIn("宽高比", layout_warnings(profile, (40, 6, 1280, 800))[0])

    def test_profile_accepts_normalized_overrides_and_new_live_fields(self):
        profile = parse_profile({
            "version": 1,
            "reference_aspect_ratio": 1.5,
            "regions": {
                "hero_passive_button": {"x": 0.75, "y": 0.875, "width": 0.125, "height": 0.125},
                "top_right.status": {"x": 0.5, "y": 0.25, "width": 0.125, "height": 0.125},
                "hero.wager": {"x": 0.25, "y": 0.125, "width": 0.25, "height": 0.125},
                "hero.cardback": {"x": 0.25, "y": 0.125, "width": 0.25, "height": 0.125},
                "center_pot": None,
            },
        })
        boxes = pixel_regions(profile, (10, 20, 1200, 800))
        self.assertEqual(boxes["hero_passive_button"], (910, 720, 150, 100))
        self.assertEqual(profile.regions["hero.wager"]["kind"], "number")
        self.assertEqual(profile.regions["hero.cardback"]["kind"], "marker")
        self.assertEqual(profile.regions["top_right.status"]["kind"], "label")
        self.assertEqual(profile.regions["pot"], default_profile().regions["pot"])
        self.assertNotIn("center_pot", boxes)
        self.assertEqual(layout_warnings(profile, (10, 20, 1200, 800)), [])

        for expanded in (False, True):
            with self.subTest(expanded=expanded), TemporaryDirectory() as directory:
                path = Path(directory) / "配置.json"
                path.write_text(json.dumps(profile_template(profile, include_unconfigured=expanded)),
                                encoding="utf-8-sig")
                self.assertEqual(load_profile(path), profile)

    def test_rejects_invalid_profile_and_window_bounds(self):
        invalid = (
            ("nonsense", {"x": 0, "y": 0, "width": 1, "height": 1}),
            ("pot", {"x": 0.5, "y": 0, "width": 0.6, "height": 1}),
            ("pot", {"x": False, "y": 0, "width": 0.6, "height": 1}),
            ("pot", {"x": float("nan"), "y": 0, "width": 0.6, "height": 1}),
            ("pot", {"x": 0, "y": 0, "width": 0, "height": 1}),
            ("pot", {"x": 0, "y": 0, "width": 0.6, "height": 1, "kind": "card"}),
        )
        for key, field in invalid:
            with self.subTest(key=key, field=field), self.assertRaises(ValueError):
                parse_profile({"version": 1, "regions": {key: field}})
        for ratio in (True, 0, float("inf"), -1):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                parse_profile({"version": 1, "reference_aspect_ratio": ratio, "regions": {}})
        for viewport in ((-1, 0, 10, 10), (True, 0, 10, 10), (0, 0, 0, 10), (91, 0, 10, 10), (5, 5)):
            with self.subTest(viewport=viewport), self.assertRaisesRegex(ValueError, "viewport"):
                validate_viewport(viewport, (100, 100))
        self.assertEqual(validate_viewport([90, 0, 10, 10], (100, 100)), (90, 0, 10, 10))

    def test_rejects_duplicate_profile_keys(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text('{"version":1, "regions":{"pot":null, "pot":null}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复键"):
                load_profile(path)


@unittest.skipUnless(find_spec("PIL"), "viewport fixtures require Pillow")
class AutomaticViewportTests(unittest.TestCase):
    def test_detects_toolbar_bounds_at_different_sizes_and_preserves_bottom_controls(self):
        from PIL import Image, ImageDraw

        for width, height, toolbar in ((3024, 1712, 242), (1280, 720, 85), (864, 700, 63), (690, 460, 0)):
            with self.subTest(width=width, height=height, toolbar=toolbar):
                image = Image.new("RGB", (width, height), (239, 240, 242))
                draw = ImageDraw.Draw(image)
                draw.rectangle((0, toolbar, width - 1, height - 1), fill=(13, 109, 97))
                draw.rectangle((0, height * 91 // 100, width - 1, height - 1), fill=(41, 40, 42))
                draw.rectangle((width * 42 // 100, height * 50 // 100,
                                width * 56 // 100, height * 67 // 100), fill="white")
                self.assertEqual(detect_viewport(image), (0, toolbar, width, height - toolbar))

    def test_declines_ambiguous_desktop_and_nonfelt_images(self):
        from PIL import Image, ImageDraw

        desktop = Image.new("RGB", (1280, 900), (30, 29, 37))
        self.assertIsNone(detect_viewport(desktop))
        draw = ImageDraw.Draw(desktop)
        draw.rectangle((100, 41, 1100, 800), fill=(13, 109, 97))
        self.assertIsNone(detect_viewport(desktop))

        shallow = Image.new("RGB", (1024, 768), (37, 34, 39))
        ImageDraw.Draw(shallow).rectangle((0, 60, 1023, 620), fill=(13, 109, 97))
        self.assertIsNone(detect_viewport(shallow))

    def test_detects_shifted_browser_and_rejects_an_occluded_table(self):
        from PIL import Image, ImageDraw

        desktop = Image.new("RGB", (1920, 1200), (164, 168, 178))
        draw = ImageDraw.Draw(desktop)
        draw.rectangle((250, 113, 1629, 1074), fill=(228, 229, 234))
        draw.rectangle((250, 187, 1629, 994), fill=(13, 109, 97))
        draw.rectangle((250, 995, 1629, 1074), fill=(26, 25, 35))
        self.assertEqual(detect_viewport(desktop), (250, 187, 1380, 888))

        draw.rectangle((500, 310, 1110, 644), fill=(244, 240, 239))
        self.assertIsNone(detect_viewport(desktop))

    def test_finds_a_narrow_window_on_a_large_desktop(self):
        from PIL import Image, ImageDraw

        desktop = Image.new("RGB", (3840, 2160), (164, 168, 178))
        draw = ImageDraw.Draw(desktop)
        draw.rectangle((2500, 113, 3299, 616), fill=(228, 229, 234))
        draw.rectangle((2500, 157, 3299, 580), fill=(13, 109, 97))
        draw.rectangle((2500, 581, 3299, 616), fill=(26, 25, 35))
        self.assertEqual(detect_viewport(desktop), (2500, 157, 800, 460))

    def test_rejects_two_candidates_on_the_same_desktop(self):
        from PIL import Image, ImageDraw

        desktop = Image.new("RGB", (1800, 850), (164, 168, 178))
        draw = ImageDraw.Draw(desktop)
        for left in (35, 985):
            draw.rectangle((left, 129, left + 750, 674), fill=(13, 109, 97))
            draw.rectangle((left, 675, left + 750, 707), fill=(31, 31, 34))
        self.assertIsNone(detect_viewport(desktop))


if __name__ == "__main__":
    unittest.main()
