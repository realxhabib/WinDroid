"""An MCP server, over stdio, that lets an AI agent use Android apps: run them hidden, read them as a
compact accessibility tree, operate them, and hand them to the user as a normal window when they should
see them (ghost.py does the work).

    python -m androiddesktop mcp

MCP over stdio is JSON-RPC 2.0, one message per line. Only tools are offered, so the little protocol
there is lives here rather than in a dependency. Every tool call goes to logs/agent.log.
"""
from __future__ import annotations

import json
import sys
import time
import traceback

from . import __version__
from .agenthelper import HelperError
from .ghost import GhostError, Sessions
from .log import agent_log

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
Android apps for you to use, from WinDroid (a headless Android VM on this PC).

start_session runs an app hidden: on its own display, with no window, so the user's screen doesn't \
change. read_screen gives you its accessibility tree; tap, type_text, press and scroll operate it, \
naming elements by the ids read_screen returned (they change with every read). wait_for waits for text to \
appear. show_to_user moves the app into a normal window on the user's desktop, exactly as you left it, \
and the session goes on there. end_session closes a hidden app.

Screens Android marks secure (PINs, passwords, payments) are the user's: read_screen says "secure screen: \
needs the user" and input is refused. Call show_to_user and let the user take it from there.
"""

SESSION = {"type": "string", "description": "The session id start_session returned."}
ELEMENT = {"type": "integer", "description": "An element id from the last read_screen."}

TOOLS = [
    {"name": "list_apps",
     "description": "List the Android apps installed in WinDroid: label and package.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "start_session",
     "description": "Start an Android app for you to use and return a session id for the other tools. By "
                    "default it runs hidden, on a display of its own with no window: the user sees nothing. "
                    "visible=true opens it in a normal window on the user's desktop instead.",
     "inputSchema": {"type": "object", "properties": {
         "package": {"type": "string", "description": "Package name (com.google.android.deskclock) or the "
                                                      "app's name (Clock)."},
         "visible": {"type": "boolean", "default": False}},
         "required": ["package"]}},
    {"name": "read_screen",
     "description": "Read the session's screen as a compact accessibility tree: its windows, top first, and "
                    "their elements, each with an id, class, text or desc, bounds [left, top, right, bottom] "
                    "in screen pixels, and flags such as clickable, editable, scrollable, checked. "
                    "screenshot=true adds an image of the screen (hidden sessions; its pixels match the "
                    "bounds). On a secure screen you get \"secure screen: needs the user\" instead.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "screenshot": {"type": "boolean", "default": False}},
         "required": ["session"]}},
    {"name": "tap",
     "description": "Tap an element (element_id) or a point (x and y, in screen pixels). Waits for the "
                    "screen to settle.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "element_id": ELEMENT, "x": {"type": "integer"}, "y": {"type": "integer"}},
         "required": ["session"]}},
    {"name": "type_text",
     "description": "Put text in a text field, replacing what's there. element_id names the field (or a row "
                    "holding it); without one, the field that has focus. submit=true then presses the "
                    "keyboard's action key (Enter, Search, Go, Send).",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "text": {"type": "string"}, "element_id": ELEMENT,
         "submit": {"type": "boolean", "default": False}},
         "required": ["session", "text"]}},
    {"name": "press",
     "description": "Press a key: back, home or enter.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "key": {"type": "string", "enum": ["back", "home", "enter"]}},
         "required": ["session", "key"]}},
    {"name": "scroll",
     "description": "Scroll to show more in a direction (down shows what's further down). element_id picks "
                    "the scrollable element; by default the largest one on screen.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "element_id": ELEMENT},
         "required": ["session", "direction"]}},
    {"name": "wait_for",
     "description": "Wait until text appears on the session's screen (in any element's text or description, "
                    "any case), for up to timeout seconds. Returns the matching elements.",
     "inputSchema": {"type": "object", "properties": {
         "session": SESSION, "text": {"type": "string"},
         "timeout": {"type": "number", "default": 10, "description": "Seconds, at most 120."}},
         "required": ["session", "text"]}},
    {"name": "show_to_user",
     "description": "Hand the session's app to the user: it moves into a normal window on their desktop, "
                    "exactly as it is, and the session goes on there. Use it when the user should see or "
                    "finish something, and for secure screens.",
     "inputSchema": {"type": "object", "properties": {"session": SESSION}, "required": ["session"]}},
    {"name": "end_session",
     "description": "End a session. A hidden app closes; an app in the user's window stays open for them.",
     "inputSchema": {"type": "object", "properties": {"session": SESSION}, "required": ["session"]}},
]


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def text(value: object) -> dict:
    return {"type": "text", "text": value if isinstance(value, str) else dumps(value)}


def summary(name: str, args: dict) -> str:
    """A tool call's arguments for the log. Typed text is left out: it may be a password."""
    parts = []
    for k, v in args.items():
        if name == "type_text" and k == "text" and isinstance(v, str):
            parts.append(f"text=<{len(v)} characters>")
        else:
            parts.append(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}")
    return " ".join(parts)


