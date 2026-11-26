import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from deep_stack.recognition import RecognitionError, _table_state, recognize


def observations(*texts, confidence=0.99, empty_felt=False, card_surface=False):
    return {
        "observations": [{"text": text, "confidence": confidence} for text in texts],
        "empty_felt": empty_felt,
        "card_surface": card_surface,
    }


def by_seat(result, seat):
    return next(player for player in result["seats"] if player["seat"] == seat)


class TableRecognitionTests(unittest.TestCase):
    def test_extracts_visible_fields_and_leaves_hidden_names_unknown(self):
        result = _table_state({
            "top_left.label": observations("让牌"),
            "right_upper.label": observations("下註 154"),
            "right_upper.wager": observations("154"),
            "right_upper.stack": observations("１,２３４.５"),
            "left_upper.label": observations("River Visitor"),
            "left_upper.avatar": observations("棄牌"),
            "left_upper.stack": observations("2.5 K"),
            "hero.label": observations("Hero"),
            "hero.stack": observations("0"),
            "pot": observations("总底池：384"),
            "center_pot": observations("230"),
        })
        self.assertEqual(len(result["seats"]), 9)
        self.assertEqual(by_seat(result, "top_left")["action"], "check")
        self.assertIsNone(by_seat(result, "top_left")["name"])
        self.assertEqual(by_seat(result, "right_upper"), {
            "seat": "right_upper", "name": None, "stack": 1234.5,
            "action": "bet", "wager": 154,
        })
        self.assertEqual(by_seat(result, "left_upper")["name"], "River Visitor")
        self.assertEqual(by_seat(result, "left_upper")["action"], "fold")
        self.assertEqual(by_seat(result, "left_upper")["stack"], 2500)
        self.assertEqual(by_seat(result, "hero")["stack"], 0)
        self.assertEqual((result["pot"], result["center_pot"]), (384, 230))
        self.assertTrue(any("取整" in warning for warning in result["warnings"]))
        json.dumps(result, allow_nan=False)

    def test_does_not_guess_when_labels_conflict_or_are_unreliable(self):
        result = _table_state({
            "top_left.label": observations("跟注 20"),
            "top_left.avatar": observations("弃牌"),
            "right_upper.stack": observations("1,234", "578"),
            "right_middle.stack": observations("17", confidence=0.15),
            "right_lower.stack": observations("1 2"),
            "hero.avatar": observations("加注"),
            "pot": observations("invalid 384"),
        })
        self.assertIsNone(by_seat(result, "top_left")["action"])
        self.assertIsNone(by_seat(result, "right_upper")["stack"])
        self.assertIsNone(by_seat(result, "right_middle")["stack"])
        self.assertIsNone(by_seat(result, "right_lower")["stack"])
        self.assertIsNone(by_seat(result, "hero")["action"])
        self.assertIsNone(result["pot"])
        self.assertTrue(any("不同读数" in warning for warning in result["warnings"]))

    def test_uses_confirmed_empty_slots_to_distinguish_streets(self):
        fields = {
            "hero_card_0": observations("A", "♠"),
            "hero_card_1": observations("10♥"),
        }
        fields.update({f"board_card_{index}": observations(empty_felt=True) for index in range(5)})
        result = _table_state(fields)
        self.assertEqual(result["hero_cards"], ["As", "Th"])
        self.assertEqual(result["board"], [])
        self.assertEqual(result["street"], "preflop")

        fields.update({
            "board_card_0": observations("K♦"),
            "board_card_1": observations("2c"),
            "board_card_2": observations("7♥"),
            "board_card_3": observations("J"),
        })
        result = _table_state(fields)
        self.assertIsNone(result["board"])
        self.assertEqual(result["observed_board_cards"], ["Kd", "2c", "7h", None, None])
        self.assertIsNone(result["street"])

        fields["board_card_3"] = observations("JC")
        result = _table_state(fields)
        self.assertEqual(result["board"], ["Kd", "2c", "7h", "Jc"])
        self.assertEqual(result["street"], "turn")

    def test_uses_visual_card_count_when_rank_or_suit_is_missing(self):
        result = _table_state({
            "board_card_0": observations("Kd"),
            "board_card_1": observations("2", card_surface=True),
            "board_card_2": observations("7h"),
            "board_card_3": observations(empty_felt=True),
            "board_card_4": observations(empty_felt=True),
        })
        self.assertEqual(result["street"], "flop")
        self.assertIsNone(result["board"])

    def test_conflicting_symbol_and_ocr_card_remains_unknown(self):
        conflicting = observations("Ah", card_surface=True)
        conflicting["corner_card"] = {
            "suit": "s", "overlap": 0.96,
            "rank_observations": [{"text": "AAA", "confidence": 0.99}],
        }
        result = _table_state({
            "hero_card_0": conflicting,
            "hero_card_1": observations("Kd"),
        })
        self.assertEqual(result["observed_hero_cards"], [None, "Kd"])
        self.assertIsNone(result["hero_cards"])

    def test_duplicate_cards_do_not_yield_a_complete_state(self):
        result = _table_state({
            "hero_card_0": observations("As"),
            "hero_card_1": observations("Th"),
            "board_card_0": observations("As"),
            "board_card_1": observations("2c"),
            "board_card_2": observations("Js"),
            "board_card_3": observations(empty_felt=True),
            "board_card_4": observations(empty_felt=True),
        })
        self.assertIsNone(result["board"])
        self.assertIsNone(result["hero_cards"])
        self.assertTrue(any("重复牌" in warning for warning in result["warnings"]))

    def test_rejects_invalid_viewports_without_launching_ocr(self):
        for viewport in ((0, 0, 0, 1), (0, -4, 1, 1), (False, 0, 1, 1), (0,), 34):
            with self.subTest(viewport=viewport), self.assertRaisesRegex(RecognitionError, "viewport"):
                recognize("does-not-exist.png", viewport=viewport)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swift"), "requires macOS Vision")
