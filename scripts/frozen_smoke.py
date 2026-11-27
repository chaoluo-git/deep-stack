"""Exercise the standalone executable with local OCR and H5 fixtures."""

from base64 import b64encode
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


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


def assert_table_replay(executable, folder, fixtures, env, *, capacity):
    from PIL import Image, ImageDraw

    from deep_stack.layout import BOARD_CARD_FIELDS, pixel_regions
    from deep_stack.recognition import live_profile

    viewport = (0, 0, 3024, 1470)
    regions = pixel_regions(live_profile(), viewport)
    if capacity == 8:
        positions = {"bottom_center": "BTN", "left_lower": "SB", "left_middle": "BB", "left_upper": "UTG",
                     "top_center": "LJ", "right_upper": "HJ", "right_lower": "CO"}
        badge = "bottom_center.dealer_left"
        actor = "right_upper"
    else:
        positions = {"left_lower": "BTN", "left_middle": "SB", "left_upper": "BB", "top_left": "UTG",
                     "top_right": "UTG+1", "right_upper": "LJ", "right_lower": "HJ", "bottom_center": "CO"}
        badge = "left_lower.dealer_right"
        actor = "right_lower"
    vacant = "right_middle"
    felt = (22, 108, 68)
    table = Image.new("RGB", viewport[2:], felt)
    draw = ImageDraw.Draw(table)
    font = fixtures["_standard_font"]

    def label(painter, key, value, *, color="white"):
        x, y, width, height = regions[key]
        painter.text((x + width // 2, y + height // 2), value, anchor="mm", fill=color,
                     font=font(max(12, round(height * 0.64))))

    for index, seat in enumerate(positions, start=1):
        label(draw, f"{seat}.label", f"P{index}")
        label(draw, f"{seat}.stack", str(100 * index))
        x, y, width, height = regions[f"{seat}.cardback"]
        draw.rectangle((x + 1, y + 1, x + width - 2, y + height - 2), fill=(179, 39, 51),
                       outline=(237, 232, 229), width=3)

    x, y, width, height = regions[f"{vacant}.avatar"]
    cx, cy = x + width // 2, y + height // 2
    draw.line((cx - width // 5, cy, cx + width // 5, cy), fill=(148, 152, 148), width=3)
    draw.line((cx, cy - height * 2 // 5, cx, cy + height * 2 // 5), fill=(148, 152, 148), width=3)

    x, y, width, height = regions[badge]
    draw.ellipse((x + 1, y + 1, x + width - 2, y + height - 2), fill="white")
    label(draw, badge, "D", color="black")

    cards = ("As", "Kc", "6d", "Th", "7c")
    colors = {"s": "#181818", "c": "#119854", "d": "#266de6", "h": "#e52c41"}
    deck = fixtures["PortableOCRTests"]._large_pip
    paths = []
    streets = ("flop", "turn", "river")
    for count, street in enumerate(streets, start=3):
        image = table.copy()
        if street == "flop":
            action_key = f"{actor}.label"
            painter = ImageDraw.Draw(image)
            x, y, width, height = regions[action_key]
            painter.rectangle((x, y, x + width - 1, y + height - 1), fill=felt)
            label(painter, action_key, "Bet")
        for key, (rank, suit) in zip(BOARD_CARD_FIELDS, cards[:count]):
            x, y, width, height = regions[key]
            card = deck("10" if rank == "T" else rank, suit, colors[suit])
            image.paste(card.resize((width, height), Image.Resampling.LANCZOS), (x, y))
        path = folder / f"watch-{capacity}-{street}.png"
        image.save(path)
        paths.append(path)

    output = subprocess.check_output(
        [executable, "live", "--frames", paths[0], *paths, "--viewport", ",".join(map(str, viewport)),
         "--spectate", "--no-player-log", "--json"], cwd=folder, env=env, encoding="utf-8", timeout=180,
    )
    events = [json.loads(line) for line in output.splitlines()]
    board = [event for event in events if event["type"] == "board"]
    expected = [(street, list(cards[:count]), True) for count, street in enumerate(streets, start=3)]
    if [(event["street"], event["cards"], event["complete"]) for event in board] != expected:
        raise ValueError(f"Unexpected public cards in spectator replay: {board}")
    hand_ids = {event["hand_id"] for event in board}
    if None in hand_ids or len(hand_ids) != 1 or any(event["type"] == "recommendation" for event in events):
        raise ValueError(f"Unexpected hand or hero output in spectator replay: {events}")
    if not any(event["type"] == "table" and event["seat_capacity"] == capacity and event["dealt_players"] == capacity - 1
               and event["position_status"] == "confirmed" and event["seat_statuses"].get(vacant) == "vacant"
               and event["position_by_seat"] == positions and "空位" in event["message"] for event in events):
        raise ValueError(f"Missing {capacity - 1}-player positions and visual vacancy in spectator replay: {events}")
    actions = [event for event in events if event["type"] == "action"]
    if not any(event["seat"] == actor and event["position"] == "HJ" and event["action"] == "bet"
               and "HJ" in event["message"] for event in actions):
        raise ValueError(f"Missing action with its poker position in spectator replay: {actions}")


def assert_profile_replay(executable, folder, fixtures, env):
    popup = folder / "popup.png"
    database = folder / "popup.sqlite3"
    fixtures["draw_popup_fixture"]().save(popup)
    viewport = ",".join(map(str, fixtures["POPUP_VIEWPORT"]))
    output = subprocess.check_output(
        [executable, "live", "--frames", popup, popup, "--db", database, "--session", "popup-test",
         "--viewport", viewport, "--json"],
        cwd=folder, env=env, encoding="utf-8", timeout=180,
    )
    events = [json.loads(line) for line in output.splitlines()]
    expected = {
        "player_id": fixtures["POPUP_ID"],
        "name": fixtures["POPUP_NAME"],
        "seat": fixtures["POPUP_SEAT"],
        "stats": fixtures["POPUP_STATS"],
    }
    bound = [{key: event.get(key) for key in expected} for event in events if event["type"] == "player_bound"]
    if bound != [expected] or any(event["type"] == "recommendation" for event in events):
        raise ValueError(f"Unexpected profile replay events: {events}")

    player = json.loads(subprocess.check_output(
        [executable, "players", "--db", database, "show", expected["player_id"], "--profiles", "1"],
        cwd=folder, env=env, encoding="utf-8", timeout=30,
    ))
    history = player["profiles"]
    if (not player["registered"] or player["player_id"] != expected["player_id"]
            or len(history) != 1 or history[0]["name"] != expected["name"]
            or history[0]["stats"] != expected["stats"] or player["club_profile"] != expected["stats"]):
        raise ValueError(f"Unexpected saved player profile: {player}")


def assert_h5_capture(executable, folder, env):
    extension = executable.parent / "wpk-h5-capture"
    if not all((extension / name).is_file() for name in ("manifest.json", "background.mjs", "popup.html")):
        raise ValueError("Missing Chrome capture extension in the standalone package.")

    trace = folder / "synthetic-h5.json"
    frames = []
    for index in range(6):
        for method, delay, size in (("Sent", 0, 82), ("Received", 118, 62)):
            frames.append({"method": f"Network.webSocketFrame{method}", "params": {
                "requestId": "synthetic-connection", "timestamp": index * 5 + delay / 1000,
                "response": {"opcode": 2, "payloadData": b64encode(bytes(size)).decode("ascii")},
            }})
    trace.write_text(json.dumps(frames), encoding="utf-8")
    report = run_command(executable, folder, env, "h5-inspect", trace, "--patterns")
    patterns = report["traffic_patterns"]
    if report["frame_count"] != 12 or len(patterns) != 1 or patterns[0]["period_ms"] != 5000:
        raise ValueError("Standalone H5 replay did not identify the synthetic exchange.")

    pairing_file = folder / "pairing.json"
    output = folder / "h5-live.jsonl"
    with output.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([executable, "h5-live", "--port", "0", "--pair-file", pairing_file, "--json"],
                                   cwd=folder, env=env, stdout=log)
        try:
            deadline = monotonic() + 20
            while process.poll() is None and monotonic() < deadline:
                if pairing_file.is_file() and pairing_file.stat().st_size and output.read_bytes().endswith(b"\n"):
                    break
                sleep(0.05)
            else:
                raise ValueError("Standalone H5 receiver did not create a pairing file.")
            pairing = json.loads(pairing_file.read_text(encoding="utf-8"))
            frame = {**report["frames"][0], "encoding": "binary"}
            batch = {"capture_id": str(uuid4()), "batch_id": str(uuid4()), "frames": [frame]}
            request = Request(pairing["url"], data=json.dumps(batch).encode("utf-8"), headers={
                "Authorization": f"Bearer {pairing['token']}", "Content-Type": "application/json",
            })
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                if response.status != 204:
                    raise ValueError("Standalone H5 receiver did not acknowledge the batch.")
            terminal = output.read_text(encoding="utf-8")
            events = [json.loads(line) for line in terminal.splitlines()]
            if (pairing["token"] in terminal or [event.get("frames") for event in events[1:]] != [[frame]]):
                raise ValueError("Unexpected standalone H5 receiver output.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def assert_h5_correlation(executable, folder, env):
    from deep_stack.h5_calibration import CalibrationRecorder

    help_output = subprocess.check_output([executable, "h5-calibrate", "--help"], cwd=folder, env=env,
                                          encoding="utf-8", timeout=30)
    if not all(option in help_output for option in ("TIMELINE", "--pair-file", "--seconds")):
        raise ValueError("Standalone H5 calibration options are missing.")

    timeline = folder / "anonymous-timeline.jsonl"
    private = "synthetic-private-field"
    clock = iter((100.0, 101.0)).__next__
    with CalibrationRecorder(timeline, clock=clock) as recorder:
        recorder.h5({"type": "frames", "capture_id": str(uuid4()), "batch_id": str(uuid4()), "frames": [{
            "connection": "ws-1", "offset_ms": 36, "direction": "received", "opcode": 2,
            "size_bytes": 63, "encoding": "binary", "payloadData": private,
        }]})
        recorder.screen({"type": "action", "street": "flop", "seat": "right_lower", "position": "HJ",
                         "action": "bet", "wager": 12, "hand_id": "fixture-hand", "confirmed_hand": True,
                         "player_id": private}, captured_at=100.98, sample_started_at=100.96)
        recorder.screen({"type": "board", "street": "turn", "cards": ["As", "Kc", "6d", "Th"],
                         "complete": True, "hand_id": "fixture-hand"}, captured_at=102.0)

    report = run_command(executable, folder, env, "h5-correlate", timeline, "--window-ms", "30")
    action, board = report["markers"]
    candidate, = action["candidates"]
    if (report["source"]["frame_count"] != 1 or report["summary"]["screen_action"]["single"] != 1
            or report["summary"]["screen_board"]["none"] != 1):
        raise ValueError("Unexpected H5 candidate totals in standalone correlation.")
    if (action["candidate_status"] != "single" or action["candidate_frame_count"] != 1
            or action["position"] != "HJ" or action["sample_started_ms"] != 960):
        raise ValueError("The visible H5 calibration action did not retain its observed timing or position.")
    if (candidate["size_bytes"] != 63 or candidate["arrival_delta_ms"] != 20
            or board["candidate_status"] != "none" or report["method"]["application_message_mapping"] is not None):
        raise ValueError("Unexpected H5 timing candidate in standalone correlation.")
    for output in (timeline.read_text(encoding="utf-8"), json.dumps(report)):
        if any(value in output for value in (private, "payloadData", "player_id", "fixture-hand")):
            raise ValueError("Standalone H5 correlation exposed an excluded field.")


def main(executable):
    tests = Path(__file__).resolve().parents[1] / "tests"
    fixture_helpers = runpy.run_path(str(tests / "test_windows_ocr.py"))
    popup_fixtures = runpy.run_path(str(tests / "test_player_popup.py"))
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    with TemporaryDirectory(prefix="deep-stack-smoke-") as directory:
        folder = Path(directory)
        subprocess.run([executable, "live", "--help"], cwd=folder, env=env, check=True, timeout=30)
        demo = run_command(executable, folder, env, "demo")
        recommendation = demo["recommendation"]
        if (recommendation["kind"], recommendation["amount"], recommendation["ev"]) != ("call", 20, 70):
            raise ValueError(f"Unexpected EV recommendation: {recommendation}")

        assert_h5_capture(executable, folder, env)
        assert_h5_correlation(executable, folder, env)
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
        for capacity in (8, 9):
            assert_table_replay(executable, folder, fixture_helpers, env, capacity=capacity)
        assert_profile_replay(executable, folder, popup_fixtures, env)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True))
