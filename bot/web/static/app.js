/* CandyDownloader web app. No frameworks, no inline scripts/styles (strict CSP), and every piece of data is put on
   the page with textContent / setAttribute, never as markup. */
"use strict";

const state = { me: null, csrf: "", tab: "download", jobs: [], preview: null, tool: null, toolsList: [], settings: null,
  settingKeys: [], cookies: [], shares: null, history: null, admin: {}, eventSource: null, draft: {} };

// ---------------------------------------------------------------- tiny DOM helper
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const $app = document.getElementById("app");
const sizeText = (b) => !b ? "" : b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : b >= 1e6 ? Math.round(b / 1e6) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";
const clock = (s) => { if (!s && s !== 0) return ""; s = Math.round(s); const hh = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  return hh ? `${hh}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`; };
const safeImg = (u) => typeof u === "string" && u.startsWith("https://") ? u : "";

let toastTimer;
function toast(msg, isErr) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = h("div", { class: "toast" + (isErr ? " err" : ""), role: "status", text: msg });
  document.body.append(t); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.remove(), isErr ? 6000 : 3000);
}

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
  if (init.method !== "GET") init.headers["X-CSRF-Token"] = state.csrf;
  if (opts.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
  if (opts.body !== undefined) init.body = opts.body;
  let res;
  try { res = await fetch(path, init); } catch (e) { throw new Error("Can't reach the server."); }
  let data = {};
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (res.status === 401 && state.me) { state.me = null; stopStream(); render(); }
  if (res.status === 403 && data.must_change) { state.me = { ...(state.me || {}), account: { ...(state.me?.account || {}), must_change: true } }; render(); }
  if (!res.ok) { const err = new Error(data.error || "Something went wrong."); err.status = res.status; err.data = data; throw err; }
  return data;
}
const act = async (fn, okMsg) => { try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, true); return undefined; } };

// ---------------------------------------------------------------- theme
function applyTheme() { try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* storage blocked */ } }
function toggleTheme() {
  const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  const next = dark ? "light" : "dark"; document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------- boot / render
async function boot() {
  applyTheme();
  try { state.me = await api("/api/me"); state.csrf = state.me.csrf; } catch (e) { state.me = null; }
  render();
  if (state.me && !state.me.account.must_change) startStream();
}
function render() {
  $app.replaceChildren();
  if (!state.me) return $app.append(loginView());
  if (state.me.account.must_change) return $app.append(passwordView(true));
  $app.append(shellView());
}

function loginView() {
  const err = h("div", { class: "err", role: "alert" });
  const user = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: "false", required: true });
  const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
  const go = h("button", { class: "btn primary", type: "submit", style: false, text: "Sign in" });
  const form = h("form", { class: "stack", onsubmit: async (ev) => {
    ev.preventDefault(); err.textContent = ""; go.disabled = true;
    try {
      const r = await api("/api/auth/login", { method: "POST", json: { username: user.value, password: pass.value } });
      state.csrf = r.csrf; state.me = await api("/api/me"); state.csrf = state.me.csrf; render();
      if (!state.me.account.must_change) startStream();
    } catch (e) { err.textContent = e.status === 429 ? "Too many tries - please wait a bit." : e.message; go.disabled = false; pass.value = ""; }
  } }, h("div", null, h("label", { class: "field", text: "Username" }), user), h("div", null, h("label", { class: "field", text: "Password" }), pass), err, go);
  return h("div", { class: "login-wrap" }, h("div", { class: "card login" },
    h("div", { class: "logo", text: "🍬" }), h("h1", { text: "Candy Downloader" }), h("p", { class: "muted", text: "Sign in to grab your videos, music and more." }), form,
    h("p", { class: "small muted", text: "No account yet? Ask the owner, or send /weblogin to the Telegram bot." })));
}

function passwordView(forced) {
  const cur = h("input", { type: "password", autocomplete: "current-password" }), nw = h("input", { type: "password", autocomplete: "new-password" });
  const nw2 = h("input", { type: "password", autocomplete: "new-password" }), err = h("div", { class: "err", role: "alert" });
  const form = h("form", { class: "stack", onsubmit: async (ev) => {
    ev.preventDefault(); err.textContent = "";
    if (nw.value !== nw2.value) { err.textContent = "The two new passwords differ."; return; }
    try {
      const r = await api("/api/me/password", { method: "POST", json: { current: cur.value, new: nw.value } });
      state.csrf = r.csrf; state.me = await api("/api/me"); state.csrf = state.me.csrf; toast("Password changed ✓"); render(); startStream();
    } catch (e) { err.textContent = e.message; }
  } }, h("div", null, h("label", { class: "field", text: forced ? "Current (one-time) password" : "Current password" }), cur),
    h("div", null, h("label", { class: "field", text: "New password (at least 10 characters)" }), nw),
    h("div", null, h("label", { class: "field", text: "New password again" }), nw2), err,
    h("button", { class: "btn primary", type: "submit", text: "Save password" }));
  if (!forced) return form;
  return h("div", { class: "login-wrap" }, h("div", { class: "card login" }, h("div", { class: "logo", text: "🔑" }),
    h("h2", { text: "Choose your own password" }), h("p", { class: "muted", text: "The one you were given only works for this first sign-in." }), form,
    h("button", { class: "btn small", type: "button", text: "Sign out", onclick: logout })));
}

async function logout() { stopStream(); await act(() => api("/api/auth/logout", { method: "POST" })); state.me = null; render(); }

const TABS = [["download", "⬇️", "Download"], ["tools", "🧰", "Toolbox"], ["jobs", "🍭", "Queue"], ["history", "🕘", "History"], ["links", "🔗", "Links"], ["settings", "⚙️", "Settings"]];
function shellView() {
  const tabs = TABS.slice(); if (state.me.account.role === "admin") tabs.push(["admin", "🛡️", "Admin"]);
  const active = state.jobs.filter((j) => j.state === "queued" || j.state === "running").length;
  const tabBar = h("nav", { class: "tabs", role: "tablist" }, tabs.map(([id, ico, label]) =>
    h("button", { class: "tab", role: "tab", "aria-selected": state.tab === id ? "true" : "false", onclick: () => { state.tab = id; render(); loadTab(); } },
      h("span", { class: "ico", text: ico }), label, id === "jobs" && active ? h("span", { class: "badge", text: active }) : null)));
  const body = h("main", { id: "view" });
  const shell = h("div", { class: "shell" },
    h("header", { class: "topbar" }, h("div", { class: "brand" }, h("span", { class: "logo", text: state.me.owner_emoji || "🍬" }), h("span", { text: state.me.owner_name || "Candy" })),
      h("div", { class: "spacer" }), h("button", { class: "btn small", title: "Light / dark", "aria-label": "Switch light or dark theme", onclick: toggleTheme, text: "🌗" }),
      h("button", { class: "btn small", onclick: logout, text: "Sign out" })), tabBar, h("div", { style: false, class: "stack" }, body));
  body.append(tabView());
  return shell;
}
function rerenderView() { const v = document.getElementById("view"); if (v) { v.replaceChildren(tabView()); } }
function tabView() {
  return ({ download: downloadView, tools: toolsView, jobs: jobsView, history: historyView, links: linksView, settings: settingsView, admin: adminView }[state.tab] || downloadView)();
}
async function loadTab() {
  if (state.tab === "history") { await loadHistory(0); }
  if (state.tab === "links") { await loadShares(); }
  if (state.tab === "settings") { await loadSettings(); }
  if (state.tab === "tools") { await loadTools(); }
  if (state.tab === "admin") { await loadAdmin(); }
}

