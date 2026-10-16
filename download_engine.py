"""Download videos into one output folder: the links in an Excel file, a whole
channel, or the results of keyword searches.

Every run saves into the same folder, so rerunning the same sheet picks up
where the last run left off: videos already there are skipped, and only what
is new gets fetched.

YouTube (and other video sites) are handled by yt-dlp; plain links to .mp4 and
friends are streamed directly over HTTP. Everything is saved as .mp4.

With reuse_only, a video is only saved when its own license allows reuse (CC BY,
CC BY-SA, CC0); yt-dlp reads the license with the video's details, so the check
costs no extra request. Each saved video gets an .attribution.txt next to it.

This is the engine behind the Video Downloader (see downloader.py):
scan_source(), prepare_job() and run_job() do the work, and an Events object
receives the progress the page draws.
"""

from __future__ import annotations

import csv
import mimetypes
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import pandas as pd
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError

from common import find_ffmpeg, length_breakdown, probe_duration

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv",
    ".mpg", ".mpeg", ".3gp", ".ts", ".ogv",
}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".opus", ".ogg", ".oga", ".flac", ".weba"}
# "Audio only" saves one of these, converted by ffmpeg where the site serves another.
AUDIO_FORMATS = ("m4a", "mp3", "wav")
# Audio is fetched and converted in here, then moved up into the folder. Fetching
# straight into the folder could hit a video of the same name from a video run,
# and yt-dlp deletes what it took the audio from.
AUDIO_WORK = "_audio_parts"
# HEVC (H.265): TikTok's default, and something the app's player (like plenty of
# editors) can only show where Windows has an HEVC decoder. "?" lets formats
# whose codec the site doesn't name through.
NOT_HEVC = "".join(f"[vcodec!^=?{codec}]" for codec in ("hvc1", "hev1", "h265", "hevc"))

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# yt-dlp names each unmerged stream "<title>.f<format id>.<ext>".
FORMAT_INTERMEDIATE_RE = re.compile(r"\.f\d+\.[^.]+$", re.IGNORECASE)
NAME_COLUMN_HINTS = ("name", "title", "filename", "file_name", "video", "id", "slug")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")  # yt-dlp colours "ERROR:" for a terminal

# yt-dlp failures that will never succeed on a retry.
PERMANENT_ERRORS = (
    "video unavailable", "is unavailable", "private video", "members-only",
    "removed by the uploader", "is not available", "has been removed",
    "account associated with this video has been terminated",
    "sign in to confirm your age", "unsupported url", "no video formats found",
    "incomplete youtube id", "copyright", "404: not found", "404 client error", "410 client error",
)

# YouTube saying "slow down": worth retrying, but only after a real pause. A run
# over a whole channel makes hundreds of requests and can run into these.
RATE_LIMITED = ("not a bot", "429", "too many requests")
RATE_LIMIT_PAUSE = 30  # seconds, times the attempt number
# Failures a signed-in browser's cookies can fix (Instagram and Facebook often want a login).
LOGIN_NEEDED = ("login", "log in", "logged-in", "logged in", "sign in", "cookies", "private")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_SIZE = 1 << 20  # 1 MiB

REPORT_CSV = "_download_report.csv"
REPORT_TXT = "report.txt"
# Every video the folder holds, kept across runs: length, size, license, channel.
# The report only describes the last run; this is what the dataset overview reads.
DATASET_INDEX = "_dataset.json"

# YouTube's own search filter "Features > Creative Commons" (the `sp` parameter
# its results page uses). It only narrows the search: every video's license is
# still checked before it is saved.
YOUTUBE_CC_FILTER = "EgIwAQ=="
# Channel tabs that hold videos, by their URL path.
CHANNEL_TABS = {"videos": "Videos", "streams": "Live", "shorts": "Shorts"}
YOUTUBE_LICENSE = "Creative Commons Attribution license (reuse allowed)"
# A TikTok profile link, and the profile id every TikTok video names ("secUid").
TIKTOK_PROFILE_RE = re.compile(r"tiktok\.com/@[\w.-]+/?(?:$|[?#])", re.IGNORECASE)
TIKTOK_PROFILE_ID_RE = re.compile(r"MS4wLjABAAAA[\w-]{64}")


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
    # Audio leaves "Deleting original file ... (pass -k to keep)": the video it took
    # the sound from, in the work folder, with advice for yt-dlp's command line.
    NOISE = ("[debug] ", "[download] ", "Deleting original file ")

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
    title: str = ""  # from a channel or search listing; only shown, never used as the file name


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
# Licenses
# --------------------------------------------------------------------------- #

