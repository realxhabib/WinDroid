"""Wire formats and pure logic for iPhones and iPads: scrcpy's stream and control messages, WebSocket
frames, the phone's messages, display sizes, the web server's request handling, firewall and
certificates."""
import ipaddress
import json
import plistlib
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from androiddesktop.remote import firewall, protocol
from androiddesktop.remote.hub import display_size, scale_point, text_chunks
from androiddesktop.remote.mirror import parse_display_announcement, server_args
from androiddesktop.remote.server import HttpError, Lan, choose_lan, parse_head, response_bytes

CONFIG = bytes.fromhex("000000016742c02a8d680dc0f2ca80000000016808ce06e2")


class ScrcpyStream(unittest.TestCase):
    def test_session_header(self):
        s = protocol.parse_header(struct.pack(">III", 0x80000001, 1912, 880))
        self.assertEqual(s, protocol.Session(1912, 880, True))
        self.assertFalse(protocol.parse_header(struct.pack(">III", 0x80000000, 880, 1912)).resized)

    def test_packet_headers(self):
        config = protocol.parse_header(struct.pack(">QI", protocol.FLAG_CONFIG, 33))
        self.assertEqual((config.config, config.key, config.size), (True, False, 33))
        key = protocol.parse_header(struct.pack(">QI", protocol.FLAG_KEY | 123456, 5000))
        self.assertEqual((key.pts, key.config, key.key, key.size), (123456, False, True, 5000))
        delta = protocol.parse_header(struct.pack(">QI", 99, 12))
        self.assertEqual((delta.pts, delta.key), (99, False))
        with self.assertRaises(ValueError):
            protocol.parse_header(b"short")

    def test_codec_string_from_sps(self):
        self.assertEqual(protocol.avc_codec(CONFIG), "avc1.42C02A")
        self.assertEqual(protocol.avc_codec(bytes.fromhex("00000001640028ac")), None)  # no SPS (NAL type 4)
        self.assertEqual(protocol.avc_codec(bytes.fromhex("0000000167640028")), "avc1.640028")
        self.assertIsNone(protocol.avc_codec(b"\x00\x00\x01"))
        self.assertIsNone(protocol.avc_codec(b""))

    def test_display_announcement(self):
        self.assertEqual(parse_display_announcement("[server] INFO: New display: 880x1912/320 (id=12)"), 12)
        self.assertIsNone(parse_display_announcement("[server] INFO: Device: [Google] google sdk"))

    def test_server_args(self):
        new = server_args("0abc1234", "/data/local/tmp/x.jar", new_display=(880, 1912, 320))
        for expected in ("scid=0abc1234", "tunnel_forward=true", "audio=false", "control=true", "video_codec=h264",
                         "new_display=880x1912/320", "flex_display=true", "vd_destroy_content=false",
                         "vd_system_decorations=false", "clipboard_autosync=false"):
            self.assertIn(expected, new)
        self.assertEqual(new[:5], ["CLASSPATH=/data/local/tmp/x.jar", "app_process", "/",
                                   "com.genymobile.scrcpy.Server", "5.0.1"])
        mirror = server_args("0abc1234", "/x.jar", display=7)
        self.assertIn("display_id=7", mirror)
        self.assertFalse([a for a in mirror if a.startswith(("new_display", "flex_display"))])
        with self.assertRaises(ValueError):
            server_args("0abc1234", "/x.jar")


