// WinDroid on an iPhone or iPad: one Android app from the PC, full screen, by touch.
//
// Pairs once (QR code token, or approval on the PC), proves itself on every connection, then shows the
// app's video: decoded here with WebCodecs on https:// (after the PC's certificate is trusted), or as
// JPEG frames the PC decodes for us over plain http://. Touches, keys and text go back signed.
"use strict";
(() => {
  const WD = window.WDCrypto;
  const $ = (id) => document.getElementById(id);
  const cfg = JSON.parse(document.querySelector('meta[name="wd-config"]').content);
  const secure = location.protocol === "https:";
  const standalone = navigator.standalone === true || matchMedia("(display-mode: standalone)").matches;
  const STORE = "windroid.v1";
  const now = () => performance.timeOrigin + performance.now();

  // ------------------------------------------------------------ small helpers
  function show(id) {
    for (const s of ["pair", "wait", "lobby", "player"]) $(s).hidden = s !== id;
  }
  let toastTimer = 0;
  function toast(text, ms = 2600) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function waiting(text) { $("wait-text").textContent = text; show("wait"); }
  function load() {
    try { const c = JSON.parse(localStorage.getItem(STORE)); return c && c.device && c.secret ? c : null; } catch { return null; }
  }
  function save(c) { try { localStorage.setItem(STORE, JSON.stringify(c)); } catch {} }
  function forget() { try { localStorage.removeItem(STORE); } catch {} }
  function deviceName() {
    const ua = navigator.userAgent;
    const kind = /iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) ? "iPad"
      : /iPhone/.test(ua) ? "iPhone" : /Android/.test(ua) ? "Android phone" : "Browser";
    return standalone ? `${kind} (${cfg.label || "WinDroid"} icon)` : kind;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), cache: "no-store" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `The PC said ${r.status}`);
    return data;
  }
  function timeout(ms) { return new Promise((_, no) => setTimeout(() => no(new Error("timeout")), ms)); }

  // ------------------------------------------------------------ pairing
  let creds = load();

  async function exchange() {
    const kp = WD.keyPair();
    const start = await post("/api/pair/start", { pub: WD.b64e(kp.pub), name: deviceName() });
    const spub = WD.b64d(start.pub);
    return { id: start.id, kp, spub, shared: WD.x25519(kp.secret, spub) };
  }

  async function pairWithToken(token) {
    waiting("Pairing...");
    const x = await exchange();
    const proof = WD.pairProof(WD.b64d(token), x.id, x.kp.pub, x.spub);
    const done = await post("/api/pair/finish", { id: x.id, proof: WD.b64e(proof) });
    keep(x, done.device, done.name);
  }

  function keep(x, device, name) {
    creds = { device, name, secret: WD.b64e(WD.deviceSecret(x.shared, device)), pc: cfg.pc };
    save(creds);
  }

  async function pairWithApproval() {
    $("pair-error").hidden = true;
    $("pair-ask").disabled = true;
    try {
      const x = await exchange();
      $("pair-digits").textContent = WD.sasCode(x.shared, x.id, x.kp.pub, x.spub).replace(/(\d{3})/, "$1 ");
      await post("/api/pair/finish", { id: x.id });
      $("pair-code").hidden = false;
      $("pair-ask").hidden = true;
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500));
        const st = await (await fetch(`/api/pair/status?id=${encodeURIComponent(x.id)}`, { cache: "no-store" })).json();
        if (st.state === "approved") { keep(x, st.device, st.name); start(); return; }
        if (st.state !== "approve") throw new Error(st.state === "denied" ? "The PC said no." : "That took too long. Try again.");
      }
    } catch (e) {
      $("pair-error").textContent = e.message;
      $("pair-error").hidden = false;
      $("pair-code").hidden = true;
      $("pair-ask").hidden = false;
    } finally {
      $("pair-ask").disabled = false;
    }
  }
  $("pair-ask").addEventListener("click", pairWithApproval);

  function showPair(message) {
    show("pair");
    $("pair-code").hidden = true;
    $("pair-ask").hidden = false;
    $("pair-error").textContent = message || "";
    $("pair-error").hidden = !message;
  }

  // https:// works once the device trusts the PC's certificate; until then fetch() fails at once.
  async function httpsWorks() {
    try {
      const r = await Promise.race([fetch(`https://${cfg.host}/api/ping`, { cache: "no-store", mode: "cors" }), timeout(2500)]);
      return r.ok;
    } catch { return false; }
  }

  // ------------------------------------------------------------ the connection
  let ws = null, key = null, seq = 0, online = false, retry = 0, retryTimer = 0, wanted = cfg.app;
  let upgrading = false;

  function send(obj) {
    if (ws && ws.readyState === 1 && key) ws.send(WD.seal(key, ++seq, JSON.stringify(obj)));
  }

  function connect() {
    clearTimeout(retryTimer);
    if (ws) { ws.onclose = null; ws.close(); }
    const sock = new WebSocket(`${secure ? "wss" : "ws"}://${cfg.host}/ws`);
    sock.binaryType = "arraybuffer";
    ws = sock;
    key = null;
    seq = 0;
    let nonce = "", cnonce = "";
    const slow = setTimeout(() => { if (!online) waiting(`Connecting to ${cfg.pc}...`); }, 1200);
    sock.onmessage = (ev) => {
      if (typeof ev.data !== "string") { video(ev.data); return; }
      const m = JSON.parse(ev.data);
      if (m.t === "hello") {
        nonce = m.nonce;
        cnonce = WD.b64e(WD.random(18));
        const secret = WD.b64d(creds.secret);
        sock.send(JSON.stringify({ t: "auth", device: creds.device, cnonce, proof: WD.b64e(WD.authProof(secret, nonce, cnonce)) }));
      } else if (m.t === "welcome") {
        const secret = WD.b64d(creds.secret);
        if (!WD.equal(WD.b64d(m.proof), WD.serverProof(secret, nonce, cnonce))) {
          sock.close();
          showPair("That isn't the PC this device paired with.");
          return;
        }
        key = WD.sessionKey(secret, nonce, cnonce);
        online = true;
        retry = 0;
        clearTimeout(slow);
        welcomed();
      } else if (m.t === "denied") {
        forget();
        creds = null;
        online = false;
        sock.onclose = null;
        showPair(m.message);
      } else {
        handle(m);
      }
    };
    sock.onclose = () => {
      clearTimeout(slow);
      online = false;
      key = null;
      if (ws !== sock) return;
      const delay = [300, 1000, 2000, 3000, 5000][Math.min(retry++, 4)];
      if (retry > 2) waiting(`Reconnecting to ${cfg.pc}...`);
      retryTimer = setTimeout(connect, delay);
    };
  }

  function welcomed() {
    if (upgrading) { send({ t: "upgrade" }); return; }
    pingClock();
    if (wanted) openApp(wanted);
    else { send({ t: "apps" }); show("lobby"); }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && creds && (!ws || ws.readyState > 1)) { retry = 0; connect(); }
  });

  // Clock offset between the PC and us, for the latency shown in the chip.
  let offset = 0, rtt = 0;
  function pingClock() { send({ t: "ping", c: now() }); }
  setInterval(() => { if (online) pingClock(); }, 5000);

  // ------------------------------------------------------------ messages from the PC
  const app = { cap: null, label: "", state: "", mode: "", device: "", secure: false };

  function handle(m) {
    switch (m.t) {
      case "pong": {
        const t = now();
        rtt = t - m.c;
        offset = m.s - (m.c + t) / 2;
        break;
      }
      case "apps": renderApps(m.apps); break;
      case "app":
        Object.assign(app, { cap: m.app, label: m.label, state: m.state, mode: m.mode, device: m.device, secure: m.secure });
        setHead(m.app, m.label);
        refreshOverlay();
        break;
      case "video": configure(m.codec); break;
      case "go":
        toast(`${m.label} is on this ${deviceName().split(" ")[0]} now`);
        openApp(m.app);
        break;
      case "state":
        if (m.app === app.cap) { app.state = m.state; refreshOverlay(); }
        break;
      case "secure": app.secure = m.on; refreshOverlay(); break;
      case "status": overlay(m.message, []); break;
      case "error": toast(m.message, 4500); if (app.state === "starting") { app.state = "error"; refreshOverlay(); } break;
      case "upgrade":
        location.replace(`https://${cfg.host}${location.pathname}#up=${encodeURIComponent(m.token)}`);
        break;
    }
  }

  // The head carries the app's name and icon, so "Add to Home Screen" names the icon after it.
  function setHead(cap, label) {
    if (!cap) return;
    const base = `/a/${cap}/`;
    document.title = label;
    document.querySelector('meta[name="apple-mobile-web-app-title"]').content = label;
    document.querySelector('link[rel="apple-touch-icon"]').href = `${base}icon.png`;
    document.querySelector('link[rel="manifest"]').href = `${base}manifest.webmanifest`;
    $("overlay-icon").src = `${base}icon.png`;
    if (!standalone && location.pathname !== base) history.replaceState(null, "", base);
  }

  // ------------------------------------------------------------ the app list
  function renderApps(apps) {
    const grid = $("grid");
    grid.innerHTML = "";
    $("lobby-pc").textContent = `On ${cfg.pc}`;
    for (const a of apps) {
      const b = document.createElement("button");
      b.className = "tile";
      b.innerHTML = '<img alt=""><span></span>';
      b.querySelector("img").src = a.icon;
      b.querySelector("span").textContent = a.label;
      if (a.where) {
        const i = document.createElement("i");
        i.textContent = a.where === "here" ? "here" : a.where === "pc" ? "PC" : "away";
        if (a.where === "pc") i.className = "pc";
        b.appendChild(i);
      }
      b.addEventListener("click", () => openApp(a.app));
      grid.appendChild(b);
    }
  }

  // ------------------------------------------------------------ playing an app
  let mode = null;  // "webcodecs" or "jpeg"
  async function pickDecoder() {
    if (mode) return mode;
    mode = "jpeg";
    if (window.isSecureContext && "VideoDecoder" in window) {
      try {
        const r = await VideoDecoder.isConfigSupported({ codec: "avc1.42C02A", optimizeForLatency: true });
        if (r.supported) mode = "webcodecs";
      } catch {}
    }
    return mode;
  }

  async function openApp(cap) {
    wanted = cap;
    app.cap = cap;
    app.state = "starting";
    show("player");
    refreshOverlay();
    await pickDecoder();
    layout();
    send({ t: "open", app: cap, decoder: mode, view: view() });
    chipShow();
  }

  function view() {
    const ins = getComputedStyle($("insets"));
    const px = (v) => parseFloat(v) || 0;
    const nav = document.body.classList.contains("nonav") ? 0 : 44;
    const w = innerWidth - px(ins.paddingLeft) - px(ins.paddingRight);
    const h = innerHeight - px(ins.paddingTop) - px(ins.paddingBottom) - nav;
    return { w: Math.max(120, Math.round(w)), h: Math.max(120, Math.round(h)), dpr: devicePixelRatio || 2 };
  }

  let lastView = "";
  let viewTimer = 0;
  function viewChanged() {
    layout();
    clearTimeout(viewTimer);
    viewTimer = setTimeout(() => {
      const v = view();
      const k = `${v.w}x${v.h}`;
      if (k !== lastView && online && !$("player").hidden) { lastView = k; send({ t: "view", ...v }); }
    }, 200);
  }
  addEventListener("resize", viewChanged);
  addEventListener("orientationchange", () => setTimeout(viewChanged, 300));

  const canvas = $("screen");
  const ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
  function layout() {
    const st = $("stage").getBoundingClientRect();
    const w = canvas.width || 1, h = canvas.height || 1;
    const s = Math.min(st.width / w, st.height / h);
    canvas.style.width = `${Math.floor(w * s)}px`;
    canvas.style.height = `${Math.floor(h * s)}px`;
  }

  // frames drawn, for the chip
  let frames = 0, fps = 0, latency = 0;
  setInterval(() => { fps = frames; frames = 0; chipText(); }, 1000);

  function draw(src, w, h, sentMs) {
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      layout();
    }
    ctx.drawImage(src, 0, 0, w, h);  // VideoFrame or ImageBitmap
    frames++;
    latency = latency ? latency * 0.85 + (now() + offset - sentMs) * 0.15 : now() + offset - sentMs;
    if (app.state === "starting" && app.cap) { app.state = "live"; refreshOverlay(); }
  }

  // WebCodecs
  let decoder = null, needKey = true, ts = 0, codec = "", bitmaps = false;
  const pending = [];
  function configure(c) {
    if (mode !== "webcodecs") return;
    codec = c;
    if (!decoder || decoder.state === "closed") {
      decoder = new VideoDecoder({
        output: (frame) => {
          const sent = pending.shift() || now() - offset;
          if (bitmaps) {  // a Safari that can't draw a VideoFrame directly
            createImageBitmap(frame).then((b) => draw(b, b.width, b.height, sent)).finally(() => frame.close());
            return;
          }
          try { draw(frame, frame.displayWidth, frame.displayHeight, sent); }
          catch (e) { bitmaps = true; console.warn("drawImage(VideoFrame)", e); }
          finally { if (!bitmaps) frame.close(); }
          if (bitmaps) createImageBitmap(frame).then((b) => draw(b, b.width, b.height, sent)).finally(() => frame.close());
        },
        error: (e) => { console.warn("decoder", e); decoder = null; needKey = true; send({ t: "keyframe" }); },
      });
    }
    decoder.configure({ codec, optimizeForLatency: true, hardwareAcceleration: "prefer-hardware" });
    needKey = true;
    pending.length = 0;
  }

  function video(buf) {
    const dv = new DataView(buf);
    const kind = dv.getUint8(0), sent = dv.getFloat64(1);
    const data = new Uint8Array(buf, 9);
    if (kind === 3) { jpeg(data, sent); return; }
    if (!decoder || decoder.state !== "configured") { if (codec) configure(codec); else return; }
    if (kind === 2 && needKey) return;
    if (kind === 2 && decoder.decodeQueueSize > 3) {  // we fell behind: skip to a fresh key frame
      needKey = true;
      send({ t: "keyframe" });
      return;
    }
    needKey = false;
    try {
      pending.push(sent);
      decoder.decode(new EncodedVideoChunk({ type: kind === 1 ? "key" : "delta", timestamp: (ts += 16666), data }));
    } catch (e) {
      console.warn("decode", e);
      needKey = true;
      send({ t: "keyframe" });
    }
  }

  // JPEG frames: one at a time, acknowledged once drawn, so a slow device skips frames instead of lagging.
  let jpegBusy = false;
  async function jpeg(data, sent) {
    if (jpegBusy) return;
    jpegBusy = true;
    try {
      const bmp = await createImageBitmap(new Blob([data], { type: "image/jpeg" }));
      draw(bmp, bmp.width, bmp.height, sent);
      bmp.close && bmp.close();
    } catch (e) {
      console.warn("jpeg", e);
    } finally {
      jpegBusy = false;
      send({ t: "ack" });
    }
  }

  // ------------------------------------------------------------ overlays and the chip
  function overlay(text, buttons) {
    $("overlay").hidden = !text;
    if (!text) return;
    $("overlay-text").textContent = text;
    const box = $("overlay-buttons");
    box.innerHTML = "";
    for (const [label, fn, cls] of buttons) {
      const b = document.createElement("button");
      b.className = cls || "ghost";
      b.textContent = label;
      b.addEventListener("click", fn);
      box.appendChild(b);
    }
  }

  function refreshOverlay() {
    const name = app.label || "The app";
    const again = ["Open it here", () => openApp(app.cap), "primary"];
    const lobby = ["All apps", showLobby];
    if (app.secure) {
      overlay(`Android hides this screen (a PIN, password or payment screen) from other devices. Finish it on ${cfg.pc}.`,
        [["Bring back to PC", () => send({ t: "back_to_pc" }), "primary"]]);
    } else if (app.state === "starting") {
      overlay(`Opening ${name}...`, []);
    } else if (app.state === "pc") {
      overlay(`${name} is back on ${cfg.pc}.`, [again, lobby]);
    } else if (app.state === "closed") {
      overlay(`${name} was closed.`, [again, lobby]);
    } else if (app.state === "error") {
      overlay(`${name} stopped on this device.`, [["Try again", () => openApp(app.cap), "primary"], lobby]);
    } else {
      overlay("", []);
    }
  }

  let chipTimer = 0;
  function chipShow(ms = 5000) {
    const c = $("chip");
    c.hidden = false;
    c.classList.remove("fade");
    clearTimeout(chipTimer);
    chipTimer = setTimeout(() => c.classList.add("fade"), ms);
    chipText();
  }
  function chipText() {
    if ($("chip").hidden) return;
    const what = mode === "webcodecs" ? "WebCodecs" : "JPEG";
    const lat = latency > 0 && latency < 5000 ? ` · ~${Math.round(latency)} ms` : "";
    $("chip").innerHTML = `<b>${what}</b> · ${fps} fps${lat}`;
  }

  // ------------------------------------------------------------ touch, keys, text
  const pointers = new Map();  // pointerId -> {slot, edge, x0, y0}
  function slot() {
    const used = new Set([...pointers.values()].map((p) => p.slot));
    for (let i = 0; i < 10; i++) if (!used.has(i)) return i;
    return -1;
  }
  function point(e) {
    const r = canvas.getBoundingClientRect();
    return [Math.round((e.clientX - r.left) * canvas.width / r.width), Math.round((e.clientY - r.top) * canvas.height / r.height)];
  }
  function sendTouch(action, slotId, x, y, pressure) {
    send({ t: "touch", w: canvas.width, h: canvas.height, p: [[action, slotId, x, y, pressure]] });
  }
  const pressure = (e) => (e.pointerType === "mouse" ? 1 : e.pressure > 0 ? Math.min(1, e.pressure) : 1);
  const EDGE = 16;

  canvas.addEventListener("pointerdown", (e) => {
    if (!canvas.width) return;
    e.preventDefault();
    const s = slot();
    if (s < 0) return;
    canvas.setPointerCapture(e.pointerId);
    // A swipe in from the screen's left or right edge is Android's Back gesture.
    const edge = e.pointerType === "touch" && (e.clientX < EDGE || e.clientX > innerWidth - EDGE) && pointers.size === 0;
    const p = { slot: s, edge, x0: e.clientX, y0: e.clientY, down: !edge };
    pointers.set(e.pointerId, p);
    if (!edge) { const [x, y] = point(e); sendTouch("down", s, x, y, pressure(e)); }
  }, { passive: false });

  canvas.addEventListener("pointermove", (e) => {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    e.preventDefault();
    if (p.edge) {
      if (Math.abs(e.clientY - p.y0) > 40 && Math.abs(e.clientX - p.x0) < 20) {  // a vertical drag after all
        p.edge = false; p.down = true;
        const [x, y] = point(e);
        sendTouch("down", p.slot, x, y, pressure(e));
      }
      return;
    }
    const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    const last = events[events.length - 1] || e;
    const [x, y] = point(last);
    sendTouch("move", p.slot, x, y, pressure(last));
  }, { passive: false });

  function lift(e) {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    pointers.delete(e.pointerId);
    if (p.edge) {
      if (Math.abs(e.clientX - p.x0) > 50) send({ t: "nav", k: "back" });
      else {  // just a tap near the edge
        const [x, y] = point(e);
        sendTouch("down", p.slot, x, y, 1);
        sendTouch("up", p.slot, x, y, 0);
      }
      return;
    }
    const [x, y] = point(e);
    sendTouch("up", p.slot, x, y, 0);
  }
  canvas.addEventListener("pointerup", lift);
  canvas.addEventListener("pointercancel", lift);
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  document.addEventListener("gesturestart", (e) => e.preventDefault());  // Safari's own pinch zoom
  document.addEventListener("dblclick", (e) => e.preventDefault());

  // A trackpad or mouse wheel on an iPad.
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const [x, y] = point(e);
    const unit = e.deltaMode === 1 ? 1 : 1 / 40;
    send({ t: "scroll", w: canvas.width, h: canvas.height, x: Math.max(0, Math.min(canvas.width - 1, x)),
      y: Math.max(0, Math.min(canvas.height - 1, y)), dx: -e.deltaX * unit, dy: -e.deltaY * unit });
  }, { passive: false });

  const kbd = $("kbd");
  function typing() { return document.activeElement === kbd; }
  $("nav-keys").addEventListener("click", () => {
    if (typing()) kbd.blur();
    else { kbd.value = ""; kbd.focus(); }
  });
  kbd.addEventListener("focus", () => { $("nav-keys").classList.add("on"); fitAboveKeyboard(); });
  kbd.addEventListener("blur", () => { $("nav-keys").classList.remove("on"); fitAboveKeyboard(); });
  // What's typed lands in the (invisible) text area: pass it on and empty it. Keys that type nothing
  // (Backspace, Enter, arrows) come through keydown below. Composed text (Chinese, Japanese) goes once
  // it's complete.
  function flush() {
    const v = kbd.value;
    kbd.value = "";
    if (v) send({ t: "text", s: v });
  }
  kbd.addEventListener("input", (e) => { if (!e.isComposing) flush(); });
  kbd.addEventListener("compositionend", flush);

  // While the keyboard is up, the app shrinks to fit above it rather than hiding behind it.
  function fitAboveKeyboard() {
    const vv = window.visualViewport;
    const covered = typing() && vv ? Math.max(0, innerHeight - vv.height - vv.offsetTop) : 0;
    $("stage").style.bottom = covered > 80 ? `${covered}px` : "";
    layout();
  }
  if (window.visualViewport) visualViewport.addEventListener("resize", fitAboveKeyboard);

  const SPECIAL = new Set(["Backspace", "Delete", "Enter", "Tab", "Escape", "ArrowLeft", "ArrowUp", "ArrowRight",
    "ArrowDown", "Home", "End", "PageUp", "PageDown", "Insert", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    "F10", "F11", "F12"]);
  document.addEventListener("keydown", (e) => {
    if ($("player").hidden || e.isComposing) return;
    const shortcut = (e.ctrlKey || e.metaKey) && /^(Key[A-Z]|Digit\d)$/.test(e.code);
    if (SPECIAL.has(e.key) || shortcut) {
      e.preventDefault();
      send({ t: "key", key: e.key, code: e.code, shift: e.shiftKey, alt: e.altKey, ctrl: e.ctrlKey, meta: e.metaKey });
    } else if (!typing() && e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();  // an iPad's keyboard, with nothing focused: type straight into the app
      send({ t: "text", s: e.key });
    }
  });

  // ------------------------------------------------------------ buttons and menus
  $("nav-back").addEventListener("click", () => send({ t: "nav", k: "back" }));
  $("nav-apps").addEventListener("click", showLobby);
  $("nav-more").addEventListener("click", menu);
  $("lobby-menu").addEventListener("click", menu);
  $("sheet-back").addEventListener("click", closeSheets);
  $("trust-open").addEventListener("click", () => openSheet("trust"));
  $("trust-close").addEventListener("click", closeSheets);

  function showLobby() {
    wanted = null;
    if (kbd) kbd.blur();
    send({ t: "close" });
    send({ t: "apps" });
    show("lobby");
  }

  function openSheet(id) {
    closeSheets();
    $("sheet-back").hidden = false;
    $(id).hidden = false;
  }
  function closeSheets() { for (const id of ["sheet-back", "menu", "trust"]) $(id).hidden = true; }

  function menu() {
    const m = $("menu");
    m.innerHTML = "";
    const add = (label, note, fn, cls) => {
      const b = document.createElement("button");
      b.className = "item" + (cls ? " " + cls : "");
      b.innerHTML = "<span></span><small></small>";
      b.firstChild.textContent = label;
      b.lastChild.textContent = note || "";
      if (fn) b.addEventListener("click", () => { closeSheets(); fn(); });
      m.appendChild(b);
      return b;
    };
    const playing = !$("player").hidden && app.cap;
    if (playing) add(`Bring ${app.label} back to ${cfg.pc}`, "", () => send({ t: "back_to_pc" }));
    if (playing && app.mode === "move") {
      const q = add("Picture", "");
      const seg = document.createElement("span");
      seg.className = "seg";
      for (const [k, label] of [["smooth", "Smooth"], ["balanced", "Balanced"], ["sharp", "Sharp"]]) {
        const b = document.createElement("button");
        b.textContent = label;
        b.addEventListener("click", (e) => { e.stopPropagation(); send({ t: "quality", q: k }); closeSheets(); toast(`${label} picture`); });
        seg.appendChild(b);
      }
      q.replaceChild(seg, q.lastChild);
    }
    add("Video", mode === "webcodecs" ? "WebCodecs (decoded on this device)" : "JPEG frames (decoded on the PC)");
    if (!secure && !standalone && mode !== "webcodecs") add("Trust this PC for smoother video", "", () => openSheet("trust"));
    add(document.body.classList.contains("nonav") ? "Show buttons" : "Hide buttons",
      "Swipe in from the edge for Back", () => { document.body.classList.toggle("nonav"); viewChanged(); });
    if (playing && !standalone) add(`Add ${app.label} to the Home Screen`, "Share, then Add to Home Screen",
      () => toast("Tap Share, then Add to Home Screen", 4000));
    add(`Forget ${cfg.pc}`, "", () => { forget(); creds = null; if (ws) { ws.onclose = null; ws.close(); } showPair(); }, "danger");
    openSheet("menu");
  }

  // ------------------------------------------------------------ the trust step
  $("trust-pc").textContent = cfg.pc;
  $("trust-name").textContent = `WinDroid on ${cfg.pc}`;
  $("trust-fp").textContent = cfg.fp || "";
  let trustWatch = 0;
  function watchTrust() {
    if (secure || standalone || trustWatch) return;
    trustWatch = setInterval(async () => {
      if (document.visibilityState !== "visible" || upgrading) return;
      if (await httpsWorks()) { clearInterval(trustWatch); upgrade(); }
    }, 3000);
  }
  function upgrade() {
    upgrading = true;
    waiting("Switching to the secure connection...");
    if (online) send({ t: "upgrade" }); else connect();
  }

  // ------------------------------------------------------------ start
  async function start() {
    const hash = new URLSearchParams(location.hash.slice(1));
    const token = hash.get("pair") || hash.get("up");
    if (token) {
      history.replaceState(null, "", location.pathname);
      try { await pairWithToken(token); } catch (e) { showPair(e.message); return; }
    }
    if (!creds) { showPair(); return; }
    if (!secure && !standalone) {
      if (await httpsWorks()) { upgrade(); return; }
      $("trust-banner").hidden = false;
      watchTrust();
    }
    connect();
  }
  start();
})();
