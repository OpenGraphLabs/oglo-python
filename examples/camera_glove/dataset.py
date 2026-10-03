#!/usr/bin/env python3
"""Index and upload the capture tree that collect.py writes.

The ``--out`` folder of collect.py is the dataset root and, unchanged, the root of
the Hugging Face dataset repo:

    <out>/
      README.md                  dataset card, rewritten by ``dataset.py index``
      episodes.jsonl             one row per saved episode, rewritten by ``dataset.py index``
      gloves/<serial>/           reserved for per-glove files OGLO sends back (static
                                 calibration, keypoint maps); listed on the card
      <task>/<task>_NNN/         one saved episode exactly as capture.py wrote it
      <task>/<task>_NNN/derived/ reserved for per-episode files produced later
                                 (keypoint time series, labels); raw files never change
      <task>/_discarded/         episodes ended with x            } local only,
      <task>/_failed/            episodes with complete=false     } never uploaded

An episode is *publishable* when :func:`publishable` finds nothing wrong: its
manifest says ``complete``, every camera and glove file it names exists, and
``alignment.preview.jsonl`` holds one row per decoded frame. That one test decides
both what is indexed and what may be uploaded: ``upload`` refuses while any folder
under a task is not a publishable episode or any file under a task belongs to no
indexed episode, then sends exactly the indexed episodes plus ``gloves/`` (one
``hf upload`` with an ``--include`` per episode, so a recording that starts meanwhile
is not swept up) and, in a second commit, the two root files, so the index on the
Hub never lists an episode whose files are not there yet.

Both root files are derived from the manifests, so they are regenerated rather
than maintained. The Hub skips files whose content did not change, so running the
upload after a session only transfers the new episodes. Files deleted locally stay
on the Hub until removed there (``hf repos delete-files``); their episode numbers
are never reused by collect.py.

The Hub repo comes from ``--repo`` or ``$OGLO_HF_REPO`` (``scripts/workstation.env``).
It must be private: ``hf upload --private`` only applies to a repo it creates, so
the visibility of an existing repo is checked before anything is sent.
"""

import argparse
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import uuid4

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent.parent / "captures"
DEFAULT_REPO = os.environ.get("OGLO_HF_REPO")  # workstation configuration; --repo overrides
INDEX = "episodes.jsonl"
CARD = "README.md"
ALIGNMENT = "alignment.preview.jsonl"
RESERVED = {"gloves"}  # root folders that hold no episodes
CARD_MARKER = "Generated from the episode manifests by `dataset.py index`"
# Manifest camera keys that name session-relative files; every one present must exist.
CAMERA_FILE_KEYS = ("video", "timestamps", "native_stereo_metadata", "calibration", "imu", "accel",
                    "gyro", "mag", "sync_point", "finalization")
FIELDS = {
    "task": "the --task text", "task_slug": "task folder name", "session": "episode folder name",
    "path": "episode folder relative to the root", "recorded_at": "local wall time with offset",
    "complete": "manifest complete flag (always true here)", "stop_reason": "cancelled = stopped by h, duration = hit the cap",
    "duration_s": "first to last camera frame", "frames": "decoded video frames",
    "fps": "video playback rate (the encoder's, or the requested rate of the OVISION passthrough)",
    "measured_fps": "frames minus one over the first-to-last frame time: the rate actually recorded",
    "width": "video width", "height": "video height", "codec": "video encoder", "video_quality": "CRF / CQ",
    "camera_kind": "usb_webcam = any camera through OpenCV; ovision = OVISION-EGO-V1 through its native backend; "
                   "realsense = RealSense D455 through pyrealsense2",
    "camera_imu": "true when the episode holds the camera's own IMU files (OVISION camera/cam_ego.imu.jsonl; "
                  "RealSense camera/realsense.accel.jsonl + realsense.gyro.jsonl)",
    "gloves": "per glove: serial, side, fw_rev, stream_clean, tactile / imu / mag rows, hz, dropped",
    "aligned": "alignment.preview.jsonl has one row per frame (always true here)",
    "alignment_max_delta_ms": "the tolerance align.py matched glove rows to frames with",
    "derived": "files under derived/",
    "bytes": "size of the episode folder", "sdk_version": "oglo SDK that recorded it",
}


