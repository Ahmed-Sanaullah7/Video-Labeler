"use strict";

// Video Downloader: download the videos linked in a spreadsheet (see downloader.py).

const DL_STATUS = {
  queued: "Queued", downloading: "Downloading", downloaded: "Downloaded",
  skipped: "Already there", failed: "Failed", cancelled: "Stopped",
};
const DL_FILTERS = {
  all: null,
  active: ["queued", "downloading"],
  done: ["downloaded", "skipped"],
  failed: ["failed", "cancelled"],
};
const DL_FINISHED = ["downloaded", "skipped", "failed", "cancelled"];

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

function sheetChoice() {
  return { file: $("dl-file").value, sheet: $("dl-sheet").value, url_column: $("dl-url").value,
           name_column: $("dl-name").value, limit: Number($("dl-limit").value) || 0 };
}

function dlOptions() {
  return {
    quality: $("dl-quality").value, workers: Number($("dl-workers").value),
    retries: Number($("dl-retries").value), timeout: Number($("dl-timeout").value),
    h264: $("dl-h264").checked, playlists: $("dl-playlists").checked,
    cookies: $("dl-cookies").value, limit: Number($("dl-limit").value) || 0,
  };
}

// ---------------------------------------------------------------- screen

async function loadDownload() {
  const data = await api("GET", "/api/download/state");
  showSection("downloader");
  $("dl-ffmpeg").classList.toggle("hidden", data.ffmpeg);
  if (!dl.loaded) {
    dl.loaded = true;
    const o = data.options;
    $("dl-file").value = data.file;
    $("dl-output").value = data.output;
    $("dl-quality").value = o.quality;
    $("dl-workers").value = o.workers;
    $("dl-retries").value = o.retries;
    $("dl-timeout").value = o.timeout;
    $("dl-h264").checked = o.h264;
    $("dl-playlists").checked = o.playlists;
    $("dl-cookies").value = o.cookies;
    $("dl-limit").value = o.limit;
    renderWorkers();
  }
  if ($("dl-file").value && !dl.rows.size) await scanSheet(true);
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
  const token = ++dl.scanToken;
  if (!$("dl-file").value.trim()) {
    setRows([]);
    $("dl-detected").textContent = "Pick an .xlsx or .csv file with a column of video links.";
    return renderDownloadControls();
  }
  $("dl-detected").textContent = "Reading the sheet…";
  try {
    const s = await api("POST", "/api/download/scan", sheetChoice());
    if (token !== dl.scanToken) return;
    const columns = s.columns.map((c) => [c, c]);
    fillSelect($("dl-sheet"), s.sheets.length ? s.sheets.map((n) => [n, n]) : [["", "Single sheet"]], s.sheet);
    fillSelect($("dl-url"), [["", "Auto-detect"], ...columns], $("dl-url").value);
    fillSelect($("dl-name"), [["", "Auto-detect"], ["-", "Video title"], ...columns], $("dl-name").value);
    $("dl-sheet").disabled = !s.sheets.length;
    $("dl-url").disabled = $("dl-name").disabled = false;
    $("dl-output").placeholder = `Defaults to ${s.default_output}`;
    setRows(s.rows.map((r) => ({ ...r, status: "queued", done: 0, total: 0, speed: 0, size: 0, detail: "", path: "", title: "" })));
    const names = s.name_column ? `“${s.name_column}”` : "the video's own title";
    $("dl-detected").textContent = `${plural(s.rows.length, "link")} in “${s.url_column}”, named after ${names}`
      + (s.headerless ? ". The sheet has no header row." : ".");
  } catch (e) {
    if (token !== dl.scanToken) return;
    setRows([]);
    $("dl-detected").textContent = "Couldn't read this sheet.";
    dlAlert(e.message);
  }
  renderDownloadControls();
}

async function browseSheet() {
  const file = await desktop().pick_file($("dl-file").value);
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
      + '<td><span class="badge"></span></td><td><div class="dl-rowprog"></div></td><td class="muted"></td>';
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
  detail.textContent = r.status === "failed" ? r.detail : "";
  detail.title = detail.textContent;
  detail.classList.toggle("hidden", !detail.textContent);
  status.firstChild.textContent = DL_STATUS[r.status] || r.status;
  status.firstChild.className = `badge ${r.status}`;
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
  $("dl-empty").textContent = dl.rows.size ? "No matching videos" : "Pick a spreadsheet to see the videos it links to.";
  $("dl-empty").classList.toggle("hidden", shown > 0);
}

// ---------------------------------------------------------------- totals

