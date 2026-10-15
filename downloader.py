"""Video Downloader: fetch the videos linked in a spreadsheet into one folder.

The page picks a spreadsheet (.xlsx/.xls/.csv) and the columns holding the
links and the file names; download_engine does the work (yt-dlp for YouTube and
other sites, a plain HTTP download for direct file links). Videos already in the
output folder are skipped, so a sheet can be run again after adding rows.
Every run writes _download_report.csv and report.txt into the output folder.
"""

from __future__ import annotations

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
MAX_WORKERS = 8
MAX_LOG = 4000  # lines kept for the activity log
DEFAULTS = {"quality": "1080", "workers": 3, "retries": 3, "timeout": 60, "h264": False, "playlists": False,
            "cookies": "", "limit": 0}


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
    }


def make_spec(data: dict, options: dict | None = None, output: Path | None = None) -> engine.JobSpec:
    """The engine's JobSpec for the sheet and columns the page chose."""
    file = (data.get("file") or "").strip().strip('"')
    if not file:
        raise engine.JobError("Please pick the spreadsheet with the video links.")
    options = options or {"limit": int(data.get("limit") or 0)}
    spec = engine.JobSpec(
        excel=Path(file),
        sheet=data.get("sheet") or 0,
        # None: let the engine work the column out. NO_COLUMN: name the files after the video titles.
        url_column=data.get("url_column") or None,
        name_column=engine.NO_COLUMN if data.get("name_column") == "-" else data.get("name_column") or None,
        limit=options["limit"] or None,
    )
    if output is not None:
        spec.output = output
        spec.quality = options["quality"]
        spec.workers = options["workers"]
        spec.retries = options["retries"]
        spec.timeout = options["timeout"]
        spec.h264 = options["h264"]
        spec.playlists = options["playlists"]
        spec.cookies_from_browser = options["cookies"] or None
    return spec


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
        self.job.update_row(result.row_number, status=result.status, detail=" ".join(result.detail.split()),
                            path=result.path, title=result.title, size=result.size, speed=0.0,
                            finished=result.status == "downloaded")


class DownloadJob:
    """Downloads every row of the sheet in a background thread."""

    def __init__(self, spec: engine.JobSpec):
        self.spec = spec
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
            job = engine.prepare_job(self.spec)
            with self.data_lock:
                self.rows = {r.row_number: {"row": r.row_number, "url": r.url, "name": r.name or "", "status": "queued",
                                            "done": 0, "total": 0, "speed": 0.0, "size": 0, "detail": "", "path": "",
                                            "title": "", "rev": 0}
                             for r in job.rows}
                self.ffmpeg = job.options.ffmpeg is not None
            self.add_log(f"Saving to {self.folder}")
            if not self.ffmpeg:
                self.add_log("ffmpeg not found - quality is capped around 360p.")
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
        "options": {**DEFAULTS, **cfg.get("download_options", {})},
        "ffmpeg": find_ffmpeg() is not None,
        "running": job is not None and job.running,
        "has_job": job is not None,
    })


@bp.post("/scan")
def api_scan():
    """Read the sheet: its sheets, columns and the links found in it."""
    data = request.get_json(force=True)
    try:
        spec = make_spec(data)
        sheets = engine.sheet_names(spec.excel)
        if sheets and spec.sheet not in sheets:
            spec.sheet = sheets[0]
        scan = engine.scan_source(spec)
    except (engine.JobError, ValueError) as e:
        return error(str(e))
    except Exception as e:  # noqa: BLE001 - a corrupt workbook is not a crash
        return error(f"Couldn't read the spreadsheet: {e}")
    return jsonify({
        "sheets": sheets,
        "sheet": spec.sheet if sheets else "",
        "columns": scan.columns,
        "url_column": scan.url_column,
        "name_column": scan.name_column,
        "headerless": scan.headerless,
        "rows": [{"row": r.row_number, "url": r.url, "name": r.name or ""} for r in scan.rows],
        "default_output": str(default_output(str(spec.excel))),
    })


@bp.post("/start")
def api_start():
    global job
    data = request.get_json(force=True)
    if job is not None and job.running:
        return error("A download is already running.", 409)
    try:
        options = clean_options(data.get("options") or {})
        file = (data.get("file") or "").strip().strip('"')
        output = (data.get("output") or "").strip().strip('"')
        output_dir = Path(output).resolve() if output else default_output(file) if file else None
        spec = make_spec(data, options, output_dir)
    except (engine.JobError, KeyError, TypeError, ValueError) as e:
        return error(str(e) or "Invalid settings.")
    if not spec.excel.is_file():
        return error(f"Spreadsheet not found: {spec.excel}")
    update_config(download_file=str(spec.excel.resolve()), download_output=output, download_options=options)
    with lock:
        job = DownloadJob(spec)
    return jsonify({"ok": True})


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
