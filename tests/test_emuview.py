"""App windows on the emulator's own displays: the gRPC client, finding the emulator, display sizes,
keys, slots. No Android needed: a small fake gRPC server stands in for the emulator."""
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import win32con

from androiddesktop import emudisplays, emugrpc, emuview, runtime
from androiddesktop.appwindow import SCRCPY_QUALITY, scrcpy_args


class Protobuf(unittest.TestCase):
    def test_round_trip(self):
        msg = emugrpc.pb((1, 300), (2, b"\x01\x02"), (3, "text"), (4, 0), (5, None), (6, -1))
        fields = emugrpc.unpb(msg)
        self.assertEqual(fields[1], [300])
        self.assertEqual(bytes(fields[2][0]), b"\x01\x02")
        self.assertEqual(bytes(fields[3][0]), b"text")
        self.assertNotIn(4, fields)  # proto3 leaves out zero...
        self.assertNotIn(5, fields)  # ...and we leave out None
        self.assertEqual(fields[6], [(1 << 64) - 1])  # a negative int32 is ten bytes of varint

    def test_empty_nested_message_is_kept(self):
        self.assertEqual(emugrpc.pb((1, b"")), b"\x0a\x00")  # an empty oneof member still says which it is

    def test_image_format_and_image(self):
        f = emugrpc.unpb(emugrpc.image_format(3, emugrpc.RGBA8888, handle="abc"))
        self.assertEqual((f[1], f[5]), ([emugrpc.RGBA8888], [3]))
        transport = emugrpc.unpb(f[6][0])
        self.assertEqual((transport[1], bytes(transport[2][0])), ([emugrpc.MMAP], b"abc"))
        image = emugrpc.pb((1, emugrpc.pb((1, 1), (2, emugrpc.pb((1, 1))), (3, 396), (4, 822))), (5, 7))
        self.assertEqual(emugrpc.parse_image(memoryview(image)), (396, 822, 1, 7, None))

    def test_input_events(self):
        touch = emugrpc.unpb(emugrpc.unpb(emugrpc.touch_event(2, emugrpc.touch(10, 20, 0, 1)))[2][0])
        self.assertEqual(touch[2], [2])  # the display
        finger = emugrpc.unpb(touch[1][0])
        self.assertEqual((finger[1], finger[2], finger[4], finger[7]), ([10], [20], [1], [1]))
        lifted = emugrpc.unpb(emugrpc.unpb(emugrpc.unpb(emugrpc.touch_event(2, emugrpc.touch(10, 20, 0, 0)))[2][0])[1][0])
        self.assertNotIn(4, lifted)  # pressure 0 lifts the finger


class Hpack(unittest.TestCase):
    def test_integers(self):  # RFC 7541, C.1
        self.assertEqual(emugrpc._hpack_int(10, 5), bytes([10]))
        self.assertEqual(emugrpc._hpack_int(1337, 5), bytes([31, 154, 10]))
        self.assertEqual(emugrpc._hpack_int(42, 8), bytes([42]))

    def test_literal_headers(self):
        self.assertEqual(emugrpc.encode_headers([("te", "trailers")]), b"\x00\x02te\x08trailers")


class FakeEmulator(threading.Thread):
    """Just enough of a gRPC server: answers each call with the messages in `replies`, then trailers."""

    def __init__(self, replies: list[bytes], token: str = "") -> None:
        super().__init__(daemon=True)
        self.replies, self.token = replies, token
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.requests: list[bytes] = []
        self.headers: list[bytes] = []

    def frame(self, conn, kind, flags, stream, payload=b""):
        conn.sendall(struct.pack(">I", len(payload))[1:] + bytes([kind, flags]) + struct.pack(">I", stream) + payload)

    def run(self) -> None:
        conn, _ = self.server.accept()
        f = conn.makefile("rb")
        assert f.read(len(emugrpc.PREFACE)) == emugrpc.PREFACE
        self.frame(conn, emugrpc.SETTINGS, 0, 0)
        body = bytearray()
        while True:
            head = f.read(9)
            if len(head) < 9:
                return
            length, kind, flags, stream = int.from_bytes(head[:3], "big"), head[3], head[4], int.from_bytes(head[5:], "big")
            payload = f.read(length)
            if kind == emugrpc.HEADERS:
                self.headers.append(payload)
                self.frame(conn, emugrpc.HEADERS, emugrpc.END_HEADERS, stream, b"\x88")  # :status 200
            elif kind == emugrpc.DATA:
                body += payload
                if flags & emugrpc.END_STREAM:
                    self.requests.append(bytes(body[5:]))
                    body.clear()
                    for reply in self.replies:
                        # Split across frames, as a real server may.
                        data = b"\x00" + struct.pack(">I", len(reply)) + reply
                        self.frame(conn, emugrpc.DATA, 0, stream, data[:3])
                        self.frame(conn, emugrpc.DATA, 0, stream, data[3:])
                    self.frame(conn, emugrpc.HEADERS, emugrpc.END_HEADERS | emugrpc.END_STREAM, stream, b"\x00")


