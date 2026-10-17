"use strict";
// The launcher's settings page: one card per feature, with its live status (Api.features(), see
// features.py) and what it can do; the command palette over every action and app (Ctrl+K); and the
// What's new card. Uses app.js's state, $, api, toast and open.

const hub = (() => {
  let isOpen = false;
  let features = null; // the last Api.features() answer
  let loading = null;
  let timer = 0;
  const view = { checks: null, checksBusy: false, pin: false, pinNote: "", pinError: false, desktop: "",
                 armed: 0, news: false, newsHidden: false };
  const QUALITY = { high: "High", balanced: "Balanced", low: "Low" };
  const LOCKS = { pin: "PIN", password: "Password", pattern: "Pattern", none: "None", locked: "Set, Android is locked" };

  // ------------------------------------------------------------ DOM helpers
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "style") el.style.cssText = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const glyph = (code) => h("span", { class: "glyph", text: code });
  const tag = (text, tone) => h("span", { class: `state ${tone || ""}`, title: text }, h("i"), h("span", { text }));
  const stat = (label, value) => h("div", { class: "stat" }, h("small", { text: label }), h("b", { text: value }));
  const okMark = (text) => h("span", { class: "ok-mark" }, glyph("\uE73E"), text);
  const row = (title, detail, ...controls) =>
    h("div", { class: "row" }, h("div", { class: "row-text" }, h("b", { text: title }), detail ? h("span", { text: detail }) : null), ...controls);

  // A button that runs fn (maybe async) once at a time, then looks again at how things stand.
  function button(label, icon, fn, cls = "ghost small", title = null) {
    const b = h("button", { class: cls, title }, icon ? glyph(icon) : null, h("span", { text: label }));
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (b.disabled) return;
      b.disabled = true;
      try { await fn(b); } catch (err) { toast(String((err && err.message) || err)); } finally { b.disabled = false; refresh(); }
    });
    return b;
  }

  function toggle(on, fn, disabled = false, label = "") {
    return h("button", { class: `switch${on ? " on" : ""}`, role: "switch", "aria-checked": String(!!on), "aria-label": label,
                         disabled, onclick: async (e) => {
                           const b = e.currentTarget;
                           b.disabled = true;
                           try { await fn(!on); } finally { b.disabled = false; refresh(); }
                         } });
  }

  function segmented(options, value, fn) {
    return h("div", { class: "segmented", role: "radiogroup" }, Object.entries(options).map(([v, label]) =>
      h("button", { class: v === value ? "on" : "", role: "radio", "aria-checked": String(v === value),
                    onclick: async () => { if (v !== value) { await fn(v); refresh(); } } }, label)));
  }

  function card(id, icon, colors, title, sub, status, ...body) {
    return h("section", { class: "card", id: `card-${id}`, "data-card": id, style: `--c1:${colors[0]};--c2:${colors[1]}` },
      h("div", { class: "card-head" },
        h("div", { class: "card-icon" }, glyph(icon)),
        h("div", { class: "card-title" }, h("b", { text: title }), sub ? h("span", { text: sub }) : null),
        status),
      ...body);
  }

  const up = () => state.vm.state === "running" || state.vm.state === "locked";

  // ------------------------------------------------------------ the cards
  function androidCard(f) {
    const a = f.android || {};
    const s = state.vm.state;
    const tone = { running: "ok", locked: "warn", starting: "busy", stopping: "busy" }[s] || "";
    const armed = view.armed > Date.now();
    return card("android", "\uE770", ["#5bf0a0", "#12a85a"], "Android",
      `${a.version || "Android"}${a.play ? " with Google Play" : ""}`, tag(STATUS[s] || s, tone),
      s !== "running" && state.vm.detail ? h("p", { class: "card-text", text: state.vm.detail }) : null,
      h("div", { class: "stats" },
        stat("Memory", a.memory ? `${a.memory} GB` : "-"), stat("Cores", a.cores || "-"),
        stat("Windows", a.windows ? `${a.windows} open` : "None")),
      h("div", { class: "card-actions" },
        up() ? button("Shut down", "\uE7E8", () => api().power())
             : button("Start", "\uE7E8", () => api().power(), "primary tiny"),
        up() ? button(armed ? "Restart? App windows close" : "Restart", "\uE72C", () => {
          if (!(view.armed > Date.now())) { view.armed = Date.now() + 4000; setTimeout(render, 4100); render(); return; }
          view.armed = 0;
          return api().restart_android();
        }, `ghost small${armed ? " confirm" : ""}`) : null,
        button(view.checksBusy ? "Checking..." : view.checks ? "Check again" : "Run checks", "\uE9D9", runChecks),
        s === "running" ? button("Android settings", "\uE8A7", () => api().android_settings()) : null),
      view.checks ? h("div", { class: "checks" }, view.checks.map((c) => h("div", { class: `check ${c.level}` },
        glyph(c.level === "ok" ? "\uE73E" : c.level === "blocking" ? "\uE7BA" : "\uE946"),
        h("div", {}, h("b", { text: c.title }), c.detail ? h("span", { text: c.detail }) : null,
          c.fix && c.level !== "ok" ? button(c.fix_label, null, () => api().fix(c.id)) : null)))) : null);
  }

  async function runChecks() {
    view.checksBusy = true;
    render();
    try { view.checks = await api().run_checks(); } finally { view.checksBusy = false; render(); }
  }

  function securityCard(f) {
    const s = f.security || {};
    const running = state.vm.state === "running";
    const rows = [];
    rows.push(row("Unlock with Windows Hello",
      s.unlock ? "After each start, Windows Hello unlocks Android with your remembered PIN."
               : "Skip typing Android's PIN after each start: Windows Hello confirms it's you.",
      toggle(s.unlock || view.pin, async (on) => {
        if (on) { view.pin = true; view.pinNote = ""; view.pinError = false; render(); setTimeout(() => { const p = $("hub-pin"); if (p) p.focus(); }, 30); return; }
        if (view.pin && !s.unlock) { view.pin = false; render(); return; }
        await api().hello_unlock(false, "");
        toast("Your PIN is no longer remembered");
      }, !s.hello, "Unlock with Windows Hello")));
    if (view.pin && !s.unlock) {
      const input = h("input", { id: "hub-pin", type: "password", placeholder: "Android's PIN or password", maxlength: "64",
                                 autocomplete: "off", spellcheck: "false" });
      const form = h("form", { class: "inline-form", autocomplete: "off", onsubmit: async (e) => {
        e.preventDefault();
        if (!input.value) { input.focus(); return; }
        go.disabled = true;
        const message = await api().hello_unlock(true, input.value);
        input.value = "";
        go.disabled = false;
        view.pinError = !!message;
        view.pinNote = message;
        if (!message) { view.pin = false; toast("Android unlocks with Windows Hello from now on"); }
        refresh();
      } }, input);
      const go = h("button", { class: "primary tiny", type: "submit", text: "Turn on" });
      form.append(go);
      rows.push(h("div", { class: "row-extra" }, form,
        h("p", { class: `form-note${view.pinError ? " error" : ""}`, text: view.pinNote ||
          "Android checks it (wrong tries count towards its lockout). It's kept encrypted for your Windows account." })));
    }
    const prints = s.fingerprints;
    rows.push(row("Fingerprint",
      !s.hello ? "Set up Windows Hello in Windows first." :
      s.enrolling ? "Adding one: confirm your PIN in Android's Settings, Android Desktop touches the sensor." :
      prints ? "Windows Hello answers Android apps that ask for your fingerprint." :
      running ? "Add one to Android once, and Windows Hello answers its fingerprint prompts." :
      "Android says whether one is set up while it runs. Setting one up starts Android.",
      prints ? okMark(`${prints} set up`) :
      s.enrolling ? button("Open Settings", "\uE8A7", () => api().setup_fingerprint()) :
      s.hello && state.vm.state !== "locked" ? button("Set up fingerprint", "\uE928", () => api().setup_fingerprint(), running ? "primary tiny" : "ghost small") : null));
    rows.push(row("Screen lock", LOCKS[s.lock] || (running ? "Checking..." : "Shown while Android runs."),
      running ? button("Change", null, () => api().screen_lock_settings()) : null));
    return card("security", "\uE72E", ["#ffc85a", "#ff8a3d"], "Security", "Windows Hello, fingerprint and screen lock",
      s.hello ? tag("Windows Hello ready", "ok") : tag("No Windows Hello", "warn"),
      h("div", { class: "rows" }, rows));
  }

  function cameraCard(f) {
    const c = f.camera || {};
    const cams = c.webcams; // null while looking
    const status = cams === null ? tag("Looking...", "busy") : c.android && c.android !== "No camera" ? tag(c.android, "ok") : tag("No webcam", "warn");
    const rows = [
      row("Android's cameras", "Front and back both show your PC's webcam, picked again at every start of Android.",
        h("span", { class: "muted", text: c.android || "-" })),
      row("Webcams on this PC", cams === null ? "Looking..." : cams.length ? cams.join(", ") : "None found."),
    ];
    if (c.virtual) {
      const v = c.virtual;
      rows.push(row("Virtual camera", "Shows Android the webcam's whole picture, upright, instead of a tall slice. Windows asks for administrator rights.",
        v.installed && v.current ? okMark("Installed") : null,
        v.installed ? button(v.current ? "Remove" : "Update", null, () => cameraAction(v.current ? "uninstall" : "install"))
                    : button("Install", "\uE896", () => cameraAction("install"), "primary tiny")));
    }
    return card("camera", "\uE722", ["#ff8fb8", "#c056ff"], "Camera", "Your PC's webcam, in Android", status, h("div", { class: "rows" }, rows));
  }

  async function cameraAction(command) {
    toast(command === "install" ? "Installing the virtual camera: Windows asks for permission" : "Removing the virtual camera");
    toast(await api().camera(command));
  }

  function agentsCard(f) {
    const a = f.agents || {};
    const code = a.claudeCode || {}, desk = a.claudeDesktop || {};
    const connected = code.connected || desk.connected;
    const status = a.hidden ? tag(`${a.hidden} hidden app${a.hidden > 1 ? "s" : ""} running`, "busy") :
                   connected ? tag("Connected", "ok") : tag("Not connected");
    const rows = [
      row("Claude Code",
        !code.installed ? "Not installed on this PC." :
        code.current ? "Connected, for every project." :
        code.connected ? "Connected to another copy of Android Desktop." : "Adds Android Desktop's MCP server, for every project.",
        code.current ? okMark("Connected") : null,
        code.current ? button("Disconnect", null, async () => { const m = await api().disconnect_claude(); toast(m || "Disconnected from Claude Code"); }) :
        code.installed ? button(code.connected ? "Reconnect" : "Connect Claude", "\uE99A", connectClaude, "primary tiny") : null),
      row("Claude Desktop",
        desk.connected ? "In its config file." : "Paste the setup into claude_desktop_config.json, then restart Claude Desktop.",
        button(view.desktop ? "Copied" : "Copy setup", "\uE8C8", copyDesktop)),
    ];
    if (view.desktop) rows.push(h("div", { class: "row-extra" }, h("pre", { class: "code", text: view.desktop })));
    const activity = a.activity || [];
    rows.push(h("div", { class: "row-extra" },
      h("div", { class: "row-text" }, h("b", { text: "Recent activity" })),
      activity.length ? h("ul", { class: "activity" }, activity.slice(0, 5).map((e) =>
        h("li", { title: `${e.date} ${e.time}  ${e.text}` }, h("time", { text: e.time }), h("span", { text: e.text }))))
        : h("p", { class: "form-note", text: "Nothing yet. Ask Claude to use an Android app for you." })));
    return card("agents", "\uE99A", ["#8fa2ff", "#7a4dff"], "AI agents", "Claude and other MCP clients use Android apps", status,
      h("p", { class: "card-text", text: "Agents run apps hidden, read and operate them, and hand one to you in a window when you should see it. Secure screens and your PIN stay yours." }),
      h("div", { class: "rows" }, rows),
      h("div", { class: "card-actions" }, button("Open logs", "\uE838", () => api().open_folder("logs"))));
  }

  async function connectClaude() {
    const message = await api().connect_claude();
    toast(message || "Connected: Claude Code can use Android apps (new sessions)");
  }

  async function copyDesktop() {
    view.desktop = await api().copy_claude_desktop();
    toast("Claude Desktop setup copied");
    render();
  }

  function pluginsCard(f) {
    if (!f.plugins) return null;
    const label = (pkg) => (state.apps.find((a) => a.pkg === pkg) || {}).label || pkg;
    const rows = f.plugins.map((p) => row(p.title, `For ${p.packages.map(label).join(", ")}`,
      toggle(p.on, (on) => setPlugin(p, on), false, p.title)));
    return card("plugins", "\uEA86", ["#4ce0d2", "#2c8cff"], "Plugins", "Companions for particular apps",
      tag(`${f.plugins.filter((p) => p.on).length} on`, f.plugins.some((p) => p.on) ? "ok" : ""),
      rows.length ? h("div", { class: "rows" }, rows) : h("p", { class: "card-text", text: "No plugins in this version." }));
  }

  async function setPlugin(p, on) {
    await api().set_plugin(p.name, on);
    toast(`${p.title} ${on ? "on" : "off"}`);
  }

  function phonesCard(f) {
    const p = f.phones;
    if (!p) return null;
    const online = p.devices.some((d) => d.online);
    return card("phones", "\uE8EA", ["#6cc4ff", "#2c6cff"], "iPhone & iPad", "Your Android apps, full screen in Safari",
      !p.enabled ? tag("Off") : p.problem ? tag(p.problem, "warn") : tag(online ? "Connected" : "On", "ok"),
      h("p", { class: "card-text", text: "Send one app at a time to an iPhone or iPad over your home Wi-Fi, and bring it back whenever you like." }),
      p.devices.length ? h("div", { class: "pills" }, p.devices.map((d) =>
        h("span", { class: `pill-item${d.online ? " on" : ""}`, title: d.apps.length ? `Has ${d.apps.join(", ")}` : "" }, h("i"), d.name))) : null,
      h("div", { class: "card-actions" },
        button("Pair an iPhone or iPad", "\uE8EA", () => openRemote(true), "primary tiny"),
        p.devices.length || p.asking ? button(p.asking ? `${p.asking} waiting` : "Manage", null, () => openRemote(false)) : null));
  }

  function openRemote(pair) {
    closeHub();
    if (window.ui.openRemote) window.ui.openRemote(pair);
  }

  function performanceCard(f) {
    const p = f.performance || {};
    const w = p.windows || {};
    return card("performance", "\uE945", ["#ffb36b", "#ff5a52"], "Performance", "How app windows are drawn",
      p.mode === "emulator" ? tag("Smooth", "ok") : tag("Video", p.setting === "scrcpy" ? "" : "warn"),
      h("p", { class: "card-text", text: p.why || "" }),
      h("div", { class: "stats" }, stat("Smooth windows", w.emulator || 0), stat("Video windows", w.scrcpy || 0),
        stat("Video quality", QUALITY[p.quality] || "-")),
      h("div", { class: "rows" },
        row("App windows", "Smooth draws apps straight from the emulator, with nothing encoded. Video (scrcpy) works anywhere.",
          segmented({ auto: "Smooth", scrcpy: "Video" }, p.setting, async (v) => { await api().set_app_windows(v); toast("New app windows use this"); })),
        row("Video quality", "For windows shown as video: lower costs Android less CPU and looks softer.",
          segmented(QUALITY, p.quality, async (v) => { await api().set_quality(v); toast("New video windows use this"); }))));
  }

  function aboutCard(f) {
    const u = f.updates || {};
    return card("about", "\uE946", ["#a3b1c6", "#5c677d"], "Android Desktop", `Version ${u.version || ""}`,
      u.offer ? tag(`${u.offer.version} available`, "warn") : null,
      h("p", { class: "card-text", text: "Android runs in a virtual machine on this PC, and nothing leaves it." }),
      h("div", { class: "card-actions" },
        u.available ? button("Check for updates", "\uE895", async () => toast(await api().check_updates())) : null,
        button("What's new", "\uE734", () => { closeHub(); showNews(); }),
        button("Logs", "\uE838", () => api().open_folder("logs")),
        button("Data folder", "\uE8B7", () => api().open_folder("data"))));
  }

  const CARDS = [["android", "Android", androidCard], ["security", "Security", securityCard], ["camera", "Camera", cameraCard],
                 ["agents", "AI agents", agentsCard], ["plugins", "Plugins", pluginsCard], ["phones", "iPhone & iPad", phonesCard],
                 ["performance", "Performance", performanceCard], ["about", "About", aboutCard]];

  // ------------------------------------------------------------ the page
  function render() {
    if (!isOpen) return;
    const body = $("hub-body");
    if (!features) {
      if (!body.children.length) body.append(h("p", { class: "card-text", text: "Reading how things stand..." }));
      return;
    }
    const built = [];
    for (const [id, , make] of CARDS) {
      let node = null;
      try { node = make(features); } catch (e) { node = card(id, "\uE7BA", ["#ff8a80", "#ff5a52"], id, String(e), null); }
      if (node) built.push(node);
    }
    const chips = $("hub-chips");
    const names = built.map((n) => n.dataset.card);
    if (chips.dataset.cards !== names.join()) {
      chips.dataset.cards = names.join();
      chips.replaceChildren(...names.map((id) => h("button", { class: "chip", text: CARDS.find((c) => c[0] === id)[1], onclick: () => reveal(id) })));
    }
    // Keep cards that haven't changed (and the one being typed in), so the page doesn't flicker.
    const old = new Map([...body.querySelectorAll(".card")].map((n) => [n.dataset.card, n]));
    if (body.firstElementChild && !body.firstElementChild.classList.contains("card")) body.replaceChildren();
    let prev = null;
    for (const node of built) {
      const was = old.get(node.dataset.card);
      old.delete(node.dataset.card);
      let keep = node;
      if (was && (was.outerHTML === node.outerHTML || was.contains(document.activeElement) && document.activeElement.tagName === "INPUT")) {
        keep = was;
      } else if (was) {
        if (was.classList.contains("flash")) node.classList.add("flash");
        was.replaceWith(node);
      }
      if (keep.parentNode !== body || keep.previousElementSibling !== prev) {
        if (prev) prev.after(keep); else body.prepend(keep);
      }
      prev = keep;
    }
    old.forEach((n) => n.remove());
  }

  function reveal(id) {
    const node = $(`card-${id}`);
    if (!node) return;
    node.scrollIntoView({ behavior: "smooth", block: "start" });
    node.classList.add("flash");
    setTimeout(() => { const n = $(`card-${id}`); if (n) n.classList.remove("flash"); }, 1400);
  }

  async function refresh() {
    if (loading) return loading;
    loading = (async () => {
      try { features = await api().features(); } catch (e) { /* the agent is busy: next time */ }
      finally { loading = null; }
      render();
      renderNews();
      if (palOpen) renderPalette();
    })();
    return loading;
  }

  async function openHub(id, then) {
    if (!state.setup.installed) return;
    closePalette();
    hideMenu();
    const first = !isOpen;
    isOpen = true;
    $("hub").hidden = false;
    $("home").hidden = true;
    $("dock").hidden = true;
    if (first) {
      $("hub-body").classList.add("entering");
      $("hub-body").scrollTop = 0;
      setTimeout(() => $("hub-body").classList.remove("entering"), 500);
    }
    render();
    clearInterval(timer);
    timer = setInterval(refresh, 2500);
    await refresh();
    if (id) setTimeout(() => reveal(id), first ? 60 : 0);
    if (then) then();
  }

  function closeHub() {
    if (!isOpen) return;
    isOpen = false;
    clearInterval(timer);
    view.pin = false;
    view.desktop = "";
    $("hub").hidden = true;
    renderSetup(); // home and dock come back
    $("search").focus();
  }

  // ------------------------------------------------------------ What's new
  function news() {
    const f = features || {};
    const items = [
      { glyph: "\uE713", title: "Settings for every feature", text: "The gear: live status, one click to use each.", go: () => openHub() },
      { glyph: "\uE721", title: "Ctrl+K finds anything", text: "Every app and action, as you type.", go: () => palette() },
      { glyph: "\uE712", title: "⋯ in every app window", text: "Pin to Start, shortcut, App info, Uninstall.", go: null },
      { glyph: "\uE928", title: "Windows Hello for Android", text: "Unlock it, answer its fingerprint prompts.", go: () => openHub("security") },
      { glyph: "\uE99A", title: "Android apps for AI agents", text: "Connect Claude in one click.", go: () => openHub("agents") },
    ];
    if (f.phones) items.splice(3, 0, { glyph: "\uE8EA", title: "Your apps on iPhone & iPad", text: "Full screen in Safari.", go: () => openRemote(true) });
    if (f.plugins && f.plugins.length) items.splice(4, 0, { glyph: "\uEA86", title: "Plugins", text: f.plugins.map((p) => p.title).join(", "), go: () => openHub("plugins") });
    return items.slice(0, 5);
  }

  function renderNews() {
    const box = $("whatsnew");
    const w = features && features.whatsNew;
    const show = !!features && features.installed && (view.news || (w && !view.newsHidden));
    box.hidden = !show;
    if (!show) return;
    const version = (features.updates || {}).version || "";
    const key = JSON.stringify([version, w && w.first, news().map((n) => n.title)]);
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.replaceChildren(
      h("div", { class: "whatsnew-head" }, h("b", { text: w && !w.first ? "What's new" : "Welcome to Android Desktop" }),
        h("span", { text: version ? `Version ${version}` : "" })),
      h("ul", {}, news().map((n) => h("li", {}, glyph(n.glyph),
        h("div", { class: "wn-text" }, h("b", { text: n.title }), h("span", { text: n.text })),
        n.go ? button("Try it", null, n.go) : null))),
      h("button", { class: "icon-btn dismiss", title: "Got it", onclick: dismissNews }, glyph("\uE711")));
  }

  function showNews() {
    view.news = true;
    view.newsHidden = false;
    $("whatsnew").dataset.key = "";
    renderNews();
    $("whatsnew").scrollIntoView({ block: "nearest" });
  }

  function dismissNews() {
    view.news = false;
    view.newsHidden = true;
    $("whatsnew").hidden = true;
    api().dismiss_whats_new();
  }

  // ------------------------------------------------------------ command palette
  let palOpen = false;
  let palRows = []; // what's listed: {item, hits} in order
  let palSel = 0;

  function actions() {
    const f = features || {};
    const vm = state.vm.state, installed = !!state.setup.installed;
    const sec = f.security || {}, cam = f.camera || {}, ag = f.agents || {}, perf = f.performance || {}, upd = f.updates || {};
    const A = (id, title, icon, run, keywords = "", when = true) => (when ? { id, title, glyph: icon, run, keywords, hint: "" } : null);
    return [
      A("settings", "Settings", "\uE713", () => openHub(), "preferences options features status", installed),
      A("android-settings", "Android's Settings app", "\uE8A7", () => api().android_settings(), "system wifi accounts google", installed && vm !== "locked"),
      A("setup", "Set up Android", "\uE896", () => api().setup(), "install download", !installed),
      A("start", "Start Android", "\uE7E8", () => api().power(), "boot power on", installed && vm === "off"),
      A("unlock", "Unlock Android", "\uE72E", () => $("pin").focus(), "pin password", vm === "locked"),
      A("unlock-hello", "Unlock Android with Windows Hello", "\uE928", async () => toast(await api().unlock_hello() || "Unlocked"), "pin", vm === "locked" && sec.unlock && sec.hello),
      A("shutdown", "Shut down Android", "\uE7E8", () => api().power(), "stop power off turn off", up()),
      A("restart", "Restart Android", "\uE72C", () => api().restart_android(), "reboot", up()),
      A("checks", "Run checks", "\uE9D9", () => openHub("android", runChecks), "doctor diagnose problems troubleshoot virtualization memory disk"),
      A("fingerprint", "Set up fingerprint", "\uE928", () => api().setup_fingerprint(), "windows hello biometrics enroll", installed && sec.hello && vm !== "locked" && !sec.fingerprints),
      A("hello-on", "Unlock with Windows Hello from now on", "\uE928", () => openHub("security", () => { view.pin = true; render(); }), "pin remember", installed && sec.hello && !sec.unlock),
      A("hello-off", "Stop unlocking with Windows Hello", "\uE928", async () => { await api().hello_unlock(false, ""); toast("Your PIN is no longer remembered"); }, "forget pin", sec.unlock),
      A("screen-lock", "Change Android's screen lock", "\uE72E", () => api().screen_lock_settings(), "pin password security", installed && vm !== "locked"),
      A("camera", "Camera", "\uE722", () => openHub("camera"), "webcam", installed),
      A("camera-install", "Install the virtual camera", "\uE722", () => openHub("camera", () => cameraAction("install")), "webcam whole picture", cam.virtual && !cam.virtual.installed),
      A("camera-remove", "Remove the virtual camera", "\uE722", () => openHub("camera", () => cameraAction("uninstall")), "webcam uninstall", cam.virtual && cam.virtual.installed),
      A("claude", "Connect Claude", "\uE99A", () => openHub("agents", connectClaude), "ai agent mcp claude code", (ag.claudeCode || {}).installed && !(ag.claudeCode || {}).current),
      A("claude-desktop", "Copy the Claude Desktop setup", "\uE8C8", () => openHub("agents", copyDesktop), "ai agent mcp config"),
      A("agents", "AI agents", "\uE99A", () => openHub("agents"), "mcp claude activity", installed),
      A("pair", "Pair an iPhone or iPad", "\uE8EA", () => openRemote(true), "ios safari phone tablet remote send", !!f.phones),
      ...(f.plugins || []).map((p) => A(`plugin:${p.name}`, `${p.on ? "Turn off" : "Turn on"} ${p.title}`, "\uEA86", () => setPlugin(p, !p.on), "plugin")),
      A("smooth", "App windows: smooth", "\uE945", async () => { await api().set_app_windows("auto"); toast("New app windows are smooth"); }, "performance emulator displays", perf.setting === "scrcpy"),
      A("video", "App windows: video (scrcpy)", "\uE945", async () => { await api().set_app_windows("scrcpy"); toast("New app windows use video"); }, "performance", perf.setting && perf.setting !== "scrcpy"),
      ...Object.entries(QUALITY).filter(([q]) => perf.quality && perf.quality !== q).map(([q, label]) =>
        A(`quality-${q}`, `Video quality: ${label}`, "\uE945", async () => { await api().set_quality(q); toast(`Video quality: ${label}`); }, "scrcpy performance")),
      A("updates", "Check for updates", "\uE895", async () => toast(await api().check_updates()), "version upgrade", upd.available),
      A("whats-new", "What's new", "\uE734", showNews, "features release notes", installed),
      A("get-apps", "Get apps from Google Play", "\uE719", () => api().open_app("com.android.vending"), "play store install", installed),
      A("apk", "Install an APK", "\uE7B8", () => api().install_apk(), "sideload", installed),
      A("logs", "Open the logs folder", "\uE838", () => api().open_folder("logs"), "agent.log androiddesktop.log emulator"),
      A("hide", "Close the launcher", "\uE8BB", () => api().hide(), "hide"),
      A("quit", "Quit Android Desktop", "\uE7E8", () => api().quit(), "exit close everything"),
    ].filter(Boolean);
  }

  function palette(query = "") {
    hideMenu();
    palOpen = true;
    palSel = 0;
    $("palette").hidden = false;
    $("palette-q").value = query;
    renderPalette();
    refresh();
    setTimeout(() => { $("palette-q").focus(); $("palette-q").select(); }, 0);
  }

  function closePalette() {
    if (!palOpen) return;
    palOpen = false;
    $("palette").hidden = true;
    if (!isOpen && !$("home").hidden) $("search").focus();
  }

  function highlighted(text, hits) {
    const span = h("span", { class: "p-title" });
    const at = new Set(hits);
    let run = "";
    let marked = false;
    const flush = () => { if (run) span.append(marked ? h("mark", { text: run }) : document.createTextNode(run)); run = ""; };
    [...text].forEach((c, i) => { if (at.has(i) !== marked) { flush(); marked = at.has(i); } run += c; });
    flush();
    return span;
  }

  function renderPalette() {
    const q = $("palette-q").value.trim();
    const acts = actions();
    const apps = (state.apps || []).map((a) => ({ id: `app:${a.pkg}`, title: a.label, keywords: a.pkg, icon: a.icon,
                                                    hint: a.running ? "Open" : "App", run: () => open(a.pkg) }));
    const list = $("palette-list");
    const out = [];
    palRows = [];
    const add = (item, hits) => {
      const i = palRows.length;
      palRows.push(item);
      out.push(h("button", { class: `palette-item${i === palSel ? " sel" : ""}`, role: "option", "aria-selected": String(i === palSel),
                             onmousemove: () => select(i), onclick: () => runRow(i) },
        item.icon ? h("img", { class: "p-icon", src: item.icon, alt: "" }) : h("span", { class: "p-icon glyph", text: item.glyph || "\uE76C" }),
        highlighted(item.title, hits || []), h("span", { class: "p-hint", text: item.hint || "" })));
    };
    if (!q) {
      out.push(h("div", { class: "palette-group", text: "Actions" }));
      acts.forEach((a) => add(a));
      if (apps.length) out.push(h("div", { class: "palette-group", text: "Apps" }));
      apps.forEach((a) => add(a));
    } else {
      const ranked = fuzzy.rank(q, [...acts, ...apps]).slice(0, 40);
      ranked.forEach((r) => add(r.item, r.hits));
      if (state.setup.installed && state.vm.state !== "locked") {
        add({ id: "play-search", title: `Search Google Play for "${q}"`, glyph: "\uE721", hint: "Play", run: () => api().search_play(q) });
      }
      if (!palRows.length) out.push(h("div", { class: "palette-empty", text: "Nothing matches" }));
    }
    palSel = Math.min(palSel, Math.max(palRows.length - 1, 0));
    list.replaceChildren(...out);
    const sel = list.querySelector(".sel");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }

  function select(i) {
    if (i === palSel) return;
    palSel = i;
    const items = $("palette-list").querySelectorAll(".palette-item");
    items.forEach((el, k) => { el.classList.toggle("sel", k === i); el.setAttribute("aria-selected", String(k === i)); });
    if (items[i]) items[i].scrollIntoView({ block: "nearest" });
  }

  async function runRow(i) {
    const item = palRows[i];
    if (!item) return;
    closePalette();
    try { await item.run(); } catch (e) { toast(String((e && e.message) || e)); }
    refresh();
  }

  $("palette-q").addEventListener("input", () => { palSel = 0; renderPalette(); });
  $("palette").addEventListener("mousedown", (e) => { if (e.target === $("palette")) closePalette(); });
  $("hub-back").addEventListener("click", closeHub);
  $("hub-android").addEventListener("click", () => api().android_settings());

  // ------------------------------------------------------------ keys (before app.js's own)
  function key(e) {
    const k = e.key.toLowerCase();
    const ctrl = (e.ctrlKey || e.metaKey) && !e.altKey;
    if (ctrl && k === "k") { e.preventDefault(); if (palOpen) closePalette(); else palette(); return true; }
    if (ctrl && k === ",") { e.preventDefault(); closePalette(); openHub(); return true; }
    if (palOpen) {
      if (e.key === "Escape") { e.preventDefault(); closePalette(); }
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (palRows.length) select((palSel + (e.key === "ArrowDown" ? 1 : -1) + palRows.length) % palRows.length);
      } else if (e.key === "Enter") { e.preventDefault(); runRow(palSel); }
      else if (document.activeElement !== $("palette-q") && e.key.length === 1 && !ctrl) $("palette-q").focus();
      return true;
    }
    if (isOpen) {
      if (e.key === "Escape") { e.preventDefault(); if (view.pin) { view.pin = false; render(); } else closeHub(); }
      return true;
    }
    return false;
  }

  window.addEventListener("pywebviewready", () => setTimeout(refresh, 300)); // What's new

  return {
    open: openHub, close: closeHub, palette, isOpen: () => isOpen, key,
    show(page) { if (page === "palette") palette(); else openHub(); },
    update(patch) {
      if (isOpen && (patch.vm || patch.hello || patch.apps)) render();
      if (patch.setup && !patch.setup.installed && isOpen) closeHub();
      if (palOpen && (patch.vm || patch.apps)) renderPalette();
      if (!features && patch.setup && patch.setup.installed) refresh();
    },
  };
})();
window.hub = hub; // app.js looks for it there
