"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview && window.pywebview.api;

const state = { apps: [], vm: { state: "off", detail: "" }, setup: { installed: true }, checks: [], query: "", sel: 0 };
const dismissed = new Set(); // warnings put away for this session; problems that stop Android stay
const STATUS = { off: "Off", starting: "Starting", running: "Running", stopping: "Stopping", locked: "Locked" };

// ------------------------------------------------------------ rendering
function filtered() {
  const q = state.query.trim().toLowerCase();
  if (!q) return state.apps;
  return state.apps
    .filter((a) => a.label.toLowerCase().includes(q) || a.pkg.toLowerCase().includes(q))
    .sort((a, b) => a.label.toLowerCase().indexOf(q) - b.label.toLowerCase().indexOf(q));
}

let lastKey = "";
function renderGrid() {
  const grid = $("grid");
  const apps = filtered();
  const away = (state.remote && state.remote.links) || {};
  const key = JSON.stringify([apps.map((a) => [a.pkg, a.label, a.running, a.v, away[a.pkg]]), state.sel, state.vm.state]);
  if (key === lastKey) return;
  const first = lastKey === "";
  lastKey = key;

  if (!state.apps.length) {
    grid.innerHTML = Array.from({ length: 12 }, (_, i) =>
      `<div class="tile skeleton" style="animation-delay:${i * 25}ms"><div class="ph-icon"></div><div class="ph-text"></div></div>`).join("");
    $("count").textContent = "";
    $("empty").hidden = true;
    return;
  }
  state.sel = Math.min(state.sel, Math.max(apps.length - 1, 0));
  grid.innerHTML = "";
  apps.forEach((app, i) => {
    const tile = document.createElement("button");
    tile.className = "tile" + (app.running ? " running" : "") + (state.query && i === state.sel ? " sel" : "");
    tile.style.animationDelay = first ? `${Math.min(i, 24) * 18}ms` : "0ms";
    if (!first) tile.style.animation = "none";
    tile.title = away[app.pkg] ? `${app.label} (on ${away[app.pkg].device})` : app.label;
    if (away[app.pkg]) tile.classList.add("away");
    tile.dataset.pkg = app.pkg;
    tile.innerHTML = `<img src="${app.icon}" alt=""><span class="label"></span><span class="dot"></span><span class="launching"></span>`;
    tile.querySelector(".label").textContent = app.label;
    tile.addEventListener("click", () => open(app.pkg, tile));
    tile.addEventListener("contextmenu", (e) => { e.preventDefault(); showMenu(app, e.clientX, e.clientY); });
    grid.appendChild(tile);
  });
  $("count").textContent = state.query ? `${apps.length} of ${state.apps.length}` : `${state.apps.length} apps`;
  $("grid-title").textContent = state.query ? "Results" : "All apps";
  $("empty").hidden = apps.length > 0;
  $("empty-q").textContent = state.query;
}

function renderStatus() {
  const pill = $("status");
  pill.className = "pill " + state.vm.state;
  pill.querySelector("b").textContent = STATUS[state.vm.state] || state.vm.state;
  pill.title = state.vm.detail || "";
  const power = $("power");
  const up = state.vm.state === "running" || state.vm.state === "locked";
  power.classList.toggle("on", up);
  power.title = up ? "Shut down Android" : "Start Android";
  power.disabled = state.vm.state === "starting" || state.vm.state === "stopping";
}

function renderSetup() {
  const s = state.setup;
  const showSetup = !s.installed;
  const inHub = !showSetup && window.hub && hub.isOpen(); // the settings page (hub.js) stands in for both
  $("setup").hidden = !showSetup;
  $("home").hidden = showSetup || remoteOpen || inHub;
  $("dock").hidden = showSetup || inHub;
  $("setup-go").hidden = !!s.busy;
  $("setup-terms").hidden = !!s.busy;
  if (showSetup && !s.busy) loadTerms();
  $("setup-progress").hidden = !s.busy;
  $("setup-fill").style.width = `${Math.round((s.progress || 0) * 100)}%`;
  $("setup-text").textContent = s.message || "";
  $("setup-error").hidden = !s.error;
  $("setup-error").textContent = s.error || "";
}

