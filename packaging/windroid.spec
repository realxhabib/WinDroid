# PyInstaller spec for the installed WinDroid (tools/build_installer.py runs it). One folder, dist/WinDroid:
#   WinDroid.exe      no console: the launcher, app windows, Start menu shortcuts, taskbar pins, and the
#                     MCP server (its stdin/stdout are the pipes an MCP client gives it)
#   windroid-cli.exe  the same with a console, for what prints: setup, doctor, camera
#   _internal/        Python, the androiddesktop package with its assets and ui, and their libraries
# -*- mode: python -*-
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent
PACKAGE = ROOT / "androiddesktop"
VERSION = re.search(r'__version__ = "([\d.]+)"', (PACKAGE / "__init__.py").read_text(encoding="utf-8")).group(1)
ICON = str(PACKAGE / "assets" / "icon.ico")

# Plugins (androiddesktop/plugins) run inside WinDroid with what it bundles, and they aren't here to be traced,
# so all of the standard library and all of Pillow come along, not only what WinDroid itself imports.
NOT_FOR_PLUGINS = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "ensurepip", "venv", "lib2to3",
                   "pydoc_data", "this", "antigravity"}
PLUGIN_RUNTIME = [name for module in sorted(sys.stdlib_module_names - NOT_FOR_PLUGINS) if not module.startswith("_")
                  for name in collect_submodules(module, filter=lambda n: ".test" not in n) or [module]]
PLUGIN_RUNTIME += collect_submodules("PIL", filter=lambda n: n not in ("PIL.ImageTk", "PIL.ImageQt", "PIL._tkinter_finder"))


def version_info(exe: str, description: str) -> VSVersionInfo:
    numbers = (tuple(int(n) for n in VERSION.split(".")) + (0, 0, 0, 0))[:4]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "realxhabib"),
            StringStruct("FileDescription", description),  # Task Manager's name for the process
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", exe),
            StringStruct("OriginalFilename", exe),
            StringStruct("ProductName", "WinDroid"),
            StringStruct("ProductVersion", VERSION),
            StringStruct("LegalCopyright", "github.com/realxhabib/WinDroid"),
        ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])])


a = Analysis(
    [str(Path(SPECPATH) / "windroid.py")],
    pathex=[str(ROOT)],
    # The package finds these next to its modules (Path(__file__).with_name(...)), so they go there:
    # assets/, ui/, remote/web/, plugins' own pages... every file in it that isn't Python.
    datas=[(str(f), str(Path("androiddesktop") / f.parent.relative_to(PACKAGE)))
           for f in PACKAGE.rglob("*")
           if f.is_file() and f.suffix not in (".py", ".pyc") and "__pycache__" not in f.parts],
    # Some of ours are imported only when a command needs them; pywintypes imports win32timezone itself,
    # whenever it hands back a time (a shortcut's GetPath does).
    hiddenimports=collect_submodules("androiddesktop") + ["win32timezone"] + PLUGIN_RUNTIME,
    excludes=["tkinter"],  # Pillow's ImageTk would bring Tcl/Tk along
)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="WinDroid", icon=ICON, console=False, upx=False,
          version=version_info("WinDroid.exe", "WinDroid"))
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="windroid-cli", icon=ICON, console=True, upx=False,
          version=version_info("windroid-cli.exe", "WinDroid (command line)"))
COLLECT(gui, cli, a.binaries, a.datas, upx=False, name="WinDroid")
