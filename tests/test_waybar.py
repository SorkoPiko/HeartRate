import io
import json
import unittest
from contextlib import redirect_stdout

from helpers import StopFollow, TempEnv, hr


def caption(value):
    return f"<span color='#9a978f'>hr</span> <span font_family='JetBrainsMono Nerd Font'>{value}</span>"


DEAD = {"text": caption("–"), "class": "disconnected", "tooltip": "heartrate daemon not running"}


class WaybarTest(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv(self)
        self.env.state_file.parent.mkdir()

    def write_state(self, **fields):
        state = {"status": "live", "bpm": 72, "device": "COROS PACE 3 8B7D1F", "source": "ble", "reason": ""}
        state["updated"] = self.env.clock.now
        state.update(fields)
        self.env.state_file.write_text(json.dumps(state))

    def follow(self, polls=1):
        """Run the follower for `polls` iterations and return the lines it printed."""
        self.env.clock.sleeps = 0
        self.env.clock.max_sleeps = polls
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(StopFollow):
            hr.follow()
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_live(self):
        self.write_state()
        self.assertEqual(
            self.follow(),
            [{"text": caption(72), "class": "live", "tooltip": "COROS PACE 3 8B7D1F\n72 bpm"}],
        )

    def test_waiting(self):
        self.write_state(status="waiting", bpm=None, reason="waiting for first reading")
        self.assertEqual(
            self.follow(),
            [{"text": caption("…"), "class": "waiting", "tooltip": "COROS PACE 3 8B7D1F\nwaiting for first reading"}],
        )

    def test_disconnected(self):
        self.write_state(status="disconnected", bpm=None, reason="device not connected")
        self.assertEqual(
            self.follow(),
            [{"text": caption("–"), "class": "disconnected", "tooltip": "COROS PACE 3 8B7D1F\ndevice not connected"}],
        )

    def test_disconnected_without_device(self):
        self.write_state(status="disconnected", bpm=None, device="", reason="looking for a heart rate device")
        self.assertEqual(self.follow()[0]["tooltip"], "looking for a heart rate device")

    def test_missing_file(self):
        self.assertEqual(self.follow(), [DEAD])

    def test_stale_file(self):
        self.write_state(updated=self.env.clock.now - 16)
        self.assertEqual(self.follow(), [DEAD])

    def test_fresh_within_15s(self):
        self.write_state(updated=self.env.clock.now - 14)
        self.assertEqual(self.follow()[0]["class"], "live")

    def test_goes_stale_while_following(self):
        self.write_state()
        lines = self.follow(polls=17)
        self.assertEqual([line["class"] for line in lines], ["live", "disconnected"])
        self.assertEqual(lines[-1], DEAD)

    def test_prints_only_on_change(self):
        self.write_state()
        self.assertEqual(len(self.follow(polls=5)), 1)

    def test_live_with_null_bpm_renders_waiting(self):
        self.write_state(bpm=None)
        [line] = self.follow()
        self.assertEqual(line["class"], "waiting")
        self.assertEqual(line["text"], caption("…"))
        self.assertNotIn("None", line["tooltip"])
        self.assertEqual(line["tooltip"], "COROS PACE 3 8B7D1F\nwaiting for first reading")


if __name__ == "__main__":
    unittest.main()
