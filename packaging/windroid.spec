# PyInstaller spec for the installed WinDroid (tools/build_installer.py runs it). One folder, dist/WinDroid:
#   WinDroid.exe      no console: the launcher, app windows, Start menu shortcuts, taskbar pins, and the
#                     MCP server (its stdin/stdout are the pipes an MCP client gives it)
#   windroid-cli.exe  the same with a console, for what prints: setup, doctor, camera
#   _internal/        Python, the androiddesktop package with its assets and ui, and their libraries
# -*- mode: python -*-
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parent
PACKAGE = ROOT / "androiddesktop"
VERSION = re.search(r'__version__ = "([\d.]+)"', (PACKAGE / "__init__.py").read_text(encoding="utf-8")).group(1)
ICON = str(PACKAGE / "assets" / "icon.ico")


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
    # The package finds these next to its modules (Path(__file__).with_name(...)), so they go there.
    datas=[(str(PACKAGE / "assets"), "androiddesktop/assets"), (str(PACKAGE / "ui"), "androiddesktop/ui")],
    # Some of ours are imported only when a command needs them; pywintypes imports win32timezone itself,
    # whenever it hands back a time (a shortcut's GetPath does).
    hiddenimports=collect_submodules("androiddesktop") + ["win32timezone"],
    excludes=["tkinter"],  # Pillow's ImageTk would bring Tcl/Tk along
)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="WinDroid", icon=ICON, console=False, upx=False,
          version=version_info("WinDroid.exe", "WinDroid"))
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="windroid-cli", icon=ICON, console=True, upx=False,
          version=version_info("windroid-cli.exe", "WinDroid (command line)"))
COLLECT(gui, cli, a.binaries, a.datas, upx=False, name="WinDroid")
