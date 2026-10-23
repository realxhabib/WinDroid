"""The MCP server (mcp.py): the protocol, the tools, and what goes in agent.log."""
import json
import unittest
from unittest import mock

from androiddesktop import log, mcp
from androiddesktop.ghost import GhostError

TOOLS = {"list_apps", "start_session", "read_screen", "tap", "type_text", "press", "scroll", "wait_for",
         "show_to_user", "end_session"}


class FakeSessions:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
            if name == "read":
                return {"session": args[0], "windows": []}, ({"format": "jpeg", "data": "AAAA"} if args[1] else None)
            if name == "tap" and args[1] == 404:
                raise GhostError("No element 404 on the last screen read.")
            if name == "press":
                raise RuntimeError("adb went away")
            return f"{name} done"
        return record


def request(method, params=None, mid=1):
    out = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        out["params"] = params
    return out


class Protocol(unittest.TestCase):
    def setUp(self):
        self.sessions = FakeSessions()
        self.server = mcp.Server(self.sessions)

    def test_initialize(self):
        reply = self.server.handle(request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "test", "version": "1"}}))
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")  # the client's, when we speak it
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "androiddesktop")
        self.assertIn("show_to_user", result["instructions"])
        newer = self.server.handle(request("initialize", {"protocolVersion": "2099-01-01"}))
        self.assertEqual(newer["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping_and_unknown_methods(self):
        self.assertEqual(self.server.handle(request("ping"))["result"], {})
        self.assertEqual(self.server.handle(request("resources/list"))["error"]["code"], -32601)

    def test_tools(self):
        tools = self.server.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, TOOLS)
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
            json.dumps(t)  # plain JSON


class Tools(unittest.TestCase):
    def setUp(self):
        self.sessions = FakeSessions()
        self.server = mcp.Server(self.sessions)

    def call(self, name, **arguments):
        return self.server.handle(request("tools/call", {"name": name, "arguments": arguments}))["result"]

    def test_read_screen_with_screenshot(self):
        result = self.call("read_screen", session="clock-1", screenshot=True)
        self.assertFalse(result["isError"])
        text, image = result["content"]
        self.assertEqual(json.loads(text["text"]), {"session": "clock-1", "windows": []})
        self.assertEqual(image, {"type": "image", "data": "AAAA", "mimeType": "image/jpeg"})
        self.assertEqual(len(self.call("read_screen", session="clock-1")["content"]), 1)

    def test_arguments_reach_the_session(self):
        self.call("tap", session="clock-1", element_id=21)
        self.call("tap", session="clock-1", x=10, y=20)
        self.call("type_text", session="clock-1", text="hi", submit=True)
        self.call("scroll", session="clock-1", direction="down")
        self.call("wait_for", session="clock-1", text="Start", timeout=3)
        self.assertEqual(self.sessions.calls, [
            ("tap", ("clock-1", 21, None, None)), ("tap", ("clock-1", None, 10, 20)),
            ("type_text", ("clock-1", "hi", None, True)), ("scroll", ("clock-1", "down", None)),
            ("wait_for", ("clock-1", "Start", 3.0))])

    def test_errors_are_tool_results(self):
        result = self.call("tap", session="clock-1", element_id=404)
        self.assertTrue(result["isError"])
        self.assertIn("No element 404", result["content"][0]["text"])
        crashed = self.call("press", session="clock-1", key="back")
        self.assertTrue(crashed["isError"])
        self.assertIn("adb went away", crashed["content"][0]["text"])
        self.assertTrue(self.call("tap")["isError"])  # no session
        self.assertTrue(self.call("no_such_tool", session="x")["isError"])

    def test_every_call_is_logged_without_typed_text(self):
        with mock.patch.object(mcp, "agent_log") as agent_log:
            self.call("type_text", session="clock-1", text="hunter2")
            self.call("tap", session="clock-1", element_id=404)
            self.call("wait_for", session="clock-1", text="Pause")
        typed, tapped, waited = (c.args[0] for c in agent_log.call_args_list)
        self.assertTrue(typed.startswith("type_text session='clock-1' text=<7 characters>"), typed)
        self.assertNotIn("hunter2", typed)
        self.assertIn("element_id=404 -> ERROR No element 404", tapped)
        self.assertIn("text='Pause'", waited)  # what it waits for isn't a secret

    def test_typed_text_is_kept_out_of_errors_in_the_log(self):
        self.sessions.type_text = mock.Mock(side_effect=GhostError("input text 'hunter2 secret': boom"))
        with mock.patch.object(mcp, "agent_log") as agent_log:
            result = self.call("type_text", session="clock-1", text="hunter2 secret")
            self.sessions.type_text.side_effect = RuntimeError("couldn't type 'hunter2'")
            self.call("type_text", session="clock-1", text="hunter2 secret")
        self.assertTrue(result["isError"])
        logged = "\n".join(c.args[0] for c in agent_log.call_args_list)
        self.assertNotIn("hunter2", logged)
        self.assertNotIn("secret", logged)
        self.assertIn("<typed text>", logged)

    def test_redact(self):
        self.assertEqual(mcp.redact("typed 1234 then", "1234"), "typed <typed text> then")
        self.assertEqual(mcp.redact("keys a, b, c", "a b%c"), "keys a, b, c")  # pieces too short to matter
        self.assertEqual(mcp.redact("input text 'it'\"'\"'s' x", "it's x"), "input text <typed text> x")
        self.assertEqual(mcp.redact("nothing typed", None), "nothing typed")

    def test_agent_log_file(self):
        log.agent_log("hello from the tests")
        self.assertIn("hello from the tests", (log.LOGS / "agent.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
