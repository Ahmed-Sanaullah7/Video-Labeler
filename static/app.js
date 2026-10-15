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
const BRAND = { labeler: "Speaker Labeler", splitter: "Video Splitter", downloader: "Video Downloader" };
const MODE_KEY = "labeler:mode";

// Show one screen. The modes are the downloader, the splitter (split-setup + splitter)
// and the labeler (setup + labeler).
function showSection(id) {
  if (id !== "splitter" && !$("splitter").classList.contains("hidden")) splitLeave();
  if (id !== "downloader" && !$("downloader").classList.contains("hidden")) downloadLeave();
  for (const s of SECTIONS) $(s).classList.toggle("hidden", s !== id);
  const mode = id === "home" ? null : id === "downloader" ? "downloader" : id.startsWith("split") ? "splitter" : "labeler";
  document.body.classList.toggle("has-project", id === "labeler");
  document.body.classList.toggle("has-split", id === "splitter");
  document.body.classList.toggle("has-download", id === "downloader");
  $("home-btn").classList.toggle("hidden", id === "home");
  $("brand-name").textContent = BRAND[mode] || BRAND.labeler;
  if (id !== "labeler") $("player").pause();
  if (id !== "splitter") $("split-player").pause();
  if (mode) localStorage.setItem(MODE_KEY, mode);
  if (id === "downloader") document.title = `Video Downloader · ${APP_TITLE}`;
  else if (id !== "labeler" && id !== "splitter") document.title = APP_TITLE;
}

function showHome() {
  showSection("home");
  const last = localStorage.getItem(MODE_KEY) || "splitter";
  document.querySelector(`.tool[data-mode="${last}"]`).focus();
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

async function browse(inputId) {
  const input = $(inputId);
  // Start in the folder already typed, or next to the videos folder for the output.
  const start = input.value || (input.dataset.startFrom ? $(input.dataset.startFrom).value : "");
  const folder = await desktop().pick_folder(start);
  if (!folder) return;
  input.value = folder;
  if (inputId === "setup-videos" && !$("setup-project").value) {
    $("setup-project").placeholder = `Defaults to "${folder.split(/[\\/]/).pop()}"`;
  }
}

// ---------------------------------------------------------------- labeler

async function load() {
  const data = await api("GET", "/api/state");
  if (!data.ready) return showSetup(data);
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
document.querySelectorAll(".tool[data-mode]").forEach((b) =>
  b.addEventListener("click", () => openMode(b.dataset.mode).catch((e) => toast(e.message, true))));

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
    r: () => { player.currentTime = 0; player.play(); },
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
