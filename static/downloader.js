"use strict";

// Video Downloader: download the videos linked in a spreadsheet, a whole channel,
// or the results of keyword searches (see downloader.py).

const DL_STATUS = {
  queued: "Queued", downloading: "Downloading", downloaded: "Downloaded",
  skipped: "Already there", not_allowed: "Not allowed", failed: "Failed", cancelled: "Stopped",
};
const DL_FILTERS = {
  all: null,
  active: ["queued", "downloading", "cancelled"],  // "To do": not finished yet, stopped ones included
  done: ["downloaded", "skipped"],
  failed: ["failed"],
  blocked: ["not_allowed"],
};
const DL_FINISHED = ["downloaded", "skipped", "not_allowed", "failed", "cancelled"];
const DL_EMPTY = {
  sheet: "Pick a spreadsheet to see the videos it links to.",
  links: "Paste video links, one per line.",
  channel: "Paste a channel link and click Find videos.",
  search: "Type your searches, one per line, and click Find videos.",
};

const dl = {
  loaded: false,      // the settings were filled in from the last session
  rows: new Map(),    // row number -> {row, url, name, status, done, total, speed, size, detail, path, title}
  trs: new Map(),     // row number -> <tr>
  rev: -1,            // last row revision received from /job (-1: none yet)
  logEnd: 0,          // activity log lines received so far
  running: false,
  stopping: false,
  jobShown: false,    // the last download (from before the page was opened) was put on screen
  watching: false,
  timer: null,
  scanToken: 0,
  listing: false,     // a list is being fetched (Start waits for it)
  partial: false,     // this run is a Resume / Retry of part of the list on screen
  runRows: null,      // row numbers in this run (null: the whole list)
  lastSearch: null,   // {count, notes, filtered}: what Find videos found for the searches
  folder: "",
  elapsed: 0,
};

// ---------------------------------------------------------------- helpers

