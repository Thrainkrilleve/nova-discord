// Nova's web app. Everything Nova or anyone else wrote is put on the page as text nodes (never
// innerHTML), so a reply can't run script or load anything; links open only for http(s) URLs.
"use strict";

const MAX_FILES = 5;
const MAX_FILE_BYTES = 8 * 1024 * 1024;

const $ = (id) => document.getElementById(id);
const log = $("log"), input = $("input"), send = $("send"), status = $("status");
const fileInput = $("file-input"), filesBox = $("files");
let csrf = "";
let pending = [];   // {name, type, data(base64), size}
let busy = false;

// -- preferences (this browser only; the page works the same if storage is blocked) -----------------

const DEFAULT_PREFS = { theme: "system", size: "medium", clock: "auto", stamps: true, enter: true };
const prefs = { ...DEFAULT_PREFS };
try { Object.assign(prefs, JSON.parse(localStorage.getItem("nova-prefs") || "{}")); } catch { /* blocked: defaults */ }

function savePrefs() {
  try { localStorage.setItem("nova-prefs", JSON.stringify(prefs)); } catch { /* blocked: this visit only */ }
}

function applyPrefs() {
  const root = document.documentElement;
  if (prefs.theme === "system") delete root.dataset.theme; else root.dataset.theme = prefs.theme;
  root.dataset.size = prefs.size;
  root.dataset.stamps = prefs.stamps ? "on" : "off";
  input.placeholder = prefs.enter ? "Message Nova" : "Message Nova (Ctrl+Enter to send)";
}
applyPrefs();

// -- rendering ---------------------------------------------------------------------------------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function safeUrl(url) {
  try {
    const u = new URL(url, location.origin);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

function link(text, url) {
  const href = safeUrl(url);
  if (!href) return document.createTextNode(text);
  const a = el("a", "", text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

// Inline markdown: `code`, **bold**, *italic* / _italic_, [text](url), bare URLs.
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\((?:https?:\/\/|\/)[^)\s]+\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])|(\*[^*\s][^*\n]*\*)|(\b_[^_\n]+_\b)/g;

function inline(parent, text) {
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    const t = m[0];
    if (m[1]) parent.append(el("code", "", t.slice(1, -1)));
    else if (m[2]) { const b = el("strong"); inline(b, t.slice(2, -2)); parent.append(b); }
    else if (m[3]) {
      const close = t.indexOf("](");
      parent.append(link(t.slice(1, close), t.slice(close + 2, -1)));
    }
    else if (m[4]) parent.append(link(t, t));
    else { const i = el("em"); inline(i, t.slice(1, -1)); parent.append(i); }
    last = m.index + t.length;
  }
  if (last < text.length) parent.append(text.slice(last));
}

