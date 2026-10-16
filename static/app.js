"use strict";

const $ = (id) => document.getElementById(id);
const APP_TITLE = "Video Speaker Labeler";
const STATUS = {
  onscreen: "On-screen · A",
  offscreen: "Off-screen · B",
  unclear: "Unclear",
};

let state = null;   // last /api/state response (ready)
let current = 0;    // index into state.items

// ---------------------------------------------------------------- helpers

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.data = data;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("toast");
  $("toast-text").textContent = msg;
  $("toast-icon").setAttribute("href", isError ? "#i-alert" : "#i-check");
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  el.style.animation = "none";
  void el.offsetWidth;  // restart the slide-in animation
  el.style.animation = "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 2600);
}

// UI preferences are stored per project so each project keeps its own settings.
function prefKey(name) { return `labeler:${state ? state.project : ""}:${name}`; }
function getPref(name, fallback) {
  const v = localStorage.getItem(prefKey(name));
  return v === null ? fallback : JSON.parse(v);
}
function setPref(name, value) { localStorage.setItem(prefKey(name), JSON.stringify(value)); }

// The desktop app (pywebview) exposes window.pywebview.api; a plain browser doesn't.
function desktop() { return window.pywebview && window.pywebview.api; }

// ---------------------------------------------------------------- panel views + dataset overviews

