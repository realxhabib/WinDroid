"""Android with a screen-lock PIN boots locked ("direct boot") until the PIN is entered."""
import threading
import time
import unittest
from unittest import mock

from androiddesktop import adb, catalog, vm
from androiddesktop.vm import LOCKED, OFF, RUNNING, VM

PIN = "4821"


class Detect(unittest.TestCase):
    def test_parse(self):
        self.assertIs(adb.parse_user_locked("true\n", ""), False)
        self.assertIs(adb.parse_user_locked("\n", "RUNNING_LOCKED\n"), True)
        self.assertIs(adb.parse_user_locked("", "RUNNING_UNLOCKING"), True)
        self.assertIs(adb.parse_user_locked("", "RUNNING_UNLOCKED"), False)
        self.assertIsNone(adb.parse_user_locked("", "error: closed"))


class FakeAndroid:
    """Just enough of `adb shell` for unlocking: the PIN check, then the user unlocking a moment later."""

    def __init__(self, answer="Lock credential verified successfully", unlocks=True):
        self.answer, self.unlocks = answer, unlocks
        self.locked = True
        self.commands: list[str] = []

    def shell(self, command, timeout=30):
        self.commands.append(command)
        if command.startswith("locksettings verify"):
            if "verified" in self.answer and self.unlocks:
                self.locked = False
            return self.answer
        return ""

    def user_locked(self):
        return self.locked


class Unlock(unittest.TestCase):
    def setUp(self):
        self.logged: list[str] = []
        for p in (mock.patch.object(vm, "log", self.logged.append), mock.patch.object(vm.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        self.machine = VM()
        self.machine.state = LOCKED

    def unlock(self, android, pin=PIN):
        with mock.patch.object(vm.adb, "shell", android.shell), \
                mock.patch.object(vm.adb, "user_locked", android.user_locked):
            return self.machine.unlock(pin)

    def assert_pin_never_logged(self, pin=PIN):
        self.assertFalse([line for line in self.logged if pin in line])

    def test_right_pin(self):
        android = FakeAndroid()
        self.assertEqual(self.unlock(android), "")
        self.assertEqual(self.machine.state, RUNNING)
        self.assertTrue(any("dismiss-keyguard" in c for c in android.commands))
        self.assert_pin_never_logged()

    def test_wrong_pin(self):
        # Android's own message repeats the PIN: it must not reach the log or the user.
        android = FakeAndroid(answer=f"Old password '{PIN}' didn't match")
        message = self.unlock(android)
        self.assertIn("not the right PIN", message)
        self.assertNotIn(PIN, message)
        self.assertEqual(self.machine.state, LOCKED)
        self.assert_pin_never_logged()

    def test_throttled(self):
        message = self.unlock(FakeAndroid(answer="Request throttled"))
        self.assertIn("Wait", message)
        self.assertEqual(self.machine.state, LOCKED)

    def test_accepted_but_still_locked(self):
        message = self.unlock(FakeAndroid(unlocks=False))
        self.assertIn("still unlocking", message)
        self.assertEqual(self.machine.state, LOCKED)

    def test_password_is_quoted_for_androids_shell(self):
        android = FakeAndroid()
        self.unlock(android, pin="pa ss'; reboot")
        self.assertEqual(android.commands[0], "locksettings verify --old 'pa ss'\"'\"'; reboot'")
        self.assert_pin_never_logged("pa ss")

    def test_rejects_junk_without_asking_android(self):
        android = FakeAndroid()
        for pin in ("", "12\n34", "x" * 65):
            self.assertIn("Enter the PIN", self.unlock(android, pin=pin))
        self.assertEqual(android.commands, [])

    def test_not_locked(self):
        self.machine.state = RUNNING
        android = FakeAndroid()
        self.assertEqual(self.unlock(android), "")
        self.assertEqual(android.commands, [])


class States(unittest.TestCase):
    def test_adopting_a_locked_android(self):
        machine = VM()
        with mock.patch.object(vm.adb, "online", return_value=True), \
                mock.patch.object(vm.adb, "booted", return_value=True), \
                mock.patch.object(vm.adb, "user_locked", return_value=True):
            self.assertEqual(machine.refresh(), LOCKED)

    def test_unlocked_elsewhere(self):
        machine = VM()
        machine.state = LOCKED
        with mock.patch.object(vm.adb, "online", return_value=True), \
                mock.patch.object(vm.adb, "user_locked", return_value=False), mock.patch.object(vm, "log"):
            self.assertEqual(machine.refresh(), RUNNING)

    def test_a_locked_android_that_dies_is_noticed(self):
        machine = VM()
        machine.state = LOCKED
        with mock.patch.object(vm.adb, "online", return_value=False), mock.patch.object(vm.time, "sleep"), \
                mock.patch.object(vm, "log"):
            self.assertEqual(machine.refresh(), OFF)

    def test_waiting_callers_can_opt_out(self):
        machine = VM()
        with mock.patch.object(vm.adb, "online", return_value=True), \
                mock.patch.object(vm.adb, "booted", return_value=True), \
                mock.patch.object(vm.adb, "user_locked", return_value=True):
            self.assertFalse(machine.ensure_running(timeout=5, wait_for_unlock=False))

    def test_windows_wait_for_the_unlock(self):
        machine = VM()
        machine.state = LOCKED
        unlocked = {"now": False}
        with mock.patch.object(vm.adb, "online", return_value=True), \
                mock.patch.object(vm.adb, "user_locked", side_effect=lambda: not unlocked["now"]):
            threading.Timer(0.3, lambda: machine._set(RUNNING)).start()
            started = time.monotonic()
            self.assertTrue(machine.ensure_running(timeout=0.1))  # the timeout is on hold while locked
            self.assertGreater(time.monotonic() - started, 0.25)


class AppListWhileLocked(unittest.TestCase):
    def test_keeps_the_known_apps(self):
        apps = catalog.Catalog.__new__(catalog.Catalog)
        apps.apps = {"com.android.vending": catalog.App("com.android.vending", "x", "Play Store", True, 1)}
        apps._lock = threading.Lock()
        apps.listeners = []
        with mock.patch.object(catalog.adb, "user_locked", return_value=True), \
                mock.patch.object(catalog.adb, "run", side_effect=AssertionError("asked Android while locked")):
            self.assertFalse(apps.refresh())
        self.assertIn("com.android.vending", apps.apps)


if __name__ == "__main__":
    unittest.main()
