import functools
import hashlib
import http.server
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from androiddesktop import state, update
from androiddesktop.update import AVAILABLE, FAILED, IDLE, INSTALLING, Release, Updater


def github_release(tag="v0.2.0", files=("WinDroid-Setup-0.2.0.exe", "WinDroid-Setup-0.2.0.exe.sha256"), **extra):
    """The shape of GitHub's /releases/latest answer, trimmed to what update.py reads."""
    return {"tag_name": tag, "html_url": f"https://github.com/realxhabib/WinDroid/releases/tag/{tag}",
            "draft": False, "prerelease": False, **extra,
            "assets": [{"name": f, "size": 17_000_000,
                        "browser_download_url": f"https://github.com/realxhabib/WinDroid/releases/download/{tag}/{f}"}
                       for f in files]}


class Versions(unittest.TestCase):
    def test_version_key(self):
        self.assertEqual(update.version_key("v1.2.10"), (1, 2, 10))
        self.assertEqual(update.version_key("0.1.0"), (0, 1, 0))
        self.assertEqual(update.version_key("nightly"), ())

    def test_newer(self):
        self.assertTrue(update.newer("0.1.10", than="0.1.9"))  # numbers, not strings
        self.assertTrue(update.newer("v0.2", than="0.1.5"))
        self.assertFalse(update.newer("0.1.0", than="0.1.0"))
        self.assertFalse(update.newer("0.0.9", than="0.1.0"))
        self.assertFalse(update.newer("latest", than="0.1.0"))


class Parsing(unittest.TestCase):
    def test_release_with_installer_and_checksum(self):
        r = update.parse_release(github_release())
        self.assertEqual(r.version, "0.2.0")
        self.assertTrue(r.installer.endswith("/v0.2.0/WinDroid-Setup-0.2.0.exe"))
        self.assertTrue(r.sha256.endswith("/v0.2.0/WinDroid-Setup-0.2.0.exe.sha256"))
        self.assertEqual(r.size, 17_000_000)

    def test_no_checksum_no_update(self):
        self.assertIsNone(update.parse_release(github_release(files=("WinDroid-Setup-0.2.0.exe",))))

    def test_installer_must_match_the_tag(self):
        self.assertIsNone(update.parse_release(github_release(tag="v0.3.0")))

    def test_drafts_and_prereleases_are_skipped(self):
        self.assertIsNone(update.parse_release(github_release(prerelease=True)))
        self.assertIsNone(update.parse_release(github_release(draft=True)))

    def test_other_assets_are_ignored(self):
        r = update.parse_release(github_release(files=("source.zip", "WinDroid-Setup-0.2.0.exe",
                                                       "WinDroid-Setup-0.2.0.exe.sha256")))
        self.assertEqual(r.version, "0.2.0")

    def test_sha256_file(self):
        digest = "ab" * 32
        self.assertEqual(update.parse_sha256(f"{digest.upper()}  WinDroid-Setup-0.2.0.exe\n"), digest)
        self.assertEqual(update.parse_sha256(digest), digest)
        with self.assertRaises(ValueError):
            update.parse_sha256("<html>Not Found</html>")