// Google's terms for what setup downloads (runtime.py): shown in full, and setup stays off until they're
// agreed to. The agreement names the exact text shown (its fingerprint), so a changed license asks again.
let diskBlocked = false, terms = null, termsLoading = false;
function updateSetupButton() {
  $("setup-go").disabled = diskBlocked || !(terms && terms.fingerprint) || !$("terms-agree").checked;
}
function loadTerms() {
  if (termsLoading || (terms && terms.fingerprint)) return;
  termsLoading = true;
  $("terms-retry").hidden = true;
  $("terms-text").textContent = "Getting Google's terms...";
  api().setup_terms().then((t) => {
    terms = t || {};
    $("terms-text").textContent = terms.text || terms.error || "Couldn't get Google's terms.";
    $("terms-retry").hidden = !!terms.text;
    if (terms.accepted) $("terms-agree").checked = true;
  }).catch(() => {
    terms = {};
    $("terms-text").textContent = "Couldn't get Google's terms. Check your connection and try again.";
    $("terms-retry").hidden = false;
  }).finally(() => {
    termsLoading = false;
    updateSetupButton();
  });
}
$("terms-agree").addEventListener("change", updateSetupButton);
$("terms-retry").addEventListener("click", () => { terms = null; loadTerms(); });
$("terms-link").addEventListener("click", (e) => { e.preventDefault(); api().open_terms(); });

// Problems with this PC (see checks.py): what's wrong, why it matters, and a button to fix it.
let lastNotices = "";
function renderNotices() {
  const items = (state.checks || []).filter((c) => c.level === "blocking" || !dismissed.has(c.id));
  const key = JSON.stringify(items);
  diskBlocked = items.some((c) => c.id === "disk" && c.level === "blocking");
  updateSetupButton();
  if (key === lastNotices) return;
  lastNotices = key;
  for (const box of [$("notices"), $("setup-notices")]) {
    box.innerHTML = "";
    for (const c of items) {
      const el = document.createElement("div");
      el.className = "notice " + c.level;
      el.innerHTML = `<span class="glyph">${c.level === "blocking" ? "\uE7BA" : "\uE946"}</span>
        <div class="notice-text"><b></b><p></p><div class="notice-actions"></div></div>`;
      el.querySelector("b").textContent = c.title;
      el.querySelector("p").textContent = c.detail;
      const actions = el.querySelector(".notice-actions");
      if (c.fix) {
        const fix = document.createElement("button");
        fix.className = "ghost";
        fix.textContent = c.fix_label;
        fix.addEventListener("click", () => api().fix(c.id));
        actions.appendChild(fix);
      }
      if (c.level === "blocking") {
        const again = document.createElement("button");
        again.className = "ghost quiet";
        again.textContent = "Check again";
        again.addEventListener("click", () => api().recheck());
        actions.appendChild(again);
      } else {
        const close = document.createElement("button");
        close.className = "icon-btn dismiss";
        close.title = "Dismiss";
        close.innerHTML = '<span class="glyph">\uE711</span>';
        close.addEventListener("click", () => { dismissed.add(c.id); renderNotices(); });
        el.appendChild(close);
      }
      box.appendChild(el);
    }
  }
}

// Android boots locked when a screen-lock PIN is set in it: ask for the PIN here (or, if the user
// chose so, unlock with Windows Hello and the PIN remembered for them).
let wasLocked = false;
function renderUnlock() {
  const locked = state.vm.state === "locked" && !!state.setup.installed;
  const hello = state.hello || {};
  $("unlock").hidden = !locked;
  $("unlock-hello").hidden = !(hello.available && hello.remembered);
  $("remember-row").hidden = !hello.available;
  if (locked && !wasLocked) {
    $("pin").value = "";
    $("remember").checked = !!hello.remembered;
    $("unlock-error").hidden = true;
    setTimeout(() => $("pin").focus(), 60);
  }
  wasLocked = locked;
}

function showUnlockError(message) {
  $("unlock-error").textContent = message || "";
  $("unlock-error").hidden = !message;
  if (message) {
    const card = $("unlock");
    card.classList.remove("shake"); void card.offsetWidth; card.classList.add("shake");
    $("pin").focus();
  }
}

$("unlock").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("pin").value;
  if (!pin) { $("pin").focus(); return; }
  const go = $("unlock-go");
  go.disabled = true;
  go.textContent = "Unlocking...";
  try {
    // null: the box isn't shown (no Windows Hello right now), so leave a remembered PIN alone.
    const message = await api().unlock(pin, $("remember-row").hidden ? null : $("remember").checked);
    $("pin").value = "";
    showUnlockError(message);
  } finally {
    go.disabled = false;
    go.textContent = "Unlock";
  }
});

$("unlock-hello").addEventListener("click", async () => {
  const b = $("unlock-hello");
  b.disabled = true;
  try { showUnlockError(await api().unlock_hello()); } finally { b.disabled = false; }
});