// Block markdown: code fences, headings, Discord's -# subtext, quotes, lists, paragraphs.
function markdown(text) {
  const root = document.createDocumentFragment();
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let para = [], list = null;
  const flush = () => {
    if (para.length) {
      const p = el("p");
      para.forEach((line, i) => { if (i) p.append(el("br")); inline(p, line); });
      root.append(p);
      para = [];
    }
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (/^```/.test(line)) {
      flush();
      const body = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) body.push(lines[i]);
      const pre = el("pre");
      pre.append(el("code", "", body.join("\n")));
      root.append(pre);
    } else if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flush();
      const h = el(m[1].length <= 2 ? "h3" : "h4");
      inline(h, m[2]);
      root.append(h);
    } else if ((m = /^-#\s+(.*)$/.exec(line))) {
      flush();
      const s = el("p", "sub");
      inline(s, m[1]);
      root.append(s);
    } else if ((m = /^>\s?(.*)$/.exec(line))) {
      flush();
      const q = el("blockquote");
      inline(q, m[1]);
      root.append(q);
    } else if ((m = /^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/.exec(line))) {
      if (para.length) { const keep = list; flush(); list = keep; }
      const kind = m[1] ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== kind) { list = el(kind); root.append(list); }
      const li = el("li");
      inline(li, m[2]);
      list.append(li);
    } else if (!line.trim()) {
      flush();
    } else {
      list = null;
      para.push(line);
    }
  }
  flush();
  return root;
}

// -- times -----------------------------------------------------------------------------------------

let lastDay = "";

function timeFormat() {
  const hour12 = prefs.clock === "12" ? true : prefs.clock === "24" ? false : undefined;
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", hour12 });
}

function dayLabel(date) {
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  const opts = { weekday: "short", day: "numeric", month: "short" };
  if (date.getFullYear() !== today.getFullYear()) opts.year = "numeric";
  return date.toLocaleDateString(undefined, opts);
}

function stamp(date) {
  const s = el("time", "stamp", timeFormat().format(date));
  s.dateTime = date.toISOString();
  s.title = date.toLocaleString(undefined, { dateStyle: "full", timeStyle: "short" });
  return s;
}

function restamp() {
  const fmt = timeFormat();
  for (const s of log.querySelectorAll("time.stamp")) s.textContent = fmt.format(new Date(s.dateTime));
}

function addMessage(role, text, extra, at) {
  $("empty").hidden = true;
  const date = at ? new Date(at) : null;
  if (date && !isNaN(date)) {
    const day = date.toDateString();
    if (day !== lastDay) {
      log.append(el("div", "day", dayLabel(date)));
      lastDay = day;
    }
  }
  const row = el("div", `msg ${role}${extra ? " " + extra : ""}`);
  const bubble = el("div", "bubble");
  if (role === "assistant" && !extra) bubble.append(markdown(text));
  else bubble.textContent = text;
  row.append(bubble);
  if (date && !isNaN(date)) row.append(stamp(date));
  log.append(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

// -- talking to Nova -------------------------------------------------------------------------------

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (opts.method === "POST") opts.headers = { "Content-Type": "application/json", "X-Nova-CSRF": csrf, ...opts.headers };
  const resp = await fetch(path, opts);
  if (resp.status === 401) { location.href = "/"; throw new Error("signed out"); }
  return resp;
}

function setStatus(text, bad) {
  status.textContent = text || "";
  status.classList.toggle("bad", !!bad);
}

async function start() {
  const me = await (await api("/web/me")).json();
  csrf = me.csrf;
  $("me-name").textContent = me.name;
  $("signout").hidden = !me.signout;
  $("account-section").hidden = !me.signout;
  const { messages } = await (await api("/web/history")).json();
  for (const m of messages) addMessage(m.role === "user" ? "user" : "assistant", m.content, "", m.at);
  if (!messages.length) $("empty").hidden = false;
  input.focus();
}

async function sendMessage() {
  const text = input.value.trim();
  if (busy || (!text && !pending.length)) return;
  busy = true;
  send.disabled = true;
  setStatus("");
  const files = pending.map(({ name, type, data }) => ({ name, type, data }));
  const row = addMessage("user", text || "(files)", "", new Date().toISOString());
  if (files.length) row.querySelector(".bubble").append(el("div", "attached", "📎 " + files.map((f) => f.name).join(", ")));
  input.value = "";
  autosize();
  pending = [];
  renderChips();
  const thinking = addMessage("assistant", "Nova is thinking…", "thinking");
  try {
    const resp = await api("/web/chat", { method: "POST", body: JSON.stringify({ text, files, tz: Intl.DateTimeFormat().resolvedOptions().timeZone }) });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", done = false;
    while (!done) {
      const chunk = await reader.read();
      done = chunk.done;
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !done });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.t === "wait") thinking.querySelector(".bubble").textContent = `Nova is thinking… ${msg.seconds}s`;
        else if (msg.t === "answer") { thinking.remove(); addMessage("assistant", msg.text || "(no reply)", "", msg.at); }
        else if (msg.t === "error") { thinking.remove(); addMessage("assistant", msg.text, "error"); }
      }
    }
  } catch (e) {
    thinking.remove();
    if (e.message !== "signed out") setStatus(e.message, true);
  } finally {
    busy = false;
    send.disabled = false;
    input.focus();
  }
}

// -- files -----------------------------------------------------------------------------------------

function renderChips() {
  filesBox.replaceChildren();
  filesBox.hidden = !pending.length;
  pending.forEach((f, i) => {
    const chip = el("span", "chip", f.name);
    const x = el("button", "", "×");
    x.type = "button";
    x.title = "Remove";
    x.addEventListener("click", () => { pending.splice(i, 1); renderChips(); });
    chip.append(x);
    filesBox.append(chip);
  });
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function addFiles(list) {
  for (const file of list) {
    if (pending.length >= MAX_FILES) { setStatus(`At most ${MAX_FILES} files at once.`, true); break; }
    if (file.size > MAX_FILE_BYTES) { setStatus(`${file.name} is over 8 MB.`, true); continue; }
    pending.push({ name: file.name || "pasted-image.png", type: file.type, data: await readFile(file), size: file.size });
  }
  renderChips();
}

// -- wiring ----------------------------------------------------------------------------------------

function autosize() {
  const max = window.innerHeight * 0.4;
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, max) + "px";
  input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
}

$("composer").addEventListener("submit", (e) => { e.preventDefault(); sendMessage(); });
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  const sends = prefs.enter ? !e.shiftKey : e.ctrlKey || e.metaKey;
  if (sends) { e.preventDefault(); sendMessage(); }
});
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});
fileInput.addEventListener("change", () => { addFiles([...fileInput.files]); fileInput.value = ""; });
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => { e.preventDefault(); addFiles([...(e.dataTransfer?.files || [])]); });
$("signout").addEventListener("click", async () => {
  await api("/logout", { method: "POST", body: "{}" });
  location.href = "/";
});

// -- settings panel ----------------------------------------------------------------------------------

const dialog = $("settings");
const settingsStatus = $("settings-status");

function settingsNote(text, bad) {
  settingsStatus.textContent = text || "";
  settingsStatus.classList.toggle("bad", !!bad);
}

async function postJSON(path, body) {
  const resp = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function renderFacts(facts) {
  const list = $("facts");
  list.replaceChildren();
  $("facts-empty").hidden = facts.length > 0;
  for (const f of facts) {
    const li = el("li");
    li.append(el("span", "", f.text));
    const del = el("button", "", "Delete");
    del.type = "button";
    del.addEventListener("click", async () => {
      try {
        await postJSON("/web/facts/delete", { id: f.id });
        li.remove();
        if (!list.children.length) $("facts-empty").hidden = false;
        settingsNote("Forgotten.");
      } catch (e) { settingsNote(e.message, true); }
    });
    li.append(del);
    list.append(li);
  }
}

let zonesLoaded = false;

async function openSettings() {
  $("pref-theme").value = prefs.theme;
  $("pref-size").value = prefs.size;
  $("pref-clock").value = prefs.clock;
  $("pref-stamps").checked = prefs.stamps;
  $("pref-enter").checked = prefs.enter;
  $("enter-hint").hidden = prefs.enter;
  settingsNote("");
  if (!zonesLoaded && Intl.supportedValuesOf) {
    const list = $("tz-list");
    for (const z of Intl.supportedValuesOf("timeZone")) { const o = el("option"); o.value = z; list.append(o); }
    zonesLoaded = true;
  }
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  $("tz-browser").textContent = `This device is set to ${browserZone}.`;
  dialog.showModal();
  try {
    const data = await (await api("/web/settings")).json();
    $("tz").value = data.timezone || "";
    renderFacts(data.facts || []);
  } catch (e) { if (e.message !== "signed out") settingsNote(`Couldn't load: ${e.message}`, true); }
}

$("open-settings").addEventListener("click", openSettings);
dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });   // click outside closes

