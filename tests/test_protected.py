"""What protected.py reads from Android, and how it types text."""
import unittest

from androiddesktop.protected import (FLAGS, parse_emulator_displays, parse_metrics, parse_root_tasks,
                                      parse_secure_displays, text_commands)

# Trimmed from a real `dumpsys display`: scrcpy's display 3 and the emulator's display in slot 1.
DUMPSYS_DISPLAY = (
    '    mBaseDisplayInfo=DisplayInfo{"scrcpy", displayId 3, displayGroupId 1, FLAG_PRESENTATION, '
    'FLAG_TRUSTED, real 396 x 822, hdrCapabilities HdrCapabilities{mSupportedHdrTypes=[]}, '
    'uniqueId "virtual:com.android.shell,2000,scrcpy,2", app 396 x 822}\n'
    '    mBaseDisplayInfo=DisplayInfo{"Emulator 2D Display", displayId 8, displayGroupId 2, FLAG_SECURE, '
    'FLAG_TRUSTED, real 396 x 822, hdrCapabilities null, '
    'uniqueId "virtual:com.android.emulator.multidisplay:1234562", app 396 x 822}\n'
    '    mOverrideDisplayInfo=DisplayInfo{"Emulator 2D Display", displayId 8, displayGroupId 2, '
    'uniqueId "virtual:com.android.emulator.multidisplay:1234562"}\n')

STACK_LIST = """RootTask id=47 bounds=[0,0][396,822] displayId=8 userId=0
  taskId=47: com.android.settings/com.android.settings.password.SetNewPasswordActivity bounds=[0,0][396,822] userId=0 visible=true
RootTask id=46 bounds=[0,0][396,822] displayId=8 userId=0
  taskId=46: com.android.settings/com.android.settings.Settings bounds=[0,0][396,822] userId=0 visible=false
RootTask id=48 bounds=[0,0][396,822] displayId=8 userId=0
  taskId=49: com.google.android.apps.nexuslauncher/com.android.launcher3.secondarydisplay.SecondaryDisplayLauncher userId=0
RootTask id=1 bounds=[0,0][720,1280] displayId=0 userId=0
"""


class Reading(unittest.TestCase):
    def test_secure_displays(self):
        self.assertEqual(parse_secure_displays("mDisplayId=5\nmDisplayId=12\n"), {5, 12})
        self.assertEqual(parse_secure_displays(""), set())

    def test_root_tasks_top_first(self):
        self.assertEqual(parse_root_tasks(STACK_LIST, 8), [47, 46, 48])
        self.assertEqual(parse_root_tasks(STACK_LIST, 0), [1])
        self.assertEqual(parse_root_tasks(STACK_LIST, 3), [])

    def test_metrics(self):
        self.assertEqual(parse_metrics("Physical size: 396x822\nPhysical density: 160\n"), (396, 822, 160))
        # An override is what apps see.
        self.assertEqual(parse_metrics("Physical size: 396x822\nOverride size: 400x800\n"
                                       "Physical density: 160\nOverride density: 240\n"), (400, 800, 240))

    def test_emulator_displays(self):
        self.assertEqual(parse_emulator_displays(DUMPSYS_DISPLAY), {1: 8})
        self.assertEqual(parse_emulator_displays('DisplayInfo{"scrcpy", displayId 3, uniqueId "x"}'), {})

    def test_flags(self):
        self.assertTrue(FLAGS & (1 << 2), "SECURE")
        self.assertTrue(FLAGS & (1 << 9), "system decorations: a display with no task sleeps")
        self.assertTrue(FLAGS & (1 << 10) and FLAGS & (1 << 14), "TRUSTED and OWN_FOCUS")


class Typing(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(text_commands("1234"), [("text", "1234")])

    def test_spaces_and_percent_on_their_own(self):
        # `input text` would turn "%s" into a space.
        self.assertEqual(text_commands("50%s off"),
                         [("text", "50"), ("text", "%"), ("text", "s"), ("keyevent", "62"), ("text", "off")])

    def test_quoted_for_the_shell(self):
        self.assertEqual(text_commands("it's"), [("text", "'it'\"'\"'s'")])

    def test_ascii_only(self):
        self.assertEqual(text_commands("é1\t2"), [("text", "12")])


if __name__ == "__main__":
    unittest.main()
