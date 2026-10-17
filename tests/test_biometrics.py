"""Windows Hello for Android's fingerprint prompts: reading what Android says, and what we do about it."""
import ctypes
import unittest
import uuid
from unittest import mock

import win32con
import win32gui

from androiddesktop import biometrics, camera, hello, runtime
from androiddesktop.biometrics import Operation

IDLE = """\
Dumping for sensorId: 0, provider: FingerprintProvider
Fps state: 0
{"service":"FingerprintProvider\\/default","prints":[{"id":0,"count":0,"accept":0,"reject":0,"acquire":0,"lockout":0,"permanentLockout":0,"acceptCrypto":0,"rejectCrypto":0,"acquireCrypto":0}]}
HAL deaths since last reboot: 0
---AuthSessionCoordinator logs begin---
---AuthSessionCoordinator logs end  ---
Dump of BiometricScheduler BiometricScheduler
Type: 3
Current operation: null
Pending operations: 0
"""

AUTH = IDLE.replace('"count":0', '"count":1').replace(
    "Current operation: null",
    "Current operation: {[37] com.android.server.biometrics.sensors.fingerprint.aidl.FingerprintAuthenticationClient, "
    "proto=3, owner=com.google.android.gms, cookie=0, requestId=12, userId=0}, State: 2")

WAITING = AUTH.replace("State: 2", "State: 1")

ENROLL = IDLE.replace(
    "Current operation: null",
    "Current operation: {[41] com.android.server.biometrics.sensors.fingerprint.aidl.FingerprintEnrollClient, "
    "proto=1, owner=com.android.settings, cookie=0, requestId=-1, userId=0}, State: 2")

STACKS = """\
RootTask id=61 bounds=[0,0][392,822] displayId=7 userId=0
 configuration={1.0 310mcc260mnc [en_US] ldltr sw392dp w392dp h822dp 160dpi}
  taskId=61: com.android.chrome/org.chromium.chrome.browser.ChromeTabbedActivity bounds=[0,0][392,822] userId=0 visible=true topActivity=ComponentInfo{com.google.android.gms/com.google.android.gms.fido.fido2.ui.Fido2FullScreenActivity}

RootTask id=62 bounds=[0,0][392,822] displayId=8 userId=0
  taskId=62: com.android.vending/com.android.vending.AssetBrowserActivity bounds=[0,0][392,822] userId=0 visible=true topActivity=ComponentInfo{com.android.vending/com.android.vending.AssetBrowserActivity}

RootTask id=1 bounds=[0,0][720,1280] displayId=0 userId=0
  taskId=2: com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity bounds=[0,0][720,1280] userId=0 visible=true topActivity=ComponentInfo{com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity}
  taskId=9: com.android.vending/com.android.vending.AssetBrowserActivity bounds=[0,0][720,1280] userId=0 visible=false topActivity=ComponentInfo{com.android.vending/com.android.vending.AssetBrowserActivity}
"""


PROMPT_WINDOW = """\
  Window #7 Window{5b1c0a1 u0 NotificationShade}:
    mDisplayId=0 mSession=Session{2ff6acf 925:u0a10187} mClient=android.os.BinderProxy@7c1e2f0
  Window #8 Window{2899270 u0 BiometricPrompt}:
    mDisplayId=0 mSession=Session{2ff6acf 925:u0a10187} mClient=android.os.BinderProxy@c49ea22
"""