// Show one view of a panel with tabs: its tab is active, and only elements marked data-for that view show.
function showPanelView(tabs, view) {
  tabs.querySelectorAll(".panel-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  tabs.closest(".panel").querySelectorAll("[data-for]").forEach((el) => el.classList.toggle("hidden", el.dataset.for !== view));
}

function panelView(tabs) {
  return tabs.querySelector(".panel-tab.active").dataset.view;
}

// Short, for a tile: 3.2 h · 42 min · 35 s
function fmtLength(sec) {
  if (sec >= 3600) return `${(sec / 3600).toFixed(sec >= 360000 ? 0 : 1)} h`;
  if (sec >= 60) return `${Math.round(sec / 60)} min`;
  return `${sec >= 10 || !sec ? Math.round(sec) : sec.toFixed(1)} s`;
}

// Exact, for a tooltip: 3h 12m 5s
function fmtLong(sec) {
  const t = Math.round(sec), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;
}

// A dataset overview: tiles [value, label, tooltip, class], then sections {title, rows: [{label, value, text, cls, title}]}
// drawn as bars scaled to the section's largest value, then note lines.
function renderDataset(el, tiles, sections, notes = []) {
  const make = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  el.replaceChildren();
  const grid = make("div", "ds-tiles");
  for (const [value, label, title, cls] of tiles) {
    const tile = make("div", `ds-tile ${cls || ""}`);
    tile.title = title || "";
    tile.append(make("b", "", value), make("span", "", label));
    grid.append(tile);
  }
  el.append(grid);
  for (const { title, rows } of sections) {
    if (!rows.length) continue;
    const section = make("section", "ds-section");
    section.append(make("h3", "", title));
    const max = Math.max(...rows.map((r) => r.value), 0) || 1;
    for (const r of rows) {
      const row = make("div", "ds-row");
      row.title = r.title || "";
      const meter = make("div", `ds-meter ${r.cls || ""}`);
      const fill = make("i");
      fill.style.width = `${(100 * r.value) / max}%`;
      meter.append(fill);
      row.append(make("span", "ds-label", r.label), meter, make("span", "ds-num", r.text));
      section.append(row);
    }
    el.append(section);
  }
  for (const line of notes) el.append(make("p", "ds-note", line));
}

function datasetMessage(el, text) {
  el.replaceChildren();
  const p = document.createElement("p");
  p.className = "ds-note";
  p.textContent = text;
  el.append(p);
}

// ---------------------------------------------------------------- theme

const THEME_KEY = "labeler:theme";
const darkQuery = matchMedia("(prefers-color-scheme: dark)");

function themeChoice() { return localStorage.getItem(THEME_KEY) || "system"; }

function applyTheme(animate = false) {
  const choice = themeChoice();
  const dark = choice === "dark" || (choice === "system" && darkQuery.matches);
  if (animate) {
    document.body.classList.add("theme-anim");
    setTimeout(() => document.body.classList.remove("theme-anim"), 250);
  }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.querySelectorAll("#theme-switch button").forEach((b) =>
    b.classList.toggle("active", b.dataset.themeChoice === choice));
  if (desktop()) desktop().set_dark_title_bar(dark).catch(() => {});
}

function setTheme(choice) {
  localStorage.setItem(THEME_KEY, choice);
  applyTheme(true);
}

darkQuery.addEventListener("change", () => { if (themeChoice() === "system") applyTheme(true); });

// ---------------------------------------------------------------- settings menu

function toggleMenu(open) {
  const show = open ?? $("menu").classList.contains("hidden");
  $("menu").classList.toggle("hidden", !show);
  $("menu-btn").setAttribute("aria-expanded", String(show));
}

// ---------------------------------------------------------------- screens

const SECTIONS = ["home", "setup", "labeler", "split-setup", "splitter", "downloader"];
const MODE_KEY = "labeler:mode";

// Show one screen. The modes are the downloader, the splitter (split-setup + splitter)
// and the labeler (setup + labeler), switched with the tabs in the top bar.
function showSection(id) {
  if (id !== "splitter" && !$("splitter").classList.contains("hidden")) splitLeave();
  if (id !== "downloader" && !$("downloader").classList.contains("hidden")) downloadLeave();
  for (const s of SECTIONS) $(s).classList.toggle("hidden", s !== id);
  const mode = id === "home" ? null : id === "downloader" ? "downloader" : id.startsWith("split") ? "splitter" : "labeler";
  document.body.classList.toggle("has-project", id === "labeler");
  document.body.classList.toggle("has-split", id === "splitter");
  document.body.classList.toggle("has-download", id === "downloader");
  $("home-btn").classList.toggle("hidden", id === "home");
  $("workspaces").classList.toggle("hidden", id === "home");
  document.querySelectorAll("[data-workspace]").forEach((b) => b.classList.toggle("active", b.dataset.workspace === mode));
  if (id !== "labeler") $("player").pause();
  if (id !== "splitter") $("split-player").pause();
  if (mode) localStorage.setItem(MODE_KEY, mode);
  if (id === "downloader") document.title = `Video Downloader · ${APP_TITLE}`;
  else if (id !== "labeler" && id !== "splitter") document.title = APP_TITLE;
}

function showHome() {
  showSection("home");
  loadHome().catch(() => {});
}

// ---------------------------------------------------------------- home: recent folders

const RECENT = {
  downloader: { step: "Step 1 · Download", go: "Open" },
  splitter: { step: "Step 2 · Split", go: "Resume" },
  labeler: { step: "Step 3 · Label", go: "Resume" },
};

// "12 min ago", "3 hours ago", "Yesterday", or the date.
function timeAgo(epochSeconds) {
  const s = Date.now() / 1000 - epochSeconds;
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} hour${s < 7200 ? "" : "s"} ago`;
  if (s < 172800) return "Yesterday";
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function recentContents(r) {
  const n = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
  if (r.tool === "downloader") {
    const files = [r.videos || !r.audio ? n(r.videos, "video") : "", r.audio ? n(r.audio, "audio file") : ""];
    return [...files, humanSize(r.bytes), r.seconds ? fmtLength(r.seconds) : ""].filter(Boolean).join(" · ");
  }
  if (r.tool === "splitter") {
    return [n(r.clips, "clip"), r.clips ? fmtLength(r.seconds) : "", r.planned ? `${r.planned} to export` : ""]
      .filter(Boolean).join(" · ");
  }
  return `${r.labeled} / ${r.videos} labeled`;
}

// The folder each tool last worked in, with what's in it, and the free space on the dataset's drive.
async function loadHome() {
  const data = await api("GET", "/api/home");
  const body = $("home-recent-body");
  body.replaceChildren();
  for (const r of data.recent) {
    const tr = document.createElement("tr");
    tr.className = r.tool;
    tr.title = r.source ? `${r.source}  →  ${r.folder}` : r.folder;
    const cell = (cls, text) => {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      if (text !== undefined) td.textContent = text;
      tr.append(td);
      return td;
    };
    const name = document.createElement("span");
    name.className = "recent-name";
    name.append(document.createElement("i"), r.name);
    cell().append(name);
    const stage = document.createElement("span");
    stage.className = "recent-stage";
    stage.textContent = RECENT[r.tool].step + (r.tool === "labeler" && r.videos ? ` (${Math.round((100 * r.labeled) / r.videos)}%)` : "");
    cell().append(stage);
    cell("recent-count", recentContents(r));
    cell("recent-time", timeAgo(r.updated));
    const actions = cell("recent-actions actions");
    for (const [cls, text, run] of [["go", RECENT[r.tool].go, () => openRecent(r)],
                                     ["folder", "Folder", () => api("POST", "/api/reveal-recent", { tool: r.tool })]]) {
      const b = document.createElement("button");
      b.className = cls;
      b.textContent = text;
      b.addEventListener("click", () => run().catch((e) => toast(e.message, true)));
      actions.append(b);
    }
    body.append(tr);
  }
  $("home-recent").classList.toggle("hidden", !data.recent.length);
  $("home-recent-count").textContent = `${data.recent.length} of 3 tools`;
  const disk = data.disk;
  $("home-disk").classList.toggle("hidden", !disk);
  $("home-disk-sep").classList.toggle("hidden", !disk);
  if (disk) {
    $("home-disk").innerHTML = "";
    const label = document.createElement("span");
    label.className = "dim";
    label.textContent = `Disk ${disk.drive}`;
    $("home-disk").append(label, ` ${humanSize(disk.free)} free of ${humanSize(disk.total)}`);
  }
}

// Go back to a recent folder: the downloader with its settings, the splitter and labeler with their folders opened.
async function openRecent(r) {
  if (r.tool === "downloader") return openMode("downloader");
  if (r.tool === "splitter") {
    try {
      await api("POST", "/api/split/setup", { source: r.source, output: r.folder });
    } catch (_) { /* e.g. an export still running: open the splitter as it is */ }
    return loadSplit();
  }
  if (state) return openMode("labeler");  // already open: keep the clip you were on
  await api("POST", "/api/setup", { project: r.project, videos: r.folder, output: r.output });
  return load();
}

function renderUpdateState(info) {
  const el = $("home-update-state");
  el.classList.toggle("hidden", !!info.error);
  if (info.error) return;
  el.className = `status-pill ${info.available ? "new" : "ok"}`;
  el.textContent = info.available ? `● ${info.latest} available` : "● Up to date";
}

function openMode(mode) {
  if (mode === "downloader") return loadDownload();
  return mode === "splitter" ? loadSplit() : load();
}

// ---------------------------------------------------------------- setup screen

function showSetup(cfg = {}) {
  state = null;
  $("player").removeAttribute("src");
  $("player").load();
  showSection("setup");
  $("setup-error").classList.add("hidden");
  if (cfg.last_project !== undefined) $("setup-project").value = cfg.last_project;
  if (cfg.last_videos !== undefined) $("setup-videos").value = cfg.last_videos;
  if (cfg.last_output !== undefined) $("setup-output").value = cfg.last_output;
  $(cfg.last_videos ? "setup-project" : "setup-videos").focus();
}

async function submitSetup() {
  const btn = $("setup-go");
  btn.disabled = true;
  btn.textContent = "Opening…";
  $("setup-error").classList.add("hidden");
  try {
    await api("POST", "/api/setup", {
      project: $("setup-project").value,
      videos: $("setup-videos").value,
      output: $("setup-output").value,
    });
    await load();
  } catch (e) {
    $("setup-error").textContent = e.message;
    $("setup-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Open project";
  }
}

// A folder or file picker: the app window's own, or, in a browser on this PC, one the server opens.
async function pickPath(kind, start) {
  if (desktop()) return kind === "file" ? desktop().pick_file(start) : desktop().pick_folder(start);
  return (await api("POST", "/api/pick", { kind, start })).path;
}

async function browse(inputId) {
  const input = $(inputId);
  // Start in the folder already typed, or next to the videos folder for the output.
  const start = input.value || (input.dataset.startFrom ? $(input.dataset.startFrom).value : "");
  const folder = await pickPath("folder", start);
  if (!folder) return;
  input.value = folder;
  input.dispatchEvent(new Event("change"));  // as if typed: e.g. the dataset overview follows the new folder
  if (inputId === "setup-videos" && !$("setup-project").value) {
    $("setup-project").placeholder = `Defaults to "${folder.split(/[\\/]/).pop()}"`;
  }
}

// ---------------------------------------------------------------- labeler

async function load() {
  const data = await api("GET", "/api/state");
  if (!data.ready) return showSetup(data);
  // Back from the splitter to the same project: stay on the video that was open.
  const back = state && state.project === data.project && current < data.items.length ? current : null;
  state = data;
  showSection("labeler");

  document.title = `${state.project} · ${APP_TITLE}`;
  $("project-name").textContent = state.project;
  $("project-name").title = state.project;
  $("project-path").textContent = `${state.videos_dir}  →  ${state.output_file}`;
  $("project-path").title = $("project-path").textContent;
  $("auto-advance").checked = getPref("autoAdvance", true);
  setFilter(getPref("filter", "all"), false);
  $("sidebar").classList.toggle("collapsed", getPref("sidebarHidden", false));
  $("search").value = "";

  const empty = state.items.length === 0;
  $("empty").classList.toggle("hidden", !empty);
  $("work").classList.toggle("hidden", empty);
  updateCounts(state);
  renderList();
  if (!empty) {
    if (back !== null) return select(back);
    const done = state.first_unlabeled === null;
    select(done ? 0 : state.first_unlabeled);
    toast(done ? `"${state.project}": all videos are labeled` : `Opened "${state.project}"`);
  }
}

function updateCounts(c) {
  state.total = c.total;
  state.labeled = c.labeled;
  state.first_unlabeled = c.first_unlabeled;
  const pct = c.total ? Math.round((100 * c.labeled) / c.total) : 0;
  $("count-labeled").textContent = c.labeled;
  $("count-total").textContent = c.total;
  $("count-pct").textContent = `${pct}%`;
  $("progress-bar").style.width = c.total ? `${(100 * c.labeled) / c.total}%` : "0";
  const per = { onscreen: 0, offscreen: 0, unclear: 0 };
  for (const item of state.items) if (item.label) per[item.label]++;
  for (const k in per) $(`stat-${k}`).textContent = per[k];
}

function setFilter(filter, save = true) {
  document.querySelectorAll("#list-filter button, #label-filter button").forEach((b) =>
    b.classList.toggle("active", b.dataset.filter === filter));
  if (save) {
    setPref("filter", filter);
    renderList();
    // A label filter opens its first video, so you can start relabeling right away.
    if (isLabelFilter(filter) && state && state.items.length && !matchesFilter(state.items[current], filter)) {
      const first = state.items.find((item) => matchesFilter(item, filter));
      if (first) select(first.index);
    }
  }
}

function isLabelFilter(filter) {
  return filter in STATUS;
}

function currentFilter() {
  const active = document.querySelector("#list-filter button.active, #label-filter button.active");
  return active ? active.dataset.filter : "all";
}

function matchesFilter(item, filter) {
  if (filter === "all") return true;
  if (filter === "todo") return !item.label;
  if (filter === "done") return !!item.label;
  return item.label === filter;  // onscreen / offscreen / unclear
}

function renderList() {
  const q = $("search").value.trim().toLowerCase();
  const filter = currentFilter();
  const ul = $("video-list");
  ul.innerHTML = "";
  let shown = 0;
  for (const item of state.items) {
    if (q && !item.name.toLowerCase().includes(q)) continue;
    // Keep the open video in the list, even if the filter would hide it.
    if (item.index !== current && !matchesFilter(item, filter)) continue;
    const li = document.createElement("li");
    li.dataset.index = item.index;
    li.className = item.index === current ? "active" : "";
    li.title = item.file;
    const dot = document.createElement("span");
    dot.className = `dot ${item.label || ""}`;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = item.name;
    const num = document.createElement("span");
    num.className = "num";
    num.textContent = item.index + 1;
    li.append(dot, name, num);
    ul.appendChild(li);
    shown++;
  }
  $("list-empty").classList.toggle("hidden", shown > 0);
}

function select(index) {
  if (!state || !state.items.length) return;
  current = Math.max(0, Math.min(index, state.items.length - 1));
  const item = state.items[current];
  const player = $("player");
  $("video-error").classList.add("hidden");
  player.src = `/video/${encodeURIComponent(item.file)}`;
  player.play().catch(() => { /* autoplay may be blocked */ });
  $("current-name").textContent = item.name;
  $("current-name").title = item.file;
  $("current-pos").textContent = `${current + 1} of ${state.items.length}`;
  $("prev").disabled = current === 0;
  $("next").disabled = current === state.items.length - 1;
  renderStatus();
  renderList();
  const active = $("video-list").querySelector("li.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function renderStatus() {
  const label = state.items[current].label;
  const badge = $("current-status");
  badge.textContent = label ? STATUS[label] : "Not labeled";
  badge.className = `badge ${label || ""}`;
  $("clear-label").disabled = !label;
  document.querySelectorAll(".choice").forEach((b) => b.classList.toggle("selected", b.dataset.label === label));
}

function nextUnlabeled(from) {
  const n = state.items.length;
  for (let k = 1; k <= n; k++) {
    const i = (from + k) % n;
    if (!state.items[i].label) return i;
  }
  return null;
}

// With a label filter on (e.g. Unclear), move to the next video that still has
// that label; otherwise to the next unlabeled video.
function nextMatching(from) {
  const filter = currentFilter();
  if (!isLabelFilter(filter)) return nextUnlabeled(from);
  const n = state.items.length;
  for (let k = 1; k < n; k++) {
    const i = (from + k) % n;
    if (matchesFilter(state.items[i], filter)) return i;
  }
  return null;
}

async function setLabel(label) {
  if (!state || !state.items.length) return;
  const item = state.items[current];
  const btn = document.querySelector(`.choice[data-label="${label}"]`);
  btn.classList.remove("flash");
  void btn.offsetWidth;
  btn.classList.add("flash");
  try {
    const counts = await api("POST", "/api/label", { file: item.file, label });
    item.label = label;
    updateCounts(counts);
    renderStatus();
    renderList();
    if ($("auto-advance").checked) {
      const next = nextMatching(current);
      const filter = currentFilter();
      if (next !== null) select(next);
      else if (isLabelFilter(filter)) toast(`No more ${STATUS[filter]} videos`);
      else toast(`All videos in "${state.project}" are labeled 🎉`);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

async function clearLabel() {
  if (!state || !state.items.length) return;
  const item = state.items[current];
  if (!item.label) return;
  try {
    const counts = await api("DELETE", `/api/label/${encodeURIComponent(item.file)}`);
    item.label = null;
    updateCounts(counts);
    renderStatus();
    renderList();
  } catch (e) {
    toast(e.message, true);
  }
}

function replay() {
  const player = $("player");
  player.currentTime = 0;
  player.play().catch(() => {});
}

function jumpUnlabeled() {
  const next = nextUnlabeled(current);
  if (next === null) toast(`All videos in "${state.project}" are labeled`);
  else select(next);
}

async function changeProject() {
  await api("POST", "/api/close");
  await load();
}

// ---------------------------------------------------------------- events

$("home-btn").addEventListener("click", showHome);
document.querySelectorAll("[data-go-home]").forEach((b) => b.addEventListener("click", (e) => {
  e.preventDefault();
  showHome();
}));
document.querySelectorAll("#home [data-mode]").forEach((b) =>  // the step cards and the 1 › 2 › 3 links
  b.addEventListener("click", () => openMode(b.dataset.mode).catch((e) => toast(e.message, true))));
document.querySelectorAll("[data-workspace]").forEach((b) => b.addEventListener("click", () => {
  if (b.classList.contains("active")) return;
  openMode(b.dataset.workspace).catch((e) => toast(e.message, true));
}));

$("setup-go").addEventListener("click", submitSetup);
["setup-project", "setup-videos", "setup-output"].forEach((id) =>
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") submitSetup(); }));
document.querySelectorAll("[data-browse]").forEach((b) =>
  b.addEventListener("click", (e) => {
    e.preventDefault();  // the button sits inside a <label>
    browse(b.dataset.browse).catch((err) => toast(err.message, true));
  }));

$("menu-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
$("menu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => toggleMenu(false));
document.querySelectorAll("#theme-switch button").forEach((b) =>
  b.addEventListener("click", () => setTheme(b.dataset.themeChoice)));
$("menu-update").addEventListener("click", () => checkUpdate(true));
$("setup-update").addEventListener("click", () => checkUpdate(true));
$("home-update").addEventListener("click", () => checkUpdate(true));
$("menu-reveal").addEventListener("click", () => {
  toggleMenu(false);
  api("POST", "/api/reveal").catch((e) => toast(e.message, true));
});
$("menu-change").addEventListener("click", () => {
  toggleMenu(false);
  changeProject().catch((e) => toast(e.message, true));
});
$("empty-change").addEventListener("click", () => changeProject().catch((e) => toast(e.message, true)));

$("toggle-sidebar").addEventListener("click", () => {
  if (document.body.classList.contains("has-split")) {
    const hidden = $("split-sidebar").classList.toggle("collapsed");
    localStorage.setItem(SPLIT_SIDEBAR_KEY, JSON.stringify(hidden));
    return renderTimeline();
  }
  const hidden = $("sidebar").classList.toggle("collapsed");
  setPref("sidebarHidden", hidden);
});
$("search").addEventListener("input", renderList);
document.querySelectorAll("#list-filter button").forEach((b) =>
  b.addEventListener("click", () => setFilter(b.dataset.filter)));
// Clicking a label count shows only that label; clicking it again goes back to All.
document.querySelectorAll("#label-filter button").forEach((b) =>
  b.addEventListener("click", () => setFilter(currentFilter() === b.dataset.filter ? "all" : b.dataset.filter)));
$("auto-advance").addEventListener("change", (e) => setPref("autoAdvance", e.target.checked));
$("video-list").addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (li) select(Number(li.dataset.index));
});

$("prev").addEventListener("click", () => select(current - 1));
$("next").addEventListener("click", () => select(current + 1));
$("replay").addEventListener("click", replay);
$("clear-label").addEventListener("click", clearLabel);
$("jump-unlabeled").addEventListener("click", jumpUnlabeled);
document.querySelectorAll(".choice").forEach((b) => b.addEventListener("click", () => setLabel(b.dataset.label)));
$("player").addEventListener("error", () => {
  if ($("player").getAttribute("src")) $("video-error").classList.remove("hidden");
});

document.addEventListener("keydown", (e) => {
  if (!$("splitter").classList.contains("hidden")) return splitterKey(e);
  if (e.key === "Escape") return toggleMenu(false);
  if (!state || $("labeler").classList.contains("hidden")) return;
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea") return;
  const player = $("player");
  const actions = {
    ArrowLeft: () => select(current - 1),
    ArrowRight: () => select(current + 1),
    "1": () => setLabel("onscreen"),
    "2": () => setLabel("offscreen"),
    "3": () => setLabel("unclear"),
    " ": () => (player.paused ? player.play() : player.pause()),
    r: replay,
    n: jumpUnlabeled,
  };
  const action = actions[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (action) {
    e.preventDefault();
    action();
  }
});

// ---------------------------------------------------------------- updates

let latestVersion = null;

function showUpdateBanner(info) {
  latestVersion = info.latest;
  $("update-text").textContent = `Version ${info.latest} is available. You have ${info.current}.`;
  $("update-install").classList.toggle("hidden", !info.can_install);
  $("update-install").disabled = false;
  $("update-link").href = info.url;
  $("update-link").classList.remove("hidden");
  $("update-banner").classList.remove("hidden");
  $("menu-dot").classList.remove("hidden");
}

// manual: the user clicked "Check for updates", so always ask GitHub again and report the result.
async function checkUpdate(manual = false) {
  const buttons = [$("menu-update"), $("setup-update"), $("home-update")];
  if (manual) buttons.forEach((b) => { b.disabled = true; b.lastElementChild.textContent = "Checking…"; });
  let info;
  try {
    info = await api("GET", manual ? "/api/update?force=1" : "/api/update");
  } catch (e) {
    info = { error: e.message };
  } finally {
    buttons.forEach((b) => { b.disabled = false; b.lastElementChild.textContent = "Check for updates"; });
  }
  renderUpdateState(info);
  if (!manual) {
    if (info.available && sessionStorage.getItem("updateDismissed") !== info.latest) showUpdateBanner(info);
    return;
  }
  toggleMenu(false);
  if (info.error) return toast(info.error, true);
  if (!info.available) return toast(`You're up to date. Version ${info.current} is the latest.`);
  sessionStorage.removeItem("updateDismissed");
  showUpdateBanner(info);
  toast(`Version ${info.latest} is available`);
}

