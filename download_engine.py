"""Download videos listed in an Excel file into one output folder.

Every run saves into the same folder, so rerunning the same sheet picks up
where the last run left off: videos already there are skipped, and only what
is new gets fetched.

YouTube (and other video sites) are handled by yt-dlp; plain links to .mp4 and
friends are streamed directly over HTTP. Everything is saved as .mp4.

This is the engine behind the Video Downloader (see downloader.py):
scan_source(), prepare_job() and run_job() do the work, and an Events object
receives the progress the page draws.
"""

from __future__ import annotations

import csv
import mimetypes
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError

from common import NO_WINDOW, find_ffmpeg

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv",
    ".mpg", ".mpeg", ".3gp", ".ts", ".ogv",
}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# yt-dlp names each unmerged stream "<title>.f<format id>.<ext>".
FORMAT_INTERMEDIATE_RE = re.compile(r"\.f\d+\.[^.]+$", re.IGNORECASE)
NAME_COLUMN_HINTS = ("name", "title", "filename", "file_name", "video", "id", "slug")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# yt-dlp failures that will never succeed on a retry.
PERMANENT_ERRORS = (
    "video unavailable", "is unavailable", "private video", "members-only",
    "removed by the uploader", "is not available", "has been removed",
    "account associated with this video has been terminated",
    "sign in to confirm your age", "unsupported url", "no video formats found",
    "incomplete youtube id", "copyright", "404: not found",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_SIZE = 1 << 20  # 1 MiB

REPORT_CSV = "_download_report.csv"
REPORT_TXT = "report.txt"


class JobError(RuntimeError):
    """A problem with the request itself: missing column, empty sheet, bad path.

    The page shows these as a message instead of a traceback.
    """


class Cancelled(DownloadCancelled):
    """Raised inside a download when the caller asks the run to stop.

    Subclassing yt-dlp's own cancellation error matters: yt-dlp re-raises that
    one untouched, so a Stop from the window unwinds the download immediately
    instead of being retried as if it were a network error.
    """


# --------------------------------------------------------------------------- #
# Progress reporting
# --------------------------------------------------------------------------- #

class Events:
    """Where progress goes: the console prints it, the window draws it.

    Every method is called from the download worker threads, so an
    implementation has to be safe to call from several at once. The default
    does nothing, which is what a silent embedding wants.
    """

    def log(self, message: str) -> None:
        """A line of human-readable narration."""

    def row_started(self, item: VideoRow) -> None:
        """A row left the queue and is being fetched."""

    def row_progress(
        self, row_number: int, done: int, total: int, speed: float, filename: str
    ) -> None:
        """Bytes so far, out of `total` (0 when the size is unknown)."""

    def row_finished(self, result: Result) -> None:
        """A row reached its final state: downloaded, skipped, failed, cancelled."""


class EventLogger:
    """Hands yt-dlp's own chatter to `events` instead of letting it reach stdout.

    Two reasons. It keeps the window's activity pane honest about what yt-dlp
    is doing, and it removes the console from the picture entirely - printing a
    title containing, say, a fullwidth colon to a cp1252 console raises
    UnicodeEncodeError, and that kills the download the message was only
    describing.
    """

    # yt-dlp routes its progress redraws through debug(); we already draw those.
    NOISE = ("[debug] ", "[download] ")

    def __init__(self, events: Events, row_number: int) -> None:
        self.events = events
        self.row_number = row_number

    def _say(self, message: str) -> None:
        self.events.log(f"[{self.row_number}] {message.strip()}")

    def debug(self, message: str) -> None:
        if not message.startswith(self.NOISE):
            self._say(message)

    def info(self, message: str) -> None:
        self.debug(message)

    def warning(self, message: str) -> None:
        self._say(message)

    def error(self, message: str) -> None:
        self._say(message)


# --------------------------------------------------------------------------- #
# Reading the spreadsheet
# --------------------------------------------------------------------------- #

@dataclass
class VideoRow:
    row_number: int  # 1-based data row, matching the spreadsheet body order
    url: str
    name: str | None


def read_table(path: Path, sheet: str | int) -> tuple[pd.DataFrame, bool]:
    """Load an .xlsx/.xls/.csv file with every cell kept as text.

    Returns (frame, headerless). A sheet that is just a bare list of links has
    no header row, and pandas would otherwise swallow the first link as the
    column name - so it is re-read with generated headers instead.
    """
    def load(header: int | None) -> pd.DataFrame:
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path, dtype=str, keep_default_na=False, header=header)
        return pd.read_excel(path, sheet_name=sheet, dtype=str, header=header).fillna("")

    frame = load(0)
    if not any(URL_RE.search(str(column)) for column in frame.columns):
        return frame, False

    frame = load(None)
    frame.columns = [f"column {index + 1}" for index in range(len(frame.columns))]
    return frame, True


