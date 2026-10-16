"""Video Downloader: fetch videos into one folder from a spreadsheet, a channel or keyword searches.

For a spreadsheet (.xlsx/.xls/.csv) the page picks the columns holding the
links and the file names; a channel link lists the whole channel; searches list
the top results of each line. download_engine does the work (yt-dlp for YouTube
and other sites, a plain HTTP download for direct file links). Videos already in
the output folder are skipped, so a run can be repeated after adding rows.
"Only reuse-allowed videos" keeps only videos licensed CC BY, CC BY-SA or CC0.
Every run writes _download_report.csv and report.txt into the output folder.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

import download_engine as engine
from common import find_ffmpeg, load_config, update_config

bp = Blueprint("downloader", __name__, url_prefix="/api/download")
lock = threading.Lock()

QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
BROWSERS = {"chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium"}
SOURCES = ("sheet", "links", "channel", "search")
MAX_WORKERS = 8
MAX_PER_QUERY = 500
MAX_LOG = 4000  # lines kept for the activity log
DEFAULTS = {"quality": "1080", "workers": 3, "retries": 3, "timeout": 60, "h264": False, "playlists": False,
            "cookies": "", "limit": 0, "reuse_only": False}


def clean_options(raw: dict) -> dict:
    """Validate the download settings from the page."""
    quality = str(raw.get("quality") or DEFAULTS["quality"])
    if quality not in QUALITIES:
        raise ValueError("Pick a quality from the list.")
    cookies = str(raw.get("cookies") or "")
    if cookies and cookies not in BROWSERS:
        raise ValueError("Pick a browser from the list, or Not needed.")
    return {
        "quality": quality,
        "workers": min(MAX_WORKERS, max(1, int(raw.get("workers") or DEFAULTS["workers"]))),
        "retries": min(20, max(1, int(raw.get("retries") or DEFAULTS["retries"]))),
        "timeout": min(600, max(10, int(raw.get("timeout") or DEFAULTS["timeout"]))),
        "h264": bool(raw.get("h264")),
        "playlists": bool(raw.get("playlists")),
        "cookies": cookies,
        "limit": max(0, int(raw.get("limit") or 0)),
        "reuse_only": bool(raw.get("reuse_only")),
    }


def clean_source(data: dict) -> dict:
    """Where the videos come from: a sheet, pasted links, a channel link, or searches (one per line)."""
    source = data.get("source") or "sheet"
    if source not in SOURCES:
        raise ValueError("Pick Sheet, Links, Channel or Search.")
    tabs = [tab for tab in engine.CHANNEL_TABS if tab in (data.get("tabs") or [])]
    queries = [line.strip() for line in str(data.get("queries") or "").splitlines() if line.strip()]
    links = [line.strip() for line in str(data.get("links") or "").splitlines() if line.strip()]
    return {
        "source": source,
        "links": links,
        "channel": str(data.get("channel") or "").strip(),
        "tabs": tabs,
        "queries": queries,
        "per_query": min(MAX_PER_QUERY, max(1, int(data.get("per_query") or 50))),
    }


def make_spec(data: dict, options: dict, output: Path | None = None) -> engine.JobSpec:
    """The engine's JobSpec for the source and settings the page chose."""
    src = clean_source(data)
    if src["source"] == "links":
        if not src["links"]:
            raise engine.JobError("Paste at least one video link, one per line.")
        spec = engine.JobSpec(source="links", links=tuple(src["links"]))
    elif src["source"] == "channel":
        if not src["channel"]:
            raise engine.JobError("Paste a channel link, e.g. https://www.youtube.com/@name.")
        if not src["tabs"]:
            raise engine.JobError("Tick at least one of Videos, Live and Shorts.")
        spec = engine.JobSpec(source="channel", channel=src["channel"], channel_tabs=tuple(src["tabs"]))
    elif src["source"] == "search":
        if not src["queries"]:
            raise engine.JobError("Type what to search for, one search per line.")
        spec = engine.JobSpec(source="search", queries=tuple(src["queries"]), per_query=src["per_query"])
    else:
        file = (data.get("file") or "").strip().strip('"')
        if not file:
            raise engine.JobError("Please pick the spreadsheet with the video links.")
        spec = engine.JobSpec(
            excel=Path(file),
            sheet=data.get("sheet") or 0,
            # None: let the engine work the column out. NO_COLUMN: name the files after the video titles.
            url_column=data.get("url_column") or None,
            name_column=engine.NO_COLUMN if data.get("name_column") == "-" else data.get("name_column") or None,
        )
    spec.limit = options["limit"] or None
    spec.reuse_only = options["reuse_only"]  # a search uses YouTube's Creative Commons filter for it
    spec.cookies_from_browser = options["cookies"] or None  # listing a channel may need them too
    spec.timeout = options["timeout"]
    if output is not None:
        spec.output = output
        spec.quality = options["quality"]
        spec.workers = options["workers"]
        spec.retries = options["retries"]
        spec.timeout = options["timeout"]
        spec.h264 = options["h264"]
        spec.playlists = options["playlists"]
    return spec