class ControlMessages(unittest.TestCase):
    """Byte layouts as scrcpy-server 5.0.1's ControlMessageReader reads them."""

    def test_touch(self):
        m = protocol.touch_message(protocol.ACTION_DOWN, 3, 100, 200, 880, 1912, 1.0)
        self.assertEqual(len(m), 32)
        t, action, pid, x, y, w, h, p, button, buttons = struct.unpack(">BBqiiHHHii", m)
        self.assertEqual((t, action, pid, x, y, w, h, p, button, buttons), (2, 0, 3, 100, 200, 880, 1912, 0xFFFF, 0, 0))
        up = struct.unpack(">BBqiiHHHii", protocol.touch_message(protocol.ACTION_UP, 3, 1, 2, 880, 1912, 1.0))
        self.assertEqual(up[7], 0)  # no pressure once the finger is up

    def test_keys_text_and_others(self):
        self.assertEqual(protocol.key_message(0, 4), struct.pack(">BBiii", 0, 0, 4, 0, 0))
        self.assertEqual(protocol.text_message("hé"), b"\x01\x00\x00\x00\x03h\xc3\xa9")
        self.assertEqual(protocol.start_app_message("com.x"), b"\x10\x05com.x")
        self.assertEqual(protocol.reset_video_message(), b"\x11")
        self.assertEqual(protocol.resize_message(1912, 880), struct.pack(">BHH", 21, 1912, 880))
        clip = protocol.clipboard_message(7, "🙂", True)
        self.assertEqual(clip[:10], struct.pack(">BqB", 9, 7, 1))
        self.assertEqual(struct.unpack(">I", clip[10:14])[0], 4)
        scroll = struct.unpack(">BiiHHhhi", protocol.scroll_message(1, 2, 10, 20, 0, 16))
        self.assertEqual(scroll[:5], (3, 1, 2, 10, 20))
        self.assertEqual((scroll[5], scroll[6]), (0, 0x7FFF))
        with self.assertRaises(ValueError):
            protocol.start_app_message("x" * 300)


class WebSocketFrames(unittest.TestCase):
    def test_accept_key(self):  # RFC 6455's example
        self.assertEqual(protocol.ws_accept("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def roundtrip(self, payload: bytes):
        frame = protocol.ws_client_frame(protocol.OP_TEXT, payload, mask=b"\x9a\x01\xfe\x33")
        final, opcode, code, extra = protocol.ws_header(frame[:2])
        length, mask = protocol.ws_length(code, frame[2:2 + extra])
        body = frame[2 + extra:]
        return final, opcode, length, protocol.ws_unmask(body, mask)

    def test_client_frames(self):
        for n in (0, 5, 125, 126, 300, 70000):
            payload = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
            final, opcode, length, data = self.roundtrip(payload)
            self.assertEqual((final, opcode, length, data), (True, protocol.OP_TEXT, n, payload))

    def test_unmasked_and_oversized_rejected(self):
        with self.assertRaises(protocol.WebSocketError):
            protocol.ws_header(bytes([0x81, 0x05]))  # a server-style (unmasked) frame from the phone
        with self.assertRaises(protocol.WebSocketError):
            protocol.ws_header(bytes([0xC1, 0x85]))  # reserved bit
        with self.assertRaises(protocol.WebSocketError):
            protocol.ws_length(127, struct.pack(">Q", protocol.MAX_MESSAGE + 1) + b"mask")

    def test_server_frames(self):
        self.assertEqual(protocol.ws_frame(protocol.OP_TEXT, b"hi"), b"\x81\x02hi")
        self.assertEqual(protocol.ws_frame(protocol.OP_BINARY, b"x" * 300)[:4], b"\x82\x7e\x01\x2c")
        self.assertEqual(protocol.ws_frame(protocol.OP_BINARY, b"x" * 70000)[:2], b"\x82\x7f")

    def test_video_message(self):
        m = protocol.video_message(protocol.VIDEO_KEY, 1234.5, b"abc")
        self.assertEqual(m[0], 1)
        self.assertEqual(struct.unpack(">d", m[1:9])[0], 1234.5)
        self.assertEqual(m[9:], b"abc")


class PhoneMessages(unittest.TestCase):
    def parse(self, **m):
        return protocol.parse_message(json.dumps(m))

    def test_touch(self):
        m = self.parse(t="touch", w=880, h=1912, p=[["down", 0, 10, 20], ["move", 1, 5000, -3, 0.5]])
        self.assertEqual(m["points"], [(protocol.ACTION_DOWN, 0, 10, 20, 1.0), (protocol.ACTION_MOVE, 1, 879, 0, 0.5)])

    def test_touch_rejects(self):
        for bad in ({"t": "touch", "w": 0, "h": 10, "p": [["down", 0, 1, 1]]},
                    {"t": "touch", "w": 10, "h": 10, "p": []},
                    {"t": "touch", "w": 10, "h": 10, "p": [["tap", 0, 1, 1]]},
                    {"t": "touch", "w": 10, "h": 10, "p": [["down", 99, 1, 1]]},
                    {"t": "touch", "w": 10, "h": 10, "p": [["down", 0, "1", 1]]},
                    {"t": "touch", "w": 10, "h": 10, "p": [["down", 0, True, 1]]},
                    {"t": "touch", "w": 10, "h": 10, "p": [["down", 0, 1, 1]] * 11}):
            with self.assertRaises(protocol.BadMessage, msg=bad):
                protocol.parse_message(json.dumps(bad))

    def test_keys(self):
        self.assertEqual(self.parse(t="key", key="Backspace"), {"t": "key", "keycode": 67, "meta": 0})
        self.assertEqual(self.parse(t="key", key="ArrowLeft", shift=True)["meta"], protocol.META_SHIFT)
        # Cmd+C on an iPad keyboard is Ctrl+C for Android.
        self.assertEqual(self.parse(t="key", key="c", code="KeyC", meta=True),
                         {"t": "key", "keycode": 31, "meta": protocol.META_CTRL})
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="key", key="c", code="KeyC")  # a plain letter is text, not a key
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="key", key="Power")

    def test_text(self):
        self.assertEqual(self.parse(t="text", s="hi\x07 there\n")["s"], "hi there\n")
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="text", s="\x01")
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="text", s="x" * 2001)

    def test_open_view_and_others(self):
        m = self.parse(t="open", app="abc", decoder="webcodecs", view={"w": 430, "h": 880, "dpr": 3})
        self.assertEqual(m["view"], {"t": "view", "w": 430, "h": 880, "dpr": 3.0})
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="open", app="abc", decoder="h265")
        self.assertEqual(self.parse(t="view", w=1000, h=500, dpr=9)["dpr"], 4.0)
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="view", w=10, h=500)
        self.assertEqual(self.parse(t="nav", k="back"), {"t": "nav", "k": "back"})
        with self.assertRaises(protocol.BadMessage):
            self.parse(t="nav", k="power")
        self.assertEqual(self.parse(t="quality", q="sharp"), {"t": "quality", "q": "sharp"})
        self.assertEqual(self.parse(t="ack"), {"t": "ack"})
        self.assertEqual(self.parse(t="scroll", w=10, h=10, x=9, y=1, dx=0, dy=-40)["dy"], -16)

    def test_junk(self):
        for bad in ("", "[]", "null", '{"x":1}', '{"t":"rm -rf"}', "{bad json"):
            with self.assertRaises(protocol.BadMessage):
                protocol.parse_message(bad)


