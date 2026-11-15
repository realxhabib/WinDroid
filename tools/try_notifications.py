"""Dev harness: Android's notifications as Windows toasts, without restarting the running agent.

Reads the notifications of the Android WinDroid uses (ANDROIDDESKTOP_SERIAL picks another) and shows
new ones as toasts. Clicking one opens the app in a window of this harness, on what the notification
is about; a reply goes through the notification's own reply action. Nothing a notification says is
printed or logged. Ctrl+C to stop (its toasts go with it)."""
import time

from androiddesktop import datadir, shortcuts, winapi
from androiddesktop.catalog import Catalog
from androiddesktop.manager import WindowManager
from androiddesktop.notifications import Notifications
from androiddesktop.vm import VM

winapi.dpi_aware()
datadir.ensure()  # run from inside a packaged app (e.g. Claude Desktop), it mustn't fork the data folder
vm, catalog = VM(), Catalog()
manager = WindowManager(vm, catalog, ipc=False)  # leaves a running agent alone
manager.start()


def label(package: str) -> str:
    app = catalog.get(package)
    return app.label if app else package


def icon(package: str):
    app = catalog.get(package)
    return app.icon if app else None


notes = Notifications(label=label, icon=icon, in_front=manager.in_front,
                      open=lambda package, then: manager.open(package, then=then),
                      notice=lambda text: print(f"notice: {text}"), app_id=shortcuts.LAUNCHER_ID)
notes.on_change = lambda: print(f"{len(notes.items())} notifications in Android")
notes.android_up()
print("Reading Android's notifications; new ones show as Windows toasts. Ctrl+C to stop.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    notes.android_down(wait=2)
