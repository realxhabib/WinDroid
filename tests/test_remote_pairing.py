"""Pairing, tokens and authentication for iPhones and iPads (remote/pairing.py), and the same values
computed by the phone's JavaScript (remote/web/wdcrypto.js, run with Node when it's installed)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from androiddesktop.remote import pairing
from androiddesktop.remote.pairing import (MessageVerifier, Pairing, PairingError, auth_proof, b64d, b64e,
                                           device_secret, pair_proof, sas_code, seal, server_proof, session_key)

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class Phone:
    """What the web app does, in Python."""

    def __init__(self) -> None:
        self.key = X25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes_raw()

    def start(self, p: Pairing, ip: str = "192.168.0.20", name: str = "iPhone") -> dict:
        r = p.start(b64e(self.pub), name, ip)
        self.pid, self.server_pub = r["id"], b64d(r["pub"])
        self.shared = self.key.exchange(X25519PublicKey.from_public_bytes(self.server_pub))
        return r

    def proof(self, token: str) -> str:
        return b64e(pair_proof(b64d(token), self.pid, self.pub, self.server_pub))


def make(folder: Path, clock: Clock | None = None) -> Pairing:
    # No DPAPI in tests: an obvious stand-in that still changes the bytes.
    return Pairing(folder / "devices.json", protect=lambda b: b"P" + b[::-1], unprotect=lambda b: b[1:][::-1],
                   clock=clock or Clock())


class PairingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.clock = Clock()
        self.p = make(self.dir, self.clock)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def pair(self, phone: Phone | None = None) -> tuple[Phone, dict]:
        phone = phone or Phone()
        token = self.p.offer()
        phone.start(self.p)
        return phone, self.p.finish(phone.pid, phone.proof(token))

    def test_qr_token_pairs_and_both_sides_agree_on_the_secret(self):
        phone, done = self.pair()
        device = self.p.get(done["device"])
        self.assertEqual(device.secret, device_secret(phone.shared, done["device"]))
        self.assertEqual(device.via, "qr")

    def test_token_works_once(self):
        token = self.p.offer()
        a, b = Phone(), Phone()
        a.start(self.p)
        self.p.finish(a.pid, a.proof(token))
        b.start(self.p)
        with self.assertRaises(PairingError):
            self.p.finish(b.pid, b.proof(token))

    def test_token_expires(self):
        token = self.p.offer()
        phone = Phone()
        self.clock.t += pairing.OFFER_SECS + 1
        phone.start(self.p)
        with self.assertRaises(PairingError):
            self.p.finish(phone.pid, phone.proof(token))

    def test_withdrawn_token_fails(self):
        token = self.p.offer()
        self.p.withdraw(token)
        phone = Phone()
        phone.start(self.p)
        with self.assertRaises(PairingError):
            self.p.finish(phone.pid, phone.proof(token))

    def test_wrong_token_fails_and_uses_up_the_attempt(self):
        self.p.offer()
        phone = Phone()
        phone.start(self.p)
        with self.assertRaises(PairingError):
            self.p.finish(phone.pid, phone.proof(b64e(b"x" * 16)))
        with self.assertRaises(PairingError):  # no second guess on the same key exchange
            self.p.finish(phone.pid, phone.proof(b64e(b"y" * 16)))
        self.assertEqual(self.p.devices(), [])

    def test_swapped_keys_fail(self):
        """A man in the middle who swaps the server's key can't produce a proof the server accepts."""
        token = self.p.offer()
        phone = Phone()
        phone.start(self.p)
        phone.server_pub = X25519PrivateKey.generate().public_key().public_bytes_raw()  # the attacker's key
        with self.assertRaises(PairingError):
            self.p.finish(phone.pid, phone.proof(token))

    def test_bad_public_key_rejected(self):
        with self.assertRaises(PairingError):
            self.p.start("not base64!!", "iPhone", "192.168.0.20")
        with self.assertRaises(PairingError):
            self.p.start(b64e(b"\x00" * 31), "iPhone", "192.168.0.20")
        with self.assertRaises(PairingError):  # a low-order point gives an all-zero shared key
            self.p.start(b64e(b"\x00" * 32), "iPhone", "192.168.0.20")

    def test_rate_limited_per_address(self):
        for _ in range(pairing.STARTS_PER_MINUTE):
            Phone().start(self.p, ip="192.168.0.66")
        with self.assertRaises(PairingError):
            Phone().start(self.p, ip="192.168.0.66")
        Phone().start(self.p, ip="192.168.0.67")  # someone else is fine
        self.clock.t += 61
        Phone().start(self.p, ip="192.168.0.66")

    def test_approval_flow(self):
        phone = Phone()
        phone.start(self.p, name="iPhone (Clock icon)")
        self.assertEqual(self.p.finish(phone.pid), {"pending": True})
        asks = self.p.approvals()
        self.assertEqual(len(asks), 1)
        # The launcher shows the same code the phone works out for itself.
        self.assertEqual(asks[0]["code"], sas_code(phone.shared, phone.pid, phone.pub, phone.server_pub))
        self.assertEqual(self.p.status(phone.pid)["state"], "approve")
        self.assertTrue(self.p.approve(asks[0]["id"], True))
        st = self.p.status(phone.pid)
        self.assertEqual(st["state"], "approved")
        self.assertEqual(self.p.get(st["device"]).secret, device_secret(phone.shared, st["device"]))
        self.assertEqual(self.p.approvals(), [])

    def test_denied_and_expired_approvals(self):
        a, b = Phone(), Phone()
        a.start(self.p)
        self.p.finish(a.pid)
        self.p.approve(a.pid, False)
        self.assertEqual(self.p.status(a.pid)["state"], "denied")
        self.assertEqual(self.p.devices(), [])
        b.start(self.p)
        self.p.finish(b.pid)
        self.clock.t += pairing.APPROVAL_SECS + 1
        self.assertEqual(self.p.status(b.pid)["state"], "expired")
        self.assertFalse(self.p.approve(b.pid, True))

    def test_too_many_waiting_approvals(self):
        for _ in range(pairing.MAX_APPROVALS):
            ph = Phone()
            ph.start(self.p)
            self.p.finish(ph.pid)
        ph = Phone()
        ph.start(self.p)
        with self.assertRaises(PairingError):
            self.p.finish(ph.pid)

    def test_upgrade_token_joins_the_same_group(self):
        _, done = self.pair()
        first = self.p.get(done["device"])
        token = self.p.offer("upgrade", group=first.group)
        phone = Phone()
        phone.start(self.p, name="something else")
        second = self.p.get(self.p.finish(phone.pid, phone.proof(token))["device"])
        self.assertEqual(second.group, first.group)
        self.assertEqual(second.name, first.name)
        self.assertEqual(second.via, "upgrade")
        self.assertEqual(len(self.p.groups()), 1)
        self.assertTrue(self.p.remove(first.group))  # removing the device removes all its keys
        self.assertEqual(self.p.devices(), [])

    def test_saved_and_loaded(self):
        _, done = self.pair()
        secret = self.p.get(done["device"]).secret
        raw = (self.dir / "devices.json").read_text()
        self.assertNotIn(b64e(secret), raw)
        again = make(self.dir)
        self.assertEqual(again.get(done["device"]).secret, secret)
        self.assertEqual(again.key, self.p.key)

    def test_unreadable_file_starts_afresh(self):
        (self.dir / "devices.json").write_text("{not json")
        self.assertEqual(make(self.dir).devices(), [])

    def test_names_cleaned(self):
        self.assertEqual(pairing.clean_name("  iPhone\x00\n  of\tSam  "), "iPhone of Sam")
        self.assertEqual(pairing.clean_name(""), pairing.DEFAULT_NAME)
        self.assertEqual(len(pairing.clean_name("x" * 200)), 40)


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.p = make(self.dir)
        token = self.p.offer()
        self.phone = Phone()
        self.phone.start(self.p)
        self.device = self.p.get(self.p.finish(self.phone.pid, self.phone.proof(token))["device"])

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_challenge_response(self):
        nonce, cnonce = pairing.nonce(), pairing.nonce()
        secret = device_secret(self.phone.shared, self.device.id)
        ok = self.p.authenticate(self.device.id, nonce, cnonce, b64e(auth_proof(secret, nonce, cnonce)))
        self.assertIs(ok, self.device)
        # Replaying the answer to another challenge fails, as do wrong ids, secrets and junk.
        self.assertIsNone(self.p.authenticate(self.device.id, pairing.nonce(), cnonce,
                                              b64e(auth_proof(secret, nonce, cnonce))))
        self.assertIsNone(self.p.authenticate("nobody", nonce, cnonce, b64e(auth_proof(secret, nonce, cnonce))))
        self.assertIsNone(self.p.authenticate(self.device.id, nonce, cnonce, b64e(auth_proof(b"x" * 32, nonce, cnonce))))
        self.assertIsNone(self.p.authenticate(self.device.id, nonce, cnonce, "!!"))
        self.assertIsNone(self.p.authenticate(self.device.id, nonce, "short", b64e(b"x" * 32)))
        self.assertIsNone(self.p.authenticate(self.device.id, nonce, None, None))

    def test_removed_device_is_refused(self):
        nonce, cnonce = pairing.nonce(), pairing.nonce()
        proof = b64e(auth_proof(self.device.secret, nonce, cnonce))
        self.p.remove(self.device.group)
        self.assertIsNone(self.p.authenticate(self.device.id, nonce, cnonce, proof))

    def test_server_proves_itself_too(self):
        self.assertNotEqual(server_proof(self.device.secret, "a", "b"), auth_proof(self.device.secret, "a", "b"))

    def test_signed_messages(self):
        key = session_key(self.device.secret, "n", "c")
        v = MessageVerifier(key)
        self.assertEqual(v.open(seal(key, 1, '{"t":"ping","c":1}')), '{"t":"ping","c":1}')
        self.assertEqual(v.open(seal(key, 5, '{"t":"apps"}')), '{"t":"apps"}')
        with self.assertRaises(PairingError):
            v.open(seal(key, 5, '{"t":"apps"}'))  # replayed
        with self.assertRaises(PairingError):
            v.open(seal(key, 3, '{"t":"apps"}'))  # out of order
        with self.assertRaises(PairingError):
            v.open(seal(b"k" * 32, 9, '{"t":"apps"}'))  # someone else's key
        with self.assertRaises(PairingError):
            v.open(seal(key, 10, '{"t":"apps"}').replace("apps", "app2"))  # tampered
        with self.assertRaises(PairingError):
            v.open('{"t":"apps"}')  # not signed at all


@unittest.skipUnless(shutil.which("node"), "Node.js isn't installed")
class SameOnThePhone(unittest.TestCase):
    """wdcrypto.js computes exactly what pairing.py does (and passes RFC 7748's test vectors)."""

    def test_javascript_matches(self):
        p = make(Path(tempfile.mkdtemp()))
        token = p.offer()
        phone = Phone()
        phone.start(p)
        done = p.finish(phone.pid, phone.proof(token))
        secret = p.get(done["device"]).secret
        nonce, cnonce, body = pairing.nonce(), pairing.nonce(), '{"t":"touch","w":10,"h":10,"p":[["down",0,1,2]]}'
        vectors = {
            "device_secret_key": b64e(phone.key.private_bytes_raw()), "device_pub": b64e(phone.pub),
            "server_pub": b64e(phone.server_pub), "shared": b64e(phone.shared), "token": token,
            "pid": phone.pid, "pair_proof": phone.proof(token),
            "sas": sas_code(phone.shared, phone.pid, phone.pub, phone.server_pub),
            "device_id": done["device"], "secret": b64e(secret), "nonce": nonce, "cnonce": cnonce,
            "auth_proof": b64e(auth_proof(secret, nonce, cnonce)),
            "server_proof": b64e(server_proof(secret, nonce, cnonce)), "body": body,
            "sealed": seal(session_key(secret, nonce, cnonce), 7, body),
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(vectors, f)
        out = subprocess.run(["node", str(ROOT / "tests" / "js" / "wdcrypto.test.js"), f.name],
                             capture_output=True, text=True, timeout=60)
        Path(f.name).unlink()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
