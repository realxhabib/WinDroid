"""Where Android Desktop keeps its runtime: everything lives under %LOCALAPPDATA%\\AndroidDesktop."""
from __future__ import annotations

import os
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
EMULATOR_PORT = 5584  # off the default 5554 so we never collide with a developer's own emulator
# ANDROIDDESKTOP_SERIAL points everything at another Android, e.g. a developer's test emulator. That
# Android then gets an agent of its own (INSTANCE), apart from the user's.
SERIAL = os.environ.get("ANDROIDDESKTOP_SERIAL") or f"emulator-{EMULATOR_PORT}"
INSTANCE = "" if SERIAL == f"emulator-{EMULATOR_PORT}" else f".{SERIAL}"

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