class Sizes(unittest.TestCase):
    def test_display_fits_the_phone(self):
        # iPhone 16 Pro Max, portrait: the app sees as many dp as the phone has CSS pixels.
        w, h, dpi = display_size({"w": 440, "h": 870, "dpr": 3}, 1.5)
        self.assertEqual((w, h, dpi), (660, 1304, 240))
        self.assertAlmostEqual(w * 160 / dpi, 440)
        self.assertEqual(display_size({"w": 870, "h": 440, "dpr": 3}, 1.5)[:2], (1304, 660))  # rotated
        self.assertEqual(display_size({"w": 440, "h": 870, "dpr": 1}, 2.0)[2], 160)  # never past the dpr
        w, h, _ = display_size({"w": 1366, "h": 980, "dpr": 2}, 2.0)  # a big iPad: held under 2048
        self.assertLessEqual(max(w, h), 2048)
        self.assertEqual((w % 2, h % 2), (0, 0))

    def test_touch_scaled_to_the_new_size(self):
        self.assertEqual(scale_point(100, 200, (660, 1306), (660, 1306)), (100, 200))
        self.assertEqual(scale_point(330, 653, (660, 1306), (1306, 660)), (653, 330))
        self.assertEqual(scale_point(659, 1305, (660, 1306), (100, 100)), (99, 99))

    def test_text_chunks(self):
        self.assertEqual(text_chunks("hi there"), [("text", "hi there")])
        self.assertEqual(text_chunks("a\nb"), [("text", "a"), ("enter", ""), ("text", "b")])
        self.assertEqual(text_chunks("café 🙂!"), [("text", "caf"), ("paste", "é"), ("text", " "),
                                                   ("paste", "🙂"), ("text", "!")])
        long = text_chunks("x" * 650)
        self.assertEqual([len(c[1]) for c in long], [300, 300, 50])