class Server:
    def __init__(self, sessions: Sessions | None = None) -> None:
        self.sessions = sessions or Sessions()

    # ------------------------------------------------------------------ JSON-RPC
    def handle(self, message: dict) -> dict | None:
        """One incoming message; the reply, or None for a notification."""
        method, mid = message.get("method"), message.get("id")
        if "id" not in message:
            return None  # notifications/initialized, notifications/cancelled: nothing to say
        try:
            if method == "initialize":
                asked = (message.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "androiddesktop", "title": "WinDroid", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
        except Exception as e:
            agent_log(f"{method} failed:\n{traceback.format_exc().rstrip()}")
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": repr(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    # ------------------------------------------------------------------ tools
    def call_tool(self, name: str, args: dict) -> dict:
        start = time.perf_counter()
        try:
            content = self._run(name, args)
            error = False
        except (GhostError, HelperError) as e:
            content, error = [text(str(e))], True
        except Exception as e:
            agent_log(f"{name} crashed:\n{traceback.format_exc().rstrip()}")
            content, error = [text(f"{name} failed: {e!r}")], True
        took = (time.perf_counter() - start) * 1000
        outcome = content[0]["text"] if content and content[0]["type"] == "text" else ""
        agent_log(f"{name} {summary(name, args)} -> {'ERROR ' if error else ''}"
                  f"{outcome[:200] + ('...' if len(outcome) > 200 else '')} ({took:.0f} ms)")
        return {"content": content, "isError": error}

    def _run(self, name: str, a: dict) -> list[dict]:
        s = self.sessions
        if name == "list_apps":
            apps = sorted(s.apps(refresh=True).values(), key=lambda app: app.label.casefold())
            return [text([{"label": app.label, "package": app.package} for app in apps])]
        if name == "start_session":
            session = s.start(required(a, "package"), bool(a.get("visible", False)))
            where = "in a window on the user's desktop" if not session.hidden else "hidden (no window)"
            return [text(f"Session {session.id}: {session.label} ({session.package}) is running {where}. "
                         "Call read_screen to see it.")]
        sid = required(a, "session")
        if name == "read_screen":
            screen, shot = s.read(sid, bool(a.get("screenshot", False)))
            content = [text(screen)]
            if shot:
                content.append({"type": "image", "data": shot["data"], "mimeType": f"image/{shot['format']}"})
            return content
        if name == "tap":
            return [text(s.tap(sid, a.get("element_id"), a.get("x"), a.get("y")))]
        if name == "type_text":
            return [text(s.type_text(sid, required(a, "text"), a.get("element_id"), bool(a.get("submit", False))))]
        if name == "press":
            return [text(s.press(sid, required(a, "key")))]
        if name == "scroll":
            return [text(s.scroll(sid, required(a, "direction"), a.get("element_id")))]
        if name == "wait_for":
            return [text(s.wait_for(sid, required(a, "text"), float(a.get("timeout", 10))))]
        if name == "show_to_user":
            return [text(s.show(sid))]
        if name == "end_session":
            return [text(s.end(sid))]
        raise GhostError(f"No tool called {name!r}.")

    def close(self) -> None:
        self.sessions.close()


def required(args: dict, key: str):
    if args.get(key) in (None, ""):
        raise GhostError(f"Missing argument: {key}.")
    return args[key]


def main() -> None:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print must not land in the protocol
    agent_log(f"MCP server started (WinDroid {__version__})")
    server = Server()
    try:
        for raw in sys.stdin.buffer:
            if not raw.strip():
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}}
            else:
                reply = server.handle(message) if isinstance(message, dict) else {
                    "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "not a request"}}
            if reply is not None:
                out.write(dumps(reply).encode("utf-8") + b"\n")
                out.flush()
    finally:
        server.close()
        agent_log("MCP server stopped")
