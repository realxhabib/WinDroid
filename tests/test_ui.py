"""The launcher's page (ui/): the palette's fuzzy matching (through Node.js, when it's installed) and that
every element the scripts look up is in index.html."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "androiddesktop" / "ui"


class Page(unittest.TestCase):
    def test_every_element_the_scripts_use_is_there(self):
        html = (UI / "index.html").read_text(encoding="utf-8")
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        for script in ("app.js", "hub.js"):
            code = (UI / script).read_text(encoding="utf-8")
            ids |= set(re.findall(r'\bid: "([\w-]+)"', code))  # made by the script itself
        for script in ("app.js", "hub.js"):
            used = set(re.findall(r'\$\("([\w-]+)"\)', (UI / script).read_text(encoding="utf-8")))
            self.assertFalse(used - ids, f"{script} looks for elements index.html doesn't have")

    def test_scripts_and_styles_are_loaded(self):
        html = (UI / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "hub.css", "fuzzy.js", "app.js", "hub.js"):
            self.assertIn(f'"{name}"', html)
            self.assertTrue((UI / name).exists(), name)
        # fuzzy.js before the scripts that use it, hub.js after app.js (it uses app.js's state).
        order = [html.index(f'src="{n}"') for n in ("fuzzy.js", "app.js", "hub.js")]
        self.assertEqual(order, sorted(order))


@unittest.skipUnless(shutil.which("node"), "Node.js isn't installed")
class Fuzzy(unittest.TestCase):
    def test_fuzzy(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "js" / "fuzzy.test.js")], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("ok", out.stdout)


if __name__ == "__main__":
    unittest.main()