// ---------------------------------------------------------------- live job updates
function startStream() {
  stopStream();
  api("/api/jobs").then((d) => { state.jobs = d.jobs; refreshJobsUI(); }).catch(() => {});
  try {
    const es = new EventSource("/api/jobs/stream"); state.eventSource = es;
    es.addEventListener("jobs", (ev) => { try { state.jobs = JSON.parse(ev.data).jobs; refreshJobsUI(); } catch (e) { /* ignore */ } });
    es.onerror = () => { /* the browser reconnects by itself; a dead session shows on the next request */ };
  } catch (e) { /* no SSE: the page still works with reloads */ }
}
function stopStream() { if (state.eventSource) { state.eventSource.close(); state.eventSource = null; } }
function refreshJobsUI() {
  const open = document.activeElement && document.activeElement.closest && document.activeElement.closest(".job input");
  if (open) return;                                      // don't steal focus from a link box
  const list = document.getElementById("jobs-live"); if (list) list.replaceChildren(...jobCards());
  const active = state.jobs.filter((j) => j.state === "queued" || j.state === "running").length;
  const tab = [...document.querySelectorAll(".tab")].find((t) => t.textContent.includes("Queue"));
  if (tab) { tab.querySelector(".badge")?.remove(); if (active) tab.append(h("span", { class: "badge", text: active })); }
}

// ---------------------------------------------------------------- job cards
const STATE_PILL = { queued: ["⏳ Waiting", ""], running: ["✨ Working", "run"], done: ["✅ Done", "ok"], failed: ["⚠️ Failed", "bad"], cancelled: ["✋ Stopped", ""] };
function jobCards(filter) {
  const jobs = state.jobs.filter(filter || (() => true));
  if (!jobs.length) return [h("div", { class: "empty" }, h("div", { class: "big", text: "🍬" }), h("div", { text: "Nothing here yet. Your downloads will show up in this spot." }))];
  return jobs.map(jobCard);
}
function jobCard(job) {
  const [label, cls] = STATE_PILL[job.state] || ["", ""];
  const live = job.state === "queued" || job.state === "running";
  const bar = h("div", { class: "bar " + (job.state === "done" ? "done" : live ? "run" : "") }, h("i"));
  bar.firstChild.style.width = (job.state === "done" ? 100 : Math.max(live ? 4 : 0, Math.min(100, job.percent || 0))) + "%";
  const info = job.info || {};
  const meta = [info.type, info.size ? sizeText(info.size) : "", info.site].filter(Boolean).join(" · ");
  const card = h("div", { class: "job" },
    h("div", { class: "head" }, h("div", { class: "title", title: job.title || job.url, text: job.title || job.url || "Working…" }), h("span", { class: "pill " + cls, text: label })),
    meta ? h("div", { class: "small muted", text: meta }) : null,
    (live || job.state === "done") ? bar : null,
    job.steps && job.steps.length && job.state !== "done" ? h("div", { class: "steps", text: job.steps.slice(-3).join("\n") }) : null,
    job.error ? h("div", { class: "steps", text: job.error }) : null,
    job.files.length ? h("div", { class: "files" }, job.files.map((f) => fileRow(job, f))) : null,
    ...(job.notes || []).map((n) => h("div", { class: "small muted", text: "ℹ️ " + n })),
    job.expires ? h("div", { class: "small muted", text: "Available until " + new Date(job.expires * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) }) : null,
    h("div", { class: "row", style: false }, live ? h("button", { class: "btn small danger", text: "Cancel", onclick: () => act(() => api(`/api/jobs/${job.rid}/cancel`, { method: "POST" })) }) : null,
      (job.state === "failed" || job.state === "cancelled") ? h("button", { class: "btn small", text: "Try again", onclick: () => act(() => api(`/api/jobs/${job.rid}/retry`, { method: "POST" })) }) : null,
      !live ? h("button", { class: "btn small", text: "Remove", onclick: () => act(() => api(`/api/jobs/${job.rid}/dismiss`, { method: "POST" })).then(() => { state.jobs = state.jobs.filter((j) => j.rid !== job.rid); refreshJobsUI(); }) }) : null));
  return card;
}
function fileRow(job, f) {
  const row = h("div", { class: "file" }, h("div", { class: "n", title: f.name, text: f.name }), h("span", { class: "small muted", text: sizeText(f.size) }),
    h("a", { class: "btn small primary", href: `/api/files/${job.rid}/${f.index}`, download: f.name, text: "Download", style: false }));
  if (state.me.account.telegram_id) row.append(h("button", { class: "btn small", text: "Send to Telegram", onclick: (ev) => sendTelegram(ev.target, job, f) }));
  const sh = state.me.sharing;
  if (sh && sh.available) row.append(h("button", { class: "btn small", text: "🔗 Get a link", onclick: (ev) => linkPicker(ev.target, job, f, row) }));
  return row;
}
async function sendTelegram(btn, job, f) { btn.disabled = true; await act(() => api(`/api/files/${job.rid}/${f.index}/telegram`, { method: "POST" }), "Sent to your Telegram ✓"); btn.disabled = false; }
const absUrl = (path) => location.origin + path;
async function copyText(text, box) { try { await navigator.clipboard.writeText(text); toast("Copied ✓"); } catch (e) { if (box) box.select(); toast("Select and copy it by hand.", true); } }
function hoursChoices(sh) {
  const all = [[1, "1 hour"], [24, "1 day"], [72, "3 days"], [168, "1 week"], [720, "30 days"]].filter(([hrs]) => hrs <= sh.max_hours);
  if (!all.some(([hrs]) => hrs === sh.default_hours)) all.push([sh.default_hours, sh.default_hours + " hours"]);
  return all.sort((x, y) => x[0] - y[0]);
}
const ACCESS = [[0, "Anyone with the link"], [1, "Signed-in people only"], [2, "Only me"]];
function linkPicker(btn, job, f, row) {
  btn.disabled = true; const sh = state.me.sharing;
  const pick = h("select", { "aria-label": "How long the link works" }, hoursChoices(sh).map(([hrs, label]) => h("option", { value: hrs, selected: hrs === sh.default_hours, text: "Works for " + label })));
  const who = h("select", { "aria-label": "Who can open it" }, ACCESS.filter(([v]) => v >= sh.min_access).map(([v, label]) => h("option", { value: v, selected: v === Math.max(sh.default_access, sh.min_access), text: label })));
  const pw = h("input", { type: "password", placeholder: "Password (optional)", autocomplete: "new-password", maxlength: "100", "aria-label": "Link password" });
  const once = h("input", { type: "checkbox" });
  const make = h("button", { class: "btn small primary", text: "Make link", onclick: async () => {
    make.disabled = true;
    const r = await act(() => api(`/api/files/${job.rid}/${f.index}/link`, { method: "POST", json: { hours: parseInt(pick.value, 10), max_downloads: once.checked ? 1 : 0, access: parseInt(who.value, 10), password: pw.value } }));
    if (!r) { make.disabled = false; return; }
    const url = absUrl(r.share.path); const box = h("input", { type: "text", readonly: true, value: url, "aria-label": "Link" });
    picker.replaceWith(h("div", { class: "linkbox" }, box, h("button", { class: "btn small", text: "Copy", onclick: () => copyText(url, box) })));
    state.shares = null;
  } });
  const picker = h("div", { class: "linkbox" }, pick, who, pw, h("label", { class: "switch small" }, once, "One download only"), make,
    h("button", { class: "btn small", text: "Cancel", onclick: () => { picker.remove(); btn.disabled = false; } }));
  row.append(picker);
}

