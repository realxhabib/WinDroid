"""Recording hidden agent sessions for demo videos (agentrec.py, and its hooks in ghost.py)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import agentrec, ghost
from androiddesktop.ghost import Sessions
from tests.test_ghost import APPS, STACK_LIST, FakeAndroid, FakeHelper


class Switch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(agentrec, "RECORDING", Path(self._tmp.name) / "agent-recording.json")
        p.start()
        self.addCleanup(p.stop)

    def test_off_by_default(self):
        self.assertIsNone(agentrec.target())

    def test_on_until_it_runs_out(self):
        folder = Path(self._tmp.name) / "demo"
        agentrec.switch_on(folder, minutes=5)
        self.assertEqual(agentrec.target(), folder)
        with mock.patch.object(agentrec.time, "time", return_value=time.time() + 600):
            self.assertIsNone(agentrec.target())
        agentrec.switch_off()
        self.assertIsNone(agentrec.target())


class Recording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name) / "demo"
        self.android, self.helper = FakeAndroid(), FakeHelper()
        patches = [mock.patch.object(ghost.adb, "shell", self.android.shell),
                   mock.patch.object(ghost.adb, "online", return_value=True),
                   mock.patch.object(ghost.adb, "booted", return_value=True),
                   mock.patch.object(ghost.adb, "user_locked", return_value=False),
                   mock.patch.object(ghost, "agent_log"),
                   mock.patch.object(agentrec, "target", return_value=self.folder)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.android.stack = STACK_LIST.replace("displayId=3", "displayId=0")
        self.sessions = Sessions(self.helper)
        self.sessions._apps = dict(APPS)

    def events(self, s):
        return [json.loads(line) for line in (s.recorder.dir / "events.jsonl").read_text().splitlines()]

    def test_records_each_step_with_its_screen(self):
        s = self.sessions.start("settings")
        self.sessions.tap(s.id, x=100, y=200)
        self.sessions.type_text(s.id, "hunter2")
        self.sessions.press(s.id, "back")
        steps = self.events(s)
        self.assertEqual([e["what"] for e in steps], ["open", "tap", "type", "press"])
        self.assertEqual((steps[1]["x"], steps[1]["y"]), (100, 200))
        self.assertEqual(steps[2]["length"], 7)
        self.assertNotIn("hunter2", (s.recorder.dir / "events.jsonl").read_text())
        self.assertEqual(sorted(p.name for p in (s.recorder.dir / "frames").iterdir()),
                         ["0000.jpg", "0001.jpg", "0002.jpg", "0003.jpg"])
        meta = json.loads((s.recorder.dir / "session.json").read_text())
        self.assertEqual((meta["package"], meta["size"]), ("com.android.settings", list(ghost.SIZE)))

    def test_a_secure_screen_is_never_saved(self):
        s = self.sessions.start("settings")
        self.android.secure = "  Window #4 Window{1 u0 PinPad}:\n    mDisplayId=6\n      fl=SECURE\n    isOnScreen=true\n"
        with mock.patch.object(ghost, "parse_secure_displays", return_value={6}):
            s.recorder.frames_before = s.recorder.frames
            self.sessions._record(s, "tap", x=1, y=2)
        last = self.events(s)[-1]
        self.assertTrue(last.get("secure"))
        self.assertNotIn("frame", last)
        self.assertEqual(s.recorder.frames, s.recorder.frames_before)

    def test_a_recording_problem_never_stops_the_agent(self):
        s = self.sessions.start("settings")
        with mock.patch.object(s.recorder, "step", side_effect=OSError("disk full")):
            self.assertIn("Tapped", self.sessions.tap(s.id, x=5, y=5))

    def test_off_means_no_recorder(self):
        with mock.patch.object(agentrec, "target", return_value=None):
            s = self.sessions.start("settings")
        self.assertIsNone(s.recorder)


if __name__ == "__main__":
    unittest.main()
