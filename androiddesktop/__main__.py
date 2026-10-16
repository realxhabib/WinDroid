import os
import sys


def main() -> None:
    if getattr(sys, "frozen", False):
        # The installed WinDroid starts in its program folder. Windows won't delete a folder that is some
        # process's current one, and what we start (the emulator, adb's server) lives on: so not there.
        os.chdir(os.path.expanduser("~"))
    if sys.argv[1:2] == ["mcp"]:  # the MCP server: no windows, so none of app.py's GUI setup
        from .mcp import main as run
    else:
        from .app import main as run
    run()


if __name__ == "__main__":
    main()
