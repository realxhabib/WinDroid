import tempfile
import unittest
from pathlib import Path
from unittest import mock

from androiddesktop import checks, datadir


def tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class DataDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def private(self, app: str = "Claude_pzs8sxrjxfjjc") -> Path:
        return self.root / "Packages" / app / "LocalCache" / "Local" / datadir.DATA.name

    def test_finds_private_copies(self):
        tree(self.private(), {"state.json": "{}"})
        tree(self.root / "Packages" / "Other_abc" / "LocalCache" / "Local" / "SomethingElse", {"x": "1"})
        self.assertEqual(datadir.private_copies(self.root), [self.private()])

    def test_no_packages_folder(self):
        self.assertEqual(datadir.private_copies(self.root), [])

    def test_merge_moves_everything_new(self):
        source, target = self.root / "src", self.root / "dst"
        tree(source, {"sdk/emulator/emulator.exe": "exe", "state.json": "{}"})
        self.assertEqual(datadir.merge_into(source, target), [])
        self.assertEqual((target / "sdk/emulator/emulator.exe").read_text(), "exe")
        self.assertTrue((target / "state.json").exists())
        self.assertFalse(source.exists())  # emptied and removed

    def test_merge_keeps_target_files_and_merges_folders(self):
        source, target = self.root / "src", self.root / "dst"
        tree(source, {"logs/androiddesktop.log": "old", "logs/emulator.log": "emu", "apps.json": "src"})
        tree(target, {"logs/androiddesktop.log": "new", "apps.json": "dst"})
        left = datadir.merge_into(source, target)
        self.assertCountEqual(left, ["apps.json", "logs/androiddesktop.log"])
        self.assertEqual((target / "logs/androiddesktop.log").read_text(), "new")
        self.assertEqual((target / "apps.json").read_text(), "dst")
        self.assertEqual((target / "logs/emulator.log").read_text(), "emu")
        self.assertTrue(source.exists())  # what couldn't move stays where it was

    def test_tidy_moves_a_private_copy_into_the_real_folder(self):
        tree(self.private(), {"avd/AndroidDesktop.avd/config.ini": "hw.ramSize=8192M"})
        real = self.root / datadir.DATA.name
        self.assertIsNone(datadir.tidy(real, self.root))
        self.assertEqual((real / "avd/AndroidDesktop.avd/config.ini").read_text(), "hw.ramSize=8192M")
        self.assertEqual(datadir.private_copies(self.root), [])

    def test_tidy_reports_what_it_could_not_move(self):
        tree(self.private(), {"state.json": "private"})
        real = self.root / datadir.DATA.name
        tree(real, {"state.json": "real"})
        problem = datadir.tidy(real, self.root)
        self.assertIn("couldn't move state.json", problem)

    def test_not_redirected_in_a_plain_folder(self):
        datadir._redirected = None
        try:
            with mock.patch.object(datadir, "local_root", return_value=self.root):
                (self.root / "Packages" / "App_x" / "LocalCache" / "Local").mkdir(parents=True)
                self.assertFalse(datadir.redirected())
            self.assertEqual([p.name for p in self.root.iterdir()], ["Packages"])  # the probe is gone
        finally:
            datadir._redirected = None

    def test_doctor_names_the_app_holding_the_data(self):
        check = checks.decide_data_folder([self.private()])
        self.assertEqual(check.level, checks.WARNING)
        self.assertIn("Claude's private storage", check.detail)
        self.assertEqual(checks.decide_data_folder([]).level, checks.OK)


if __name__ == "__main__":
    unittest.main()
