"""Where WinDroid keeps its runtime: everything lives under %LOCALAPPDATA%\\AndroidDesktop (the folder
keeps the app's first name, Android Desktop, so existing installs carry on)."""
from __future__ import annotations

import os
import re
from pathlib import Path

# ANDROIDDESKTOP_DATA puts everything in another folder: a separate install, for testing. With
# ANDROIDDESKTOP_SERIAL as well, it's an instance of its own, with its own Android (see SERIAL).
DATA = Path(os.environ.get("ANDROIDDESKTOP_DATA") or
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AndroidDesktop")
DOWNLOADS = DATA / "downloads"
SDK = DATA / "sdk"
AVD_HOME = DATA / "avd"
SCRCPY_HOME = DATA / "scrcpy"
ICONS = DATA / "icons"
LOGS = DATA / "logs"
STATE = DATA / "state.json"

AVD_NAME = "AndroidDesktop"
DEFAULT_SERIAL = "emulator-5584"  # port 5584: off the default 5554, so we never collide with a developer's own emulator
# ANDROIDDESKTOP_SERIAL points everything at another Android, e.g. a developer's test emulator. That
# Android then gets an agent of its own (INSTANCE), apart from the user's. An emulator serial's port is
# also where our own Android starts, if it has to.
SERIAL = os.environ.get("ANDROIDDESKTOP_SERIAL") or DEFAULT_SERIAL
INSTANCE = "" if SERIAL == DEFAULT_SERIAL else f".{SERIAL}"
EMULATOR_PORT = int(SERIAL[len("emulator-"):]) if re.fullmatch(r"emulator-\d+", SERIAL) else 5584
GRPC_PORT = EMULATOR_PORT + 3000  # the emulator's gRPC control API (emuview.py)

EMULATOR_EXE = SDK / "emulator" / "emulator.exe"
ADB_EXE = SDK / "platform-tools" / "adb.exe"


def android_env() -> dict[str, str]:
    """Environment for the emulator, adb and scrcpy. Android's user home stays the default ~/.android:
    the emulator authorizes the adb key it finds there, so adb and the emulator must agree on it."""
    env = dict(os.environ)
    env.update({
        "ANDROID_HOME": str(SDK),
        "ANDROID_SDK_ROOT": str(SDK),
        "ANDROID_AVD_HOME": str(AVD_HOME),
        "ADB": str(ADB_EXE),  # scrcpy uses the same adb as everything else
    })
    return env