class Parsing(unittest.TestCase):
    def test_idle(self):
        self.assertIsNone(biometrics.parse_operation(IDLE))
        self.assertEqual(biometrics.parse_enrolled(IDLE), 0)

    def test_prompt_waiting_for_a_finger(self):
        self.assertEqual(biometrics.parse_operation(AUTH), Operation(37, "auth", "com.google.android.gms", True))
        self.assertEqual(biometrics.parse_enrolled(AUTH), 1)

    def test_seen_on_android_16(self):
        # The test app's BiometricPrompt, as `dumpsys fingerprint` showed it on the emulator.
        line = ("Current operation: {[13] com.android.server.biometrics.sensors.fingerprint.aidl."
                "FingerprintAuthenticationClient, proto=3, owner=bio.test, cookie=1156538622, requestId=2, userId=0}, State: 2")
        self.assertEqual(biometrics.parse_operation(IDLE.replace("Current operation: null", line)),
                         Operation(13, "auth", "bio.test", True))

    def test_prompt_still_opening(self):
        # A BiometricPrompt's client waits for its dialog first: a touch then would go nowhere.
        self.assertFalse(biometrics.parse_operation(WAITING).started)

    def test_enrollment(self):
        self.assertEqual(biometrics.parse_operation(ENROLL), Operation(41, "enroll", "com.android.settings", True))

    def test_lock_screen(self):
        out = AUTH.replace("owner=com.google.android.gms", "owner=com.android.systemui")
        self.assertEqual(biometrics.parse_operation(out).owner, biometrics.LOCK_SCREEN)

    def test_other_operations_and_junk(self):
        out = IDLE.replace("Current operation: null", "Current operation: {[5] com.x.FingerprintInternalCleanupClient, "
                                                      "proto=7, owner=android, cookie=0, requestId=-1, userId=0}, State: 2")
        self.assertEqual(biometrics.parse_operation(out).kind, "other")
        self.assertIsNone(biometrics.parse_operation("error: device offline"))
        self.assertIsNone(biometrics.parse_enrolled("error: device offline"))

    def test_several_sensors_add_up(self):
        self.assertEqual(biometrics.parse_enrolled('"prints":[{"id":0,"count":2},{"id":1,"count":1}]'), 3)

    def test_prompt_dialog_display(self):
        self.assertEqual(biometrics.parse_prompt_display(PROMPT_WINDOW), 0)
        self.assertEqual(biometrics.parse_prompt_display(PROMPT_WINDOW.replace("mDisplayId=0 mSession=Session{2ff6acf 925:u0a10187} mClient=android.os.BinderProxy@c49ea22", "mDisplayId=5")), 5)
        self.assertIsNone(biometrics.parse_prompt_display(PROMPT_WINDOW.split("  Window #8")[0]))

    def test_app_displays(self):
        # A passkey sheet (Google Play services) on top of Chrome's task, on Chrome's display.
        self.assertEqual(biometrics.parse_app_displays(STACKS, "com.google.android.gms"), [7])
        # Visible on top first, then anywhere else.
        self.assertEqual(biometrics.parse_app_displays(STACKS, "com.android.vending"), [8, 0])
        self.assertEqual(biometrics.parse_app_displays(STACKS, "com.android.chrome"), [7])
        self.assertEqual(biometrics.parse_app_displays(STACKS, "com.example.none"), [])


class FakeWindow:
    def __init__(self, package, display, hwnd, active=False):
        self.app = mock.Mock(package=package)
        self.display_id, self.hwnd, self.active = display, hwnd, active
        self.protected, self.closing = None, False