class Channel(unittest.TestCase):
    def test_unary_call(self):
        server = FakeEmulator([emugrpc.pb((1, "37.2.12.0"))])
        server.start()
        channel = emugrpc.Channel(server.port, token="secret")
        reply = channel.unary("getStatus", b"")
        self.assertEqual(bytes(reply[1][0]), b"37.2.12.0")
        self.assertIn(b"/android.emulation.control.EmulatorController/getStatus", server.headers[0])
        self.assertIn(b"Bearer secret", server.headers[0])
        channel.close()

    def test_server_stream(self):
        frames = [emugrpc.pb((1, emugrpc.pb((3, 100 + i), (4, 200))), (5, i)) for i in range(3)]
        server = FakeEmulator(frames)
        server.start()
        channel = emugrpc.Channel(server.port)
        got, ended = [], threading.Event()
        call = channel.call("streamScreenshot", emugrpc.image_format(1), on_message=lambda m: got.append(emugrpc.parse_image(m)),
                            on_end=lambda e: ended.set())
        self.assertTrue(ended.wait(5))
        self.assertEqual([g[0] for g in got], [100, 101, 102])
        self.assertEqual(emugrpc.unpb(server.requests[0])[5], [1])
        self.assertTrue(call.done.is_set())
        channel.close()

    def test_unreachable(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(OSError):
            emugrpc.Channel(port, timeout=1)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        (Path(self.dir.name) / "avd" / "running").mkdir(parents=True)
        for p in (mock.patch.object(emuview.tempfile, "gettempdir", return_value=self.dir.name),
                  mock.patch.object(emuview, "_alive", return_value=True),
                  mock.patch.object(emuview, "SERIAL", "emulator-5584")):
            p.start()
            self.addCleanup(p.stop)

    def advertise(self, pid, port, *flags, token="tok"):
        cmdline = " ".join(f'"{f}"' for f in ("qemu.exe", "-avd", "AndroidDesktop", *flags))
        (Path(self.dir.name) / "avd" / "running" / f"pid_{pid}.ini").write_text(
            f"port.serial={port}\nport.adb={port + 1}\ncmdline={cmdline}\ngrpc.port=8584\ngrpc.token={token}\n")

    def test_parse(self):
        self.assertEqual(emuview.parse_discovery("a=1\ncmdline=\"x\" \"-y\"\njunk\n"), {"a": "1", "cmdline": '"x" "-y"'})

    def test_finds_ours(self):
        self.advertise(10, 5650, "-qt-hide-window")  # someone else's emulator
        self.advertise(11, 5584, "-qt-hide-window")
        self.assertEqual(emuview.endpoint(), (8584, "tok"))

    def test_not_without_a_window(self):
        self.advertise(11, 5584, "-no-window")
        self.assertIsNone(emuview.endpoint())

    def test_not_when_gone(self):
        self.advertise(11, 5584, "-qt-hide-window")
        with mock.patch.object(emuview, "_alive", return_value=False):
            self.assertIsNone(emuview.endpoint())

    def test_scrcpy_by_choice_or_after_trouble(self):
        self.advertise(11, 5584, "-qt-hide-window")
        link = emuview.Link()
        with mock.patch.object(emuview.state, "get", return_value="scrcpy"):
            self.assertIsNone(link.usable())
        with mock.patch.object(emuview.state, "get", return_value=None), mock.patch.object(emuview, "log"):
            self.assertEqual(link.usable(), (8584, "tok"))
            link.disable((8584, "tok"), "wrong frame size")
            self.assertIsNone(link.usable())
            self.advertise(12, 5584, "-qt-hide-window", token="new")  # Android restarted
            (Path(self.dir.name) / "avd" / "running" / "pid_11.ini").unlink()
            self.assertEqual(link.usable(), (8584, "new"))


class Sizes(unittest.TestCase):
    def test_display_size(self):
        surface = emuview.Surface(mock.Mock(), (1, ""), 1)
        with mock.patch.object(emuview.Surface, "_screen", return_value=(1984, 1144)):
            self.assertEqual(surface.display_size(412, 830, 160), (412, 830))
            self.assertEqual(surface.display_size(280, 190, 160), (320, 320))  # the emulator's smallest
            self.assertEqual(surface.display_size(280, 600, 240), (480, 600))
            self.assertEqual(surface.display_size(3000, 900, 160), (1984, 900))  # no bigger than our shared memory

    def test_min_px(self):
        self.assertEqual((emudisplays.min_px(160), emudisplays.min_px(240), emudisplays.min_px(213)), (320, 480, 426))


class Touches(unittest.TestCase):
    """A finger through InputHelper: the emulator's own touchscreens make Android offer handwriting."""

    def test_down_move_up(self):
        surface = emuview.Surface(mock.Mock(), (1, ""), 1)
        surface.display = 12
        with mock.patch.object(emuview.link, "helper") as helper:
            surface.touch(5, 6, False)  # a lift without a touch: nothing
            surface.touch(5, 6, True)
            surface.touch(7, 8, True)
            surface.touch(9, 10, False)
            surface.touch(9, 10, False)
        self.assertEqual([c.args[0] for c in helper.call_args_list],
                         ["touch 12 0 5 6", "touch 12 2 7 8", "touch 12 1 9 10"])


class Polling(unittest.TestCase):
    """A stream that brings nothing new is checked with getScreenshot, and started again if it missed a frame."""

    def surface(self, live: bool, stale_for: float) -> emuview.Surface:
        surface = emuview.Surface(mock.Mock(), (1, ""), 1)
        if live:
            surface._stream_token = object()
        surface._last_change = time.monotonic() - stale_for
        surface._connect = mock.Mock()
        surface._connect.return_value.unary.return_value = emugrpc.unpb(emugrpc.pb((1, emugrpc.pb((3, 396), (4, 822)))))
        surface._go_live, surface._go_idle = mock.Mock(), mock.Mock()
        return surface

    def poll(self, surface: emuview.Surface, new_frame: bool) -> bool:
        with mock.patch.object(emuview.win32gui, "IsIconic", return_value=False), \
                mock.patch.object(surface, "_take", return_value=new_frame) as take:
            ok = surface._poll_once()
        self.took = take.call_args_list
        return ok

    def test_a_stream_bringing_frames_isnt_polled(self):
        surface = self.surface(live=True, stale_for=0.1)
        self.assertTrue(self.poll(surface, True))
        surface._connect.assert_not_called()
        self.assertEqual(self.took, [])

    def test_a_stale_stream_that_missed_a_frame_starts_again(self):
        surface = self.surface(live=True, stale_for=emuview.LIVE_SECS + 1)
        self.assertTrue(self.poll(surface, True))
        self.assertEqual(self.took, [mock.call(396, 822)])
        surface._go_idle.assert_called_once_with()
        surface._go_live.assert_called_once_with()

    def test_a_stale_stream_with_nothing_new_is_left_alone(self):
        surface = self.surface(live=True, stale_for=emuview.LIVE_SECS + 1)
        self.poll(surface, False)
        surface._go_idle.assert_not_called()
        surface._go_live.assert_not_called()

    def test_idle_polls_and_goes_live_on_a_new_frame(self):
        surface = self.surface(live=False, stale_for=0)
        self.poll(surface, True)
        surface._go_idle.assert_not_called()
        surface._go_live.assert_called_once_with()


class Slots(unittest.TestCase):
    def test_claim_and_keep_free(self):
        with mock.patch.object(emudisplays, "emulator_displays", return_value={1: 5, 3: 9}), \
                mock.patch.object(emudisplays, "_slots", set()):
            self.assertEqual(emudisplays.claim(), 2)  # 1 and 3 are someone else's
            self.assertEqual(emudisplays.claim(), 4)
            for _ in range(3):
                emudisplays.claim(keep_free=1)  # 5, 6, 7
            with self.assertRaises(emudisplays.NoSlot):
                emudisplays.claim(keep_free=1)  # 8 stays for a protected screen
            self.assertEqual(emudisplays.claim(), 8)
            emudisplays.release(4)
            self.assertEqual(emudisplays.claim(), 4)

    def test_parse_emulator_displays(self):
        dump = ('DisplayInfo{"Emulator 2D Display", displayId 12, displayGroupId 3, FLAG_SECURE, '
                'uniqueId "virtual:com.android.emulator.multidisplay:1234564", app 396 x 822}')
        self.assertEqual(emudisplays.parse_emulator_displays(dump), {3: 12})


class Keys(unittest.TestCase):
    def test_left_and_right(self):
        self.assertEqual(emuview.key_of(win32con.VK_CONTROL, 0), win32con.VK_LCONTROL)
        self.assertEqual(emuview.key_of(win32con.VK_CONTROL, 1 << 24), win32con.VK_RCONTROL)
        self.assertEqual(emuview.key_of(win32con.VK_MENU, 1 << 24), win32con.VK_RMENU)
        self.assertEqual(emuview.key_of(ord("A"), 0), ord("A"))

    def test_keycodes(self):
        self.assertEqual(emuview.VK_TO_KEYCODE[win32con.VK_RETURN], 66)
        self.assertEqual(emuview.VK_TO_KEYCODE[ord("C")], 31)  # KEYCODE_C, for Ctrl+C
        self.assertEqual(emuview.VK_TO_KEYCODE[ord("9")], 16)
        self.assertEqual(emuview.VK_TO_KEYCODE[win32con.VK_F12], 142)

    def test_meta_state(self):
        down = {win32con.VK_LSHIFT, win32con.VK_RCONTROL}
        with mock.patch.object(emuview.win32api, "GetKeyState", side_effect=lambda vk: -128 if vk in down else 0):
            self.assertEqual(emuview.meta_state(), 0x41 | 0x5000)  # SHIFT_ON|SHIFT_LEFT, CTRL_ON|CTRL_RIGHT
        with mock.patch.object(emuview.win32api, "GetKeyState", side_effect=lambda vk: 1 if vk == win32con.VK_CAPITAL else 0):
            self.assertEqual(emuview.meta_state(), 0x100000)  # CAPS_LOCK_ON


class VmSize(unittest.TestCase):
    def test_pick(self):
        self.assertEqual(runtime.pick_vm_size(64 * 1024, 16), {"hw.cpu.ncore": "6", "hw.ramSize": "8192M"})
        self.assertEqual(runtime.pick_vm_size(16 * 1024, 8), {"hw.cpu.ncore": "4", "hw.ramSize": "6144M"})
        self.assertEqual(runtime.pick_vm_size(8 * 1024, 4), {"hw.cpu.ncore": "4", "hw.ramSize": "4096M"})

    def test_saved_displays_go(self):
        config = {"hw.display1.width": "396", "hw.display3.flag": "0", "hw.lcd.width": "720", "hw.ramSize": "4096M"}
        self.assertEqual(runtime.prepare_config(config, {"hw.ramSize": "8192M", "hw.cpu.ncore": "6"}),
                         {"hw.lcd.width": "720", "hw.ramSize": "8192M", "hw.cpu.ncore": "6"})


class ScrcpyQuality(unittest.TestCase):
    def test_presets(self):
        high = scrcpy_args("p", (412, 830), 160, (0, 0), (0, 0, 0))
        self.assertIn("--video-bit-rate=12M", high)
        self.assertFalse([a for a in high if a.startswith("--max-size")])
        low = scrcpy_args("p", (412, 830), 160, (0, 0), (0, 0, 0), SCRCPY_QUALITY["low"])
        self.assertIn("--video-bit-rate=3M", low)
        self.assertIn("--max-fps=30", low)
        self.assertIn("--max-size=408", low)  # half of 830, a multiple of 8
        self.assertIn("--new-display=412x830/160", low)  # the app still gets the whole window


if __name__ == "__main__":
    unittest.main()
