import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import checks, state, vm
from androiddesktop.vm import OFF, RUNNING, VM


class State(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = Path(self.dir.name)
        patches = [mock.patch.object(state, "DATA", path), mock.patch.object(state, "STATE", path / "state.json"),
                   mock.patch.object(state, "_data", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.dir.cleanup)

    def test_round_trip(self):
        state.put("windows", {"com.android.chrome": [10, 20, 412, 860, False]})
        state._data = None  # as if freshly started
        self.assertEqual(state.get("windows"), {"com.android.chrome": [10, 20, 412, 860, False]})
        self.assertEqual(json.loads(state.STATE.read_text())["windows"]["com.android.chrome"][2], 412)

    def test_missing_or_corrupt_file(self):
        self.assertEqual(state.get("density", 1.0), 1.0)
        state.STATE.write_text("{not json")
        state._data = None
        self.assertIsNone(state.get("windows"))


class FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class CrashDetection(unittest.TestCase):
    def running_vm(self, proc=None):
        machine = VM()
        machine.state = RUNNING
        machine._ready.set()
        machine._proc = proc
        return machine

    def test_exited_emulator_is_noticed(self):
        machine = self.running_vm(FakeProc(-1073741819))
        with mock.patch.object(vm.adb, "online", return_value=True):
            self.assertEqual(machine.refresh(), OFF)
        self.assertEqual(machine.detail, "Android stopped unexpectedly.")
        self.assertFalse(machine._ready.is_set())

    def test_adb_losing_the_device_for_good_is_noticed(self):
        machine = self.running_vm()
        with mock.patch.object(vm.adb, "online", return_value=False), mock.patch.object(vm.time, "sleep"):
            self.assertEqual(machine.refresh(), OFF)

    def test_an_adb_hiccup_is_not_a_crash(self):
        machine = self.running_vm()
        answers = iter([False, True])
        with mock.patch.object(vm.adb, "online", side_effect=lambda: next(answers)), \
                mock.patch.object(vm.time, "sleep"):
            self.assertEqual(machine.refresh(), RUNNING)

    def test_healthy(self):
        machine = self.running_vm(FakeProc(None))
        with mock.patch.object(vm.adb, "online", return_value=True):
            self.assertEqual(machine.refresh(), RUNNING)


class BootThatCannotHappen(unittest.TestCase):
    def test_fails_fast_with_the_reason(self):
        problem = checks.Check("hypervisor", checks.BLOCKING, "Turn on Windows Hypervisor Platform")
        machine = VM()
        with mock.patch.object(vm.checks, "blocking", return_value=problem), \
                mock.patch.object(vm.adb, "online", return_value=False):
            started = time.monotonic()
            self.assertFalse(machine.ensure_running(timeout=60))
        self.assertLess(time.monotonic() - started, 5)  # not the full timeout
        self.assertEqual((machine.state, machine.detail), (OFF, "Turn on Windows Hypervisor Platform"))
        self.assertTrue(machine.keep_running)

    def test_stop_means_windows_should_not_reconnect(self):
        machine = VM()
        machine.keep_running = True
        with mock.patch.object(vm.adb, "online", return_value=False):
            machine.stop()
        self.assertFalse(machine.keep_running)


if __name__ == "__main__":
    unittest.main()
