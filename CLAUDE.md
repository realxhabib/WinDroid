# WinDroid

Android apps on Windows, each in its own window, from a headless Android Emulator VM. The product is
**WinDroid**; the Python package is still `androiddesktop` and the data folder
`%LOCALAPPDATA%\AndroidDesktop`. README.md explains every feature and how it works; **ROADMAP.md is the
backlog**: read it before starting, and update it (move items to Shipped, add new gaps) as work lands.

## Run and test

- `.venv\Scripts\pip install -e .` once; `.venv\Scripts\python -m pyflakes androiddesktop tools tests`
  and `.venv\Scripts\python -m unittest` must pass before any commit.
- `tools/smoke_test.py` checks a live window against Android; `tools/try_windows.py <package>` opens
  windows in a harness process; `tools/plugin_check.py` checks the plugin system; `tools/build_installer.py`
  builds the installer (build it in a clean venv from `packaging/requirements.txt`, as the release does).
- Release: bump `__version__` in `androiddesktop/__init__.py`, push a `v<version>` tag; the Release
  workflow builds and publishes the installer (about 2 minutes). Ask the user before pushing or tagging.

## Rules learned the hard way

- **Claude Desktop is a packaged app.** Anything started from these sessions that creates a new folder
  directly in `%LOCALAPPDATA%` lands in Claude's private storage, invisible to the rest of Windows (that
  happened to the whole 7 GB runtime once). `datadir.ensure()` guards every entry point; keep it first.
  To see what users see, start WinDroid from the Start menu or with `explorer.exe` on its shortcut.
- **The user's Android is real and in daily use**: Google account, Hinge, a screen-lock PIN.
  - Never restart or power it off without asking.
  - Never type the PIN or any password: the user unlocks, in the launcher or with Windows Hello.
  - Never act on the user's accounts (no likes, messages or purchases).
  - Never send input to a display you didn't create. Misrouted keystrokes once set a PIN by accident.
- **Test on your own Android** when you need to break things: a separate AVD in a short path
  (`%TEMP%\...`, since long paths fail), with `ANDROIDDESKTOP_SERIAL` and `ANDROIDDESKTOP_DATA` pointing
  at it (README, Development).
- **Emulator quirks:**
  - `emu multidisplay add` answers "KO: setMultiDisplay not supported" on a windowless emulator yet
    creates the display, so check whether it appeared. Display ids are 1 to 8.
  - Apps on emulator displays don't draw while Android's lock screen is up, so windows use scrcpy
    until it's gone (`adb.keyguard_showing()`).
- **Privacy:**
  - Never log PINs, API keys or the Hinge config.
  - No real people's names or screenshots in the repo (test fixtures included); delete captures of
    the user's apps after use.
- **Hinge Matcher** fills in text for the user to send; it never likes, sends or skips by itself. Keep it so.
- **Fewer chats.** The user prefers work done in one chat. For parallel work use git worktrees, merge
  with tests, and track ideas in ROADMAP.md rather than proposing many new sessions.
