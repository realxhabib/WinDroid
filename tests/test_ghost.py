"""Ghost sessions (ghost.py) against a pretend Android: what they ask of adb and AgentHelper."""
import json
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import ghost
from androiddesktop.agenthelper import HelperError
from androiddesktop.catalog import App
from androiddesktop.ghost import (GhostError, Session, Sessions, parse_agent_displays, parse_tasks, swipe_for,
                                  visible_center)

CLOCK_TIMER = json.loads((Path(__file__).with_name("clock_timer_tree.json")).read_text(encoding="utf-8"))

STACK_LIST = """RootTask id=9 bounds=[0,0][720,1520] displayId=6 userId=0
 configuration={1.0 310mcc260mnc [en_US] ldltr sw360dp w360dp h760dp 320dpi}
  taskId=9: com.google.android.deskclock/com.android.deskclock.DeskClock bounds=[0,0][720,1520] userId=0 visible=true
RootTask id=12 bounds=[0,0][396,822] displayId=3 userId=0
  taskId=12: com.android.settings/com.android.settings.Settings bounds=[0,0][396,822] userId=0 visible=true
RootTask id=1 bounds=[0,0][720,1280] displayId=0 userId=0
  taskId=6: com.google.android.apps.nexuslauncher/com.google.android.apps.nexuslauncher.NexusLauncherActivity
RootTask id=2 bounds=[0,0][720,1280] displayId=0 userId=0
  taskId=3: unknown bounds=[0,0][720,1280] userId=0 visible=false
"""

DUMPSYS_DISPLAY = (
    '  mBaseDisplayInfo=DisplayInfo{"androiddesktop-agent:com.google.android.deskclock", displayId 6, '
    'displayGroupId 4, FLAG_TRUSTED, real 720 x 1520, uniqueId "virtual:com.android.shell,2000,x,0"}\n'
    '  mBaseDisplayInfo=DisplayInfo{"scrcpy", displayId 3, displayGroupId 1, FLAG_TRUSTED, real 396 x 822}\n')

APPS = {"com.google.android.deskclock": App("com.google.android.deskclock", "com.android.deskclock.DeskClock",
                                            "Clock", True, 0),
        "com.android.settings": App("com.android.settings", "com.android.settings.Settings", "Settings", True, 0)}


class Parsing(unittest.TestCase):
    def test_tasks(self):
        tasks = parse_tasks(STACK_LIST)
        self.assertEqual([(t.root, t.display) for t in tasks], [(9, 6), (12, 3), (1, 0), (2, 0)])
        self.assertEqual(tasks[0].packages, {"com.google.android.deskclock"})
        self.assertEqual(tasks[3].packages, set())  # "unknown"

    def test_agent_displays(self):
        self.assertEqual(parse_agent_displays(DUMPSYS_DISPLAY), {6: "com.google.android.deskclock"})
        self.assertEqual(parse_agent_displays(""), {})

    def test_visible_center(self):
        self.assertEqual(visible_center((100, 100, 300, 200), (720, 1520)), (200, 150))
        self.assertEqual(visible_center((600, 1400, 900, 1700), (720, 1520)), (660, 1460))  # half off screen
        with self.assertRaises(GhostError):
            visible_center((0, 1600, 720, 1700), (720, 1520))

    def test_swipes_show_what_lies_that_way(self):
        x1, y1, x2, y2 = swipe_for("down", (0, 0, 720, 1000))
        self.assertEqual((x1, x2), (360, 360))
        self.assertGreater(y1, y2)  # the finger moves up
        self.assertEqual(swipe_for("up", (0, 0, 720, 1000)), (360, 200, 360, 800))
        x1, _, x2, _ = swipe_for("right", (0, 0, 1000, 100))
        self.assertGreater(x1, x2)


class FakeHelper:
    def __init__(self):
        self.calls = []
        self.on_restart = lambda: None
        self.acts = {"setText": {"done": True}}
        self.alive = True

    def running(self):
        return self.alive

    def call(self, cmd, timeout=20, **args):
        self.calls.append((cmd, args))
        if cmd == "create":
            return {"display": 6}
        if cmd == "tree":
            return CLOCK_TIMER
        if cmd == "shot":
            return {"format": "jpeg", "data": "AAAA"}
        if cmd == "mark":
            return {"events": 41}
        if cmd == "act":
            result = self.acts.get(args["action"], {"done": False})
            if isinstance(result, Exception):
                raise result
            return result
        return {}

    def close(self):
        pass


class FakeAndroid:
    """adb.shell, answering the commands ghost.py runs."""

    def __init__(self, secure=""):
        self.secure = secure
        self.commands = []
        self.stack = STACK_LIST
        self.dumpsys_display = ""

    def shell(self, command, timeout=30):
        self.commands.append(command)
        if command.startswith("dumpsys window windows"):
            return self.secure
        if command == "cmd activity stack list":
            return self.stack
        if command.startswith("am start"):
            return "Starting: Intent\nStatus: ok\n"
        if command == "dumpsys display":
            return self.dumpsys_display
        return ""


