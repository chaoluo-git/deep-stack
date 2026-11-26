"""Exercise the standalone executable with the source suite's OCR fixture."""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory


def run_command(executable, folder, env, *args):
    output = folder / f"{args[0]}.json"
    subprocess.run(
        [executable, *args, "-o", output],
        cwd=folder, env=env, check=True, timeout=180,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def assert_live_replay(executable, folder, screenshot, font, env):
    from PIL import Image, ImageDraw

    from deep_stack.layout import SEAT_IDS, pixel_regions
    from deep_stack.recognition import live_profile

    with Image.open(screenshot) as source:
        image = source.copy()
    viewport = (0, 0, *image.size)
    regions = pixel_regions(live_profile(), viewport)
    draw = ImageDraw.Draw(image)
    for key, text in (("hero.avatar", "9s"), ("right_upper.label", "Bet")):
        x, y, width, height = regions[key]
        draw.rectangle((x, y, x + width - 1, y + height - 1), fill=(10, 95, 65))
        draw.text(
            (x + width // 2, y + height // 2), text,
            font=font(max(12, round(height * 0.62))), fill="white", anchor="mm",
        )
    replay = folder / "live.png"
    image.save(replay)
    assumptions = {
        "empty_seats": [seat for seat in SEAT_IDS if seat not in {"hero", "right_upper"}],
        "pot_includes_wagers": True,
    }
    model = folder / "model.json"
    model.write_text(json.dumps(assumptions), encoding="utf-8")
    output = subprocess.check_output(
        # Reuse the model on a second frame even if the initial load took long enough to expire the first.
        [executable, "live", "--frames", replay, replay, "--viewport", ",".join(map(str, viewport)),
         "--model", model, "--samples", "300", "--max-age", "8", "--no-player-log", "--json"],
        cwd=folder, env=env, encoding="utf-8", timeout=180,
    )
    events = [json.loads(line) for line in output.splitlines()]
    recommendation = next(
        (event["analysis"]["recommendation"] for event in events if event["type"] == "recommendation"),
        None,
    )
    if recommendation is None or (recommendation["kind"], recommendation["amount"]) != ("call", 20):
        raise ValueError(f"Unexpected live replay events: {events}")


def main(executable):
    fixture = Path(__file__).resolve().parents[1] / "tests" / "test_windows_ocr.py"
    fixture_helpers = runpy.run_path(str(fixture))
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    with TemporaryDirectory(prefix="deep-stack-smoke-") as directory:
        folder = Path(directory)
        subprocess.run([executable, "live", "--help"], cwd=folder, env=env, check=True, timeout=30)
        demo = run_command(executable, folder, env, "demo")
        recommendation = demo["recommendation"]
        if (recommendation["kind"], recommendation["amount"], recommendation["ev"]) != ("call", 20, 70):
            raise ValueError(f"Unexpected EV recommendation: {recommendation}")

        screenshot = folder / "牌桌.png"
        fixture_helpers["write_ocr_fixture"](screenshot)
        observation = run_command(executable, folder, env, "observe", screenshot)
        frame = observation["frames"][0]
        seats = {seat["seat"]: seat for seat in frame["seats"]}
        hero = seats["hero"]
        opponent = seats["right_upper"]
        values = (hero["name"], hero["stack"], opponent["stack"], opponent["action"],
                  opponent["wager"], frame["pot"], frame["center_pot"], frame["street"])
        if values != ("Alice", 800, 420, "call", 20, 240, 220, "flop"):
            raise ValueError(f"Unexpected OCR state: {values}. Evidence: {frame['evidence']}")
        assert_live_replay(executable, folder, screenshot, fixture_helpers["_standard_font"], env)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True))
