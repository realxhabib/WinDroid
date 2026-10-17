"""Android with a screen-lock PIN boots locked ("direct boot") until the PIN is entered."""
import shlex
import subprocess
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

    def test_androids_lock_screen(self):
        self.assertEqual(vm.parse_keyguard(keyguard_dump()), vm.ASKING)
        self.assertEqual(vm.parse_keyguard(keyguard_dump(views=("Password",))), vm.ASKING)
        self.assertEqual(vm.parse_keyguard(keyguard_dump(device_locked=False, bouncer=False, views=())), vm.UNLOCKED)
        self.assertEqual(vm.parse_keyguard(keyguard_dump(displays=(0, 7))), vm.ASKING)
        for dump in (keyguard_dump(bouncer=False), keyguard_dump(focus="Window{abc u0 PopupWindow}"),
                     keyguard_dump(focus=None), keyguard_dump(views=("SimPin",)), keyguard_dump(views=("PIN", "SimPuk")),
                     keyguard_dump(views=("Pattern",)), keyguard_dump(displays=(7,)), "", "error: closed"):
            self.assertEqual(vm.parse_keyguard(dump), vm.WAITING)
        # Only display 0's focus counts: Android sends `input -d 0` there.
        elsewhere = keyguard_dump(focus=None, displays=(0, 7)).replace(
            "Display: mDisplayId=7 (organized)\n", "Display: mDisplayId=7 (organized)\n  mCurrentFocus=Window{9d9f0ed u0 NotificationShade}\n")
        self.assertEqual(vm.parse_keyguard(elsewhere), vm.WAITING)
        # A missing part of the answer (here SystemUI's) is no verdict, not "asking".
        self.assertEqual(vm.parse_keyguard(keyguard_dump().rsplit("@@next@@", 1)[0]), vm.WAITING)


def keyguard_dump(device_locked=True, focus="NotificationShade", bouncer=True, views=("PIN",), displays=(0,)):
    """vm.KEYGUARD_QUERY's answer, from the lines of a real one (Android 16, PIN set, after a locked boot)."""
    trust = (' User "Owner" (id=0, flags=0x4c13) (current): trustState=UNTRUSTED, trustManaged=0, '
             f'deviceLocked={int(device_locked)}, isActiveUnlockRunning=0, strongAuthRequired=0x{int(device_locked)}\n'
             '   Enabled agents:\n    com.google.android.gms/.auth.trustagent.GoogleTrustAgent\n')
    windows = "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
    for d in displays:
        windows += (f"  Display: mDisplayId={d} (organized)\n    init=720x1280 240dpi\n  mLayoutSeq=55\n"
                    + (f"  mCurrentFocus=Window{{9d9f0ed u0 {focus}}}\n" if d == 0 and focus else "")
                    + f"    mFocusedWindow=Window{{1234abc u0 {focus or 'null'}}}\n")
    systemui = ("SERVICE com.android.systemui/.SystemUIService 6195833 pid=941 user=0\n"
                "    StatusBarKeyguardViewManager:\n"
                f"      isBouncerShowing(): {str(bouncer).lower()}\n"
                "          android.widget.FrameLayout{7d8222 V.E...... ........ 0,0-720,1280 #7f0a0452 app:id/keyguard_bouncer_container}\n"
                "            com.android.keyguard.KeyguardSecurityContainer{b62f4b3 V.E...... ........ 0,0-720,1280 #7f0a0467 app:id/keyguard_security_container}\n")
    for v in views:
        systemui += f"                com.android.keyguard.Keyguard{v}View{{5ec21e9 V.E...... ........ 45,12-675,1124 #7f0a0464 app:id/keyguard_pin_view}}\n"
    systemui += "                com.android.keyguard.KeyguardPasswordView{1111111 G.E...... ......I. 0,0-0,0 #7f0a0465 app:id/keyguard_password_view}\n"
    return f"{trust}@@next@@\n{windows}@@next@@\n{systemui}"


