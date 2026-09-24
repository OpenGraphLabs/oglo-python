"""dataset.py: the episode index, the dataset card, and the upload command."""

import json
import subprocess

import pytest

from test_camera_glove import load_example
from test_collect import SLUG, TASK, ScriptedDisplay, collect, make_args, patch_devices

cv2 = pytest.importorskip("cv2")
dataset = load_example("dataset")


def test_index_lists_saved_episodes_only(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    keys = ["g"] + [None] * 15 + ["h", "g"] + [None] * 10 + ["x"]  # one saved, one discarded
    episodes = collect.Collector(make_args(tmp_path, seconds=5), display=ScriptedDisplay(keys)).run()
    assert [e["outcome"] for e in episodes] == ["saved", "discarded"]
    out = tmp_path / "captures"
    assert (out / "episodes.jsonl").is_file() and (out / "README.md").is_file()  # Written on quit.

    rows = dataset.write_index(out)
    assert len(rows) == 1
    row = rows[0]
    manifest = json.loads((out / SLUG / f"{SLUG}_001" / "manifest.json").read_text())
    assert row["task"] == TASK and row["task_slug"] == SLUG and row["session"] == f"{SLUG}_001"
    assert row["path"] == f"{SLUG}/{SLUG}_001"
    assert row["complete"] is True and row["stop_reason"] == "cancelled" and row["aligned"] is True
    assert row["frames"] == manifest["camera"]["frames_decoded"]
    assert 0 < row["duration_s"] < 5 and row["codec"] == "mp4v"
    assert {g["side"] for g in row["gloves"]} == {"left", "right"}
    assert all(g["tactile"]["rows"] > 0 and g["tactile"]["dropped"] == 0 for g in row["gloves"])
    assert row["derived"] == [] and row["bytes"] > 0 and row["sdk_version"]
    assert row["recorded_at"][:2] == "20" and ("+" in row["recorded_at"] or "-" in row["recorded_at"][10:])
    assert [json.loads(line) for line in (out / "episodes.jsonl").read_text().splitlines()] == rows
    card = (out / "README.md").read_text()
    assert card.startswith("---\npretty_name:") and f"| {TASK} | `{SLUG}/` | 1 |" in card

    # Later additions: per-episode derived files, per-glove files, and local-only folders.
    derived = out / SLUG / f"{SLUG}_001" / "derived"
    derived.mkdir()
    (derived / "keypoints.jsonl").write_text("{}\n")
    (out / "gloves" / "OGLO-R-00126").mkdir(parents=True)
    (out / "gloves" / "OGLO-R-00126" / "keypoint_map.json").write_text("{}")
    (out / SLUG / "_failed" / f"{SLUG}_009").mkdir(parents=True)
    (out / SLUG / "_failed" / f"{SLUG}_009" / "manifest.json").write_text(json.dumps(manifest))
    (out / "_scratch").mkdir()
    rows = dataset.write_index(out)
    assert [(r["session"], r["derived"]) for r in rows] == [(f"{SLUG}_001", ["keypoints.jsonl"])]
    assert "`OGLO-R-00126`" in (out / "README.md").read_text()


def write_episode(session, manifest, frames, aligned_rows="frames"):
    """A publishable-looking episode: manifest, the camera files it names, an alignment
    with one row per frame (``aligned_rows`` overrides that count; ``None`` writes none)."""
    session.mkdir(parents=True, exist_ok=True)
    (session / "manifest.json").write_text(json.dumps(manifest))
    for key in ("video", "timestamps"):
        path = session / manifest["camera"][key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    rows = frames if aligned_rows == "frames" else aligned_rows
    if rows is not None:
        (session / "alignment.preview.jsonl").write_text("{}\n" * rows)


def webcam_manifest(frames=1):
    return {"task_description": "Pick", "complete": True,
            "camera": {"kind": "usb_webcam", "video": "camera/video.mp4", "timestamps": "camera/timestamps.jsonl",
                       "frames_decoded": frames, "playback_fps": 30, "codec": "mp4v"},
            "gloves": []}


def minimal_tree(out):
    """A hand-made tree with every kind of folder the upload must and must not send."""
    write_episode(out / "pick" / "pick_001", webcam_manifest(), frames=1)
    for path, text in {
        "pick/pick_001/derived/keypoints.jsonl": "{}",
        "pick/_discarded/pick_002/manifest.json": "{}",
        "pick/_failed/pick_003/manifest.json": "{}",
        "gloves/OGLO-R-1/keypoint_map.json": "{}",
        "_scratch/notes.txt": "x",
        ".gitignore": "*",
    }.items():
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_text(text)


def test_upload_sends_everything_but_local_only_folders(tmp_path, monkeypatch):
    out = tmp_path / "captures"
    minimal_tree(out)
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("dry run must not call hf"))
    assert dataset.upload(out, "me/test", dry_run=True) == 0
    sent = {p.relative_to(out).as_posix() for p in dataset.upload_files(out)}
    assert sent == {"README.md", "episodes.jsonl", "pick/pick_001/manifest.json",
                    "pick/pick_001/camera/video.mp4", "pick/pick_001/camera/timestamps.jsonl",
                    "pick/pick_001/alignment.preview.jsonl", "pick/pick_001/derived/keypoints.jsonl",
                    "gloves/OGLO-R-1/keypoint_map.json"}
    rows = [json.loads(line) for line in (out / "episodes.jsonl").read_text().splitlines()]
    assert [(r["session"], r["duration_s"], r["frames"], r["aligned"]) for r in rows] == [("pick_001", None, 1, True)]

    calls = []
    monkeypatch.setattr(dataset.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setenv("HF_CLI", "/opt/fake/hf")
    assert dataset.upload(out, "me/test", message="first batch") == 0
    (cmd,) = calls
    assert cmd[:5] == ["/opt/fake/hf", "upload", "me/test", str(out.resolve()), "."]
    assert cmd[cmd.index("--repo-type") + 1] == "dataset" and "--private" in cmd
    assert cmd[cmd.index("--commit-message") + 1] == "first batch"
    excludes = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--exclude"]
    assert excludes == ["_*/*", "*/_*/*", ".gitignore"]


def test_incomplete_episodes_are_left_out_and_block_the_upload(tmp_path, monkeypatch, capsys):
    out = tmp_path / "captures"
    minimal_tree(out)
    stray = out / "pick" / "pick_004"  # Interrupted mid-recording, never moved aside.
    stray.mkdir()
    (stray / "manifest.json").write_text(json.dumps({"task_description": "Pick", "complete": False}))
    rows = dataset.write_index(out)
    assert [r["session"] for r in rows] == ["pick_001"]
    assert "pick/pick_004: manifest is not complete" in capsys.readouterr().err
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("must not upload"))
    assert dataset.upload(out, "me/test") == 1
    assert "refusing to upload" in capsys.readouterr().err
    stray.rename(out / "pick" / "_failed" / "pick_004")
    assert dataset.upload(out, "me/test", dry_run=True) == 0


def test_one_gate_decides_indexing_and_upload(tmp_path, monkeypatch, capsys):
    """Unaligned, short-aligned, file-less and manifest-less folders are held back from both."""
    out = tmp_path / "captures"
    minimal_tree(out)
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("must not upload"))
    cases = {
        "pick_011": ("not aligned yet", lambda s: write_episode(s, webcam_manifest(100), 100, aligned_rows=None)),
        "pick_013": ("alignment has 1 rows for 100 decoded frames",
                     lambda s: write_episode(s, webcam_manifest(100), 100, aligned_rows=1)),
        "pick_015": ("camera video file missing", lambda s: (
            write_episode(s, webcam_manifest(), 1), (s / "camera" / "video.mp4").unlink())),
        "pick_016": ("no manifest.json", lambda s: (
            (s / "camera").mkdir(parents=True), (s / "camera" / "video.mp4").write_text("partial"))),
        "pick_017": ("right episode folder missing", lambda s: write_episode(s, {
            **webcam_manifest(), "gloves": [{"side": "right", "episode": "gloves/right/ep_0001"}]}, 1)),
    }
    for name, (_, make) in cases.items():
        make(out / "pick" / name)
    rows = dataset.write_index(out)
    assert [r["session"] for r in rows] == ["pick_001"]
    err = capsys.readouterr().err
    for name, (reason, _) in cases.items():
        assert f"pick/{name}: {reason}" in err
    assert dataset.upload(out, "me/test") == 1
    assert "not publishable episodes" in capsys.readouterr().err
    for name in cases:
        (out / "pick" / name).rename(out / "pick" / "_failed" / name)
    assert dataset.upload(out, "me/test", dry_run=True) == 0

    # A stray file where only episodes may live is caught even though it is no folder.
    (out / "pick" / "leftover.mp4").write_text("partial")
    assert dataset.upload(out, "me/test", dry_run=True) == 1
    assert "pick/leftover.mp4" in capsys.readouterr().err
    (out / "pick" / "leftover.mp4").unlink()
    assert dataset.upload(out, "me/test", dry_run=True) == 0
    sent = {p.relative_to(out).as_posix() for p in dataset.upload_files(out)}
    assert not dataset.stray_files(out, rows, dataset.upload_files(out))
    assert all(f.startswith(("pick/pick_001/", "gloves/")) or f in ("README.md", "episodes.jsonl") for f in sent)