class WebServerBits(unittest.TestCase):
    def test_request_head(self):
        r = parse_head(b"GET /a/abc/?x=1&y= HTTP/1.1\r\nHost: 192.168.0.35:47821\r\nOrigin: http://x\r\n")
        self.assertEqual((r.method, r.path, r.query), ("GET", "/a/abc/", {"x": "1", "y": ""}))
        self.assertEqual(r.headers["host"], "192.168.0.35:47821")
        for bad in (b"GET\r\n", b"GET /x SPDY/3\r\n", b"GET x HTTP/1.1\r\n", b"GET / HTTP/1.1\r\nNoColon\r\n",
                    b"GET / HTTP/1.1\r\n Folded: x\r\n"):
            with self.assertRaises(HttpError):
                parse_head(bad)

    def test_response(self):
        from androiddesktop.remote.server import Response
        raw = response_bytes(Response(200, b"hi"), True, {"X-Frame-Options": "DENY"})
        self.assertTrue(raw.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertIn(b"Content-Length: 2\r\n", raw)
        self.assertIn(b"Cache-Control: no-store\r\n", raw)
        self.assertIn(b"X-Content-Type-Options: nosniff\r\n", raw)
        self.assertTrue(raw.endswith(b"\r\n\r\nhi"))

    def test_choose_lan(self):
        home = Lan("192.168.0.35", ipaddress.ip_network("192.168.0.0/24"), "Wi-Fi", True)
        wsl = Lan("172.21.80.1", ipaddress.ip_network("172.21.80.0/20"), "vEthernet", True)
        cafe = Lan("10.0.0.7", ipaddress.ip_network("10.0.0.0/24"), "Wi-Fi", False)
        self.assertIs(choose_lan([wsl, home], "192.168.0.35"), home)
        self.assertIs(choose_lan([home, wsl], None), home)
        self.assertIsNone(choose_lan([cafe], "10.0.0.7"))  # a Public network: no listening
        self.assertIn("192.168.0.20", home)
        self.assertNotIn("192.168.1.20", home)
        self.assertNotIn("8.8.8.8", home)
        self.assertNotIn("junk", home)


class Routing(unittest.TestCase):
    """The web server's answers, with a stand-in hub."""

    def setUp(self):
        from androiddesktop.remote.server import WebServer

        class FakeApp:
            package, label = "com.google.android.deskclock", "Clock"
            icon = Path("nowhere.png")

        class FakeHub:
            pc_name = "TESTPC"
            certs = None

            def cap(self, package):
                return "CAPCAPCAPCAPCAPC"

            def app_for_cap(self, cap):
                return FakeApp() if cap == "CAPCAPCAPCAPCAPC" else None

        self.server = WebServer(FakeHub(), Lan("192.168.0.35", ipaddress.ip_network("192.168.0.0/24"), "Wi-Fi", True))

    def get(self, path, host="192.168.0.35:47821", **headers):
        req = parse_head(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n".encode() +
                         "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode())
        self.server._check_host(req)
        return self.server.route(req, False, "192.168.0.20")

    def test_pages(self):
        page = self.get("/a/CAPCAPCAPCAPCAPC/")
        self.assertEqual(page.status, 200)
        self.assertIn(b"<title>Clock</title>", page.body)
        self.assertIn(b'href="/a/CAPCAPCAPCAPCAPC/manifest.webmanifest"', page.body)
        manifest = json.loads(self.get("/a/CAPCAPCAPCAPCAPC/manifest.webmanifest").body)
        self.assertEqual((manifest["name"], manifest["start_url"], manifest["display"]),
                         ("Clock", "/a/CAPCAPCAPCAPCAPC/", "standalone"))
        self.assertEqual(self.get("/a/CAPCAPCAPCAPCAPC/icon.png").type, "image/png")
        self.assertEqual(self.get("/").status, 200)

    def test_unknown_apps_and_files(self):
        for path in ("/a/guessed/", "/a/com.google.android.deskclock/", "/s/../server.py", "/s/app.html",
                     "/etc/passwd", "/a/CAPCAPCAPCAPCAPC/other"):
            with self.assertRaises(HttpError, msg=path):
                self.get(path)

    def test_host_must_be_ours(self):
        with self.assertRaises(HttpError):
            self.get("/", host="evil.example:47821")  # DNS rebinding
        self.assertEqual(self.get("/", host="192.168.0.35").status, 200)

    def test_cors_only_for_our_twin(self):
        ok = self.get("/api/ping", Origin="https://192.168.0.35:47821")
        self.assertEqual(ok.headers["Access-Control-Allow-Origin"], "https://192.168.0.35:47821")
        self.assertNotIn("Access-Control-Allow-Origin", self.get("/api/ping", Origin="https://evil.example").headers)

    def test_security_headers(self):
        h = self.server._security_headers(False)
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])


