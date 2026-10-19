"""The launcher's settings page, command palette and What's new (features.py), the Api methods behind
them (app.py), and the app window's "..." menu. No Android, no Windows prompts, no real config files."""
import json
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import __version__, appwindow, features, state
from androiddesktop.app import Agent, Api
from androiddesktop.catalog import App
from androiddesktop.checks import Check
from androiddesktop.vm import LOCKED, OFF, RUNNING

PIN = "4821"


def temp_state(test: unittest.TestCase) -> Path:
    """state.json in a folder of the test's own, never the user's."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name)
    for p in (mock.patch.object(state, "DATA", path), mock.patch.object(state, "STATE", path / "state.json"),
              mock.patch.object(state, "_data", None)):
        p.start()
        test.addCleanup(p.stop)
    return path


def done(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Window:
    def __init__(self, package, surface=None, child=1, closing=False):
        self.app = types.SimpleNamespace(package=package)
        self.surface, self.child, self.closing = surface, child, closing


class FakeAgent:
    """What Features and Api use of the agent."""

    def __init__(self, vm_state=RUNNING):
        self.vm = types.SimpleNamespace(state=vm_state, detail="")
        self.manager = types.SimpleNamespace(windows={}, call=lambda fn: fn())
        self.fingerprint = types.SimpleNamespace(available=True, enrolled=1, enrolling=False)
        self.setup_state = {"installed": True}
        self.calls: list = []
        self.checks_found = [Check("hypervisor", "ok", "Virtualization is ready"),
                             Check("disk", "warning", "Drive C: is almost full", "Only 1 GB is free.", "open_storage", "Free up space")]

    def forget_pin(self):
        self.calls.append("forget_pin")

    def unlock(self, pin, remember):
        self.calls.append(("unlock", pin, remember))
        return ""

    def push(self, apps=False):
        self.calls.append("push")

    def recheck(self):
        return self.checks_found

    def restart(self):
        self.calls.append("restart")

    def open(self, package, intent=None, adopt=None):
        self.calls.append(("open", package, intent))

    def quit(self):
        self.calls.append("quit")


# ------------------------------------------------------------------ what each card shows
class Cards(unittest.TestCase):
    def test_memory_from_config(self):
        self.assertEqual(features.gigabytes("8192M"), 8)
        self.assertEqual(features.gigabytes("6144"), 6)
        self.assertEqual(features.gigabytes("4G"), 4)
        self.assertIsNone(features.gigabytes(""))
        self.assertIsNone(features.gigabytes("lots"))

    def test_android(self):
        card = features.android_card(RUNNING, "", {"hw.ramSize": "8192M", "hw.cpu.ncore": "6"}, 2)
        self.assertEqual((card["memory"], card["cores"], card["windows"], card["version"]), (8, 6, 2, "Android 16"))
        self.assertTrue(card["play"])
        self.assertIsNone(features.android_card(OFF, "", {}, 0)["cores"])

    def test_screen_lock(self):
        dumpsys = "User State:\n  User 0\n    Quality: 131072\n    CredentialType: Pin\n    SeparateChallenge: false\n"
        self.assertEqual(features.parse_credential_type(dumpsys), "pin")
        self.assertEqual(features.parse_credential_type("CredentialType: None"), "none")
        self.assertEqual(features.parse_credential_type("CredentialType: PASSWORD"), "password")
        self.assertEqual(features.parse_credential_type("CredentialType: Pattern"), "pattern")
        self.assertIsNone(features.parse_credential_type("Can't find service: lock_settings"))
        self.assertIsNone(features.parse_credential_type("CredentialType: Unknown 7"))

    def test_security_says_locked_while_android_is(self):
        card = features.security_card(True, True, 1, False, LOCKED, None)
        self.assertEqual(card["lock"], "locked")
        self.assertFalse(card["running"])
        card = features.security_card(False, False, None, False, RUNNING, "none")
        self.assertEqual((card["hello"], card["unlock"], card["lock"], card["running"]), (False, False, "none", True))

    def test_camera(self):
        webcams = [("MX Brio", "webcam0"), ("Integrated Webcam", "webcam1")]
        card = features.camera_card(webcams, {"hw.camera.front": "webcam1"}, None)
        self.assertEqual(card, {"webcams": ["MX Brio", "Integrated Webcam"], "android": "Integrated Webcam", "virtual": None})
        self.assertEqual(features.camera_card([], {"hw.camera.front": "none"}, None)["android"], "No camera")
        self.assertIsNone(features.camera_card(None, {}, None)["webcams"])  # still looking

    def test_virtual_camera_when_this_build_has_it(self):
        camera = types.SimpleNamespace(installed_dll=lambda: Path("x.dll"), up_to_date=lambda: False)
        self.assertEqual(features.camera_card([], {}, camera)["virtual"], {"installed": True, "current": False})
        camera = types.SimpleNamespace(installed_dll=lambda: None, up_to_date=lambda: False)
        self.assertEqual(features.camera_card([], {}, camera)["virtual"], {"installed": False, "current": False})

        def broken():
            raise OSError("registry")
        camera = types.SimpleNamespace(installed_dll=broken, up_to_date=broken)
        with mock.patch.object(features, "log"):
            self.assertEqual(features.camera_card([], {}, camera)["virtual"], {"installed": False, "current": False})

    def test_performance(self):
        wins = [Window("a", surface=object()), Window("b"), Window("c", child=0)]
        card = features.performance_card("auto", "balanced", RUNNING, True, "", wins)
        self.assertEqual((card["mode"], card["setting"], card["quality"]), ("emulator", "auto", "balanced"))
        self.assertEqual(card["windows"], {"emulator": 1, "scrcpy": 1})
        self.assertEqual(features.performance_card("scrcpy", "high", RUNNING, True, "", [])["mode"], "scrcpy")
        older = features.performance_card("auto", "high", RUNNING, False, "", [])
        self.assertEqual(older["mode"], "scrcpy")
        self.assertIn("older version", older["why"])
        off = features.performance_card("auto", "high", RUNNING, True, "frames are (720, 1280)", [])
        self.assertIn("frames are", off["why"])
        self.assertEqual(features.performance_card("auto", "high", OFF, False, "", [])["mode"], "emulator")
        self.assertEqual(features.performance_card("weird", "weird", OFF, False, "", [])["quality"], "high")

    def test_updates(self):
        self.assertEqual(features.updates_card(None), {"version": __version__, "available": False, "managed": False, "offer": None})
        offer = {"state": "available", "version": "9.0.0"}
        updater = types.SimpleNamespace(enabled=True, snapshot=lambda: offer)
        self.assertEqual(features.updates_card(updater)["offer"], offer)
        self.assertTrue(features.updates_card(updater)["managed"])

    def test_whats_new_after_first_run_and_updates(self):
        self.assertEqual(features.whats_new(None, "1.2.0"), {"version": "1.2.0", "first": True})
        self.assertEqual(features.whats_new("1.1.0", "1.2.0"), {"version": "1.2.0", "first": False})
        self.assertIsNone(features.whats_new("1.2.0", "1.2.0"))


class Agents(unittest.TestCase):
    def test_mcp_command(self):
        with mock.patch.object(features.sys, "executable", r"C:\venv\Scripts\pythonw.exe"), \
                mock.patch.object(features.Path, "exists", lambda p: True):
            self.assertEqual(features.mcp_command(), [r"C:\venv\Scripts\python.exe", "-m", "androiddesktop", "mcp"])
        with mock.patch.object(features.sys, "executable", r"C:\Program Files\WinDroid\WinDroid.exe"), \
                mock.patch.object(features.sys, "frozen", True, create=True):
            self.assertEqual(features.mcp_command(), [r"C:\Program Files\WinDroid\windroid-cli.exe", "mcp"])

    def test_registered(self):
        command = [r"C:\venv\Scripts\python.exe", "-m", "androiddesktop", "mcp"]
        entry = features.server_entry(command)
        config = {"mcpServers": {"androiddesktop": entry, "other": {}}}
        self.assertEqual(features.registered(config), entry)
        self.assertTrue(features.same_command(entry, command))
        self.assertFalse(features.same_command({"command": r"C:\elsewhere\python.exe", "args": command[1:]}, command))
        self.assertFalse(features.same_command(None, command))
        self.assertIsNone(features.registered({"mcpServers": {"other": {}}}))
        self.assertIsNone(features.registered({"mcpServers": []}))
        self.assertIsNone(features.registered({}))

    def test_desktop_snippet(self):
        command = [r"C:\venv\Scripts\python.exe", "-m", "androiddesktop", "mcp"]
        parsed = json.loads(features.desktop_snippet(command))
        self.assertEqual(parsed, {"mcpServers": {"androiddesktop": {"command": command[0], "args": command[1:]}}})

    def test_activity(self):
        log = ("2026-10-16 16:51:15 MCP server started (Android Desktop 0.1.0)\n"
               "2026-10-16 16:51:16 press crashed:\n"
               "Traceback (most recent call last):\n"
               "RuntimeError: adb went away\n"
               "2026-10-16 16:51:17 tap session='clock-1' element_id=4 -> tapped (95 ms)\n")
        entries = features.parse_activity(log)
        self.assertEqual([e["text"] for e in entries],
                         ["tap session='clock-1' element_id=4 -> tapped (95 ms)", "press crashed:",
                          "MCP server started (Android Desktop 0.1.0)"])
        self.assertEqual((entries[0]["date"], entries[0]["time"]), ("2026-10-16", "16:51:17"))
        self.assertEqual(len(features.parse_activity(log, limit=1)), 1)
        self.assertEqual(features.parse_activity(""), [])

    def test_tail_reads_the_end(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "agent.log"
            path.write_bytes(b"x" * 100 + b"\nlast line\n")
            self.assertTrue(features.tail(path, 20).endswith("last line\n"))
            self.assertEqual(len(features.tail(path, 20)), 20)
            self.assertEqual(features.tail(Path(d) / "missing.log"), "")


class Plugin:
    def __init__(self, name, title, packages):
        self.name, self.title, self.packages = name, title, frozenset(packages)


class FakeHost:
    """plugins.PluginHost, as far as features.py uses it."""

    def __init__(self, classes):
        self.classes = classes
        self.events: list = []

    def _load(self):
        return self.classes

    @staticmethod
    def enabled(cls):
        return bool((state.get("plugins") or {}).get(cls.name, True))

    def closed(self, win):
        self.events.append(("closed", win.app.package))

    def shown(self, win):
        self.events.append(("shown", win.app.package))


class Plugins(unittest.TestCase):
    def setUp(self):
        temp_state(self)
        self.host = FakeHost([Plugin("hinge", "Hinge Matcher", ["co.hinge.app"])])
        quiet = mock.patch.object(features, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_none_without_plugins_in_this_build(self):
        self.assertIsNone(features.plugins_card(None))
        self.assertEqual(features.plugins_for(None, "co.hinge.app"), [])

    def test_list_and_toggle(self):
        self.assertEqual(features.plugins_card(self.host),
                         [{"name": "hinge", "title": "Hinge Matcher", "packages": ["co.hinge.app"], "on": True}])
        wins = [Window("co.hinge.app"), Window("com.android.chrome"), Window("co.hinge.app", closing=True)]
        self.assertTrue(features.set_plugin(self.host, wins, "hinge", False, lambda fn: fn()))
        self.assertEqual(state.get("plugins"), {"hinge": False})
        self.assertFalse(features.plugins_card(self.host)[0]["on"])
        # Only the Hinge window that's open restarts its plugins (now without Hinge Matcher).
        self.assertEqual(self.host.events, [("closed", "co.hinge.app"), ("shown", "co.hinge.app")])
        self.assertEqual(features.plugins_for(self.host, "co.hinge.app")[0]["on"], False)
        self.assertEqual(features.plugins_for(self.host, "com.android.chrome"), [])

    def test_unknown_plugin(self):
        self.assertFalse(features.set_plugin(self.host, [], "nope", True, lambda fn: fn()))
        self.assertIsNone(state.get("plugins"))


class Phones(unittest.TestCase):
    def test_from_the_hub(self):
        self.assertIsNone(features.phones_card(None))
        hub = types.SimpleNamespace(snapshot=lambda: {
            "enabled": True, "problem": "", "approvals": [{"name": "iPad"}],
            "devices": [{"group": "g1", "name": "Habib's iPhone", "online": True, "apps": ["Clock"]},
                        {"group": "g2", "name": "", "online": False, "apps": []}]})
        card = features.phones_card(hub)
        self.assertEqual(card["asking"], 1)
        self.assertEqual(card["devices"][0], {"name": "Habib's iPhone", "online": True, "apps": ["Clock"]})
        self.assertEqual(card["devices"][1]["name"], "iPhone or iPad")


# ------------------------------------------------------------------ the whole page and its actions
class Snapshot(unittest.TestCase):
    def setUp(self):
        temp_state(self)
        self.agent = FakeAgent()
        self.f = features.Features(self.agent, run=mock.Mock(return_value=done()))
        self.logs = tempfile.TemporaryDirectory()
        self.addCleanup(self.logs.cleanup)
        patches = [
            mock.patch.object(features.runtime, "vm_config", lambda: {"hw.ramSize": "8192M", "hw.cpu.ncore": "6",
                                                                      "hw.camera.front": "webcam0"}),
            mock.patch.object(features.runtime, "installed", lambda: True),
            mock.patch.object(features.runtime, "webcams", lambda: [("MX Brio", "webcam0")]),
            mock.patch.object(features.hello, "has_pin", lambda: True),
            mock.patch.object(features.adb, "shell", self.shell),
            mock.patch.object(features.ghost, "agent_displays", lambda: {7: "androiddesktop-agent:clock"}),
            mock.patch.object(features.emuview, "endpoint", lambda: (8584, "token")),
            mock.patch.object(features, "optional", lambda name: None),
            mock.patch.object(features, "claude_code_config", lambda: Path(self.logs.name) / "claude.json"),
            mock.patch.object(features, "claude_desktop_config", lambda: Path(self.logs.name) / "Claude" / "config.json"),
            mock.patch.object(features, "find_claude", lambda: r"C:\bin\claude.exe"),
            mock.patch.object(features, "LOGS", Path(self.logs.name)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.shell_out = {"dumpsys lock_settings": "CredentialType: Pin"}

    def shell(self, command, timeout=30):
        return self.shell_out.get(command, "")

    def test_every_card(self):
        snap = self.f.snapshot()
        self.assertEqual(set(snap), {"installed", "android", "security", "camera", "agents", "plugins", "phones",
                                     "performance", "updates", "whatsNew"})
        self.assertEqual((snap["android"]["memory"], snap["android"]["cores"]), (8, 6))
        self.assertEqual(snap["security"], {"hello": True, "unlock": True, "fingerprints": 1, "enrolling": False,
                                            "lock": "pin", "running": True})
        self.assertEqual(snap["agents"]["hidden"], 1)
        self.assertFalse(snap["agents"]["claudeCode"]["connected"])
        self.assertIsNone(snap["plugins"])
        self.assertIsNone(snap["phones"])
        self.assertEqual(snap["performance"]["mode"], "emulator")
        self.assertEqual(snap["whatsNew"], {"version": __version__, "first": True})

    def test_webcams_are_looked_up_in_the_background(self):
        self.assertIsNone(self.f.snapshot()["camera"]["webcams"])  # looking
        for t in threading.enumerate():
            if t.name == "webcams":
                t.join(5)
        self.assertEqual(self.f.snapshot()["camera"]["webcams"], ["MX Brio"])

    def test_a_broken_part_doesnt_break_the_page(self):
        with mock.patch.object(features, "performance_card", side_effect=RuntimeError("boom")), \
                mock.patch.object(features, "log") as log:
            snap = self.f.snapshot()
            self.f.snapshot()
        self.assertEqual(snap["performance"], {"error": "Couldn't read this right now."})
        self.assertEqual(snap["android"]["memory"], 8)
        self.assertEqual(len([c for c in log.call_args_list if "performance" in c.args[0]]), 1)  # logged once

    def test_claude_code_connected(self):
        command = features.mcp_command()
        (Path(self.logs.name) / "claude.json").write_text(
            json.dumps({"mcpServers": {"androiddesktop": features.server_entry(command)}}), encoding="utf-8")
        code = self.f.snapshot()["agents"]["claudeCode"]
        self.assertEqual(code, {"installed": True, "connected": True, "current": True})

    def test_activity_from_agent_log(self):
        (Path(self.logs.name) / "agent.log").write_text("2026-10-17 09:00:01 start_session package='Clock' -> clock-1\n",
                                                        encoding="utf-8")
        self.assertEqual(self.f.snapshot()["agents"]["activity"][0]["text"], "start_session package='Clock' -> clock-1")

    def test_screen_lock_and_hidden_apps_only_while_android_runs(self):
        self.agent.vm.state = OFF
        snap = self.f.snapshot()
        self.assertIsNone(snap["security"]["lock"])
        self.assertEqual(snap["agents"]["hidden"], 0)


class HelloUnlock(unittest.TestCase):
    def setUp(self):
        self.agent = FakeAgent()
        self.f = features.Features(self.agent)
        self.logged: list[str] = []
        self.remembered: list[str] = []
        self.answer = "Lock credential verified successfully"
        for p in (mock.patch.object(features, "log", self.logged.append),
                  mock.patch.object(features.hello, "remember_pin", self.remembered.append),
                  mock.patch.object(features.adb, "shell", lambda command, timeout=30: self.answer)):
            p.start()
            self.addCleanup(p.stop)

    def assert_pin_never_logged(self):
        self.assertFalse([line for line in self.logged if PIN in line])

    def test_off_forgets_the_pin(self):
        self.assertEqual(self.f.hello_unlock(False), "")
        self.assertEqual(self.agent.calls, ["forget_pin"])

    def test_on_checks_the_pin_with_android_first(self):
        self.assertEqual(self.f.hello_unlock(True, PIN), "")
        self.assertEqual(self.remembered, [PIN])
        self.assert_pin_never_logged()

    def test_wrong_pin(self):
        self.answer = f"Old password '{PIN}' didn't match"
        self.assertIn("not Android's PIN", self.f.hello_unlock(True, PIN))
        self.assertEqual(self.remembered, [])
        self.assert_pin_never_logged()

    def test_too_many_tries(self):
        self.answer = "Request throttled"
        self.assertIn("Wait 30 seconds", self.f.hello_unlock(True, PIN))
        self.assertEqual(self.remembered, [])

    def test_no_screen_lock(self):
        self.answer = "Old password provided but user has no password"
        self.assertIn("no screen lock", self.f.hello_unlock(True, PIN))
        self.assertEqual(self.remembered, [])

    def test_locked_android_unlocks_and_remembers(self):
        self.agent.vm.state = LOCKED
        self.assertEqual(self.f.hello_unlock(True, PIN), "")
        self.assertEqual(self.agent.calls, [("unlock", PIN, True)])

    def test_needs_windows_hello_android_and_a_pin(self):
        self.assertIn("Enter the PIN", self.f.hello_unlock(True, ""))
        self.agent.vm.state = OFF
        self.assertIn("Start Android", self.f.hello_unlock(True, PIN))
        self.agent.fingerprint.available = False
        self.assertIn("isn't set up", self.f.hello_unlock(True, PIN))
        self.assertEqual(self.remembered, [])


class Actions(unittest.TestCase):
    def setUp(self):
        temp_state(self)
        self.agent = FakeAgent()
        self.run = mock.Mock(return_value=done())
        self.f = features.Features(self.agent, run=self.run)
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        for p in (mock.patch.object(features, "claude_code_config", lambda: Path(self.config.name) / "claude.json"),
                  mock.patch.object(features, "log")):
            p.start()
            self.addCleanup(p.stop)

    def test_run_checks_lists_every_check(self):
        found = self.f.run_checks()
        self.assertEqual([c["level"] for c in found], ["ok", "warning"])
        self.assertEqual(found[1]["fix"], "open_storage")

    def test_virtual_camera(self):
        with mock.patch.object(features, "optional", lambda name: None):
            self.assertIn("isn't part", self.f.camera("install"))
        camera = types.SimpleNamespace(main=mock.Mock(return_value=0))
        with mock.patch.object(features, "optional", lambda name: camera):
            self.assertIn("Installed", self.f.camera("install"))
            camera.main.assert_called_with(["install"])
            camera.main.return_value = 2
            self.assertIn("Canceled", self.f.camera("uninstall"))
            camera.main.return_value = 1
            self.assertIn("didn't work", self.f.camera("install"))
            self.assertIn("isn't part", self.f.camera("test on"))  # only install and uninstall

    def test_connect_claude(self):
        with mock.patch.object(features, "find_claude", lambda: None):
            self.assertIn("isn't installed", self.f.connect_claude())
        self.run.assert_not_called()
        with mock.patch.object(features, "find_claude", lambda: "claude"):
            self.assertEqual(self.f.connect_claude(), "")
        args = self.run.call_args.args[0]
        self.assertEqual(args[:7], ["claude", "mcp", "add", "--scope", "user", "androiddesktop", "--"])
        self.assertEqual(args[7:], features.mcp_command())

    def test_connect_claude_replaces_another_copys_server(self):
        (Path(self.config.name) / "claude.json").write_text(
            json.dumps({"mcpServers": {"androiddesktop": {"command": "elsewhere.exe", "args": []}}}), encoding="utf-8")
        with mock.patch.object(features, "find_claude", lambda: "claude"):
            self.assertEqual(self.f.connect_claude(), "")
        self.assertEqual([c.args[0][1:3] for c in self.run.call_args_list], [["mcp", "remove"], ["mcp", "add"]])

    def test_connect_claude_fails(self):
        self.run.return_value = done(1, stderr="MCP server androiddesktop already exists")
        with mock.patch.object(features, "find_claude", lambda: "claude"):
            self.assertIn("couldn't add", self.f.connect_claude())
        self.run.side_effect = subprocess.TimeoutExpired("claude", 60)
        with mock.patch.object(features, "find_claude", lambda: "claude"):
            self.assertIn("didn't answer", self.f.connect_claude())

    def test_disconnect_claude(self):
        with mock.patch.object(features, "find_claude", lambda: "claude"):
            self.assertEqual(self.f.disconnect_claude(), "")
        self.assertEqual(self.run.call_args.args[0], ["claude", "mcp", "remove", "--scope", "user", "androiddesktop"])

    def test_window_settings(self):
        self.assertTrue(self.f.set_app_windows("scrcpy"))
        self.assertFalse(self.f.set_app_windows("fast"))
        self.assertEqual(state.get("app_windows"), "scrcpy")
        self.assertTrue(self.f.set_quality("low"))
        self.assertFalse(self.f.set_quality("ultra"))
        self.assertEqual(state.get("scrcpy_quality"), "low")
        self.assertEqual(appwindow.scrcpy_quality(), appwindow.SCRCPY_QUALITY["low"])  # new windows use it

    def test_check_updates(self):
        self.assertIn("installer", self.f.check_updates())
        self.agent.updater = types.SimpleNamespace(enabled=False)
        self.assertIn("from source", self.f.check_updates())
        offer = None
        self.agent.updater = types.SimpleNamespace(enabled=True, check=mock.Mock(), snapshot=lambda: offer)
        self.assertEqual(self.f.check_updates(), "You have the latest version.")
        self.agent.updater.check.assert_called_with(force=True)
        offer = {"version": "0.2.0"}
        self.assertIn("0.2.0 is available", self.f.check_updates())

    def test_dismiss_whats_new(self):
        self.f.dismiss_whats_new()
        self.assertEqual(state.get(features.WHATS_NEW_SEEN), __version__)
        self.assertIsNone(features.whats_new(state.get(features.WHATS_NEW_SEEN)))

    def test_open_folder(self):
        with mock.patch.object(features.os, "startfile", create=True) as startfile, \
                mock.patch.object(features, "LOGS", Path(self.config.name) / "logs"):
            self.f.open_folder("logs")
            self.f.open_folder("C:\\Windows")  # only ours
        startfile.assert_called_once_with(Path(self.config.name) / "logs")


# ------------------------------------------------------------------ the launcher's Api and the agent
class ApiMethods(unittest.TestCase):
    def setUp(self):
        self.agent = FakeAgent()
        self.agent.features = mock.Mock(spec=features.Features)
        self.api = Api(self.agent)

    def test_cards_go_to_features(self):
        f = self.agent.features
        f.snapshot.return_value = {"android": {}}
        self.assertEqual(self.api.features(), {"android": {}})
        self.api.run_checks()
        self.api.hello_unlock(1, None)
        f.hello_unlock.assert_called_with(True, "")
        self.api.camera("install")
        f.camera.assert_called_with("install")
        self.api.connect_claude()
        self.api.disconnect_claude()
        self.api.set_plugin("hinge", 0)
        f.set_plugin.assert_called_with("hinge", False)
        self.api.set_app_windows("auto")
        self.api.set_quality("low")
        self.api.check_updates()
        self.api.dismiss_whats_new()
        self.api.open_folder("logs")
        for name in ("run_checks", "connect_claude", "disconnect_claude", "set_app_windows", "set_quality",
                     "check_updates", "dismiss_whats_new", "open_folder"):
            self.assertTrue(getattr(f, name).called, name)

    def test_android(self):
        self.api.restart_android()
        self.api.android_settings()
        self.api.screen_lock_settings()
        self.assertEqual(self.agent.calls, ["restart", ("open", "com.android.settings", None),
                                            ("open", "com.android.settings", ["-a", "android.settings.SECURITY_SETTINGS"])])

    def test_copy_claude_desktop(self):
        with mock.patch("androiddesktop.winapi.copy_text") as copy:
            text = self.api.copy_claude_desktop()
        copy.assert_called_once_with(text)
        self.assertIn('"androiddesktop"', text)

    def test_quit(self):
        before = set(threading.enumerate())
        self.api.quit()
        for t in set(threading.enumerate()) - before:
            t.join(5)
        self.assertIn("quit", self.agent.calls)


def bare_agent() -> Agent:
    """An Agent without starting anything (no VM, windows or WebView2)."""
    a = Agent.__new__(Agent)
    a.window, a.loaded, a.visible, a._page = None, False, False, ""
    a.features = mock.Mock()
    a.vm = types.SimpleNamespace(state=RUNNING, ensure_running=mock.Mock())
    return a


class AgentActions(unittest.TestCase):
    def test_show_page_waits_for_the_launcher(self):
        a = bare_agent()
        a.show_page("settings")
        self.assertEqual(a._page, "settings")
        a.window, a.loaded = mock.Mock(), True
        a.show_page("palette")
        a.window.evaluate_js.assert_called_once_with('window.ui && ui.show("palette")')
        a.show_page("nonsense")
        self.assertEqual(a.window.evaluate_js.call_count, 1)

    def test_restart(self):
        a = bare_agent()
        order: list[str] = []
        a.shutdown = lambda: order.append("shutdown")
        a.vm.ensure_running = lambda: order.append("start")
        before = set(threading.enumerate())
        with mock.patch("androiddesktop.app.log"):
            a.restart()
            for t in set(threading.enumerate()) - before:
                t.join(5)
        self.assertEqual(order, ["shutdown", "start"])
        a.vm.state = OFF
        a.restart()  # nothing to restart
        self.assertEqual(order, ["shutdown", "start"])

    def test_window_menu(self):
        a = bare_agent()
        app = App("com.example.notes", ".Main", "Notes", False, 0)
        a.notice, a.push, a.open = mock.Mock(), mock.Mock(), mock.Mock()
        a.show_launcher, a.uninstall = mock.Mock(), mock.Mock()
        with mock.patch("androiddesktop.app.shortcuts") as shortcuts:
            shortcuts.toggle_start.return_value = True
            a._window_action(app, "start")
            a._window_action(app, "desktop")
        shortcuts.toggle_start.assert_called_once_with(app)
        shortcuts.desktop_shortcut.assert_called_once_with(app)
        self.assertEqual(a.notice.call_args_list[0].args[0], "Notes pinned to Start")
        a._window_action(app, "info")
        a.open.assert_called_once_with("com.android.settings", ["-a", "android.settings.APPLICATION_DETAILS_SETTINGS",
                                                                "-d", "package:com.example.notes"])
        a._window_action(app, "plugin:hinge=off")
        a.features.set_plugin.assert_called_once_with("hinge", False)
        a._window_action(app, "uninstall")  # the window asked "are you sure?" already
        a.uninstall.assert_called_once_with("com.example.notes", confirmed=True)
        a._window_action(app, "settings")
        a.show_launcher.assert_called_once()
        self.assertEqual(a._page, "settings")

    def test_window_menu_works_off_the_window_thread(self):
        a = bare_agent()
        done = threading.Event()
        names: list[str] = []
        a._window_action = lambda app, action: (names.append(threading.current_thread().name), done.set())
        a.window_action(types.SimpleNamespace(app="app"), "desktop")
        self.assertTrue(done.wait(5))
        self.assertEqual(names, ["window-menu"])


class WindowMenu(unittest.TestCase):
    def test_items(self):
        app = App("com.example.notes", ".Main", "Notes & Lists", False, 0)
        items = appwindow.menu_items(app, False, True, [], None)
        actions = [i[0] for i in items if i]
        self.assertEqual(actions, ["pin", "start", "desktop", "info", "uninstall", "settings"])
        self.assertEqual(items[0], ("pin", "Keep on top", True))
        self.assertEqual(items[1][1], "Pin to Start")
        self.assertEqual(appwindow.menu_items(app, True, False, [], None)[1][1], "Remove from Start")

    def test_system_apps_cant_be_uninstalled(self):
        app = App("com.android.settings", ".Settings", "Settings", True, 0)
        self.assertNotIn("uninstall", [i[0] for i in appwindow.menu_items(app, False, False, [], None) if i])

    def test_plugins_and_phones(self):
        app = App("co.hinge.app", ".Main", "Hinge", False, 0)
        plugins = [{"name": "hinge", "title": "Hinge Matcher", "packages": ["co.hinge.app"], "on": True}]
        phones = [("send:group1", "Habib's iPhone"), ("pair", "Pair an iPhone or iPad...")]
        items = appwindow.menu_items(app, False, False, plugins, phones)
        self.assertEqual(items[0], ("Send to iPhone or iPad", [("send:send:group1", "Habib's iPhone", False),
                                                               ("send:pair", "Pair an iPhone or iPad...", False)]))
        self.assertIsNone(items[1])
        self.assertEqual(items[2], ("plugin:hinge=off", "Hinge Matcher", True))
        plugins[0]["on"] = False
        self.assertEqual(appwindow.menu_items(app, False, False, plugins, None)[0], ("plugin:hinge=on", "Hinge Matcher", False))


if __name__ == "__main__":
    unittest.main()