def reuse_allowed(license_text: str | None) -> bool:
    """True when a video's license allows reuse with edits and commercial use:
    CC BY, CC BY-SA, or CC0 / public domain.

    YouTube has two licenses, and its Creative Commons one is CC BY. Other sites
    write the license as a name ("Attribution - Share Alike"), a code ("by-nc")
    or a creativecommons.org link. Non-commercial (NC) and no-derivatives (ND)
    licenses rule out a commercial dataset of clips, and no license at all means
    all rights reserved.
    """
    text = " ".join((license_text or "").lower().split())
    if not text or "rights reserved" in text:
        return False
    if "reuse allowed" in text or "cc0" in text or "public domain" in text or "publicdomain" in text:
        return True
    words = set(re.findall(r"[a-z]+", text))
    if words & {"nc", "nd", "noncommercial", "noderivs", "noderivatives"} \
            or "non-commercial" in text or "non commercial" in text or "no deriv" in text:
        return False
    creative_commons = ("creative commons" in text or "creativecommons" in text or "cc" in words
                        or text.startswith("attribution") or words <= {"by", "sa"})
    return creative_commons and ("by" in words or "attribution" in text)


def license_label(license_text: str) -> str:
    """The license as a credit line names it."""
    return "CC BY" if license_text == YOUTUBE_LICENSE else license_text


def write_attribution(path: Path, title: str, channel: str, page_url: str, license_text: str) -> None:
    """Save the credit the license asks for next to the video, as <video>.attribution.txt."""
    credit = f'"{title}" by {channel or "unknown"} is licensed under {license_label(license_text)}. Source: {page_url}'
    lines = [
        f"Title: {title}",
        f"Channel: {channel}",
        f"Source: {page_url}",
        f"License: {license_text}",
    ]
    if license_text == YOUTUBE_LICENSE:
        lines.append("License reference: https://support.google.com/youtube/answer/2797468")
    lines += ["", "Suggested attribution line:", credit, ""]
    path.with_name(f"{path.stem}.attribution.txt").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Channels and searches
# --------------------------------------------------------------------------- #

def youtube_channel_base(url: str) -> str | None:
    """https://www.youtube.com/@name (or /channel/…, /c/…, /user/…) without its
    tab, or None when the link isn't a YouTube channel."""
    url = url.strip()
    if url.startswith("@"):
        url = f"https://www.youtube.com/{url}"
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host != "youtube.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].startswith("@"):
        return f"https://www.youtube.com/{parts[0]}"
    if len(parts) > 1 and parts[0] in ("channel", "c", "user"):
        return f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    return None


def search_url(query: str, reuse_only: bool) -> str:
    """A YouTube search, with its Creative Commons filter when only reusable videos are wanted."""
    params = {"search_query": query}
    if reuse_only:
        params["sp"] = YOUTUBE_CC_FILTER
    return f"https://www.youtube.com/results?{urlencode(params)}"


def listing_error(error: Exception) -> str:
    """yt-dlp's error without its "ERROR:" prefix and the "please report this issue…" advice."""
    message = ANSI_COLOUR.sub("", str(error)).replace("ERROR: ", "")
    return re.split(r";?\s*please report this issue", message, flags=re.IGNORECASE)[0].strip()


class QuietLogger:
    """Keeps yt-dlp's listing chatter (and its warnings) off the console."""

    def debug(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def list_entries(url: str, limit: int | None, spec: JobSpec) -> tuple[list[dict], dict]:
    """The videos on a channel tab, search or playlist page, without opening each one.

    Returns (entries, page info). A link to a single video comes back as itself.
    """
    settings = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": limit or None,
        "socket_timeout": spec.timeout,
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
    }
    if spec.cookies_from_browser:
        settings["cookiesfrombrowser"] = (spec.cookies_from_browser,)
    if spec.cookies_file:
        settings["cookiefile"] = str(spec.cookies_file)
    with YoutubeDL(settings) as listing:
        info = listing.extract_info(url, download=False)
    if info.get("_type") != "playlist":
        return [info], info
    return [entry for entry in (info.get("entries") or []) if entry], info


def entry_url(entry: dict) -> str:
    # A listed entry's "url" is its page; a fully read video's "url" is the media stream.
    if entry.get("_type") in ("url", "url_transparent"):
        url = entry.get("url") or ""
    else:
        url = entry.get("webpage_url") or entry.get("url") or ""
    if not url.startswith("http") and YOUTUBE_ID_RE.match(entry.get("id") or ""):
        url = f"https://www.youtube.com/watch?v={entry['id']}"
    return url


def add_entries(rows: list[VideoRow], seen: set[str], entries: list[dict], videos_only: bool,
                limit: int | None = None) -> None:
    """Append listed videos as rows, once each, in listing order (at most `limit` of them)."""
    added = 0
    for entry in entries:
        if limit is not None and added >= limit:
            break
        url = entry_url(entry)
        ident = video_id(url) or url
        # A search also lists channels and playlists; only videos are wanted.
        if not url or ident in seen or (videos_only and not video_id(url)):
            continue
        seen.add(ident)
        rows.append(VideoRow(len(rows) + 1, url, None, title=entry.get("title") or ""))
        added += 1