def read_hyperlinks(path: Path, sheet: str | int, headerless: bool) -> dict[tuple[int, str], str]:
    """Map (data row index, column name) -> hyperlink target.

    Sheets often show a label such as "Watch" while the real URL lives in the
    cell's hyperlink, which pandas does not expose.
    """
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return {}
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(path, data_only=True)
        worksheet = workbook[sheet] if isinstance(sheet, str) else workbook.worksheets[sheet]
        rows = worksheet.iter_rows()
        if headerless:
            header = [f"column {index + 1}" for index in range(worksheet.max_column)]
        else:
            header = [str(cell.value).strip() if cell.value is not None else ""
                      for cell in next(rows)]
        links: dict[tuple[int, str], str] = {}
        for index, row in enumerate(rows):
            for cell in row:
                position = cell.column - 1
                if cell.hyperlink and cell.hyperlink.target and position < len(header):
                    links[(index, header[position])] = cell.hyperlink.target
        workbook.close()
        return links
    except Exception:  # an odd workbook must not stop the run
        return {}


def pick_url_column(frame: pd.DataFrame, hyperlinks: dict[tuple[int, str], str]) -> str:
    """Choose the column holding the most links, preferring URL-ish headers."""
    scores: dict[str, int] = {}
    for column in frame.columns:
        score = sum(1 for value in frame[column] if isinstance(value, str) and URL_RE.search(value))
        score += sum(1 for (_, name) in hyperlinks if name == column)
        if score:
            header = str(column).lower()
            if any(hint in header for hint in ("url", "link", "href", "video", "download")):
                score += len(frame)  # tie-break in favour of an explicit header
            scores[column] = score
    if not scores:
        raise JobError(
            "No column containing http(s) links was found. "
            "Choose the URL column yourself."
        )
    return max(scores, key=lambda column: scores[column])


def pick_name_column(frame: pd.DataFrame, url_column: str) -> str | None:
    for column in frame.columns:
        if column == url_column:
            continue
        if any(hint in str(column).lower() for hint in NAME_COLUMN_HINTS):
            return column
    return None


def cell_to_url(cell: str) -> str:
    """Pull a usable URL out of a cell, accepting bare YouTube video IDs."""
    cell = cell.strip()
    match = URL_RE.search(cell)
    if match:
        return match.group(0).rstrip('.,;)"\'')
    if YOUTUBE_ID_RE.match(cell):
        return f"https://www.youtube.com/watch?v={cell}"
    return ""


def extract_rows(
    frame: pd.DataFrame,
    url_column: str,
    name_column: str | None,
    hyperlinks: dict[tuple[int, str], str],
) -> list[VideoRow]:
    rows: list[VideoRow] = []
    for index, record in enumerate(frame.to_dict("records")):
        url = cell_to_url(str(record.get(url_column, "") or ""))
        url = url or hyperlinks.get((index, url_column), "")
        if not url:
            continue
        name = str(record.get(name_column, "") or "").strip() if name_column else ""
        rows.append(VideoRow(row_number=index + 1, url=url, name=name or None))
    return rows


# --------------------------------------------------------------------------- #
# Filenames
# --------------------------------------------------------------------------- #