class Prompts(unittest.TestCase):
    """What the bridge does with a prompt: Windows Hello above the asking window, then the finger."""

    def setUp(self):
        self.manager = mock.Mock()
        self.manager.vm.state = biometrics.RUNNING
        self.chrome = FakeWindow("com.android.chrome", 7, 0x700)
        self.play = FakeWindow("com.android.vending", 8, 0x800, active=True)
        self.manager.windows = {"a": self.chrome, "b": self.play}
        self.bridge = biometrics.Bridge(self.manager, lambda p: {"com.google.android.gms": "Google Play services"}.get(p, p),
                                        lambda: 0x111)
        self.bridge.available = True
        self.bridge._spawn = lambda fn, *args: fn(*args)  # the prompt, here and now
        for p in (mock.patch.object(biometrics, "log"), mock.patch.object(biometrics.time, "sleep"),
                  mock.patch.object(biometrics.win32gui, "GetForegroundWindow", return_value=0)):
            p.start()
            self.addCleanup(p.stop)

    def run_check(self, states, verdict=hello.VERIFIED, prompt_display=0):
        """states: what `dumpsys fingerprint` says on each look (the last one repeats)."""
        looks = list(states)
        self.sent = []

        def shell(command, timeout=0):
            if "stack list" in command:
                return STACKS
            if "dumpsys window" in command:
                return PROMPT_WINDOW.replace("mDisplayId=0", f"mDisplayId={prompt_display}")
            if command.startswith("input"):
                self.sent.append(command)
                return ""
            return looks.pop(0) if len(looks) > 1 else looks[0]

        with mock.patch.object(biometrics.adb, "shell", side_effect=shell), \
                mock.patch.object(biometrics.hello, "verify", return_value=verdict) as verify, \
                mock.patch.object(biometrics, "touch", return_value=True) as touch:
            self.bridge._check()
        return verify, touch

    def test_verified_prompt_gets_the_finger(self):
        verify, touch = self.run_check([AUTH])
        verify.assert_called_once()
        hwnd, message = verify.call_args.args
        self.assertEqual(hwnd, 0x700)  # above Chrome's window, where the passkey sheet is
        self.assertIn("Google Play services", message)
        touch.assert_called_once()
        self.assertEqual(self.sent, [])

    def test_asked_once_per_prompt(self):
        self.run_check([AUTH])
        verify, touch = self.run_check([AUTH])
        verify.assert_not_called()
        touch.assert_not_called()

    def test_canceled_hello_cancels_androids_hidden_prompt(self):
        # Android draws BiometricPrompt on its main display, which no window shows: Back there
        # cancels it, so the app hears "canceled" instead of waiting forever.
        verify, touch = self.run_check([AUTH], verdict=hello.CANCELED)
        verify.assert_called_once()
        touch.assert_not_called()
        self.assertEqual(self.sent, ["input -d 0 keyevent 4"])

    def test_canceled_hello_leaves_a_visible_prompt_alone(self):
        # If Android ever draws it in the app's window, the user can still use it (e.g. "Use PIN").
        self.run_check([AUTH], verdict=hello.CANCELED, prompt_display=7)
        self.assertEqual(self.sent, [])

    def test_prompt_gone_by_the_time_hello_answers(self):
        for verdict in (hello.VERIFIED, hello.CANCELED):
            self.bridge._handled.clear()
            verify, touch = self.run_check([AUTH, IDLE], verdict=verdict)
            touch.assert_not_called()
            self.assertEqual(self.sent, [])

    def test_hello_goes_away_with_the_prompt(self):
        verify, _ = self.run_check([AUTH], verdict=hello.CANCELED)
        wanted = verify.call_args.kwargs["wanted"]
        self.assertTrue(wanted())
        self.run_check([IDLE])  # the scheduler logged the operation finishing: the watcher looks again
        self.assertFalse(wanted())  # the app gave up, so the Windows Hello prompt closes

    def test_wakes_only_for_the_scheduler(self):
        lines = ["D/BiometricScheduler(  664): [Started] {[12] ...FingerprintAuthenticationClient, owner=com.foo}",
                 "D/BiometricStateCallback(  664): Enrollment state changed: true",
                 "D/BiometricService(  664): canAuthenticate: User=0, Caller=0, Authenticators=15",
                 "I/AconfigPackage(28365): com.android.server.biometrics is mapped to system",
                 "--------- beginning of system"]
        self.assertEqual([bool(biometrics.WAKE.search(line)) for line in lines], [True, True, False, False, False])
        self.assertIn("-s BiometricScheduler:V", biometrics.LOGCAT)

    def test_hello_waits_as_long_as_android_does(self):
        # No time limit of its own: giving up while Android still asks would cancel the app's prompt
        # under a user who's on their way to verify.
        verify, _ = self.run_check([AUTH])
        self.assertIsNone(verify.call_args.kwargs["timeout"])

    def test_hello_goes_away_when_android_stops(self):
        with mock.patch.object(biometrics.adb, "shell", side_effect=lambda c, timeout=0: STACKS if "stack" in c else AUTH), \
                mock.patch.object(biometrics.hello, "verify", return_value=hello.CANCELED) as verify:
            self.bridge._check()
            wanted = verify.call_args.kwargs["wanted"]
        with mock.patch.object(biometrics.adb, "shell", side_effect=OSError("adb: device offline")):
            self.assertTrue(wanted())  # can't tell for a moment: keep asking
            self.manager.vm.state = "off"
            self.assertFalse(wanted())

    def test_waits_for_the_dialog(self):
        verify, touch = self.run_check([WAITING])
        verify.assert_not_called()

    def test_nothing_without_windows_hello_or_a_fingerprint(self):
        self.bridge.available = False
        verify, _ = self.run_check([AUTH])
        verify.assert_not_called()
        self.bridge.available = True
        verify, _ = self.run_check([AUTH.replace('"count":1', '"count":0')])
        verify.assert_not_called()

    def test_lock_screen_goes_above_our_fallback_window(self):
        verify, _ = self.run_check([AUTH.replace("com.google.android.gms", "com.android.systemui")] * 2)
        hwnd, message = verify.call_args.args
        self.assertEqual(hwnd, 0x111)
        self.assertIn("Unlock Android", message)

    def test_enrollment_touches_only_during_setup(self):
        verify, touch = self.run_check([ENROLL])
        touch.assert_not_called()
        self.bridge.enroll()
        verify, touch = self.run_check([ENROLL])
        touch.assert_called_once()
        verify.assert_not_called()  # enrolling needs no Windows Hello: Android asked for its PIN already

    def test_enrollment_finishing_ends_setup(self):
        notices = []
        self.bridge.on_notice = notices.append
        self.run_check([IDLE])
        self.bridge.enroll()
        self.run_check([IDLE.replace('"count":0', '"count":1')])
        self.assertFalse(self.bridge.enrolling)
        self.assertTrue(notices)


