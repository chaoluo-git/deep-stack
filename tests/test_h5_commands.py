from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import uuid4

from deep_stack.__main__ import main


class H5CalibrationCommandTests(unittest.TestCase):
    def test_receiver_and_screen_feed_the_same_cli_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "timeline.jsonl"
            pairing_file = root / "pairing.json"
            report_path = root / "correlation.json"

            def scan_screen(**options):
                self.assertTrue(options["observe_only"])
                self.assertTrue(options["force_spectator"])
                self.assertIsNone(options["database"])
                self.assertFalse(options["should_stop"]())
                pairing = json.loads(pairing_file.read_text(encoding="utf-8"))
                sample_started = monotonic()
                frames = [{
                    "connection": "ws-1", "offset_ms": 13, "direction": "received",
                    "opcode": 2, "size_bytes": 72, "encoding": "binary",
                }]
                payload = {"capture_id": str(uuid4()), "batch_id": str(uuid4()), "frames": frames}
                request = Request(pairing["url"], json.dumps(payload).encode("utf-8"), headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + pairing["token"],
                })
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 204)
                options["on_observation"]({
                    "type": "action", "hand_id": "original-hand-reference",
                    "street": "preflop", "seat": "left_middle", "action": "call",
                    "wager": 20, "confirmed_hand": True, "player_id": "original-player-reference",
                }, captured_at=monotonic(), sample_started_at=sample_started)

            with patch("deep_stack.live.scan_live", side_effect=scan_screen), redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["h5-calibrate", str(journal), "--pair-file", str(pairing_file),
                                       "--port", "0", "--spectate", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["type"], "listening")
            self.assertFalse(pairing_file.exists())
            self.assertNotIn("original-", journal.read_text(encoding="utf-8"))

            self.assertEqual(main(["h5-correlate", str(journal), "--window-ms", "0", "-o", str(report_path)]), 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["screen_action"]["single"], 1)
            self.assertEqual(report["markers"][0]["candidates"][0]["size_bytes"], 72)
            self.assertIsNone(report["method"]["application_message_mapping"])

            original = journal.read_bytes()
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["h5-correlate", str(journal), "-o", str(journal)]), 2)
            self.assertEqual(journal.read_bytes(), original)
