"use strict";
// Video Splitter: mark segments of a long video on a timeline and export them as clips (see splitter.py).
// Uses the helpers from app.js ($, api, toast, showSection, showSetup, toggleMenu).

const SPLIT_SIDEBAR_KEY = "labeler:splitter:sidebarHidden";
const MIN_SEG = 0.2;          // seconds, same as splitter.MIN_SEGMENT
const FRAME = 1 / 30;         // step for , and .
const MIN_VISIBLE = 10;       // seconds visible at the highest zoom
const TICK_STEPS = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const SAVE_TEXT = { saved: "Saved", pending: "Unsaved changes", saving: "Saving…", error: "Not saved" };
const MIN_CROP = 32;          // pixels, same as splitter.MIN_CROP
const ASPECT_KEY = "labeler:splitter:cropAspect";
const ASPECTS = { free: null, "1:1": 1, "9:16": 9 / 16, "16:9": 16 / 9 };  // width / height

let split = null;   // last /api/split/state response (ready)
let sIndex = 0;     // index into split.items
const tl = {
  file: null,
  duration: 0,
  segments: [],     // [{start, end}] sorted by start, never overlapping
  markers: [],      // [t] sorted
  crop: null,       // {x, y, w, h} in pixels of the video, applied to every clip; null = full frame
  exported: [],     // clips exported from this video: [{file, start, end, crop}]
  clips: [],        // clip files of this video in the output folder
  selected: null,   // {kind: "segment" | "marker", index}
  pendingIn: null,  // start time set with I, waiting for O
  zoom: 1,          // 1 = the whole video fits
  undo: [],
  playUntil: null,  // pause here (playing one segment)
  vw: 0, vh: 0,     // size of the video in pixels, once it's loaded
  cropMode: false,  // editing the crop box on the video
};
let drag = null;
let cropDrag = null;
let openToken = 0;
let saveTimer = null, savePromise = null, dirty = false;
let jobTimer = null, jobRunning = false;

const sPlayer = () => $("split-player");
const enc = encodeURIComponent;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const r3 = (t) => Math.round(t * 1000) / 1000;

// ---------------------------------------------------------------- time