def test_cli_index_and_missing_root(tmp_path, capsys):
    out = tmp_path / "captures"
    minimal_tree(out)
    assert dataset.main(["index", "--out", str(out)]) == 0
    assert "1 episodes indexed" in capsys.readouterr().out
    assert dataset.main(["index", "--out", str(tmp_path / "nowhere")]) == 2
    monkeypatch_repo = dataset.DEFAULT_REPO
    dataset.DEFAULT_REPO = None
    try:
        assert dataset.upload(out, None, dry_run=True) == 2  # No workstation repo, no --repo.
        assert "OGLO_HF_REPO" in capsys.readouterr().err
    finally:
        dataset.DEFAULT_REPO = monkeypatch_repo
    assert dataset.main(["upload", "--out", str(out), "--dry-run", "--repo", "me/test"]) == 0
    assert "me/test" in capsys.readouterr().out


def ovision_manifest():
    return {"task_description": "Pick", "complete": True, "started_wall_time_ns": 1_700_000_000_000_000_000,
            "camera": {"kind": "ovision", "codec": "h264_passthrough", "width": 3840, "height": 1080,
                       "requested_fps": 30, "frames_decoded": 90, "first_host_received_ns": 1_000_000_000,
                       "last_host_received_ns": 4_000_000_000, "imu": "camera/cam_ego.imu.jsonl",
                       "video": "camera/cam_ego.mp4", "timestamps": "camera/timestamps.jsonl"},
            "gloves": []}


