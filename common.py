"""Paths, settings and helpers shared by the labeler (main.py) and the splitter (splitter.py)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # running as the installed .exe
# PyInstaller unpacks the bundled templates/static to sys._MEIPASS.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "VideoLabeler"
if FROZEN:
    # The install folder isn't writable, so keep settings in the user's profile.
    CONFIG_FILE = APP_DATA / "config.json"
else:
    CONFIG_FILE = RESOURCE_DIR / ".labeler_config.json"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
START_TRIES = 5
# No console window for ffmpeg: the installed app has no console of its own.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_ffmpeg() -> str | None:
    """The ffmpeg bundled with the app (imageio-ffmpeg), else the one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed or no binary for this platform
        return shutil.which("ffmpeg")


DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path: Path, ffmpeg: str | None = None) -> float:
    """How long a video file plays for, in seconds. 0.0 when unknown.

    The app ships ffmpeg without ffprobe, so the length is read from what
    `ffmpeg -i` prints (it only reads the file's header, so it's quick).
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


def length_breakdown(durations: list[float], edges: list[tuple[float, str]]) -> list[dict]:
    """Count videos (and their seconds) per length range, for a dataset overview.

    `edges` is [(upper limit in seconds, label), ...] in increasing order; the
    last range catches everything longer.
    """
    rows = [{"label": label, "count": 0, "seconds": 0.0} for _, label in edges]
    for seconds in durations:
        index = next((i for i, (limit, _) in enumerate(edges) if seconds < limit), len(edges) - 1)
        rows[index]["count"] += 1
        rows[index]["seconds"] += seconds
    return rows


def start_again_later():
    """Start the installed app again in a moment, and exit this copy.

    Right after an update the installer starts the new version while Windows
    Defender may still be scanning the new files. Loading a library then fails
    ("being used by another process"), so try again a few times before giving up.
    Returns only when it's time to give up (or when running from source).
    """
    tries = int(os.environ.get("VIDEOLABELER_START_TRY", "1"))
    if not FROZEN or tries >= START_TRIES:
        return
    time.sleep(2)
    # PYINSTALLER_RESET_ENVIRONMENT: start as a new app, not as a child of this one.
    env = {**os.environ, "VIDEOLABELER_START_TRY": str(tries + 1), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen([sys.executable, *sys.argv[1:]], env=env)
    os._exit(0)


def natural_key(name: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def list_videos(folder: Path) -> list[str]:
    """Video file names in a folder, in natural order (video2 before video10)."""
    return sorted((p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS), key=natural_key)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_config(**values: str):
    """Save the given settings, keeping the others (the labeler and splitter share one file)."""
    cfg = {**load_config(), **values}
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