function counts() {
  const c = { queued: 0, downloading: 0, downloaded: 0, skipped: 0, failed: 0, cancelled: 0 };
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
  $("dl-n-failed").textContent = c.failed + c.cancelled;
  $("dl-n-data").textContent = humanSize(data);
  const pct = (k) => (n ? `${(100 * k) / n}%` : "0");
  $("dl-bar-done").style.width = pct(c.downloaded + c.skipped);
  $("dl-bar-failed").style.width = pct(c.failed + c.cancelled);
  $("dl-bar-active").style.width = pct(partial);
}

function renderRunProgress() {
  const c = counts(), n = dl.rows.size;
  const done = DL_FINISHED.reduce((sum, k) => sum + c[k], 0);
  let speed = 0;
  for (const r of dl.rows.values()) if (r.status === "downloading") speed += r.speed;
  $("dl-progress-title").textContent = dl.stopping ? "Stopping, finishing the current chunks…"
    : c.downloading ? `Downloading ${plural(c.downloading, "video")}` : n ? "Working…" : "Reading the sheet…";
  const parts = [`${done} of ${n} done`];
  if (speed) parts.push(`${humanSize(speed)}/s`);
  parts.push(`${fmtDuration(dl.elapsed)} elapsed`);
  if (done && done < n) parts.push(`about ${fmtDuration((dl.elapsed / done) * (n - done))} left`);
  $("dl-progress-detail").textContent = parts.join(" · ");
}

function renderDownloadControls() {
  document.querySelectorAll("#downloader .dl-rail-body :is(input, select, button)").forEach((el) => {
    if (el.id === "dl-sheet" || el.id === "dl-url" || el.id === "dl-name") {
      if (dl.running) el.disabled = true;
      else if (el.id === "dl-sheet") el.disabled = el.options.length < 2 && !el.value;
      else el.disabled = !dl.rows.size && el.options.length < 2;
    } else {
      el.disabled = dl.running;
    }
  });
  $("dl-start").disabled = dl.running || !dl.rows.size;
  $("dl-stop").classList.toggle("hidden", !dl.running);
  $("dl-stop").disabled = dl.stopping;
  $("dl-stop").lastChild.textContent = dl.stopping ? "Stopping…" : "Stop";
  if (!dl.running && !dl.watching && $("dl-done").classList.contains("hidden")) {
    $("dl-progress-title").textContent = "Ready";
    $("dl-progress-detail").textContent = dl.rows.size ? `${dl.rows.size} queued` : "";
  }
}

// ---------------------------------------------------------------- running

async function startDownload() {
  if (dl.running || !dl.rows.size) return;
  dlAlert("");
  $("dl-done").classList.add("hidden");
  await api("POST", "/api/download/start", { ...sheetChoice(), output: $("dl-output").value, options: dlOptions() });
  dl.jobShown = true;
  resetJobView();
  dl.running = true;
  for (const r of dl.rows.values()) Object.assign(r, { status: "queued", done: 0, total: 0, speed: 0, size: 0, detail: "", path: "" });
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
  if (job.total) {
    if (dl.rev === -1) setRows(job.rows);
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
  const s = job.summary, c = s.counts;
  const headline = c.cancelled ? "Stopped early" : "Finished";
  $("dl-progress-title").textContent = `${headline}: ${c.downloaded} downloaded, ${c.skipped} already there, ${c.failed} failed`;
  $("dl-progress-detail").textContent = `${humanSize(s.bytes)} in ${fmtDuration(s.elapsed)}`;
  const onDisk = c.downloaded + c.skipped;
  let text = `${plural(onDisk, "video")} in ${job.folder}`;
  if (s.folder_bytes) text += ` · ${humanSize(s.folder_bytes)}`;
  if (s.folder_duration) text += ` · ${fmtDuration(s.folder_duration)} of video`;
  $("dl-done-text").textContent = text;
  $("dl-open-report").classList.toggle("hidden", !s.report);
  $("dl-open-splitter").classList.toggle("hidden", !onDisk);
  $("dl-done").classList.remove("hidden");
  if (c.failed) dlAlert(`${plural(c.failed, "video")} couldn't be downloaded. The Failed filter shows which, and report.txt has the reasons.`, true);
  if (live) toast(c.cancelled ? "Download stopped" : "Download finished");
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
["dl-url", "dl-name", "dl-limit"].forEach((id) => $(id).addEventListener("change", () => scanSheet()));
$("dl-workers").addEventListener("input", renderWorkers);
$("dl-start").addEventListener("click", () => startDownload().catch((e) => dlAlert(e.message)));
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