def tiktok_profile(url: str, spec: JobSpec) -> tuple[str, str, str] | None:
    """The profile a TikTok video was posted on: ("tiktokuser:<id>", @handle, name).

    yt-dlp can no longer read TikTok's profile pages (TikTok blocks them), but
    every video names its profile's id, and a profile can still be listed by id.
    None for anything that isn't a TikTok video, or a video that can't be read.
    """
    if not is_tiktok(url) or TIKTOK_PROFILE_RE.search(url):
        return None
    settings = {"skip_download": True, "socket_timeout": spec.timeout, "quiet": True,
                "no_warnings": True, "logger": QuietLogger()}
    if spec.cookies_from_browser:
        settings["cookiesfrombrowser"] = (spec.cookies_from_browser,)
    if spec.cookies_file:
        settings["cookiefile"] = str(spec.cookies_file)
    try:
        with YoutubeDL(settings) as reader:
            info = reader.extract_info(url, download=False)
    except DownloadError:
        return None
    profile = info.get("channel_id") or ""
    if not TIKTOK_PROFILE_ID_RE.fullmatch(profile):
        return None
    handle = info.get("uploader") or ""
    return f"tiktokuser:{profile}", handle, info.get("channel") or handle


def list_channel(spec: JobSpec) -> Scan:
    """Every video on a channel, newest first: its Videos tab, plus Live and Shorts if asked.

    A link that isn't a YouTube channel (a playlist, or a channel on another
    site) is listed as it is - except a TikTok video, which stands for its whole profile.
    """
    base = youtube_channel_base(spec.channel)
    link, profile = spec.channel.strip(), None
    if not base:
        profile = tiktok_profile(link, spec)
        if profile:
            link = profile[0]
    pages = [(f"{base}/{tab}", CHANNEL_TABS[tab]) for tab in spec.channel_tabs] if base else [(link, "")]
    rows: list[VideoRow] = []
    seen: set[str] = set()
    notes: list[str] = []
    label = ""
    for url, tab in pages:
        remaining = spec.limit - len(rows) if spec.limit else None
        if remaining is not None and remaining <= 0:
            break
        try:
            entries, info = list_entries(url, remaining, spec)
        except DownloadError as error:
            message = listing_error(error)
            if tab and "does not have" in message.lower():  # e.g. a channel that never streamed
                notes.append(f"no {tab} tab")
                continue
            if is_tiktok(url) and "secondary user id" in message.lower():
                raise JobError(
                    "TikTok doesn't let profile pages be read. Paste the link of any one video from "
                    "this profile instead: the whole profile is listed from it.") from error
            hint = "" if base else (
                " Some sites' profiles can't be listed (Instagram, Facebook, X): "
                "paste the links of the posts under Links instead.")
            raise JobError(f"Couldn't list {url}: {message.rstrip('.')}.{hint}") from error
        if profile:
            # Listed by id, the videos' links read "@<id>/video/..."; put the @handle back.
            label = profile[2]
            for entry in entries:
                if entry.get("url") and profile[1]:
                    entry["url"] = entry["url"].replace(f"/@{link.removeprefix('tiktokuser:')}/", f"/@{profile[1]}/")
        label = label or info.get("channel") or info.get("uploader") or (info.get("title") or "").removesuffix(f" - {tab}")
        before = len(rows)
        add_entries(rows, seen, entries, videos_only=bool(base))
        if len(pages) > 1:  # say how the videos split over the tabs
            notes.append(f"{len(rows) - before} {tab.lower()}")
    if not rows:
        raise JobError("No videos found on this channel." if base else "No videos found at this link.")
    return Scan([], "", None, rows, False, label=label, notes=notes)