class Firewall(unittest.TestCase):
    RULE = """
Rule Name:                            WinDroid (iPhone and iPad)
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
LocalIP:                              Any
RemoteIP:                             LocalSubnet
Protocol:                             TCP
LocalPort:                            47821
Program:                              C:\\python314\\pythonw.exe
Action:                               Allow
"""

    def test_parse(self):
        self.assertTrue(firewall.parse_allows(self.RULE, r"C:\Python314\pythonw.exe", 47821))
        self.assertFalse(firewall.parse_allows(self.RULE, r"C:\Other\pythonw.exe", 47821))
        self.assertFalse(firewall.parse_allows(self.RULE, r"C:\Python314\pythonw.exe", 8080))
        self.assertFalse(firewall.parse_allows(self.RULE.replace("Yes", "No"), r"C:\Python314\pythonw.exe", 47821))
        self.assertFalse(firewall.parse_allows("No rules match the specified criteria.", "x", 1))

    def test_rule_is_narrow(self):
        args = firewall.add_rule_args(r"C:\Python314\pythonw.exe", 47821)
        for expected in ("dir=in", "action=allow", r"program=C:\Python314\pythonw.exe", "protocol=TCP",
                         "localport=47821", "remoteip=localsubnet", "profile=private"):
            self.assertIn(expected, args)


class Certificates(unittest.TestCase):
    def setUp(self):
        from androiddesktop.remote.certs import Certificates as Certs
        self.dir = Path(tempfile.mkdtemp())
        self.certs = Certs(self.dir, "TESTPC", protect=lambda b: b[::-1], unprotect=lambda b: b[::-1])

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def verify(self, ip):
        from cryptography import x509
        from cryptography.x509.verification import PolicyBuilder, Store
        chain = x509.load_pem_x509_certificates(self.certs.server(ip)[0].read_bytes())
        ca = x509.load_pem_x509_certificate(self.certs.ca_cert_path.read_bytes())
        verifier = PolicyBuilder().store(Store([ca])).build_server_verifier(x509.IPAddress(ipaddress.ip_address(ip)))
        return verifier.verify(chain[0], [])

    def test_server_certificate_verifies_for_its_address(self):
        self.assertEqual(len(self.verify("192.168.0.35")), 2)

    def test_renewed_for_a_new_address_signed_by_the_same_authority(self):
        fp = self.certs.fingerprint()
        self.verify("192.168.0.35")
        self.verify("10.1.2.3")
        self.assertEqual(self.certs.fingerprint(), fp)

    def test_only_private_addresses(self):
        with self.assertRaises(ValueError):
            self.certs.server("8.8.8.8")

    def test_authority_is_name_constrained(self):
        from cryptography import x509
        ca = self.certs.ca()
        nc = ca.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(nc.critical)
        nets = {str(n.value) for n in nc.value.permitted_subtrees if isinstance(n, x509.IPAddress)}
        self.assertEqual(nets, {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"})
        self.assertIn(x509.DNSName("invalid"), nc.value.permitted_subtrees)

    def test_keys_encrypted_at_rest(self):
        self.certs.server("192.168.0.35")
        self.assertIn(b"ENCRYPTED", self.certs.ca_key_path.read_bytes())
        self.assertIn(b"ENCRYPTED", self.certs.key_path.read_bytes())

    def test_profile(self):
        profile = plistlib.loads(self.certs.mobileconfig())
        payload = profile["PayloadContent"][0]
        self.assertEqual(payload["PayloadType"], "com.apple.security.root")
        self.assertEqual(payload["PayloadContent"], self.certs.ca_der())
        self.assertEqual(profile["PayloadDisplayName"], "WinDroid on TESTPC")
        self.assertEqual(plistlib.loads(self.certs.mobileconfig())["PayloadUUID"], profile["PayloadUUID"])  # stable


if __name__ == "__main__":
    unittest.main()
