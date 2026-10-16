"""Video Splitter: cut long videos into clips for the labeler.

The user marks segments on a timeline in the app. Each segment is cut out with
ffmpeg (re-encoded, so cuts are frame-accurate) and saved to the output
(dataset) folder as <video>_1.mp4, <video>_2.mp4, ... numbered in time order.
An optional crop box per source video is applied to all of its clips.

Segments, markers, the crop and the list of exported clips are kept per source
video in <output>/_splits.json, so a long video can be split over several
sessions and every clip can be traced back to its source. The dataset overview
also keeps each source video's length there, measured once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_from_directory

from common import NO_WINDOW, find_ffmpeg, length_breakdown, list_videos, load_config, probe_duration, update_config

bp = Blueprint("splitter", __name__, url_prefix="/api/split")
lock = threading.Lock()

MANIFEST_NAME = "_splits.json"
MIN_SEGMENT = 0.2  # seconds
MIN_CROP = 32  # pixels
CLIP_LENGTHS = [(3, "Under 3 s"), (10, "3–10 s"), (30, "10–30 s"), (60, "30–60 s"), (float("inf"), "Over 1 min")]


def clip_name(source: str, number: int) -> str:
    return f"{Path(source).stem}_{number}.mp4"


def clip_pattern(source: str) -> re.Pattern:
    return re.compile(rf"{re.escape(Path(source).stem)}_\d+\.mp4", re.IGNORECASE)


def clip_number(name: str) -> int:
    """The N of <video>_N.mp4."""
    return int(Path(name).stem.rsplit("_", 1)[1])


def clean_segments(raw) -> list[dict]:
    """Validate segments from the page: sorted by start, long enough, not overlapping."""
    segments = []
    for s in raw or []:
        start, end = round(float(s["start"]), 3), round(float(s["end"]), 3)
        if start < 0 or end - start < MIN_SEGMENT - 1e-6:
            raise ValueError("Each segment must be at least 0.2 s long.")
        segments.append({"start": start, "end": end})
    segments.sort(key=lambda s: s["start"])
    for a, b in zip(segments, segments[1:]):
        if b["start"] < a["end"] - 1e-6:
            raise ValueError("Segments can't overlap.")
    return segments


def clean_crop(raw) -> dict | None:
    """Validate the crop box from the page, in pixels of the source video. None = the full frame."""
    if not raw:
        return None
    x, y, w, h = (int(raw[k]) for k in ("x", "y", "w", "h"))
    if x < 0 or y < 0:
        raise ValueError("The crop must be inside the video.")
    if w < MIN_CROP or h < MIN_CROP:
        raise ValueError(f"The crop must be at least {MIN_CROP} × {MIN_CROP} pixels.")
    if w % 2 or h % 2:  # H.264 with yuv420p needs an even width and height
        raise ValueError("The crop width and height must be even.")
    return {"x": x, "y": y, "w": w, "h": h}


class SplitProject:
    def __init__(self, source_dir: Path, output_dir: Path):
        self.source_dir = source_dir.resolve()
        if not self.source_dir.is_dir():
            raise FileNotFoundError(f"Source videos folder not found: {self.source_dir}")
        self.output_dir = output_dir.resolve()
        if self.output_dir == self.source_dir:
            raise ValueError("Pick an output folder that is not the source folder, so the clips don't mix with the long videos.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_file = self.output_dir / MANIFEST_NAME
        self.videos = list_videos(self.source_dir)
        self.data: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.manifest_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as e:
            raise ValueError(f"Can't read {self.manifest_file}: {e}") from e

    def save(self):
        tmp = self.manifest_file.with_name("~tmp_" + MANIFEST_NAME)
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.manifest_file)

    def entry(self, file: str) -> dict:
        e = self.data.get(file, {})
        return {"segments": e.get("segments", []), "markers": e.get("markers", []), "crop": e.get("crop"),
                "exported": e.get("exported", []), "last_clip": e.get("last_clip", 0)}

    def existing_clips(self, file: str) -> list[str]:
        pattern = clip_pattern(file)
        return sorted(p.name for p in self.output_dir.iterdir() if p.is_file() and pattern.fullmatch(p.name))

    def state(self) -> dict:
        items = []
        for i, f in enumerate(self.videos):
            e = self.entry(f)
            items.append({"index": i, "file": f, "name": Path(f).stem,
                          "segments": len(e["segments"]), "exported": len(e["exported"])})
        return {
            "ready": True,
            "source_dir": str(self.source_dir),
            "output_dir": str(self.output_dir),
            "items": items,
            "ffmpeg": find_ffmpeg() is not None,
            "exporting": job is not None and job.running,
        }


def export_plan(current: SplitProject, file: str) -> dict:
    """What Export has to do for one video: keep the clips that still match their segment
    (same times, same crop), cut the other segments, and delete clips no segment matches.

    New clips take the next free numbers, after every number this video ever used: a
    clip's name never changes and is never reused, because the labeler keeps labels by
    file name. Working through a video from start to end, that is still time order.
    """
    entry = current.entry(file)
    on_disk = set(current.existing_clips(file))
    keep, todo, used = [], [], set()
    for seg in entry["segments"]:
        match = next((x for x in entry["exported"]
                      if x["file"] in on_disk and x["file"] not in used and same_clip(x, seg, entry["crop"])), None)
        if match:
            keep.append(match)
            used.add(match["file"])
        else:
            todo.append(seg)
    last = max([entry["last_clip"], *(clip_number(name) for name in on_disk)])
    return {
        "keep": keep,
        "todo": [(last + i, seg) for i, seg in enumerate(todo, 1)],  # (clip number, segment), in time order
        "remove": sorted(on_disk - used, key=clip_number),
        "last": last,
    }


class ExportJob:
    """Cuts segments of one video, one after another, in a background thread.

    `segments` is [(clip number, segment)]: only what isn't exported yet.
    """

    def __init__(self, project: SplitProject, file: str, segments: list[tuple[int, dict]], crop: dict | None, ffmpeg: str):
        self.project, self.file, self.segments, self.crop, self.ffmpeg = project, file, segments, crop, ffmpeg
        self.total_time = sum(s["end"] - s["start"] for _, s in segments)
        self.done_time = 0.0
        self.index = 0
        self.percent = 0.0
        self.done: list[str] = []
        self.error: str | None = None
        self.cancelled = False
        self.running = True
        self.proc: subprocess.Popen | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def status(self) -> dict:
        return {
            "running": self.running,
            "file": self.file,
            "index": self.index,
            "total": len(self.segments),
            "percent": round(self.percent, 1),
            "current": clip_name(self.file, self.segments[min(self.index, len(self.segments) - 1)][0]),
            "done": self.done,
            "error": self.error,
            "cancelled": self.cancelled,
            "output_dir": str(self.project.output_dir),
        }

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _run(self):
        try:
            for i, (number, seg) in enumerate(self.segments):
                if self.cancelled:
                    break
                self.index = i
                self._cut(number, seg)
                self.done_time += seg["end"] - seg["start"]
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.error = str(e)
        finally:
            self.running = False
            self.proc = None

    def _cut(self, number: int, seg: dict):
        name = clip_name(self.file, number)
        final = self.project.output_dir / name
        part = final.with_name(final.stem + ".part.mp4")
        length = seg["end"] - seg["start"]
        crop = self.crop
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            # -ss before -i seeks fast; with re-encoding the cut is still frame-accurate.
            "-ss", f"{seg['start']:.3f}", "-i", str(self.project.source_dir / self.file), "-t", f"{length:.3f}",
            "-map", "0:v:0", "-map", "0:a:0?",
            *(["-vf", f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}"] if crop else []),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(part),
        ]
        # stderr goes to a file: a pipe could fill up and block ffmpeg on a damaged video.
        with tempfile.TemporaryFile() as log:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.isdigit():
                    t = min(int(value) / 1e6, length)
                    self.percent = 100 * (self.done_time + t) / self.total_time
            code = self.proc.wait()
            log.seek(0)
            message = log.read().decode("utf-8", "replace").strip().splitlines()
        if self.cancelled or code != 0:
            part.unlink(missing_ok=True)
            if self.cancelled:
                return
            hint = " Check that the crop fits inside the video." if crop else ""
            raise RuntimeError(f"ffmpeg couldn't cut {name}: {message[-1] if message else f'exit code {code}'}.{hint}")
        os.replace(part, final)
        with lock:
            entry = self.project.data.setdefault(self.file, {})
            entry.setdefault("exported", []).append({
                "file": name, "start": seg["start"], "end": seg["end"], "crop": self.crop,
                "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            entry["exported"].sort(key=lambda x: x["start"])
            entry["last_clip"] = max(entry.get("last_clip", 0), number)
            self.project.save()
        self.done.append(name)


project: SplitProject | None = None
job: ExportJob | None = None


def cancel_export():
    if job is not None and job.running:
        job.cancel()


def error(msg: str, status: int = 400, **extra):
    return jsonify({"ok": False, "error": msg, **extra}), status


def require(file: str | None = None):
    if project is None or (file is not None and file not in project.videos):
        abort(404)


# ---------------------------------------------------------------- routes


@bp.get("/state")
def api_state():
    if project is None:
        cfg = load_config()
        return jsonify({"ready": False, "last_source": cfg.get("split_source", ""), "last_output": cfg.get("split_output", "")})
    with lock:
        return jsonify(project.state())


@bp.post("/setup")
def api_setup():
    global project
    data = request.get_json(force=True)
    source = (data.get("source") or "").strip().strip('"')
    output = (data.get("output") or "").strip().strip('"')
    if not source:
        return error("Please enter the folder with the long videos.")
    if not output:
        return error("Please enter the output folder for the clips.")
    if job is not None and job.running:
        return error("Wait for the export to finish, or cancel it.", 409)
    if find_ffmpeg() is None:
        return error("ffmpeg wasn't found. Install ffmpeg and add it to PATH, then try again.")
    try:
        with lock:
            project = SplitProject(Path(source), Path(output))
        update_config(split_source=str(project.source_dir), split_output=str(project.output_dir))
    except Exception as e:  # noqa: BLE001 - surface any problem to the user
        return error(str(e))
    return jsonify({"ok": True})


@bp.post("/close")
def api_close():
    global project
    if job is not None and job.running:
        return error("Wait for the export to finish, or cancel it.", 409)
    project = None
    return jsonify({"ok": True})


@bp.get("/segments/<path:file>")
def api_get_segments(file):
    require(file)
    with lock:
        return jsonify({**project.entry(file), "clips": project.existing_clips(file)})


@bp.put("/segments/<path:file>")
def api_put_segments(file):
    require(file)
    data = request.get_json(force=True)
    try:
        segments = clean_segments(data.get("segments"))
        markers = sorted(round(float(t), 3) for t in data.get("markers") or [] if float(t) >= 0)
        crop = clean_crop(data.get("crop"))
    except (KeyError, TypeError, ValueError) as e:
        return error(str(e) or "Invalid segments.")
    with lock:
        entry = project.data.setdefault(file, {})
        entry["segments"], entry["markers"] = segments, markers
        if crop:
            entry["crop"] = crop
        else:
            entry.pop("crop", None)
        try:
            project.save()
        except OSError as e:
            return error(f"Save failed: {e}", 409)
    return jsonify({"ok": True})


@bp.post("/export")
def api_export():
    global job
    data = request.get_json(force=True)
    file = data.get("file")
    require(file)
    if job is not None and job.running:
        return error("An export is already running.", 409)
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return error("ffmpeg wasn't found. Install ffmpeg and add it to PATH, then try again.")
    with lock:
        entry = project.entry(file)
        if not entry["segments"]:
            return error("Add at least one segment first.")
        # Only what's new: clips that still match their segment are kept as they are.
        plan = export_plan(project, file)
        if not plan["todo"] and not plan["remove"]:
            return error("Every segment is already exported.")
        if plan["remove"] and not data.get("overwrite"):
            return error("Some exported clips no longer match a segment.", 409,
                         existing=plan["remove"], new=len(plan["todo"]))
        for name in plan["remove"]:  # their segment was changed or deleted
            (project.output_dir / name).unlink(missing_ok=True)
        stored = project.data.setdefault(file, {})
        stored["exported"] = plan["keep"]
        stored["last_clip"] = plan["last"]  # numbers of deleted clips are not used again
        project.save()
        if not plan["todo"]:
            return jsonify({"ok": True, "job": False, "removed": plan["remove"]})
        job = ExportJob(project, file, plan["todo"], entry["crop"], ffmpeg)
    return jsonify({"ok": True, "job": True, "removed": plan["remove"]})


@bp.get("/job")
def api_job():
    return jsonify(job.status() if job else {"running": False})


@bp.post("/job/cancel")
def api_job_cancel():
    cancel_export()
    return jsonify({"ok": True})


def same_clip(exported: dict, segment: dict, crop: dict | None) -> bool:
    """An exported clip still matches its segment: same times, same crop."""
    return (abs(exported["start"] - segment["start"]) < 0.002 and abs(exported["end"] - segment["end"]) < 0.002
            and (exported.get("crop") or None) == (crop or None))


def folder_summary(source_dir: Path, output_dir: Path) -> dict:
    """A quick look at a splitter project for the home screen, from its _splits.json."""
    manifest = output_dir / MANIFEST_NAME
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    clips = planned = 0
    seconds = 0.0
    for entry in data.values():
        exported = [x for x in entry.get("exported", []) if (output_dir / x["file"]).is_file()]
        clips += len(exported)
        seconds += sum(x["end"] - x["start"] for x in exported)
        planned += sum(1 for s in entry.get("segments", [])
                       if not any(same_clip(x, s, entry.get("crop")) for x in exported))
    return {
        "videos": len(list_videos(source_dir)) if source_dir.is_dir() else 0,
        "clips": clips,
        "seconds": seconds,
        "planned": planned,
        "updated": (manifest if manifest.exists() else output_dir).stat().st_mtime,
    }


@bp.get("/overview")
def api_overview():
    """The whole dataset: source videos and their hours, the clips and their hours, what's left to export."""
    require()
    current = project
    with lock:
        videos = list(current.videos)
        known = {file: (entry.get("duration"), entry.get("source_size")) for file, entry in current.data.items()}
    # Source lengths are measured once with ffmpeg and kept in the manifest.
    ffmpeg = find_ffmpeg()
    measured = {}
    for file in videos:
        path = current.source_dir / file
        size = path.stat().st_size if path.is_file() else 0
        duration, measured_size = known.get(file, (None, None))
        if not duration or measured_size != size:
            measured[file] = (round(probe_duration(path, ffmpeg), 3), size)
    with lock:
        if measured:
            for file, (duration, size) in measured.items():
                current.data.setdefault(file, {}).update(duration=duration, source_size=size)
            try:
                current.save()
            except OSError:
                pass  # measured again next time
        data = json.loads(json.dumps(current.data))  # a snapshot to count from

    clip_lengths: list[float] = []
    cropped = planned = split = in_progress = 0
    planned_seconds = source_seconds = 0.0
    per_video = []
    for file in videos:
        entry = data.get(file, {})
        source = entry.get("duration") or 0.0
        source_seconds += source
        exported = [x for x in entry.get("exported", []) if (current.output_dir / x["file"]).is_file()]
        lengths = [x["end"] - x["start"] for x in exported]
        clip_lengths += lengths
        cropped += sum(1 for x in exported if x.get("crop"))
        todo = [s for s in entry.get("segments", []) if not any(same_clip(x, s, entry.get("crop")) for x in exported)]
        planned += len(todo)
        planned_seconds += sum(s["end"] - s["start"] for s in todo)
        if exported:
            split += 1
        elif entry.get("segments"):
            in_progress += 1
        if exported or todo:
            per_video.append({"name": Path(file).stem, "clips": len(exported), "seconds": sum(lengths),
                              "source": source, "planned": len(todo)})
    clip_seconds = sum(clip_lengths)
    return jsonify({
        "folder": str(current.output_dir),
        "videos": len(videos),
        "source_seconds": source_seconds,
        "unmeasured": sum(1 for file in videos if not data.get(file, {}).get("duration")),
        "split": split,
        "in_progress": in_progress,
        "untouched": len(videos) - split - in_progress,
        "clips": len(clip_lengths),
        "clip_seconds": clip_seconds,
        "average": clip_seconds / len(clip_lengths) if clip_lengths else 0.0,
        "shortest": min(clip_lengths, default=0.0),
        "longest": max(clip_lengths, default=0.0),
        "coverage": clip_seconds / source_seconds if source_seconds else 0.0,
        "planned": planned,
        "planned_seconds": planned_seconds,
        "cropped": cropped,
        "lengths": length_breakdown(clip_lengths, CLIP_LENGTHS),
        "per_video": sorted(per_video, key=lambda v: (-v["seconds"], v["name"]))[:12],
    })


@bp.post("/reveal")
def api_reveal():
    """Open the output folder in Explorer (only for the PC the app runs on)."""
    if project is None or request.remote_addr not in ("127.0.0.1", "::1"):
        abort(400)
    os.startfile(project.output_dir)
    return jsonify({"ok": True})


@bp.get("/video/<path:file>")
def api_video(file):
    require(file)
    # send_from_directory supports HTTP Range requests, so seeking works.
    return send_from_directory(project.source_dir, file, conditional=True)
