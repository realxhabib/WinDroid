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
  const key = JSON.stringify([apps.map((a) => [a.pkg, a.label, a.running, a.v]), state.sel, state.vm.state]);
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
    tile.title = app.label;
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
  $("setup").hidden = !showSetup;
  $("home").hidden = showSetup;
  $("dock").hidden = showSetup;
  $("setup-go").hidden = !!s.busy;
  $("setup-progress").hidden = !s.busy;
  $("setup-fill").style.width = `${Math.round((s.progress || 0) * 100)}%`;
  $("setup-text").textContent = s.message || "";
  $("setup-error").hidden = !s.error;
  $("setup-error").textContent = s.error || "";
}

// Problems with this PC (see checks.py): what's wrong, why it matters, and a button to fix it.
let lastNotices = "";
function renderNotices() {
  const items = (state.checks || []).filter((c) => c.level === "blocking" || !dismissed.has(c.id));
  const key = JSON.stringify(items);
  $("setup-go").disabled = items.some((c) => c.id === "disk" && c.level === "blocking");
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

// Android boots locked when a screen-lock PIN is set in it: ask for the PIN here.
let wasLocked = false;
function renderUnlock() {
  const locked = state.vm.state === "locked" && !!state.setup.installed;
  $("unlock").hidden = !locked;
  if (locked && !wasLocked) {
    $("pin").value = "";
    $("unlock-error").hidden = true;
    setTimeout(() => $("pin").focus(), 60);
  }
  wasLocked = locked;
}

$("unlock").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pin = $("pin").value;
  if (!pin) { $("pin").focus(); return; }
  const go = $("unlock-go");
  go.disabled = true;
  go.textContent = "Unlocking...";
  try {
    const message = await api().unlock(pin);
    $("pin").value = "";
    $("unlock-error").textContent = message || "";
    $("unlock-error").hidden = !message;
    if (message) {
      const card = $("unlock");
      card.classList.remove("shake"); void card.offsetWidth; card.classList.add("shake");
      $("pin").focus();
    }
  } finally {
    go.disabled = false;
    go.textContent = "Unlock";
  }
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
    renderGrid();
  },
  toast,
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
  const input = $("search");
  if (e.key === "Escape") {
    if (!$("menu").hidden) hideMenu();
    else if (input.value) { input.value = ""; state.query = ""; renderGrid(); }
    else api().hide();
    return;
  }
  // Type anywhere to search, like the Start menu (or, while Android is locked, to enter the PIN).
  if (!(document.activeElement instanceof HTMLInputElement) && e.key.length === 1 && !e.ctrlKey && !e.altKey
      && !e.metaKey && $("setup").hidden) {
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
$("settings").addEventListener("click", () => api().open_app("com.android.settings"));
$("setup-go").addEventListener("click", () => api().setup());
$("search-play").addEventListener("click", () => api().search_play(state.query));

window.addEventListener("pywebviewready", async () => {
  ui.update(await api().get_state());
  $("search").focus();
});
renderGrid();
