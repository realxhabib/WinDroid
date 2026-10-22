# Write a plugin

A plugin gives one Android app's window more to do on your PC. It runs inside WinDroid and gets that
app's window:

- **What's on screen:** screenshots of the app, and Windows' own text recognition (OCR) on them, with
  where every line and word sits.
- **Input:** text pasted or typed into the app, clicks, taps, drags and swipes.
- **Things on top:** a side panel (a small web page) glued to the window's edge, and click-through
  overlays on the app itself. Both move, minimize and close with the window.

Everything stays on your PC. A plugin is a folder of Python, and WinDroid 0.1.2 or later loads it.

## Try the example

[Screen Text](examples/plugins/screen_text) lists the text on the Settings or Clock app's screen in a
side panel. Click a line, and the app gets a tap right there.

1. Copy `examples/plugins/screen_text` into `%LOCALAPPDATA%\AndroidDesktop\plugins` (in WinDroid,
   **Settings > Plugins > Plugins folder** opens it).
2. Restart WinDroid (tray icon > **Quit**, then start it again). It loads plugins when it starts.
3. In **Settings > Plugins**, turn on **Screen Text (example)**, then open Settings or Clock.

## The shape of a plugin

```
my_plugin\          its folder; the name is how WinDroid imports it (letters, digits and _)
  __init__.py       sets PLUGIN
  panel.html        anything else it needs, beside __init__.py
```

The smallest useful one:

```python
from androiddesktop.log import log
from androiddesktop.plugins import Plugin


class Hello(Plugin):
    name = "hello"                                   # its key in WinDroid's settings: keep it
    title = "Hello"                                  # as Settings > Plugins shows it
    packages = frozenset({"com.android.settings"})   # the apps it's for
    default_on = False                               # off until turned on in Settings > Plugins

    def start(self):
        log(f"hello: {self.app.label} is showing")   # into WinDroid's log (see Work on one)


PLUGIN = Hello
```

An app's package name is in its Play Store link: `play.google.com/store/apps/details?id=<package>`.

WinDroid makes one instance for every window of those apps and calls these, all optional:

| Hook | When |
|---|---|
| `start()` | The app is showing in its window for the first time. |
| `geometry_changed(geo)` | The window moved, resized, changed monitor or scale, was minimized or restored. |
| `foreground_changed(hwnd)` | Another window came to the front (compare with `self.app.hwnd`). |
| `hotkey(key_id)` | A hotkey you registered with `self.app.hotkeys()` was pressed. |
| `app_lost()`, `app_back()` | The app stopped showing for a moment (reconnecting), and came back. |
| `stop()` | The window is closing: take down your panel and stop your threads. |

## What the window gives you

`self.app` is the plugin's handle on its window.

**Reading the app**

- `screenshot()`: the app's screen as a Pillow image, in view pixels; `None` while it isn't showing.
- `androiddesktop.winocr.recognize(image, scale)`: the lines of text in an image, top to bottom.
  Each line has `text`, `x`, `y`, `w`, `h` and its `words`, each with a box of its own. A `scale` of 2
  reads it enlarged, which helps with small type.
- `geometry()`: where the window is (`frame`), where the app is drawn inside it (`content`), the
  monitor's work area, the display `scale`, and whether it's `visible` or `maximized`.
  `geo.to_screen(x, y)` turns a point on the app's screen into screen coordinates.

**Doing things in it**

- `keyboard()`: a keyboard into the app's focused text field, even when the window isn't in front.
  `paste(text)` puts text in at once, `type(text)` types it key by key, and `select_all()` and
  `to_end()` move around it.
- `tap(x, y)`: a finger's tap on the app's screen (what reliably puts the cursor in a text field).
  `click(x, y)` is a mouse click, and `drag(x, y, dy)` and `fling(x, y, dy)` move a finger.
- `focus()`: bring the window to the front, with the keyboard in the app.

**Putting things on top**

- `panel(title=, url=, api=, size=, light=)`: a side panel showing your HTML page. Your page calls
  Python through `window.pywebview.api.<method>()` on the `api` object you pass. Python calls the page
  with `panel.call("fnName", data)`, `panel.place(rect, visible)` puts it in place, and
  `panel.destroy()` takes it down. `androiddesktop.overlay.side_panel(...)` works out where it goes
  beside the window, as the example does.
- `layer()`: a click-through overlay. `layer.update(image, x, y)` shows a transparent (RGBA) Pillow
  image at screen coordinates, and `update(None, 0, 0)` hides it.

**Also:** `package` and `label` (the app's), `hwnd` (the window), `foreground()`, `protected`
(a screen Android marks secure is showing), `call(fn)` and `hotkeys(...)`. The docstrings in
[`androiddesktop/plugins/__init__.py`](androiddesktop/plugins/__init__.py) have every detail.

## Threads

Hooks run on the window's own thread, so they must return quickly. Do anything slow (reading the
screen, OCR, typing, waiting) on a thread of your own, as Screen Text does, and stop it in `stop()`.
`self.app.call(fn)` runs `fn` back on the window thread. Panels can be called from any thread, and
your `api` methods run on the panel's own thread.

## What a plugin can use

A plugin runs inside WinDroid, so it has what WinDroid bundles:

- Python's standard library, Pillow, and the pywin32 modules WinDroid uses
- WinDroid's own modules: `androiddesktop.*`, OCR and overlays included
- your own pure-Python code inside your plugin's folder, imported relatively (`from .helpers import x`)

Packages from pip aren't there in the installed WinDroid.

## Work on one

- Point WinDroid at the folder you're working in instead of copying it each time:
  `setx ANDROIDDESKTOP_PLUGINS C:\path\to\your\plugins`. A plugin there comes first, ahead of an
  installed copy of the same name. Restart WinDroid to load changes.
- `%LOCALAPPDATA%\AndroidDesktop\logs\androiddesktop.log` says `plugins: <name> from <folder>` when
  one loads, `can't load` with the traceback when it doesn't, and `plugin <name>: <hook> failed`
  with the traceback when a hook raises.
- Tests can load a plugin the way WinDroid does, with `androiddesktop.plugins.import_plugin(folder)`,
  and hand it a stand-in app: see [`tests/test_example_plugin.py`](tests/test_example_plugin.py).
- While a plugin for an app is on, WinDroid shows that app as a video (scrcpy) window, which is what
  plugins read and type into.

## Play fair

- A plugin runs with your Windows account's rights, inside WinDroid. Install only plugins you trust,
  and say plainly what yours reads and does.
- While `self.app.protected` is true, the app is showing a screen Android marks secure (a PIN, a
  password, a payment). Leave it to the person.
- Fill things in and let the person send them. Don't act on someone's accounts by yourself.

## Share it

A plugin is just a folder. Publish it as a repository or a zip, and people copy it into their
plugins folder.