$("update-install").addEventListener("click", async () => {
  const btn = $("update-install");
  btn.disabled = true;
  $("update-text").textContent = `Downloading version ${latestVersion}…`;
  try {
    await api("POST", "/api/update/install");
    $("update-text").textContent = desktop()
      ? "Installing the update. The app will close and reopen by itself in a moment."
      : "Installing the update. The app closes and reopens in a new tab when it's done. You can close this tab.";
    $("update-link").classList.add("hidden");
    $("update-dismiss").classList.add("hidden");
  } catch (e) {
    btn.disabled = false;
    $("update-text").textContent = `Version ${latestVersion} is available.`;
    toast(e.message, true);
  }
});
$("update-dismiss").addEventListener("click", () => {
  sessionStorage.setItem("updateDismissed", latestVersion);
  $("update-banner").classList.add("hidden");
});

// ---------------------------------------------------------------- start

function onDesktopReady() {
  document.body.classList.add("desktop");
  applyTheme();  // match the window's title bar to the theme
}
if (desktop()) onDesktopReady();
else window.addEventListener("pywebviewready", onDesktopReady);

// Start on the Home screen, unless a project was opened from the command line (--videos).
async function start() {
  const data = await api("GET", "/api/state");
  if (data.ready) return load();
  showHome();
}

applyTheme();
start().catch((e) => toast(e.message, true));
checkUpdate();
