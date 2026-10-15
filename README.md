# Android Desktop

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
- **Sound** plays through your PC.

Everything runs locally.

## Requirements

- Windows 10/11 (64-bit) with hardware virtualization and the **Windows Hypervisor Platform**
  feature (on by default when Hyper-V, WSL2 or Windows Sandbox are in use).
- Python 3.11+.
- About 7 GB of disk (more as you install apps), 2.4 GB of it downloaded once on first run.
- 8 GB of RAM or more; Android uses about 5 GB while running.

## Run it

```powershell
py -3.14 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\pythonw -m androiddesktop
```

The first start shows **Set up Android**, which downloads Google's Android Emulator, an Android 16
(Google Play) system image, adb and [scrcpy](https://github.com/Genymobile/scrcpy). All are
checksum-verified and stored in `%LOCALAPPDATA%\AndroidDesktop`. After that, the launcher adds
itself to the Start menu as **Android Desktop**.

Android takes about 30 seconds to start; the very first boot takes a few minutes while Android
sets itself up. Sign in to Google Play from **Get apps** to install apps.

### Command line

| Command | |
|---|---|
| `pythonw -m androiddesktop` | Open the launcher (starts Android if needed) |
| `pythonw -m androiddesktop open <package>` | Open one app in its own window, e.g. `com.android.chrome` |
| `pythonw -m androiddesktop stop` | Close every app window and shut Android down |
| `pythonw -m androiddesktop quit` | ...and exit Android Desktop |
| `python -m androiddesktop setup` | Download and install the Android runtime from a console |

A tray icon (bottom right of the taskbar) opens the launcher and has **Shut down Android** and
**Quit**. Closing the launcher keeps your app windows running. When no app windows are open and the
launcher is closed, Android shuts itself down after 10 minutes.

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
  (e.g. Back on its first screen), its window closes too.
- **App catalog** (`catalog.py` and `helper/src/AppCatalog.java`): a small helper, run inside
  Android through `app_process` the way scrcpy runs its server, asks PackageManager for the
  launchable apps and renders their real icons, including adaptive icons with a rounded-square mask.
- **Agent** (`app.py`, `manager.py`): one process owns the VM, the windows, the tray icon and the
  launcher. Start menu shortcuts send their request to the running agent via `WM_COPYDATA`.
- **Launcher** (`ui/`): HTML/CSS in a frameless WebView2 window (pywebview).

Logs: `%LOCALAPPDATA%\AndroidDesktop\logs` (`androiddesktop.log`, `emulator.log`, and one file per
app window under `windows\`).

## Development

- `python tools/try_windows.py <package>...` opens app windows without the launcher.
- `python tools/snap.py "<window title>" out.png` captures one window, for checking the UI.
- `python tools/build_helper.py` rebuilds `androiddesktop/assets/appcatalog.jar` from
  `helper/src`. It needs a JDK, the Android SDK platform jar and Google's D8 in
  `%LOCALAPPDATA%\AndroidDesktop\devtools` (see the script). The built jar is committed, so
  running the app doesn't need them.
- `python tools/make_icon.py` redraws the app icon.

## Known limitations

- Video is encoded in software inside the VM, so very large windows of fast-moving games cost CPU.
- Android always cold-boots (about 30 s). Quick-boot snapshots would be faster, but restoring them
  headless proved unreliable.
- No camera (the AVD has none configured); the microphone is the PC's default.
- Notifications stay inside Android for now.
