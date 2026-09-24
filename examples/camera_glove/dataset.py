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
manifest says ``complete``, the camera and glove files it names exist, and
``alignment.preview.jsonl`` holds one row per decoded frame. That one test decides
both what is indexed and what may be uploaded: ``upload`` refuses while any folder
under a task is not a publishable episode, and checks that the file list ``hf`` would
send contains nothing outside the indexed episodes, ``gloves/`` and the two root files.

Both root files are derived from the manifests, so they are regenerated rather
than maintained. ``upload`` regenerates them and runs ``hf upload`` on the whole
tree minus every ``_*`` folder; the Hub skips files whose content did not change,
so running it after a session only transfers the new episodes. Files deleted
locally stay on the Hub until removed there (``hf repos delete-files``).

The Hub repo comes from ``--repo`` or ``$OGLO_HF_REPO`` (``scripts/workstation.env``).
"""

import argparse
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent.parent / "captures"
DEFAULT_REPO = os.environ.get("OGLO_HF_REPO")  # workstation configuration; --repo overrides
EXCLUDE = ("_*/*", "*/_*/*", ".gitignore")  # fnmatch patterns, exactly as hf upload applies them
INDEX = "episodes.jsonl"
CARD = "README.md"
ALIGNMENT = "alignment.preview.jsonl"
RESERVED = {"gloves"}  # root folders that hold no episodes
FIELDS = {
    "task": "the --task text", "task_slug": "task folder name", "session": "episode folder name",
    "path": "episode folder relative to the root", "recorded_at": "local wall time with offset",
    "complete": "manifest complete flag (always true here)", "stop_reason": "cancelled = stopped by h, duration = hit the cap",
    "duration_s": "first to last camera frame", "frames": "decoded video frames",
    "fps": "video frame rate (the encoder's playback rate, or the requested rate of the OVISION passthrough)",
    "width": "video width", "height": "video height", "codec": "video encoder", "video_quality": "CRF / CQ",
    "camera_kind": "usb_webcam = any camera through OpenCV; ovision = OVISION-EGO-V1 through its native backend",
    "camera_imu": "true when the episode has the camera's own IMU (camera/cam_ego.imu.jsonl)",
    "gloves": "per glove: serial, side, fw_rev, stream_clean, tactile / imu / mag rows, hz, dropped",
    "aligned": "alignment.preview.jsonl has one row per frame (always true here)", "derived": "files under derived/",
    "bytes": "size of the episode folder", "sdk_version": "oglo SDK that recorded it",
}


def hf_cli():
    """The ``hf`` command: $HF_CLI, else on PATH, else the uv tool install."""
    return os.environ.get("HF_CLI") or shutil.which("hf") or str(Path.home() / ".local/bin/hf")


# -- index -------------------------------------------------------------------------

def iter_sessions(out):
    """Every folder that sits where an episode would, task by task, in name order.

    ``_*`` folders and ``gloves/`` are skipped; a folder without a manifest is still
    yielded so that :func:`scan` can hold it back from the upload.
    """
    for task_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        if task_dir.name.startswith("_") or task_dir.name in RESERVED:
            continue
        for session in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            if not session.name.startswith("_"):
                yield session


def read_manifest(session):
    path = session / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def alignment_rows(session):
    path = session / ALIGNMENT
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def publishable(session, manifest=None):
    """None when the episode may be indexed and uploaded, else the reason it may not.

    The one gate for publication: a complete manifest whose camera and glove files
    exist, plus a finished alignment with exactly one row per decoded frame (align.py
    writes that file atomically, so a partial one never exists).
    """
    manifest = read_manifest(session) if manifest is None else manifest
    if manifest is None:
        return "no manifest.json"
    if manifest.get("complete") is not True:
        return "manifest is not complete"
    camera = manifest.get("camera") or {}
    for key in ("video", "timestamps"):
        if not camera.get(key) or not (session / camera[key]).is_file():
            return f"camera {key} file missing"
    for entry in manifest.get("gloves") or []:
        if not entry.get("episode") or not (session / entry["episode"]).is_dir():
            return f"{entry.get('side') or 'glove'} episode folder missing"
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


def index_row(out, session, manifest=None):
    manifest = read_manifest(session) if manifest is None else manifest
    camera = manifest.get("camera") or {}
    first, last = camera.get("first_host_received_ns"), camera.get("last_host_received_ns")
    derived = session / "derived"
    return {
        "task": manifest.get("task_description"),
        "task_slug": session.parent.name,
        "session": session.name,
        "path": session.relative_to(out).as_posix(),
        "recorded_at": iso_local(manifest.get("started_wall_time_ns")),
        "complete": manifest.get("complete"),
        "stop_reason": manifest.get("stop_reason"),
        "duration_s": round((last - first) / 1e9, 3) if first is not None and last is not None else None,
        "frames": camera.get("frames_decoded"),
        "fps": camera.get("playback_fps", camera.get("requested_fps")),
        "width": camera.get("width"),
        "height": camera.get("height"),
        "codec": camera.get("codec"),
        "video_quality": camera.get("video_quality"),
        "camera_kind": camera.get("kind"),
        "camera_imu": camera.get("kind") == "ovision",
        "gloves": [glove_row(entry) for entry in manifest.get("gloves") or []],
        "aligned": True,
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
    ``_failed/``.
    """
    rows, held = [], []
    for session in iter_sessions(out):
        manifest = read_manifest(session)
        reason = publishable(session, manifest)
        if reason is None:
            rows.append(index_row(out, session, manifest))
        else:
            held.append((session.relative_to(out).as_posix(), reason))
    return rows, held