def hf_cli():
    """The ``hf`` command: $HF_CLI, else on PATH, else the uv tool install."""
    return os.environ.get("HF_CLI") or shutil.which("hf") or str(Path.home() / ".local/bin/hf")


def atomic_text(path, text):
    """Write ``text`` to ``path`` in one step: a reader never sees a truncated file."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# -- index -------------------------------------------------------------------------

def iter_sessions(out):
    """Every folder that sits where an episode would, task by task, in name order.

    Hidden and ``_*`` folders and ``gloves/`` are skipped; a folder without a manifest
    is still yielded so that :func:`scan` can hold it back from the upload.
    """
    for task_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        if task_dir.name.startswith((".", "_")) or task_dir.name in RESERVED:
            continue
        for session in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            if not session.name.startswith((".", "_")):
                yield session


def load_manifest(session):
    """``(manifest, reason)``: the parsed manifest.json, or None and why not."""
    path = session / "manifest.json"
    if not path.is_file():
        return None, "no manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"manifest.json unreadable: {exc}"
    if not isinstance(manifest, dict):
        return None, "manifest.json is not a JSON object"
    return manifest, None


def alignment_rows(session):
    path = session / ALIGNMENT
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def glove_files_reason(session, entry):
    """Why a glove episode is incomplete, or None when all recorded files exist."""
    if not isinstance(entry, dict):
        return "invalid glove entry"
    side = entry.get("side")
    if side not in ("left", "right"):
        return f"invalid glove side: {side!r}"
    episode = entry.get("episode")
    if not episode or not (session / episode).is_dir():
        return f"{side} episode folder missing"
    calibration = entry.get("calibration")
    if not calibration or not (session / calibration).is_file():
        return f"{side} calibration file missing: {calibration}"

    folder = session / episode
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{side} meta.json unreadable: {exc}"
    if not isinstance(meta, dict) or meta.get("schema") != 3 or meta.get("complete") is not True:
        return f"{side} meta.json is not a complete schema-3 episode"
    if meta.get("side") != side or meta.get("serial") != entry.get("serial"):
        return f"{side} meta.json identity differs from the manifest"
    clean_file = f"tactile_{side}.jsonl"
    calibration_file = f"tactile_{side}.calibration.json"
    if meta.get("clean_file") != clean_file or meta.get("calibration") != calibration_file:
        return f"{side} meta.json does not name its clean tactile and calibration files"
    if type(meta.get("stream_clean")) is not bool:
        return f"{side} meta.json has no stream mode"
    tactile_file = clean_file if meta["stream_clean"] else f"tactile_{side}.raw.jsonl"
    for name in (tactile_file, clean_file, calibration_file,
                 f"wrist_imu_{side}.jsonl", f"wrist_mag_{side}.jsonl"):
        if not (folder / name).is_file():
            return f"{side} glove file missing: {episode}/{name}"
    return None


def publishable(session, manifest=None):
    """None when the episode may be indexed and uploaded, else the reason it may not.

    The one gate for publication: a complete manifest whose camera files (every
    path it names, the native OVISION IMU / calibration / report files included) and
    glove folders exist, plus a finished alignment with exactly one row per decoded
    frame (align.py writes that file atomically, so a partial one never exists).
    """
    if manifest is None:
        manifest, reason = load_manifest(session)
        if reason is not None:
            return reason
    if manifest.get("complete") is not True:
        return "manifest is not complete"
    camera = manifest.get("camera") or {}
    for key in ("video", "timestamps"):
        if not camera.get(key):
            return f"camera names no {key} file"
    named = [(key, camera[key]) for key in CAMERA_FILE_KEYS if camera.get(key)]
    named += [("native_artifacts", path) for path in camera.get("native_artifacts") or []]
    for key, relative in named:
        if not (session / relative).is_file():
            return f"camera {key} file missing: {relative}"
    for entry in manifest.get("gloves") or []:
        reason = glove_files_reason(session, entry)
        if reason is not None:
            return reason
    frames = camera.get("frames_decoded")
    rows = alignment_rows(session)
    if rows is None:
        return "not aligned yet"
    if not isinstance(frames, int) or rows != frames:
        return f"alignment has {rows} rows for {frames} decoded frames"
    return None


def folder_bytes(folder):
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def iso_local(wall_ns):
    if wall_ns is None:
        return None
    return datetime.fromtimestamp(wall_ns / 1e9, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def glove_row(entry):
    summary = entry.get("summary") or {}
    row = {"serial": entry.get("serial"), "side": entry.get("side"),
           "fw_rev": summary.get("fw_rev"), "stream_clean": summary.get("stream_clean")}
    for stream in ("tactile", "imu", "mag"):
        stats = summary.get(stream) or {}
        row[stream] = {"rows": stats.get("n"), "hz": stats.get("hz"), "dropped": stats.get("dropped")}
    return row


def alignment_tolerance(session):
    """``max_delta_ms`` as align.py wrote it into every row (read from the first)."""
    try:
        with (session / ALIGNMENT).open(encoding="utf-8") as handle:
            return json.loads(handle.readline()).get("max_delta_ms")
    except (OSError, ValueError, AttributeError):
        return None


def index_row(out, session, manifest):
    camera = manifest.get("camera") or {}
    first, last = camera.get("first_host_received_ns"), camera.get("last_host_received_ns")
    frames = camera.get("frames_decoded")
    duration = round((last - first) / 1e9, 3) if first is not None and last is not None else None
    measured = (round((frames - 1) / duration, 2)
                if duration and isinstance(frames, int) and frames > 1 else None)
    derived = session / "derived"
    return {
        "task": manifest.get("task_description"),
        "task_slug": session.parent.name,
        "session": session.name,
        "path": session.relative_to(out).as_posix(),
        "recorded_at": iso_local(manifest.get("started_wall_time_ns")),
        "complete": manifest.get("complete"),
        "stop_reason": manifest.get("stop_reason"),
        "duration_s": duration,
        "frames": frames,
        "fps": camera.get("playback_fps", camera.get("requested_fps")),
        "measured_fps": measured,
        "width": camera.get("width"),
        "height": camera.get("height"),
        "codec": camera.get("codec"),
        "video_quality": camera.get("video_quality"),
        "camera_kind": camera.get("kind"),
        # publishable() saw every file these name; RealSense keeps accel and gyro apart.
        "camera_imu": bool(camera.get("imu") or (camera.get("accel") and camera.get("gyro"))),
        "gloves": [glove_row(entry) for entry in manifest.get("gloves") or []],
        "aligned": True,
        "alignment_max_delta_ms": alignment_tolerance(session),
        "derived": sorted(p.relative_to(derived).as_posix() for p in derived.rglob("*") if p.is_file())
        if derived.is_dir() else [],
        "bytes": folder_bytes(session),
        "sdk_version": manifest.get("sdk_version"),
    }


def scan(out):
    """``(rows, held)``: index rows of the publishable episodes, and ``(path, reason)``
    for every other folder under a task.

    A held folder is an episode that was interrupted, is being recorded or aligned
    right now, or is not an episode at all; collect.py moves its own failures to
    ``_failed/``. A symbolic link is held too: ``hf upload`` would not follow it.
    """
    rows, held = [], []
    for session in iter_sessions(out):
        relative = session.relative_to(out).as_posix()
        if session.is_symlink() or session.parent.is_symlink():
            held.append((relative, "symbolic link (the upload would not follow it)"))
            continue
        manifest, reason = load_manifest(session)
        reason = reason or publishable(session, manifest)
        if reason is None:
            rows.append(index_row(out, session, manifest))
        else:
            held.append((relative, reason))
    return rows, held


def warn_held(held):
    if held:
        print(f"warning: {len(held)} folder(s) under the tasks are not publishable episodes and are "
              "not indexed (a complete episode that is not aligned yet is aligned when collect.py "
              "runs again; move anything else to _failed/ or delete it):\n  "
              + "\n  ".join(f"{path}: {reason}" for path, reason in held), file=sys.stderr, flush=True)


def write_files(out, rows):
    """Rewrite the two root files in place, atomically; refuse to replace a README.md
    that dataset.py did not write."""
    card = out / CARD
    if card.is_file() and CARD_MARKER not in card.read_text(encoding="utf-8"):
        raise RuntimeError(f"{card} was not written by dataset.py; move it away or use another --out")
    atomic_text(out / INDEX, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    atomic_text(card, dataset_card(out, rows))


def write_index(out):
    """Rewrite ``episodes.jsonl`` and ``README.md`` from the manifests; returns the rows."""
    out = Path(out)
    rows, held = scan(out)
    write_files(out, rows)
    warn_held(held)
    return rows


# -- dataset card ------------------------------------------------------------------

def hms(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def gb(count):
    return f"{count / 1e9:.2f} GB"


CAMERA_SENTENCE = {
    "ovision": ("an OVISION-EGO-V1 head-mounted stereo camera recorded through its native backend: "
                "the original 3840x1080 H.264 stream at {fps} fps (left | right eye, 1920x1080 each), "
                "the camera's own IMU (500 Hz accelerometer + gyroscope) and magnetometer, per-eye "
                "exposure timing and the unit's stereo / IMU calibration"),
    "realsense": ("an Intel RealSense D455 recorded through pyrealsense2: color video ({size}, {codec} at "
                  "{fps} fps), the camera's own accelerometer and gyroscope on the camera clock, and the "
                  "unit's factory color / IMU calibration"),
    "usb_webcam": ("a camera recorded through OpenCV ({size}, {codec} at {fps} fps); "
                   "camera IMU is not recorded on these episodes"),
}
CAMERA_LAYOUT = {
    "ovision": [
        "  camera/cam_ego.mp4             stereo H.264 exactly as the camera sent it (left | right, 1920x1080 each)",
        "  camera/cam_ego.stereo.jsonl    per frame: exposure timing of both eyes on the camera clock, host capture_ns",
        "  camera/cam_ego.imu.jsonl       camera IMU: accel (m/s^2) + gyro (rad/s) per sample, host + camera time",
        "  camera/cam_ego.{accel,gyro,mag}.jsonl   the same samples split per sensor (mag may be empty)",
        "  camera/cam_ego.calibration.*   this unit's stereo / IMU calibration (json, yaml, raw flash blob)",
        "  camera/timestamps.jsonl        one row per frame: frame_index, host_received_ns, device_timestamp",
        "  camera/sync_point.json, finalization.json   host clock anchor and the capture report",
    ],
    "realsense": [
        "  camera/video.mp4               RealSense color as the encoder named in `codec` wrote it",
        "  camera/timestamps.jsonl        one row per frame: host_received_ns and device_timestamp (camera clock, us)",
        "  camera/realsense.accel.jsonl   camera accelerometer (m/s^2) per sample, host + camera time",
        "  camera/realsense.gyro.jsonl    camera gyroscope (rad/s) per sample, host + camera time",
        "  camera/realsense.calibration.json   color intrinsics, color-to-IMU extrinsics, firmware",
    ],
    "usb_webcam": [
        "  camera/video.mp4         the video as the encoder named in `codec` wrote it",
        "  camera/timestamps.jsonl  one row per frame: frame_index, host_read_started_ns, host_received_ns",
    ],
}


def camera_kinds(rows):
    """Camera kinds present, ordered as CAMERA_SENTENCE lists them.

    An episode whose manifest names no kind (or one this card does not know) counts
    as a webcam episode: it has no camera IMU either way. No episodes: webcam.
    """
    present = {row.get("camera_kind") if row.get("camera_kind") in CAMERA_SENTENCE else "usb_webcam"
               for row in rows}
    kinds = [kind for kind in CAMERA_SENTENCE if kind in present]
    return kinds or ["usb_webcam"]


def rows_of_kind(rows, kind):
    return [row for row in rows if (row.get("camera_kind") if row.get("camera_kind") in CAMERA_SENTENCE
                                    else "usb_webcam") == kind]


def measured_fps(rows):
    """The recorded frame rate of these episodes: the median of their ``measured_fps``;
    the requested rate when nothing was measured yet."""
    rates = sorted(row["measured_fps"] for row in rows if row.get("measured_fps"))
    if rates:
        return f"{rates[len(rates) // 2]:.1f}"
    requested = [row["fps"] for row in rows if row.get("fps")]
    return f"{requested[0]:g}" if requested else "unknown"


def listed(values, fallback="unknown"):
    return " / ".join(str(v) for v in values) if values else fallback


def camera_paragraph(kinds, rows):
    sentences = []
    for kind in kinds:
        own = rows_of_kind(rows, kind)
        sizes = sorted({f"{r['width']}x{r['height']}" for r in own if r.get("width") and r.get("height")})
        codecs = sorted({r["codec"] for r in own if r.get("codec")})
        sentences.append(CAMERA_SENTENCE[kind].format(fps=measured_fps(own), size=listed(sizes),
                                                      codec=listed(codecs)))
    if len(sentences) == 1:
        camera = sentences[0]
    else:
        camera = (" or, per episode, ".join(sentences)
                  + " (`camera_kind` and `camera_imu` in `episodes.jsonl` say which)")
    tolerance = listed(sorted({r["alignment_max_delta_ms"] for r in rows if r.get("alignment_max_delta_ms")}), "50")
    return (f"Each episode records an OGLO tactile glove (80 taxels at 250 Hz, wrist IMU at 500 Hz, "
            f"magnetometer at 125 Hz) together with {camera}. Every stream carries the same host "
            "monotonic clock (`host_received_ns`), so frames and tactile rows align by timestamp; "
            f"`alignment.preview.jsonl` holds the nearest-neighbour match within {tolerance} ms for "
            "every frame (`alignment_max_delta_ms` per episode).")


def dataset_card(out, rows):
    tasks = {}
    for row in rows:
        entry = tasks.setdefault(row["task_slug"], {"tasks": [], "episodes": 0, "seconds": 0.0,
                                                    "bytes": 0, "gloves": set()})
        if row["task"] not in entry["tasks"]:  # collect.py refuses a second wording; show it if it happened
            entry["tasks"].append(row["task"])
        entry["episodes"] += 1
        entry["seconds"] += row["duration_s"] or 0.0
        entry["bytes"] += row["bytes"]
        entry["gloves"].update(g["serial"] for g in row["gloves"] if g["serial"])
    gloves_root = out / "gloves"
    glove_dirs = sorted(p.name for p in gloves_root.iterdir() if p.is_dir()) if gloves_root.is_dir() else []
    total_s = sum(e["seconds"] for e in tasks.values())
    total_b = sum(e["bytes"] for e in tasks.values())
    kinds = camera_kinds(rows)

    lines = [
        "---",
        "pretty_name: OGLO tactile glove + egocentric stereo video",
        "license: other",
        "tags:",
        "- robotics",
        "- tactile-sensing",
        "- egocentric-video",
        "- oglo",
        "---",
        "",
        "# OGLO tactile glove + egocentric stereo video",
        "",
        f"{len(tasks)} tasks, {len(rows)} episodes, {hms(total_s)} of recording, {gb(total_b)}. "
        f"{CARD_MARKER}; do not edit by hand.",
        "",
        camera_paragraph(kinds, rows),
        "",
        "## Tasks",
        "",
        "| task | folder | episodes | duration | size | gloves |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for slug, entry in sorted(tasks.items()):
        lines.append(f"| {' / '.join(entry['tasks'])} | `{slug}/` | {entry['episodes']} | {hms(entry['seconds'])} | "
                     f"{gb(entry['bytes'])} | {', '.join(sorted(entry['gloves'])) or '-'} |")
    if not tasks:
        lines.append("| (none yet) | | 0 | 0:00:00 | 0.00 GB | - |")
    lines += [
        "",
        "## Layout",
        "",
        "```",
        "README.md                  this card",
        "episodes.jsonl             one JSON object per episode (fields below)",
        "gloves/<serial>/           per-glove files returned by OGLO (static calibration, keypoint maps)",
        "<task>/<task>_NNN/         one episode, numbered per task",
        "  manifest.json            session summary: task, clocks, camera + glove metadata, overlap interval",
        *(line for kind in kinds for line in CAMERA_LAYOUT[kind]),
        "  gloves/<side>/calibration.json          the GET ZERO recipe in force during the episode",
        "  gloves/<side>/ep_0001/tactile_<side>.jsonl     250 Hz, keys <finger>_<row>_<col>, capture_ns",
        "  gloves/<side>/ep_0001/wrist_imu_<side>.jsonl   500 Hz accel + gyro",
        "  gloves/<side>/ep_0001/wrist_mag_<side>.jsonl   125 Hz magnetometer",
        "  gloves/<side>/ep_0001/meta.json, *.calibration.json   SDK episode metadata",
        "  alignment.preview.jsonl  per frame: nearest tactile / imu / mag row of each glove and delta_ns",
        "  derived/                 files produced after recording (keypoints, labels); raw files never change",
        "```",
        "",
        "Only publishable episodes are indexed and uploaded: a complete manifest whose files exist and "
        "an `alignment.preview.jsonl` with one row per decoded frame. Discarded or failed recordings "
        "stay on the collection machine under `<task>/_discarded/` and `<task>/_failed/`.",
        "",
        "## Reserved places for data that arrives later",
        "",
        "- Per episode (time series such as hand keypoints): `<task>/<task>_NNN/derived/`. "
        "The `derived` field of `episodes.jsonl` lists what is there.",
        "- Per glove (static calibration, keypoint maps): `gloves/<serial>/`. "
        f"Present now: {', '.join(f'`{d}`' for d in glove_dirs) or 'none'}.",
        "",
        "## episodes.jsonl fields",
        "",
    ]
    lines += [f"- `{name}`: {meaning}" for name, meaning in FIELDS.items()]
    lines += [
        "",
        "## Reading an episode",
        "",
        "```python",
        "import json",
        "import oglo  # the SDK that recorded the gloves: github.com/OpenGraphLabs/oglo-python (not on PyPI)",
        "",
        "session = \"<task>/<task>_001\"",
        "frames = [json.loads(line) for line in open(f\"{session}/alignment.preview.jsonl\")]",
        "# Each frame lists every glove with its side; pick one by side, not by position.",
        "right = next(glove for glove in frames[0][\"gloves\"] if glove[\"side\"] == \"right\")",
        "tactile = oglo.replay(f\"{session}/{right['episode']}\").arrays(\"tactile\")",
        "# For frame i, the tactile row nearest to it (None when none is within alignment_max_delta_ms):",
        "match = next(glove for glove in frames[i][\"gloves\"] if glove[\"side\"] == \"right\")[\"tactile\"]",
        "counts = tactile[\"counts\"][match[\"row_index\"]] if match else None",
        "```",
        "",
    ]
    return "\n".join(lines)


# -- upload ------------------------------------------------------------------------

def upload_patterns(rows):
    """``--include`` globs, in hf's fnmatch dialect: the indexed episodes and gloves/."""
    return [f"{row['path']}/*" for row in rows] + ["gloves/*"]