class Operating(unittest.TestCase):
    def setUp(self):
        self.android = FakeAndroid()
        self.helper = FakeHelper()
        patches = [mock.patch.object(ghost.adb, "shell", self.android.shell),
                   mock.patch.object(ghost.adb, "online", return_value=True),
                   mock.patch.object(ghost.adb, "booted", return_value=True),
                   mock.patch.object(ghost.adb, "user_locked", return_value=False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sessions = Sessions(self.helper)
        self.sessions._apps = dict(APPS)

    def hidden_clock(self):
        s = Session("clock-1", "com.google.android.deskclock", "Clock", 6, 6)
        self.sessions.sessions[s.id] = s
        return s

    def inputs(self):
        return [c for c in self.android.commands if c.startswith("input ")]

    def test_hidden_session_refuses_an_app_in_the_users_window(self):
        with self.assertRaisesRegex(GhostError, "open in the user's window.*visible=true"):
            self.sessions.start("Settings")
        self.assertNotIn("create", [c for c, _ in self.helper.calls])

    def test_visible_session_joins_the_users_window(self):
        with mock.patch.object(Sessions, "_display_size", return_value=(396, 822)), \
                mock.patch.object(ghost, "ask_agent") as ask:
            s = self.sessions.start("Settings", visible=True)
        self.assertEqual((s.display, s.hidden, s.size), (3, False, (396, 822)))
        ask.assert_not_called()  # nothing to open: it's there
        self.assertFalse(any(c.startswith("am start") for c in self.android.commands))

    def test_another_agents_app_is_left_alone(self):
        self.android.dumpsys_display = DUMPSYS_DISPLAY.replace("displayId 6", "displayId 3")
        with self.assertRaisesRegex(GhostError, "Another agent"):
            self.sessions.start("Settings", visible=True)

    def test_starts_hidden_on_a_display_of_its_own(self):
        self.android.stack = STACK_LIST.replace("displayId=3", "displayId=0")  # Settings only in the background
        s = self.sessions.start("settings")  # by name, any case
        self.assertEqual((s.package, s.display, s.ghost, s.hidden), ("com.android.settings", 6, 6, True))
        create = dict(self.helper.calls)["create"]
        self.assertEqual((create["width"], create["height"]), ghost.SIZE)
        self.assertTrue(create["name"].startswith(ghost.DISPLAY_PREFIX))
        self.assertIn("am start -W --display 6 -n com.android.settings/com.android.settings.Settings",
                      self.android.commands)

    def test_unknown_app(self):
        with self.assertRaisesRegex(GhostError, "list_apps"):
            self.sessions.start("Calculator")

    def test_read_gives_the_compact_tree(self):
        s = self.hidden_clock()
        screen, shot = self.sessions.read(s.id, screenshot=True)
        self.assertTrue(screen["hidden"])
        self.assertEqual(screen["windows"][0]["package"], "com.google.android.deskclock")
        self.assertEqual(shot["data"], "AAAA")

    def test_secure_screen_is_reported_not_shown(self):
        s = self.hidden_clock()
        self.android.secure = "mDisplayId=6\n"
        screen, shot = self.sessions.read(s.id, screenshot=True)
        self.assertTrue(screen["secure"])
        self.assertIn("secure screen: needs the user", screen["message"])
        self.assertNotIn("windows", screen)
        self.assertIsNone(shot)
        self.assertNotIn("shot", [c for c, _ in self.helper.calls])
        for action in (lambda: self.sessions.tap(s.id, x=10, y=10),
                       lambda: self.sessions.type_text(s.id, "1234"),
                       lambda: self.sessions.press(s.id, "enter"),
                       lambda: self.sessions.scroll(s.id, "down")):
            with self.assertRaisesRegex(GhostError, "secure screen"):
                action()
        self.assertEqual(self.inputs(), [])

    def test_another_displays_secure_screen_is_not_ours(self):
        s = self.hidden_clock()
        self.android.secure = "mDisplayId=3\n"
        self.assertNotIn("secure", self.sessions.read(s.id)[0])

    def test_tap_an_element(self):
        s = self.hidden_clock()
        self.sessions.read(s.id)
        message = self.sessions.tap(s.id, 21)  # the "2" key
        self.assertEqual(self.inputs(), ["input -d 6 tap 360 463"])
        self.assertIn('Button "2"', message)
        # then it waits for display 6 to react to the tap (an event after the 41st), and to be still
        settle = [a for c, a in self.helper.calls if c == "settle"][-1]
        self.assertEqual((settle["display"], settle["since"]), (6, 41))
        with self.assertRaisesRegex(GhostError, "read_screen"):
            self.sessions.tap(s.id, 999)

    def test_type_into_a_named_field(self):
        s = self.hidden_clock()
        self.sessions.read(s.id)
        self.sessions.type_text(s.id, "héllo wörld", element=17)
        act = [a for c, a in self.helper.calls if c == "act"][-1]
        self.assertEqual((act["action"], act["text"], act["index"], act["bounds"]),
                         ("setText", "héllo wörld", 17, [86, 128, 635, 372]))

    def test_type_without_a_focused_field_uses_keys(self):
        s = self.hidden_clock()
        self.helper.acts["setText"] = HelperError("no text field has focus")
        self.sessions.type_text(s.id, "10 min")
        self.assertEqual(self.inputs(), ["input -d 6 text 10", "input -d 6 keyevent 62", "input -d 6 text min"])
        with self.assertRaisesRegex(GhostError, "No text field has focus"):
            self.sessions.type_text(s.id, "naïve")  # `input text` can't type that

    def test_enter_prefers_the_keyboard_action(self):
        s = self.hidden_clock()
        self.helper.acts["imeEnter"] = {"done": True}
        self.sessions.press(s.id, "enter")
        self.assertEqual(self.inputs(), [])
        self.helper.acts["imeEnter"] = {"done": False}
        self.sessions.press(s.id, "enter")
        self.assertEqual(self.inputs(), ["input -d 6 keyevent 66"])

    def test_end_closes_the_app_and_its_display(self):
        s = self.hidden_clock()
        self.sessions.end(s.id)
        self.assertIn("am stack remove 9", self.android.commands)
        self.assertIn(("release", {"display": 6}), self.helper.calls)
        with self.assertRaisesRegex(GhostError, "No session"):
            self.sessions.read(s.id)

    def test_visible_session_ends_without_closing_the_window(self):
        s = Session("settings-1", "com.android.settings", "Settings", 3, None)
        self.sessions.sessions[s.id] = s
        self.assertIn("stays open", self.sessions.end(s.id))
        self.assertFalse(any(c.startswith("am stack remove") for c in self.android.commands))

    def test_visible_session_follows_its_app(self):
        s = Session("settings-1", "com.android.settings", "Settings", 3, None, size=(396, 822))
        self.sessions.sessions[s.id] = s
        self.sessions.read(s.id)
        self.assertEqual(s.display, 3)
        self.android.stack = STACK_LIST.replace("displayId=3", "displayId=11")  # protected.py moved it
        with mock.patch.object(Sessions, "_display_size", return_value=(396, 822)):
            self.sessions.read(s.id)
        self.assertEqual(s.display, 11)
        self.assertEqual(dict(self.helper.calls[-1:])["tree"], {"display": 11})
        self.android.stack = STACK_LIST.replace("displayId=3", "displayId=0")  # the user closed the window
        with self.assertRaisesRegex(GhostError, "has ended.*closed its window"):
            self.sessions.read(s.id)
        self.assertNotIn(s.id, self.sessions.sessions)

    def test_helper_restart_ends_hidden_sessions(self):
        s = self.hidden_clock()
        self.helper.alive = False  # it died: the next call starts a new one, without the old displays

        def restart(cmd, timeout=20, **args):
            self.helper.alive = True
            self.helper.on_restart()
            return {}
        with mock.patch.object(self.helper, "call", restart):
            with self.assertRaisesRegex(GhostError, "has ended: Android's helper restarted"):
                self.sessions.read(s.id)

    def test_screenshot_trouble_still_reads_the_tree(self):
        s = self.hidden_clock()
        call = self.helper.call

        def no_frames(cmd, timeout=20, **args):
            if cmd == "shot":
                raise HelperError("display 6 hasn't drawn anything yet")
            return call(cmd, timeout, **args)
        with mock.patch.object(self.helper, "call", no_frames):
            screen, shot = self.sessions.read(s.id, screenshot=True)
        self.assertIsNone(shot)
        self.assertIn("hasn't drawn", screen["screenshot"])
        self.assertTrue(screen["windows"])

    def test_not_set_up(self):
        with mock.patch.object(ghost.adb, "online", return_value=False), \
                mock.patch.object(ghost.runtime, "installed", return_value=False), \
                mock.patch.object(ghost, "ask_agent") as ask:
            with self.assertRaisesRegex(GhostError, "isn't set up"):
                self.sessions.start("Clock")
        ask.assert_not_called()

    def test_show_hands_the_app_to_a_window(self):
        s = self.hidden_clock()
        moved = STACK_LIST.replace("displayId=6", "displayId=8")  # the window's display took the task
        with mock.patch.object(ghost, "ask_agent") as ask, \
                mock.patch.object(Sessions, "_display_size", return_value=(396, 822)):
            self.android.stack = moved
            message = self.sessions.show(s.id)
        ask.assert_called_once_with({"open": "com.google.android.deskclock", "adopt": 6},
                                    ["open", "com.google.android.deskclock", "--adopt", "6"])
        self.assertEqual((s.display, s.ghost, s.size), (8, None, (396, 822)))
        self.assertIn(("release", {"display": 6}), self.helper.calls)
        self.assertIn("window", message)

    def test_locked_android(self):
        with mock.patch.object(ghost.adb, "user_locked", return_value=True):
            with self.assertRaisesRegex(GhostError, "locked"):
                self.sessions.start("Clock")


if __name__ == "__main__":
    unittest.main()
