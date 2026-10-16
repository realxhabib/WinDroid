import sys


def main() -> None:
    if sys.argv[1:2] == ["mcp"]:  # the MCP server: no windows, so none of app.py's GUI setup
        from .mcp import main as run
    else:
        from .app import main as run
    run()


if __name__ == "__main__":
    main()