def warn_held(held):
    if held:
        print(f"warning: {len(held)} folder(s) under the tasks are not publishable episodes and are "
              "not indexed; move them to _failed/ or delete them:\n  "
              + "\n  ".join(f"{path}: {reason}" for path, reason in held), file=sys.stderr, flush=True)


def write_files(out, rows):
    with (out / INDEX).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / CARD).write_text(dataset_card(out, rows), encoding="utf-8")


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
    "usb_webcam": ("a head-mounted stereo camera recorded through OpenCV (left | right side by side "
                   "in one frame, {fps} fps); camera IMU is not recorded on these episodes"),
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
    "usb_webcam": [
        "  camera/video.mp4         stereo video (H.265 unless `codec` says otherwise)",
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


def measured_fps(rows):
    """The recorded frame rate: frames over duration, median across episodes; the requested
    rate when nothing was measured yet."""
    rates = sorted(row["frames"] / row["duration_s"] for row in rows
                   if row.get("frames") and row.get("duration_s"))
    if rates:
        return f"{rates[len(rates) // 2]:.1f}"
    requested = [row["fps"] for row in rows if row.get("fps")]
    return f"{requested[0]:g}" if requested else "unknown"


def camera_paragraph(kinds, rows):
    fps = measured_fps(rows)
    if len(kinds) == 1:
        camera = CAMERA_SENTENCE[kinds[0]].format(fps=fps)
    else:
        camera = (" or, per episode, ".join(CAMERA_SENTENCE[kind].format(fps=fps) for kind in kinds)
                  + " (`camera_kind` and `camera_imu` in `episodes.jsonl` say which)")
    return (f"Each episode records an OGLO tactile glove (80 taxels at 250 Hz, wrist IMU at 500 Hz, "
            f"magnetometer at 125 Hz) together with {camera}. Every stream carries the same host "
            "monotonic clock (`host_received_ns`), so frames and tactile rows align by timestamp; "
            "`alignment.preview.jsonl` holds the nearest-neighbour match within 50 ms for every frame.")


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
        "Generated from the episode manifests by `dataset.py index`; do not edit by hand.",
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
        "import oglo  # pip install oglo, the SDK that recorded the gloves",
        "",
        "session = \"<task>/<task>_001\"",
        "frames = [json.loads(line) for line in open(f\"{session}/alignment.preview.jsonl\")]",
        "episode = oglo.replay(f\"{session}/gloves/right/ep_0001\")",
        "# frames[i][\"gloves\"][0][\"tactile\"][\"row_index\"] is the tactile row nearest to video frame i",
        "```",
        "",
    ]
    return "\n".join(lines)


# -- upload ------------------------------------------------------------------------

def upload_files(out):
    """Files ``hf upload`` will send, chosen with its own fnmatch rules."""
    files = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = path.relative_to(out).as_posix()
        if not any(fnmatchcase(rel, pattern) for pattern in EXCLUDE):
            files.append(path)
    return files


def stray_files(out, rows, files):
    """Files ``hf`` would send that belong to no indexed episode: the root files and
    ``gloves/`` are the only other things allowed on the Hub."""
    allowed = tuple(f"{row['path']}/" for row in rows) + ("gloves/",)
    strays = []
    for path in files:
        rel = path.relative_to(out).as_posix()
        if rel not in (INDEX, CARD) and not rel.startswith(allowed):
            strays.append(rel)
    return strays


def upload_command(out, repo, message):
    cmd = [hf_cli(), "upload", repo, str(out), ".", "--repo-type", "dataset", "--private",
           "--commit-message", message]
    for pattern in EXCLUDE:
        cmd += ["--exclude", pattern]
    return cmd


def upload(out, repo=DEFAULT_REPO, dry_run=False, message=None):
    """Index, then push the tree. Returns the exit code of ``hf upload`` (0 on a dry run).

    Refuses while anything under a task folder is not a publishable episode, and while
    the file list ``hf`` would send holds anything outside the indexed episodes.
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
    files = upload_files(out)
    strays = stray_files(out, rows, files)
    if strays:
        print("refusing to upload: these files belong to no indexed episode:\n  " + "\n  ".join(strays),
              file=sys.stderr, flush=True)
        return 1
    tasks = {row["task_slug"] for row in rows}
    message = message or f"{len(rows)} episodes in {len(tasks)} tasks from {socket.gethostname()}"
    cmd = upload_command(out, repo, message)
    size = sum(p.stat().st_size for p in files)
    print(f"{len(files)} files, {gb(size)} under {out} -> {repo} (private dataset); "
          "the Hub skips files whose content did not change", flush=True)
    per_folder = {}
    for path in files:
        top = path.relative_to(out).parts[0]
        entry = per_folder.setdefault(top, [0, 0])
        entry[0] += 1
        entry[1] += path.stat().st_size
    for top, (count, size) in sorted(per_folder.items()):
        print(f"  {top:32s} {count:6d} files  {gb(size)}", flush=True)
    print("  " + " ".join(shlex.quote(part) for part in cmd), flush=True)
    if dry_run:
        return 0
    return subprocess.run(cmd).returncode


# -- CLI ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="rewrite episodes.jsonl and README.md from the manifests")
    index.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"dataset root (default: {DEFAULT_OUT})")
    push = commands.add_parser("upload", help="index, then hf upload everything except _* folders")
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
    if args.command == "index":
        rows = write_index(args.out)
        print(f"{len(rows)} episodes indexed: {args.out / INDEX} and {args.out / CARD}", flush=True)
        return 0
    return upload(args.out, args.repo, args.dry_run, args.message)


if __name__ == "__main__":
    sys.exit(main())
