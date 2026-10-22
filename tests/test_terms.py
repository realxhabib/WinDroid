"""Google's terms before setup downloads anything (runtime.py, app.cli_setup), and plugins' default state."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import app, plugins, runtime

MANIFEST = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03">
  <license id="android-sdk-preview-license" type="text">Preview terms</license>
  <license id="android-sdk-license" type="text">Terms and Conditions

This is the Android Software Development Kit License Agreement
</license>
  <remotePackage path="platform-tools"><uses-license ref="android-sdk-license"/></remotePackage>
</sdk:sdk-repository>"""
TEXT = "Terms and Conditions\n\nThis is the Android Software Development Kit License Agreement"


class TermsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        record = Path(self._tmp.name) / "terms-accepted.json"
        patcher = mock.patch.object(runtime, "TERMS_RECORD", record)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.record = record

    def test_reads_the_sdk_license_from_the_manifest(self):
        self.assertEqual(runtime.parse_terms(MANIFEST), TEXT)

    def test_a_manifest_without_it_is_an_error(self):
        with self.assertRaises(ValueError):
            runtime.parse_terms(b"<sdk-repository><license id='other'>x</license></sdk-repository>")

    def test_agreeing_is_recorded_for_that_exact_text(self):
        self.assertFalse(runtime.terms_accepted(TEXT))
        runtime.accept_terms(TEXT)
        self.assertTrue(runtime.terms_accepted(TEXT))
        record = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual((record["license"], record["sha1"]), (runtime.TERMS_ID, runtime.terms_fingerprint(TEXT)))
        self.assertIn("accepted", record)

    def test_changed_terms_are_asked_again(self):
        runtime.accept_terms(TEXT)
        self.assertFalse(runtime.terms_accepted(TEXT + "\n\n14. A new clause."))

    def test_install_downloads_nothing_before_agreeing(self):
        with mock.patch.object(runtime, "needs_google_download", return_value=True), \
                mock.patch.object(runtime, "_download") as download:
            with self.assertRaises(runtime.TermsNotAccepted):
                runtime.install(lambda m, f: None, terms=TEXT)
            download.assert_not_called()

    def test_install_goes_ahead_once_agreed(self):
        runtime.accept_terms(TEXT)
        with mock.patch.object(runtime, "needs_google_download", return_value=True), \
                mock.patch.object(runtime, "PACKAGES", []), \
                mock.patch.object(runtime, "SDK", Path(self._tmp.name)), \
                mock.patch.object(runtime, "create_avd") as create_avd:
            runtime.install(lambda m, f: None, terms=TEXT)
        create_avd.assert_called_once()

    def test_an_installed_runtime_never_asks(self):
        with mock.patch.object(runtime, "needs_google_download", return_value=False), \
                mock.patch.object(runtime, "fetch_terms", side_effect=AssertionError("fetched")), \
                mock.patch.object(runtime, "PACKAGES", []), \
                mock.patch.object(runtime, "SDK", Path(self._tmp.name)), \
                mock.patch.object(runtime, "create_avd"):
            runtime.install(lambda m, f: None)


class CliSetupTest(unittest.TestCase):
    def run_setup(self, args, tty=False, answer=""):
        stdin = mock.Mock(isatty=lambda: tty)
        with mock.patch.object(runtime, "needs_google_download", return_value=True), \
                mock.patch.object(runtime, "fetch_terms", return_value=TEXT), \
                mock.patch.object(runtime, "terms_accepted", return_value=False), \
                mock.patch.object(runtime, "accept_terms") as accept, \
                mock.patch.object(runtime, "install") as install, \
                mock.patch("sys.stdin", stdin), mock.patch("builtins.input", return_value=answer), \
                mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()):
            code = app.cli_setup(args)
        return code, accept, install, out.getvalue()

    def test_prints_the_terms(self):
        _, _, _, out = self.run_setup([app.ACCEPT_FLAG])
        self.assertIn("Android Software Development Kit License Agreement", out)
        self.assertIn(runtime.TERMS_URL, out)

    def test_no_console_and_no_flag_stops(self):
        code, accept, install, _ = self.run_setup([])
        self.assertEqual(code, 1)
        accept.assert_not_called()
        install.assert_not_called()

    def test_the_flag_agrees(self):
        code, accept, install, _ = self.run_setup([app.ACCEPT_FLAG])
        self.assertEqual(code, 0)
        accept.assert_called_once_with(TEXT)
        install.assert_called_once()

    def test_answering_no_downloads_nothing(self):
        code, accept, install, _ = self.run_setup([], tty=True, answer="n")
        self.assertEqual(code, 1)
        install.assert_not_called()

    def test_answering_yes_agrees(self):
        code, accept, install, _ = self.run_setup([], tty=True, answer="y")
        self.assertEqual(code, 0)
        accept.assert_called_once_with(TEXT)


class PluginDefaultTest(unittest.TestCase):
    def enabled(self, cls, saved):
        with mock.patch.object(plugins.state, "get", lambda key, default=None: saved if key == "plugins" else default):
            return plugins.PluginHost.enabled(cls)

    def test_hinge_matcher_is_off_until_turned_on(self):
        from androiddesktop.plugins.hinge import HingeMatcher
        self.assertFalse(self.enabled(HingeMatcher, None))
        self.assertTrue(self.enabled(HingeMatcher, {"hinge": True}))

    def test_other_plugins_default_on(self):
        class Other(plugins.Plugin):
            name = "other"
        self.assertTrue(self.enabled(Other, None))
        self.assertFalse(self.enabled(Other, {"other": False}))


if __name__ == "__main__":
    unittest.main()
