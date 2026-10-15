"""Dev harness: open a few apps in their own windows, no launcher. Ctrl+C or close them all to exit."""
import sys
import time

from androiddesktop import winapi
from androiddesktop.catalog import Catalog
from androiddesktop.manager import WindowManager
from androiddesktop.vm import VM

winapi.dpi_aware()
vm, catalog = VM(), Catalog()
manager = WindowManager(vm, catalog, ipc=False)  # leaves a running agent alone
manager.start()
for pkg in sys.argv[1:] or ["com.google.android.deskclock"]:
    manager.open(pkg)
    time.sleep(0.5)
time.sleep(3)
while manager.windows:
    time.sleep(0.5)
