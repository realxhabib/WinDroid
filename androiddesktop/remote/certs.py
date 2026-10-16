"""The certificates for https://, which Safari needs before it lets a page decode video (WebCodecs).

WinDroid makes its own certificate authority, once, and the user trusts it on their iPhone or iPad with
a configuration profile (see web/app.js for the steps it shows). The authority is name-constrained to
private IP addresses: even if its key were stolen from this PC, it couldn't vouch for any website,
only for addresses on a home network. Its key stays on this PC, encrypted with a DPAPI-protected
password, and signs a server certificate for the PC's current Wi-Fi address (Apple wants server
certificates to last at most 825 days; ours last 397 and are renewed well before).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import plistlib
import secrets
import uuid
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]
CA_YEARS = 10
SERVER_DAYS = 397
RENEW_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Certificates:
    def __init__(self, folder: Path, pc_name: str, protect: Callable[[bytes], bytes] | None = None,
                 unprotect: Callable[[bytes], bytes] | None = None) -> None:
        if protect is None or unprotect is None:
            from . import vault
            protect, unprotect = vault.protect, vault.unprotect
        self.folder = folder
        self.pc_name = pc_name
        self._protect, self._unprotect = protect, unprotect
        self.ca_cert_path = folder / "ca.pem"
        self.ca_key_path = folder / "ca-key.pem"
        self.cert_path = folder / "server.pem"  # the server certificate followed by the CA's
        self.key_path = folder / "server-key.pem"
        self._pass_path = folder / "keys.bin"
        self._fingerprint = ""

    # ------------------------------------------------------------------ keys at rest
    def password(self) -> bytes:
        """Encrypts both private keys on disk; itself DPAPI-encrypted for this Windows user."""
        try:
            return self._unprotect(self._pass_path.read_bytes())
        except (OSError, Exception):
            password = secrets.token_urlsafe(32).encode("ascii")
            self.folder.mkdir(parents=True, exist_ok=True)
            self._pass_path.write_bytes(self._protect(password))
            for old in (self.ca_cert_path, self.ca_key_path, self.cert_path, self.key_path):
                old.unlink(missing_ok=True)  # their keys were under the lost password
            return password

    def _write_key(self, path: Path, key: ec.EllipticCurvePrivateKey) -> None:
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(self.password())))

    def _read_key(self, path: Path) -> ec.EllipticCurvePrivateKey:
        return serialization.load_pem_private_key(path.read_bytes(), self.password())

    # ------------------------------------------------------------------ the authority
    def ca(self) -> x509.Certificate:
        password = self.password()
        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            try:
                serialization.load_pem_private_key(self.ca_key_path.read_bytes(), password)
                return x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
            except (ValueError, TypeError):
                pass
        key = ec.generate_private_key(ec.SECP256R1())
        self._fingerprint = ""
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"WinDroid on {self.pc_name}"[:64]),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WinDroid (this PC only)")])
        now = _now()
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=365 * CA_YEARS))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                             key_encipherment=False, data_encipherment=False, key_agreement=False,
                                             key_cert_sign=True, crl_sign=True, encipher_only=False,
                                             decipher_only=False), critical=True)
                # Private addresses only; and no host names at all (".invalid" matches nothing real).
                .add_extension(x509.NameConstraints(
                    permitted_subtrees=[x509.IPAddress(n) for n in PRIVATE_NETS] + [x509.DNSName("invalid")],
                    excluded_subtrees=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        self.folder.mkdir(parents=True, exist_ok=True)
        self._write_key(self.ca_key_path, key)
        self.ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.cert_path.unlink(missing_ok=True)  # signed by an authority that's gone
        return cert

    def ca_der(self) -> bytes:
        return self.ca().public_bytes(serialization.Encoding.DER)

    def fingerprint(self) -> str:
        """SHA-256 of the authority, as iOS shows it under the profile's details."""
        if not self._fingerprint:
            digest = hashlib.sha256(self.ca_der()).hexdigest().upper()
            self._fingerprint = " ".join(digest[i:i + 2] for i in range(0, len(digest), 2))
        return self._fingerprint

    # ------------------------------------------------------------------ the server's certificate
    def server(self, ip: str) -> tuple[Path, Path, bytes]:
        """(certificate chain, key, key password) for https:// on this address, renewed when needed."""
        address = ipaddress.ip_address(ip)
        if not any(address in n for n in PRIVATE_NETS):
            raise ValueError(f"{ip} isn't a private address")
        ca = self.ca()
        if self._current(address, ca):
            return self.cert_path, self.key_path, self.password()
        ca_key = self._read_key(self.ca_key_path)
        key = ec.generate_private_key(ec.SECP256R1())
        now = _now()
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WinDroid server")]))
                .issuer_name(ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=SERVER_DAYS))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                             key_encipherment=False, data_encipherment=False, key_agreement=True,
                                             key_cert_sign=False, crl_sign=False, encipher_only=False,
                                             decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(address)]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        self._write_key(self.key_path, key)
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) +
                                   ca.public_bytes(serialization.Encoding.PEM))
        return self.cert_path, self.key_path, self.password()

    def _current(self, address, ca: x509.Certificate) -> bool:
        try:
            cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
            self._read_key(self.key_path)
        except (OSError, ValueError, TypeError):
            return False
        try:
            ips = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.IPAddress)
        except x509.ExtensionNotFound:
            return False
        fresh = cert.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_DAYS)
        return cert.issuer == ca.subject and address in ips and fresh

    # ------------------------------------------------------------------ for the iPhone
    def mobileconfig(self) -> bytes:
        """A configuration profile holding the authority: Settings installs it, then the user turns on full
        trust for it under General > About > Certificate Trust Settings."""
        der = self.ca_der()
        fp = hashlib.sha256(der).hexdigest()
        ns = uuid.UUID("6f9b1c4e-3c1a-4a35-9d5e-6b2f7f1a2c10")
        title = f"WinDroid on {self.pc_name}"
        profile = {
            "PayloadContent": [{
                "PayloadCertificateFileName": "windroid.cer",
                "PayloadContent": der,
                "PayloadDescription": "Lets Safari open WinDroid's pages on this PC securely, for smooth video.",
                "PayloadDisplayName": title,
                "PayloadIdentifier": f"app.windroid.ca.{fp[:16]}",
                "PayloadType": "com.apple.security.root",
                "PayloadUUID": str(uuid.uuid5(ns, "ca" + fp)).upper(),
                "PayloadVersion": 1,
            }],
            "PayloadDescription": (f"Trust WinDroid on {self.pc_name}, so your Android apps can stream to Safari. It "
                                   "only vouches for addresses on your home network, never for websites."),
            "PayloadDisplayName": title,
            "PayloadIdentifier": f"app.windroid.profile.{fp[:16]}",
            "PayloadRemovalDisallowed": False,
            "PayloadType": "Configuration",
            "PayloadUUID": str(uuid.uuid5(ns, "profile" + fp)).upper(),
            "PayloadVersion": 1,
        }
        return plistlib.dumps(profile, fmt=plistlib.FMT_XML)