function fmtTime(t, ms = true) {
  t = r3(Math.max(0, t || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const sec = ms ? s.toFixed(3).padStart(6, "0") : String(Math.floor(s)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

// "1:02:03.5", "2:03.5" or "123.5" -> seconds; null if it isn't a time.
function parseTime(str) {
  const parts = str.trim().split(":");
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d*)?$/.test(p))) return null;
  return parts.reduce((acc, p) => acc * 60 + parseFloat(p), 0);
}

function clipName(n) { return `${tl.file.replace(/\.[^.]+$/, "")}_${n}.mp4`; }

// ---------------------------------------------------------------- setup screen

function showSplitSetup(cfg = {}) {
  split = null;
  tl.file = null;
  sPlayer().removeAttribute("src");
  sPlayer().load();
  showSection("split-setup");
  $("split-error").classList.add("hidden");
  if (cfg.last_source !== undefined) $("split-source").value = cfg.last_source;
  if (cfg.last_output !== undefined) $("split-output").value = cfg.last_output;
  $("split-source").focus();
}

async function submitSplitSetup() {
  const btn = $("split-go");
  btn.disabled = true;
  btn.textContent = "Opening…";
  $("split-error").classList.add("hidden");
  try {
    await api("POST", "/api/split/setup", { source: $("split-source").value, output: $("split-output").value });
    await loadSplit();
  } catch (e) {
    $("split-error").textContent = e.message;
    $("split-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Open folder";
  }
}

async function changeSplitFolders() {
  await flushSave().catch(() => {});
  await api("POST", "/api/split/close");
  await loadSplit();
}

// ---------------------------------------------------------------- splitter screen

function lastVideoKey() { return `labeler:splitter:${split.source_dir}:last`; }

async function loadSplit() {
  const data = await api("GET", "/api/split/state");
  if (!data.ready) return showSplitSetup(data);
  split = data;
  showSection("splitter");
  document.title = `Video Splitter · ${APP_TITLE}`;
  $("project-name").textContent = data.source_dir.split(/[\\/]/).pop();
  $("project-name").title = data.source_dir;
  $("project-path").textContent = `${data.source_dir}  →  ${data.output_dir}`;
  $("project-path").title = $("project-path").textContent;
  $("split-sidebar").classList.toggle("collapsed", JSON.parse(localStorage.getItem(SPLIT_SIDEBAR_KEY) || "false"));
  $("split-search").value = "";

  const empty = data.items.length === 0;
  $("split-empty").classList.toggle("hidden", !empty);
  $("split-work").classList.toggle("hidden", empty);
  renderSplitList();
  if (!empty) {
    const last = data.items.findIndex((i) => i.file === localStorage.getItem(lastVideoKey()));
    await openVideo(last >= 0 ? last : 0);
  }
  if (data.exporting) watchJob();
}

// Called by showSection when the splitter is hidden: keep the work.
function splitLeave() {
  flushSave().catch(() => {});
}

function renderSplitList() {
  const q = $("split-search").value.trim().toLowerCase();
  const ul = $("split-list");
  ul.innerHTML = "";
  let shown = 0;
  for (const item of split.items) {
    if (q && !item.name.toLowerCase().includes(q)) continue;
    const li = document.createElement("li");
    li.dataset.index = item.index;
    li.className = item.index === sIndex ? "active" : "";
    li.title = `${item.file}\n${item.segments} segments · ${item.exported} clips exported`;
    const dot = document.createElement("span");
    dot.className = `dot ${item.exported ? "split" : item.segments ? "pending" : ""}`;
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
  $("split-list-empty").classList.toggle("hidden", shown > 0);
  $("split-stat-done").textContent = split.items.filter((i) => i.exported).length;
  $("split-stat-pending").textContent = split.items.filter((i) => i.segments && !i.exported).length;
}

async function openVideo(index) {
  await flushSave();  // throws if the current segments couldn't be saved: stay on this video
  const token = ++openToken;
  const item = split.items[clamp(index, 0, split.items.length - 1)];
  const entry = await api("GET", `/api/split/segments/${enc(item.file)}`);
  if (token !== openToken) return;  // another video was clicked meanwhile
  sIndex = item.index;
  Object.assign(tl, {
    file: item.file, duration: 0, segments: entry.segments, markers: entry.markers, crop: entry.crop || null,
    exported: entry.exported, clips: entry.clips,
    selected: null, pendingIn: null, zoom: 1, undo: [], playUntil: null, vw: 0, vh: 0, cropMode: false,
  });
  cropDrag = null;
  localStorage.setItem(lastVideoKey(), item.file);
  $("split-video-error").classList.add("hidden");
  $("export-done").classList.add("hidden");
  $("export-error").classList.add("hidden");
  sPlayer().src = `/api/split/video/${enc(item.file)}`;
  $("split-name").textContent = item.name;
  $("split-name").title = item.file;
  $("tl-scroll").scrollLeft = 0;
  setSaveBadge("saved");
  renderSplitList();
  renderAll();
  const active = $("split-list").querySelector("li.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

// ---------------------------------------------------------------- saving

function setSaveBadge(s) {
  const badge = $("split-save");
  badge.textContent = SAVE_TEXT[s];
  badge.className = `badge ${s === "saved" ? "" : s === "error" ? "error" : "saving"}`;
}

function scheduleSave() {
  dirty = true;
  setSaveBadge("pending");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => flushSave().catch(() => {}), 600);
}

async function flushSave() {
  clearTimeout(saveTimer);
  if (savePromise) await savePromise.catch(() => {});
  if (!dirty || !tl.file) return;
  dirty = false;
  const file = tl.file;
  setSaveBadge("saving");
  savePromise = api("PUT", `/api/split/segments/${enc(file)}`, { segments: tl.segments, markers: tl.markers, crop: tl.crop });
  try {
    await savePromise;
    const item = split && split.items.find((i) => i.file === file);
    if (item) item.segments = tl.segments.length;
    if (tl.file === file) setSaveBadge(dirty ? "pending" : "saved");
    renderSplitList();
  } catch (e) {
    dirty = true;
    setSaveBadge("error");
    toast(e.message, true);
    throw e;
  } finally {
    savePromise = null;
  }
}

// ---------------------------------------------------------------- editing

function isSel(kind, i) { return tl.selected !== null && tl.selected.kind === kind && tl.selected.index === i; }

function selectItem(kind, index = null) {
  tl.selected = kind && index !== null ? { kind, index } : null;
  renderAll();
}

function pushUndo() {
  tl.undo.push(JSON.stringify({ segments: tl.segments, markers: tl.markers, crop: tl.crop }));
  if (tl.undo.length > 200) tl.undo.shift();
}

// After any change: keep things sorted (and the selection on the same item), save, redraw.
function changed() {
  const sel = tl.selected;
  const selItem = sel && (sel.kind === "segment" ? tl.segments[sel.index] : tl.markers[sel.index]);
  for (const s of tl.segments) { s.start = r3(s.start); s.end = r3(s.end); }
  tl.markers = tl.markers.map(r3);
  tl.segments.sort((a, b) => a.start - b.start);
  tl.markers.sort((a, b) => a - b);
  if (sel) {
    const index = sel.kind === "segment" ? tl.segments.indexOf(selItem) : tl.markers.indexOf(r3(selItem));
    tl.selected = index >= 0 ? { kind: sel.kind, index } : null;
  }
  scheduleSave();
  renderAll();
}

function undo() {
  const snap = tl.undo.pop();
  if (!snap) return toast("Nothing to undo");
  Object.assign(tl, JSON.parse(snap));
  tl.selected = null;
  changed();
}

function now() { return r3(sPlayer().currentTime || 0); }
function segmentAt(t) { return tl.segments.findIndex((s) => t > s.start && t < s.end); }
function overlaps(start, end) { return tl.segments.some((s) => start < s.end && end > s.start); }

function setIn() {
  if (!tl.duration) return;
  const t = now();
  const inside = segmentAt(t);
  if (inside >= 0) return toast(`The playhead is inside segment ${inside + 1}. Move it to a free part first.`, true);
  tl.pendingIn = t;
  renderAll();
}

function setOut() {
  if (!tl.duration) return;
  if (tl.pendingIn === null) return toast("Press I (Start) where the part begins first.", true);
  const t = now();
  const start = Math.min(tl.pendingIn, t), end = Math.max(tl.pendingIn, t);
  if (end - start < MIN_SEG) return toast("The segment is too short. Move the playhead further first.", true);
  if (overlaps(start, end)) return toast("Segments can't overlap. Pick an end before the next segment.", true);
  pushUndo();
  const seg = { start, end };
  tl.segments.push(seg);
  tl.pendingIn = null;
  tl.selected = { kind: "segment", index: tl.segments.length - 1 };
  changed();
}

function addMarker() {
  if (!tl.duration) return;
  const t = now();
  if (tl.markers.some((m) => Math.abs(m - t) < 0.1)) return toast("There's already a marker here.", true);
  pushUndo();
  tl.markers.push(t);
  tl.selected = { kind: "marker", index: tl.markers.length - 1 };
  changed();
}

// Cut the video into back-to-back pieces at the markers: 0 → m1, m1 → m2, …, last → end.
function segmentsFromMarkers() {
  if (!tl.markers.length) return toast("Add markers with M first.", true);
  const points = [0, ...tl.markers, tl.duration];
  const pieces = [];
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i], end = points[i + 1];
    if (end - start >= MIN_SEG && !overlaps(start, end)) pieces.push({ start, end });
  }
  if (!pieces.length) return toast("There's no free room between the markers for new segments.", true);
  pushUndo();
  tl.segments.push(...pieces);
  tl.markers = [];  // they are the segment edges now
  tl.selected = null;
  changed();
  toast(`Added ${pieces.length} segment${pieces.length === 1 ? "" : "s"}`);
}

function deleteSelected() {
  if (!tl.selected) return;
  pushUndo();
  const list = tl.selected.kind === "segment" ? tl.segments : tl.markers;
  list.splice(tl.selected.index, 1);
  tl.selected = null;
  changed();
}

function clearAll() {
  if (!tl.segments.length && !tl.markers.length) return;
  if (!confirm("Remove all segments and markers of this video? You can undo this with Ctrl+Z.")) return;
  pushUndo();
  tl.segments = [];
  tl.markers = [];
  tl.selected = null;
  tl.pendingIn = null;
  changed();
}

// Free room around segment i (up to its neighbours).
function roomAround(i) {
  const prev = tl.segments[i - 1], next = tl.segments[i + 1];
  return [prev ? prev.end : 0, next ? next.start : tl.duration];
}

function setEdge(i, edge, t) {
  const s = tl.segments[i];
  const [lo, hi] = roomAround(i);
  if (edge === "start" && (t < lo - 1e-6 || t > s.end - MIN_SEG)) return false;
  if (edge === "end" && (t > hi + 1e-6 || t < s.start + MIN_SEG)) return false;
  pushUndo();
  s[edge] = t;
  changed();
  return true;
}

// ---------------------------------------------------------------- playback

function seek(t) {
  if (!tl.duration) return;
  sPlayer().currentTime = clamp(t, 0, tl.duration);
  updatePlayhead();
}

function seekBy(dt) { seek((sPlayer().currentTime || 0) + dt); }

function playSegment(i) {
  const s = tl.segments[i];
  if (!s) return;
  seek(s.start);
  tl.playUntil = s.end;
  sPlayer().play().catch(() => {});
}

function tick() {
  const p = sPlayer();
  if (tl.playUntil !== null && p.currentTime >= tl.playUntil) p.pause();
  updatePlayhead();
  if (!p.paused) {
    followPlayhead();
    requestAnimationFrame(tick);
  }
}

// While playing zoomed in, scroll the timeline so the playhead stays in view.
function followPlayhead() {
  if (tl.zoom === 1 || drag) return;
  const sc = $("tl-scroll");
  const x = (sPlayer().currentTime / tl.duration) * contentWidth();
  if (x < sc.scrollLeft || x > sc.scrollLeft + sc.clientWidth - 20) sc.scrollLeft = x - sc.clientWidth * 0.1;
}

// ---------------------------------------------------------------- timeline

function contentWidth() { return $("tl-scroll").clientWidth * tl.zoom; }
function pct(t) { return tl.duration ? (100 * t) / tl.duration : 0; }
function maxZoom() { return Math.max(1, tl.duration / MIN_VISIBLE); }

function renderAll() {
  renderTimeline();
  renderSegments();
  renderToolbar();
  renderCrop();
}

function renderTimeline() {
  if (!split || !tl.file || !$("tl-scroll").clientWidth) return;
  $("tl-content").style.width = tl.zoom === 1 ? "" : `${contentWidth()}px`;
  renderRuler();
  const track = $("tl-track");
  track.innerHTML = "";
  if (!tl.duration) return updatePlayhead();
  tl.segments.forEach((s, i) => {
    const el = document.createElement("div");
    el.className = `tl-seg${isSel("segment", i) ? " selected" : ""}`;
    el.dataset.index = i;
    el.style.left = `${pct(s.start)}%`;
    el.style.width = `${pct(s.end - s.start)}%`;
    el.title = `Segment ${i + 1}: ${fmtTime(s.start)} – ${fmtTime(s.end)}\nDrag to move, drag the edges to resize, double-click to play`;
    el.innerHTML = `<span class="tl-handle l" data-edge="start"></span><b>${i + 1}</b><span class="tl-handle r" data-edge="end"></span>`;
    track.appendChild(el);
  });
  if (tl.pendingIn !== null) {
    const el = document.createElement("div");
    el.id = "tl-pending-range";
    el.className = "tl-pending-range";
    track.appendChild(el);
  }
  tl.markers.forEach((t, i) => {
    const el = document.createElement("div");
    el.className = `tl-marker${isSel("marker", i) ? " selected" : ""}`;
    el.dataset.index = i;
    el.style.left = `${pct(t)}%`;
    el.title = `Marker ${fmtTime(t)} (drag to move)`;
    track.appendChild(el);
  });
  updatePlayhead();
}

// Only the ticks in view are drawn, so zooming far into a long video stays fast.
function renderRuler() {
  const ruler = $("tl-ruler");
  ruler.innerHTML = "";
  const sc = $("tl-scroll"), width = contentWidth();
  if (!tl.duration || !width) return;
  const pps = width / tl.duration;
  const step = TICK_STEPS.find((s) => s * pps >= 90) || TICK_STEPS[TICK_STEPS.length - 1];
  const from = Math.max(0, Math.floor((sc.scrollLeft - 100) / pps / step) * step);
  const to = Math.min(tl.duration, (sc.scrollLeft + sc.clientWidth + 100) / pps);
  const frag = document.createDocumentFragment();
  for (let t = from; t <= to; t += step) {
    const major = document.createElement("div");
    major.className = "tl-tick";
    major.style.left = `${pct(t)}%`;
    const label = document.createElement("span");
    label.textContent = fmtTime(t, step < 1);
    major.appendChild(label);
    frag.appendChild(major);
    if (t + step / 2 < tl.duration) {
      const minor = document.createElement("div");
      minor.className = "tl-tick minor";
      minor.style.left = `${pct(t + step / 2)}%`;
      frag.appendChild(minor);
    }
  }
  ruler.appendChild(frag);
}

function updatePlayhead() {
  const t = sPlayer().currentTime || 0;
  $("tl-playhead").style.left = `${pct(t)}%`;
  $("split-time").textContent = `${fmtTime(t)} / ${fmtTime(tl.duration, false)}`;
  const range = $("tl-pending-range");
  if (range && tl.pendingIn !== null) {
    range.style.left = `${pct(Math.min(t, tl.pendingIn))}%`;
    range.style.width = `${pct(Math.abs(t - tl.pendingIn))}%`;
  }
}

function setZoom(z, anchorX) {
  if (!tl.duration) return;
  const sc = $("tl-scroll");
  z = clamp(z, 1, maxZoom());
  if (anchorX === undefined) {
    // Keep the playhead where it is on screen if it's in view, else zoom around the middle.
    const x = (sPlayer().currentTime / tl.duration) * contentWidth() - sc.scrollLeft;
    anchorX = x >= 0 && x <= sc.clientWidth ? x : sc.clientWidth / 2;
  }
  const anchorT = ((sc.scrollLeft + anchorX) / contentWidth()) * tl.duration;
  tl.zoom = z;
  renderTimeline();
  sc.scrollLeft = (anchorT / tl.duration) * contentWidth() - anchorX;
  renderRuler();
  renderToolbar();
}

function renderToolbar() {
  const hasVideo = tl.duration > 0;
  ["tl-in", "tl-out", "tl-marker"].forEach((id) => { $(id).disabled = !hasVideo; });
  $("tl-from-markers").disabled = !tl.markers.length;
  $("tl-delete").disabled = !tl.selected;
  $("tl-undo").disabled = !tl.undo.length;
  $("tl-zoom-out").disabled = tl.zoom <= 1;
  $("tl-zoom-in").disabled = !hasVideo || tl.zoom >= maxZoom();
  $("tl-fit").disabled = tl.zoom <= 1;
  $("tl-zoom-label").textContent = tl.zoom <= 1 ? "Fit" : `${tl.zoom < 10 ? tl.zoom.toFixed(1) : Math.round(tl.zoom)}×`;
  const pending = $("tl-pending");
  pending.classList.toggle("hidden", tl.pendingIn === null);
  if (tl.pendingIn !== null) pending.textContent = `Start ${fmtTime(tl.pendingIn)} · press O at the end`;
}

function timeAt(clientX) {
  const rect = $("tl-content").getBoundingClientRect();
  return clamp(((clientX - rect.left) / rect.width) * tl.duration, 0, tl.duration);
}

function onPointerDown(e) {
  if (!tl.duration || e.button !== 0) return;
  const t = timeAt(e.clientX);
  const markEl = e.target.closest(".tl-marker"), segEl = e.target.closest(".tl-seg");
  if (markEl) {
    const i = Number(markEl.dataset.index);
    drag = { kind: "marker", index: i, x0: e.clientX, t0: t, orig: tl.markers[i], moved: false };
    selectItem("marker", i);
  } else if (segEl) {
    const i = Number(segEl.dataset.index);
    drag = { kind: "segment", index: i, edge: e.target.dataset.edge || "move", x0: e.clientX, t0: t, orig: { ...tl.segments[i] }, moved: false };
    selectItem("segment", i);
  } else {
    drag = { kind: "seek" };
    seek(t);
  }
  $("tl-content").setPointerCapture(e.pointerId);
  e.preventDefault();
}

function onPointerMove(e) {
  if (!drag) return;
  const t = timeAt(e.clientX);
  if (drag.kind === "seek") return seek(t);
  if (!drag.moved) {
    if (Math.abs(e.clientX - drag.x0) < 3) return;  // a click, not a drag (yet)
    pushUndo();
    drag.moved = true;
  }
  if (drag.kind === "marker") {
    tl.markers[drag.index] = t;
  } else {
    const s = tl.segments[drag.index], o = drag.orig;
    const [lo, hi] = roomAround(drag.index);
    if (drag.edge === "move") {
      const len = o.end - o.start;
      s.start = clamp(o.start + t - drag.t0, lo, hi - len);
      s.end = s.start + len;
    } else if (drag.edge === "start") {
      s.start = clamp(t, lo, s.end - MIN_SEG);
      seek(s.start);  // show the frame at the new edge
    } else {
      s.end = clamp(t, s.start + MIN_SEG, hi);
      seek(s.end);
    }
  }
  renderTimeline();
}

function onPointerUp() {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (d.kind === "seek") return;
  if (d.moved) changed();
  else seek(d.t0);  // a click selects the item and moves the playhead there
}

// ---------------------------------------------------------------- crop

function aspectName() {
  const name = localStorage.getItem(ASPECT_KEY);
  return name in ASPECTS ? name : "free";
}

function aspectRatio() { return ASPECTS[aspectName()]; }
function cropBox() { return tl.crop || { x: 0, y: 0, w: tl.vw, h: tl.vh }; }
function isFullFrame(b) { return b.x === 0 && b.y === 0 && b.w >= tl.vw - 1 && b.h >= tl.vh - 1; }
function sameCrop(a, b) { return a && b ? ["x", "y", "w", "h"].every((k) => a[k] === b[k]) : !a && !b; }

// Round the edges to even pixels (H.264 needs an even width and height) and keep the box in the frame.
function evenBox(b) {
  const even = (v) => 2 * Math.round(v / 2);
  const x = even(b.x), y = even(b.y);
  const w = clamp(even(b.x + b.w) - x, MIN_CROP, tl.vw - (tl.vw % 2));
  const h = clamp(even(b.y + b.h) - y, MIN_CROP, tl.vh - (tl.vh % 2));
  return { x: clamp(x, 0, tl.vw - w), y: clamp(y, 0, tl.vh - h), w, h };
}

// The box from a fixed corner (ax, ay) to the pointer, in the chosen shape and inside the frame.
function boxFromCorner(ax, ay, px, py) {
  let w = Math.abs(px - ax), h = Math.abs(py - ay);
  const a = aspectRatio();
  if (a) {
    if (w < h * a) w = h * a; else h = w / a;
    const fit = Math.min(1, (px < ax ? ax : tl.vw - ax) / w, (py < ay ? ay : tl.vh - ay) / h);
    w *= fit;
    h *= fit;
  }
  if (!(w >= MIN_CROP && h >= MIN_CROP)) return null;  // too small, or no room on that side
  return evenBox({ x: px < ax ? ax - w : ax, y: py < ay ? ay - h : ay, w, h });
}

function nextCropBox(d, p) {
  const o = d.orig;
  if (d.kind === "move") {
    return evenBox({ ...o, x: clamp(o.x + p.x - d.p0.x, 0, tl.vw - o.w), y: clamp(o.y + p.y - d.p0.y, 0, tl.vh - o.h) });
  }
  if (d.kind === "draw") return boxFromCorner(d.p0.x, d.p0.y, p.x, p.y);
  const edge = d.handle;
  if (edge.length === 2) {  // a corner: the opposite corner stays put
    return boxFromCorner(edge.includes("w") ? o.x + o.w : o.x, edge.includes("n") ? o.y + o.h : o.y, p.x, p.y);
  }
  const b = { ...o };  // an edge (Free shape only): the other edges stay put
  if (edge === "w") { b.x = Math.min(p.x, o.x + o.w - MIN_CROP); b.w = o.x + o.w - b.x; }
  if (edge === "e") b.w = Math.max(p.x - o.x, MIN_CROP);
  if (edge === "n") { b.y = Math.min(p.y, o.y + o.h - MIN_CROP); b.h = o.y + o.h - b.y; }
  if (edge === "s") b.h = Math.max(p.y - o.y, MIN_CROP);
  return evenBox(b);
}

// Where the picture is drawn in the player; the rest of the <video> is black bars.
function pictureRect() {
  const p = sPlayer(), scale = Math.min(p.clientWidth / tl.vw, p.clientHeight / tl.vh);
  const w = tl.vw * scale, h = tl.vh * scale;
  return { left: p.offsetLeft + (p.clientWidth - w) / 2, top: p.offsetTop + (p.clientHeight - h) / 2, width: w, height: h };
}

// Pointer position in pixels of the video, kept inside the frame.
function videoPoint(e) {
  const r = $("crop-layer").getBoundingClientRect();
  return {
    x: clamp(((e.clientX - r.left) / r.width) * tl.vw, 0, tl.vw),
    y: clamp(((e.clientY - r.top) / r.height) * tl.vh, 0, tl.vh),
  };
}

function renderCrop() {
  const ready = !!tl.file && tl.vw > 0;
  if (!ready) tl.cropMode = false;
  const editing = tl.cropMode, b = cropBox(), layer = $("crop-layer");
  if (sPlayer().controls === editing) sPlayer().controls = !editing;  // while cropping, the box takes the mouse
  layer.classList.toggle("hidden", !ready || !(editing || tl.crop));
  layer.classList.toggle("editing", editing);
  layer.classList.toggle("locked", !!aspectRatio());
  layer.classList.toggle("full", !tl.crop);
  if (ready) {
    const r = pictureRect();
    Object.assign(layer.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
    Object.assign($("crop-box").style, {
      left: `${(100 * b.x) / tl.vw}%`, top: `${(100 * b.y) / tl.vh}%`,
      width: `${(100 * b.w) / tl.vw}%`, height: `${(100 * b.h) / tl.vh}%`,
    });
  }
  const btn = $("crop-btn");
  btn.disabled = !ready;
  btn.classList.toggle("primary", editing);
  btn.title = editing ? "Finish cropping (C or Esc)" : "Crop the video: every clip of this video is cut to the box (C)";
  $("crop-btn-text").textContent = editing ? "Done" : "Crop";
  $("crop-aspect").classList.toggle("hidden", !editing);
  $("crop-aspect").querySelectorAll("button").forEach((el) => el.classList.toggle("active", el.dataset.aspect === aspectName()));
  $("crop-reset").classList.toggle("hidden", !editing);
  $("crop-reset").disabled = !tl.crop;
  const info = $("crop-info");
  info.classList.toggle("hidden", !ready || !(editing || tl.crop));
  info.textContent = !tl.crop ? `Full frame ${tl.vw} × ${tl.vh}`
    : editing ? `${b.w} × ${b.h} at ${b.x}, ${b.y}` : `Cropped to ${b.w} × ${b.h}`;
}

function toggleCrop(on = !tl.cropMode) {
  if (on && !tl.vw) return;
  tl.cropMode = on;
  cropDrag = null;
  renderCrop();
}

// Pick a shape. The box keeps its centre and about its area, and shrinks if it doesn't fit the frame.
function setAspect(name) {
  localStorage.setItem(ASPECT_KEY, name);
  const a = ASPECTS[name], b = cropBox();
  if (!a || !tl.vw || Math.abs(b.w / b.h - a) < 0.01) return renderCrop();
  let h = Math.sqrt((b.w * b.h) / a), w = h * a;
  const fit = Math.min(1, tl.vw / w, tl.vh / h);
  w *= fit;
  h *= fit;
  pushUndo();
  const box = evenBox({ x: clamp(b.x + (b.w - w) / 2, 0, tl.vw - w), y: clamp(b.y + (b.h - h) / 2, 0, tl.vh - h), w, h });
  tl.crop = isFullFrame(box) ? null : box;
  changed();
}

function resetCrop() {
  if (!tl.crop) return;
  pushUndo();
  tl.crop = null;
  changed();
}

function onCropDown(e) {
  if (!tl.cropMode || e.button !== 0) return;
  const handle = e.target.dataset.h;
  // On the box: move it. Outside it, or when there's no crop yet: draw a new one.
  const kind = handle ? "resize" : e.target === $("crop-box") && tl.crop ? "move" : "draw";
  cropDrag = { kind, handle, p0: videoPoint(e), orig: cropBox(), x0: e.clientX, y0: e.clientY, moved: false };
  $("crop-layer").setPointerCapture(e.pointerId);
  e.preventDefault();
}

function onCropMove(e) {
  if (!cropDrag) return;
  if (!cropDrag.moved) {
    if (Math.hypot(e.clientX - cropDrag.x0, e.clientY - cropDrag.y0) < 3) return;  // a click, not a drag (yet)
    pushUndo();
    cropDrag.moved = true;
  }
  const box = nextCropBox(cropDrag, videoPoint(e));
  if (box) {
    tl.crop = box;
    renderCrop();
  }
}

function onCropUp() {
  if (!cropDrag) return;
  const d = cropDrag;
  cropDrag = null;
  if (!d.moved) return;
  if (tl.crop && isFullFrame(tl.crop)) tl.crop = null;
  changed();
}

// ---------------------------------------------------------------- segment list

function renderSegments() {
  const body = $("seg-body");
  body.innerHTML = "";
  if (!tl.file) return;
  const sameTimes = (x, s, name) => x.file === name && Math.abs(x.start - s.start) < 0.002 && Math.abs(x.end - s.end) < 0.002;
  const done = (s, name) => tl.exported.some((x) => sameTimes(x, s, name) && sameCrop(x.crop, tl.crop));
  const oldCrop = (s, name) => tl.exported.some((x) => sameTimes(x, s, name));
  tl.segments.forEach((s, i) => {
    const tr = document.createElement("tr");
    tr.dataset.index = i;
    tr.className = isSel("segment", i) ? "selected" : "";
    tr.innerHTML = `
      <td><span class="seg-num">${i + 1}</span></td>
      <td><input class="time" type="text" data-edge="start" spellcheck="false" title="Start (h:mm:ss.mmm)"></td>
      <td><input class="time" type="text" data-edge="end" spellcheck="false" title="End (h:mm:ss.mmm)"></td>
      <td>${fmtTime(s.end - s.start)}</td>
      <td class="seg-clip"></td>
      <td class="actions">
        <button class="icon-btn sm" data-act="play" title="Play this segment"><svg class="ic"><use href="#i-play"/></svg></button>
        <button class="icon-btn sm" data-act="delete" title="Delete this segment"><svg class="ic"><use href="#i-trash"/></svg></button>
      </td>`;
    tr.querySelector('[data-edge="start"]').value = fmtTime(s.start);
    tr.querySelector('[data-edge="end"]').value = fmtTime(s.end);
    const name = clipName(i + 1);
    const clip = tr.querySelector(".seg-clip");
    clip.textContent = name;
    if (done(s, name)) {
      clip.classList.add("exists");
      clip.title = "Exported";
    } else if (oldCrop(s, name)) {
      clip.title = "Exported with a different crop. Export again to update it.";
    }
    body.appendChild(tr);
  });
  const n = tl.segments.length;
  const total = tl.segments.reduce((sum, s) => sum + s.end - s.start, 0);
  $("seg-count").textContent = n;
  $("seg-total").textContent = n ? `${fmtTime(total, false)} of ${fmtTime(tl.duration, false)} in clips` : "";
  $("seg-empty").classList.toggle("hidden", n > 0);
  document.querySelector(".seg-table").classList.toggle("hidden", n === 0);
  $("seg-clear").disabled = !n && !tl.markers.length;
  $("export-btn").disabled = !n || jobRunning;
}

function onTimeInput(input) {
  const i = Number(input.closest("tr").dataset.index);
  const t = parseTime(input.value);
  if (t === null || !setEdge(i, input.dataset.edge, r3(Math.min(t, tl.duration || t)))) {
    toast("That time doesn't fit: segments can't overlap and must be at least 0.2 s long.", true);
    renderSegments();
  }
}

// ---------------------------------------------------------------- export

async function exportClips(overwrite = false) {
  if (!tl.segments.length || jobRunning) return;
  try {
    await flushSave();
  } catch (_) {
    return;
  }
  $("export-done").classList.add("hidden");
  $("export-error").classList.add("hidden");
  try {
    await api("POST", "/api/split/export", { file: tl.file, overwrite });
  } catch (e) {
    const existing = e.data && e.data.existing;
    if (!existing) return toast(e.message, true);
    const n = tl.segments.length;
    const list = existing.slice(0, 4).join(", ") + (existing.length > 4 ? ", …" : "");
    const one = existing.length === 1;
    const ok = confirm(`${existing.length} clip${one ? "" : "s"} from this video already exist${one ? "s" : ""} in the output folder (${list}).\n\n`
      + `Replace them with the ${n} clip${n === 1 ? "" : "s"} from the current segments? Old clips that aren't in the new list are deleted.`);
    if (ok) return exportClips(true);
    return;
  }
  watchJob();
}

function watchJob() {
  clearTimeout(jobTimer);
  pollJob();
}

async function pollJob() {
  let job;
  try {
    job = await api("GET", "/api/split/job");
  } catch (_) {
    jobTimer = setTimeout(pollJob, 1500);
    return;
  }
  jobRunning = job.running;
  $("export-progress").classList.toggle("hidden", !job.running);
  $("export-btn").disabled = job.running || !tl.segments.length;
  if (job.running) {
    $("export-text").textContent = `Exporting ${job.current} (${job.index + 1} of ${job.total}) · ${Math.floor(job.percent)}%`;
    $("export-bar").style.width = `${job.percent}%`;
    jobTimer = setTimeout(pollJob, 400);
  } else if (job.file) {
    finishJob(job).catch((e) => toast(e.message, true));
  }
}

async function finishJob(job) {
  $("export-bar").style.width = "0";
  const data = await api("GET", "/api/split/state");
  if (!data.ready || !split) return;
  split.items = data.items;
  renderSplitList();
  if (tl.file === job.file) {
    const entry = await api("GET", `/api/split/segments/${enc(job.file)}`);
    tl.exported = entry.exported;
    tl.clips = entry.clips;
    renderSegments();
  }
  const saved = job.done.length;
  const name = job.file.replace(/\.[^.]+$/, "");
  if (job.error) {
    $("export-error").textContent = job.error + (saved ? ` ${saved} clip${saved === 1 ? " was" : "s were"} saved before this.` : "");
    $("export-error").classList.remove("hidden");
  } else if (job.cancelled) {
    toast(`Export cancelled. ${saved} of ${job.total} clips were saved.`);
  } else {
    $("export-done-text").textContent = `${saved} clip${saved === 1 ? "" : "s"} of "${name}" saved to ${job.output_dir}`;
    $("export-done").classList.remove("hidden");
    toast("Export finished");
  }
}

function samePath(a, b) {
  const norm = (p) => p.replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase();
  return norm(a) === norm(b);
}

// Open the output folder in the labeler setup, so the new clips are listed.
async function openInLabeler() {
  await flushSave().catch(() => {});
  const folder = split.output_dir;
  await api("POST", "/api/close");
  const cfg = await api("GET", "/api/state");
  const same = cfg.last_videos && samePath(cfg.last_videos, folder);
  showSetup({ last_project: same ? cfg.last_project : "", last_videos: folder, last_output: same ? cfg.last_output : "" });
  toast("Check the project name, then click Open project");
}

// ---------------------------------------------------------------- keys

function splitterKey(e) {
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea";
  if (e.key === "Escape") {
    toggleMenu(false);
    if (typing) return;
    if (tl.cropMode) toggleCrop(false);
    else if (tl.pendingIn !== null) { tl.pendingIn = null; renderAll(); } else if (tl.selected) selectItem(null);
    return;
  }
  if (typing || !tl.file) return;
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "z") {
    e.preventDefault();
    return undo();
  }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const p = sPlayer();
  const actions = {
    " ": () => (p.paused ? p.play().catch(() => {}) : p.pause()),
    ArrowLeft: () => seekBy(e.shiftKey ? -1 : -5),
    ArrowRight: () => seekBy(e.shiftKey ? 1 : 5),
    ",": () => { p.pause(); seekBy(-FRAME); },
    ".": () => { p.pause(); seekBy(FRAME); },
    i: setIn,
    o: setOut,
    m: addMarker,
    c: () => toggleCrop(),
    Delete: deleteSelected,
    Backspace: deleteSelected,
    "+": () => setZoom(tl.zoom * 1.5),
    "=": () => setZoom(tl.zoom * 1.5),
    "-": () => setZoom(tl.zoom / 1.5),
    "0": () => setZoom(1),
  };
  const action = actions[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (action) {
    e.preventDefault();
    action();
  }
}

// ---------------------------------------------------------------- events

$("split-go").addEventListener("click", submitSplitSetup);
["split-source", "split-output"].forEach((id) =>
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") submitSplitSetup(); }));
$("menu-split-change").addEventListener("click", () => {
  toggleMenu(false);
  changeSplitFolders().catch((e) => toast(e.message, true));
});
$("menu-split-reveal").addEventListener("click", () => {
  toggleMenu(false);
  api("POST", "/api/split/reveal").catch((e) => toast(e.message, true));
});
$("split-empty-change").addEventListener("click", () => changeSplitFolders().catch((e) => toast(e.message, true)));
$("split-search").addEventListener("input", renderSplitList);
$("split-list").addEventListener("click", (e) => {
  const li = e.target.closest("li");
  if (li) openVideo(Number(li.dataset.index)).catch((err) => toast(err.message, true));
});

const sp = sPlayer();
sp.addEventListener("durationchange", () => {
  if (!Number.isFinite(sp.duration) || !tl.file) return;
  tl.duration = sp.duration;
  renderAll();
});
sp.addEventListener("loadedmetadata", () => {
  if (!tl.file) return;
  tl.vw = sp.videoWidth;
  tl.vh = sp.videoHeight;
  renderCrop();
});
sp.addEventListener("play", () => requestAnimationFrame(tick));
sp.addEventListener("pause", () => { tl.playUntil = null; updatePlayhead(); });
sp.addEventListener("seeked", updatePlayhead);
sp.addEventListener("timeupdate", updatePlayhead);
sp.addEventListener("error", () => {
  if (sp.getAttribute("src")) $("split-video-error").classList.remove("hidden");
});

$("tl-in").addEventListener("click", setIn);
$("tl-out").addEventListener("click", setOut);
$("tl-marker").addEventListener("click", addMarker);
$("tl-from-markers").addEventListener("click", segmentsFromMarkers);
$("tl-delete").addEventListener("click", deleteSelected);
$("tl-undo").addEventListener("click", undo);
$("tl-zoom-in").addEventListener("click", () => setZoom(tl.zoom * 1.5));
$("tl-zoom-out").addEventListener("click", () => setZoom(tl.zoom / 1.5));
$("tl-fit").addEventListener("click", () => setZoom(1));

const content = $("tl-content");
content.addEventListener("pointerdown", onPointerDown);
content.addEventListener("pointermove", onPointerMove);
content.addEventListener("pointerup", onPointerUp);
content.addEventListener("pointercancel", onPointerUp);
content.addEventListener("dblclick", (e) => {
  const segEl = e.target.closest(".tl-seg");
  if (segEl) playSegment(Number(segEl.dataset.index));
});
$("tl-scroll").addEventListener("wheel", (e) => {
  if (!tl.duration) return;
  const sc = $("tl-scroll");
  if (e.ctrlKey) {
    e.preventDefault();  // also stops the page from zooming
    setZoom(tl.zoom * (e.deltaY < 0 ? 1.25 : 0.8), e.clientX - sc.getBoundingClientRect().left);
  } else if (tl.zoom > 1 && Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
    e.preventDefault();
    sc.scrollLeft += e.deltaY;
  }
}, { passive: false });
$("tl-scroll").addEventListener("scroll", renderRuler);
new ResizeObserver(() => renderTimeline()).observe($("tl-scroll"));

const cropLayer = $("crop-layer");
cropLayer.addEventListener("pointerdown", onCropDown);
cropLayer.addEventListener("pointermove", onCropMove);
cropLayer.addEventListener("pointerup", onCropUp);
cropLayer.addEventListener("pointercancel", onCropUp);
new ResizeObserver(() => renderCrop()).observe(sp);
$("crop-btn").addEventListener("click", () => toggleCrop());
$("crop-reset").addEventListener("click", resetCrop);
$("crop-aspect").addEventListener("click", (e) => {
  const b = e.target.closest("[data-aspect]");
  if (b) setAspect(b.dataset.aspect);
});

$("seg-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const i = Number(tr.dataset.index);
  const act = e.target.closest("[data-act]");
  if (act && act.dataset.act === "play") return playSegment(i);
  if (act && act.dataset.act === "delete") {
    tl.selected = { kind: "segment", index: i };
    return deleteSelected();
  }
  if (e.target.closest("input")) return;
  selectItem("segment", i);
  seek(tl.segments[i].start);
});
$("seg-body").addEventListener("change", (e) => { if (e.target.matches("input.time")) onTimeInput(e.target); });
$("seg-body").addEventListener("keydown", (e) => {
  if (!e.target.matches("input.time")) return;
  if (e.key === "Enter") e.target.blur();
  if (e.key === "Escape") renderSegments();
});
$("seg-clear").addEventListener("click", clearAll);
$("export-btn").addEventListener("click", () => exportClips().catch((e) => toast(e.message, true)));
$("export-cancel").addEventListener("click", () => api("POST", "/api/split/job/cancel").catch((e) => toast(e.message, true)));
$("open-labeler").addEventListener("click", () => openInLabeler().catch((e) => toast(e.message, true)));