// ---------------------------------------------------------------- links tab
function linkOptions(s, d) {
  const patch = async (json, msg) => { const r = await act(() => api("/api/shares/" + s.id, { method: "PATCH", json }), msg); if (r) loadShares(); };
  const who = h("select", { "aria-label": "Who can open " + s.name, onchange: (e) => patch({ access: parseInt(e.target.value, 10) }, "Saved ✓") },
    ACCESS.filter(([v]) => v >= d.min_access).map(([v, label]) => h("option", { value: v, selected: v === s.access, text: label })));
  const once = h("label", { class: "switch small" }, h("input", { type: "checkbox", checked: s.max_downloads === 1, onchange: (e) => patch({ max_downloads: e.target.checked ? 1 : 0 }, "Saved ✓") }), "One download only");
  const pw = s.has_password ? h("button", { class: "btn small", text: "Remove password", onclick: () => patch({ clear_password: true }, "Password removed") })
    : h("button", { class: "btn small", text: "Add password", onclick: () => { const v = prompt("Password for this link (4 or more characters):"); if (v) patch({ password: v }, "Password set ✓"); } });
  const more = h("button", { class: "btn small", text: "Extend", onclick: () => patch({ hours: d.default_hours }, "Lasts " + d.default_hours + " h from now") });
  return h("div", { class: "linkbox" }, who, once, pw, more);
}
async function loadShares() { const d = await act(() => api("/api/shares")); if (d) { state.shares = d; state.me.sharing = { ...state.me.sharing, available: d.available, default_hours: d.default_hours, max_hours: d.max_hours, quota_mb: d.quota_mb, default_access: d.default_access, min_access: d.min_access }; rerenderView(); } }
const whenLeft = (t) => { const m = Math.max(0, Math.round((t * 1000 - Date.now()) / 60000)); return m >= 2880 ? Math.round(m / 1440) + " days" : m >= 120 ? Math.round(m / 60) + " hours" : Math.max(1, m) + " min"; };
function linksView() {
  const d = state.shares; const wrap = h("div", { class: "stack" });
  if (!d) return wrap.append(h("span", { class: "spinner" })), wrap;
  if (!d.available) return wrap.append(h("section", { class: "card" }, h("h2", { text: "🔗 Links" }), h("p", { class: "muted", text: "Links are switched off by the admin." }))), wrap;
  const card = h("section", { class: "card" }, h("h2", { text: "🔗 Your links" }),
    h("p", { class: "muted small", text: `Anyone with a link can download that file, so share them with care. You are using ${sizeText(d.used) || "0 MB"} of ${d.quota_mb} MB. Make one with “Get a link” on a finished download, here or in the Telegram bot.` }));
  if (!d.shares.length) card.append(h("p", { class: "muted", text: "No active links." }));
  for (const s of d.shares) {
    const url = absUrl(s.path); const box = h("input", { type: "text", readonly: true, value: url, "aria-label": "Link for " + s.name });
    card.append(h("div", { class: "file linkrow" }, h("div", { class: "n", title: s.name, text: s.name }),
      h("span", { class: "small muted", text: `${sizeText(s.size)} · ${whenLeft(s.expires)} left · ${s.downloads}${s.max_downloads ? "/" + s.max_downloads : ""} downloads${s.has_password ? " · 🔑 password" : ""}` }),
      linkOptions(s, d),
      h("div", { class: "linkbox" }, box, h("button", { class: "btn small", text: "Copy", onclick: () => copyText(url, box) }),
        h("button", { class: "btn small danger", text: "Delete", onclick: async () => { if (confirm("Delete this link? The file goes with it.")) { await act(() => api("/api/shares/" + s.id, { method: "DELETE" })); loadShares(); } } }))));
  }
  wrap.append(card); return wrap;
}