def upload_files(out, rows):
    """Files the two ``hf upload`` commands will send, chosen with hf's own fnmatch rules."""
    patterns = upload_patterns(rows) + [INDEX, CARD]
    return [path for path in sorted(p for p in out.rglob("*") if p.is_file())
            if any(fnmatchcase(path.relative_to(out).as_posix(), pattern) for pattern in patterns)]


def stray_files(out, rows):
    """Files under the task folders that belong to no indexed episode: a recording in
    progress, or something left behind. Hidden entries and ``_*`` folders are the
    collector's own and stay local."""
    allowed = tuple(f"{row['path']}/" for row in rows)
    strays = []
    for task_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        if task_dir.name.startswith((".", "_")) or task_dir.name in RESERVED:
            continue
        for path in sorted(p for p in task_dir.rglob("*") if p.is_file()):
            if path.relative_to(task_dir).parts[0].startswith((".", "_")):
                continue
            relative = path.relative_to(out).as_posix()
            if not relative.startswith(allowed):
                strays.append(relative)
    return strays


def upload_commands(out, repo, rows, message):
    """Two ``hf upload`` runs: the episode files, then the index and card that list them."""
    base = [hf_cli(), "upload", repo, str(out), ".", "--repo-type", "dataset", "--private"]
    episodes = base + ["--commit-message", f"{message} (episode files)"]
    for pattern in upload_patterns(rows):
        episodes += ["--include", pattern]
    index = base + ["--commit-message", message, "--include", INDEX, "--include", CARD]
    return [episodes, index]