class Checking(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        for p in (mock.patch.object(state, "get", lambda k, d=None: self.saved.get(k, d)),
                  mock.patch.object(state, "put", self.saved.__setitem__),
                  mock.patch.object(update, "__version__", "0.1.0"),
                  mock.patch.object(update, "_clean_up"), mock.patch.object(update, "log")):
            p.start()
            self.addCleanup(p.stop)

    def test_offers_a_newer_release(self):
        changes = []
        u = Updater(True, on_change=lambda: changes.append(u.status))
        with mock.patch.object(update, "latest", return_value=update.parse_release(github_release())):
            u.check()
        self.assertEqual(u.status, AVAILABLE)
        self.assertEqual(u.snapshot()["version"], "0.2.0")
        self.assertEqual(u.snapshot()["current"], "0.1.0")
        self.assertEqual(changes, [AVAILABLE])
        self.assertEqual(self.saved["update"]["release"]["version"], "0.2.0")

    def test_asks_github_once_a_day(self):
        self.saved["update"] = {"checked": time.time() - 3600,
                                "release": asdict(update.parse_release(github_release()))}
        u = Updater(True)
        with mock.patch.object(update, "latest", side_effect=AssertionError("asked GitHub again")):
            u.check()
        self.assertEqual(u.status, AVAILABLE)  # from the answer it got an hour ago
        self.saved["update"]["checked"] -= 86400
        with mock.patch.object(update, "latest", return_value=None) as latest:
            u.check()
        latest.assert_called_once()

    def test_same_version_isnt_offered(self):
        u = Updater(True)
        with mock.patch.object(update, "latest", return_value=update.parse_release(github_release("v0.1.0", (
                "WinDroid-Setup-0.1.0.exe", "WinDroid-Setup-0.1.0.exe.sha256")))):
            u.check()
        self.assertEqual(u.status, IDLE)
        self.assertIsNone(u.snapshot())

    def test_offline_tries_again_next_time(self):
        u = Updater(True)
        with mock.patch.object(update, "latest", side_effect=OSError("offline")):
            u.check()
        self.assertEqual(u.status, IDLE)
        self.assertNotIn("update", self.saved)

    def test_a_source_checkout_never_updates(self):
        u = Updater(False)
        with mock.patch.object(update, "latest", side_effect=AssertionError("asked GitHub")):
            u.check()
        self.assertIsNone(u.snapshot())


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Installing(unittest.TestCase):
    """Download from a local web server, check the checksum, start the installer."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        served = self.dir / "served"
        served.mkdir()
        self.installer = b"MZ" + bytes(range(256)) * 4000  # ~1 MB, several progress steps
        (served / "WinDroid-Setup-0.2.0.exe").write_bytes(self.installer)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(served)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.release = Release("0.2.0", "https://github.com/realxhabib/WinDroid/releases/tag/v0.2.0",
                               f"{base}/WinDroid-Setup-0.2.0.exe", len(self.installer),
                               f"{base}/WinDroid-Setup-0.2.0.exe.sha256")
        self.served = served
        for p in (mock.patch.object(update, "UPDATES", self.dir / "updates"), mock.patch.object(update, "log")):
            p.start()
            self.addCleanup(p.stop)

    def run_install(self, checksum: str):
        (self.served / "WinDroid-Setup-0.2.0.exe.sha256").write_text(f"{checksum}  WinDroid-Setup-0.2.0.exe\n")
        ready = threading.Event()
        u = Updater(True, on_ready=ready.set)
        u.release, u.status = self.release, AVAILABLE
        with mock.patch.object(update.subprocess, "Popen") as popen:
            u.install()
            end = time.monotonic() + 10
            while u.status not in (INSTALLING, FAILED) and time.monotonic() < end:
                time.sleep(0.02)
        return u, popen, ready

    def test_verified_installer_runs_and_we_quit(self):
        u, popen, ready = self.run_install(hashlib.sha256(self.installer).hexdigest())
        self.assertEqual(u.status, INSTALLING)
        self.assertTrue(ready.is_set())
        cmd = popen.call_args.args[0]
        self.assertEqual(Path(cmd[0]), self.dir / "updates" / "WinDroid-Setup-0.2.0.exe")
        self.assertEqual(cmd[1:], update.INSTALLER_ARGS)
        self.assertIn("/relaunch=1", cmd)
        self.assertEqual(Path(cmd[0]).read_bytes(), self.installer)

    def test_checksum_mismatch_runs_nothing(self):
        u, popen, ready = self.run_install("0" * 64)
        self.assertEqual(u.status, FAILED)
        self.assertIn("checksum", u.snapshot()["error"])
        popen.assert_not_called()
        self.assertFalse(ready.is_set())
        self.assertEqual(list((self.dir / "updates").iterdir()), [])  # the bad download is gone


if __name__ == "__main__":
    unittest.main()
