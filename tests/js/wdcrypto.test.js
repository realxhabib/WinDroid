// The phone's crypto (androiddesktop/remote/web/wdcrypto.js) against Node's and the RFC 7748 vectors,
// plus values computed by pairing.py (passed in as JSON by tests/test_remote_pairing.py).
// Run: node tests/js/wdcrypto.test.js [vectors.json]
"use strict";
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const WD = require(path.join(__dirname, "..", "..", "androiddesktop", "remote", "web", "wdcrypto.js"));

const h2b = (h) => Uint8Array.from(Buffer.from(h, "hex"));
let failures = 0;
function check(name, got, want) {
  if (got !== want) { failures++; console.log(`FAIL ${name}: got ${got}, want ${want}`); }
}

for (const n of [0, 1, 55, 56, 63, 64, 65, 1000]) {
  const d = crypto.randomBytes(n);
  check(`sha256 ${n}`, WD.hex(WD.sha256(d)), crypto.createHash("sha256").update(d).digest("hex"));
}
for (const n of [0, 16, 64, 80]) {
  const key = crypto.randomBytes(n), m = crypto.randomBytes(100);
  check(`hmac key ${n}`, WD.hex(WD.hmac(key, m)), crypto.createHmac("sha256", key).update(m).digest("hex"));
}
// RFC 7748 section 5.2 and 6.1
check("x25519 5.2", WD.hex(WD.x25519(h2b("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
  h2b("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c"))),
  "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552");
const a = h2b("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a");
const b = h2b("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb");
check("x25519 alice", WD.hex(WD.x25519(a, WD.BASE)), "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a");
check("x25519 shared", WD.hex(WD.x25519(a, WD.x25519(b, WD.BASE))),
  "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742");
check("b64 round trip", WD.hex(WD.b64d(WD.b64e(h2b("00ff10fe7f")))), "00ff10fe7f");

// Values from pairing.py: the phone must compute exactly what the PC does.
if (process.argv[2]) {
  const v = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const shared = WD.x25519(WD.b64d(v.device_secret_key), WD.b64d(v.server_pub));
  check("shared", WD.b64e(shared), v.shared);
  const dpub = WD.b64d(v.device_pub), spub = WD.b64d(v.server_pub);
  check("pair proof", WD.b64e(WD.pairProof(WD.b64d(v.token), v.pid, dpub, spub)), v.pair_proof);
  check("sas", WD.sasCode(shared, v.pid, dpub, spub), v.sas);
  const secret = WD.deviceSecret(shared, v.device_id);
  check("device secret", WD.b64e(secret), v.secret);
  check("auth proof", WD.b64e(WD.authProof(secret, v.nonce, v.cnonce)), v.auth_proof);
  check("server proof", WD.b64e(WD.serverProof(secret, v.nonce, v.cnonce)), v.server_proof);
  check("seal", WD.seal(WD.sessionKey(secret, v.nonce, v.cnonce), 7, v.body), v.sealed);
}

if (failures) { console.log(`${failures} failed`); process.exit(1); }
console.log("ok");
