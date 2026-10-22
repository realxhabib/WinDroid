# WinDroid roadmap

What ships, what's known to be missing, and what's next. One list for everything: when something is
done, move it to **Shipped** with the version; when an idea turns into work, give it a line in **Next**.

Status: `todo`, `doing`, `blocked` (waiting on something outside the code), `idea` (not decided yet).

## Shipped in 0.1.2

- Plugins are installed apart from WinDroid: folders in `%LOCALAPPDATA%\AndroidDesktop\plugins`, loaded
  when it starts (Settings > Plugins has a **Plugins folder** button). WinDroid ships with none.
- Hinge Matcher moved to its own repository, WinDroidPlugins (`C:\Users\dylan\windroid-plugins`).

## Shipped in 0.1.1

- MIT license (`LICENSE`).
- Setup shows Google's Android SDK License Agreement, as Google publishes it, and downloads nothing
  from Google until it's agreed to (`terms-accepted.json`; asked again if Google changes the text).
- Hinge Matcher is off until turned on in Settings > Plugins.

## Shipped in 0.1.0

- Each Android app in its own frameless Windows window: drag, snap, resize, taskbar button, Start menu.
- Android 16 with Google Play, headless (no Android screen of its own), booting in about 30 s.
- App windows straight from the emulator's own displays, no video encoding, with scrcpy as the fallback.
- Self-healing windows and Android crash recovery; setup checks (`doctor`).
- Screen-lock PIN support: unlock from the launcher or with Windows Hello; Android's own lock screen too.
- Windows Hello for Android's fingerprint prompts; the webcam as Android's camera, plus an optional
  full-picture virtual camera.
- Secure (`FLAG_SECURE`) screens shown and usable in app windows.
- AI agent layer: an MCP server that runs Android apps hidden, reads and operates them, and hands them
  to you in a window.
- Send a single app to an iPhone or iPad over Wi-Fi (Safari, paired, encrypted).
- Plugins, and Hinge Matcher: a dock and pop-ups on Hinge's window that suggest openers and replies.
- Settings page with live status for every feature, a Ctrl+K command palette, a "..." menu per window.
- Windows installer with auto-update from GitHub Releases.
- One data folder for every launcher, even from inside packaged apps such as Claude Desktop.

## Known gaps

| Status | Gap | Notes |
|---|---|---|
| todo | Smooth (emulator-display) windows only start once Android's lock screen is gone | Until then windows use scrcpy. Making emulator displays draw over the lock screen (an "always unlocked" display flag) would make them smooth right after boot. |
| todo | Webcam video mode shows only the middle 75% of the picture | Photo mode is fine. The fix is in the virtual camera; installing it needs administrator rights (the prompt was declined). |
| todo | Send to iPhone/iPad untested on a real device | Tested end to end with a desktop browser standing in for the phone. |
| todo | Clipboard, sound, tray menu, Install APK, uninstall, Snap Layouts flyout never tested by hand | Run through each once and fix what's broken. |
| todo | Taskbar pins use the Python icon | Windows wouldn't read icons from the data folder while it lived in Claude's private storage. Now that it's in the real folder, a window's relaunch icon may work again: re-test before turning it back on. |
| todo | The installer isn't code-signed | SmartScreen warns on first run. Needs a code-signing certificate. |
| todo | Dock labels in the launcher wrap onto two lines | Cosmetic. |
| idea | Names: the product is WinDroid, the Python package and data folder are still `androiddesktop` / `AndroidDesktop` | Renaming the data folder needs a migration; the package name is internal. |
| todo | License notices for the libraries inside the installer | pywin32, Pillow, pywebview, pythonnet, cryptography and others are bundled; several of their licenses ask for their notices to ship with them. Collect them into a THIRD-PARTY-NOTICES file in the installer. |
| todo | Launch film | Rendered locally (`film/`, kept out of git; posted by hand), 50 s, 1080p60, no narration: two halves: for you, every app in its own window (an emulator's phone dissolves into app windows); for your AI, headless (Claude uses Maps with no window and answers, or hands the app over). Real captures from the demo Android, the agent's hidden Maps session replayed, a score made in code. Page: https://claude.ai/artifact/J8VydqXW5gi4uy4kVmcrzc. Left: post it; a 4:5 cut for feeds; give the GitHub repo a description first (it shows "No description" and 0 stars). |

## Next

| Status | Feature | What it is |
|---|---|---|
| todo | Ask Android | A command bar (hotkey) where you type a task; Claude does it in hidden app sessions and hands you the window at the step that needs you (paying, sending). Builds on the agent layer. |
| todo | Notifications | Android notifications as Windows notifications, with reply; clicking one opens the app's window. |
| idea | Watchers | Standing jobs that check an app in the background (a delivery, a price, a reservation) and notify you on Windows. |
| idea | Recorded skills | Do a flow once by hand; replay it later as a Start menu command, matched by meaning, not position. |
| idea | Live widgets | Pin part of an app (an ETA, a balance, a streak) to the desktop as a small tile, kept fresh in the background. |
| idea | Windows accessibility bridge | Android's UI tree as Windows UI Automation, so Narrator, Voice Access and Power Automate work with Android apps. |
| idea | Live translation | Translate any app's text in place, over its window. |
| idea | Links and files across worlds | Instagram/TikTok links open in the Android app; "Open with" an Android app in Explorer; Android's share sheet to Windows apps. |
| idea | App clones | Two copies of an app with different accounts, side by side (Android work profiles are available). |
| idea | Time machine | A checkpoint of Android at every shutdown (`qemu-img`); restore Android to an earlier day. |
| idea | Rewind for Android | Optional local index of what you've seen in apps, searchable from Windows. Never secure screens. |
| idea | Android elsewhere, windows here | The Android VM on another PC or a cloud machine, with the windows on this one (adb works over the network). |
| idea | Game controls | Gamepad passthrough, and key mapping for touch games proposed automatically from what's on screen. |
| idea | Privacy X-ray | Per-app network use, the servers it talks to and the permissions it's using, in the window's bar. |
| idea | Sound on iPhone/iPad | Send an app's own sound to the device with its picture (Android can capture one app's playback). |

## How to work on this

- Run from source: see the README (Development). `python -m unittest` must pass; `tools/smoke_test.py`
  checks a live window against Android.
- Start WinDroid the way users do (Start menu, or `explorer.exe` on its shortcut) when testing what
  they'll see. Anything started from inside a packaged app runs with that app's view of `%LOCALAPPDATA%`
  (see "One data folder for everyone" in the README).
- Releases: bump `androiddesktop/__init__.py`'s `__version__`, then push a `v<version>` tag; the release
  workflow builds the installer and publishes it. Running `tools/build_installer.py` locally first
  catches packaging mistakes.