def hf_version():
    """``(major, minor, patch)`` of the hf CLI, or None when it does not run."""
    try:
        done = subprocess.run([hf_cli(), "version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", done.stdout)
    return tuple(int(part) for part in match.groups()) if match else None


def hf_token():
    """The token hf itself would use: $HF_TOKEN, else what ``hf auth token`` prints."""
    token = os.environ.get("HF_TOKEN")
    if token:
        return token
    try:
        done = subprocess.run([hf_cli(), "auth", "token", "--quiet"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def repo_visibility(repo):
    """``"private"``, ``"public"`` or ``"not visible"`` (absent, or hidden from this token)
    for a dataset repo on the Hub. Raises OSError when the Hub cannot be asked."""
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    request = urllib.request.Request(f"{endpoint}/api/datasets/{repo}")
    token = hf_token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            info = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            return "not visible"
        raise
    return "private" if info.get("private") else "public"


def preflight(repo):
    """Why the upload must not start, or None.

    An hf CLI older than 1.0 keeps only the last of repeated ``--include`` flags and
    would send the whole tree, ``_failed/`` included. A repo that exists and is public
    would receive the recordings as they are: ``--private`` only applies to a repo hf
    creates itself.
    """
    version = hf_version()
    if version is None:
        return (f"{hf_cli()} does not run; install huggingface_hub>=1.0 "
                "(uv tool install 'huggingface_hub[cli]') or set HF_CLI")
    if version < (1, 0, 0):
        return (f"hf {'.'.join(map(str, version))} keeps only the last --include and would upload "
                "everything; install huggingface_hub>=1.0")
    try:
        visibility = repo_visibility(repo)
    except OSError as exc:
        return f"cannot check whether {repo} is private ({exc}); not uploading"
    if visibility == "public":
        return (f"{repo} exists and is PUBLIC; these recordings are for a private repo. Make it private "
                f"first (hf repos settings {repo} --repo-type dataset --private) or use another --repo")
    return None


def upload(out, repo=DEFAULT_REPO, dry_run=False, message=None):
    """Index, then push the tree. Returns the exit code of ``hf upload`` (0 on a dry run).

    Refuses while anything under a task folder is not a publishable episode or belongs
    to no indexed episode, while the hf CLI is too old, and while the repo is public.
    """
    if not repo:
        print("no Hub repo: pass --repo or set OGLO_HF_REPO (scripts/workstation.env)",
              file=sys.stderr, flush=True)
        return 2
    out = Path(out).resolve()
    rows, held = scan(out)
    write_files(out, rows)
    if held:
        warn_held(held)
        print("refusing to upload while folders that are not publishable episodes sit next to the good ones",
              file=sys.stderr, flush=True)
        return 1
    strays = stray_files(out, rows)
    if strays:
        print("refusing to upload: these files belong to no indexed episode:\n  " + "\n  ".join(strays),
              file=sys.stderr, flush=True)
        return 1
    if not rows:
        print(f"nothing to upload: no publishable episodes under {out}", file=sys.stderr, flush=True)
        return 1
    problem = preflight(repo)
    if problem:
        print(problem, file=sys.stderr, flush=True)
        return 1
    files = upload_files(out, rows)
    tasks = {row["task_slug"] for row in rows}
    message = message or f"{len(rows)} episodes in {len(tasks)} tasks from {socket.gethostname()}"
    commands = upload_commands(out, repo, rows, message)
    size = sum(p.stat().st_size for p in files)
    print(f"{len(files)} files, {gb(size)} under {out} -> {repo} (private dataset), the episode files "
          "first and the index after them; the Hub skips files whose content did not change", flush=True)
    per_folder = {}
    for path in files:
        top = path.relative_to(out).parts[0]
        entry = per_folder.setdefault(top, [0, 0])
        entry[0] += 1
        entry[1] += path.stat().st_size
    for top, (count, size) in sorted(per_folder.items()):
        print(f"  {top:32s} {count:6d} files  {gb(size)}", flush=True)
    for command in commands:
        print("  " + " ".join(shlex.quote(part) for part in command), flush=True)
    if dry_run:
        return 0
    for command in commands:
        code = subprocess.run(command).returncode
        if code:
            return code
    return 0


# -- CLI ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="rewrite episodes.jsonl and README.md from the manifests")
    index.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"dataset root (default: {DEFAULT_OUT})")
    push = commands.add_parser("upload", help="index, then hf upload the indexed episodes and the index")
    push.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"dataset root (default: {DEFAULT_OUT})")
    push.add_argument("--repo", default=DEFAULT_REPO,
                      help="Hub dataset repo (default: $OGLO_HF_REPO" + (f" = {DEFAULT_REPO})" if DEFAULT_REPO else ", unset)"))
    push.add_argument("--dry-run", action="store_true", help="index and show what would be sent, send nothing")
    push.add_argument("--message", help="commit message (default: episode and task counts)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.out.is_dir():
        print(f"{args.out} is not a folder; record something with collect.py first", file=sys.stderr)
        return 2
    try:
        if args.command == "index":
            rows = write_index(args.out)
            print(f"{len(rows)} episodes indexed: {args.out / INDEX} and {args.out / CARD}", flush=True)
            return 0
        return upload(args.out, args.repo, args.dry_run, args.message)
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