class FakeAndroid:
    """Just enough of `adb shell` for unlocking: the PIN check, the user unlocking a moment later, and
    Android's own lock screen on display 0, which takes the PIN typed on its PIN pad plus Enter."""

    def __init__(self, answer="Lock credential verified successfully", unlocks=True, pad="PIN",
                 focus="NotificationShade", auto_confirm=False):
        self.answer, self.unlocks = answer, unlocks
        self.pad, self.focus, self.auto_confirm = pad, focus, auto_confirm
        self.locked = True
        self.bouncer = False  # the PIN pad, shown by `wm dismiss-keyguard`
        self.keyguard = True  # Android's verdict: deviceLocked
        self.typed, self.pin = "", None
        self.commands: list[str] = []

    def shell(self, command, timeout=30):
        self.commands.append(command)
        if command.startswith("locksettings verify"):
            if "verified" in self.answer and self.unlocks:
                self.locked = False
            self.pin = shlex.split(command)[-1]
            return self.answer
        if "wm dismiss-keyguard" in command and self.keyguard:
            self.bouncer = True
        if command == vm.KEYGUARD_QUERY:
            return keyguard_dump(self.keyguard, self.focus, self.bouncer, (self.pad,) if self.pad else ())
        if command.startswith("input -d 0 text ") and self.asking():
            self.typed += shlex.split(command)[-1].replace("%s", " ")
            if self.auto_confirm:
                self.enter()
        if command == "input -d 0 keyevent KEYCODE_ENTER" and self.asking():
            self.enter()
        return ""

    def asking(self):
        return self.keyguard and self.bouncer and self.focus == "NotificationShade" and self.pad == "PIN"

    def enter(self):
        if self.typed == self.pin:
            self.keyguard = self.bouncer = False
        self.typed = ""

    def user_locked(self):
        return self.locked

    def typing(self):
        return [c for c in self.commands if c.startswith("input -d 0 text")]


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

    def test_right_pin_also_dismisses_androids_lock_screen(self):
        android = FakeAndroid()
        self.assertEqual(self.unlock(android), "")
        self.assertFalse(android.keyguard)  # Android counts as unlocked: deviceLocked=0
        self.assertEqual(android.typing(), [f"input -d 0 text {PIN}"])  # once, and only to display 0
        self.assertIn("input -d 0 keyevent KEYCODE_ENTER", android.commands)
        self.assertIn("vm: Android's lock screen dismissed", self.logged)
        self.assert_pin_never_logged()

    def test_types_only_once_android_accepted_the_pin(self):
        for android in (FakeAndroid(answer=f"Old password '{PIN}' didn't match"), FakeAndroid(unlocks=False),
                        FakeAndroid(answer="Request throttled")):
            self.unlock(android)
            self.assertEqual(android.typing(), [])

    def test_never_types_without_the_focus_on_the_pin_pad(self):
        # Display 0's focus elsewhere (the home screen, or no focus at all because an app's display is
        # on top), a SIM PIN pad, a pattern: the PIN must not be typed. Android is still unlocked.
        for android in (FakeAndroid(focus="com.google.android.apps.nexuslauncher/.NexusLauncherActivity"),
                        FakeAndroid(focus=None), FakeAndroid(pad="SimPin"), FakeAndroid(pad="Pattern"),
                        FakeAndroid(pad=None)):
            self.machine.state = LOCKED
            self.assertEqual(self.unlock(android), "")
            self.assertEqual(self.machine.state, RUNNING)
            self.assertEqual(android.typing(), [])
            self.assertNotIn("input -d 0 keyevent KEYCODE_ENTER", android.commands)
        self.assertIn("vm: Android's lock screen stays up: it didn't ask for the PIN on display 0", self.logged)
        self.assert_pin_never_logged()

    def test_already_unlocked_lock_screen_isnt_typed_on(self):
        android = FakeAndroid()
        android.keyguard = False
        self.assertEqual(self.unlock(android), "")
        self.assertEqual(android.typing(), [])

    def test_no_enter_after_the_pin_pad_confirmed_by_itself(self):
        android = FakeAndroid(auto_confirm=True)
        self.assertEqual(self.unlock(android), "")
        self.assertFalse(android.keyguard)
        self.assertNotIn("input -d 0 keyevent KEYCODE_ENTER", android.commands)  # would go to the home screen

    def test_lock_screen_that_stays_up(self):
        android = FakeAndroid()
        android.enter = lambda: None  # Android ignores the PIN pad
        self.assertEqual(self.unlock(android), "")
        self.assertEqual(self.machine.state, RUNNING)
        self.assertEqual(len(android.typing()), 1)  # never typed again: a miss would count as a wrong try
        self.assertIn("vm: Android's lock screen stays up after the PIN was entered there", self.logged)

    def test_typing_error_doesnt_leak_the_pin(self):
        android = FakeAndroid()
        shell = android.shell

        def hangs_on_typing(command, timeout=30):
            if command.startswith("input -d 0 text"):
                raise subprocess.TimeoutExpired(["adb", "shell", command], timeout)
            return shell(command, timeout)
        android.shell = hangs_on_typing
        self.assertEqual(self.unlock(android), "")
        self.assertEqual(self.machine.state, RUNNING)
        self.assertIn("vm: Android's lock screen stays up: TimeoutExpired", self.logged)
        self.assert_pin_never_logged()

    def test_password_with_spaces_on_the_lock_screen(self):
        android = FakeAndroid()
        self.unlock(android, pin="pa ss'; reboot")
        self.assertEqual(android.typing(), ["input -d 0 text 'pa%sss'\"'\"';%sreboot'"])
        self.assertFalse(android.keyguard)
        self.assert_pin_never_logged("pa ss")

    def test_passwords_input_text_cant_type(self):
        for pin in ("50%sale", "pässword"):
            self.machine.state = LOCKED
            android = FakeAndroid()
            self.assertEqual(self.unlock(android, pin=pin), "")
            self.assertEqual(android.typing(), [])
            self.assert_pin_never_logged(pin)

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

    def test_unlocking_isnt_cut_short(self):
        # The storage is unlocked while unlock() still enters the PIN on Android's lock screen: app
        # windows (RUNNING) would add displays and take display 0's keyboard focus away from it.
        machine = VM()
        machine.state = LOCKED
        with machine._unlocking, mock.patch.object(vm.adb, "online", return_value=True), \
                mock.patch.object(vm.adb, "user_locked", return_value=False), mock.patch.object(vm, "log"):
            self.assertEqual(machine.refresh(), LOCKED)

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