class Cameras(unittest.TestCase):
    LISTING = """List of web cameras connected to the computer:
 Camera 'MX Brio' can be specified by label as 'webcam0' or by id as '\\\\?\\usb#vid_046d&pid_0944' and will use pixel format 'NV12'
 Camera 'Integrated Webcam' can be specified by label as 'webcam1' or by id as '\\\\?\\usb#vid_0c45&pid_672e' and will use pixel format 'NV12'
"""

    def test_parse(self):
        self.assertEqual(runtime.parse_webcams(self.LISTING), [("MX Brio", "webcam0"), ("Integrated Webcam", "webcam1")])
        self.assertEqual(runtime.parse_webcams("List of web cameras connected to the computer:\n"), [])

    def test_pick(self):
        self.assertEqual(runtime.pick_cameras(runtime.parse_webcams(self.LISTING)),
                         {"hw.camera.front": "webcam0", "hw.camera.back": "none"})
        self.assertEqual(runtime.pick_cameras([]), {"hw.camera.front": "none", "hw.camera.back": "none"})

    def test_pick_the_android_desktop_camera(self):
        listing = self.LISTING + (
            " Camera 'Android Desktop Camera (Windows Virtual Camera)' can be specified by label as 'webcam2' or by "
            "id as '\\\\?\\swd#vcamdevapi#9dd4ed72f8fb#{e5323777-f976-4f5b-9b55-b94699c46e44}' and will use pixel "
            "format 'NV12'\n")
        webcams = runtime.parse_webcams(listing)
        self.assertEqual(webcams[2], ("Android Desktop Camera (Windows Virtual Camera)", "webcam2"))
        self.assertTrue(camera.is_ours(webcams[2][0]))
        self.assertFalse(camera.is_ours("Android Desktop Camera Pro"))
        self.assertEqual(runtime.pick_cameras(webcams)["hw.camera.front"], "webcam2")
        # Without a webcam to read, it only shows black: no camera, as without it.
        self.assertEqual(runtime.pick_cameras([(camera.NAME, "webcam0")])["hw.camera.front"], "none")