// Unticking forgets the remembered PIN at once.
$("remember").addEventListener("change", (e) => {
  if (!e.target.checked && state.hello && state.hello.remembered) {
    api().forget_pin();
    toast("Your PIN is no longer remembered");
  }
});

// Windows Hello for Android's fingerprint prompts: set up once by adding a fingerprint in Android.
const helloKey = "hello-card-dismissed";
function helloDismissed() { try { return localStorage.getItem(helloKey) === "1"; } catch { return false; } }
function renderHello() {
  const h = state.hello || {};
  const show = h.available && state.vm.state === "running" && (h.enrolling || (h.enrolled === 0 && !helloDismissed()));
  $("hello").hidden = !show;
  if (!show) return;
  $("hello-title").textContent = h.enrolling ? "Adding a fingerprint" : "Use Windows Hello in Android";
  $("hello-detail").textContent = h.enrolling
    ? "Follow the steps in Android's Settings: confirm your Android PIN, then WinDroid touches the fingerprint sensor for you."
    : "When an Android app asks for your fingerprint, confirm with Windows Hello instead. Add a fingerprint to Android once to set it up.";
  $("hello-go").textContent = h.enrolling ? "Open Settings" : "Set up fingerprint";
}
$("hello-go").addEventListener("click", () => api().setup_fingerprint());
$("hello-dismiss").addEventListener("click", () => {
  try { localStorage.setItem(helloKey, "1"); } catch {}
  $("hello").hidden = true;
});

// A newer WinDroid (update.py). "Later" puts the offer away until WinDroid next starts.
let updateLater = false;
function renderUpdate() {
  const u = state.update;
  const show = !!u && !(u.state === "available" && updateLater);
  $("update").hidden = !show;
  if (!show) return;
  const busy = u.state === "downloading" || u.state === "installing";
  $("update-title").textContent = {
    available: `WinDroid ${u.version} is available`,
    downloading: `Downloading WinDroid ${u.version}...`,
    installing: `Installing WinDroid ${u.version}`,
    failed: "The update didn't finish",
  }[u.state] || "";
  $("update-detail").textContent = {
    available: `You have ${u.current}. Updating closes your app windows and shuts Android down; WinDroid starts again by itself when it's done.`,
    downloading: "Your apps keep running until it's downloaded.",
    installing: "WinDroid is closing to make way for the new version. It starts again by itself.",
    failed: u.error,
  }[u.state] || "";
  $("update-bar").hidden = u.state !== "downloading";
  $("update-fill").style.width = `${Math.round((u.progress || 0) * 100)}%`;
  $("update-actions").hidden = busy;
  $("update-go").textContent = u.state === "failed" ? "Try again" : "Update";
  $("update-later").hidden = busy;
}
$("update-go").addEventListener("click", () => api().update());
$("update-notes").addEventListener("click", () => api().update_notes());
$("update-later").addEventListener("click", () => { updateLater = true; $("update").hidden = true; });

// iPhone & iPad (remote/): pairing, paired devices, and devices asking to be allowed.
let remoteOpen = false, qrTimer = 0, qrShown = false, pairedBefore = -1;
function remote() { return state.remote || {}; }

function openRemote(pair) {
  remoteOpen = true;
  $("remote").hidden = false;
  $("home").hidden = true;
  renderRemote();
  if (pair && remote().enabled) showQr();
}
function closeRemote() {
  remoteOpen = false;
  $("remote").hidden = true;
  $("home").hidden = !state.setup.installed;
  hideQr();
}