function humanSize(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function fmtDuration(sec) {
  const t = Math.round(sec), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  return h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const baseName = (p) => p.split(/[\\/]/).pop();

function dlAlert(msg, warn = false) {
  const el = $("dl-alert");
  el.textContent = msg;
  el.classList.toggle("error", !warn);
  el.classList.toggle("warn", warn);
  el.classList.toggle("hidden", !msg);
}

function fillSelect(select, options, value) {
  select.innerHTML = "";
  for (const [v, text] of options) select.add(new Option(text, v));
  select.value = options.some(([v]) => v === value) ? value : options.length ? options[0][0] : "";
}

function workerHint(n) {
  if (n === 1) return ["One at a time: slowest, but the gentlest on a flaky connection.", false];
  if (n <= 4) return [`${n} at a time: the sweet spot for YouTube.`, false];
  if (n <= 6) return [`${n} at a time: quicker, though YouTube starts throttling here.`, true];
  return [`${n} at a time: fine for direct file links, but YouTube will throttle and retry more.`, true];
}

function renderWorkers() {
  const n = Number($("dl-workers").value);
  const [text, warn] = workerHint(n);
  $("dl-workers-value").textContent = n;
  $("dl-workers-hint").textContent = text;
  $("dl-workers-hint").classList.toggle("warn", warn);
}

function currentSource() {
  const active = document.querySelector("#dl-source button.active");
  return active ? active.dataset.source : "sheet";
}

function searchLines() {
  return $("dl-queries").value.split("\n").map((q) => q.trim()).filter(Boolean);
}

// Where the videos come from, as the server expects it (see clean_source in downloader.py).
function sourceChoice() {
  return {
    source: currentSource(),
    file: $("dl-file").value, sheet: $("dl-sheet").value, url_column: $("dl-url").value, name_column: $("dl-name").value,
    links: $("dl-links").value,
    channel: $("dl-channel").value,
    tabs: [...document.querySelectorAll("#dl-tabs input:checked")].map((b) => b.value),
    queries: $("dl-queries").value, per_query: Number($("dl-per-query").value) || 50,
  };
}

function dlOptions() {
  return {
    quality: $("dl-quality").value, workers: Number($("dl-workers").value),
    retries: Number($("dl-retries").value), timeout: Number($("dl-timeout").value),
    h264: $("dl-h264").checked, playlists: $("dl-playlists").checked,
    cookies: $("dl-cookies").value, limit: Number($("dl-limit").value) || 0,
    reuse_only: $("dl-reuse").checked,
    media: currentMedia(), audio_format: $("dl-audio-format").value,
  };
}

function currentMedia() {
  return document.querySelector("#dl-media button.active")?.dataset.media || "video";
}

// Video, or only the sound: quality and H.264 only mean something for a video.
function setMedia(media) {
  document.querySelectorAll("#dl-media button").forEach((b) => b.classList.toggle("active", b.dataset.media === media));
  document.querySelectorAll("#downloader .dl-video-only").forEach((el) => el.classList.toggle("hidden", media === "audio"));
  document.querySelectorAll("#downloader .dl-audio-only").forEach((el) => el.classList.toggle("hidden", media !== "audio"));
}

const blankRow = (r) => ({ ...r, status: "queued", done: 0, total: 0, speed: 0, size: 0, duration: 0, detail: "",
                           path: "", title: r.title || "", license: "" });

// Rows of the list with one of these statuses, by number.
function rowsWith(...statuses) {
  return [...dl.rows.values()].filter((r) => statuses.includes(r.status)).map((r) => r.row);
}

// Show one source's fields. A different source means a different list, so the table is emptied.
function setSource(source, refresh = true) {
  document.querySelectorAll("#dl-source button").forEach((b) => b.classList.toggle("active", b.dataset.source === source));
  document.querySelectorAll("#downloader .dl-src").forEach((el) => el.classList.toggle("hidden", el.dataset.src !== source));
  const sheet = source === "sheet";
  // A sheet and pasted links are read straight away; a channel or search goes online, so waits for Find videos.
  $("dl-find").classList.toggle("hidden", sheet || source === "links");
  $("dl-output-opt").classList.toggle("hidden", !sheet);
  $("dl-output").placeholder = sheet ? "Defaults to a downloads folder next to the spreadsheet" : "Pick a folder for the videos";
  if (!refresh) return;
  cancelListing();  // drop the answer to a listing still on its way
  dlAlert("");
  $("dl-done").classList.add("hidden");
  setRows([]);
  if (sheet && $("dl-file").value.trim()) return scanSheet(true);
  if (source === "links") return listLinks();
  $("dl-detected").textContent = sheet ? "Pick an .xlsx or .csv file with a column of video links." : DL_EMPTY[source];
  renderDownloadControls();
}

// ---------------------------------------------------------------- screen

async function loadDownload() {
  const data = await api("GET", "/api/download/state");
  showSection("downloader");
  $("dl-ffmpeg").classList.toggle("hidden", data.ffmpeg);
  if (!dl.loaded) {
    dl.loaded = true;
    const o = data.options, src = data.source;
    $("dl-file").value = data.file;
    $("dl-links").value = (src.links || []).join("\n");
    $("dl-channel").value = src.channel || "";
    document.querySelectorAll("#dl-tabs input").forEach((b) => { b.checked = (src.tabs || []).includes(b.value); });
    $("dl-queries").value = (src.queries || []).join("\n");
    $("dl-per-query").value = src.per_query || 50;
    $("dl-reuse").checked = !!o.reuse_only;
    setSource(src.source || "sheet", false);
    $("dl-detected").textContent = currentSource() === "sheet"
      ? "Pick an .xlsx or .csv file with a column of video links." : DL_EMPTY[currentSource()];
    $("dl-output").value = data.output;
    $("dl-quality").value = o.quality;
    $("dl-workers").value = o.workers;
    $("dl-retries").value = o.retries;
    $("dl-timeout").value = o.timeout;
    $("dl-h264").checked = o.h264;
    $("dl-audio-format").value = o.audio_format || "m4a";
    setMedia(o.media || "video");
    $("dl-playlists").checked = o.playlists;
    $("dl-cookies").value = o.cookies;
    $("dl-limit").value = o.limit;
    renderWorkers();
  }
  // A sheet or pasted links are read straight away; a channel or search waits for Find videos (it goes online).
  if (currentSource() === "sheet" && $("dl-file").value && !dl.rows.size) await scanSheet(true);
  if (currentSource() === "links" && !dl.rows.size) await listLinks();
  // Show a running download, or the last one the first time the page opens the downloader.
  if (data.running || (data.has_job && !dl.jobShown)) {
    dl.jobShown = true;
    resetJobView();
    watchDownload();
  }
  renderDownloadControls();
}

// Called by showSection when the downloader is hidden: the download goes on, the page stops polling.
function downloadLeave() {
  dl.watching = false;
  clearTimeout(dl.timer);
}

// ---------------------------------------------------------------- the sheet

// Ask the server for the list. Only the newest request counts, and Start (or a change of
// source) drops one still on its way: returns null for those. Start waits while one runs.
async function fetchList(body) {
  const token = ++dl.scanToken;
  dl.listing = true;
  renderDownloadControls();
  try {
    const s = await api("POST", "/api/download/scan", body);
    return token === dl.scanToken ? s : null;
  } catch (e) {
    if (token === dl.scanToken) throw e;
    return null;
  } finally {
    if (token === dl.scanToken) dl.listing = false;
  }
}

function cancelListing() {
  dl.scanToken++;
  dl.listing = false;
}

// Read the sheet again with the chosen sheet and columns; the newest scan wins.
async function scanSheet(newFile = false) {
  if (dl.running) return;
  if (newFile) {
    fillSelect($("dl-sheet"), [["", "—"]], "");
    fillSelect($("dl-url"), [["", "Auto-detect"]], "");
    fillSelect($("dl-name"), [["", "Auto-detect"]], "");
  }
  dlAlert("");
  $("dl-done").classList.add("hidden");
  if (!$("dl-file").value.trim()) {
    cancelListing();
    setRows([]);
    $("dl-detected").textContent = "Pick an .xlsx or .csv file with a column of video links.";
    return renderDownloadControls();
  }
  $("dl-detected").textContent = "Reading the sheet…";
  try {
    const s = await fetchList({ ...sourceChoice(), source: "sheet", options: dlOptions() });
    if (!s) return;
    const columns = s.columns.map((c) => [c, c]);
    fillSelect($("dl-sheet"), s.sheets.length ? s.sheets.map((n) => [n, n]) : [["", "Single sheet"]], s.sheet);
    fillSelect($("dl-url"), [["", "Auto-detect"], ...columns], $("dl-url").value);
    fillSelect($("dl-name"), [["", "Auto-detect"], ["-", "Video title"], ...columns], $("dl-name").value);
    $("dl-sheet").disabled = !s.sheets.length;
    $("dl-url").disabled = $("dl-name").disabled = false;
    $("dl-output").placeholder = `Defaults to ${s.default_output}`;
    setRows(s.rows.map(blankRow));
    const names = s.name_column ? `“${s.name_column}”` : "the video's own title";
    $("dl-detected").textContent = `${plural(s.rows.length, "link")} in “${s.url_column}”, named after ${names}`
      + (s.headerless ? ". The sheet has no header row." : ".");
  } catch (e) {
    setRows([]);
    $("dl-detected").textContent = "Couldn't read this sheet.";
    dlAlert(e.message);
  }
  renderDownloadControls();
}

// Pasted links, read as they are typed. Nothing goes online until Start download.
async function listLinks() {
  if (dl.running) return;
  dlAlert("");
  $("dl-done").classList.add("hidden");
  if (!$("dl-links").value.trim()) {
    cancelListing();
    setRows([]);
    $("dl-detected").textContent = DL_EMPTY.links;
    return renderDownloadControls();
  }
  try {
    const s = await fetchList({ ...sourceChoice(), options: dlOptions() });
    if (!s) return;
    setRows(s.rows.map(blankRow));
    $("dl-detected").textContent = `${plural(s.rows.length, "link")}${s.notes.length ? `; ${s.notes.join(", ")}` : ""}.`;
  } catch (e) {
    setRows([]);
    $("dl-detected").textContent = e.message;
  }
  renderDownloadControls();
}

// What a search found, and what the reuse box will do with it.
function searchSummary() {
  const s = dl.lastSearch;
  const text = `${plural(s.count, "video")}: ${s.notes.join(", ")}.`;
  if (s.filtered) return `${text} YouTube's Creative Commons filter was used.`;
  if ($("dl-reuse").checked) {
    return `${text} Each video's license is checked when you start. To let YouTube's Creative Commons `
      + "filter narrow the list first, click Find videos again.";
  }
  return text;
}

// List a channel's videos, or the results of the searches. Goes online, so only on Find videos.
async function findVideos() {
  if (dl.running) return;
  const source = currentSource();
  if (source === "sheet") return scanSheet();
  if (source === "links") return listLinks();
  dlAlert("");
  $("dl-done").classList.add("hidden");
  $("dl-detected").textContent = source === "channel"
    ? "Listing the channel… a large channel can take a minute." : "Searching YouTube…";
  const filtered = $("dl-reuse").checked;  // the server applies YouTube's Creative Commons filter when ticked
  try {
    const s = await fetchList({ ...sourceChoice(), options: dlOptions() });
    if (!s) return;
    setRows(s.rows.map(blankRow));
    if (source === "channel") {
      $("dl-detected").textContent = `${plural(s.rows.length, "video")} on “${s.label}”`
        + `${s.notes.length ? ` (${s.notes.join(", ")})` : ""}.`;
    } else {
      dl.lastSearch = { count: s.rows.length, notes: s.notes, filtered };
      $("dl-detected").textContent = searchSummary();
    }
  } catch (e) {
    setRows([]);
    $("dl-detected").textContent = source === "channel" ? "Couldn't list this channel." : "The search didn't work.";
    dlAlert(e.message);
  }
  renderDownloadControls();
}

async function browseSheet() {
  const file = await pickPath("file", $("dl-file").value);
  if (!file) return;
  $("dl-file").value = file;
  scanSheet(true);
}

// ---------------------------------------------------------------- the table

function setRows(rows) {
  dl.rows = new Map(rows.map((r) => [r.row, r]));
  dl.trs = new Map();
  const body = $("dl-body");
  body.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.dataset.row = r.row;
    tr.innerHTML = '<td><span class="seg-num"></span></td>'
      + '<td><div class="dl-video"><b></b><a target="_blank" rel="noopener"></a><small></small></div></td>'
      + '<td><span class="badge"></span><button class="icon-btn row-retry hidden" title="Try this video again">'
      + '<svg class="ic"><use href="#i-refresh"/></svg></button></td>'
      + '<td><div class="dl-rowprog"></div></td><td class="muted"></td>';
    dl.trs.set(r.row, tr);
    paintRow(r);
    body.appendChild(tr);
  }
  applyFilter();
  renderStats();
}

function updateRow(r) {
  if (!dl.rows.has(r.row)) return;
  dl.rows.set(r.row, r);
  paintRow(r);
}

function paintRow(r) {
  const tr = dl.trs.get(r.row);
  const [num, video, status, prog, size] = tr.children;
  num.firstChild.textContent = r.row;
  const name = r.name || r.title || (r.path ? baseName(r.path) : "");
  const [title, link, detail] = video.firstChild.children;
  title.textContent = name || r.url;
  title.title = r.path || name || r.url;
  link.textContent = r.url;
  link.href = r.url;
  link.classList.toggle("hidden", !name);
  detail.textContent = r.status === "failed" || r.status === "not_allowed" ? r.detail : "";
  detail.classList.toggle("note", r.status === "not_allowed");
  detail.title = detail.textContent;
  detail.classList.toggle("hidden", !detail.textContent);
  status.firstChild.textContent = DL_STATUS[r.status] || r.status;
  status.firstChild.className = `badge ${r.status}`;
  status.lastChild.classList.toggle("hidden", r.status !== "failed");
  prog.firstChild.replaceChildren(...rowProgress(r));
  size.textContent = r.size ? humanSize(r.size) : r.total ? humanSize(r.total) : "";
  tr.classList.toggle("has-file", !!r.path);
  tr.title = r.path ? "Double-click to open the video" : "";
}

function rowProgress(r) {
  if (r.status !== "downloading") return [];
  const text = document.createElement("span");
  const parts = [r.total ? `${Math.floor((100 * r.done) / r.total)}%` : humanSize(r.done)];
  if (r.speed) parts.push(`${humanSize(r.speed)}/s`);
  text.textContent = parts.join(" · ");
  if (!r.total) return [text];
  const bar = document.createElement("div");
  bar.className = "progress";
  bar.innerHTML = '<div class="progress-bar"></div>';
  bar.firstChild.style.width = `${(100 * r.done) / r.total}%`;
  return [bar, text];
}

function currentDlFilter() {
  return document.querySelector("#dl-filter button.active").dataset.filter;
}

function applyFilter() {
  const statuses = DL_FILTERS[currentDlFilter()];
  const q = $("dl-search").value.trim().toLowerCase();
  let shown = 0;
  for (const [n, r] of dl.rows) {
    const match = (!statuses || statuses.includes(r.status))
      && (!q || `${r.name} ${r.title} ${r.url}`.toLowerCase().includes(q));
    dl.trs.get(n).classList.toggle("hidden", !match);
    if (match) shown++;
  }
  $("dl-count").textContent = dl.rows.size;
  $("dl-empty").textContent = dl.rows.size ? "No matching videos" : DL_EMPTY[currentSource()];
  $("dl-empty").classList.toggle("hidden", shown > 0);
}

// ---------------------------------------------------------------- totals

function counts() {
  const c = { queued: 0, downloading: 0, downloaded: 0, skipped: 0, not_allowed: 0, failed: 0, cancelled: 0 };
  for (const r of dl.rows.values()) c[r.status]++;
  return c;
}

function renderStats() {
  const c = counts(), n = dl.rows.size;
  let data = 0, partial = 0;
  for (const r of dl.rows.values()) {
    if (r.status === "downloaded") data += r.size;
    if (r.status === "downloading") {
      data += r.done;
      if (r.total) partial += r.done / r.total;
    }
  }
  $("dl-n-total").textContent = n;
  $("dl-n-downloaded").textContent = c.downloaded;
  $("dl-n-skipped").textContent = c.skipped;
  $("dl-n-failed").textContent = c.failed;  // stopped videos are not failures: Resume goes on with them
  $("dl-n-blocked").textContent = c.not_allowed;
  $("dl-tile-blocked").classList.toggle("hidden", !$("dl-reuse").checked && !c.not_allowed);
  $("dl-n-data").textContent = humanSize(data);
  const pct = (k) => (n ? `${(100 * k) / n}%` : "0");
  $("dl-bar-done").style.width = pct(c.downloaded + c.skipped);
  $("dl-bar-blocked").style.width = pct(c.not_allowed);
  $("dl-bar-failed").style.width = pct(c.failed);
  $("dl-bar-active").style.width = pct(partial);
}

function renderRunProgress() {
  // This run's rows only: a Resume or Retry goes on with part of the list.
  const run = [...dl.rows.values()].filter((r) => !dl.runRows || dl.runRows.has(r.row));
  const c = counts(), n = run.length;
  const done = run.filter((r) => DL_FINISHED.includes(r.status)).length;
  let speed = 0;
  for (const r of run) if (r.status === "downloading") speed += r.speed;
  $("dl-progress-title").textContent = dl.stopping ? "Stopping, finishing the current chunks…"
    : c.downloading ? `Downloading ${plural(c.downloading, "video")}`
    : n ? ($("dl-reuse").checked ? "Checking licenses…" : "Working…") : "Getting the list…";
  const parts = [`${done} of ${n} done`];
  if (speed) parts.push(`${humanSize(speed)}/s`);
  parts.push(`${fmtDuration(dl.elapsed)} elapsed`);
  if (done && done < n) parts.push(`about ${fmtDuration((dl.elapsed / done) * (n - done))} left`);
  $("dl-progress-detail").textContent = parts.join(" · ");
}

function renderDownloadControls() {
  document.querySelectorAll("#downloader .dl-rail-body :is(input, select, button, textarea)").forEach((el) => {
    if (el.id === "dl-sheet" || el.id === "dl-url" || el.id === "dl-name") {
      if (dl.running) el.disabled = true;
      else if (el.id === "dl-sheet") el.disabled = el.options.length < 2 && !el.value;
      else el.disabled = !dl.rows.size && el.options.length < 2;
    } else {
      el.disabled = dl.running;
    }
  });
  $("dl-start").disabled = dl.running || dl.listing || !dl.rows.size;
  $("dl-find").disabled = dl.running || dl.listing;
  // Resume / Retry failed: after a run that left videos not done, or failed.
  const c = counts(), left = c.queued + c.cancelled;
  const ran = [...dl.rows.values()].some((r) => r.status !== "queued");
  $("dl-again").classList.toggle("hidden", dl.running || dl.listing || !ran || (!left && !c.failed));
  $("dl-resume").classList.toggle("hidden", !left);
  $("dl-resume-n").textContent = left;
  $("dl-retry").classList.toggle("hidden", !c.failed);
  $("dl-retry-n").textContent = c.failed;
  $("downloader").classList.toggle("running", dl.running);
  $("dl-stop").classList.toggle("hidden", !dl.running);
  $("dl-stop").disabled = dl.stopping;
  $("dl-stop").lastChild.textContent = dl.stopping ? "Stopping…" : "Stop";
  if (!dl.running && !dl.watching && $("dl-done").classList.contains("hidden")) {
    $("dl-progress-title").textContent = "Ready";
    $("dl-progress-detail").textContent = dl.rows.size ? `${dl.rows.size} queued` : "";
  }
}

// ---------------------------------------------------------------- running

// Start the list, or with `only` (row numbers) go on with part of it: Resume, Retry failed, one row's ↻.
async function startDownload(only = null) {
  if (dl.running || dl.listing || !dl.rows.size || (only && !only.length)) return;
  dlAlert("");
  $("dl-done").classList.add("hidden");
  // A channel's videos or search results: download exactly the list on screen, don't list again.
  // Resume / Retry do the same for any source, so rows keep their numbers and sheet names.
  const listed = only || ["channel", "search"].includes(currentSource())
    ? [...dl.rows.values()].map((r) => ({ row: r.row, url: r.url, name: r.name || "", title: r.title || "" }))
    : undefined;
  cancelListing();
  await api("POST", "/api/download/start",
    { ...sourceChoice(), rows: listed, only: only || undefined, output: $("dl-output").value, options: dlOptions() });
  cancelListing();  // nothing may replace the list from here on
  dl.jobShown = true;
  resetJobView();
  dl.running = true;
  dl.partial = !!only;
  dl.runRows = new Set(only || dl.rows.keys());
  for (const r of dl.rows.values()) {
    if (dl.runRows.has(r.row)) Object.assign(r, { status: "queued", done: 0, total: 0, speed: 0, size: 0, duration: 0, detail: "", path: "" });
  }
  dl.rows.forEach(paintRow);
  renderStats();
  renderDownloadControls();
  watchDownload();
}

async function stopDownload() {
  dl.stopping = true;
  renderDownloadControls();
  renderRunProgress();
  await api("POST", "/api/download/job/stop");
}

// The next poll fetches the whole job: every row and the whole activity log.
function resetJobView() {
  dl.rev = -1;
  dl.logEnd = 0;
  $("dl-log").textContent = "";
}

function watchDownload() {
  clearTimeout(dl.timer);
  dl.watching = true;
  pollDownload();
}

async function pollDownload() {
  let job;
  try {
    job = await api("GET", `/api/download/job?rev=${dl.rev}&log=${dl.logEnd}`);
  } catch (_) {
    if (dl.watching) dl.timer = setTimeout(pollDownload, 1500);
    return;
  }
  if (!dl.watching) return;
  // Until the sheet is read the job has no rows yet: keep showing the scan.
  // A Resume / Retry runs part of the list: its rows are updated in place, the others stay as they are.
  if (job.total) {
    if (dl.rev === -1 && !dl.partial) setRows(job.rows);
    else job.rows.forEach(updateRow);
    dl.rev = job.rev;
  }
  appendLog(job.log);
  dl.logEnd = job.log_end;
  dl.folder = job.folder || dl.folder;
  dl.elapsed = job.elapsed || 0;
  dl.stopping = !!job.stopping;
  const wasRunning = dl.running;
  dl.running = !!job.running;
  if (job.ffmpeg === false && job.total) $("dl-ffmpeg").classList.remove("hidden");
  renderStats();
  applyFilter();
  if (job.running) {
    renderRunProgress();
    renderDownloadControls();
    dl.timer = setTimeout(pollDownload, 500);
    return;
  }
  dl.watching = false;
  renderDownloadControls();
  if (job.folder) finishDownload(job, wasRunning);
}

function appendLog(lines) {
  if (!lines || !lines.length) return;
  const log = $("dl-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
  log.append((log.textContent ? "\n" : "") + lines.join("\n"));
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function finishDownload(job, live) {
  if (job.error) {
    $("dl-progress-title").textContent = "Stopped";
    $("dl-progress-detail").textContent = "";
    dlAlert(job.error);
    return;
  }
  // The whole list on screen (a Resume or Retry ran part of it); the time and data are this run's.
  const s = job.summary, c = counts(), left = c.queued + c.cancelled;
  const headline = s.counts.cancelled ? "Stopped early" : "Finished";
  const blocked = c.not_allowed ? `, ${c.not_allowed} not allowed` : "";
  $("dl-progress-title").textContent = `${headline}: ${c.downloaded} downloaded, ${c.skipped} already there${blocked}, `
    + `${c.failed} failed${left ? `, ${left} left` : ""}`;
  $("dl-progress-detail").textContent = `${humanSize(s.bytes)} in ${fmtDuration(s.elapsed)}`;
  let onDisk = 0, bytes = 0, seconds = 0;
  for (const r of dl.rows.values()) {
    if (r.status !== "downloaded" && r.status !== "skipped") continue;
    onDisk++;
    bytes += r.size || 0;
    seconds += r.duration || 0;
  }
  let text = `${plural(onDisk, job.audio ? "audio file" : "video")} in ${job.folder}`;
  if (bytes) text += ` · ${humanSize(bytes)}`;
  if (seconds) text += ` · ${fmtDuration(seconds)} of ${job.audio ? "audio" : "video"}`;
  $("dl-done-text").textContent = text;
  $("dl-open-report").classList.toggle("hidden", !s.report);
  $("dl-open-splitter").classList.toggle("hidden", !onDisk || !!job.audio);  // the splitter cuts videos
  $("dl-done").classList.remove("hidden");
  if (c.failed) {
    dlAlert(`${plural(c.failed, "video")} couldn't be downloaded. Retry failed tries them again; `
      + "the Failed filter shows which, and report.txt has the reasons.", true);
  }
  if (live) toast(s.counts.cancelled ? "Download stopped" : "Download finished");
  if (panelView($("dl-views")) === "dataset") loadDataset().catch(() => {});
}

// ---------------------------------------------------------------- dataset overview

// Everything in the output folder, across every run: videos, hours, size, licenses, channels.
async function loadDataset() {
  const el = $("dl-dataset");
  datasetMessage(el, "Counting the videos in the folder… (videos from before are measured once)");
  let o;
  try {
    o = await api("POST", "/api/download/overview",
      { source: currentSource(), output: $("dl-output").value, file: $("dl-file").value });
  } catch (e) {
    $("dl-ds-folder").textContent = "";
    return datasetMessage(el, e.message);
  }
  $("dl-ds-folder").textContent = o.folder;
  $("dl-ds-folder").title = o.folder;
  if (!o.files) return datasetMessage(el, "No videos or audio in this folder yet.");
  const n = (count, seconds) => `${count} · ${fmtLength(seconds)}`;
  // The first tile counts what the folder holds: videos, audio files, or both.
  const count = !o.audio ? [String(o.videos), "Videos"]
    : !o.videos ? [String(o.audio), "Audio files"]
    : [String(o.files), "Files", `${plural(o.videos, "video")} and ${plural(o.audio, "audio file")}`];
  const notes = [];
  if (o.licenses.some((g) => g.name === "Not recorded")) {
    notes.push("Not recorded: downloaded before the app kept licenses, or put in the folder by hand.");
  }
  if (o.unmeasured) notes.push(`${plural(o.unmeasured, "file")} couldn't be measured, so the hours leave them out.`);
  renderDataset(el, [
    count,
    [fmtLength(o.seconds), "Total length", fmtLong(o.seconds), "accent"],
    [humanSize(o.bytes), "Size"],
    [fmtLength(o.average), "Avg length", `Average: ${fmtLong(o.average)}`],
    [String(o.reuse_videos), "Reuse-allowed", `${fmtLong(o.reuse_seconds)} licensed CC BY, CC BY-SA or CC0`, "ok"],
    [String(o.channel_count), "Channels"],
  ], [
    { title: "Type", rows: o.videos && o.audio
      ? o.kinds.map((g) => ({ label: g.name, value: g.seconds || g.count, text: n(g.count, g.seconds), title: fmtLong(g.seconds) }))
      : [] },
    { title: "Licenses", rows: o.licenses.map((g) => ({
      label: g.name, value: g.seconds || g.count, text: n(g.count, g.seconds), title: fmtLong(g.seconds),
      cls: g.reuse ? "ok" : g.name === "Not recorded" ? "dim" : "warn",
    })) },
    { title: "Length", rows: o.lengths.map((g) => ({ label: g.label, value: g.count, text: n(g.count, g.seconds) })) },
    { title: o.channel_count > o.channels.length ? `Top ${o.channels.length} channels` : "Channels",
      rows: o.channels.map((c) => ({ label: c.name, value: c.seconds, text: n(c.count, c.seconds), title: c.name })) },
  ], notes);
}

function showDlView(view) {
  showPanelView($("dl-views"), view);
  if (view === "dataset") loadDataset().catch((e) => datasetMessage($("dl-dataset"), e.message));
}

function reveal(what, row) {
  return api("POST", "/api/download/reveal", { what, row });
}

// Open the download folder in the splitter setup, to cut the long videos into clips.
async function openInSplitter() {
  const folder = dl.folder;
  await api("POST", "/api/split/close");
  const cfg = await api("GET", "/api/split/state");
  showSplitSetup({ last_source: folder, last_output: cfg.last_output || "" });
  toast("Check the output folder for the clips, then click Open folder");
}

// ---------------------------------------------------------------- events

$("dl-browse").addEventListener("click", (e) => {
  e.preventDefault();  // the button sits inside a <label>
  browseSheet().catch((err) => toast(err.message, true));
});
$("dl-file").addEventListener("change", () => scanSheet(true));
$("dl-file").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
$("dl-sheet").addEventListener("change", () => {
  fillSelect($("dl-url"), [["", "Auto-detect"]], "");
  fillSelect($("dl-name"), [["", "Auto-detect"]], "");
  scanSheet();
});
["dl-url", "dl-name"].forEach((id) => $(id).addEventListener("change", () => scanSheet()));
$("dl-limit").addEventListener("change", () => {
  if (currentSource() === "sheet") scanSheet();
  if (currentSource() === "links") listLinks();
});
let linksTimer = null;
$("dl-links").addEventListener("input", () => {
  clearTimeout(linksTimer);
  linksTimer = setTimeout(() => listLinks().catch((e) => dlAlert(e.message)), 300);
});
$("dl-source").addEventListener("click", (e) => {
  const b = e.target.closest("[data-source]");
  if (b && !b.classList.contains("active")) setSource(b.dataset.source);
});
$("dl-find").addEventListener("click", () => findVideos().catch((e) => dlAlert(e.message)));
$("dl-channel").addEventListener("keydown", (e) => { if (e.key === "Enter") findVideos().catch((err) => dlAlert(err.message)); });
$("dl-queries").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.ctrlKey) findVideos().catch((err) => dlAlert(err.message));
});
$("dl-reuse").addEventListener("change", () => {
  renderStats();
  // The list stays as it is (Start checks each license); the hint says whether YouTube's filter narrowed it.
  if (currentSource() === "search" && dl.rows.size && dl.lastSearch && !dl.running) {
    $("dl-detected").textContent = searchSummary();
  }
});
$("dl-views").addEventListener("click", (e) => {
  const tab = e.target.closest(".panel-tab");
  if (tab) showDlView(tab.dataset.view);
});
$("dl-ds-refresh").addEventListener("click", () => loadDataset().catch((e) => datasetMessage($("dl-dataset"), e.message)));
$("dl-output").addEventListener("change", () => {
  if (panelView($("dl-views")) === "dataset") loadDataset().catch(() => {});
});
$("dl-workers").addEventListener("input", renderWorkers);
$("dl-media").addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (button && !dl.running) setMedia(button.dataset.media);
});
$("dl-start").addEventListener("click", () => startDownload().catch((e) => dlAlert(e.message)));
$("dl-resume").addEventListener("click", () => startDownload(rowsWith("queued", "cancelled")).catch((e) => dlAlert(e.message)));
$("dl-retry").addEventListener("click", () => startDownload(rowsWith("failed")).catch((e) => dlAlert(e.message)));
$("dl-body").addEventListener("click", (e) => {
  const retry = e.target.closest(".row-retry");
  if (retry) startDownload([Number(retry.closest("tr").dataset.row)]).catch((err) => dlAlert(err.message));
});
$("dl-stop").addEventListener("click", () => stopDownload().catch((e) => toast(e.message, true)));
$("dl-search").addEventListener("input", applyFilter);
$("dl-filter").addEventListener("click", (e) => {
  const b = e.target.closest("[data-filter]");
  if (!b) return;
  document.querySelectorAll("#dl-filter button").forEach((x) => x.classList.toggle("active", x === b));
  applyFilter();
});
$("dl-body").addEventListener("dblclick", (e) => {
  const tr = e.target.closest("tr");
  if (tr && tr.classList.contains("has-file") && !e.target.closest("a")) {
    reveal("video", Number(tr.dataset.row)).catch((err) => toast(err.message, true));
  }
});
$("dl-open-folder").addEventListener("click", () => reveal("folder").catch((e) => toast(e.message, true)));
$("dl-open-report").addEventListener("click", () => reveal("report").catch((e) => toast(e.message, true)));
$("dl-open-splitter").addEventListener("click", () => openInSplitter().catch((e) => toast(e.message, true)));
$("menu-dl-reveal").addEventListener("click", () => {
  toggleMenu(false);
  reveal("folder").catch((e) => toast(e.message, true));
});
$("dl-log-copy").addEventListener("click", () => {
  navigator.clipboard.writeText($("dl-log").textContent)
    .then(() => toast("Activity log copied"), () => toast("Couldn't copy the log", true));
});
$("dl-log-clear").addEventListener("click", () => { $("dl-log").textContent = ""; });