def sanitize(name: str, fallback: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120].strip() or fallback


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def video_id(url: str) -> str:
    """The YouTube id in a URL, used to spot an already-downloaded file."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    candidate = ""
    if host in ("youtu.be", "youtube.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif "youtube.com" in host:
        if "/shorts/" in parsed.path:
            candidate = parsed.path.split("/shorts/")[1].split("/")[0]
        else:
            candidate = parse_qs(parsed.query).get("v", [""])[0]
    return candidate if YOUTUBE_ID_RE.match(candidate) else ""


def target_stem(item: VideoRow) -> str | None:
    """File name (without extension) for a row.

    None means "no usable name in the sheet": yt-dlp then names the file after
    the video's own title, which beats naming it after an opaque video id.
    """
    fallback = f"video_{item.row_number:04d}"
    if item.name:
        return sanitize(item.name, fallback)
    if is_direct_file(item.url):
        return sanitize(Path(unquote(urlparse(item.url).path)).stem, fallback)
    return None


def existing_by_id(folder: Path, ident: str) -> Path | None:
    """Find a previous download tagged with this video id, e.g. "Title [abc123].mp4"."""
    for path in sorted(folder.glob(f"*[[]{ident}].*")):
        if is_finished_video(path):
            return path
    return None


def unique_stem(stem: str, taken: set[str], lock: threading.Lock) -> str:
    """Reserve a stem so parallel rows with the same title do not collide."""
    with lock:
        candidate, counter = stem, 2
        while candidate.lower() in taken:
            candidate = f"{stem} ({counter})"
            counter += 1
        taken.add(candidate.lower())
        return candidate


def is_finished_video(path: Path) -> bool:
    """True only for a completed video file.

    An interrupted run leaves debris - "name.mp4.part", ".part-Frag12",
    ".ytdl", and per-stream intermediates such as "name.f605.mp4". Matching on
    a whitelist of real video extensions is the only reliable way to tell those
    apart from a finished download; treating one as complete would skip the
    video forever.
    """
    return (
        path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and not FORMAT_INTERMEDIATE_RE.search(path.name)
    )


def existing_download(folder: Path, stem: str) -> Path | None:
    for path in sorted(folder.glob(f"{glob_escape(stem)}.*")):
        if is_finished_video(path):
            return path
    return None


def read_previous_report(folder: Path) -> dict[str, tuple[float, str]]:
    """What earlier runs measured, keyed by file name -> (runtime, title).

    A rerun skips everything already on disk, and a skipped video still belongs
    in the totals. Reading the last report back is how those totals survive:
    the alternative is probing every file in the folder on every run.
    """
    report = folder / REPORT_CSV
    if not report.is_file():
        return {}
    known: dict[str, tuple[float, str]] = {}
    try:
        with report.open(newline="", encoding="utf-8") as handle:
            for record in csv.DictReader(handle):
                name = Path(record.get("file") or "").name
                if not name:
                    continue
                try:
                    seconds = float(record.get("runtime_seconds") or 0)
                except ValueError:
                    seconds = 0.0
                known[name] = (max(0.0, seconds), record.get("title") or "")
    except (OSError, csv.Error):
        return {}  # a half-written or hand-edited report is not worth a crash
    return known


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #

@dataclass
class Options:
    output: Path
    quality: str
    retries: int
    timeout: int
    verbose: bool
    ffmpeg: str | None = None  # the ffmpeg executable; None: single-file formats only
    h264_only: bool = False
    cookies_from_browser: str | None = None
    cookies_file: Path | None = None
    playlists: bool = False


@dataclass
class Result:
    row_number: int
    url: str
    status: str  # downloaded | skipped | failed | cancelled
    path: str = ""
    detail: str = ""
    title: str = ""
    size: int = 0
    duration: float = 0.0  # how long the video plays for, in seconds (0 = unknown)


def is_direct_file(url: str) -> bool:
    return Path(unquote(urlparse(url).path)).suffix.lower() in VIDEO_EXTENSIONS


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path: Path, ffmpeg: str | None = None) -> float:
    """How long a downloaded file plays for, in seconds. 0.0 when unknown.

    Only the sources yt-dlp cannot describe need this - plain file URLs, and
    the occasional extractor that reports no duration. The app ships ffmpeg
    without ffprobe, so the length is read from what `ffmpeg -i` prints.
    """
    if not ffmpeg:
        return 0.0
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)],
            capture_output=True, timeout=60, text=True, errors="replace", creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0
    match = DURATION_RE.search(proc.stderr)  # "N/A" for a live stream: no match
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_selector(quality: str, have_ffmpeg: bool, h264_only: bool = False) -> str:
    """Select mp4 video plus m4a audio, capped at `quality` (a height or 'best').

    Without ffmpeg nothing can be merged, so only already-combined formats are
    offered; on YouTube that generally caps out at 360p.
    """
    cap = "" if quality in ("best", "") else f"[height<=?{quality.rstrip('p')}]"
    if not have_ffmpeg:
        return f"best[ext=mp4]{cap}/best[ext=mp4]/best{cap}/best"

    chain = []
    if h264_only:
        # avc1 plays everywhere; YouTube otherwise often serves av01 inside mp4.
        chain.append(f"bestvideo[ext=mp4][vcodec^=avc1]{cap}+bestaudio[ext=m4a]")
    chain += [
        f"bestvideo[ext=mp4]{cap}+bestaudio[ext=m4a]",
        f"bestvideo{cap}+bestaudio",
        f"best[ext=mp4]{cap}",
        f"best{cap}",
        "best",
    ]
    return "/".join(chain)


def ytdlp_options(
    options: Options,
    stem: str | None,
    row_number: int,
    events: Events,
    cancel: threading.Event | None,
) -> dict:
    have_ffmpeg = options.ffmpeg is not None
    # No name in the sheet: let yt-dlp use the video's own title, tagged with the
    # id so a repeat run can still recognise the file.
    name_template = f"{stem}.%(ext)s" if stem else "%(title)s [%(id)s].%(ext)s"
    settings = {
        "outtmpl": {"default": str(options.output / name_template)},
        "format": format_selector(options.quality, have_ffmpeg, options.h264_only),
        "noplaylist": not options.playlists,
        "retries": options.retries,
        "fragment_retries": options.retries,
        "socket_timeout": options.timeout,
        "concurrent_fragment_downloads": 4,
        "ignoreerrors": False,
        "noprogress": not options.verbose,
        "quiet": not options.verbose,
        "no_warnings": not options.verbose,
        "consoletitle": False,
        "windowsfilenames": True,
        "overwrites": False,
        "continuedl": True,
    }
    if have_ffmpeg:
        # Pin the app's own binary so a broken copy earlier on PATH cannot win,
        # and guarantee an .mp4 container even when the source is webm.
        settings["ffmpeg_location"] = options.ffmpeg
        settings["merge_output_format"] = "mp4"
        # --remux-video is a command line option only; through the API it is
        # this post-processor (the "remux_video" key would be ignored).
        settings["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    if options.cookies_from_browser:
        settings["cookiesfrombrowser"] = (options.cookies_from_browser,)
    if options.cookies_file:
        settings["cookiefile"] = str(options.cookies_file)
    # The hook is always installed: besides progress it is the only place a
    # Stop can interrupt yt-dlp part-way through a large file.
    settings["progress_hooks"] = [make_progress_hook(row_number, events, cancel)]
    if not options.verbose:
        # --verbose means "show me raw yt-dlp", so only take the output over
        # when the caller has not asked to see it.
        settings["logger"] = EventLogger(events, row_number)
    return settings


def make_progress_hook(row_number: int, events: Events, cancel: threading.Event | None):
    """Forward yt-dlp's byte counts to `events`, and honour a Stop request."""

    def hook(status: dict) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled("stopped by the user")
        if status.get("status") != "downloading":
            return
        try:
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            events.row_progress(
                row_number,
                int(status.get("downloaded_bytes") or 0),
                int(total or 0),
                float(status.get("speed") or 0.0),
                Path(status.get("filename") or "").name,
            )
        except Cancelled:
            raise
        except Exception:
            pass  # progress reporting must never interrupt a download

    return hook


