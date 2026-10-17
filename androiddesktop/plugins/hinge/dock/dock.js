// The Hinge Matcher dock. Python pushes the whole state with ui.update(); clicks go back through
// window.pywebview.api (the plugin's DockApi).
"use strict";

const $ = (id) => document.getElementById(id);
const api = () => (window.pywebview && window.pywebview.api) || null;
const call = (name, ...args) => { const a = api(); if (a && a[name]) a[name](...args); };
const LIMIT = 255; // Hinge's comment limit

let S = null;            // the last state from Python
let current = 0;         // the idea the bubble shows; Alt+arrows on the Hinge window move it too
let editing = -1;        // the idea being edited, or -1
let hovered = -1;
let toneTimer = 0;
const drawn = {};  // what each part of the dock was last drawn from: unchanged parts aren't redrawn

function changed(part, data) {
  const key = JSON.stringify(data);
  if (drawn[part] === key) return false;
  drawn[part] = key;
  return true;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function setStatus(st) {
  const pill = $("status");
  pill.className = "pill " + (st.kind || "");
  pill.querySelector("b").textContent = st.text || "";
  pill.title = st.detail || "";
}

function renderWho(s) {
  if (!changed("who", [s.mode, s.profile, s.chat, s.reply && s.reply.stage, s.reply && s.reply.read,
                       s.draft && s.draft.summary, s.draft && s.draft.target_id, s.more])) return;
  const who = s.mode === "chat" ? s.chat : s.profile;
  const show = (s.mode === "profile" || s.mode === "chat") && who;
  $("who").hidden = !show;
  if (!show) return;
  $("who-name").textContent = who.name || (s.mode === "chat" ? "This chat" : "This profile");
  const stage = s.mode === "chat" && s.reply && s.reply.stage;
  $("who-stage").hidden = !stage;
  if (stage) $("who-stage").textContent = stage.replace(/_/g, " ");
  const chips = s.mode === "profile" ? Object.values(who.details || {}) : [];
  if (s.mode === "chat" && who.count) chips.push(`${who.count} messages read`);
  $("who-chips").innerHTML = chips.slice(0, 8).map((c) => `<span class="chip">${esc(c)}</span>`).join("");
  const summary = s.mode === "chat" ? s.reply && s.reply.read : s.draft && s.draft.summary;
  $("who-summary").textContent = summary || "";
  $("who-summary").hidden = !summary;
  const pips = s.mode === "profile" ? who.cards || [] : [];
  const target = s.draft && s.draft.target_id;
  $("who-cards").innerHTML = pips.map((c) =>
    `<span class="cardpip go${c.id === target ? " target" : ""}" data-card="${esc(c.id)}"
       title="${esc(c.text ? c.text + "\n" : "")}Click to go to it in Hinge">${esc(c.label)}</span>`
  ).join("") + (s.mode === "profile" && s.more ? `<span class="cardpip">scroll for more…</span>` : "");
}

function renderPick(s) {
  const d = s.mode === "profile" ? s.draft : null;
  $("pick").hidden = !(d && d.recommend === "like" && d.target_label);
  if (!$("pick").hidden) {
    $("pick-title").textContent = `Like their ${d.target_label}`;
    $("pick-title").dataset.card = d.target_id || "";
    $("pick-title").classList.toggle("go", !!d.target_id);
    $("pick-title").title = d.target_id ? "Click to go to it in Hinge" : "";
    $("pick-why").textContent = d.target_why || "";
  }
  let skip = null;
  if (d && d.recommend === "skip") skip = ["Claude would skip this one", d.skip_reason];
  const r = s.mode === "chat" ? s.reply : null;
  if (r && r.recommend === "wait") skip = ["Claude would wait for their reply", r.wait_reason];
  $("skipnote").hidden = !skip;
  if (skip) { $("skip-title").textContent = skip[0]; $("skip-why").textContent = skip[1] || ""; }
  $("needs").hidden = !(r && r.needs_info);
  if (r && r.needs_info) $("needs-text").textContent = r.needs_info;
}

function ideaHTML(o, i, s) {
  const best = s.best === i ? `<span class="best">&#x2605; Claude's pick</span>` : "";
  const on = !o.target_label ? "" : s.mode === "profile" && o.target_id
    ? `<span class="on-card go" data-card="${esc(o.target_id)}" title="${esc(o.target_label)}: click to go to it in Hinge">on ${esc(o.target_label)}</span>`
    : `<span class="on-card" title="${esc(o.target_label)}">on ${esc(o.target_label)}</span>`;
  const kbd = s.keys && s.keys.insert ? `<kbd>${esc(s.keys.insert)}</kbd>` : "";
  return `<li class="idea${i === current ? " current" : ""}${s.inserted === i ? " done" : ""}" data-i="${i}">
    <div class="idea-top"><span class="angle">${esc(o.angle || "idea")}</span>${best}${on}</div>
    <div class="idea-text" spellcheck="false">${esc(o.text)}</div>
    <div class="idea-foot">
      <button class="icon-btn edit" title="Edit before filling it in"><span class="glyph">&#xE70F;</span></button>
      <span class="count"></span>
      <button class="insert" title="Type it into Hinge's box">${s.inserted === i ? "Filled in" : "Fill in"} ${kbd}</button>
    </div>
    <div class="progress"></div>
  </li>`;
}

function renderIdeas(s) {
  if (!changed("ideas", [s.options, s.current, s.inserted, s.drafting, s.best, s.keys, s.mode, s.box])) return;
  const opts = s.options || [];
  const wrap = $("ideas-wrap");
  const mode = s.mode === "profile" || s.mode === "chat";
  wrap.hidden = !mode || (!opts.length && !s.drafting);
  if (wrap.hidden) return;
  $("ideas-title").textContent = s.mode === "chat" ? "Reply ideas" : "Openers";
  $("ideas-hint").textContent = s.box ? "click one to fill in" : (s.mode === "chat" ? "click to fill in" : "tap ♥ in Hinge, then click one");
  if (editing >= 0) { drawn.ideas = ""; return; } // don't yank the text out from under an edit
  if (!opts.length) {
    $("ideas").innerHTML = [0, 1, 2].map(() =>
      `<li class="idea skeleton"><div class="ph tag"></div><div class="ph"></div><div class="ph short"></div></li>`).join("");
    return;
  }
  current = Math.min(Math.max(s.current ?? current, 0), opts.length - 1);
  $("ideas").innerHTML = opts.map((o, i) => ideaHTML(o, i, s)).join("");
  $("ideas").style.opacity = s.drafting ? ".55" : "1";
}

function renderControls(s) {
  const busy = !!s.drafting;
  const regen = $("regen");
  regen.classList.toggle("spin", busy);
  regen.querySelector("span:last-child").textContent = busy ? "Writing…" : "New ideas";
  const mode = s.mode === "profile" || s.mode === "chat";
  $("controls").hidden = !mode;
  $("skip").hidden = s.mode !== "profile";
  $("skip").classList.toggle("armed", !!s.skip_armed);
  if (s.tone && document.activeElement !== $("warmth")) $("warmth").value = Math.round(s.tone.warmth * 100);
  if (s.tone) for (const b of $("length").children) b.classList.toggle("on", Number(b.dataset.v) === Math.round(s.tone.length));
  const k = s.keys || {};
  $("keys").innerHTML = k.insert
    ? `In Hinge: <kbd>${esc(k.prev)}</kbd> <kbd>${esc(k.next)}</kbd> pick · <kbd>${esc(k.insert)}</kbd> fill in · ` +
      `<kbd>${esc(k.toggle)}</kbd> ${s.bubble === false ? "show" : "hide"}`
    : "";
}

function renderBanner(s) {
  const b = $("banner");
  b.hidden = !s.message;
  b.className = "banner " + (s.message_kind || "");
  b.textContent = s.message || "";
}

function render(s) {
  S = s;
  document.documentElement.dataset.theme = s.theme || "";
  document.body.classList.toggle("tucked", !!s.tucked);
  $("tab-count").textContent = s.options && s.options.length ? `${s.options.length} ideas` : "";
  setStatus(s.status || {});
  const shown = s.bubble !== false;
  $("bubble").classList.toggle("off", !shown);
  $("bubble").querySelector(".glyph").innerHTML = shown ? "&#xE7B3;" : "&#xED1A;";
  $("bubble").title = `${shown ? "Hide" : "Show"} the idea on Hinge (${(s.keys && s.keys.toggle) || "Alt+Q"})`;
  $("pause").classList.toggle("on", !!s.paused);
  $("pause").title = s.paused ? "Read the screen again" : "Pause reading the screen";
  const idle = !(s.mode === "profile" || s.mode === "chat");
  $("idle").hidden = !idle;
  if (idle && s.idle) { $("idle-title").textContent = s.idle.title; $("idle-text").textContent = s.idle.text; }
  renderWho(s);
  renderPick(s);
  renderIdeas(s);
  renderControls(s);
  renderBanner(s);
}

function setCurrent(i, tell = true) {
  if (!S || !S.options || !S.options.length) return;
  current = (i + S.options.length) % S.options.length;
  for (const li of document.querySelectorAll(".idea")) li.classList.toggle("current", Number(li.dataset.i) === current);
  const li = document.querySelector(`.idea[data-i="${current}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
  if (tell) call("select", current);
}

function ideaText(i) {
  const el = document.querySelector(`.idea[data-i="${i}"] .idea-text`);
  return el ? el.innerText.replace(/\s+/g, " ").trim() : "";
}

function insert(i) {
  const text = ideaText(i);
  if (!text) return;
  stopEditing(false);
  setCurrent(i, false);
  call("insert", i, text);
}

function startEditing(i) {
  stopEditing(false);
  const el = document.querySelector(`.idea[data-i="${i}"] .idea-text`);
  if (!el) return;
  editing = i;
  el.contentEditable = "true";
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  updateCount(el);
}

function stopEditing(revert) {
  if (editing < 0) return;
  const el = document.querySelector(`.idea[data-i="${editing}"] .idea-text`);
  if (el) {
    if (revert && S && S.options[editing]) el.textContent = S.options[editing].text;
    el.contentEditable = "false";
    const c = el.parentElement.querySelector(".count");
    if (c) c.textContent = "";
  }
  editing = -1;
}

function updateCount(el) {
  const c = el.parentElement.querySelector(".count");
  const n = el.innerText.trim().length;
  c.textContent = `${n}/${LIMIT}`;
  c.classList.toggle("over", n > LIMIT);
}

function toneChanged() {
  clearTimeout(toneTimer);
  const warmth = Number($("warmth").value) / 100;
  const on = [...$("length").children].find((b) => b.classList.contains("on"));
  const length = on ? Number(on.dataset.v) : 0;
  toneTimer = setTimeout(() => call("tone", warmth, length), 350);
}

// ---------------------------------------------------------------- events
$("ideas").addEventListener("click", (e) => {
  const li = e.target.closest(".idea");
  if (!li || li.classList.contains("skeleton")) return;
  const i = Number(li.dataset.i);
  const go = e.target.closest(".on-card.go");
  if (go) { setCurrent(i); call("go_to", go.dataset.card); return; } // the card it's for, not filling it in
  if (e.target.closest(".edit")) { editing === i ? stopEditing(false) : startEditing(i); return; }
  if (e.target.closest(".idea-text") && editing === i) return;
  insert(i);
});
$("ideas").addEventListener("input", (e) => { if (e.target.classList.contains("idea-text")) updateCount(e.target); });
$("ideas").addEventListener("keydown", (e) => {
  if (!e.target.classList.contains("idea-text")) return;
  if (e.key === "Enter") { e.preventDefault(); insert(editing); }
  else if (e.key === "Escape") { e.preventDefault(); stopEditing(true); }
  e.stopPropagation();
});
$("ideas").addEventListener("mouseover", (e) => {
  const li = e.target.closest(".idea");
  const i = li && !li.classList.contains("skeleton") ? Number(li.dataset.i) : -1;
  if (i !== hovered) { hovered = i; if (i >= 0) call("preview", i); }
});
$("ideas").addEventListener("mouseleave", () => { hovered = -1; call("preview", -1); });

for (const id of ["who-cards", "pick"]) {
  $(id).addEventListener("click", (e) => {
    const go = e.target.closest(".go");
    if (go && go.dataset.card) call("go_to", go.dataset.card);
  });
}
$("regen").addEventListener("click", () => call("regenerate", $("hint").value.trim()));
$("hint").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); call("regenerate", $("hint").value.trim()); }
  e.stopPropagation();
});
$("warmth").addEventListener("input", () => {
  const v = Number($("warmth").value);
  $("tone-left").style.color = v < -20 ? "var(--accent-2)" : "";
  $("tone-right").style.color = v > 20 ? "var(--accent)" : "";
});
$("warmth").addEventListener("change", toneChanged);
$("length").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  for (const x of $("length").children) x.classList.toggle("on", x === b);
  toneChanged();
});
$("skip").addEventListener("click", () => call("skip"));
$("pause").addEventListener("click", () => call("pause", !(S && S.paused)));
$("bubble").addEventListener("click", () => call("bubble", !(S && S.bubble !== false)));
$("collapse").addEventListener("click", () => call("tuck", true));
$("tab").addEventListener("click", () => call("tuck", false));

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.key === "ArrowDown") { e.preventDefault(); setCurrent(current + 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); setCurrent(current - 1); }
  else if (e.key === "Enter") { e.preventDefault(); insert(current); }
  else if (e.key.toLowerCase() === "r" && !e.ctrlKey) { e.preventDefault(); call("regenerate", $("hint").value.trim()); }
  else if (e.key === "Escape") { e.preventDefault(); call("focus_app"); }
});

// ---------------------------------------------------------------- from Python
window.ui = {
  update: render,
  current(i) { setCurrent(i, false); },
  typing(i, p) {
    const li = document.querySelector(`.idea[data-i="${i}"]`);
    if (!li) return;
    li.querySelector(".progress").style.width = `${Math.round(Math.min(1, p) * 100)}%`;
    if (p >= 1) setTimeout(() => { li.querySelector(".progress").style.width = "0"; }, 600);
  },
  toast(text) {
    const t = $("toast");
    t.hidden = false;
    t.textContent = text;
    t.style.animation = "none";
    void t.offsetWidth;
    t.style.animation = "";
  },
};

window.addEventListener("pywebviewready", () => call("ready"));
