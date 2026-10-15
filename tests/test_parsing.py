"""What we read from Android and scrcpy, and the scrcpy command line we build."""
import unittest

from androiddesktop.appwindow import parse_display_id, scrcpy_args
from androiddesktop.catalog import parse_list
from androiddesktop.manager import live_displays

STACK_LIST = """RootTask id=32 bounds=[0,0][396,822] displayId=5 userId=0
  taskId=32: com.android.vending/com.google.android.finsky.MainActivity bounds=[0,0][396,822] userId=0 visible=true
RootTask id=1 bounds=[0,0][720,1280] displayId=0 userId=0
  taskId=1: com.google.android.apps.nexuslauncher/.NexusLauncherActivity userId=0 visible=true
"""


class Scrcpy(unittest.TestCase):
    def test_display_id(self):
        self.assertEqual(parse_display_id("[server] INFO: New display: 396x822/160 (id=3)\n"), 3)
        self.assertEqual(parse_display_id("[server] INFO: New display: 1920x1080/240 (id=17)"), 17)
        self.assertIsNone(parse_display_id("INFO: Renderer: direct3d"))

    def test_args(self):
        args = scrcpy_args("com.android.chrome", (412, 830), 240, (10, -20), (32, 32, 32))
        for expected in ("--new-display=412x830/240", "--flex-display", "--start-app=com.android.chrome",
                         "--window-x=10", "--window-y=-20", "--window-title=androiddesktop:com.android.chrome",
                         "--window-borderless", "--keyboard=uhid", "--background-color=202020"):
            self.assertIn(expected, args)
        self.assertEqual(args[1:3], ["-s", "emulator-5584"])

    def test_no_window_size_flags(self):
        # scrcpy refuses --window-width/--window-height together with --flex-display.
        args = scrcpy_args("p", (100, 100), 160, (0, 0), (0, 0, 0))
        self.assertFalse([a for a in args if a.startswith(("--window-width", "--window-height"))])


class TaskList(unittest.TestCase):
    def test_live_displays(self):
        self.assertEqual(live_displays(STACK_LIST), {0, 5})

    def test_no_verdict_without_the_home_screen(self):
        # adb errors and a half-shut-down Android must not read as "every app exited".
        self.assertIsNone(live_displays(""))
        self.assertIsNone(live_displays("error: device offline"))
        self.assertIsNone(live_displays("RootTask id=32 displayId=5"))


class AppList(unittest.TestCase):
    def test_parse(self):
        out = ("com.android.chrome\tcom.google.android.apps.chrome.Main\t1\t1720000000000\tChrome\r\n"
               "org.example.app\torg.example.MainActivity\t0\t\tExample App\n"
               "com.android.stk\tcom.android.stk.StkMain\t1\t0\tSIM Toolkit\n"
               "garbage line\n")
        apps = parse_list(out)
        self.assertEqual(sorted(apps), ["com.android.chrome", "org.example.app"])  # SIM Toolkit is hidden
        chrome = apps["com.android.chrome"]
        self.assertEqual((chrome.label, chrome.system, chrome.updated), ("Chrome", True, 1720000000000))
        self.assertEqual((apps["org.example.app"].system, apps["org.example.app"].updated), (False, 0))


if __name__ == "__main__":
    unittest.main()