def downloaded_path(downloader: YoutubeDL, info: dict) -> Path:
    """The file yt-dlp actually produced, after any merge or remux."""
    for entry in info.get("requested_downloads") or []:
        if entry.get("filepath"):
            return Path(entry["filepath"])
    return Path(downloader.prepare_filename(info))


def download_with_ytdlp(
    item: VideoRow,
    stem: str | None,
    options: Options,
    events: Events,
    cancel: threading.Event | None = None,
) -> tuple[Path, str, float]:
    """Returns the file written, its title, and its runtime in seconds."""
    settings = ytdlp_options(options, stem, item.row_number, events, cancel)
    with YoutubeDL(settings) as downloader:
        info = downloader.extract_info(item.url, download=True)
        if info.get("_type") == "playlist":  # --playlists: keep the first entry's file
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise DownloadError("playlist contained no downloadable videos")
            info = entries[0]
        try:  # most extractors give a duration; a live stream gives None
            duration = max(0.0, float(info.get("duration") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        return downloaded_path(downloader, info), info.get("title") or "", duration


def download_direct(
    item: VideoRow,
    stem: str,
    options: Options,
    events: Events,
    cancel: threading.Event | None = None,
) -> tuple[Path, str, float]:
    """Stream a plain file URL to disk via a .part file.

    Nothing here knows the runtime - `download_one` asks ffmpeg for it.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        response = session.get(item.url, stream=True, timeout=options.timeout)
        response.raise_for_status()

        extension = Path(unquote(urlparse(response.url).path)).suffix.lower()
        if extension not in VIDEO_EXTENSIONS:
            mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            extension = mimetypes.guess_extension(mime) or ".mp4"

        destination = options.output / f"{stem}{extension}"
        partial = destination.with_suffix(destination.suffix + ".part")
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        started = time.monotonic()

        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if cancel is not None and cancel.is_set():
                    raise Cancelled("stopped by the user")
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                elapsed = time.monotonic() - started
                events.row_progress(
                    item.row_number, downloaded, total,
                    downloaded / elapsed if elapsed > 0 else 0.0, destination.name,
                )

        if total and downloaded < total:
            partial.unlink(missing_ok=True)
            raise IOError(f"incomplete download: {downloaded} of {total} bytes")
        partial.replace(destination)
        return destination, "", 0.0
    finally:
        session.close()


def is_permanent(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in PERMANENT_ERRORS)


def sleep_unless_cancelled(seconds: float, cancel: threading.Event | None) -> None:
    """Wait out a retry backoff, but wake up the moment Stop is pressed."""
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise Cancelled("stopped by the user")


def download_one(
    item: VideoRow,
    options: Options,
    taken: set[str],
    taken_lock: threading.Lock,
    events: Events | None = None,
    cancel: threading.Event | None = None,
    known: dict[str, tuple[float, str]] | None = None,
) -> Result:
    events = events or Events()
    known = known if known is not None else {}

    def finish(result: Result) -> Result:
        events.row_finished(result)
        return result

    if cancel is not None and cancel.is_set():
        # Queued but never started: the pool drains these instantly on a Stop.
        return finish(Result(item.row_number, item.url, "cancelled", detail="not started"))

    stem = target_stem(item)
    ident = video_id(item.url)
    if stem:
        existing = existing_download(options.output, stem)
    elif ident:
        existing = existing_by_id(options.output, ident)
    else:
        existing = None

    if existing:
        events.log(f"[{item.row_number}] skip, already downloaded: {existing.name}")
        if stem:
            with taken_lock:
                taken.add(stem.lower())
        # Carry the file's runtime and title forward so a rerun's report still
        # describes the whole folder, not just the handful of new videos.
        duration, title = known.get(existing.name, (0.0, ""))
        if not duration:
            duration = probe_duration(existing, options.ffmpeg)
        return finish(Result(
            item.row_number, item.url, "skipped", str(existing), "already present",
            title=title, size=file_size(existing), duration=duration,
        ))

    if stem:
        stem = unique_stem(stem, taken, taken_lock)
    events.row_started(item)
    events.log(f"[{item.row_number}] {item.url}")
    last_error = ""

    for attempt in range(1, options.retries + 1):
        try:
            if is_direct_file(item.url):
                path, title, duration = download_direct(item, stem, options, events, cancel)
            else:
                path, title, duration = download_with_ytdlp(item, stem, options, events, cancel)
            size = file_size(path)
            if not duration:  # direct files, and extractors that report none
                duration = probe_duration(path, options.ffmpeg)
            measured = human_size(size)
            if duration:
                measured += f", {format_duration(duration)}"
            events.log(f"[{item.row_number}] done: {path.name} ({measured})")
            return finish(Result(
                item.row_number, item.url, "downloaded", str(path), title=title, size=size,
                duration=duration,
            ))
        except Cancelled:
            events.log(f"[{item.row_number}] stopped")
            return finish(Result(
                item.row_number, item.url, "cancelled", detail="stopped by the user",
            ))
        except Exception as error:
            last_error = str(error).strip() or type(error).__name__
            if is_permanent(last_error) or attempt == options.retries:
                break
            delay = 2 ** attempt
            events.log(f"[{item.row_number}] attempt {attempt} failed; retrying in {delay}s")
            try:
                sleep_unless_cancelled(delay, cancel)
            except Cancelled:
                events.log(f"[{item.row_number}] stopped")
                return finish(Result(
                    item.row_number, item.url, "cancelled", detail="stopped by the user",
                ))

    events.log(f"[{item.row_number}] FAILED: {last_error[:200]}")
    return finish(Result(item.row_number, item.url, "failed", detail=last_error))


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


DOWNLOADED = ("downloaded",)
ON_DISK = ("downloaded", "skipped")


def total_size(results: list[Result], statuses: tuple[str, ...] = DOWNLOADED) -> int:
    """Bytes across the rows with one of `statuses`.

    DOWNLOADED is what this run fetched; ON_DISK is everything the sheet asks
    for that is now in the folder, this run's work plus earlier runs'.
    """
    total = 0
    for result in results:
        if result.status in statuses and result.path:
            total += result.size or file_size(Path(result.path))
    return total


def total_runtime(results: list[Result], statuses: tuple[str, ...] = DOWNLOADED) -> float:
    """Seconds of video across those same rows, unknown runtimes aside."""
    return sum(result.duration for result in results if result.status in statuses)


def write_summary(
    path: Path,
    results: list[Result],
    counts: dict[str, int],
    started_at: datetime,
    elapsed: float,
    header_lines: list[str],
) -> None:
    """Append a short human-readable summary of this job to report.txt."""
    finished_at = started_at + timedelta(seconds=elapsed)
    failures = [result for result in results if result.status == "failed"]

    def runtime_text(seconds: float, videos: int) -> str:
        if seconds:
            return format_duration(seconds)
        if videos:
            return "not available (no ffmpeg, or the site reports none)"
        return "0s"

    on_disk = counts["downloaded"] + counts["skipped"]

    lines = [
        "=" * 68,
        f"Run started : {started_at:%Y-%m-%d %H:%M:%S}",
        f"Run finished: {finished_at:%Y-%m-%d %H:%M:%S}",
        f"Run took    : {format_duration(elapsed)}",
        "=" * 68,
        *header_lines,
        "",
        f"Videos in list  : {len(results)}",
        f"Downloaded      : {counts['downloaded']}",
        f"Skipped         : {counts['skipped']} (already present)",
        f"Failed          : {counts['failed']}",
        "",
        "This run",
        f"  Size          : {human_size(total_size(results))}",
        f"  Video runtime : {runtime_text(total_runtime(results), counts['downloaded'])}",
        "",
        "In the folder, across every run",
        f"  Videos        : {on_disk} of {len(results)}",
        f"  Size          : {human_size(total_size(results, ON_DISK))}",
        f"  Video runtime : "
        f"{runtime_text(total_runtime(results, ON_DISK), on_disk)}",
    ]
    if counts.get("cancelled"):
        lines.append(f"Cancelled       : {counts['cancelled']} (run stopped early)")

    if failures:
        lines += ["", "Failed rows:"]
        for failure in failures[:20]:
            reason = " ".join(failure.detail.split())[:110] or "unknown error"
            lines.append(f"  row {failure.row_number}: {failure.url}")
            lines.append(f"          -> {reason}")
        if len(failures) > 20:
            lines.append(f"  ... and {len(failures) - 20} more (see _download_report.csv)")

    lines.append("")
    block = "\n".join(lines) + "\n"
    # Append so several runs on the same date each keep their own record.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block)


# --------------------------------------------------------------------------- #
# Putting a job together
# --------------------------------------------------------------------------- #

# "Deliberately no name column", so yt-dlp falls back to the video's own title.
# A header read from a sheet can never be a bare NUL byte, so this cannot clash.
NO_COLUMN = "\x00"


@dataclass
class JobSpec:
    """Everything the user chose - on the command line or in the window."""
    excel: Path
    sheet: str | int = 0
    url_column: str | None = None       # None: work it out from the sheet
    name_column: str | None = None      # None: work it out; NO_COLUMN: none at all
    output: Path = Path("output")
    quality: str = "1080"
    workers: int = 3
    retries: int = 3
    timeout: int = 60
    h264: bool = False
    playlists: bool = False
    cookies_from_browser: str | None = None
    cookies_file: Path | None = None
    limit: int | None = None
    verbose: bool = False


@dataclass
class Scan:
    """What the spreadsheet turned out to hold."""
    columns: list[str]
    url_column: str
    name_column: str | None
    rows: list[VideoRow]
    headerless: bool


@dataclass
class PreparedJob:
    """A scanned sheet plus a created output folder: ready to run."""
    spec: JobSpec
    scan: Scan
    options: Options
    workers: int

    @property
    def rows(self) -> list[VideoRow]:
        return self.scan.rows

    @property
    def folder(self) -> Path:
        return self.options.output


@dataclass
class Summary:
    results: list[Result]
    counts: dict[str, int]
    started_at: datetime
    elapsed: float
    folder: Path
    report_csv: Path
    report_txt: Path
    total_bytes: int
    total_duration: float = 0.0  # seconds of video downloaded, 0 where unknown
    # The same two totals over everything the sheet asks for that is now in the
    # folder - this run's downloads plus the files earlier runs left behind.
    folder_bytes: int = 0
    folder_duration: float = 0.0

    @property
    def cancelled(self) -> bool:
        return bool(self.counts.get("cancelled"))


def sheet_names(path: Path) -> list[str]:
    """The sheets in a workbook. A .csv has none, and neither has a bad file."""
    if path.suffix.lower() == ".csv":
        return []
    try:
        with pd.ExcelFile(path) as book:
            return [str(name) for name in book.sheet_names]
    except Exception:
        return []


def scan_source(spec: JobSpec) -> Scan:
    """Read the sheet and settle which columns hold the links and the names.

    Raises JobError for anything the user can fix - a missing file, a column
    that is not there, a sheet with no links in it.
    """
    if not spec.excel.exists():
        raise JobError(f"Input file not found: {spec.excel}")

    sheet: str | int = spec.sheet
    # "--sheet 2" means the third sheet, but a sheet genuinely named "2" wins.
    if isinstance(sheet, str) and sheet.isdigit() and sheet not in sheet_names(spec.excel):
        sheet = int(sheet)
    try:
        frame, headerless = read_table(spec.excel, sheet)
    except Exception as error:
        raise JobError(f"Could not read {spec.excel.name}: {error}") from error

    frame.columns = [str(column).strip() for column in frame.columns]
    columns = list(frame.columns)
    hyperlinks = read_hyperlinks(spec.excel, sheet, headerless)

    url_column = spec.url_column or pick_url_column(frame, hyperlinks)
    if url_column not in columns:
        raise JobError(f"Column {url_column!r} is not in the sheet. Columns: {columns}")

    if spec.name_column == NO_COLUMN:
        name_column = None
    else:
        name_column = spec.name_column or pick_name_column(frame, url_column)
    if name_column and name_column not in columns:
        raise JobError(f"Column {name_column!r} is not in the sheet. Columns: {columns}")

    rows = extract_rows(frame, url_column, name_column, hyperlinks)
    if spec.limit:
        rows = rows[: spec.limit]
    if not rows:
        raise JobError(f"No links found in column {url_column!r}.")
    return Scan(columns, url_column, name_column, rows, headerless)


def prepare_job(spec: JobSpec, scan: Scan | None = None) -> PreparedJob:
    """Turn a spec into something runnable: folder created, ffmpeg located.

    Pass `scan` to reuse a scan the caller already did - the window scans as
    soon as a file is picked, so the sheet is not read twice.
    """
    scan = scan or scan_source(spec)

    folder = spec.output
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise JobError(f"Cannot create the output folder {folder}: {error}") from error

    options = Options(
        output=folder,
        quality=str(spec.quality),
        retries=max(1, spec.retries),
        timeout=spec.timeout,
        verbose=spec.verbose,
        ffmpeg=find_ffmpeg(),
        h264_only=spec.h264,
        cookies_from_browser=spec.cookies_from_browser,
        cookies_file=spec.cookies_file,
        playlists=spec.playlists,
    )
    return PreparedJob(
        spec=spec,
        scan=scan,
        options=options,
        workers=1 if spec.verbose else max(1, spec.workers),
    )


def job_header_lines(job: PreparedJob) -> list[str]:
    """The settings block shared by the console output and report.txt."""
    return [
        f"Input       : {job.spec.excel}  (sheet {job.spec.sheet})",
        f"Output      : {job.folder.resolve()}",
        f"URL column  : {job.scan.url_column}",
        f"Name column : {job.scan.name_column or '(derived from the URL)'}",
        f"Quality     : {job.options.quality}    Workers: {job.workers}",
    ]


def run_job(
    job: PreparedJob,
    events: Events | None = None,
    cancel: threading.Event | None = None,
) -> Summary:
    """Download every row, write both reports, and hand back the totals.

    `cancel` stops the run: rows in flight unwind at their next chunk, queued
    rows return immediately, and every row still lands in the report with an
    honest status.
    """
    events = events or Events()
    started_at = datetime.now()
    start_clock = time.monotonic()

    taken: set[str] = set()
    taken_lock = threading.Lock()
    results: list[Result] = []
    known = read_previous_report(job.folder)

    with ThreadPoolExecutor(max_workers=job.workers) as pool:
        futures = {
            pool.submit(download_one, row, job.options, taken, taken_lock,
                        events, cancel, known): row
            for row in job.rows
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                results.append(future.result())
            except Exception as error:  # a crash in one row must not lose the rest
                broken = Result(row.row_number, row.url, "failed",
                                detail=f"internal error: {error}")
                events.log(f"[{row.row_number}] FAILED: internal error: {error}")
                events.row_finished(broken)
                results.append(broken)

    results.sort(key=lambda result: result.row_number)
    counts = {status: sum(1 for result in results if result.status == status)
              for status in ("downloaded", "skipped", "failed", "cancelled")}
    elapsed = time.monotonic() - start_clock

    report_csv = job.folder / REPORT_CSV
    try:
        with report_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["row", "url", "status", "file", "title",
                             "runtime", "runtime_seconds", "size", "size_bytes", "detail"])
            for result in results:
                writer.writerow([
                    result.row_number, result.url, result.status, result.path, result.title,
                    format_duration(result.duration) if result.duration else "",
                    int(round(result.duration)) if result.duration else "",
                    human_size(result.size) if result.size else "",
                    result.size or "",
                    result.detail,
                ])
    except OSError as error:  # e.g. the previous report is still open in Excel
        events.log(f"Could not write {report_csv.name}: {error}")

    report_txt = job.folder / REPORT_TXT
    try:
        write_summary(report_txt, results, counts, started_at, elapsed,
                      header_lines=job_header_lines(job))
    except OSError as error:
        events.log(f"Could not write {report_txt.name}: {error}")

    return Summary(
        results=results,
        counts=counts,
        started_at=started_at,
        elapsed=elapsed,
        folder=job.folder,
        report_csv=report_csv,
        report_txt=report_txt,
        total_bytes=total_size(results),
        total_duration=total_runtime(results),
        folder_bytes=total_size(results, ON_DISK),
        folder_duration=total_runtime(results, ON_DISK),
    )
