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

Both root files are derived from the manifests, so they are regenerated rather
than maintained. ``upload`` regenerates them and runs ``hf upload`` on the whole
tree minus every ``_*`` folder; the Hub skips files whose content did not change,
so running it after a session only transfers the new episodes. Files deleted
locally stay on the Hub until removed there (``hf repos delete-files``).
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
DEFAULT_REPO = "ntumars-opengraph/oglo-tactile-ego"
EXCLUDE = ("_*/*", "*/_*/*", ".gitignore")  # fnmatch patterns, exactly as hf upload applies them
INDEX = "episodes.jsonl"
CARD = "README.md"
RESERVED = {"gloves"}  # root folders that hold no episodes
FIELDS = {
    "task": "the --task text", "task_slug": "task folder name", "session": "episode folder name",
    "path": "episode folder relative to the root", "recorded_at": "local wall time with offset",
    "complete": "manifest complete flag (always true here)", "stop_reason": "cancelled = stopped by h, duration = hit the cap",
    "duration_s": "first to last camera frame", "frames": "decoded video frames", "fps": "playback fps",
    "width": "video width", "height": "video height", "codec": "video encoder", "video_quality": "CRF / CQ",
    "gloves": "per glove: serial, side, fw_rev, stream_clean, tactile / imu / mag rows, hz, dropped",
    "aligned": "alignment.preview.jsonl present", "derived": "files under derived/",
    "bytes": "size of the episode folder", "sdk_version": "oglo SDK that recorded it",
}


def hf_cli():
    """The ``hf`` command: $HF_CLI, else on PATH, else the uv tool install."""
    return os.environ.get("HF_CLI") or shutil.which("hf") or str(Path.home() / ".local/bin/hf")


# -- index -------------------------------------------------------------------------

def iter_sessions(out):
    """Saved episode folders, task by task, in name order. ``_*`` folders are skipped."""
    for task_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        if task_dir.name.startswith("_") or task_dir.name in RESERVED:
            continue
        for session in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            if not session.name.startswith("_") and (session / "manifest.json").is_file():
                yield session


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


def index_row(out, session):
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
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
        "fps": camera.get("playback_fps"),
        "width": camera.get("width"),
        "height": camera.get("height"),
        "codec": camera.get("codec"),
        "video_quality": camera.get("video_quality"),
        "gloves": [glove_row(entry) for entry in manifest.get("gloves") or []],
        "aligned": (session / "alignment.preview.jsonl").is_file(),
        "derived": sorted(p.relative_to(derived).as_posix() for p in derived.rglob("*") if p.is_file())
        if derived.is_dir() else [],
        "bytes": folder_bytes(session),
        "sdk_version": manifest.get("sdk_version"),
    }


def scan(out):
    """``(rows, incomplete)``: complete episodes for the index, and the ones that are not.

    An incomplete manifest under ``<task>/`` is an episode that was interrupted (or is
    being recorded right now); collect.py moves its own failures to ``_failed/``.
    """
    rows, incomplete = [], []
    for session in iter_sessions(out):
        row = index_row(out, session)
        (rows if row["complete"] is True else incomplete).append(row)
    return rows, incomplete


def warn_incomplete(incomplete):
    if incomplete:
        print(f"warning: {len(incomplete)} incomplete episode(s) are not indexed; move them to "
              "_failed/ or delete them:\n  " + "\n  ".join(r["path"] for r in incomplete),
              file=sys.stderr, flush=True)


def write_files(out, rows):
    with (out / INDEX).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out / CARD).write_text(dataset_card(out, rows), encoding="utf-8")


def write_index(out):
    """Rewrite ``episodes.jsonl`` and ``README.md`` from the manifests; returns the rows."""
    out = Path(out)
    rows, incomplete = scan(out)
    write_files(out, rows)
    warn_incomplete(incomplete)
    return rows


# -- dataset card ------------------------------------------------------------------

def hms(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def gb(count):
    return f"{count / 1e9:.2f} GB"


def dataset_card(out, rows):
    tasks = {}
    for row in rows:
        entry = tasks.setdefault(row["task_slug"], {"task": row["task"], "episodes": 0, "seconds": 0.0,
                                                    "bytes": 0, "gloves": set()})
        entry["episodes"] += 1
        entry["seconds"] += row["duration_s"] or 0.0
        entry["bytes"] += row["bytes"]
        entry["gloves"].update(g["serial"] for g in row["gloves"] if g["serial"])
    gloves_root = out / "gloves"
    glove_dirs = sorted(p.name for p in gloves_root.iterdir() if p.is_dir()) if gloves_root.is_dir() else []
    total_s = sum(e["seconds"] for e in tasks.values())
    total_b = sum(e["bytes"] for e in tasks.values())
    today = datetime.now().astimezone().isoformat(timespec="minutes")

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
        f"Generated by `dataset.py index` on {today}; do not edit by hand.",
        "",
        "Each episode records an OGLO tactile glove (80 taxels at 250 Hz, wrist IMU at 500 Hz, "
        "magnetometer at 125 Hz) together with a head-mounted stereo camera (left|right side by side "
        "in one frame, 30 fps). Every stream carries the same host monotonic clock "
        "(`host_received_ns`), so frames and tactile rows align by timestamp; `alignment.preview.jsonl` "
        "holds the nearest-neighbour match within 50 ms for every frame.",
        "",
        "## Tasks",
        "",
        "| task | folder | episodes | duration | size | gloves |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for slug, entry in sorted(tasks.items()):
        lines.append(f"| {entry['task']} | `{slug}/` | {entry['episodes']} | {hms(entry['seconds'])} | "
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
        "  camera/video.mp4         stereo video (H.265 unless `codec` says otherwise)",
        "  camera/timestamps.jsonl  one row per frame: frame_index, host_read_started_ns, host_received_ns",
        "  gloves/<side>/calibration.json          the GET ZERO recipe in force during the episode",
        "  gloves/<side>/ep_0001/tactile_<side>.jsonl     250 Hz, keys <finger>_<row>_<col>, capture_ns",
        "  gloves/<side>/ep_0001/wrist_imu_<side>.jsonl   500 Hz accel + gyro",
        "  gloves/<side>/ep_0001/wrist_mag_<side>.jsonl   125 Hz magnetometer",
        "  gloves/<side>/ep_0001/meta.json, *.calibration.json   SDK episode metadata",
        "  alignment.preview.jsonl  per frame: nearest tactile / imu / mag row of each glove and delta_ns",
        "  derived/                 files produced after recording (keypoints, labels); raw files never change",
        "```",
        "",
        "Only complete, aligned episodes are published. Discarded or failed recordings stay on the "
        "collection machine under `<task>/_discarded/` and `<task>/_failed/`.",
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


def upload_command(out, repo, message):
    cmd = [hf_cli(), "upload", repo, str(out), ".", "--repo-type", "dataset", "--private",
           "--commit-message", message]
    for pattern in EXCLUDE:
        cmd += ["--exclude", pattern]
    return cmd


def upload(out, repo=DEFAULT_REPO, dry_run=False, message=None):
    """Index, then push the tree. Returns the exit code of ``hf upload`` (0 on a dry run)."""
    out = Path(out).resolve()
    rows, incomplete = scan(out)
    write_files(out, rows)
    if incomplete:
        warn_incomplete(incomplete)
        print("refusing to upload while incomplete episodes sit next to the good ones",
              file=sys.stderr, flush=True)
        return 1
    files = upload_files(out)
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
    push.add_argument("--repo", default=DEFAULT_REPO, help=f"Hub dataset repo (default: {DEFAULT_REPO})")
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
