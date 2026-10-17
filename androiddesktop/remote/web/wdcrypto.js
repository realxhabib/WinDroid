// WinDroid's crypto for the phone: SHA-256, HMAC-SHA256, X25519 and base64url, in plain JavaScript.
// Safari only offers crypto.subtle on https:// pages, and pairing has to work on the http:// one too,
// before any certificate is trusted. Randomness still comes from crypto.getRandomValues, which works
// everywhere. Mirrors androiddesktop/remote/pairing.py.
"use strict";
(function (root) {
  const enc = new TextEncoder();
  const utf8 = (s) => enc.encode(s);

  // ---------------------------------------------------------------- SHA-256 (FIPS 180-4)
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
  const W = new Uint32Array(64);

  function compress(h, block, off) {
    for (let i = 0; i < 16; i++) {
      const j = off + i * 4;
      W[i] = (block[j] << 24) | (block[j + 1] << 16) | (block[j + 2] << 8) | block[j + 3];
    }
    for (let i = 16; i < 64; i++) {
      const a = W[i - 15], b = W[i - 2];
      const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
      const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
      W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
    }
    let [a, b, c, d, e, f, g, k] = h;
    for (let i = 0; i < 64; i++) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const t1 = (k + S1 + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      k = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + k) | 0;
  }

  function sha256(data) {
    const h = new Int32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
      0x1f83d9ab, 0x5be0cd19]);
    const n = data.length;
    const padded = new Uint8Array(((n + 9 + 63) >> 6) << 6);
    padded.set(data);
    padded[n] = 0x80;
    const bits = n * 8;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, Math.floor(bits / 0x100000000));
    view.setUint32(padded.length - 4, bits >>> 0);
    for (let off = 0; off < padded.length; off += 64) compress(h, padded, off);
    const out = new Uint8Array(32);
    const ov = new DataView(out.buffer);
    for (let i = 0; i < 8; i++) ov.setInt32(i * 4, h[i]);
    return out;
  }

  function concat(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let off = 0;
    for (const p of parts) { out.set(p, off); off += p.length; }
    return out;
  }

  function hmac(key, message) {
    if (key.length > 64) key = sha256(key);
    const ipad = new Uint8Array(64), opad = new Uint8Array(64);
    for (let i = 0; i < 64; i++) {
      const k = i < key.length ? key[i] : 0;
      ipad[i] = k ^ 0x36; opad[i] = k ^ 0x5c;
    }
    return sha256(concat(opad, sha256(concat(ipad, message))));
  }

  // HMAC over the parts joined by "|", as pairing.mac() does.
  const mac = (key, ...parts) => hmac(key, utf8(parts.join("|")));

  // ---------------------------------------------------------------- X25519 (RFC 7748)
  const P = (1n << 255n) - 19n;
  const A24 = 121665n;
  const md = (a) => { const r = a % P; return r < 0n ? r + P : r; };

  function powmod(b, e) {
    let r = 1n;
    b = md(b);
    while (e > 0n) {
      if (e & 1n) r = (r * b) % P;
      b = (b * b) % P;
      e >>= 1n;
    }
    return r;
  }

  function leToBig(bytes) {
    let n = 0n;
    for (let i = bytes.length - 1; i >= 0; i--) n = (n << 8n) | BigInt(bytes[i]);
    return n;
  }

  function bigToLe(n) {
    const out = new Uint8Array(32);
    for (let i = 0; i < 32; i++) { out[i] = Number(n & 0xffn); n >>= 8n; }
    return out;
  }

  function x25519(scalar, u) {
    const k = Uint8Array.from(scalar);
    k[0] &= 248; k[31] &= 127; k[31] |= 64;
    const kn = leToBig(k);
    const x1 = leToBig(u) & ((1n << 255n) - 1n);
    let x2 = 1n, z2 = 0n, x3 = x1, z3 = 1n, swap = 0n;
    for (let t = 254; t >= 0; t--) {
      const kt = (kn >> BigInt(t)) & 1n;
      swap ^= kt;
      if (swap) { [x2, x3] = [x3, x2]; [z2, z3] = [z3, z2]; }
      swap = kt;
      const A = md(x2 + z2), AA = (A * A) % P, B = md(x2 - z2), BB = (B * B) % P, E = md(AA - BB);
      const C = md(x3 + z3), D = md(x3 - z3), DA = (D * A) % P, CB = (C * B) % P;
      x3 = md((DA + CB) * (DA + CB));
      z3 = md(x1 * md((DA - CB) * (DA - CB)));
      x2 = (AA * BB) % P;
      z2 = md(E * md(AA + A24 * E));
    }
    if (swap) { [x2, x3] = [x3, x2]; [z2, z3] = [z3, z2]; }
    return bigToLe((x2 * powmod(z2, P - 2n)) % P);
  }

  const BASE = (() => { const b = new Uint8Array(32); b[0] = 9; return b; })();

  function random(n) {
    const out = new Uint8Array(n);
    root.crypto.getRandomValues(out);
    return out;
  }

  function keyPair() {
    const secret = random(32);
    return { secret, pub: x25519(secret, BASE) };
  }

  // ---------------------------------------------------------------- base64url
  function b64e(bytes) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function b64d(text) {
    const s = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
    const out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  function hex(bytes) { return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(""); }

  function equal(a, b) {
    if (a.length !== b.length) return false;
    let d = 0;
    for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
    return d === 0;
  }

  // ---------------------------------------------------------------- WinDroid's protocol
  const transcript = (pid, dpub, spub) => `${pid}|${b64e(dpub)}|${b64e(spub)}`;
  const WD = {
    sha256, hmac, mac, x25519, keyPair, random, b64e, b64d, hex, utf8, equal, BASE,
    pairProof: (token, pid, dpub, spub) => mac(token, "windroid-pair-v1", transcript(pid, dpub, spub)),
    sasCode(shared, pid, dpub, spub) {
      const m = mac(shared, "windroid-sas-v1", transcript(pid, dpub, spub));
      const n = ((m[0] << 24) | (m[1] << 16) | (m[2] << 8) | m[3]) >>> 0;
      return String(n % 1000000).padStart(6, "0");
    },
    deviceSecret: (shared, deviceId) => mac(shared, "windroid-device-v1", deviceId),
    authProof: (secret, nonce, cnonce) => mac(secret, "windroid-auth-v1", nonce, cnonce),
    serverProof: (secret, nonce, cnonce) => mac(secret, "windroid-server-v1", nonce, cnonce),
    sessionKey: (secret, nonce, cnonce) => mac(secret, "windroid-session-v1", nonce, cnonce),
    seal: (key, seq, body) => `${seq}.${b64e(mac(key, "windroid-msg-v1", String(seq), body).slice(0, 16))}.${body}`,
  };

  root.WDCrypto = WD;
  if (typeof module !== "undefined") module.exports = WD;
})(typeof globalThis !== "undefined" ? globalThis : this);
