"""How WinDroid starts itself (shortcuts, taskbar pins, the MCP server's boot request), in a source checkout
and installed, and what the uninstaller's shortcut cleanup takes away."""
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import app, shortcuts, winapi
from androiddesktop.catalog import App

INSTALLED = r"C:\Users\someone\AppData\Local\Programs\WinDroid"


class Command(unittest.TestCase):
    def test_source_checkout_runs_pythonw(self):
        cmd = winapi.command("open", "com.android.chrome")
        self.assertEqual(Path(cmd[0]).name.lower(), "pythonw.exe")
        self.assertEqual(cmd[1:], ["-m", "androiddesktop", "open", "com.android.chrome"])
        self.assertFalse(winapi.console_exe())

    def test_installed_runs_windroid_exe_even_from_the_console_one(self):
        for exe in ("WinDroid.exe", "windroid-cli.exe"):
            with mock.patch.object(winapi, "FROZEN", True), mock.patch.object(sys, "executable", rf"{INSTALLED}\{exe}"):
                self.assertEqual(winapi.command("open", "com.android.chrome"),
                                 [rf"{INSTALLED}\WinDroid.exe", "open", "com.android.chrome"])
                self.assertEqual(winapi.command(), [rf"{INSTALLED}\WinDroid.exe"])
                self.assertEqual(winapi.console_exe(), exe == "windroid-cli.exe")


class Paths(unittest.TestCase):
    def paths(self, **env) -> list[str]:
        code = "from androiddesktop import paths as p; print(p.DATA, p.SERIAL, p.INSTANCE, p.EMULATOR_PORT, sep='|')"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("ANDROIDDESKTOP_")}
        out = subprocess.run([sys.executable, "-c", code], env={**clean, **env}, capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1], check=True).stdout
        return out.strip().split("|")

    def test_default(self):
        data, serial, instance, port = self.paths()
        self.assertEqual(Path(data).name, "AndroidDesktop")
        self.assertEqual((serial, instance, port), ("emulator-5584", "", "5584"))

    def test_separate_instance(self):
        data, serial, instance, port = self.paths(ANDROIDDESKTOP_DATA=r"C:\test\data",
                                                  ANDROIDDESKTOP_SERIAL="emulator-5640")
        self.assertEqual((data, serial, instance, port), (r"C:\test\data", "emulator-5640", ".emulator-5640", "5640"))

    def test_serial_that_isnt_an_emulator(self):
        self.assertEqual(self.paths(ANDROIDDESKTOP_SERIAL="192.168.1.5:5555")[3], "5584")


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


needs_port = unittest.skipUnless(port_free(app.UI_PORT), f"port {app.UI_PORT} is in use, likely by a running WinDroid")


class LauncherPage(unittest.TestCase):
    @needs_port
    def test_port_is_ours_and_stable(self):
        with mock.patch.object(app, "INSTANCE", ""):
            self.assertEqual(app.ui_port(), app.UI_PORT)
        with mock.patch.object(app, "INSTANCE", ".emulator-5640"):
            other = app.ui_port()
            self.assertNotIn(other, (app.UI_PORT, 42001))
            self.assertEqual(app.ui_port(), other)

    @needs_port
    def test_port_taken_by_something_else(self):
        with socket.socket() as taken, mock.patch.object(app, "INSTANCE", ""):
            taken.bind(("127.0.0.1", app.UI_PORT))
            taken.listen()
            self.assertNotEqual(app.ui_port(), app.UI_PORT)

    def test_cache_goes_when_the_page_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            webview, saved = Path(tmp), {}
            cache = webview / "EBWebView" / "Default" / "Cache"
            with mock.patch.object(app, "WEBVIEW", webview), \
                    mock.patch.object(app.state, "get", lambda k, d=None: saved.get(k, d)), \
                    mock.patch.object(app.state, "put", saved.__setitem__):
                cache.mkdir(parents=True)
                app.forget_old_ui()  # first start of this version
                self.assertFalse(cache.exists())
                cache.mkdir(parents=True)
                app.forget_old_ui()  # same page: the cache stays
                self.assertTrue(cache.exists())
                saved["ui"] = "an older page"
                app.forget_old_ui()
                self.assertFalse(cache.exists())


class Shortcuts(unittest.TestCase):
    """Real shortcuts, in a scratch Start menu and desktop."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "Desktop").mkdir()
        for p in (mock.patch.object(shortcuts, "APPS_FOLDER", self.root / "Start" / "Android apps"),
                  mock.patch.object(shortcuts, "_desktop", lambda: self.root / "Desktop")):
            p.start()
            self.addCleanup(p.stop)
        self.chrome = App("com.android.chrome", "", "Chrome", False, 0)
        self.clock = App("com.google.android.deskclock", "", "Clock", True, 0)

    def test_start_menu_shortcut_runs_open(self):
        self.assertTrue(shortcuts.toggle_start(self.chrome))
        target, aumid = shortcuts._target(shortcuts.start_path(self.chrome))
        self.assertEqual(os.path.normcase(target), os.path.normcase(winapi.command()[0]))
        self.assertEqual(aumid, "AndroidDesktop.App.com.android.chrome")
        self.assertFalse(shortcuts.toggle_start(self.chrome))
        self.assertFalse(shortcuts.APPS_FOLDER.exists())

    def test_uninstall_removes_only_our_shortcuts(self):
        shortcuts.toggle_start(self.chrome)
        ours = shortcuts.desktop_shortcut(self.clock)
        # Someone else's shortcut, and an app shortcut of another WinDroid (a source checkout, say).
        shortcuts.create(self.root / "Desktop" / "Other.lnk", [], shortcuts.MAIN_ICON, "Someone.Else", "")
        other_install = self.root / "Desktop" / "Clock (other WinDroid).lnk"
        with mock.patch.object(winapi, "command", lambda *a: [r"C:\elsewhere\WinDroid.exe", *a]):
            shortcuts.create(other_install, ["open", "x"], shortcuts.MAIN_ICON, shortcuts.app_id("x"), "")
        removed = shortcuts.remove_ours()
        self.assertCountEqual(removed, [shortcuts.start_path(self.chrome), ours])
        self.assertFalse(shortcuts.APPS_FOLDER.exists())
        self.assertCountEqual([p.name for p in (self.root / "Desktop").iterdir()], ["Other.lnk", other_install.name])


if __name__ == "__main__":
    unittest.main()
