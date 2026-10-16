# WinDroid

Android apps on Windows, each in its own window. Android runs as a headless virtual machine on your
PC (no phone needed); every app you open gets a separate, frameless, resizable window that you can
drag, snap, pin to the taskbar or maximize like any other Windows app.

## What it does

- **One window per app.** Every app runs on its own Android virtual display, shown in its own
  Windows window. Resize the window and the app re-lays itself out for the new size.
- **Sleek, frameless windows.** No Windows title bar: a slim strip that takes on the app's own top
  color, with Back, the app's icon and name, Keep on top, Minimize, Maximize and Close. The native
  frame is kept underneath, so rounded corners, shadows, Aero Snap and the Windows 11 Snap Layouts
  flyout (hover Maximize) all work.
- **Real Windows citizens.** Each app has its own taskbar button and Alt+Tab entry with its real
  icon. Right-click an app in the launcher, choose **Pin to Start**, and it appears in the Start
  menu; from there you can also pin it to the taskbar.
- **Launcher.** A Start-menu-style window listing every Android app with its real icon, plus search,
  **Get apps** (Play Store) and **Install APK**.
- **Google Play.** Android 16 with the Play Store, so you can install apps the usual way.
- **PC keyboard and mouse.** Android sees a hardware keyboard, so the on-screen keyboard stays out of
  the way. Right-click (or your mouse's back button) goes Back; Shift+right-click is a real
  right-click. Clipboard is shared both ways.
- **Sound** plays through your PC, and **the camera** is your PC's webcam.
- **Windows Hello for Android's fingerprint.** When an Android app asks for your fingerprint,
  Windows Hello asks instead, above that app's window: face, fingerprint reader or Windows PIN. Set it up once with **Set up fingerprint** in the launcher.
- **Recovers by itself.** If an app's connection drops, or Android itself crashes, its window shows
  "Reconnecting..." and brings the app back (starting Android again if needed) instead of vanishing.
- **Android apps for AI agents.** An MCP server lets Claude (or any MCP client) run Android apps
  hidden, read and operate them, and hand one to you in a normal window when you should see it.
- **Keeps itself up to date.** The launcher offers each new version, and installs it when you say so.

Everything runs locally.

## Requirements

- Windows 10 (version 1809 or later) or Windows 11, 64-bit, with hardware virtualization and the
  **Windows Hypervisor Platform** feature (on by default when Hyper-V, WSL2 or Windows Sandbox are in
  use).
- About 7 GB of disk (more as you install apps), 2.4 GB of it downloaded once on first run.
- 8 GB of RAM or more; Android uses about 5 GB while running.

WinDroid checks these itself, before setup and before every start of Android, and shows what to
fix in the launcher (for example a **Turn it on** button for Windows Hypervisor Platform).

## Install

1. Download **WinDroid-Setup-*version*.exe** from the
   [latest release](https://github.com/realxhabib/WinDroid/releases/latest).
2. Run it. It installs WinDroid for your Windows account only, so it doesn't ask for administrator
   rights, and adds **WinDroid** to the Start menu. There's no Python or anything else to install.

The installer isn't code-signed yet, so **Microsoft Defender SmartScreen** may stop it with
"Windows protected your PC". Click **More info**, then **Run anyway**. To make sure the download is
the one published, compare its SHA-256 with the `.sha256` file next to it in the release:

```powershell
Get-FileHash "$env:USERPROFILE\Downloads\WinDroid-Setup-0.1.0.exe"
```

**Updates.** Once a day, WinDroid asks GitHub whether there's a newer release. If there is, the
launcher says so; click **Update** and it downloads the new installer, checks it against its published
SHA-256, closes your app windows, shuts Android down, installs the new version and starts again.
**What's new** opens the release notes, and the cross puts the offer away until WinDroid next starts.

**Uninstall** from **Settings > Apps > Installed apps > WinDroid > Uninstall** (or Control Panel's
Programs and Features). That shuts Android down and removes the program, its Start menu entry and the
app shortcuts WinDroid made (Start menu and desktop). Then it asks whether to delete Android as well:
the Android runtime WinDroid downloaded and everything in Android, your apps, their data and your
Google sign-in (`%LOCALAPPDATA%\AndroidDesktop`, about 7 GB). Keep it if you might install WinDroid
again: it carries on where you left off.

## Use it

Open **WinDroid** from the Start menu. The first start shows **Set up Android**, which downloads
Google's Android Emulator, an Android 16 (Google Play) system image, adb and
[scrcpy](https://github.com/Genymobile/scrcpy). All are checksum-verified and stored in
`%LOCALAPPDATA%\AndroidDesktop` (the folder keeps WinDroid's first name, Android Desktop).

Android takes about 30 seconds to start; the very first boot takes a few minutes while Android
sets itself up. Sign in to Google Play from **Get apps** to install apps.

If you set a screen-lock PIN or password in Android (Google Play may ask for one), Android starts
**locked** after every boot, like a phone after a restart: your apps' data stays encrypted until the
PIN is entered. The launcher then asks for it, and apps you open wait with an **Unlock** button. Wrong
tries count towards Android's own lockout. To stop this, remove the screen lock in Android's
**Settings > Security > Screen lock**.

To skip typing the PIN, tick **Unlock with Windows Hello from now on** under the PIN field (shown when
Windows Hello is set up on the PC). WinDroid then keeps the PIN, encrypted with DPAPI for your
Windows account (`%LOCALAPPDATA%\AndroidDesktop\android-pin.bin`), and after each start of Android
asks Windows Hello instead, above the launcher or the app window that's waiting. Untick the box
the next time Android asks for its PIN to forget it. If the PIN changes in Android, it's forgotten
and asked for again. DPAPI keeps it from other Windows users and from copies of the file, not from
programs running as you; Windows Hello is what stands between them and Android. The PIN never goes
into a log.

### Windows Hello for Android's fingerprint

Android here has a (virtual) fingerprint sensor. Click **Set up fingerprint** in the launcher (shown
until a fingerprint is added, if Windows Hello is set up on the PC): Android's Settings opens at
**Add fingerprint**, asks for your Android PIN, and WinDroid touches the sensor each time
Android asks. From then on, whenever Android waits for a fingerprint, Windows Hello asks you instead,
above the window of the app that's asking, and touches the sensor once it's you. Canceling Windows
Hello leaves the finger off.

### Command line

WinDroid is installed in `%LOCALAPPDATA%\Programs\WinDroid`. `WinDroid.exe` never opens a console
window, so it's what shortcuts run; `windroid-cli.exe` is the same program for a console, for the
commands that print.

| Command | |
|---|---|
| `WinDroid.exe` | Open the launcher (starts Android if needed) |
| `WinDroid.exe open <package>` | Open one app in its own window, e.g. `com.android.chrome` |
| `WinDroid.exe stop` | Close every app window and shut Android down |
| `WinDroid.exe quit` | ...and exit WinDroid |
| `WinDroid.exe boot` | Start Android without showing anything (what an AI agent's session asks for) |
| `WinDroid.exe mcp` | The MCP server for AI agents (below), over stdin/stdout |
| `windroid-cli.exe setup` | Download and install the Android runtime, with progress in the console |
| `windroid-cli.exe doctor` | Check whether this PC can run Android, and print what's wrong |

From a source checkout (see Development), `pythonw -m androiddesktop` takes the place of
`WinDroid.exe` and `python -m androiddesktop` that of `windroid-cli.exe`.

A tray icon (bottom right of the taskbar) opens the launcher and has **Shut down Android** and
**Quit**. Closing the launcher keeps your app windows running. When no app windows are open, the
launcher is closed and no AI agent has an app running, Android shuts itself down after 10 minutes.

### Android apps for AI agents (MCP)

WinDroid is also an MCP server, so an AI agent such as Claude can use Android apps for you.
By default the agent's apps run **hidden**: each on an Android display of its own with no window, so
nothing on your screen changes while it works. The agent reads an app as a compact accessibility
tree (about 2 KB of JSON for a typical screen, read in under 100 ms), taps, types and scrolls by
element id, and can add a screenshot when the tree isn't enough. When you should see something, or
take over, the agent calls `show_to_user` and the app moves into a normal WinDroid window,
exactly as the agent left it.

Add it to **Claude Code** (`--scope user` makes it available in every project):

```powershell
claude mcp add --scope user windroid -- "$env:LOCALAPPDATA\Programs\WinDroid\WinDroid.exe" mcp
```

Or to **Claude Desktop**, in `%APPDATA%\Claude\claude_desktop_config.json` (with your Windows user
name), then restart it:

```json
{
  "mcpServers": {
    "windroid": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\WinDroid\\WinDroid.exe",
      "args": ["mcp"]
    }
  }
}
```

From a source checkout, the command is the venv's `python.exe` with `-m androiddesktop mcp`.

Then ask, for example: *"Using WinDroid, set a 10-minute timer in Clock and tell me how long
is left."* The tools:

| Tool | |
|---|---|
| `list_apps` | The installed apps: label and package |
| `start_session(package, visible=false)` | Start an app (by package or name) hidden, or in a window with `visible=true`. Returns a session id. With `visible=true`, an app already open in your window is joined rather than started again |
| `read_screen(session, screenshot=false)` | The screen as JSON: windows, top first, and their elements with `id`, `class`, `text`/`desc`, `bounds` and flags (`clickable`, `editable`, `scrollable`, `checked`, ...). `screenshot=true` adds a JPEG (hidden sessions) |
| `tap(session, element_id \| x, y)` | Tap an element or a point, then wait for the screen to settle |
| `type_text(session, text, element_id?, submit=false)` | Put text in a field (any language; it replaces what's there), optionally pressing the keyboard's action key |
| `press(session, back \| home \| enter)` | A key |
| `scroll(session, direction, element_id?)` | Scroll up, down, left or right; `down` shows what's further down |
| `wait_for(session, text, timeout=10)` | Wait until text appears; returns the matching elements |
| `show_to_user(session)` | Move the app into a normal window; the session goes on there |
| `end_session(session)` | End it: a hidden app closes, an app in your window stays open |

Things worth knowing:

- **Secure screens are yours.** While Android shows a screen marked secure (PIN and password entry,
  payment sheets: `FLAG_SECURE`), the agent gets `secure screen: needs the user` instead of its
  content, its input is refused, and it's told to call `show_to_user` so you can take it from there.
- **Locked Android.** If Android is locked after a start (screen-lock PIN), the agent is told to ask
  you to unlock it in the launcher; it never types your PIN. If Android isn't running, the first
  session starts it, without showing anything.
- **Logs.** Every tool call goes to `%LOCALAPPDATA%\AndroidDesktop\logs\agent.log`, with its result
  and timing. Text typed with `type_text` is logged as its length only, since it may be a password.
- **Cleanup.** Ending a session, or the MCP server (when Claude Code exits), closes its hidden apps.

Logs: `%LOCALAPPDATA%\AndroidDesktop\logs` (`androiddesktop.log`, `emulator.log`, `agent.log` for AI
agents, and one file per app window under `windows\`). Uncaught errors from any thread are logged
too, with their traceback.

## How it works

```
Launcher (WebView2)      App window (Win32, frameless)      App window ...
        \                          |  hosts                       |
         \                 scrcpy video window  --- adb ---  virtual display N
          \________ agent process ________/                       |
                         |                               Android 16 VM (headless
                    adb / emulator console                Android Emulator, WHPX)
```

- **The VM** (`vm.py`): Google's Android Emulator, run with `-no-window`, accelerated by the Windows
  Hypervisor Platform. Android's own screen is never shown.
- **App windows** (`appwindow.py`): each app is started on a new Android *virtual display* by
  [scrcpy 5](https://github.com/Genymobile/scrcpy) (`--new-display --flex-display --start-app`). Its
  borderless video window is re-parented into a custom Win32 window that draws the slim bar
  (`WM_NCCALCSIZE` removes the caption, `WM_NCHITTEST` maps the bar to caption, resize edges and
  `HTMAXBUTTON` for Snap Layouts). The bar's color is sampled from the top of the app's image. A
  flex display follows the window size, so resizing the window resizes the app. When an app exits
  (e.g. Back on its first screen), its window closes too. If scrcpy exits on its own instead, the
  window reconnects: it relaunches scrcpy on a fresh display, backing off 1, 2, 4, 8 and 15 seconds,
  then offers **Try again**.
- **Protected screens** (`protected.py`): some screens are marked secure (`FLAG_SECURE`): Play's
  install and purchase sheet, Google's passkey QR code, PIN pads. Android blacks them out on any
  display that isn't secure itself and, since Android 16, drops every click and key sent to them.
  scrcpy's displays can't be secure (that takes a permission the shell user doesn't have), but the
  emulator's own multi-display service can make secure displays, and the emulator draws them on the
  PC side. So when a secure window turns up, the app moves to such a display (`adb emu multidisplay`,
  `am display move-stack`), and the window shows it through emulator screenshots, a few frames a
  second, passing clicks and keys on with `input`. When the screen closes, the app moves back.
  Android 16's SurfaceFlinger has a bug that moving such an app sets off: after a screenshot of a
  secure layer it treats *every* layer as secure, so every app window goes black and deaf until the
  next screenshot of something else. After each move, `helper/src/SecureReset.java` (in
  `appcatalog.jar`) takes that next screenshot: 1×1 pixel of a layer of its own, on no display.
- **Crash recovery** (`vm.py`): the VM notices an emulator that died (its process ended, or adb lost
  it for good) and marks Android as off; the next window that reconnects boots it again.
- **Locked Android** (`vm.py`, `adb.py`): after boot, `sys.user.0.ce_available` and
  `am get-started-user-state 0` tell whether the user is still locked ("direct boot"). Locked, the VM
  reports `locked`, the app list isn't refreshed (Android lists only its direct-boot apps then), and
  the PIN from the launcher goes to `locksettings verify`, which unlocks the user's storage.
- **Camera** (`runtime.py`): before every start of Android, `emulator -webcam-list` tells which
  webcams are plugged in, and both of Android's cameras (`hw.camera.front`, `hw.camera.back` in the
  AVD's `config.ini`) are set to the first one by label (`webcam0`), or to `none` without one. On a PC
  every camera faces you, so apps that pick the "back" camera (QR code scanners) get the good one too.
- **Windows Hello** (`biometrics.py`, `hello.py`): the emulator's console puts a finger on Android's
  virtual sensor (`adb emu finger touch 1`). Android's `BiometricScheduler` runs one client at a time,
  shown in `dumpsys fingerprint` as its "Current operation" with the package that asked (an
  authentication for a BiometricPrompt, the lock screen, Play or a passkey; an enrollment for
  Settings). Lines from Android's biometric services in logcat say when to look again. When an
  authentication is waiting, the app's window is found through `cmd activity stack list` (the
  display where that package is on top), and Windows Hello asks above it: WinRT's
  `UserConsentVerifier` through `IUserConsentVerifierInterop.RequestVerificationForWindowAsync`,
  called with plain ctypes. Verified, the finger goes down; if the prompt goes away first, so does
  the Windows Hello prompt. During **Set up fingerprint**, enrollment clients get the finger
  without Windows Hello: Android asked for its PIN already.
- **PC checks** (`checks.py`): Windows Hypervisor Platform (asked directly through `WinHvPlatform.dll`,
  and through `emulator -accel-check` once installed), memory and disk space.
- **App catalog** (`catalog.py` and `helper/src/AppCatalog.java`): a small helper, run inside
  Android through `app_process` the way scrcpy runs its server, asks PackageManager for the
  launchable apps and renders their real icons, including adaptive icons with a rounded-square mask.
- **Agent** (`app.py`, `manager.py`): one process owns the VM, the windows, the tray icon and the
  launcher. Start menu shortcuts send their request to the running agent via `WM_COPYDATA`.
- **Installed program and updates** (`packaging/`, `update.py`): see Development > Build the installer.
  The updater reads `api.github.com/repos/realxhabib/WinDroid/releases/latest` (at most once a day,
  remembered in `state.json`), and runs a downloaded installer only if its SHA-256 matches the
  `.sha256` published with it, with `/SILENT /relaunch=1`: the installer shows its progress and starts
  WinDroid again when it's done.
- **Launcher** (`ui/`): HTML/CSS in a frameless WebView2 window (pywebview).
- **Agent sessions** (`ghost.py`, `mcp.py`, `uitree.py`, `agenthelper.py` and
  `helper/src/AgentHelper.java`): the MCP server keeps one helper process running inside Android
  (`app_process`, one JSON line each way over `adb shell`). It makes each hidden app's virtual
  display (scrcpy's display flags, drawn into an `ImageReader`, which is also where screenshots come
  from: `screencap` can't capture virtual displays), and reads accessibility trees through
  `UiAutomation`, as `uiautomator` does. `uiautomator dump` takes about 2.5 s because it starts a new
  process, connects and waits a second for the screen to be idle; the helper stays connected, so a
  read is a few accessibility round trips (10 to 50 ms, under 100 ms with the secure-screen check).
  `uitree.py` drops layout-only nodes, folds a tappable row's labels into it and drops repeated
  labels: Clock's Timer keypad goes from 61 nodes to 20 elements. Taps, keys and swipes go through
  `input -d <display>`; typing uses the accessibility "set text" action, which reaches a field
  without keyboard focus and takes any language. `show_to_user` asks the agent process to open the
  app's window with `--adopt <display>`: scrcpy starting the app on the window's display makes
  Android move the app's existing task there, and `AppWindow.adopt` moves anything left behind. A
  session in a window follows its app, including onto `protected.py`'s secure display.

## Development

### Run from source

Needs Python 3.11 or later (the installer is built with 3.14) and git:

```powershell
git clone https://github.com/realxhabib/WinDroid
cd WinDroid
py -3.14 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\pythonw -m androiddesktop
```

The Python package is `androiddesktop`, WinDroid's first name. Run from source, WinDroid puts its
launcher in the Start menu itself (as **WinDroid**) and never updates itself; it shares
`%LOCALAPPDATA%\AndroidDesktop`, and so Android, with an installed WinDroid.

### Tests and tools

- `python -m unittest` runs the unit tests (no Android needed): PC checks, VM crash detection,
  parsing what Android and scrcpy report (including the fingerprint prompts the Windows Hello bridge
  reacts to and what it does about them), updates, and the shortcuts and command lines WinDroid
  starts itself with. `python -m pyflakes androiddesktop tools tests` is the lint.
- `python tools/smoke_test.py [package]` checks app windows end to end against the real Android: the
  app opens and draws, follows a resize, comes back after scrcpy is killed, and cleans up on close.
  Its windows live in its own process, so it tests your working copy even while the agent runs. It
  doesn't cover typing.
- `python tools/agent_demo.py` runs the agent demo through the MCP server, the way Claude Code
  talks to it: Clock starts hidden, gets a 10-minute timer, the time left is read back, no window
  appears on the PC until `show_to_user` puts Clock in one, timer still running.
- `ANDROIDDESKTOP_SERIAL=emulator-5620` (any adb serial) points everything at another Android, such
  as a test emulator, which then gets an agent process and app list of its own, apart from yours. An
  emulator serial's port is where WinDroid starts its Android, if it has to.
- `ANDROIDDESKTOP_DATA=<folder>` keeps everything in that folder instead of
  `%LOCALAPPDATA%\AndroidDesktop`. With both set, you get a separate WinDroid, with its own Android,
  that never touches yours: handy for trying an installed build. (The uninstaller's "delete Android"
  question then means that folder too.)
- `python tools/try_windows.py <package>...` opens app windows without the launcher.
- `python tools/snap.py "<window title>" out.png` captures one window, for checking the UI.
- `python tools/build_helper.py` rebuilds `androiddesktop/assets/appcatalog.jar` from
  `helper/src`. It needs a JDK, the Android SDK platform jar and Google's D8 in
  `%LOCALAPPDATA%\AndroidDesktop\devtools` (see the script). The built jar is committed, so
  running the app doesn't need them.
- `python tools/make_icon.py` redraws the app icon.

### Build the installer

```powershell
.venv\Scripts\pip install -r packaging\requirements.txt
winget install JRSoftware.InnoSetup
.venv\Scripts\python tools\build_installer.py
```

That makes `dist\WinDroid-Setup-<version>.exe` and its `.sha256`:

- [PyInstaller](https://pyinstaller.org) (`packaging/windroid.spec`) freezes the package, its assets
  and its libraries (pywebview and pythonnet, pywin32, Pillow) into `dist\WinDroid`: `WinDroid.exe`
  without a console and `windroid-cli.exe` with one, sharing `_internal\`. When frozen,
  `winapi.command()` makes shortcuts, taskbar pins and the MCP server's boot request run
  `WinDroid.exe` instead of `pythonw -m androiddesktop`.
- A smoke test runs `windroid-cli.exe doctor` and the MCP server in `WinDroid.exe` over pipes.
- [Inno Setup](https://jrsoftware.org/isinfo.php) (`packaging/windroid.iss`) packs it into a
  per-user installer: `%LOCALAPPDATA%\Programs\WinDroid`, a Start menu entry with the launcher's
  AppUserModelID, an Apps & features entry. Installing over an older version has it quit first
  (`WinDroid.exe quit` returns once WinDroid has exited); the uninstaller runs `WinDroid.exe uninstall`
  (quit, and remove the app shortcuts WinDroid made) and then asks about the data folder.

### Release

Set the version in `androiddesktop/__init__.py`, commit, then push a tag for it:

```powershell
git tag v0.2.0
git push origin v0.2.0
```

`.github/workflows/release.yml` checks that the tag matches the version, runs the lint and the unit
tests, builds the installer on Windows and publishes it with its `.sha256` as a GitHub Release.
Installed copies find it within a day (`androiddesktop/update.py`). Run the workflow by hand to
build an installer without publishing it.

## Known limitations

- Video is encoded in software inside the VM, so very large windows of fast-moving games cost CPU.
- Android always cold-boots (about 30 s). Quick-boot snapshots would be faster, but restoring them
  headless proved unreliable.
- The camera and microphone are the PC's: the first webcam the emulator lists, and the default
  microphone. A webcam plugged in or out is noticed at the next start of Android, not while it runs.
- After a start, unlocking from the launcher (PIN or Windows Hello) unlocks Android's storage, but
  Android's own lock screen stays up on its main display, which no window shows. Android then
  treats the PIN as not entered yet. So Android's own "confirm it's you" screens (Settings) ask for
  the PIN instead of a fingerprint, and the lock screen never asks for a fingerprint. Apps' own
  fingerprint prompts (BiometricPrompt) aren't affected; Google's passkeys and Play purchases
  haven't been tried yet.
- Android draws an app's fingerprint dialog behind that lock screen, so it's never shown and can't be
  canceled from the PC. If you cancel Windows Hello, the app keeps waiting until it gives up or another
  app comes to the front (Android then cancels it), or until it asks again.
- Notifications stay inside Android for now.
- AI agents: moving an app from its hidden display into a window changes its screen size, so Android
  recreates the app's screen, as when a phone rotates. Well-made apps keep their state (Clock's
  timer runs on); an app that doesn't may lose what's on screen, such as half-typed text.
- AI agents: only one `UiAutomation` can be connected in Android at a time. While an agent reads
  screens, `uiautomator dump` (and a second MCP server's reads) fail; the helper lets go after 30
  seconds without a read. `home` does nothing on a hidden display, which has no home screen.
  Screenshots are for hidden apps; for an app in your window the agent gets the tree only.
- Protected (`FLAG_SECURE`) screens update a few times a second instead of at full speed, and typing
  on them is limited to plain ASCII text. If one can't be moved to a secure display, the window says
  so (close it and reopen the app to leave that screen); it never just goes black.
- The installer isn't code-signed, so SmartScreen warns about it (see Install).