// ---------------------------------------------------------------- download tab
const URL_RE = /https?:\/\/[^\s<>"']+/g;
function downloadView() {
  const box = h("textarea", { placeholder: "Paste a link here (or several, one per line)…", "aria-label": "Link", value: state.draft.text || "", oninput: (e) => { state.draft.text = e.target.value; } });
  const status = h("div", { id: "preview-area" });
  const check = h("button", { class: "btn primary", text: "Check link ✨", onclick: async () => {
    const urls = [...new Set((box.value.match(URL_RE) || []).map((u) => u.replace(/[),.;]+$/, "")))];
    if (!urls.length) return toast("Paste a link first.", true);
    check.disabled = true; status.replaceChildren(h("div", { class: "card" }, h("span", { class: "spinner" }), " Looking at it…"));
    try {
      if (urls.length === 1) { state.preview = await api("/api/preview", { method: "POST", json: { url: urls[0] } }); state.draft.preview = initChoices(state.preview); status.replaceChildren(previewCard()); }
      else { const d = await api("/api/titles", { method: "POST", json: { urls } }); state.preview = { kind: "playlist", links: true, title: urls.length + " links", entries: d.entries, note: urls.length > 25 ? "The first 25 are used." : "" }; state.draft.preview = initChoices(state.preview); status.replaceChildren(previewCard()); }
    } catch (e) { status.replaceChildren(); toast(e.message, true); }
    check.disabled = false;
  } });
  const paste = navigator.clipboard && navigator.clipboard.readText ? h("button", { class: "btn", text: "📋 Paste", onclick: async () => { try { box.value = await navigator.clipboard.readText(); state.draft.text = box.value; } catch (e) { toast("Your browser didn't allow pasting.", true); } } }) : null;
  const wrap = h("div", null, h("section", { class: "card" }, h("h2", { text: "What shall we grab?" }), h("div", { class: "stack" }, box, h("div", { class: "row" }, check, paste))), status,
    h("section", { class: "card" }, h("h2", { text: "Right now" }), h("div", { id: "jobs-live" }, jobCards((j) => j.kind === "download" || true).slice(0, 6))));
  if (state.preview) status.append(previewCard());
  return wrap;
}
function initChoices(p) {
  return { kind: p.kind === "video" ? "video" : p.kind === "audio" ? "audio" : p.kind === "spotify" ? "audio" : "simple", quality: "best", audio_format: "mp3",
    sections: [], merge: false, subLangs: [], subFlags: { embed: true, file: false, burn: false }, selected: new Set((p.entries || []).map((_, i) => i)), batchQuality: "best" };
}
function subMode(f) { return f.burn ? (f.file ? "burnfile" : "burn") : (f.embed && f.file ? "both" : f.embed ? "embed" : "file"); }
function chip(label, pressed, onclick, small) { return h("button", { class: "chip", type: "button", "aria-pressed": pressed ? "true" : "false", onclick }, label, small ? h("small", { text: small }) : null); }
function previewCard() {
  const p = state.preview, c = state.draft.preview; if (!p || !c) return h("div");
  const redraw = () => { const area = document.getElementById("preview-area"); if (area) area.replaceChildren(previewCard()); };
  const thumb = safeImg(p.thumbnail) ? h("img", { class: "thumb", src: p.thumbnail, alt: "", referrerpolicy: "no-referrer", loading: "lazy" }) : h("div", { class: "thumb", text: p.kind === "playlist" ? "🎞️" : "🎬" });
  const meta = [p.site, p.duration ? clock(p.duration) : ""].filter(Boolean).join(" · ");
  const head = h("div", { class: "preview" }, thumb, h("div", { class: "stack" }, h("h2", { text: p.title || "Your link" }), meta ? h("div", { class: "muted", text: meta }) : null, p.note ? h("div", { class: "small muted", text: p.note }) : null));
  const card = h("section", { class: "card" }, head);
  const sizes = p.sizes || {};

  if (p.kind === "playlist") {
    card.append(h("h3", { text: "Pick what you want" }),
      h("div", { class: "row" }, h("button", { class: "btn small", text: "All", onclick: () => { c.selected = new Set(p.entries.map((_, i) => i)); redraw(); } }), h("button", { class: "btn small", text: "None", onclick: () => { c.selected = new Set(); redraw(); } }), h("span", { class: "muted small", text: c.selected.size + " selected" })),
      h("div", {}, p.entries.map((e, i) => h("label", { class: "entry" }, h("input", { type: "checkbox", checked: c.selected.has(i), onchange: (ev) => { ev.target.checked ? c.selected.add(i) : c.selected.delete(i); redraw(); } }), h("span", { class: "t", text: e.title || e.url }), e.duration ? h("span", { class: "small muted", text: clock(e.duration) }) : null))),
      h("h3", { text: "Quality" }), h("div", { class: "chips" }, ["best", "1080p", "720p", "480p", "360p", "mp3", "opus"].map((q) => chip(q === "mp3" ? "♪ MP3" : q === "opus" ? "♪ Opus" : q === "best" ? "★ Best" : q, c.batchQuality === q, () => { c.batchQuality = q; redraw(); }))),
      h("div", { class: "row" }, h("button", { class: "btn primary", disabled: !c.selected.size, text: `Download ${c.selected.size} 🍬`, onclick: async () => {
        const picked = [...c.selected].map((i) => p.entries[i]); const titles = Object.fromEntries(picked.map((e) => [e.url, e.title || ""]));
        const r = await act(() => api("/api/batch", { method: "POST", json: { urls: picked.map((e) => e.url), quality: c.batchQuality, titles } }));
        if (r) { if (r.stopped) toast(r.stopped, true); state.preview = null; state.tab = "jobs"; render(); }
      } })));
    return card;
  }

  if (p.kind === "video" || p.kind === "audio" || p.kind === "spotify") {
    if (p.kind === "video") {
      const heights = []; for (const hh of p.heights) if (!heights.length || heights[heights.length - 1] - hh >= 60) heights.push(hh);
      card.append(h("h3", { text: "Video" }), h("div", { class: "chips" },
        chip("★ Best", c.kind === "video" && c.quality === "best", () => { c.kind = "video"; c.quality = "best"; redraw(); }, sizes.best ? "~" + sizeText(sizes.best) : ""),
        heights.map((hh) => chip(hh + "p", c.kind === "video" && c.quality === hh + "p", () => { c.kind = "video"; c.quality = hh + "p"; redraw(); }, sizes[String(hh)] ? "~" + sizeText(sizes[String(hh)]) : "")),
        chip("↓ Smallest", c.kind === "video" && c.quality === "worst", () => { c.kind = "video"; c.quality = "worst"; redraw(); }, sizes.worst ? "~" + sizeText(sizes.worst) : "")));
    }
    if (p.has_audio) {
      const fmts = [["mp3", "♪ MP3"], ["opus", "♪ Opus"], ["m4a", "♪ M4A"], ["flac", "♪ FLAC"]];
      if (p.chapters && p.chapters.length >= 2) fmts.push(["mp3split", `♪ MP3 · ${p.chapters.length} tracks`]);
      card.append(h("h3", { text: "Audio only" }), h("div", { class: "chips" }, fmts.map(([f, l]) => chip(l, c.kind === "audio" && c.audio_format === f, () => { c.kind = "audio"; c.audio_format = f; redraw(); }, sizes[f] ? "~" + sizeText(sizes[f]) : ""))));
    }
    if (p.duration && c.audio_format !== "mp3split") card.append(sectionsEditor(p, c, redraw));
    if (p.kind === "video" && p.subtitles && p.subtitles.length && !c.sections.length && c.kind === "video") card.append(subtitleEditor(p, c, redraw));
  } else {
    card.append(h("p", { class: "muted", text: p.kind === "simple" ? "This looks like a photo, gallery or plain file." : "I'll try everything I have." }));
  }
  card.append(h("div", { class: "row", style: false }, h("button", { class: "btn primary", text: "Download 🍬", onclick: () => startDownload(p, c) })));
  return card;
}
function sectionsEditor(p, c, redraw) {
  const wrap = h("div", null, h("h3", { text: "Only part of it (optional)" }));
  c.sections.forEach((s, i) => wrap.append(h("div", { class: "sec-row" },
    h("input", { type: "text", placeholder: "Start (e.g. 1:20)", value: s.start, "aria-label": "Start", oninput: (e) => { s.start = e.target.value; } }),
    h("input", { type: "text", placeholder: "End (e.g. 2:45)", value: s.end, "aria-label": "End", oninput: (e) => { s.end = e.target.value; } }),
    h("button", { class: "btn small danger", type: "button", text: "✕", "aria-label": "Remove section", onclick: () => { c.sections.splice(i, 1); redraw(); } }))));
  const row = h("div", { class: "row" }, h("button", { class: "btn small", type: "button", text: "✄ Add section", onclick: () => { if (c.sections.length < 10) { c.sections.push({ start: "", end: "" }); redraw(); } } }));
  if (c.sections.length > 1) row.append(h("label", { class: "switch" }, h("input", { type: "checkbox", checked: c.merge, onchange: (e) => { c.merge = e.target.checked; } }), "Merge into one file"));
  wrap.append(row);
  if (p.chapters && p.chapters.length) wrap.append(h("div", { class: "chips" }, p.chapters.slice(0, 30).map((ch) => chip(ch.title.slice(0, 28), c.sections.some((s) => s.start === clock(ch.start) && s.end === clock(ch.end)), () => {
    const i = c.sections.findIndex((s) => s.start === clock(ch.start) && s.end === clock(ch.end));
    if (i >= 0) c.sections.splice(i, 1); else if (c.sections.length < 10) c.sections.push({ start: clock(ch.start), end: clock(ch.end) }); redraw(); }, clock(ch.start)))));
  return wrap;
}
function subtitleEditor(p, c, redraw) {
  const wrap = h("div", null, h("h3", { text: "Subtitles (optional)" }),
    h("div", { class: "chips" }, p.subtitles.map((t) => chip(t.name + (t.auto ? " (auto)" : ""), c.subLangs.includes(t.code), () => {
      const i = c.subLangs.indexOf(t.code); if (i >= 0) c.subLangs.splice(i, 1); else if (c.subLangs.length < 4) c.subLangs.push(t.code); else toast("Up to 4 languages.", true); redraw(); }))));
  if (c.subLangs.length) {
    const f = c.subFlags;
    const sw = (key, label) => h("label", { class: "switch" }, h("input", { type: "checkbox", checked: f[key], onchange: (e) => {
      const on = e.target.checked; const next = { ...f, [key]: on }; if (on && key === "embed") next.burn = false; if (on && key === "burn") next.embed = false;
      if (!next.embed && !next.file && !next.burn) { toast("Keep at least one option on.", true); return redraw(); } c.subFlags = next; redraw(); } }), label);
    wrap.append(sw("embed", "Inside the video (soft track)"), sw("file", "As a separate .srt file"), sw("burn", "Burned into the picture (first language; takes longer)"));
  }
  return wrap;
}
async function startDownload(p, c) {
  const req = { url: p.url, kind: c.kind, quality: c.quality, audio_format: c.audio_format, duration: p.duration, title: p.title, had_preview: p.kind === "video" || p.kind === "audio",
    sections: c.sections.filter((s) => s.start || s.end), merge: c.merge };
  if (c.subLangs.length && c.kind === "video" && !req.sections.length) req.subs = { langs: c.subLangs, mode: subMode(c.subFlags) };
  const r = await act(() => api("/api/download", { method: "POST", json: req }), "Added to the queue 🍬");
  if (r) { state.preview = null; state.draft.text = ""; state.jobs = [r, ...state.jobs.filter((j) => j.rid !== r.rid)]; render(); }
}