class CameraInstall(unittest.TestCase):
    def test_each_build_gets_its_own_file(self):
        self.assertRegex(camera.install_name(b"one"), r"^AndroidDesktopCamera-[0-9a-f]{12}\.dll$")
        self.assertNotEqual(camera.install_name(b"one"), camera.install_name(b"two"))

    def test_up_to_date(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            built = Path(d, "AndroidDesktopCamera.dll")
            built.write_bytes(b"build")
            with mock.patch.object(camera, "DLL", built), \
                    mock.patch.object(camera, "installed_dll", return_value=Path(d, camera.install_name(b"build"))):
                self.assertTrue(camera.up_to_date())
            with mock.patch.object(camera, "DLL", built), \
                    mock.patch.object(camera, "installed_dll", return_value=Path(d, camera.install_name(b"old"))):
                self.assertFalse(camera.up_to_date())
            with mock.patch.object(camera, "DLL", built), mock.patch.object(camera, "installed_dll", return_value=None):
                self.assertFalse(camera.up_to_date())

    def test_test_pattern_flag(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, mock.patch.object(camera, "TEST_FLAG", Path(d, "x", "flag")):
            camera.test_pattern(True)
            self.assertTrue(camera.TEST_FLAG.exists())
            camera.test_pattern(False)
            camera.test_pattern(False)
            self.assertFalse(camera.TEST_FLAG.exists())


class Hello(unittest.TestCase):
    def test_generic_interface_id(self):
        # IAsyncOperation<UserConsentVerificationResult>, as the Windows SDK headers have it.
        self.assertEqual(hello.IID_VERIFY_OPERATION, uuid.UUID("fd596ffd-2318-558f-9dbe-d21df43764a5"))

    def test_pin_round_trip_is_encrypted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hello, "PIN_FILE", Path(d) / "pin.bin"), \
                mock.patch.object(hello, "DATA", Path(d)):
            self.assertIsNone(hello.remembered_pin())
            hello.remember_pin("4821")
            self.assertNotIn(b"4821", hello.PIN_FILE.read_bytes())
            self.assertEqual(hello.remembered_pin(), "4821")
            hello.forget_pin()
            self.assertFalse(hello.has_pin())
            hello.PIN_FILE.write_bytes(b"junk")
            self.assertIsNone(hello.remembered_pin())


class Clock:
    """Stands in for hello's `time`: sleeping moves the clock on."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeOperation:
    """hello._call on an IAsyncOperation<UserConsentVerificationResult> that's answered at answer_at
    seconds (never if None)."""

    def __init__(self, clock, answer_at=None, answer=hello.VERIFIED):
        self.clock, self.answer_at, self.answer = clock, answer_at, answer
        self.canceled_at = None

    def call(self, obj, index, *args):
        if index == 7:  # get_Status
            done = self.answer_at is not None and self.clock.now >= self.answer_at
            args[0][1]._obj.value = hello.COMPLETED if done else hello.STARTED
        elif index == 8:  # GetResults
            args[0][1]._obj.value = self.answer
        elif index == 9:  # Cancel
            self.canceled_at = self.clock.now


class FakeDesktop:
    """win32gui as hello sees it: top-level windows, hwnd -> (class, owner). SC_CLOSE closes a window
    unless it's stubborn; any other message is refused, as CredentialUIBroker refuses WM_CLOSE."""
    error = win32gui.error

    def __init__(self, windows, stubborn=()):
        self.windows, self.stubborn, self.posted = dict(windows), set(stubborn), []

    def EnumWindows(self, fn, extra):
        for h in list(self.windows):
            fn(h, extra)

    def GetClassName(self, h):
        return self.windows[h][0]

    def IsWindowVisible(self, h):
        return True

    def GetWindow(self, h, which):
        return self.windows[h][1]

    def PostMessage(self, h, message, wparam, lparam):
        if message != win32con.WM_SYSCOMMAND:
            raise self.error(5, "PostMessage", "Access is denied.")
        self.posted.append(h)
        if wparam == win32con.SC_CLOSE and h not in self.stubborn:
            del self.windows[h]


class HelloGivesUp(unittest.TestCase):
    """When verify stops waiting (time's up, or the prompt isn't wanted any more), the Windows Hello
    dialog goes too: canceling the request alone leaves it up (seen 2026-10-15), and the user then
    verifies for nothing."""
    OURS, THEIRS = 0x700, 0x900  # the windows the dialogs are above

    def setUp(self):
        self.clock = Clock()
        for p in (mock.patch.object(hello, "time", self.clock), mock.patch.object(hello, "log")):
            p.start()
            self.addCleanup(p.stop)

    # ------------------------------------------------------------------ waiting
    def result(self, op, timeout, wanted=None):
        with mock.patch.object(hello, "_query", return_value=ctypes.c_void_p(1)), \
                mock.patch.object(hello, "_release"), mock.patch.object(hello, "_call", side_effect=op.call):
            return hello._result(ctypes.c_void_p(2), timeout, wanted)

    def test_answered(self):
        op = FakeOperation(self.clock, answer_at=5)
        self.assertEqual(self.result(op, 120), hello.VERIFIED)
        self.assertIsNone(op.canceled_at)

    def test_time_up(self):
        op = FakeOperation(self.clock)
        self.assertIsNone(self.result(op, 120))
        self.assertAlmostEqual(op.canceled_at, 120, delta=0.1)

    def test_no_time_limit_while_wanted(self):
        op = FakeOperation(self.clock)
        self.assertIsNone(self.result(op, None, wanted=lambda: self.clock.now < 600))
        self.assertAlmostEqual(op.canceled_at, 600, delta=1.1)  # wanted is asked about once a second
        op = FakeOperation(self.clock, answer_at=self.clock.now + 900)
        self.assertEqual(self.result(op, None, wanted=lambda: True), hello.VERIFIED)
        self.assertIsNone(op.canceled_at)

    # ------------------------------------------------------------------ verify
    def verify(self, waited, closes=True):
        def call(obj, index, *args):
            if index == 6:  # RequestVerificationForWindowAsync: the operation comes out
                args[3][1]._obj.value = 0x1234

        with mock.patch.object(hello, "_factory", return_value=ctypes.c_void_p(1)), \
                mock.patch.object(hello, "_hstring", return_value=ctypes.c_void_p(2)), \
                mock.patch.object(hello, "combase"), mock.patch.object(hello, "_release"), \
                mock.patch.object(hello, "_call", side_effect=call), \
                mock.patch.object(hello, "_dialogs", return_value={0x51}), \
                mock.patch.object(hello, "_result", return_value=waited), \
                mock.patch.object(hello, "_close_dialog", return_value=closes) as close:
            return hello.verify(self.OURS, "Confirm it's you", timeout=None, wanted=lambda: True), close

    def test_giving_up_closes_the_dialog(self):
        result, close = self.verify(None)
        self.assertEqual(result, hello.CANCELED)
        close.assert_called_once_with(self.OURS, {0x51})  # with the dialogs that were up before we asked
        hello.log.assert_not_called()

    def test_an_answer_leaves_the_dialog_to_windows(self):
        for answer in (hello.VERIFIED, hello.CANCELED):  # the user canceled it themselves
            result, close = self.verify(answer)
            self.assertEqual(result, answer)
            close.assert_not_called()

    def test_a_dialog_that_wont_close_is_logged(self):
        result, _ = self.verify(None, closes=False)
        self.assertEqual(result, hello.CANCELED)
        hello.log.assert_called_once()

    # ------------------------------------------------------------------ closing the dialog
    def close(self, windows, before=(), stubborn=()):
        desktop = FakeDesktop(windows, stubborn)
        with mock.patch.object(hello, "win32gui", desktop):
            closed = hello._close_dialog(self.OURS, hello._dialogs() & set(before))
        return closed, desktop

    def test_closes_only_our_new_dialog(self):
        windows = {0x51: (hello.DIALOG_CLASS, self.THEIRS),  # another app's, up before we asked
                   0x52: (hello.DIALOG_CLASS, self.OURS),
                   0x53: (hello.DIALOG_CLASS, self.THEIRS),  # another app's, new
                   0x54: ("Chrome_WidgetWin_1", 0)}
        closed, desktop = self.close(windows, before={0x51})
        self.assertTrue(closed)
        self.assertEqual(desktop.posted, [0x52])
        self.assertEqual(set(desktop.windows), {0x51, 0x53, 0x54})

    def test_closes_it_while_its_still_opening(self):
        # The dialog has no owner for a moment as it opens.
        closed, desktop = self.close({0x52: (hello.DIALOG_CLASS, 0)})
        self.assertTrue(closed)
        self.assertEqual(desktop.posted, [0x52])

    def test_nothing_to_close(self):
        closed, desktop = self.close({0x54: ("Chrome_WidgetWin_1", 0)})
        self.assertTrue(closed)
        self.assertEqual(desktop.posted, [])
        self.assertLess(self.clock.now, 1.5)  # looked for a second, in case it was still coming up

    def test_a_dialog_that_stays(self):
        closed, desktop = self.close({0x52: (hello.DIALOG_CLASS, self.OURS)}, stubborn={0x52})
        self.assertFalse(closed)
        self.assertEqual(desktop.posted, [0x52] * 3)  # asked again each second, for 3 s


class UnlockWithHello(unittest.TestCase):
    """The opt-in: the PIN kept with DPAPI, sent to Android after Windows Hello. Never logged."""
    PIN = "4821"

    def setUp(self):
        import tempfile
        from pathlib import Path
        from androiddesktop import app
        from androiddesktop.vm import LOCKED, RUNNING
        self.app, self.LOCKED, self.RUNNING = app, LOCKED, RUNNING
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.logged = []
        for p in (mock.patch.object(hello, "PIN_FILE", Path(d.name) / "pin.bin"), mock.patch.object(hello, "DATA", Path(d.name)),
                  mock.patch.object(app, "log", self.logged.append)):
            p.start()
            self.addCleanup(p.stop)
        self.agent = app.Agent(show_launcher=False)
        self.agent.fingerprint.available = True
        self.agent.vm.state = LOCKED
        self.tried = []

        def unlock(pin):
            self.tried.append(pin)
            if pin != self.PIN:
                return "That's not the right PIN. Try again."
            self.agent.vm.state = RUNNING
            return ""
        self.agent.vm.unlock = unlock

    def tearDown(self):
        self.assertFalse([line for line in self.logged if self.PIN in line])

    def test_remembered_only_when_asked_and_right(self):
        self.agent.unlock("1111", remember=True)
        self.assertFalse(hello.has_pin())
        self.agent.unlock(self.PIN, remember=True)
        self.assertEqual(hello.remembered_pin(), self.PIN)

    def test_unticking_forgets_but_a_hidden_box_changes_nothing(self):
        hello.remember_pin(self.PIN)
        self.agent.unlock(self.PIN, remember=None)
        self.assertTrue(hello.has_pin())
        self.agent.vm.state = self.LOCKED
        self.agent.unlock(self.PIN, remember=False)
        self.assertFalse(hello.has_pin())

    def test_hello_unlock(self):
        hello.remember_pin(self.PIN)
        with mock.patch.object(hello, "verify", return_value=hello.CANCELED):
            self.assertEqual(self.agent.unlock_hello(0x1), "")
        self.assertEqual(self.tried, [])  # no Windows Hello, no PIN
        with mock.patch.object(hello, "verify", return_value=hello.VERIFIED):
            self.assertEqual(self.agent.unlock_hello(0x1), "")
        self.assertEqual(self.tried, [self.PIN])
        self.assertEqual(self.agent.vm.state, self.RUNNING)

    def test_a_changed_pin_is_forgotten(self):
        hello.remember_pin("1111")
        with mock.patch.object(hello, "verify", return_value=hello.VERIFIED), mock.patch.object(self.agent, "notice"):
            self.assertIn("changed", self.agent.unlock_hello(0x1))
        self.assertFalse(hello.has_pin())
        self.assertEqual(self.agent.vm.state, self.LOCKED)


if __name__ == "__main__":
    unittest.main()