def test_index_and_card_describe_the_camera_actually_used(tmp_path):
    out = tmp_path / "captures"
    minimal_tree(out)  # pick_001: an OpenCV episode (no camera IMU)
    rows = dataset.write_index(out)
    card = dataset.dataset_card(out, rows)
    assert "camera IMU is not recorded" in card and "camera/video.mp4" in card
    assert "camera/cam_ego.mp4" not in card and "3200x1200" not in card
    assert card == dataset.dataset_card(out, rows)  # No timestamp: the same tree gives the same card.
    assert "Generated from the episode manifests" in card

    write_episode(out / "pick" / "pick_002", ovision_manifest(), frames=90)
    rows = dataset.write_index(out)
    assert [(r["session"], r["camera_kind"], r["camera_imu"], r["fps"], r["codec"], r["duration_s"]) for r in rows] == [
        ("pick_001", "usb_webcam", False, 30, "mp4v", None), ("pick_002", "ovision", True, 30, "h264_passthrough", 3.0)]
    card = (out / "README.md").read_text()
    assert "or, per episode," in card and "`camera_kind` and `camera_imu`" in card
    assert "camera/cam_ego.imu.jsonl" in card and "camera/video.mp4" in card
    assert "at 30.0 fps" in card  # 90 frames over 3.0 s: the rate actually recorded, not a constant.
    assert "camera_kind" in card and "camera_imu" in card  # Listed with the other fields.

    write_episode(out / "pick" / "pick_001", ovision_manifest(), frames=90)
    card = dataset.dataset_card(out, dataset.write_index(out))
    assert "through its native backend" in card and "camera/cam_ego.mp4" in card
    assert "camera/video.mp4" not in card and "or, per episode," not in card