for (const [id, key] of [["pref-theme", "theme"], ["pref-size", "size"], ["pref-clock", "clock"]]) {
  $(id).addEventListener("change", (e) => { prefs[key] = e.target.value; savePrefs(); applyPrefs(); restamp(); });
}
$("pref-stamps").addEventListener("change", (e) => { prefs.stamps = e.target.checked; savePrefs(); applyPrefs(); });
$("pref-enter").addEventListener("change", (e) => {
  prefs.enter = e.target.checked;
  $("enter-hint").hidden = prefs.enter;
  savePrefs();
  applyPrefs();
});

$("tz-save").addEventListener("click", async () => {
  try {
    const data = await postJSON("/web/settings", { timezone: $("tz").value.trim() });
    settingsNote(data.timezone ? `Saved: ${data.timezone}.` : "Cleared: Nova will learn it from this device.");
  } catch (e) { settingsNote(e.message, true); }
});

$("clear-history").addEventListener("click", async () => {
  if (!confirm("Clear the web chat history? Nova can still recall it if you ask.")) return;
  try {
    await postJSON("/web/history/clear");
    for (const node of [...log.children]) if (node.id !== "empty") node.remove();
    lastDay = "";
    $("empty").hidden = false;
    settingsNote("Chat cleared.");
  } catch (e) { settingsNote(e.message, true); }
});

$("signout-all").addEventListener("click", async () => {
  if (!confirm("Sign out on every device?")) return;
  try {
    await postJSON("/logout/everywhere");
    location.href = "/";
  } catch (e) { settingsNote(e.message, true); }
});

start().catch((e) => setStatus(`Couldn't load: ${e.message}`, true));