def list_search(spec: JobSpec) -> Scan:
    """The top results of each keyword search, each video once."""
    rows: list[VideoRow] = []
    seen: set[str] = set()
    notes: list[str] = []
    for query in spec.queries:
        # Results also hold channels, playlists and Shorts shelves, which are left out:
        # ask for some extra so the search still gives `per_query` videos.
        extra = max(10, spec.per_query // 2)
        try:
            entries, _ = list_entries(search_url(query, spec.reuse_only), spec.per_query + extra, spec)
        except DownloadError as error:
            raise JobError(f"The search for {query!r} failed: {listing_error(error)}") from error
        before = len(rows)
        add_entries(rows, seen, entries, videos_only=True, limit=spec.per_query)
        notes.append(f"{len(rows) - before} for “{query}”")
    if spec.limit:
        rows = rows[: spec.limit]
    if not rows:
        raise JobError("The searches found no videos." + (
            " With Only reuse-allowed videos on, YouTube's Creative Commons filter is applied: "
            "try broader keywords." if spec.reuse_only else ""))
    return Scan([], "", None, rows, False, label=", ".join(spec.queries), notes=notes)


def list_links(spec: JobSpec) -> Scan:
    """Pasted links, one per line, each video once: any site yt-dlp supports, or a direct file link.

    Nothing goes online here; a link is only opened when it is downloaded.
    """
    rows: list[VideoRow] = []
    seen: set[str] = set()
    skipped = 0
    for line in spec.links:
        url = cell_to_url(line)
        if not url:
            skipped += 1
            continue
        ident = video_id(url) or url
        if ident not in seen:
            seen.add(ident)
            rows.append(VideoRow(len(rows) + 1, url, None))
    if spec.limit:
        rows = rows[: spec.limit]
    if not rows:
        raise JobError("Paste at least one video link, one per line.")
    notes = [f"{skipped} line{'s' if skipped != 1 else ''} without a link left out"] if skipped else []
    return Scan([], "", None, rows, False, notes=notes)


# --------------------------------------------------------------------------- #
# Filenames
# --------------------------------------------------------------------------- #

def sanitize(name: str, fallback: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120].strip() or fallback


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


# The id yt-dlp gives a video on a few other sites, where the link shows it. File
# names carry it ("Title [id].mp4"), which is how a rerun knows what it already has.
SITE_IDS = (
    (("tiktok.com",), re.compile(r"/(?:video|photo)/(\d+)")),
    (("instagram.com",), re.compile(r"/(?:p|reels?|tv)/([\w-]+)")),
    (("vimeo.com",), re.compile(r"^/(?:.*/)?(\d{5,})(?:/|$)")),
)


def file_id(url: str) -> str:
    """The id a download of this link is named with: YouTube's, or one of SITE_IDS'. "" if unknown."""
    ident = video_id(url)
    if ident:
        return ident
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for hosts, pattern in SITE_IDS:
        if host.endswith(hosts):
            found = pattern.search(parsed.path)
            return found[1] if found else ""
    return ""


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


def existing_by_id(folder: Path, ident: str, audio: str = "") -> Path | None:
    """Find a previous download tagged with this video id, e.g. "Title [abc123].mp4"."""
    for path in sorted(folder.glob(f"*[[]{ident}].*")):
        if is_finished(path, audio):
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


def is_finished(path: Path, audio: str = "") -> bool:
    """A finished download of the kind the run saves: a video, or audio in the `audio` format."""
    if not audio:
        return is_finished_video(path)
    return path.is_file() and path.suffix.lower() == f".{audio}" and not FORMAT_INTERMEDIATE_RE.search(path.name)


def is_finished_audio(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and not FORMAT_INTERMEDIATE_RE.search(path.name)
    )


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


def existing_download(folder: Path, stem: str, audio: str = "") -> Path | None:
    for path in sorted(folder.glob(f"{glob_escape(stem)}.*")):
        if is_finished(path, audio):
            return path
    return None


def read_previous_report(folder: Path) -> dict[str, tuple[float, str, str | None, str]]:
    """What earlier runs measured, keyed by file name -> (runtime, title, license, channel).

    A rerun skips everything already on disk, and a skipped video still belongs
    in the totals. Reading the last report back is how those totals survive:
    the alternative is probing every file in the folder on every run.
    """
    report = folder / REPORT_CSV
    if not report.is_file():
        return {}
    known: dict[str, tuple[float, str, str | None, str]] = {}
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
                # license None: a report from before the license column (not the same as "none given").
                known[name] = (max(0.0, seconds), record.get("title") or "",
                               record.get("license"), record.get("channel") or "")
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
    reuse_only: bool = False  # save only videos whose license allows reuse
    audio: str = ""  # "": save the video; else save only its sound, in this format (AUDIO_FORMATS)


@dataclass
class Result:
    row_number: int
    url: str
    status: str  # downloaded | skipped | not_allowed | failed | cancelled
    path: str = ""
    detail: str = ""
    title: str = ""
    size: int = 0
    duration: float = 0.0  # how long the video plays for, in seconds (0 = unknown)
    license: str = ""
    channel: str = ""


@dataclass
class Fetched:
    """What a download produced, and what the site said about the video."""
    path: Path
    title: str = ""
    duration: float = 0.0
    license: str = ""
    channel: str = ""
    page_url: str = ""


class NotAllowed(Exception):
    """The video's license doesn't allow reuse, so it was not downloaded."""

    def __init__(self, license_text: str, title: str = ""):
        super().__init__(license_text)
        self.license = license_text
        self.title = title


def is_direct_file(url: str) -> bool:
    return Path(unquote(urlparse(url).path)).suffix.lower() in VIDEO_EXTENSIONS


def is_tiktok(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(("tiktok.com", "douyin.com"))


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


# probe_duration (in common) measures what yt-dlp can't describe: plain file URLs,
# and the occasional extractor that reports no duration.


def format_selector(have_ffmpeg: bool, h264_only: bool = False) -> str:
    """Select mp4 video plus m4a audio. How large a picture is quality_sort's part.

    Without ffmpeg nothing can be merged, so only already-combined formats are
    offered; on YouTube that generally caps out at 360p.
    """
    if not have_ffmpeg:
        return f"best[ext=mp4]{NOT_HEVC}/best[ext=mp4]/best"

    chain = []
    if h264_only:
        # avc1 plays everywhere; YouTube otherwise often serves av01 inside mp4.
        # TikTok and the like serve video and sound in one file, its codec named "h264".
        chain += [
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]",
            "best[ext=mp4][vcodec^=avc1]",
            "best[ext=mp4][vcodec^=h264]",
        ]
    # Anything but HEVC first, even at a lower resolution (one TikTok video's H.264
    # is 576p where its HEVC is 720p): a video the player can't show is no use here.
    for codecs in (NOT_HEVC, ""):
        chain += [
            f"bestvideo[ext=mp4]{codecs}+bestaudio[ext=m4a]",
            f"bestvideo{codecs}+bestaudio",
            f"best[ext=mp4]{codecs}",
            f"best{codecs}",
        ]
    return "/".join(chain)


def quality_sort(quality: str) -> list[str]:
    """yt-dlp's format order for a quality ('best' or a number of lines, e.g. '1080').

    The largest picture up to it, else the smallest above it. yt-dlp's "res" is
    the short side, so a vertical 1080x1920 video (TikTok, Shorts) is 1080p; a
    height limit would have ruled it out, and taken 576x1024 instead.
    """
    return [] if quality in ("best", "") else [f"res:{quality.rstrip('p')}"]


def audio_selector(url: str, audio: str) -> str:
    """The best audio-only stream; failing that, a whole video to take the sound from.

    TikTok's audio-only format is the post's sound - often the whole song a video
    borrowed, not what was said in it - so there the sound comes out of the video.
    """
    if is_tiktok(url):
        return "best"
    # An m4a stream is saved as it is; any other target is re-encoded, so take the best stream.
    return ("bestaudio[ext=m4a]/" if audio == "m4a" else "") + "bestaudio/best"


def ytdlp_options(
    options: Options,
    stem: str | None,
    row_number: int,
    events: Events,
    cancel: threading.Event | None,
    url: str = "",
) -> dict:
    have_ffmpeg = options.ffmpeg is not None
    # No name in the sheet: let yt-dlp use the video's own title, tagged with the
    # id so a repeat run can still recognise the file. The title is cut at 100
    # characters: a TikTok or Instagram caption can run to thousands, and Windows
    # paths can't. (YouTube titles are never longer, so their names are unchanged.)
    name_template = f"{stem}.%(ext)s" if stem else "%(title).100s [%(id)s].%(ext)s"
    folder = options.output / AUDIO_WORK if options.audio else options.output
    settings = {
        "outtmpl": {"default": str(folder / name_template)},
        "format": (audio_selector(url, options.audio) if options.audio
                   else format_selector(have_ffmpeg, options.h264_only)),
        "format_sort": [] if options.audio else quality_sort(options.quality),
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
        # Pin the app's own binary so a broken copy earlier on PATH cannot win.
        settings["ffmpeg_location"] = options.ffmpeg
    if options.audio:
        # Pulls the sound out of whatever came down (and deletes that); a stream
        # already in the format asked for is kept as it is, not re-encoded.
        extract = {"key": "FFmpegExtractAudio", "preferredcodec": options.audio}
        if options.audio != "wav":
            extract["preferredquality"] = "192"  # kbps, where it has to be encoded
        settings["postprocessors"] = [extract]
    elif have_ffmpeg:
        # Guarantee an .mp4 container even when the source is webm.
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
) -> Fetched:
    """Download one link. Raises NotAllowed when reuse_only rules the video out."""
    settings = ytdlp_options(options, stem, item.row_number, events, cancel, item.url)
    refused: dict = {}
    if options.reuse_only:
        # yt-dlp asks this with the video's details, before downloading anything.
        def license_filter(info: dict, *, incomplete: bool = False) -> str | None:
            if incomplete or reuse_allowed(info.get("license")):
                return None
            refused.update(license=info.get("license") or "", title=info.get("title") or "")
            return "its license does not allow reuse"

        settings["match_filter"] = license_filter
    with YoutubeDL(settings) as downloader:
        info = downloader.extract_info(item.url, download=True)
        if refused:
            raise NotAllowed(refused["license"], refused["title"])
        if info.get("_type") == "playlist":  # --playlists: keep the first entry's file
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise DownloadError("playlist contained no downloadable videos")
            info = entries[0]
        try:  # most extractors give a duration; a live stream gives None
            duration = max(0.0, float(info.get("duration") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        path = downloaded_path(downloader, info)
        if options.audio:  # out of the work folder, now it is finished
            path = path.replace(options.output / path.name)
        return Fetched(
            path, info.get("title") or "", duration,
            license=info.get("license") or "", channel=info.get("channel") or info.get("uploader") or "",
            page_url=info.get("webpage_url") or item.url,
        )


def download_direct(
    item: VideoRow,
    stem: str,
    options: Options,
    events: Events,
    cancel: threading.Event | None = None,
) -> Fetched:
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
        return Fetched(destination, page_url=item.url)
    finally:
        session.close()


def is_permanent(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in PERMANENT_ERRORS)


def is_rate_limited(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in RATE_LIMITED)


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
    known: dict[str, tuple[float, str, str | None, str]] | None = None,
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
    ident = file_id(item.url)
    if stem:
        existing = existing_download(options.output, stem, options.audio)
    elif ident:
        existing = existing_by_id(options.output, ident, options.audio)
    else:
        existing = None

    if existing:
        events.log(f"[{item.row_number}] skip, already downloaded: {existing.name}")
        if stem:
            with taken_lock:
                taken.add(stem.lower())
        # Carry the file's runtime, title and license forward so a rerun's report
        # still describes the whole folder, not just the handful of new videos.
        duration, title, license_text, channel = known.get(existing.name, (0.0, "", "", ""))
        if not duration:
            duration = probe_duration(existing, options.ffmpeg)
        return finish(Result(
            item.row_number, item.url, "skipped", str(existing), "already present",
            title=title or item.title, size=file_size(existing), duration=duration,
            license=license_text or "", channel=channel,
        ))

    if options.reuse_only and is_direct_file(item.url):
        events.log(f"[{item.row_number}] not allowed: a direct file link has no license to check")
        return finish(Result(item.row_number, item.url, "not_allowed",
                             detail="no license information (direct file link)", title=item.title))

    if stem:
        stem = unique_stem(stem, taken, taken_lock)
    events.row_started(item)
    events.log(f"[{item.row_number}] {item.url}")
    last_error = ""

    for attempt in range(1, options.retries + 1):
        try:
            if is_direct_file(item.url) and not options.audio:  # for audio, yt-dlp fetches and converts it
                fetched = download_direct(item, stem, options, events, cancel)
            else:
                fetched = download_with_ytdlp(item, stem, options, events, cancel)
            path, duration = fetched.path, fetched.duration
            size = file_size(path)
            if not duration:  # direct files, and extractors that report none
                duration = probe_duration(path, options.ffmpeg)
            measured = human_size(size)
            if duration:
                measured += f", {format_duration(duration)}"
            events.log(f"[{item.row_number}] done: {path.name} ({measured})")
            if options.reuse_only:
                try:
                    write_attribution(path, fetched.title, fetched.channel, fetched.page_url, fetched.license)
                except OSError as error:
                    events.log(f"[{item.row_number}] couldn't write the attribution file: {error}")
            return finish(Result(
                item.row_number, item.url, "downloaded", str(path), title=fetched.title, size=size,
                duration=duration, license=fetched.license, channel=fetched.channel,
            ))
        except NotAllowed as refused:
            if refused.license:
                reason = f"license: {refused.license}"
            elif video_id(item.url):
                reason = "no license given (all rights reserved)"
            else:  # TikTok, Instagram, Facebook and most others never say
                reason = "the site gives no license, so reuse can't be confirmed"
            events.log(f"[{item.row_number}] not allowed, {reason}")
            return finish(Result(item.row_number, item.url, "not_allowed", detail=reason,
                                 title=refused.title or item.title, license=refused.license))
        except Cancelled:
            events.log(f"[{item.row_number}] stopped")
            return finish(Result(
                item.row_number, item.url, "cancelled", detail="stopped by the user",
            ))
        except Exception as error:
            last_error = ANSI_COLOUR.sub("", str(error)).strip() or type(error).__name__
            if is_permanent(last_error) or attempt == options.retries:
                break
            if is_rate_limited(last_error):
                delay = RATE_LIMIT_PAUSE * attempt
                events.log(f"[{item.row_number}] YouTube asked to slow down; waiting {delay}s. If this keeps "
                           "happening, set Advanced settings > Cookies from to a browser you're signed in to.")
            else:
                delay = 2 ** attempt
                events.log(f"[{item.row_number}] attempt {attempt} failed; retrying in {delay}s")
            try:
                sleep_unless_cancelled(delay, cancel)
            except Cancelled:
                events.log(f"[{item.row_number}] stopped")
                return finish(Result(
                    item.row_number, item.url, "cancelled", detail="stopped by the user",
                ))

    if any(word in last_error.lower() for word in LOGIN_NEEDED):
        last_error += " Set Advanced settings > Cookies from to a browser you're signed in to."
    if is_tiktok(item.url) and re.search(r"unsupported url: \S*tiktok\.com/?(\?|$)", last_error, re.IGNORECASE):
        last_error += " The link leads to TikTok's home page: the video was probably deleted or made private."
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
        *([f"Not allowed     : {counts['not_allowed']} (license doesn't allow reuse)"]
          if counts.get("not_allowed") else []),
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
# Dataset overview
# --------------------------------------------------------------------------- #

DOWNLOAD_LENGTHS = [(60, "Under 1 min"), (300, "1–5 min"), (1200, "5–20 min"), (3600, "20–60 min"),
                    (float("inf"), "Over 1 hour")]


def read_index(folder: Path) -> dict[str, dict]:
    try:
        return json.loads((folder / DATASET_INDEX).read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError, AttributeError):
        return {}


def write_index(folder: Path, files: dict[str, dict]) -> None:
    try:
        path = folder / DATASET_INDEX
        partial = path.with_name("~" + DATASET_INDEX)
        partial.write_text(json.dumps({"files": files}, indent=1, ensure_ascii=False), encoding="utf-8")
        partial.replace(path)
    except OSError:
        pass  # the overview just measures again next time


def update_index(folder: Path, results: list[Result]) -> None:
    """Record what this run knows about each video in the folder, keeping what earlier runs knew."""
    files = read_index(folder)
    for result in results:
        if result.status not in ON_DISK or not result.path:
            continue
        path = Path(result.path)
        entry = files.setdefault(path.name, {})
        entry.update(size=file_size(path), url=result.url)
        for key, value in (("duration", result.duration), ("title", result.title),
                           ("license", result.license), ("channel", result.channel)):
            if value:  # a rerun's "already there" row may know less than the run that downloaded it
                entry[key] = value
        if result.status == "downloaded":
            entry["license"] = result.license  # empty means checked: no license given
    write_index(folder, files)


def read_attribution(video: Path) -> dict[str, str]:
    """License and channel from a video's .attribution.txt, when it has one."""
    found = {}
    try:
        for line in video.with_name(f"{video.stem}.attribution.txt").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(": ")
            if key in ("License", "Channel", "Title") and value:
                found[key.lower()] = value.strip()
    except OSError:
        pass
    return found


def license_group(license_text: str | None) -> str:
    """The license as the overview groups it. None: nobody recorded it."""
    if license_text is None:
        return "Not recorded"
    text = license_text.lower()
    if not text:
        return "No license given"  # on YouTube: the standard license, all rights reserved
    if reuse_allowed(license_text):
        if "cc0" in text or "public" in text:
            return "CC0 / public domain"
        words = set(re.findall(r"[a-z]+", text))
        return "CC BY-SA" if "sa" in words or "share" in text else "CC BY"
    if "creative" in text or "cc" in text or "attribution" in text or re.search(r"\bby\b", text):
        return "Creative Commons NC / ND"
    return "Other license"


def folder_summary(folder: Path) -> dict:
    """A quick look at a download folder for the home screen: only what's recorded, nothing measured."""
    files = read_index(folder)
    report = read_previous_report(folder)
    videos = [path for path in folder.iterdir() if is_finished_video(path)]
    audio = [path for path in folder.iterdir() if is_finished_audio(path)]
    seconds = 0.0
    for path in videos + audio:
        seconds += files.get(path.name, {}).get("duration") or report.get(path.name, (0.0,))[0] or 0.0
    return {
        "videos": len(videos),
        "audio": len(audio),
        "bytes": sum(file_size(path) for path in videos + audio),
        "seconds": seconds,
        "updated": max((path.stat().st_mtime for path in videos + audio), default=folder.stat().st_mtime),
    }


def folder_overview(folder: Path, ffmpeg: str | None = None) -> dict:
    """Everything in a download folder, across every run: videos and audio, hours, size, licenses, channels.

    Lengths come from the dataset index or the last report; anything unknown is
    measured once with ffmpeg and remembered in the index.
    """
    if not folder.is_dir():
        raise JobError(f"The folder {folder} doesn't exist yet.")
    files = read_index(folder)
    report = read_previous_report(folder)
    measured = False
    videos = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if is_finished_video(path):
            kind = "Video"
        elif is_finished_audio(path):
            kind = "Audio"
        else:
            continue
        size = file_size(path)
        entry = files.get(path.name, {})
        reported = report.get(path.name)
        attribution = read_attribution(path)
        duration = entry.get("duration") if entry.get("size") == size else None
        if not duration and reported:
            duration = reported[0]
        if not duration:
            duration = probe_duration(path, ffmpeg)
            measured = True
            files.setdefault(path.name, {}).update(size=size, duration=duration)
        if "license" in entry:
            license_text = entry["license"]
        elif attribution.get("license"):
            license_text = attribution["license"]
        elif reported and reported[2] is not None:
            license_text = reported[2]  # "" here: checked, and the site gave none
        else:
            license_text = None
        channel = entry.get("channel") or attribution.get("channel") or (reported[3] if reported else "")
        videos.append({"duration": float(duration or 0), "size": size, "license": license_group(license_text),
                       "reuse": license_text is not None and reuse_allowed(license_text), "channel": channel,
                       "kind": kind})
    if measured:
        write_index(folder, files)

    def grouped(key: str) -> list[dict]:
        groups: dict[str, dict] = {}
        for video in videos:
            name = video[key] or "Unknown"
            group = groups.setdefault(name, {"name": name, "count": 0, "seconds": 0.0, "bytes": 0})
            group["count"] += 1
            group["seconds"] += video["duration"]
            group["bytes"] += video["size"]
            if key == "license":
                group["reuse"] = video["reuse"]
        return sorted(groups.values(), key=lambda g: (-g["seconds"], -g["count"], g["name"]))

    channels = [g for g in grouped("channel") if g["name"] != "Unknown"]
    seconds = sum(video["duration"] for video in videos)
    return {
        "folder": str(folder),
        "files": len(videos),  # videos plus audio files
        "videos": sum(1 for video in videos if video["kind"] == "Video"),
        "audio": sum(1 for video in videos if video["kind"] == "Audio"),
        "kinds": grouped("kind"),
        "seconds": seconds,
        "bytes": sum(video["size"] for video in videos),
        "average": seconds / len(videos) if videos else 0.0,
        "unmeasured": sum(1 for video in videos if not video["duration"]),
        "reuse_videos": sum(1 for video in videos if video["reuse"]),
        "reuse_seconds": sum(video["duration"] for video in videos if video["reuse"]),
        "licenses": grouped("license"),
        "channels": channels[:10],
        "channel_count": len(channels),
        "lengths": length_breakdown([video["duration"] for video in videos], DOWNLOAD_LENGTHS),
    }


# --------------------------------------------------------------------------- #
# Putting a job together
# --------------------------------------------------------------------------- #

# "Deliberately no name column", so yt-dlp falls back to the video's own title.
# A header read from a sheet can never be a bare NUL byte, so this cannot clash.
NO_COLUMN = "\x00"


@dataclass
class JobSpec:
    """Everything the user chose - on the command line or in the window."""
    excel: Path | None = None           # the spreadsheet, for source "sheet"
    source: str = "sheet"               # sheet | links | channel | search
    links: tuple[str, ...] = ()         # source "links": pasted lines, each holding a link
    channel: str = ""                   # source "channel": a channel (or playlist) link
    channel_tabs: tuple[str, ...] = ("videos",)  # keys of CHANNEL_TABS
    queries: tuple[str, ...] = ()       # source "search": one search per entry
    per_query: int = 50                 # results taken from each search
    reuse_only: bool = False            # save only videos whose license allows reuse
    sheet: str | int = 0
    url_column: str | None = None       # None: work it out from the sheet
    name_column: str | None = None      # None: work it out; NO_COLUMN: none at all
    output: Path = Path("output")
    quality: str = "1080"
    workers: int = 3
    retries: int = 3
    timeout: int = 60
    h264: bool = False
    audio: str = ""                     # "": videos; else only their sound, in this format (AUDIO_FORMATS)
    playlists: bool = False
    cookies_from_browser: str | None = None
    cookies_file: Path | None = None
    limit: int | None = None
    verbose: bool = False


@dataclass
class Scan:
    """What the spreadsheet, channel or searches turned out to hold."""
    columns: list[str]
    url_column: str
    name_column: str | None
    rows: list[VideoRow]
    headerless: bool
    label: str = ""                     # the channel's name, or the searches
    notes: list[str] = field(default_factory=list)  # e.g. how many videos each tab or search gave


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
    """List the videos to download: the sheet's links, a channel's videos, or search results.

    For a sheet this settles which columns hold the links and the names.
    Raises JobError for anything the user can fix - a missing file, a column
    that is not there, a sheet with no links in it, a channel that isn't there.
    """
    if spec.source == "channel":
        return list_channel(spec)
    if spec.source == "search":
        return list_search(spec)
    if spec.source == "links":
        return list_links(spec)
    if spec.excel is None or not spec.excel.exists():
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
    if spec.audio and spec.audio not in AUDIO_FORMATS:
        raise JobError(f"Pick an audio format: {', '.join(AUDIO_FORMATS)}.")
    ffmpeg = find_ffmpeg()
    if spec.audio and not ffmpeg:
        raise JobError("Saving audio needs ffmpeg, to take the sound out of the videos. "
                       "Install it (or the imageio-ffmpeg package) and try again.")
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
        ffmpeg=ffmpeg,
        h264_only=spec.h264,
        audio=spec.audio,
        cookies_from_browser=spec.cookies_from_browser,
        cookies_file=spec.cookies_file,
        playlists=spec.playlists,
        reuse_only=spec.reuse_only,
    )
    return PreparedJob(
        spec=spec,
        scan=scan,
        options=options,
        workers=1 if spec.verbose else max(1, spec.workers),
    )


def job_header_lines(job: PreparedJob) -> list[str]:
    """The settings block shared by the console output and report.txt."""
    spec = job.spec
    if spec.source == "channel":
        tabs = ", ".join(CHANNEL_TABS[tab] for tab in spec.channel_tabs)
        source = [f"Channel     : {spec.channel}  ({tabs})"]
    elif spec.source == "search":
        source = [f"Searches    : {'; '.join(spec.queries)}  (up to {spec.per_query} each)"]
    elif spec.source == "links":
        source = [f"Links       : {len(job.rows)} pasted"]
    else:
        source = [f"Input       : {spec.excel}  (sheet {spec.sheet})"]
        if job.scan.url_column:  # empty for Resume / Retry, which go on with the rows already read
            source += [
                f"URL column  : {job.scan.url_column}",
                f"Name column : {job.scan.name_column or '(derived from the URL)'}",
            ]
    return [
        *source,
        f"Output      : {job.folder.resolve()}",
        (f"Saving      : audio only, as {spec.audio}    Workers: {job.workers}" if spec.audio
         else f"Quality     : {job.options.quality}    Workers: {job.workers}"),
        *(["Licenses    : only reuse-allowed videos (CC BY, CC BY-SA, CC0)"] if spec.reuse_only else []),
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
    try:  # audio's work folder, unless a stopped download left a part to resume
        (job.folder / AUDIO_WORK).rmdir()
    except OSError:
        pass
    counts = {status: sum(1 for result in results if result.status == status)
              for status in ("downloaded", "skipped", "not_allowed", "failed", "cancelled")}
    elapsed = time.monotonic() - start_clock

    report_csv = job.folder / REPORT_CSV
    try:
        with report_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["row", "url", "status", "file", "title", "channel", "license",
                             "runtime", "runtime_seconds", "size", "size_bytes", "detail"])
            for result in results:
                writer.writerow([
                    result.row_number, result.url, result.status, result.path, result.title,
                    result.channel, result.license,
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
    update_index(job.folder, results)

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
