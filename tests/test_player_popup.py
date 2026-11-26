"""Conservative profile reading and same-table seat matching."""

from importlib.util import find_spec
import unittest

from deep_stack.player_popup import PlayerPopup, match_popup, parse_popup


POPUP_VIEWPORT = (0, 0, 1512, 735)
POPUP_ID = "00123456"
POPUP_NAME = "Study Player"
POPUP_SEAT = "right_upper"
POPUP_STATS = {"hands": 365, "win_rate": 0.22, "pot_entry_rate": 0.41, "showdown_win_rate": 0.55}


def draw_popup_fixture():
    """Render a reproducible profile with one matching seat nickname."""
    from PIL import Image, ImageDraw, ImageFont

    from deep_stack.layout import pixel_regions
    from deep_stack.recognition import live_profile

    image = Image.new("RGB", POPUP_VIEWPORT[2:], (4, 63, 49))
    draw = ImageDraw.Draw(image)
    draw.rectangle((590, 120, 921, 635), fill=(24, 40, 35))
    regions = pixel_regions(live_profile(), POPUP_VIEWPORT)
    for key, text, color in (
        ("id_label", "ID", "#20201a"), ("id_value", POPUP_ID, "#35d8ca"),
        ("name", POPUP_NAME, "white"), ("hands", "365", "#35d8ca"),
        ("win_rate", "22%", "#35d8ca"), ("pot_entry_rate", "41%", "#35d8ca"),
        ("showdown_win_rate", "55%", "#35d8ca"),
    ):
        x, y, width, height = regions[f"player_popup_{key}"]
        if key == "id_label":
            draw.rounded_rectangle((x + 1, y + 1, x + width - 2, y + height - 2), 3, fill="#d49351")
        draw.text((x + width / 2, y + height / 2), text, anchor="mm", fill=color,
                  font=ImageFont.load_default(round(height * 0.76)))

    x, y, width, height = regions[f"{POPUP_SEAT}.label"]
    draw.text((x + width / 2, y + height / 2), POPUP_NAME, anchor="mm", fill="white",
              font=ImageFont.load_default(round(height * 0.60)))
    return image


@unittest.skipUnless(all(find_spec(name) for name in ("PIL", "rapidocr", "onnxruntime")),
                     "requires portable OCR dependencies")
class PopupImageTests(unittest.TestCase):
    def test_reads_small_profile_badge_id_name_and_club_stats(self):
        from deep_stack.recognition import recognize

        result = recognize(draw_popup_fixture(), viewport=POPUP_VIEWPORT, live=True, backend="portable")
        self.assertTrue(result["popup_open"])
        self.assertEqual(result["player_popup"], {
            "player_id": POPUP_ID, "name": POPUP_NAME, "stats": POPUP_STATS,
        })
        self.assertEqual(match_popup(PlayerPopup(**result["player_popup"]), result["seats"]), POPUP_SEAT)


def reading(text, *, confidence=0.98, coordinates=None):
    result = {"text": text, "confidence": confidence}
    if coordinates is not None:
        result["center_x"], result["center_y"] = coordinates
    return result


def field(*readings):
    return {"observations": [reading(item) if isinstance(item, str) else item for item in readings]}


def seat(seat_id, name, *, vacant=False):
    return {"seat": seat_id, "name": name, "vacant": vacant}