// ---------------------------------------------------------------- queue tab
function jobsView() { return h("section", { class: "card" }, h("h2", { text: "Queue & results" }), h("div", { id: "jobs-live" }, jobCards())); }

// ---------------------------------------------------------------- history
async function loadHistory(page) { const d = await act(() => api("/api/history?page=" + page)); if (d) { state.history = d; rerenderView(); } }
function historyView() {
  const hst = state.history; const card = h("section", { class: "card" }, h("h2", { text: "History" }));
  if (!hst) return card.append(h("span", { class: "spinner" })), card;
  if (!hst.items.length) { card.append(h("div", { class: "empty" }, h("div", { class: "big", text: "🕘" }), "Nothing downloaded yet.")); return card; }
  card.append(h("div", { class: "table-wrap" }, h("table", null, h("tbody", null, hst.items.map((it) => h("tr", null,
    h("td", { text: it.status === "success" ? "✅" : it.status === "failed" ? "⚠️" : "✋" }),
    h("td", null, h("div", { text: (it.title || it.url).slice(0, 80) }), h("div", { class: "small muted", text: [it.mode, it.quality, it.at ? new Date(it.at).toLocaleString() : ""].filter(Boolean).join(" · ") })),
    h("td", null, /^https?:\/\//.test(it.url) ? h("button", { class: "btn small", text: "Again", onclick: () => { state.draft.text = it.url; state.preview = null; state.tab = "download"; render(); } }) : null)))))));
  const pages = Math.ceil(hst.total / hst.size);
  card.append(h("div", { class: "row" }, h("button", { class: "btn small", text: "← Newer", disabled: hst.page === 0, onclick: () => loadHistory(hst.page - 1) }), h("span", { class: "muted small", text: `Page ${hst.page + 1} of ${pages}` }),
    h("button", { class: "btn small", text: "Older →", disabled: hst.page + 1 >= pages, onclick: () => loadHistory(hst.page + 1) }), h("div", { class: "spacer" }),
    h("button", { class: "btn small danger", text: "Clear history", onclick: async () => { if (confirm("Clear your whole history?")) { await act(() => api("/api/history/clear", { method: "POST" }), "Cleared"); loadHistory(0); } } })));
  return card;
}

// ---------------------------------------------------------------- toolbox
async function loadTools() { const d = await act(() => api("/api/tools")); if (d) { state.toolsList = d.items; if (!state.tool || !d.items.find((t) => t.rid === state.tool.rid)) state.tool = d.items[0] || null; rerenderView(); } }
function uploadFile(file, url, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open("PUT", url); xhr.setRequestHeader("X-CSRF-Token", state.csrf); xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => { let d = {}; try { d = JSON.parse(xhr.responseText); } catch (e) { /* */ } xhr.status < 300 ? resolve(d) : reject(new Error(d.error || "Upload failed.")); };
    xhr.onerror = () => reject(new Error("Upload failed.")); xhr.send(file);
  });
}
function toolsView() {
  const wrap = h("div", null); const prog = h("div", { class: "small muted" });
  const input = h("input", { type: "file", accept: "video/*,audio/*", class: "hidden" });
  const pick = async (file) => { if (!file) return; prog.textContent = "Uploading…"; try {
    const item = await uploadFile(file, "/api/tools/upload?name=" + encodeURIComponent(file.name), (f) => { prog.textContent = `Uploading… ${Math.round(f * 100)}%`; });
    state.tool = item; await loadTools(); state.tool = state.toolsList.find((t) => t.rid === item.rid) || item; rerenderView(); toast("Got it ✓"); } catch (e) { prog.textContent = ""; toast(e.message, true); } };
  input.addEventListener("change", () => pick(input.files[0]));
  const drop = h("div", { class: "drop", tabindex: "0", role: "button", onclick: () => input.click(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") input.click(); },
    ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); }, ondragleave: () => drop.classList.remove("over"), ondrop: (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); } },
    h("div", { class: "big", text: "🧰" }), h("b", { text: "Drop a video or audio file here" }), h("div", { class: "muted small", text: "or tap to choose (up to 2 GB, kept for an hour)" }), prog);
  wrap.append(h("section", { class: "card" }, h("h2", { text: "Toolbox" }), drop, input));
  const t = state.tool; if (t) wrap.append(toolCard(t));
  return wrap;
}
function toolCard(t) {
  const sel = state.draft.tool || (state.draft.tool = { which: "trim", exact: false, fmt: "mp3", target: null, range: "" });
  const redraw = () => rerenderView();
  const card = h("section", { class: "card" }, h("div", { class: "row" }, h("h2", { class: "grow", text: t.name }), h("button", { class: "btn small danger", text: "Remove", onclick: async () => { await act(() => api("/api/tools/" + t.rid, { method: "DELETE" })); state.tool = null; loadTools(); } })),
    h("div", { class: "muted small", text: [clock(t.duration), sizeText(t.size), t.has_video && t.width ? `${t.width}×${t.height}` : "audio"].filter(Boolean).join(" · ") }));
  const tools = [["trim", "✄", "Trim", "Keep just a part"], t.has_audio ? ["audio", "♪", "Extract audio", "MP3 or M4A"] : null,
    t.has_video ? ["compress", "⇩", "Compress", "Fit a size"] : null, t.has_video ? ["gif", "◍", "GIF", "Short loop"] : null,
    t.burn_ok ? ["burn", "◧", "Burn subtitles", "Draw an .srt"] : null, ["strip", "⌫", "Remove metadata", "Tags & location"]].filter(Boolean);
  card.append(h("div", { class: "tools-grid" }, tools.map(([id, ico, name, sub]) => h("button", { class: "tool", "aria-pressed": sel.which === id ? "true" : "false", onclick: () => { sel.which = id; redraw(); } }, h("b", { text: `${ico} ${name}` }), h("span", { text: sub })))));
  const opts = h("div", { class: "stack", style: false });
  const run = (body) => act(() => api(`/api/tools/${t.rid}/run`, { method: "POST", json: { tool: sel.which, ...body } }), "Started 🍬").then((r) => { if (r) { state.jobs = [r, ...state.jobs.filter((j) => j.rid !== r.rid)]; state.tab = "jobs"; render(); } });
  if (sel.which === "trim" || sel.which === "gif") {
    const range = h("input", { type: "text", placeholder: sel.which === "trim" ? "1:20 2:45  (start end)" : "1:20 5  (start, seconds)", value: sel.range, oninput: (e) => { sel.range = e.target.value; }, "aria-label": "Range" });
    opts.append(h("label", { class: "field", text: sel.which === "trim" ? "Start and end (only a start means 'to the end')" : "Start and, if you like, seconds (max 15)" }), range);
    if (sel.which === "trim" && t.has_video) opts.append(h("label", { class: "switch" }, h("input", { type: "checkbox", checked: sel.exact, onchange: (e) => { sel.exact = e.target.checked; } }), "Exact cut (slower; fast mode may start a moment early)"));
    opts.append(h("button", { class: "btn primary", text: "Go ✨", onclick: () => run({ range: sel.range, exact: sel.exact }) }));
  } else if (sel.which === "audio") {
    opts.append(h("div", { class: "chips" }, ["mp3", "m4a"].map((f) => chip(f.toUpperCase(), sel.fmt === f, () => { sel.fmt = f; redraw(); }))), h("button", { class: "btn primary", text: "Extract ♪", onclick: () => run({ audio_format: sel.fmt }) }));
  } else if (sel.which === "compress") {
    opts.append(t.compress_targets.length ? h("div", { class: "chips" }, t.compress_targets.map((mb) => chip(mb + " MB", sel.target === mb, () => { sel.target = mb; redraw(); }))) : h("div", { class: "muted", text: "This file is already small." }),
      h("button", { class: "btn primary", disabled: !sel.target, text: "Compress ⇩", onclick: () => run({ target_mb: sel.target }) }));
  } else if (sel.which === "burn") {
    const srt = h("input", { type: "file", accept: ".srt", class: "hidden" });
    srt.addEventListener("change", async () => { if (!srt.files[0]) return; await act(() => uploadFile(srt.files[0], `/api/tools/${t.rid}/srt`), "Subtitles added ✓"); loadTools(); });
    opts.append(h("div", { class: "row" }, h("button", { class: "btn", text: t.has_srt ? "Replace .srt" : "Choose .srt file", onclick: () => srt.click() }), t.has_srt ? h("span", { class: "pill ok", text: "✓ subtitles ready" }) : null), srt,
      h("button", { class: "btn primary", disabled: !t.has_srt, text: "Burn in ◧", onclick: () => run({}) }));
  } else { opts.append(h("button", { class: "btn primary", text: "Remove metadata ⌫", onclick: () => run({}) })); }
  card.append(opts);
  return card;
}

// ---------------------------------------------------------------- settings
async function loadSettings() {
  const [s, c] = await Promise.all([act(() => api("/api/settings")), act(() => api("/api/cookies"))]);
  if (s) { state.settings = s.settings; state.settingKeys = s.keys; } if (c) state.cookies = c.sites; rerenderView();
}
const CHOICES = { mode: ["video", "audio"], quality: ["best", "1080p", "720p", "480p", "360p", "worst"], video_codec: ["any", "h264", "vp9", "av1"], audio_format: ["mp3", "m4a", "opus", "flac", "wav"],
  audio_bitrate: ["128", "192", "256", "320"], playlist_mode: ["single", "full", "range"], bar_style: ["auto", "candy", "jar", "pacman", "slider", "moon"] };
const LABELS = { mode: "Default type", quality: "Default video quality", video_codec: "Preferred codec", audio_format: "Audio format", audio_bitrate: "Audio bitrate (kbps)", playlist_mode: "Playlist links",
  embed_thumbnail: "Cover art in files", embed_metadata: "Title/artist tags in files", sponsorblock: "Skip sponsor segments (YouTube)", use_archive: "Skip things already downloaded", show_sizes: "Show size estimates",
  cookies_enabled: "Use my cookies", rate_limit_kbps: "Speed limit (kbps, 0 = none)", concurrent_fragments: "Parallel fragments (1-16)", playlist_range: "Playlist range (like 1-5)", bar_style: "Progress look (Telegram bot)" };
async function saveSetting(key, value) { const r = await act(() => api("/api/settings", { method: "PUT", json: { [key]: value } })); if (r) state.settings = r.settings; }
function settingsView() {
  const s = state.settings; const wrap = h("div", null);
  if (!s) return wrap.append(h("span", { class: "spinner" })), wrap;
  const form = h("div", { class: "stack" });
  for (const key of state.settingKeys.length ? state.settingKeys : Object.keys(s)) {
    if (!(key in s)) continue; const v = s[key], label = LABELS[key] || key;
    if (typeof v === "boolean") form.append(h("label", { class: "switch" }, h("input", { type: "checkbox", checked: v, onchange: (e) => saveSetting(key, e.target.checked) }), label));
    else if (CHOICES[key]) form.append(h("div", null, h("label", { class: "field", text: label }), h("select", { onchange: (e) => saveSetting(key, e.target.value) }, CHOICES[key].map((o) => h("option", { value: o, selected: String(v) === o, text: o })))));
    else form.append(h("div", null, h("label", { class: "field", text: label }), h("input", { type: typeof v === "number" ? "number" : "text", value: v, onchange: (e) => saveSetting(key, typeof v === "number" ? parseInt(e.target.value || "0", 10) : e.target.value) })));
  }
  wrap.append(h("section", { class: "card" }, h("h2", { text: "Defaults" }), h("p", { class: "muted small", text: "These are shared with the Telegram bot when your account is linked to it." }), form,
    h("div", { class: "row", style: false }, h("button", { class: "btn small danger", text: "Reset to defaults", onclick: async () => { if (confirm("Reset all your defaults?")) { const r = await act(() => api("/api/settings/reset", { method: "POST" }), "Reset ✓"); if (r) { state.settings = r.settings; rerenderView(); } } } }))));
  wrap.append(cookiesCard(), h("section", { class: "card" }, h("h2", { text: "Password" }), passwordView(false)));
  return wrap;
}
function cookiesCard() {
  const file = h("input", { type: "file", accept: ".txt,text/plain", class: "hidden" });
  file.addEventListener("change", async () => { if (!file.files[0]) return; const r = await act(() => api("/api/cookies", { method: "POST", body: file.files[0] })); file.value = "";
    if (r) { state.cookies = r.sites; toast("Cookies saved for " + r.saved.join(", ") + (r.warnings.length ? " - " + r.warnings[0] : "")); rerenderView(); } });
  return h("section", { class: "card" }, h("h2", { text: "Cookies (for sites that want a login)" }),
    h("p", { class: "muted small", text: "Export a cookies.txt from your browser while logged in. Each site is kept separately: sending Instagram's file doesn't remove YouTube's." }),
    state.cookies.length ? h("div", { class: "stack" }, state.cookies.map((c) => h("div", { class: "row" }, h("span", { class: "grow", text: c.label + ` · ${c.count} cookies` }), c.expired ? h("span", { class: "pill bad", text: "expired" }) : h("span", { class: "pill ok", text: "active" }),
      h("button", { class: "btn small danger", text: "Remove", onclick: async () => { const r = await act(() => api("/api/cookies/" + encodeURIComponent(c.site), { method: "DELETE" })); if (r) { state.cookies = r.sites; rerenderView(); } } })))) : h("div", { class: "muted", text: "No cookies yet." }),
    h("div", { class: "row", style: false }, h("button", { class: "btn", text: "Upload cookies.txt", onclick: () => file.click() }), file));
}
// ---------------------------------------------------------------- admin
async function loadAdmin() {
  const [o, a, up, au] = await Promise.all([act(() => api("/api/admin/overview")), act(() => api("/api/admin/accounts")), act(() => api("/api/admin/sharing")), act(() => api("/api/admin/audit"))]);
  state.admin = { overview: o, accounts: a && a.accounts, sharing: up, audit: au && au.items, warp: state.admin.warp }; rerenderView();
}
function secretModal(title, username, password) {
  const back = h("div", { class: "modal-back", role: "dialog", "aria-modal": "true" }, h("div", { class: "modal stack" }, h("h2", { text: title }), h("div", { class: "muted", text: "Username" }), h("div", { class: "secret", text: username }),
    h("div", { class: "muted", text: "One-time password (shown only now)" }), h("div", { class: "secret", text: password }),
    h("div", { class: "small muted", text: "They must choose their own password at first sign-in." }),
    h("div", { class: "row" }, h("button", { class: "btn", text: "Copy", onclick: async () => { try { await navigator.clipboard.writeText(password); toast("Copied ✓"); } catch (e) { toast("Select and copy it by hand.", true); } } }), h("button", { class: "btn primary", text: "Done", onclick: () => back.remove() }))));
  document.body.append(back);
}
function adminView() {
  const A = state.admin; const wrap = h("div", null); if (!A.overview) return wrap.append(h("span", { class: "spinner" })), wrap;
  const o = A.overview;
  wrap.append(h("section", { class: "card" }, h("h2", { text: "Overview" }), h("div", { class: "stats" }, [["In queue", o.queue], ["Downloads", o.downloads.total], ["Succeeded", o.downloads.ok], ["Failed", o.downloads.failed], ["Bot users", o.bot_users], ["Web accounts", o.accounts]].map(([l, v]) => h("div", { class: "stat" }, h("b", { text: v }), h("span", { class: "muted small", text: l }))))));
  // accounts
  const nu = h("input", { type: "text", placeholder: "username", autocapitalize: "none", "aria-label": "New username" }), tg = h("input", { type: "text", inputmode: "numeric", placeholder: "Telegram id (optional)", "aria-label": "Telegram id" });
  const role = h("select", { "aria-label": "Role" }, h("option", { value: "user", text: "user" }), h("option", { value: "admin", text: "admin" }));
  const rows = (A.accounts || []).map((a) => h("tr", null, h("td", null, h("b", { text: a.username }), a.telegram_id ? h("div", { class: "small muted", text: "tg " + a.telegram_id }) : null),
    h("td", null, h("select", { "aria-label": "Role of " + a.username, onchange: async (e) => { const r = await act(() => api("/api/admin/accounts/" + a.id, { method: "PATCH", json: { role: e.target.value } }), "Saved ✓"); if (!r) loadAdmin(); } }, ["user", "admin"].map((r) => h("option", { value: r, selected: a.role === r, text: r })))),
    h("td", { text: a.disabled ? "⛔ blocked" : a.last_login ? new Date(a.last_login * 1000).toLocaleDateString() : "never" }),
    h("td", null, h("div", { class: "row" },
      h("button", { class: "btn small", text: "New password", onclick: async () => { if (!confirm("Sign " + a.username + " out everywhere and make a new password?")) return; const r = await act(() => api(`/api/admin/accounts/${a.id}/reset`, { method: "POST" })); if (r) secretModal("New password", a.username, r.password); } }),
      h("button", { class: "btn small", text: a.disabled ? "Allow" : "Block", onclick: async () => { await act(() => api("/api/admin/accounts/" + a.id, { method: "PATCH", json: { disabled: !a.disabled } })); loadAdmin(); } }),
      h("button", { class: "btn small danger", text: "Delete", onclick: async () => { if (confirm("Delete " + a.username + " and their files?")) { await act(() => api("/api/admin/accounts/" + a.id, { method: "DELETE" })); loadAdmin(); } } })))));
  wrap.append(h("section", { class: "card" }, h("h2", { text: "Accounts" }), h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, ["Account", "Role", "Last sign-in", ""].map((t) => h("th", { text: t })))), h("tbody", null, rows))),
    h("h3", { text: "Add an account" }), h("div", { class: "row" }, h("div", { class: "grow" }, nu), h("div", { class: "grow" }, tg), role,
      h("button", { class: "btn primary", text: "Create", onclick: async () => { const r = await act(() => api("/api/admin/accounts", { method: "POST", json: { username: nu.value, telegram_id: tg.value, role: role.value } })); if (r) { secretModal("Account created", r.account.username, r.password); loadAdmin(); } } }))));
  // sharing
  const S = A.sharing || {};
  const on = h("input", { type: "checkbox", checked: S.enabled !== false });
  const num = (v, label) => { const i = h("input", { type: "number", min: "1", value: v, "aria-label": label }); return [h("div", null, h("label", { class: "field", text: label }), i), i]; };
  const accSel = (v, label) => { const sel = h("select", { "aria-label": label }, ACCESS.map(([val, text]) => h("option", { value: val, selected: val === v, text }))); return [h("div", null, h("label", { class: "field", text: label }), sel), sel]; };
  const [da, daI] = accSel(S.default_access, "New links can be opened by (default)"), [ma, maI] = accSel(S.min_access, "Never looser than");
  const [dh, dhI] = num(S.default_hours, "Link lasts (hours, default)"), [mh, mhI] = num(S.max_hours, "Longest allowed (hours)"), [qm, qmI] = num(S.user_quota_mb, "Per person (MB of shared files)");
  const slist = (S.shares || []).map((i) => h("div", { class: "file linkrow" }, h("div", { class: "n", title: i.name, text: i.name }),
    h("span", { class: "small muted", text: `owner ${i.owner} · ${["anyone", "signed-in", "owner only"][i.access] || ""}${i.has_password ? " + password" : ""} · ${sizeText(i.size)} · ${whenLeft(i.expires)} left · ${i.downloads} downloads` }),
    h("button", { class: "btn small danger", text: "Delete", onclick: async () => { await act(() => api("/api/admin/shares/" + i.id, { method: "DELETE" })); loadAdmin(); } })));
  wrap.append(h("section", { class: "card" }, h("h2", { text: "🔗 Download links" }),
    h("p", { class: "muted small", text: `Finished files can be kept here and shared by a link that expires (web app and Telegram bot). Links open at ${S.public_url || "?"}. ${S.count || 0} active, ${sizeText(S.bytes || 0) || "0 MB"} used.` }),
    S.web_enabled === false ? h("p", { class: "muted small", text: "The web server is off, so links can't be opened." }) : null,
    h("div", { class: "stack" }, h("label", { class: "switch" }, on, "Links on"), dh, mh, qm, da, ma,
      h("div", { class: "row" }, h("button", { class: "btn primary", text: "Save", onclick: async () => {
        const r = await act(() => api("/api/admin/sharing", { method: "PUT", json: { enabled: on.checked, default_hours: parseInt(dhI.value, 10), max_hours: parseInt(mhI.value, 10), user_quota_mb: parseInt(qmI.value, 10), default_access: parseInt(daI.value, 10), min_access: parseInt(maI.value, 10) } }), "Saved ✓"); if (r) loadAdmin(); } }))),
    slist.length ? h("h3", { text: "Active links" }) : null, slist));
  // access
  const allow = h("input", { type: "text", inputmode: "numeric", placeholder: "Telegram id", "aria-label": "Telegram id to allow" });
  wrap.append(h("section", { class: "card" }, h("h2", { text: "Telegram bot access" }), h("div", { class: "chips" }, ["public", "private"].map((m) => chip(m === "public" ? "Anyone can use the bot" : "Only allowed people", o.access_mode === m, async () => { await act(() => api("/api/admin/access", { method: "POST", json: { mode: m } })); loadAdmin(); }))),
    o.access_mode === "private" ? h("div", { class: "stack" }, h("div", { class: "chips" }, o.allowed_users.map((id) => chip(String(id) + " ✕", false, async () => { await act(() => api("/api/admin/access", { method: "POST", json: { remove: id } })); loadAdmin(); }))),
      h("div", { class: "row" }, h("div", { class: "grow" }, allow), h("button", { class: "btn", text: "Allow", onclick: async () => { await act(() => api("/api/admin/access", { method: "POST", json: { allow: allow.value } })); loadAdmin(); } }))) : null));
  // warp
  if (o.warp.configured) { const w = A.warp;
    wrap.append(h("section", { class: "card" }, h("h2", { text: "🌐 WARP address" }), w ? h("p", { text: "Current address: " + (w.ip || "unknown") }) : null,
      h("div", { class: "row" }, h("button", { class: "btn", text: "Check address", onclick: async () => { const r = await act(() => api("/api/admin/warp")); if (r) { state.admin.warp = r; rerenderView(); } } }),
        h("button", { class: "btn danger", text: "Change address", onclick: async () => { if (!confirm("Restart WARP for a new address? Downloads using it may fail for a moment.")) return; const r = await act(() => api("/api/admin/warp/rotate", { method: "POST" })); if (r) { toast(r.message, !r.ok); state.admin.warp = { ...(state.admin.warp || {}), ip: r.new_ip || r.old_ip }; rerenderView(); } } })))); }
  wrap.append(h("section", { class: "card" }, h("h2", { text: "Recent activity" }), h("div", { class: "table-wrap" }, h("table", null, h("tbody", null, (A.audit || []).slice(0, 30).map((e) => h("tr", null, h("td", { class: "small muted", text: new Date(e.at * 1000).toLocaleString() }), h("td", { text: e.username || "-" }), h("td", { text: e.action }), h("td", { class: "small muted", text: e.detail })))))))); 
  return wrap;
}

boot();
