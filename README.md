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
- **Sound** plays through your PC, and **the camera** is your PC's webcam.
- **Windows Hello for Android's fingerprint.** When an Android app asks for your fingerprint,
  Windows Hello asks instead, above that app's window: face, fingerprint reader or Windows PIN. Set it up once with **Set up fingerprint** in the launcher.
- **Recovers by itself.** If an app's connection drops, or Android itself crashes, its window shows
  "Reconnecting..." and brings the app back (starting Android again if needed) instead of vanishing.

Everything runs locally.

## Requirements

- Windows 10/11 (64-bit) with hardware virtualization and the **Windows Hypervisor Platform**
  feature (on by default when Hyper-V, WSL2 or Windows Sandbox are in use).
- Python 3.11+.
- About 7 GB of disk (more as you install apps), 2.4 GB of it downloaded once on first run.
- 8 GB of RAM or more; Android uses about 5 GB while running.

Android Desktop checks these itself, before setup and before every start of Android, and shows what to
fix in the launcher (for example a **Turn it on** button for Windows Hypervisor Platform). To see the
checks from a console: `python -m androiddesktop doctor`.

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

If you set a screen-lock PIN or password in Android (Google Play may ask for one), Android starts
**locked** after every boot, like a phone after a restart: your apps' data stays encrypted until the
PIN is entered. The launcher then asks for it, and apps you open wait with an **Unlock** button. Wrong
tries count towards Android's own lockout. To stop this, remove the screen lock in Android's
**Settings > Security > Screen lock**.

To skip typing the PIN, tick **Unlock with Windows Hello from now on** under the PIN field (shown when
Windows Hello is set up on the PC). Android Desktop then keeps the PIN, encrypted with DPAPI for your
Windows account (`%LOCALAPPDATA%\AndroidDesktop\android-pin.bin`), and after each start of Android
asks Windows Hello instead, above the launcher or the app window that's waiting. Untick the box
the next time Android asks for its PIN to forget it. If the PIN changes in Android, it's forgotten
and asked for again. DPAPI keeps it from other Windows users and from copies of the file, not from
programs running as you; Windows Hello is what stands between them and Android. The PIN never goes
into a log.

### Windows Hello for Android's fingerprint

Android here has a (virtual) fingerprint sensor. Click **Set up fingerprint** in the launcher (shown
until a fingerprint is added, if Windows Hello is set up on the PC): Android's Settings opens at
**Add fingerprint**, asks for your Android PIN, and Android Desktop touches the sensor each time
Android asks. From then on, whenever Android waits for a fingerprint, Windows Hello asks you instead,
above the window of the app that's asking, and touches the sensor once it's you. Canceling Windows
Hello leaves the finger off.

### Command line

| Command | |
|---|---|
| `pythonw -m androiddesktop` | Open the launcher (starts Android if needed) |
| `pythonw -m androiddesktop open <package>` | Open one app in its own window, e.g. `com.android.chrome` |
| `pythonw -m androiddesktop stop` | Close every app window and shut Android down |
| `pythonw -m androiddesktop quit` | ...and exit Android Desktop |
| `python -m androiddesktop setup` | Download and install the Android runtime from a console |
| `python -m androiddesktop doctor` | Check whether this PC can run Android, and print what's wrong |

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
- **Launcher** (`ui/`): HTML/CSS in a frameless WebView2 window (pywebview).

Logs: `%LOCALAPPDATA%\AndroidDesktop\logs` (`androiddesktop.log`, `emulator.log`, and one file per
app window under `windows\`). Uncaught errors from any thread are logged too, with their traceback.

## Development

- `python -m unittest` runs the unit tests (no Android needed): PC checks, VM crash detection, and
  parsing what Android and scrcpy report, including the fingerprint prompts the Windows Hello bridge
  reacts to and what it does about them.
- `python tools/smoke_test.py [package]` checks app windows end to end against the real Android: the
  app opens and draws, follows a resize, comes back after scrcpy is killed, and cleans up on close.
  Its windows live in its own process, so it tests your working copy even while the agent runs. It
  doesn't cover typing.
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
- Protected (`FLAG_SECURE`) screens update a few times a second instead of at full speed, and typing
  on them is limited to plain ASCII text. If one can't be moved to a secure display, the window says
  so (close it and reopen the app to leave that screen); it never just goes black.
