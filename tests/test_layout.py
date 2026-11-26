"""Calibration and image-boundary tests for desktop screenshots."""

from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from deep_stack.layout import (
    EXTRA_SEAT_IDS,
    default_profile,
    detect_viewport,
    layout_warnings,
    load_profile,
    parse_profile,
    pixel_regions,
    profile_template,
    responsive_profile,
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

    def test_split_screen_uses_measured_seat_anchors_and_keeps_cards_wide(self):
        profile = default_profile()
        viewport = (0, 242, 1512, 1482)
        adapted = responsive_profile(profile, viewport)
        self.assertIsNotNone(adapted)
        self.assertEqual(layout_warnings(adapted, viewport), [])
        boxes = pixel_regions(adapted, viewport)
        for region, center in (("left_middle.avatar", 182), ("hero.avatar", 756),
                               ("right_middle.avatar", 1330)):
            with self.subTest(region=region):
                x, _, width, _ = boxes[region]
                self.assertAlmostEqual(x + width / 2, center, delta=1)
                self.assertGreaterEqual(width, 92)

        x, y, width, height = boxes["board_card_0"]
        self.assertAlmostEqual(x, 551, delta=1)
        self.assertAlmostEqual(y, 862, delta=1)
        self.assertGreaterEqual(width, 78)
        self.assertGreaterEqual(height, 113)

        for key, edge in (("hero_card_0", 675), ("hero_card_1", 761)):
            with self.subTest(key=key):
                x, _, width, _ = boxes[key]
                self.assertAlmostEqual(x, edge, delta=1)
                self.assertAlmostEqual(width, 76, delta=1)

        mid_view = (0, 242, 2271, 1476)
        mid = pixel_regions(responsive_profile(profile, mid_view), mid_view)
        x, _, width, _ = mid["left_middle.avatar"]
        self.assertAlmostEqual(x + width / 2, 249, delta=2)
        self.assertEqual(responsive_profile(profile, (0, 242, 3024, 1470)), profile)
        self.assertIsNotNone(responsive_profile(profile, (0, 242, 1510, 1482)))
        self.assertIsNone(responsive_profile(profile, (0, 242, 1400, 1482)))
        self.assertIsNone(responsive_profile(profile, (0, 0, 3500, 1470)))

    def test_profile_accepts_normalized_overrides_and_new_live_fields(self):
        profile = parse_profile({
            "version": 1,
            "reference_aspect_ratio": 1.5,
            "regions": {
                "hero_passive_button": {"x": 0.75, "y": 0.875, "width": 0.125, "height": 0.125},
                "top_right.status": {"x": 0.5, "y": 0.25, "width": 0.125, "height": 0.125},
                "hero.wager": {"x": 0.25, "y": 0.125, "width": 0.25, "height": 0.125},
                "hero.cardback": {"x": 0.25, "y": 0.125, "width": 0.25, "height": 0.125},
                "top_center.label": {"x": 0.5, "y": 0.125, "width": 0.25, "height": 0.125},
                "bottom_center.stack": {"x": 0.5, "y": 0.875, "width": 0.25, "height": 0.125},
                "spectator_hint_left": {"x": 0.25, "y": 0.75, "width": 0.25, "height": 0.125},
                "center_pot": None,
            },
        })
        boxes = pixel_regions(profile, (10, 20, 1200, 800))
        self.assertEqual(boxes["hero_passive_button"], (910, 720, 150, 100))
        self.assertEqual(profile.regions["hero.wager"]["kind"], "number")
        self.assertEqual(profile.regions["hero.cardback"]["kind"], "marker")
        self.assertEqual(profile.regions["top_right.status"]["kind"], "label")
        self.assertEqual(EXTRA_SEAT_IDS, ("top_center", "bottom_center"))
        self.assertEqual(profile.regions["top_center.label"]["kind"], "label")
        self.assertEqual(profile.regions["bottom_center.stack"]["kind"], "number")
        self.assertEqual(profile.regions["spectator_hint_left"]["kind"], "label")
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
        self.assertIsNone(detect_viewport(desktop, window=True))

    def test_recovers_window_bounds_with_a_shadow_and_a_changing_popup(self):
        from PIL import Image, ImageDraw

        screenshot = Image.new("RGBA", (1660, 936), (10, 83, 64, 120))
        draw = ImageDraw.Draw(screenshot)
        draw.rectangle((52, 33, 1563, 888), fill=(228, 229, 234, 255))
        draw.rectangle((52, 154, 1563, 827), fill=(13, 109, 97, 255))
        draw.rectangle((52, 828, 1563, 888), fill=(26, 25, 35, 255))

        expected = (52, 154, 1512, 735)
        self.assertEqual(detect_viewport(screenshot, window=True), expected)
        for popup in ((630, 365, 987, 550), (630, 324, 987, 888), (630, 278, 987, 701)):
            with self.subTest(popup=popup):
                frame = screenshot.copy()
                ImageDraw.Draw(frame).rectangle(popup, fill=(31, 36, 35, 255))
                self.assertEqual(detect_viewport(frame, window=True), expected)

    def test_known_window_still_rejects_large_occlusion_or_unrelated_bottom_panel(self):
        from PIL import Image, ImageDraw

        screenshot = Image.new("RGB", (1512, 856), (228, 229, 234))
        draw = ImageDraw.Draw(screenshot)
        draw.rectangle((0, 121, 1511, 804), fill=(13, 109, 97))
        draw.rectangle((0, 805, 1511, 855), fill=(26, 25, 35))
        self.assertEqual(detect_viewport(screenshot, window=True), (0, 121, 1512, 735))

        draw.rectangle((400, 245, 1010, 630), fill=(244, 240, 239))
        self.assertIsNone(detect_viewport(screenshot, window=True))

        draw.rectangle((0, 121, 1511, 680), fill=(13, 109, 97))
        draw.rectangle((0, 681, 1511, 855), fill=(244, 240, 239))
        self.assertIsNone(detect_viewport(screenshot, window=True))


    def test_split_screen_keeps_footer_below_a_curved_table(self):
        from PIL import Image, ImageDraw

        screenshot = Image.new("RGBA", (1512, 1724), (228, 229, 234, 255))
        draw = ImageDraw.Draw(screenshot)
        draw.rectangle((0, 242, 1511, 1723), fill=(26, 25, 35, 255))
        draw.polygon([
            (0, 242), (1511, 242), (1511, 1504), (1285, 1564), (1209, 1584),
            (1103, 1604), (1027, 1624), (970, 1626), (772, 1645),
            (739, 1645), (541, 1626), (484, 1624), (408, 1604), (302, 1584), (226, 1564), (0, 1504),
        ], fill=(13, 109, 97, 255))
        self.assertEqual(detect_viewport(screenshot, window=True), (0, 242, 1512, 1482))


if __name__ == "__main__":
    unittest.main()
