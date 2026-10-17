"""Builds helper/camera/AndroidDesktopCamera.cpp into androiddesktop/assets/AndroidDesktopCamera.dll,
the Android Desktop Camera (see androiddesktop/camera.py).

Needs Visual Studio 2022 (or its Build Tools) with the C++ workload and a Windows 11 SDK (10.0.22000 or
later, for mfvirtualcamera.h). Only developers run this; the built DLL is committed so users never need
a compiler.

    python tools/build_camera.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "helper" / "camera" / "AndroidDesktopCamera.cpp"
OUT = ROOT / "androiddesktop" / "assets" / "AndroidDesktopCamera.dll"
VSWHERE = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
               "Microsoft Visual Studio", "Installer", "vswhere.exe")


def vcvars() -> Path:
    found = subprocess.run([str(VSWHERE), "-latest", "-products", "*", "-requires",
                            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                           capture_output=True, text=True).stdout.strip()
    if not found:
        sys.exit("Visual Studio with the C++ workload isn't installed.")
    return Path(found, "VC", "Auxiliary", "Build", "vcvars64.bat")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        compile_ = (f'cl /nologo /O2 /MT /EHsc /std:c++17 /W4 /wd4100 /DUNICODE /D_UNICODE /LD "{SOURCE}" '
                    f'/Fo"{tmp}\\\\" /Fe"{tmp}\\AndroidDesktopCamera.dll" /link /NOLOGO')
        env = dict(os.environ)  # vcvars64 looks for vswhere on PATH
        env["PATH"] = str(VSWHERE.parent) + os.pathsep + env.get("PATH", "")
        subprocess.run(f'call "{vcvars()}" >nul && {compile_}', shell=True, check=True, cwd=tmp, env=env)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_bytes(Path(tmp, "AndroidDesktopCamera.dll").read_bytes())
    print(f"built {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
