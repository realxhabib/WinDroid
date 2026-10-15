"""Builds helper/src/*.java into androiddesktop/assets/appcatalog.jar (a dex jar for app_process).

Needs, in %LOCALAPPDATA%\\AndroidDesktop\\devtools: Microsoft OpenJDK 21 (jdk21.zip, or already
unpacked in jdk\\), Android SDK platform 36 (platform-36_r02.zip, or unpacked in platform\\) and
r8-9.5.26.jar (Google's D8). Only developers run this; the built jar is committed so users never
need a JDK.

    python tools/build_helper.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from androiddesktop.paths import DATA  # noqa: E402

DEVTOOLS = DATA / "devtools"
OUT = ROOT / "androiddesktop" / "assets" / "appcatalog.jar"


def unpack(zip_name: str, dest: Path) -> Path:
    if not dest.exists():
        with zipfile.ZipFile(DEVTOOLS / zip_name) as z:
            z.extractall(dest)
    return dest


def main() -> None:
    jdk_root = unpack("jdk21.zip", DEVTOOLS / "jdk")
    javac = next(jdk_root.glob("*/bin/javac.exe"))
    java = javac.with_name("java.exe")
    platform = unpack("platform-36_r02.zip", DEVTOOLS / "platform")
    android_jar = next(platform.glob("*/android.jar"))
    d8 = DEVTOOLS / "r8-9.5.26.jar"

    sources = sorted(str(p) for p in (ROOT / "helper" / "src").glob("*.java"))
    with tempfile.TemporaryDirectory() as tmp:
        classes, dex = Path(tmp, "classes"), Path(tmp, "dex")
        classes.mkdir()
        dex.mkdir()
        subprocess.run([str(javac), "--release", "11", "-cp", str(android_jar), "-d", str(classes), *sources],
                       check=True)
        class_files = [str(p) for p in classes.rglob("*.class")]
        subprocess.run([str(java), "-cp", str(d8), "com.android.tools.r8.D8", "--release", "--min-api", "26",
                        "--lib", str(android_jar), "--output", str(dex), *class_files], check=True)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dex / "classes.dex", "classes.dex")
    shutil.rmtree(DEVTOOLS / "tmp", ignore_errors=True)
    print(f"built {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
