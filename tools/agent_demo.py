"""End-to-end demo of the agent layer, through the MCP server exactly as Claude Code would use it:

    python tools/agent_demo.py [--keep]

1. Start Clock hidden (start_session), with no window anywhere on the PC.
2. Go to the Timer tab, enter 10 minutes, start the timer: all by element ids from read_screen.
3. Read the time remaining back, and check no window appeared on the PC the whole time.
4. show_to_user: Clock moves into a normal Android Desktop window, its timer still running.
5. end_session; the window stays with the user (closed again at the end unless --keep).

It speaks MCP over the server's stdin/stdout (python -m androiddesktop mcp), so it tests the server,
not just ghost.py. Set ANDROIDDESKTOP_SERIAL to run it against a test emulator. Exit code 0 on success.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import win32gui  # noqa: E402

from androiddesktop.manager import APP_CLASS  # noqa: E402

failures: list[str] = []


def check(ok: object, what: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}", flush=True)
    if not ok:
        failures.append(what)
    return bool(ok)


class Client:
    """A minimal MCP client over stdio."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "androiddesktop", "mcp"], cwd=str(ROOT),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._id = 0
        init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "agent_demo", "version": "1"}})
        self.notify("notifications/initialized")
        print(f"server: {init['serverInfo']} protocol {init['protocolVersion']}")

    def notify(self, method: str) -> None:
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                                           "params": params}) + "\n").encode())
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def tool(self, name: str, **arguments) -> tuple[str, list[dict]]:
        start = time.perf_counter()
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        took = (time.perf_counter() - start) * 1000
        text = result["content"][0]["text"]
        shown = text if len(text) < 160 else text[:157] + "..."
        print(f"> {name} {json.dumps(arguments)} [{took:.0f} ms]\n  {'ERROR ' if result['isError'] else ''}{shown}")
        if result["isError"]:
            raise RuntimeError(f"{name}: {text}")
        return text, result["content"]

    def screen(self, session: str) -> dict:
        return json.loads(self.tool("read_screen", session=session)[0])

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(20)


def elements(screen: dict):
    def walk(items):
        for e in items:
            yield e
            yield from walk(e.get("children", ()))
    for w in screen.get("windows", ()):
        yield from walk(w["elements"])


def find(screen: dict, label: str) -> dict:
    """The element labelled `label`, as an agent would pick it from the tree: tappable ones first."""
    found = [e for e in elements(screen) if label in (e.get("text"), e.get("desc"))]
    found.sort(key=lambda e: not e.get("clickable"))
    if not found:
        raise RuntimeError(f"no {label!r} on screen")
    return found[0]


def app_windows() -> list[str]:
    """Visible top-level windows of Android Desktop's apps and of scrcpy."""
    found = []

    def visit(hwnd: int, _: object) -> bool:
        if win32gui.IsWindowVisible(hwnd) and (win32gui.GetClassName(hwnd) in (APP_CLASS, "SDL_app")
                                               or win32gui.GetWindowText(hwnd).startswith("androiddesktop:")):
            found.append(f"{win32gui.GetClassName(hwnd)} {win32gui.GetWindowText(hwnd)!r} ({hwnd:#x})")
        return True
    win32gui.EnumWindows(visit, None)
    return found


def main() -> None:
    keep = "--keep" in sys.argv
    before = set(app_windows())
    client = Client()
    tools = {t["name"] for t in client.request("tools/list", {})["tools"]}
    check(len(tools) == 10, f"10 tools: {', '.join(sorted(tools))}")
    session = ""
    try:
        print("\n1. Start Clock hidden")
        text, _ = client.tool("start_session", package="Clock")
        session = re.search(r"Session (\S+):", text)[1]

        print("\n2. Set a 10-minute timer")
        screen = client.screen(session)
        check(screen["hidden"], "the session is hidden")
        tab = next(e for e in elements(screen) if e.get("res") == "tab_menu_timer")
        if not tab.get("selected"):
            client.tool("tap", session=session, element_id=tab["id"])
            screen = client.screen(session)
        if not any(e.get("res", "").startswith("timer_setup_digit") for e in elements(screen)):
            # There are timers already, so the tab shows them: add one rather than touch them.
            client.tool("tap", session=session, element_id=find(screen, "Add timer")["id"])
            screen = client.screen(session)
        for digit in "1000":  # 00h 10m 00s
            client.tool("tap", session=session, element_id=find(screen, digit)["id"])
        screen = client.screen(session)
        setup = next(e for e in elements(screen) if e.get("res") == "timer_setup_time")
        check("10m" in setup["text"], f"the keypad shows {setup['text']!r}")
        client.tool("tap", session=session, element_id=find(screen, "Start")["id"])
        text, _ = client.tool("wait_for", session=session, text="Pause", timeout=10)
        check(json.loads(text)["found"], "the timer is running (a Pause button appeared)")

        print("\n3. Read the time remaining")
        time.sleep(2)
        screen = client.screen(session)
        remaining = [e["text"] for e in elements(screen) if re.fullmatch(r"\d{1,2}:\d\d", e.get("text", ""))]
        check(remaining, f"time remaining: {remaining}")
        check(any(9 * 60 <= int(m) * 60 + int(s) < 10 * 60 for m, s in (t.split(":") for t in remaining)),
              "one timer is between 9:00 and 10:00")
        _, content = client.tool("read_screen", session=session, screenshot=True)
        check(len(content) == 2 and content[1]["type"] == "image", "read_screen can add a screenshot")
        new = set(app_windows()) - before
        check(not new, f"no window appeared on the PC{': ' + ', '.join(new) if new else ''}")

        print("\n4. Show it to the user")
        client.tool("show_to_user", session=session)
        time.sleep(1.5)
        new = set(app_windows()) - before
        check(any(APP_CLASS in w and "'Clock'" in w for w in new), f"Clock is in a window now: {', '.join(new)}")
        screen = client.screen(session)
        check(not screen["hidden"], "the session follows the app into the window")
        remaining = [e["text"] for e in elements(screen) if re.fullmatch(r"\d{1,2}:\d\d", e.get("text", ""))]
        check(remaining, f"the timer is still running there: {remaining}")
    finally:
        if session:
            try:
                client.tool("end_session", session=session)
            except RuntimeError:
                pass
        client.close()
    if not keep:
        for hwnd_text in set(app_windows()) - before:
            hwnd = int(hwnd_text.rsplit("(", 1)[1].rstrip(")"), 16)
            if win32gui.GetClassName(hwnd) == APP_CLASS:
                win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE: the window closes Clock with it
    print("\nall checks passed" if not failures else f"\n{len(failures)} check(s) failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
