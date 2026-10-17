# WinDroid

Android apps on Windows, each in its own window. Android runs as a headless virtual machine on your
PC (no phone needed); every app you open gets a separate, frameless, resizable window that you can
drag, snap, pin to the taskbar or maximize like any other Windows app.

## What it does

- **One window per app.** Every app runs on its own Android virtual display, shown in its own
  Windows window. Resize the window and the app re-lays itself out for the new size.
- **Sleek, frameless windows.** No Windows title bar: a slim strip that takes on the app's own top
  color, with Back, the app's icon and name, **...** (more), Keep on top, Minimize, Maximize and
  Close. The native frame is kept underneath, so rounded corners, shadows, Aero Snap and the
  Windows 11 Snap Layouts flyout (hover Maximize) all work.
- **Everything for an app, from its own window.** The bar's **...** button: Keep on top, **Pin to
  Start**, **Add a desktop shortcut**, Android's **App info**, **Uninstall**, and the launcher's
  settings.
- **Real Windows citizens.** Each app has its own taskbar button and Alt+Tab entry with its real
  icon. **Pin to Start** (from the app's **...** menu, or right-click it in the launcher) puts it in
  the Start menu; from there you can also pin it to the taskbar.
- **Launcher.** A Start-menu-style window listing every Android app with its real icon, plus search,
  **Get apps** (Play Store) and **Install APK**.
- **Settings for every feature.** The launcher's gear opens Android Desktop's own settings: a card
  per feature with how it stands right now and a button for what it does (Android, Security,
  Camera, AI agents, Performance, About; Plugins, iPhone & iPad and updates where this version has
  them). **Android settings** at the top opens Android's own Settings app. Also in the tray menu,
  and `pythonw -m androiddesktop settings`.
- **Ctrl+K finds anything.** A command palette over every app and action: type a few letters of
  "set up fingerprint", "connect claude", "run checks", "restart", "quit"... and press Enter.
  **Ctrl+,** opens settings.
- **What's new.** After the first run and after each update, a short card lists the headline
  features with **Try it** buttons (dismiss it with the x; it's back under Settings > About).
- **Google Play.** Android 16 with the Play Store, so you can install apps the usual way.
- **PC keyboard and mouse.** Android sees a hardware keyboard, so the on-screen keyboard stays out of
  the way. Right-click (or your mouse's back button) goes Back; Shift+right-click is a real
  right-click. Clipboard is shared both ways.
- **Sound** plays through your PC, and **the camera** is your PC's webcam: Settings > Camera says
  which one Android uses (and, where this version has it, installs the virtual camera that shows
  Android the webcam's whole picture).
- **Windows Hello for Android.** When an Android app asks for your fingerprint, Windows Hello asks
  instead, above that app's window: face, fingerprint reader or Windows PIN. After each start,
  Windows Hello can unlock Android too. Settings > Security: **Set up fingerprint**, **Unlock with
  Windows Hello** on or off, and Android's screen lock.
- **Recovers by itself.** If an app's connection drops, or Android itself crashes, its window shows
  "Reconnecting..." and brings the app back (starting Android again if needed) instead of vanishing.
- **Checks itself.** Whether this PC can run Android (virtualization, memory, disk), with a fix
  where there is one: Settings > Android > **Run checks**, or `python -m androiddesktop doctor`.
- **Android apps for AI agents.** An MCP server lets Claude (or any MCP client) run Android apps
  hidden, read and operate them, and hand one to you in a normal window when you should see it.
  Settings > AI agents: **Connect Claude** (Claude Code), **Copy setup** (Claude Desktop) and the
  agents' recent activity.
- **Performance.** Settings > Performance: how app windows are drawn right now (smooth, straight from
  the emulator, or video through scrcpy), and the video quality.
- **Keeps itself up to date.** The launcher offers each new version, and installs it when you say so.
- **On your iPhone or iPad.** Send one app (not the whole of Android) to your iPhone or iPad over
  your home Wi-Fi. It runs full screen in Safari, by touch, sized to the device and following its
  rotation, and comes back to its window when you like.
- **Plugins.** Companions that attach to a particular app's window: a side panel glued to its edge
  and pop-ups on the app itself. The first one, **Hinge Matcher**, suggests which card to like and
  writes openers and replies with Claude, and fills them in for you to send.

Everything runs locally.

## Requirements

- Windows 10 (version 1809 or later) or Windows 11, 64-bit, with hardware virtualization and the
  **Windows Hypervisor Platform** feature (on by default when Hyper-V, WSL2 or Windows Sandbox are in
  use).
- About 7 GB of disk (more as you install apps), 2.4 GB of it downloaded once on first run.
- 8 GB of RAM or more. Android gets 8 GB of it on a PC with 24 GB or more (6 GB from 14 GB, else
  4 GB), and up to 6 processor cores (half the PC's threads, at least 4).

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

Two settings, in Settings > Performance (kept in `%LOCALAPPDATA%\AndroidDesktop\state.json`; they
apply to windows opened from then on):

- `"app_windows": "scrcpy"` shows every app through scrcpy, as before; the default (`"auto"`) uses
  the emulator's own displays for up to seven windows (see How it works).
- `"scrcpy_quality"`: `"high"` (default: the window's full size, 12 Mbit/s, 60 fps), `"balanced"`
  (75%, 6 Mbit/s) or `"low"` (half size, 3 Mbit/s, 30 fps), for windows shown through scrcpy. Lower
  costs Android less CPU and looks softer.

If you set a screen-lock PIN or password in Android (Google Play may ask for one), Android starts
**locked** after every boot, like a phone after a restart: your apps' data stays encrypted until the
PIN is entered. The launcher then asks for it, and apps you open wait with an **Unlock** button. Wrong
tries count towards Android's own lockout. Once Android accepts the PIN, Android Desktop also enters
it on Android's own lock screen, which no window shows, so Android counts as fully unlocked, as a
phone would after you unlock it. To stop all this, remove the screen lock in Android's
**Settings > Security > Screen lock**.

To skip typing the PIN, tick **Unlock with Windows Hello from now on** under the PIN field (shown when
Windows Hello is set up on the PC), or turn on **Unlock with Windows Hello** in Settings > Security
(it asks for the PIN once and has Android check it). WinDroid then keeps the PIN, encrypted with DPAPI for your
Windows account (`%LOCALAPPDATA%\AndroidDesktop\android-pin.bin`), and after each start of Android
asks Windows Hello instead, above the launcher or the app window that's waiting. Untick the box
the next time Android asks for its PIN to forget it. If the PIN changes in Android, it's forgotten
and asked for again. DPAPI keeps it from other Windows users and from copies of the file, not from
programs running as you; Windows Hello is what stands between them and Android. The PIN never goes
into a log.

### Windows Hello for Android's fingerprint

Android here has a (virtual) fingerprint sensor. Click **Set up fingerprint** in Settings > Security,
in the palette (Ctrl+K) or on the launcher's card (shown
until a fingerprint is added, if Windows Hello is set up on the PC): Android's Settings opens at
**Add fingerprint**, asks for your Android PIN, and WinDroid touches the sensor each time
Android asks. From then on, whenever Android waits for a fingerprint, Windows Hello asks you instead,
above the window of the app that's asking, and touches the sensor once it's you. Canceling Windows
Hello leaves the finger off and cancels Android's prompt, so the app hears "canceled".

### Webcam

Android uses your PC's webcam as its front camera. On its own, though, Android sees only a tall
slice from the left of a widescreen webcam's picture (see Known limitations). To give it the whole
picture, install the **Android Desktop Camera** once (Windows 11; Windows asks for administrator
rights):

```powershell
.venv\Scripts\python -m androiddesktop camera install
```

From the next start of Android, apps show the webcam's whole picture, upright, with black bars around
it: above and below in videos and video calls, and also on one side in photos. The Android Desktop
Camera is a Windows virtual camera that turns on your first webcam only while something uses it.
Other Windows apps list it too, but its picture is laid out for Android. `camera uninstall` removes
it.

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
| `windroid-cli.exe camera` | Is the Android Desktop Camera installed? `camera install` / `camera uninstall` |
| `windroid-cli.exe camera test on` | The Android Desktop Camera shows a test pattern instead of the webcam (`off` to stop) |

From a source checkout (see Development), `pythonw -m androiddesktop` takes the place of
`WinDroid.exe` and `python -m androiddesktop` that of `windroid-cli.exe`.

A tray icon (bottom right of the taskbar) opens the launcher and has **Settings**, **Shut down
Android** and **Quit**. Closing the launcher keeps your app windows running. When no app windows are open, the
launcher is closed and no AI agent has an app running, Android shuts itself down after 10 minutes.

### Android apps for AI agents (MCP)

WinDroid is also an MCP server, so an AI agent such as Claude can use Android apps for you.
By default the agent's apps run **hidden**: each on an Android display of its own with no window, so
nothing on your screen changes while it works. The agent reads an app as a compact accessibility
tree (about 2 KB of JSON for a typical screen, read in under 100 ms), taps, types and scrolls by
element id, and can add a screenshot when the tree isn't enough. When you should see something, or
take over, the agent calls `show_to_user` and the app moves into a normal WinDroid window,
exactly as the agent left it.

The quickest way: **Connect Claude** in Settings > AI agents (or Ctrl+K, "connect claude") runs the
command below for you, and **Copy setup** puts Claude Desktop's config on the clipboard.

By hand, add it to **Claude Code** (from a source checkout, use the Python of its venv;
`--scope user` makes it available in every project):

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

### Your iPhone or iPad

Send an app to your iPhone or iPad on the same Wi-Fi, where it runs full screen in Safari: only that
app. Android gives it a display of its own, the size and shape of the device's screen (an app sees as
many dp as the device has points), and resizes it when you rotate the device.

**Once:**

1. In the launcher, click **iPhone & iPad**, then **Turn on**. WinDroid listens only on your home
   network (one Windows calls *Private*), on this PC's own address there, never on the internet.
   Windows asks for administrator rights once, when you click **Allow**, to add one firewall rule:
   this program, port 47821, devices on your local subnet, Private networks only.
2. Click **Pair an iPhone or iPad** and scan the QR code with the device's Camera, then tap the
   link. The code works once, for 5 minutes. Paired devices are listed there; the bin icon unpairs one.
3. For smooth video, let the device trust this PC, once. The page shows how: **Get the profile**,
   then in Settings **Profile Downloaded > Install**, then **General > About > Certificate Trust
   Settings**, turn on **WinDroid on *your PC***. Come back to Safari and the page switches to
   https:// by itself. Until then the PC decodes the video and sends JPEG frames: it works, less smoothly.

**Then:**

- Send an app with the phone button in its window's bar (**Move to iPhone**, or **Show on iPhone too**
  to have both show the same screen, letterboxed), or right-click it in the launcher. Or open
  WinDroid on the device and tap an app: it moves there.
- The window then says the app is on your iPhone, with **Bring back**; so does the device's **⋯**
  menu. Closing that window leaves the app on the device.
- Tap, drag, long-press and pinch as on Android. Back is **◁** or a swipe in from the screen's left
  or right edge; **○** lists your apps; **⌨** opens the device's keyboard (Android's own keyboard also
  shows up when a field is focused). An iPad's keyboard types straight in, with Cmd as Ctrl.
- **Add to Home Screen** (Share) gives each app its own icon and name. A new icon may ask to be
  allowed once: the launcher shows a 6-digit code, allow it if the device shows the same. Add icons
  from the https:// page (after step 3) to get the smooth video in them too.
- It reconnects by itself when the Wi-Fi drops or the device sleeps. **⋯ > Picture** trades
  sharpness for frame rate (Smooth, Balanced, Sharp). The chip at the top says which decoder is in
  use (WebCodecs or JPEG), the frame rate and the latency from the PC.

**Security.** Nothing works until a device is paired. Pairing combines the QR code's one-time token
(in the link's #fragment, which browsers never send) with an X25519 key exchange, so the device's
secret never crosses the Wi-Fi, not even before step 3. Every connection then proves itself with that
secret, in both directions, and every touch and keystroke is signed. Without the token (a new Home
Screen icon), you approve the device on the PC after comparing a code derived from the key exchange.
App pages and icons live at unguessable addresses, so the network can't learn which apps you have.
The certificate authority your device trusts is limited to private network addresses: it can't vouch
for any website, and its key stays on the PC, encrypted for your Windows account. Before step 3 the
video itself travels unencrypted on your Wi-Fi.

### Plugins: Hinge Matcher

Open Hinge in its window and a dock appears glued to the window's side. On **Discover**, within
about ten seconds of a profile opening, it shows who they seem to be, which card Claude would like
and why, and three openers (witty, funny, cute), from the part of the profile on screen. Meanwhile it
scrolls through the whole profile once, reading every card, and back to the top; the ideas are then
written again from all of it (unless you've started using the first ones). After that, scrolling
changes nothing: the ideas only change when you ask. On Hinge itself, the card it picked glows, with
the current idea in a bubble on it, against its right side and clear of their words and the heart as
far as the card allows; both follow the card as you scroll. Point at the bubble and it fades to let
you read what's under it; the eye in the dock (or **Alt+Q** on the Hinge window) hides it altogether,
and that sticks. In a **chat** you get three reply ideas instead, written for where the conversation
stands; there the bubble goes in an empty stretch above the message box, off the messages (with no
room, over the oldest ones on screen). A message too short for the whole-screen read ("Hey") gets its
bubble read on its own, and Hinge's notes ("You liked their photo", "Double tap to like") count as how
the chat started, not as messages.

- **It fills in, you tap.** Tap ♥ on a card in Hinge so the comment box opens, then click an idea
  in the dock (or press **Alt+Enter** on the Hinge window): it goes into the box exactly as written,
  and the plugin checks the box shows it. Read it over, then press **Send like** yourself. Clicked an
  idea before opening the box? It waits, and goes in as soon as the box opens. In a chat, it goes into
  the message box. The plugin never presses Like, Send or Skip, and never sends anything.
- **Go to a card.** Click a card in the dock (Photo 2, Prompt 1…, numbered as on the page), an
  idea's "on prompt …", or the pick's "Like their …", and Hinge scrolls to it: steered by which cards
  are on screen after each move, with the same margin drags as the read. Photo 1 is the top.
- **It remembers.** Resizing or moving the window keeps everything: what was read, the ideas, where
  you were. The card positions are scaled to the new width and put right as each card shows again,
  so nothing is read or written twice. A profile seen in the last half hour that shows again (back
  from a chat) gets its ideas back, unless its cards turn out to say something else.
- **Shape the ideas.** Hover an idea to see it on its card. **Alt+←/→** on the Hinge window cycles
  them. The pencil edits one before it goes in. The slider leans playful or sincere, **Shorter** /
  **Longer** sets the length, and **New ideas** writes three more, following anything you type in
  "Tell Claude…" ("mention I cook"). **Skip** notes the pass in the history; you tap ✕ in Hinge.
- **Unnoticed.** It reads Hinge from screenshots of its window, taken on the PC, and Windows' own
  OCR: nothing runs inside Android, and the accessibility service stays off. It works in both kinds
  of window, smooth (the emulator's own displays) and scrcpy's. An idea goes in through the window's
  own keyboard, the one yours uses: pasted (put on Android's clipboard, then Ctrl+V), which keeps it
  exact. Typed key by key, Android's keyboard app can auto-correct and capitalise depending on timing
  ("can i" became "Can I", and keys went missing), so typing is only an option: `"insert": "type"` in
  the config. Your clipboard is put back afterwards. Reading
  the whole profile is a finger dragging the page up a little under half a screen at a time, in the
  page's left margin where no card, video or button can take the touch, and a few flicks back to the
  top: the touches a mouse drag in the window makes. It stops if you scroll, tap ♥ or move on; a
  passing frame of something else doesn't stop it, and drags an overloaded emulator drops aren't
  taken for the end of the profile. In a chat, the box gets a tap (a touch on Hinge's display: it
  doesn't take a posted click) before the paste.
- **Your voice.** Settings come from Hinge Matcher's `config.json` (`%USERPROFILE%\hinge-matcher`)
  when it's there, so the iPhone version and this one share your profile (`me`), voice and model;
  otherwise from `%LOCALAPPDATA%\AndroidDesktop\plugins\hinge\config.json`. The history (what was
  filled in, sent or skipped) is shared too, so openers don't repeat. `backend` picks how Claude is
  reached: `claude-cli` (your Claude login, no key), `api` (needs the `anthropic` package and an API
  key) or `auto`. `android_effort` (default `low`, about 7 s a draft) is how hard Claude thinks
  here; the shared `effort` stays the iPhone version's. The config is never logged.
- **Off switch.** `"plugins": {"hinge": false}` in `%LOCALAPPDATA%\AndroidDesktop\state.json`;
  `"read_whole_profile": false` in the config to read only what you scroll past yourself.
  `ANDROIDDESKTOP_PLUGIN_DEBUG=1` logs what the reader sees and does to `logs\hinge-debug.log`
  (card ids and names, never what the cards say).

Logs: `%LOCALAPPDATA%\AndroidDesktop\logs` (`androiddesktop.log`, `emulator.log`, `agent.log` for AI
agents, and one file per app window under `windows\`). Uncaught errors from any thread are logged
too, with their traceback.

## How it works

```
Launcher (WebView2)      App window (Win32, frameless)            App window (8th and on) ...
        \                          |  hosts                              |  hosts
         \                 Direct3D view <-- gRPC frames,     scrcpy video window -- adb --
          \                        |         shared memory           virtual display N
           \________ agent process ________/        |                       |
                         |                emulator display 1..7     Android 16 VM (Android
                    adb / emulator console / gRPC              Emulator, window hidden, WHPX)
```

- **The VM** (`vm.py`): Google's Android Emulator with its window hidden (`-qt-hide-window`),
  accelerated by the Windows Hypervisor Platform, with its gRPC control API on (`-grpc`, guarded by
  the console's auth token). Android's own screen is never shown. Before every start `runtime.py`
  sizes it for the PC (cores, memory) and drops any extra displays the emulator saved at its last exit.
- **App windows** (`appwindow.py`, `emuview.py`): each app gets an Android display of its own, sized
  like the window, in a custom Win32 window that draws the slim bar (`WM_NCCALCSIZE` removes the
  caption, `WM_NCHITTEST` maps the bar to caption, resize edges and `HTMAXBUTTON` for Snap Layouts).
  The bar's color is sampled from the top of the app's image. Resizing the window resizes the
  display (and the app re-lays itself out); moving it to a monitor with another scale changes the
  display's density.
  - Up to seven windows use the **emulator's own displays** (`adb emu multidisplay add`, slots 1 to 8,
    one kept for protected screens; `emudisplays.py`). The emulator composes them on the PC's GPU
    anyway, so their frames come straight from its gRPC API (`emugrpc.py`, a small HTTP/2 + protobuf
    client with no dependencies: `streamScreenshot`, written into shared memory) and a Direct3D 11
    child window (`d3d.py`) copies each one into its back buffer, 1:1. Nothing is video-encoded:
    scrolling Settings costs Android about 60% of a core instead of 285% (`media.swcodec` alone was
    140%), at a steady 59 fps instead of 35, and input reaches the screen a frame sooner. The emulator
    wakes every stream whenever any display draws, so only the window in use and windows whose app is
    drawing stream; the others look for a change three times a second, and minimized ones not at all.
    The left mouse button is a finger on that display's touchscreen (gRPC); keys, typed text, the
    wheel and the pointer's hover go through `helper/src/InputHelper.java`, which injects them with
    the display's id, as `input -d` does without a process per key. These displays are secure, so `FLAG_SECURE` screens
    (Play's purchase sheet, PIN pads) work in them at full speed. They don't rotate with their app: an
    app that wants another orientation shows upright, letterboxed, until the window is shaped to suit.
    A windowless emulator streams extra displays at the wrong size, which is why the emulator keeps
    its (hidden) window.
  - Beyond that, or without the emulator's gRPC (an Android started by an older version), windows use
    [scrcpy 5](https://github.com/Genymobile/scrcpy) (`--new-display --flex-display --start-app`):
    a virtual display encoded to H.264 inside Android, its borderless video window re-parented into
    ours.

  When an app exits (e.g. Back on its first screen), its window closes too. If its display goes away
  on its own (scrcpy exited, or Android did), the window reconnects: it starts the app on a fresh
  display, backing off 1, 2, 4, 8 and 15 seconds, then offers **Try again**.
- **Protected screens** (`protected.py`, scrcpy windows only): some screens are marked secure (`FLAG_SECURE`): Play's
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
  It looks for secure windows when Android's event log says a window got focus or an activity came
  to the front, and every 15 seconds besides, only while a scrcpy window is open.
- **Crash recovery** (`vm.py`): the VM notices an emulator that died (its process ended, or adb lost
  it for good) and marks Android as off; the next window that reconnects boots it again.
- **Locked Android** (`vm.py`, `adb.py`): after boot, `sys.user.0.ce_available` and
  `am get-started-user-state 0` tell whether the user is still locked ("direct boot"). Locked, the VM
  reports `locked`, the app list isn't refreshed (Android lists only its direct-boot apps then), and
  the PIN from the launcher goes to `locksettings verify`, which unlocks the user's storage. That
  leaves Android's lock screen (keyguard) up on display 0 with "strong auth required after boot"
  (`dumpsys trust`: `deviceLocked=1`, `strongAuthRequired=0x1`), which the shell can't clear: Android
  would go on refusing fingerprints in its own "confirm it's you" screens and keep apps'
  fingerprint dialogs behind its lock screen. So `wm dismiss-keyguard` brings up the keyguard's PIN
  pad, and once display 0's keyboard focus is on it (`dumpsys window displays`: `NotificationShade`;
  SystemUI's dump: bouncer showing, with a PIN or password view, never a SIM PIN pad), the PIN goes
  there with `input -d 0 text` and Enter, once. This happens before the VM reports `running`, while
  no app display can take the focus from display 0.
- **Camera** (`runtime.py`): before every start of Android, `emulator -webcam-list` tells which
  webcams are plugged in, and Android's front camera (`hw.camera.front` in the AVD's `config.ini`) is
  set to the first one by label (`webcam0`), or to `none` without one. There's no back camera, as on
  a Chromebook: giving both the same webcam makes the emulator offer only a back one (video calls
  look for the front), and a second webcam is often a closed laptop's, which only shows black.
  When the **Android Desktop Camera** is listed (and a real webcam is too), it's the one.
- **Android Desktop Camera** (`camera.py`, `helper/camera/AndroidDesktopCamera.cpp`): Android's camera
  driver (in Google's system image) always presents a phone camera, a 1280×960 sensor mounted at 90°,
  so the emulator asks the webcam for 1280×960, turns each frame as a phone held upright would, which
  keeps only a 720×960 strip from its left edge (`convert_frame_fast` in the emulator), and Android
  turns that strip back upright. For a 16:9 stream (video recording, most video calls) Android's
  camera driver then keeps only the strip's left three quarters, 540×960 (the recordings show it
  too). The Android Desktop Camera is a Windows 11 virtual camera (`MFCreateVirtualCamera`, a Media
  Foundation media source that Windows' Frame Server service loads) that offers exactly 1280×960
  NV12 and draws the first physical webcam (read at its native size nearest 1280×720, 30 fps) into
  that 540×960 area, scaled to fit, so photos and videos both get the whole picture. Frames go out
  at a steady 30 fps from the start, the last picture repeated while the webcam is late, so the
  emulator never has to make up an empty frame. Installing it copies the DLL to
  `%ProgramFiles%\Android Desktop`, where the service can read it, and registers it for all users
  (`DllRegisterServer`, which also creates the camera; each build gets its own file name, so an
  update never has to replace a DLL the service still holds; the service lets go of the old one
  about 40 seconds after the last app stops using the camera). It logs to
  `%ProgramData%\AndroidDesktop\camera-LOCAL-SERVICE.log`.
- **Windows Hello** (`biometrics.py`, `hello.py`): the emulator's console puts a finger on Android's
  virtual sensor (`adb emu finger touch 1`). Android's `BiometricScheduler` runs one client at a time,
  shown in `dumpsys fingerprint` as its "Current operation" with the package that asked (an
  authentication for a BiometricPrompt, the lock screen, Play or a passkey; an enrollment for
  Settings). The scheduler logs each operation as it starts and finishes; logcat, filtered to its
  tags inside Android, says when to look again (and nothing else: apps ask "can I authenticate?"
  constantly). When an
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
- **One data folder for everyone** (`datadir.py`). Packaged Windows apps, Claude Desktop among them,
  get their new files in `%LOCALAPPDATA%` redirected into a private copy only they can see. If
  WinDroid's first run happened there (Claude Desktop starting the MCP server, say), the
  runtime would land in Claude's private storage and a Start menu launch would offer setup again.
  So every entry point first makes sure `%LOCALAPPDATA%\AndroidDesktop` exists for real, creating it
  through Explorer when redirected, and moves any private copy into it. Inside a folder that exists
  for real, those apps' writes go to the real folder.
  The updater reads `api.github.com/repos/realxhabib/WinDroid/releases/latest` (at most once a day,
  remembered in `state.json`), and runs a downloaded installer only if its SHA-256 matches the
  `.sha256` published with it, with `/SILENT /relaunch=1`: the installer shows its progress and starts
  WinDroid again when it's done.
- **Launcher** (`ui/`): HTML/CSS in a frameless WebView2 window (pywebview).
- **Plugins** (`plugins/__init__.py`, `overlay.py`, `panel.py`, `keys.py`, `winocr.py`): a plugin names the
  packages it's for; when one of them shows in its window, the host makes an instance and hands it
  the window: its geometry (where Android's display is drawn, in screen pixels, updated on every move,
  resize, DPI change and minimize), screenshots of the view, text input, clicks and drags. Overlays
  are layered windows (per-pixel alpha, click-through) and side panels are WebView2 windows, both
  **owned** by the app window, so Windows keeps them above it, hides them while it's minimized and
  destroys them with it. Input goes only to the app's own display, so the window needn't be in front
  and nothing can land in another window. On an emulator display the screenshot is the frame the
  window is showing (Direct3D draws it, so `PrintWindow` sees nothing); text goes to InputHelper with
  the display's id, as the view's own keyboard does, and a paste puts the text on Android's clipboard
  over gRPC before Ctrl+V; a click is a touch on the display; a tap, drag or fling makes the display
  stream while the page moves. In a scrcpy window the screenshot is `PrintWindow`, as the bar's color
  sampling does, and text and clicks are key and mouse messages posted to scrcpy's window, which
  hands them to Android as its UHID keyboard and its mouse. Text is pasted (the PC's clipboard, then
  Ctrl+V: scrcpy copies the clipboard to Android before passing it on); typed key by key, Gboard
  rewrites it depending on timing, and SDL lets go of a posted Shift after each batch of messages
  unless Windows says the key is down. Real keystrokes (`SendInput`) lost characters on their way
  through Windows when tested. Windows OCR is `Windows.Media.Ocr`
  called with plain ctypes, one recognition at a time (the engine fails a second one meanwhile).
- **Hinge Matcher** (`plugins/hinge/`): a capture loop watches the view several times a second. Between
  frames it measures how far the content scrolled (row-by-row comparison of small greyscale copies,
  then to the pixel on the full frames), which keeps the pop-ups on their card. When the picture
  holds still it OCRs it and tells the screens apart (a profile: cards on the page; the like sheet:
  "Add a comment" and "Send like"; a chat: the Chat / Profile tabs and "Send a message"). Cards are
  found from the page color down their edges, a prompt's question and answer by type size, and each
  card gets a place on the page (view position plus the scroll so far), so the profile adds up as
  you scroll. A scroll is measured on the rows with detail, by a low percentile of how far each row
  is off rather than the average, so a playing video or the floating X doesn't outvote the page.
  Reading the whole profile drags it (`input touchscreen motionevent`, over one `adb shell` kept
  open: a finger follows 1:1 and, held still before lifting, leaves no fling; Hinge treats quick
  wheel turns as acceleration, so the wheel's steps vary from 6 to 500 px). Each step is measured on
  the still pictures before and after it, and Hinge can take half a second to start moving while a
  video plays. The name is read again without the purple verified badge, which OCR takes for a
  letter, and the reading that comes up most often wins. `writer.py` (from Hinge Matcher) turns it
  into a draft with Claude.
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
  app opens and draws, follows a resize, comes back after its display goes away (and, on an emulator
  display, after its frame stream drops), and cleans up on close. `--scrcpy` checks the scrcpy
  fallback. Its windows live in its own process, so it tests your working copy even while the agent
  runs. It doesn't cover typing. With `ANDROIDDESKTOP_SERIAL` set it never boots an Android: start the
  test emulator first.
- `python tools/plugin_check.py` checks the plugin system against the real Android, on Settings: the
  plugin attaches, screenshots and OCR work, an overlay and a side panel follow the window and hide
  with it, and text reaches Android exactly, pasted and typed. It uses no real mouse or keyboard input.
  It runs on an emulator display when Android allows; `--scrcpy` checks a scrcpy window instead.
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
- `ANDROIDDESKTOP_REMOTE_LOOPBACK=1` also serves the iPhone & iPad pages on http://127.0.0.1:47821,
  which browsers treat as secure, so the web app (`androiddesktop/remote/web`) can be tried with
  WebCodecs in a desktop browser without a certificate. The unit tests include the web app's crypto
  (`tests/js/wdcrypto.test.js`, run through Node.js when it's installed).
- `python tools/try_windows.py <package>...` opens app windows without the launcher.
- `python tools/snap.py "<window title>" out.png` captures one window, for checking the UI.
- `python tools/build_helper.py` rebuilds `androiddesktop/assets/appcatalog.jar` from
  `helper/src`. It needs a JDK, the Android SDK platform jar and Google's D8 in
  `%LOCALAPPDATA%\AndroidDesktop\devtools` (see the script). The built jar is committed, so
  running the app doesn't need them.
- `python tools/make_icon.py` redraws the app icon.
- `python tools/build_camera.py` rebuilds `androiddesktop/assets/AndroidDesktopCamera.dll` from
  `helper/camera`. It needs Visual Studio 2022 (or its Build Tools) with the C++ workload and a
  Windows 11 SDK. The built DLL is committed, so installing the camera doesn't need them. After a
  rebuild, `camera install` again (`camera` says when the installed one is outdated).

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

What's missing and what's next is tracked in [ROADMAP.md](ROADMAP.md).

- Until Android's lock screen is gone after a start (screen-lock PIN), windows use scrcpy rather than
  the emulator's own displays.
- From the eighth app window on (or with an Android started by an older version), apps show through
  scrcpy: video encoded in software inside the VM, so large windows of fast-moving games cost CPU
  there (`scrcpy_quality` trades sharpness for it).
- On the emulator's displays a mouse drag is a finger: it scrolls and flings like a phone rather than
  selecting text, and Shift+right-click is Back like right-click.
- Android always cold-boots (about 30 s). Quick-boot snapshots would be faster, but restoring them
  headless proved unreliable.
- The camera and microphone are the PC's: the first webcam the emulator lists, and the default
  microphone. A webcam plugged in or out is noticed at the next start of Android, not while it runs.
- Without the Android Desktop Camera (see Webcam), Android sees only part of the webcam's picture,
  a tall section from its left side. Android's camera driver (in Google's system image) always
  presents a phone camera: 4:3 at most 1280×960, mounted at 90°, whatever `hw.camera.*.orientation`
  says. The emulator turns each webcam frame to match a phone held upright and keeps the part that
  fits from the left edge. With a widescreen webcam that doesn't offer 1280×960 (most don't), some
  frames also fail to convert ("Could not convert from BGR4 to I420" in `emulator.log`). The
  emulator's other camera sources (`imagefile:`, `videofile:`, `environment`) can't stand in: they
  aren't live, and they fill the frame, cutting off the sides.
- With it, the picture is whole but small: 540×304 pixels of Android's 960×1280 portrait frame, and
  in photos it sits to one side. Its layout follows the emulator's and Android's crops, which assume
  Android's main display stays portrait (it does here). Other Windows apps see the picture on the
  left of a black frame. The Android Desktop Camera needs Windows 11. While Android uses it,
  other apps can use the webcam only in the format it's reading (1280×720), and another app using
  the webcam first (the Windows Camera app, say) can stop Android getting it at all.
- Unlocking from the launcher (PIN or Windows Hello) takes a few seconds longer than the PIN check
  itself: the PIN is entered on Android's own lock screen too, and Android plays its unlock animation.
  If that part fails (a password with characters other than plain ASCII, or with `%s` in it, which
  Android's `input text` can't type), apps still work, but Android treats the PIN as not entered
  yet: its own "confirm it's you" screens (Settings) ask for the PIN instead of a fingerprint, and if
  you cancel Windows Hello, the app keeps waiting until it gives up or another app comes to the front.
  The log then says "Android's lock screen stays up".
- Google's passkeys and Play purchases haven't been tried with the fingerprint yet.
- Notifications stay inside Android for now.
- AI agents: moving an app from its hidden display into a window changes its screen size, so Android
  recreates the app's screen, as when a phone rotates. Well-made apps keep their state (Clock's
  timer runs on); an app that doesn't may lose what's on screen, such as half-typed text.
- AI agents: only one `UiAutomation` can be connected in Android at a time. While an agent reads
  screens, `uiautomator dump` (and a second MCP server's reads) fail; the helper lets go after 30
  seconds without a read. `home` does nothing on a hidden display, which has no home screen.
  Screenshots are for hidden apps; for an app in your window the agent gets the tree only.
- In scrcpy windows, protected (`FLAG_SECURE`) screens update a few times a second instead of at full speed, and typing
  on them is limited to plain ASCII text. If one can't be moved to a secure display, the window says
  so (close it and reopen the app to leave that screen); it never just goes black.
- The installer isn't code-signed, so SmartScreen warns about it (see Install).
- iPhone & iPad: video is as smooth as Android draws it in the VM, about 20 to 40 frames a second for
  everyday apps (software encoding). On a test VM a tap's first frame reached the PC after about 70 to
  90 ms, almost all of it Android's own; the Wi-Fi and the device's decoding come on top. A secure
  (`FLAG_SECURE`) screen is black on the device, as Android wants: the device says so and offers
  Bring back. Moving an app to the device, back, or rotating it recreates the app's screen, as when a
  phone rotates. If the PC's address on the Wi-Fi changes, pair again (Home Screen icons keep the old one).
- Sound stays on the PC. Sending one app's sound to the device looks feasible: Android 13+ can capture
  a single app's playback (AudioPlaybackCapture by app; scrcpy has a playback source), which could go
  to Safari as Opus or AAC beside the video. Not built yet.