def listed_scan(data: dict) -> engine.Scan:
    """The list the page showed: the run downloads exactly these videos.

    Always for a channel or search: listing again would cost time and requests, and
    could give a different list (search results change, and the Creative Commons filter
    depends on when the box was ticked). And for Resume / Retry, from any source: the
    rows keep their numbers and sheet names, so files are named as in the first run.
    """
    rows = []
    for item in data.get("rows") or []:
        url = str(item.get("url") or "").strip() if isinstance(item, dict) else ""
        if url.startswith(("http://", "https://")):
            number = int(item.get("row") or 0) or len(rows) + 1
            rows.append(engine.VideoRow(number, url, str(item.get("name") or "") or None,
                                        title=str(item.get("title") or "")))
    if not rows:
        raise engine.JobError("Click Find videos first.")
    return engine.Scan([], "", None, rows, False)


def default_output(file: str) -> Path:
    """A downloads folder next to the spreadsheet."""
    return Path(file.strip().strip('"')).resolve().parent / "downloads"


class PageEvents(engine.Events):
    """Keeps what the engine reports, for the page to poll."""

    def __init__(self, job: DownloadJob):
        self.job = job

    def log(self, message: str) -> None:
        self.job.add_log(message)

    def row_started(self, item) -> None:
        self.job.update_row(item.row_number, status="downloading")

    def row_progress(self, row_number, done, total, speed, filename) -> None:
        self.job.update_row(row_number, status="downloading", done=done, total=total, speed=speed)

    def row_finished(self, result: engine.Result) -> None:
        # A channel or search row already shows its listed title; keep it when the result has none.
        title = {"title": result.title} if result.title else {}
        self.job.update_row(result.row_number, status=result.status, detail=" ".join(result.detail.split()),
                            path=result.path, size=result.size, duration=result.duration, speed=0.0,
                            license=result.license,
                            finished=result.status == "downloaded", **title)