class AppleVisionSmokeTest(unittest.TestCase):
    def test_reads_an_original_synthetic_retina_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.png"
            subprocess.run(
                ["swift", "-", str(path)], input=_DRAW_TEST_SCREEN,
                encoding="utf-8", capture_output=True, timeout=60, check=True,
            )
            result = recognize(path, viewport=(0, 242, 3024, 1470))

        self.assertEqual(by_seat(result, "top_left")["action"], "check", result["evidence"])
        self.assertEqual(by_seat(result, "right_upper")["action"], "bet", result["evidence"])
        self.assertEqual(by_seat(result, "right_upper")["wager"], 154)
        self.assertEqual(by_seat(result, "left_lower")["action"], "fold", result["evidence"])
        self.assertEqual(by_seat(result, "hero")["name"], "Study Hero")
        self.assertEqual(by_seat(result, "hero")["stack"], 800)
        self.assertEqual(result["hero_cards"], ["As", "Kd"], result["evidence"])
        self.assertEqual(result["board"], ["3c", "5h", "Tc"], result["evidence"])
        for index, key in enumerate(("hero_card_0", "hero_card_1", "board_card_1", "board_card_2")):
            self.assertEqual(result["evidence"][key]["corner_card"]["suit"], "sdhc"[index])
        self.assertEqual(result["street"], "flop")
        self.assertEqual((result["pot"], result["center_pot"]), (384, 230))


_DRAW_TEST_SCREEN = r"""
import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

let width = 3024
let height = 1712
let rgb = CGColorSpace(name: CGColorSpace.sRGB)!
let canvas = CGContext(
    data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
    space: rgb, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
)!

func fill(_ x: Int, _ y: Int, _ w: Int, _ h: Int, _ r: CGFloat, _ g: CGFloat, _ b: CGFloat) {
    canvas.setFillColor(red: r, green: g, blue: b, alpha: 1)
    canvas.fill(CGRect(x: x, y: height - y - h, width: w, height: h))
}

func label(_ text: String, _ x: Int, _ y: Int, dark: Bool = false, suit: Bool = false, red: Bool = false) {
    let font = CTFontCreateWithName((suit ? "Apple Symbols" : "Helvetica-Bold") as CFString, suit ? 23 : 27, nil)
    let color = red ? CGColor(red: 0.78, green: 0.05, blue: 0.10, alpha: 1)
        : CGColor(gray: dark ? 0.05 : 0.97, alpha: 1)
    let attributes = [
        kCTFontAttributeName: font,
        kCTForegroundColorAttributeName: color
    ] as CFDictionary
    let attributed = CFAttributedStringCreate(nil, text as CFString, attributes)!
    let line = CTLineCreateWithAttributedString(attributed)
    var ascent: CGFloat = 0
    var descent: CGFloat = 0
    let w = CTLineGetTypographicBounds(line, &ascent, &descent, nil)
    canvas.textPosition = CGPoint(x: CGFloat(x) - w / 2, y: CGFloat(height - y) - (ascent - descent) / 2)
    CTLineDraw(line, canvas)
}

fill(0, 0, width, height, 0.74, 0.76, 0.79)
fill(0, 242, width, 1470, 0.04, 0.27, 0.13)
label("让牌", 1400, 354)
label("下注", 2708, 459)
label("154", 2600, 528)
fill(273, 1057, 82, 36, 0.18, 0.18, 0.18)
label("弃牌", 314, 1075)
label("Study Hero", 1512, 1312)
label("800", 1512, 1648)
label("384", 1512, 611)
label("230", 1526, 653)

func printedCard(_ rank: String, _ suit: String, _ x: Int, _ y: Int, red: Bool = false) {
    fill(x, y, 78, 113, 0.95, 0.95, 0.95)
    label(rank, x + (rank == "10" ? 20 : 15), y + 19, dark: true)
    label(suit, x + 15, y + 50, dark: true, suit: true, red: red)
}

printedCard("A", "♠", 1430, 1471)
printedCard("K", "♦", 1512, 1471, red: true)
fill(1309, 857, 78, 113, 0.95, 0.95, 0.95)
label("3C", 1348, 901, dark: true)
printedCard("5", "♥", 1391, 857, red: true)
printedCard("10", "♣", 1473, 857)

let output = URL(fileURLWithPath: CommandLine.arguments[1])
let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, canvas.makeImage()!, nil)
precondition(CGImageDestinationFinalize(destination))
"""

if __name__ == "__main__":
    unittest.main()
