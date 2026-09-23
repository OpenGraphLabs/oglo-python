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


def minimal_tree(out):
    """A hand-made tree with every kind of folder the upload must and must not send."""
    for path, text in {
        "pick/pick_001/manifest.json": json.dumps({"task_description": "Pick", "complete": True,
                                                    "camera": {}, "gloves": []}),
        "pick/pick_001/camera/video.mp4": "x",
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
                    "pick/pick_001/camera/video.mp4", "pick/pick_001/derived/keypoints.jsonl",
                    "gloves/OGLO-R-1/keypoint_map.json"}
    rows = [json.loads(line) for line in (out / "episodes.jsonl").read_text().splitlines()]
    assert [(r["session"], r["duration_s"], r["frames"]) for r in rows] == [("pick_001", None, None)]

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
    assert "pick/pick_004" in capsys.readouterr().err
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("must not upload"))
    assert dataset.upload(out, "me/test") == 1
    assert "refusing to upload" in capsys.readouterr().err
    stray.rename(out / "pick" / "_failed" / "pick_004")
    assert dataset.upload(out, "me/test", dry_run=True) == 0


def test_cli_index_and_missing_root(tmp_path, capsys):
    out = tmp_path / "captures"
    minimal_tree(out)
    assert dataset.main(["index", "--out", str(out)]) == 0
    assert "1 episodes indexed" in capsys.readouterr().out
    assert dataset.main(["index", "--out", str(tmp_path / "nowhere")]) == 2
    assert dataset.main(["upload", "--out", str(out), "--dry-run"]) == 0
    assert dataset.DEFAULT_REPO in capsys.readouterr().out