class DownloadJob:
    """Downloads every row of the list in a background thread.

    `scan` is the list the page showed (a channel's videos or search results), so
    the run downloads exactly that; without it the source is read again (a sheet, pasted links).
    """

    def __init__(self, spec: engine.JobSpec, scan: engine.Scan | None = None, only: set[int] | None = None):
        self.spec = spec
        self.scan = scan
        self.only = only  # Resume / Retry: just these row numbers of the list
        self.cancel = threading.Event()
        self.rows: dict[int, dict] = {}
        self.rev = 0  # bumped on every row change, so the page only fetches what changed
        self.log: list[str] = []
        self.log_start = 0  # index of self.log[0] in the whole log (old lines are dropped)
        self.running = True
        self.error: str | None = None
        self.summary: dict | None = None
        self.folder = str(spec.output.resolve())
        self.ffmpeg = False
        self.started = time.monotonic()
        self.elapsed = 0.0
        self.data_lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def add_log(self, message: str):
        with self.data_lock:
            self.log.append(message)
            if len(self.log) > MAX_LOG:
                drop = len(self.log) - MAX_LOG
                del self.log[:drop]
                self.log_start += drop

    def update_row(self, number: int, finished: bool = False, **values):
        with self.data_lock:
            row = self.rows.get(number)
            if row is None:
                return
            self.rev += 1
            row.update(values, rev=self.rev)
            if finished and row["total"]:
                row["done"] = row["total"]  # the last progress report can fall just short

    def _run(self):
        try:
            job = engine.prepare_job(self.spec, self.scan)
            if self.only is not None:
                job.scan = dataclasses.replace(job.scan, rows=[r for r in job.rows if r.row_number in self.only])
                if not job.rows:
                    raise engine.JobError("Nothing left to download in this list.")
            with self.data_lock:
                self.rows = {r.row_number: {"row": r.row_number, "url": r.url, "name": r.name or "", "status": "queued",
                                            "done": 0, "total": 0, "speed": 0.0, "size": 0, "duration": 0.0,
                                            "detail": "", "path": "", "title": r.title, "license": "", "rev": 0}
                             for r in job.rows}
                self.ffmpeg = job.options.ffmpeg is not None
            if self.only is not None:
                self.add_log(f"Going on with {len(job.rows)} video{'s' if len(job.rows) != 1 else ''} of the list.")
            self.add_log(f"Saving to {self.folder}")
            if not self.ffmpeg:
                self.add_log("ffmpeg not found - quality is capped around 360p.")
            if self.spec.reuse_only:
                self.add_log("Only reuse-allowed videos (CC BY, CC BY-SA, CC0): each license is checked before downloading.")
            summary = engine.run_job(job, PageEvents(self), self.cancel)
            self.summary = {
                "counts": summary.counts,
                "elapsed": summary.elapsed,
                "bytes": summary.total_bytes,
                "duration": summary.total_duration,
                "folder_bytes": summary.folder_bytes,
                "folder_duration": summary.folder_duration,
                "report": summary.report_txt.exists(),
            }
        except engine.JobError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001 - never leave the page stuck on "running"
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.elapsed = time.monotonic() - self.started
            self.running = False

    def status(self, rev: int = -1, log_from: int = 0) -> dict:
        with self.data_lock:
            rows = [dict(r) for r in self.rows.values() if r["rev"] > rev]
            log = self.log[max(0, log_from - self.log_start):]
            log_end = self.log_start + len(self.log)
            total = len(self.rows)
            current_rev = self.rev
        return {
            "running": self.running,
            "stopping": self.running and self.cancel.is_set(),
            "folder": self.folder,
            "ffmpeg": self.ffmpeg,
            "total": total,
            "rows": rows,
            "rev": current_rev,
            "log": log,
            "log_end": log_end,
            "elapsed": time.monotonic() - self.started if self.running else self.elapsed,
            "error": self.error,
            "summary": self.summary,
        }


job: DownloadJob | None = None


def cancel_download():
    if job is not None and job.running:
        job.cancel.set()


def error(msg: str, status: int = 400, **extra):
    return jsonify({"ok": False, "error": msg, **extra}), status


def local_only():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(400)


# ---------------------------------------------------------------- routes


@bp.get("/state")
def api_state():
    cfg = load_config()
    return jsonify({
        "file": cfg.get("download_file", ""),
        "output": cfg.get("download_output", ""),
        "source": {"source": "sheet", "links": [], "channel": "", "tabs": ["videos"], "queries": [], "per_query": 50,
                   **cfg.get("download_source", {})},
        "options": {**DEFAULTS, **cfg.get("download_options", {})},
        "ffmpeg": find_ffmpeg() is not None,
        "running": job is not None and job.running,
        "has_job": job is not None,
    })