class PlayerPopupParsingTests(unittest.TestCase):
    def test_reads_id_row_and_typed_visible_stats(self):
        popup = parse_popup({
            "player_popup_id": field("玩家 ID：００１２３４"),
            "player_popup_name": field("样本玩家"),
            "player_popup_hands": field("12,345"),
            "player_popup_win_rate": field("４５.２％"),
            "player_popup_pot_entry_rate": field("0%"),
            "player_popup_showdown_win_rate": field("100 %"),
        })
        self.assertEqual(popup, PlayerPopup("001234", "样本玩家", {
            "hands": 12345,
            "win_rate": 0.452,
            "pot_entry_rate": 0.0,
            "showdown_win_rate": 1.0,
        }))

    def test_reads_two_regions_or_two_blocks_on_the_same_row(self):
        self.assertEqual(parse_popup({
            "player_popup_id_label": field("Player ID"),
            "player_popup_id_value": field("012345"),
        }), PlayerPopup("012345"))
        self.assertEqual(parse_popup({
            "player_popup_id": field("ID:", "012345"),
        }), PlayerPopup("012345"))
        self.assertEqual(parse_popup({
            "player_popup": field(
                reading("ID:", coordinates=(0.22, 0.53)),
                reading("012345", coordinates=(0.49, 0.54)),
                reading("99999", coordinates=(0.51, 0.82)),
                "Nickname: Example",
            ),
        }), PlayerPopup("012345", "Example"))

    def test_declines_multiple_id_candidates_and_cross_row_pair(self):
        fields = {
            "player_popup_id": field("ID: 12345", "ID: 12346"),
            "player_popup_name": field("Example"),
        }
        self.assertIsNone(parse_popup(fields))
        self.assertIsNone(parse_popup({
            "player_popup": field(
                reading("ID", coordinates=(0.22, 0.33)),
                reading("12345", coordinates=(0.49, 0.62)),
            ),
        }))
        self.assertIsNone(parse_popup({
            "player_popup_id_label": field("ID:"),
            "player_popup_id_value": field("12345", "12346"),
        }))

    def test_declines_partial_low_confidence_and_unrelated_numbers(self):
        for fields in (
            {"player_popup_id": field(reading("ID: 12345", confidence=0.42))},
            {"player_popup_id": field("ID: 123")},
            {"player_popup_id": field("ID: 00000")},
            {"player_popup": field("ID", "12345", "32984")},
            {"player_popup_id_value": field("12345")},
            {"other_screen_area": field("ID: 12345")},
        ):
            with self.subTest(fields=fields):
                self.assertIsNone(parse_popup(fields))

    def test_declines_conflicting_names_and_omits_invalid_stats(self):
        self.assertIsNone(parse_popup({
            "player_popup_id": field("ID:12345"),
            "player_popup_name": field("One", "Two"),
        }))
        popup = parse_popup({
            "player_popup_id": field("ID:12345"),
            "player_popup_hands": field("7,12"),
            "player_popup_win_rate": field("101%"),
            "player_popup_pot_entry_rate": field("14.2"),
            "player_popup_showdown_win_rate": field("40%", "50%"),
        })
        self.assertEqual(popup, PlayerPopup("12345"))


class PlayerPopupMatchingTests(unittest.TestCase):
    def test_normalizes_inserted_spacing_but_still_rejects_duplicate_nicknames(self):
        popup = PlayerPopup("00123", "样 本玩家")
        rows = [seat("top_center", "样本　玩家"), seat("right_upper", "旁观者")]
        self.assertEqual(match_popup(popup, rows), "top_center")
        rows[1]["name"] = "样本玩家"
        self.assertIsNone(match_popup(popup, rows))

    def test_uses_current_or_adjacent_visible_name_only(self):
        popup = PlayerPopup("00123", "Example")
        first = [seat("top_left", "Ｅxample"), seat("right_upper", "Someone else")]
        second = [seat("top_left", None), seat("right_upper", "Someone else")]
        self.assertEqual(match_popup(popup, first), "top_left")
        self.assertEqual(match_popup(popup, second, previous_seats=first), "top_left")
        self.assertIsNone(match_popup(popup, second))
        self.assertIsNone(match_popup(PlayerPopup("00123"), first))

    def test_declines_duplicate_name_moved_player_or_empty_seat(self):
        popup = PlayerPopup("00123", "Example")
        current = [seat("top_left", "Example"), seat("right_upper", "Example")]
        self.assertIsNone(match_popup(popup, current))
        previous = [seat("top_left", "Example"), seat("right_upper", "Other")]
        for current in (
            [seat("top_left", None), seat("right_upper", "Example")],
            [seat("top_left", "Third"), seat("right_upper", "Other")],
            [seat("top_left", None, vacant=True), seat("right_upper", "Other")],
            [seat("right_upper", "Other")],
        ):
            with self.subTest(current=current):
                self.assertIsNone(match_popup(popup, current, previous_seats=previous))


if __name__ == "__main__":
    unittest.main()
