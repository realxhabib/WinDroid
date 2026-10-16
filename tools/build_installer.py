"""Builds the WinDroid installer, dist/WinDroid-Setup-<version>.exe, and its .sha256 beside it (the
auto-updater checks downloads against it).

    pip install -r packaging/requirements.txt    # once, in a venv
    python tools/build_installer.py

1. PyInstaller (packaging/windroid.spec) makes dist/WinDroid: WinDroid.exe, windroid-cli.exe, _internal.
2. A smoke test runs both, in a scratch data folder: windroid-cli.exe doctor, and the MCP server in
   WinDroid.exe over pipes, the way an MCP client runs it. (The windows need a person: see the README.)
3. Inno Setup 6 (ISCC.exe, on PATH or where its installer puts it) compiles packaging/windroid.iss.
The release workflow (.github/workflows/release.yml) runs this too.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP = DIST / "WinDroid"


def version() -> str:
    text = (ROOT / "androiddesktop" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([\d.]+)"', text).group(1)


def iscc() -> Path:
    for path in (shutil.which("ISCC.exe"), *(os.path.expandvars(rf"{base}\Inno Setup 6\ISCC.exe") for base in (
            r"%LOCALAPPDATA%\Programs", "%ProgramFiles(x86)%", "%ProgramFiles%"))):
        if path and Path(path).is_file():
            return Path(path)
    sys.exit("Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup).")


def run(*cmd: str | Path, **kwargs) -> subprocess.CompletedProcess:
    print(">", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def smoke_test() -> None:
    with tempfile.TemporaryDirectory() as data:
        # A data folder and an Android of its own: nothing here touches an installed WinDroid's.
        env = dict(os.environ, ANDROIDDESKTOP_DATA=data, ANDROIDDESKTOP_SERIAL="emulator-5998")
        out = run(APP / "windroid-cli.exe", "doctor", env=env, capture_output=True, text=True, timeout=120).stdout
        assert "Data folder:" in out, out
        mcp = subprocess.Popen([str(APP / "WinDroid.exe"), "mcp"], env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE)
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "build", "version": "0"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out, _ = mcp.communicate("".join(json.dumps(r) + "\n" for r in requests).encode(), timeout=120)
        replies = [json.loads(line) for line in out.splitlines()]
        assert replies[0]["result"]["serverInfo"]["version"] == version(), replies
        assert any(t["name"] == "start_session" for t in replies[1]["result"]["tools"]), replies
    print("smoke test passed: windroid-cli.exe doctor, WinDroid.exe mcp over pipes")


def main() -> None:
    v = version()
    shutil.rmtree(APP, ignore_errors=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", DIST,
        "--workpath", ROOT / "build", ROOT / "packaging" / "windroid.spec", cwd=ROOT)
    smoke_test()
    run(iscc(), f"/DAppVersion={v}", ROOT / "packaging" / "windroid.iss", cwd=ROOT)
    setup = DIST / f"WinDroid-Setup-{v}.exe"
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    (DIST / (setup.name + ".sha256")).write_text(f"{digest}  {setup.name}\n", encoding="ascii")
    print(f"built {setup} ({setup.stat().st_size >> 20} MB), sha256 {digest}")


if __name__ == "__main__":
    main()