@bp.post("/scan")
def api_scan():
    """List the videos: the sheet's sheets, columns and links, a channel's videos, or search results."""
    data = request.get_json(force=True)
    sheets: list[str] = []
    try:
        spec = make_spec(data, clean_options(data.get("options") or {}))
        if spec.source == "sheet":
            sheets = engine.sheet_names(spec.excel)
            if sheets and spec.sheet not in sheets:
                spec.sheet = sheets[0]
        scan = engine.scan_source(spec)
    except (engine.JobError, ValueError) as e:
        return error(str(e))
    except Exception as e:  # noqa: BLE001 - a corrupt workbook or an odd site is not a crash
        what = "the spreadsheet" if (data.get("source") or "sheet") == "sheet" else "the videos"
        return error(f"Couldn't read {what}: {e}")
    return jsonify({
        "source": spec.source,
        "sheets": sheets,
        "sheet": spec.sheet if sheets else "",
        "columns": scan.columns,
        "url_column": scan.url_column,
        "name_column": scan.name_column,
        "headerless": scan.headerless,
        "label": scan.label,
        "notes": scan.notes,
        "rows": [{"row": r.row_number, "url": r.url, "name": r.name or "", "title": r.title} for r in scan.rows],
        "default_output": str(default_output(str(spec.excel))) if spec.source == "sheet" else "",
    })


@bp.post("/start")
def api_start():
    global job
    data = request.get_json(force=True)
    if job is not None and job.running:
        return error("A download is already running.", 409)
    try:
        options = clean_options(data.get("options") or {})
        source = clean_source(data)
        file = (data.get("file") or "").strip().strip('"')
        output = (data.get("output") or "").strip().strip('"')
        if output:
            output_dir = Path(output).resolve()
        elif source["source"] == "sheet" and file:
            output_dir = default_output(file)
        elif source["source"] == "sheet":
            output_dir = None  # make_spec asks for the spreadsheet
        else:
            return error("Pick an output folder for the videos.")
        spec = make_spec(data, options, output_dir)
        # Resume / Retry: only these rows, from the exact list the page shows.
        only = {int(n) for n in data["only"]} if data.get("only") is not None else None
        listed = listed_scan(data) if spec.source in ("channel", "search") or only is not None else None
    except (engine.JobError, KeyError, TypeError, ValueError) as e:
        return error(str(e) or "Invalid settings.")
    if spec.source == "sheet" and not spec.excel.is_file():
        return error(f"Spreadsheet not found: {spec.excel}")
    remembered = {"download_file": str(spec.excel.resolve())} if spec.source == "sheet" else {}
    update_config(**remembered, download_output=output, download_options=options, download_source=source)
    with lock:
        job = DownloadJob(spec, listed, only)
    return jsonify({"ok": True})


@bp.post("/overview")
def api_overview():
    """The dataset in the output folder, across every run: videos, hours, size, licenses, channels."""
    data = request.get_json(force=True)
    output = (data.get("output") or "").strip().strip('"')
    file = (data.get("file") or "").strip().strip('"')
    if output:
        folder = Path(output)
    elif (data.get("source") or "sheet") == "sheet" and file:
        folder = default_output(file)
    elif job is not None:
        folder = Path(job.folder)
    else:
        return error("Pick an output folder to see what's in it.")
    try:
        return jsonify(engine.folder_overview(folder.resolve(), find_ffmpeg()))
    except engine.JobError as e:
        return error(str(e))


@bp.get("/job")
def api_job():
    if job is None:
        return jsonify({"running": False, "rows": [], "log": []})
    return jsonify(job.status(int(request.args.get("rev", -1)), int(request.args.get("log", 0))))


@bp.post("/job/stop")
def api_job_stop():
    cancel_download()
    return jsonify({"ok": True})


@bp.post("/reveal")
def api_reveal():
    """Open the output folder, the report or a downloaded video (only on the PC the app runs on)."""
    local_only()
    data = request.get_json(force=True)
    if job is not None:
        target = Path(job.folder)
    else:  # nothing downloaded yet this session: the folder from the last settings
        cfg = load_config()
        if cfg.get("download_output"):
            target = Path(cfg["download_output"])
        elif cfg.get("download_file"):
            target = default_output(cfg["download_file"])
        else:
            return error("Pick a spreadsheet and an output folder first.")
        if data.get("what") != "folder":
            abort(400)
    if data.get("what") == "report":
        target = target / engine.REPORT_TXT
    elif data.get("what") == "video":
        row = job.rows.get(int(data.get("row", -1)))
        if not row or not row["path"]:
            abort(400)
        target = Path(row["path"])
    if not target.exists():
        return error(f"{target} doesn't exist yet.")
    os.startfile(target)
    return jsonify({"ok": True})