async function showQr() {
  clearTimeout(qrTimer);
  const r = await api().remote_pair();
  if (!remoteOpen) return;
  if (r.error) { toast(r.error); hideQr(); return; }
  qrShown = true;
  pairedBefore = (remote().devices || []).length;
  $("remote-qr").src = r.qr;
  $("remote-url").textContent = r.url.replace(/#.*/, "");
  $("remote-qr-box").hidden = false;
  $("remote-pair").hidden = true;
  qrTimer = setTimeout(showQr, (r.expires - 20) * 1000);  // a fresh code before this one runs out
}
function hideQr() {
  clearTimeout(qrTimer);
  if (qrShown) api().remote_pair_done();
  qrShown = false;
  $("remote-qr-box").hidden = true;
  $("remote-pair").hidden = false;
}

function ago(t) {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago`
    : new Date(t * 1000).toLocaleDateString();
}

function askCard(a) {
  const el = document.createElement("div");
  el.className = "notice ask";
  el.innerHTML = `<span class="glyph"></span><div class="notice-text"><b></b>
    <p>Allow it only if it shows this code:</p><div class="sas"></div>
    <div class="notice-actions"><button class="primary small">Allow</button><button class="ghost">Deny</button></div></div>`;
  el.querySelector("b").textContent = `${a.name} wants to use your Android apps`;
  el.querySelector(".sas").textContent = a.code.replace(/(\d{3})/, "$1 ");
  const [allow, deny] = el.querySelectorAll("button");
  allow.addEventListener("click", () => api().remote_approve(a.id, true));
  deny.addEventListener("click", () => api().remote_approve(a.id, false));
  return el;
}

let lastRemote = "";
function renderRemote() {
  const r = remote();
  $("remote-open").hidden = !state.setup.installed;
  const asks = r.approvals || [];
  for (const box of [$("home-asks"), $("remote-asks")]) {
    box.innerHTML = "";
    for (const a of asks) box.appendChild(askCard(a));
  }
  const key = JSON.stringify([r, remoteOpen]);
  if (key === lastRemote) return;
  lastRemote = key;
  if (!remoteOpen) return;
  $("remote-intro").hidden = !!r.enabled;
  $("remote-main").hidden = !r.enabled;
  $("remote-enable").disabled = !!r.error;
  if (r.error) { $("remote-intro").querySelector("p").textContent = r.error; return; }
  if (!r.enabled) return;
  const problem = r.problem || (!r.listening ? "Getting ready..." : "");
  $("remote-problem").hidden = !problem;
  $("remote-problem-text").textContent = problem;
  $("remote-firewall").hidden = r.firewall !== false || !problem.includes("Firewall");
  $("remote-problem-detail").textContent = $("remote-firewall").hidden ? "" :
    "Windows asks for administrator permission once, to let devices on your home network (only) reach WinDroid.";
  $("remote-pair").disabled = !r.listening;
  const devices = r.devices || [];
  if (qrShown && devices.length > pairedBefore) { toast(`${devices[devices.length - 1].name} is paired`); hideQr(); }
  $("remote-count").textContent = devices.length ? `${devices.length}` : "";
  const list = $("remote-devices");
  list.innerHTML = devices.length ? "" : '<p class="none">None yet.</p>';
  for (const d of devices) {
    const row = document.createElement("div");
    row.className = "device";
    row.innerHTML = `<span class="glyph"></span><div><b></b><p></p></div>
      <button class="icon-btn" title="Remove"><span class="glyph"></span></button>`;
    row.querySelector("b").textContent = d.name;
    row.querySelector("p").textContent = (d.online ? "Connected" : `Last seen ${ago(d.last_seen)}`) +
      (d.apps.length ? ` · ${d.apps.join(", ")}` : "");
    if (d.online) row.classList.add("online");
    row.querySelector("button").addEventListener("click", () => api().remote_remove(d.group));
    list.appendChild(row);
  }
  $("remote-fp").textContent = r.fingerprint ? `On your device, this PC's certificate fingerprint starts ${r.fingerprint.slice(0, 23)}.` : "";
}

$("remote-open").addEventListener("click", () => openRemote(false));
$("remote-close").addEventListener("click", closeRemote);
$("remote-enable").addEventListener("click", () => { api().remote_enable(true); setTimeout(() => showQr(), 1500); });
$("remote-disable").addEventListener("click", () => { hideQr(); api().remote_enable(false); });
$("remote-pair").addEventListener("click", showQr);
$("remote-firewall").addEventListener("click", async () => {
  const b = $("remote-firewall");
  b.disabled = true;
  try { if (!(await api().remote_firewall())) toast("The firewall rule wasn't added"); } finally { b.disabled = false; }
});

// Called from Python with the latest state.
window.ui = {
  update(patch) {
    Object.assign(state, patch);
    if (patch.theme) document.documentElement.dataset.theme = patch.theme;
    if (patch.play) $("play-icon").src = patch.play;
    renderStatus();
    renderSetup();
    renderNotices();
    renderUnlock();
    renderHello();
    renderUpdate();
    renderGrid();
    renderRemote();
    if (window.hub) hub.update(patch);
  },
  show: (page) => window.hub && hub.show(page), // "settings" or "palette", from Python
  toast,
  openRemote: () => openRemote(true),
};

// ------------------------------------------------------------ actions
function open(pkg, tile) {
  if (tile) { tile.classList.remove("opening"); void tile.offsetWidth; tile.classList.add("opening"); }
  api().open_app(pkg);
}

let toastTimer = 0;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

function showMenu(app, x, y) {
  const menu = $("menu");
  const items = [
    ["", "Open", () => open(app.pkg)],
    ["", app.inStart ? "Remove from Start" : "Pin to Start", () => api().toggle_start(app.pkg)],
    ["", "Add desktop shortcut", () => api().desktop_shortcut(app.pkg)],
  ];
  const rs = remote();
  if (state.remote && !rs.error) {
    const link = (rs.links || {})[app.pkg];
    const devices = rs.enabled ? (rs.devices || []).slice(0, 3) : [];
    items.push(null);
    if (link) items.push(["", `Bring back from ${link.device}`, () => api().remote_back(app.pkg)]);
    else if (!devices.length) items.push(["", "Send to iPhone or iPad...", () => openRemote(true)]);
    for (const d of link ? [] : devices) {
      items.push(["", `Send to ${d.name}`, () => api().remote_send(app.pkg, d.group, "move")]);
      if (app.running) items.push(["", `Show on ${d.name} too`, () => api().remote_send(app.pkg, d.group, "share")]);
    }
  }
  if (!app.system) items.push(null, ["", "Uninstall", () => api().uninstall(app.pkg), "danger"]);
  menu.innerHTML = "";
  for (const item of items) {
    if (!item) { menu.appendChild(document.createElement("hr")); continue; }
    const [glyph, text, fn, cls] = item;
    const b = document.createElement("button");
    if (cls) b.className = cls;
    b.innerHTML = `<span class="glyph">${glyph}</span><span></span>`;
    b.lastChild.textContent = text;
    b.addEventListener("click", () => { hideMenu(); fn(); });
    menu.appendChild(b);
  }
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - r.height - 8)}px`;
}
function hideMenu() { $("menu").hidden = true; }

// ------------------------------------------------------------ events
$("search").addEventListener("input", (e) => { state.query = e.target.value; state.sel = 0; renderGrid(); });
$("search").addEventListener("keydown", (e) => {
  const apps = filtered();
  if (e.key === "Enter" && apps.length) { open(apps[state.sel].pkg, $("grid").children[state.sel]); }
  else if (e.key === "ArrowDown" || e.key === "ArrowRight") { state.sel = Math.min(state.sel + (e.key === "ArrowDown" ? 4 : 1), apps.length - 1); renderGrid(); e.preventDefault(); }
  else if (e.key === "ArrowUp" || e.key === "ArrowLeft") { state.sel = Math.max(state.sel - (e.key === "ArrowUp" ? 4 : 1), 0); renderGrid(); e.preventDefault(); }
});
document.addEventListener("keydown", (e) => {
  if (window.hub && hub.key(e)) return; // Ctrl+K, Ctrl+, and the keys of the palette and settings page
  const input = $("search");
  if (e.key === "Escape") {
    if (!$("menu").hidden) hideMenu();
    else if (remoteOpen) closeRemote();
    else if (input.value) { input.value = ""; state.query = ""; renderGrid(); }
    else api().hide();
    return;
  }
  // Type anywhere to search, like the Start menu (or, while Android is locked, to enter the PIN).
  if (!(document.activeElement instanceof HTMLInputElement) && e.key.length === 1 && !e.ctrlKey && !e.altKey
      && !e.metaKey && $("setup").hidden && !$("home").hidden) {
    ($("unlock").hidden ? input : $("pin")).focus();
  }
});
document.addEventListener("click", (e) => { if (!$("menu").contains(e.target)) hideMenu(); });
document.addEventListener("contextmenu", (e) => { if (!e.target.closest(".tile")) e.preventDefault(); });
window.addEventListener("blur", hideMenu);

$("close").addEventListener("click", () => api().hide());
$("minimize").addEventListener("click", () => api().minimize());
$("power").addEventListener("click", () => api().power());
$("play").addEventListener("click", () => api().open_app("com.android.vending"));
$("apk").addEventListener("click", () => api().install_apk());
$("settings").addEventListener("click", () => hub.open());
$("palette-open").addEventListener("click", () => hub.palette());
$("search-actions").addEventListener("click", () => hub.palette(state.query));
$("setup-go").addEventListener("click", () => api().setup(terms && terms.fingerprint || ""));
$("search-play").addEventListener("click", () => api().search_play(state.query));

window.addEventListener("pywebviewready", async () => {
  ui.update(await api().get_state());
  $("search").focus();
});
renderGrid();
