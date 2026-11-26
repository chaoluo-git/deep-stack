"""Synthetic visual fixtures for session-only avatar matching."""

from importlib.util import find_spec
from random import Random
import unittest

from deep_stack.avatar_match import avatar_signature, avatars_conflict, match_avatar


def avatar(seed):
    from PIL import Image, ImageDraw

    random = Random(seed)
    image = Image.new("RGB", (72, 72), (109, 121, 113))
    draw = ImageDraw.Draw(image)
    for _ in range(26):
        x, y = random.randrange(64), random.randrange(64)
        fill = tuple(random.randrange(20, 237) for _ in range(3))
        draw.rectangle((x, y, x + random.randrange(8, 26), y + random.randrange(8, 26)), fill=fill)
    return image


@unittest.skipUnless(find_spec("PIL"), "avatar fixtures require Pillow")
class AvatarMatchTests(unittest.TestCase):
    def test_finds_same_avatar_after_scale_and_scrim(self):
        from PIL import Image, ImageEnhance

        image = avatar(11)
        popup = avatar_signature(image.resize((117, 117), Image.Resampling.BICUBIC))
        dimmed = ImageEnhance.Brightness(image.resize((43, 43))).enhance(0.46)
        self.assertEqual(match_avatar(popup, {
            "seat_left": [avatar_signature(avatar(5))],
            "seat_right": [avatar_signature(dimmed)],
        }), "seat_right")

    def test_labels_covering_center_can_use_both_exposed_bands(self):
        from PIL import ImageDraw, ImageEnhance

        image = avatar(25)
        small = ImageEnhance.Brightness(image.resize((48, 48))).enhance(0.55)
        drawing = ImageDraw.Draw(small)
        drawing.rectangle((8, 17, 40, 30), fill=(18, 18, 18))
        drawing.text((9, 16), "FOLD", fill=(255, 255, 255))
        self.assertEqual(match_avatar(avatar_signature(image), {
            "seat_left": [avatar_signature(small)],
            "seat_right": [avatar_signature(avatar(14))],
        }), "seat_left")

    def test_duplicate_avatar_does_not_pick_between_seats(self):
        image = avatar(25)
        signature = avatar_signature(image)
        self.assertIsNone(match_avatar(signature, {
            "seat_left": [signature],
            "seat_right": [avatar_signature(image.resize((41, 41)))],
        }))

    def test_rejects_uniform_and_unrelated_images(self):
        from PIL import Image

        self.assertIsNone(avatar_signature(Image.new("RGB", (50, 50), "purple")))
        self.assertIsNone(avatar_signature(Image.new("RGB", (10, 10), "red")))
        self.assertIsNone(match_avatar(avatar_signature(avatar(26)), {
            "seat_left": [avatar_signature(avatar(18))],
            "seat_right": [avatar_signature(avatar(10))],
        }))

    def test_uses_previous_clear_view_when_current_view_is_missing(self):
        signature = avatar_signature(avatar(5))
        self.assertEqual(match_avatar(signature, {
            "seat_left": [None, signature],
            "seat_right": [avatar_signature(avatar(6)), None],
        }), "seat_left")

    def test_requires_an_independent_visible_candidate(self):
        signature = avatar_signature(avatar(5))
        self.assertIsNone(match_avatar(signature, {"seat_left": [signature], "seat_right": [None]}))

    def test_conflict_requires_visible_disagreement_in_both_bands(self):
        from PIL import Image, ImageDraw, ImageEnhance

        vertical = Image.new("RGB", (60, 60), "white")
        horizontal = vertical.copy()
        vertical_draw, horizontal_draw = ImageDraw.Draw(vertical), ImageDraw.Draw(horizontal)
        for start in (4, 24, 44):
            vertical_draw.rectangle((start, 0, start + 7, 59), fill="black")
            horizontal_draw.rectangle((0, start, 59, start + 7), fill="black")
        first, second = avatar_signature(vertical), avatar_signature(horizontal)
        self.assertTrue(avatars_conflict(first, second))
        self.assertFalse(avatars_conflict(first, None))

        dimmed = ImageEnhance.Brightness(vertical).enhance(0.5)
        ImageDraw.Draw(dimmed).rectangle((5, 22, 56, 37), fill="white")
        self.assertFalse(avatars_conflict(first, avatar_signature(dimmed)))
