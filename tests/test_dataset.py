"""dataset.py: the episode index, the dataset card, and the upload commands."""

import json
import subprocess

import pytest

from test_camera_glove import load_example
from test_collect import SLUG, TASK, ScriptedDisplay, collect, make_args, patch_devices

cv2 = pytest.importorskip("cv2")
dataset = load_example("dataset")


@pytest.fixture(autouse=True)
def hub_checks(monkeypatch):
    """The upload preflight asks the hf CLI and the Hub; tests answer for them."""
    monkeypatch.setattr(dataset, "hf_version", lambda: (1, 28, 0))
    monkeypatch.setattr(dataset, "repo_visibility", lambda repo: "private")


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
    assert row["measured_fps"] == round((row["frames"] - 1) / row["duration_s"], 2)
    assert row["camera_kind"] == "usb_webcam" and row["camera_imu"] is False
    assert row["alignment_max_delta_ms"] == 50
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
    (out / "gloves" / "OGLO-R-TEST02").mkdir(parents=True)
    (out / "gloves" / "OGLO-R-TEST02" / "keypoint_map.json").write_text("{}")
    (out / SLUG / "_failed" / f"{SLUG}_009").mkdir(parents=True)
    (out / SLUG / "_failed" / f"{SLUG}_009" / "manifest.json").write_text(json.dumps(manifest))
    (out / "_scratch").mkdir()
    rows = dataset.write_index(out)
    assert [(r["session"], r["derived"]) for r in rows] == [(f"{SLUG}_001", ["keypoints.jsonl"])]
    assert "`OGLO-R-TEST02`" in (out / "README.md").read_text()


@pytest.mark.parametrize("clean", [True, False])
def test_missing_glove_files_are_held_from_index_and_upload(tmp_path, monkeypatch, clean):
    patch_devices(monkeypatch, clean=clean)
    display = ScriptedDisplay(["g"] + [None] * 15 + ["h"])
    episodes = collect.Collector(make_args(tmp_path, seconds=5), display=display).run()
    assert [episode["outcome"] for episode in episodes] == ["saved"]

    out = tmp_path / "captures"
    session = out / SLUG / f"{SLUG}_001"
    manifest = json.loads((session / "manifest.json").read_text())
    entry = manifest["gloves"][0]
    side = entry["side"]
    glove = session / entry["episode"]
    required = [
        session / entry["calibration"],
        glove / "meta.json",
        glove / f"tactile_{side}.jsonl",
        glove / f"tactile_{side}.calibration.json",
        glove / f"wrist_imu_{side}.jsonl",
        glove / f"wrist_mag_{side}.jsonl",
    ]
    if not clean:
        required.append(glove / f"tactile_{side}.raw.jsonl")
    for path in required:
        content = path.read_bytes()
        path.unlink()
        assert dataset.publishable(session) is not None, path
        rows, held = dataset.scan(out)
        assert rows == [] and len(held) == 1, path
        path.write_bytes(content)
    assert dataset.publishable(session) is None

    tactile = glove / f"tactile_{side}.jsonl"
    tactile.unlink()
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("must not upload"))
    assert dataset.upload(out, "me/test", dry_run=True) == 1


def write_episode(session, manifest, frames, aligned_rows="frames"):
    """A publishable-looking episode: manifest, every camera file it names, an alignment
    with one row per frame (``aligned_rows`` overrides that count; ``None`` writes none)."""
    session.mkdir(parents=True, exist_ok=True)
    (session / "manifest.json").write_text(json.dumps(manifest))
    camera = manifest["camera"]
    named = [camera[key] for key in dataset.CAMERA_FILE_KEYS if camera.get(key)]
    for relative in named + list(camera.get("native_artifacts") or []):
        path = session / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    rows = frames if aligned_rows == "frames" else aligned_rows
    if rows is not None:
        (session / "alignment.preview.jsonl").write_text((json.dumps({"max_delta_ms": 50}) + "\n") * rows)


def webcam_manifest(frames=1):
    return {"task_description": "Pick", "complete": True,
            "camera": {"kind": "usb_webcam", "video": "camera/video.mp4", "timestamps": "camera/timestamps.jsonl",
                       "frames_decoded": frames, "playback_fps": 30, "codec": "mp4v", "width": 1280, "height": 720},
            "gloves": []}


def minimal_tree(out):
    """A hand-made tree with every kind of folder the upload must and must not send."""
    write_episode(out / "pick" / "pick_001", webcam_manifest(), frames=1)
    for path, text in {
        "pick/pick_001/derived/keypoints.jsonl": "{}",
        "pick/_discarded/pick_002/manifest.json": "{}",
        "pick/_failed/pick_003/manifest.json": "{}",
        "pick/.next_session": "3\n",
        "gloves/OGLO-R-1/keypoint_map.json": "{}",
        "_scratch/notes.txt": "x",
        ".gitignore": "*",
        ".git/HEAD": "ref: refs/heads/main",  # hf download / a git clone: never tasks
        ".cache/huggingface/download/x.lock": "",
    }.items():
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_text(text)


def test_upload_sends_the_indexed_episodes_then_the_index(tmp_path, monkeypatch):
    out = tmp_path / "captures"
    minimal_tree(out)
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("dry run must not call hf"))
    assert dataset.upload(out, "me/test", dry_run=True) == 0
    rows = [json.loads(line) for line in (out / "episodes.jsonl").read_text().splitlines()]
    assert [(r["session"], r["duration_s"], r["frames"], r["aligned"]) for r in rows] == [("pick_001", None, 1, True)]
    sent = {p.relative_to(out).as_posix() for p in dataset.upload_files(out, rows)}
    assert sent == {"README.md", "episodes.jsonl", "pick/pick_001/manifest.json",
                    "pick/pick_001/camera/video.mp4", "pick/pick_001/camera/timestamps.jsonl",
                    "pick/pick_001/alignment.preview.jsonl", "pick/pick_001/derived/keypoints.jsonl",
                    "gloves/OGLO-R-1/keypoint_map.json"}

    calls = []
    monkeypatch.setattr(dataset.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setenv("HF_CLI", "/opt/fake/hf")
    assert dataset.upload(out, "me/test", message="first batch") == 0
    episodes, index = calls  # The files first; the index that lists them only once they are there.
    for cmd in calls:
        assert cmd[:5] == ["/opt/fake/hf", "upload", "me/test", str(out.resolve()), "."]
        assert cmd[cmd.index("--repo-type") + 1] == "dataset" and "--private" in cmd
        assert "--exclude" not in cmd
    includes = lambda cmd: [cmd[i + 1] for i, part in enumerate(cmd) if part == "--include"]
    assert includes(episodes) == ["pick/pick_001/*", "gloves/*"]  # Only the indexed episodes, by name.
    assert episodes[episodes.index("--commit-message") + 1] == "first batch (episode files)"
    assert includes(index) == ["episodes.jsonl", "README.md"]
    assert index[index.index("--commit-message") + 1] == "first batch"

    calls.clear()
    monkeypatch.setattr(dataset.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 3))
    assert dataset.upload(out, "me/test") == 3  # The index is not pushed over episodes that did not arrive.
    assert len(calls) == 1


def test_upload_refuses_a_public_repo_and_an_old_hf(tmp_path, monkeypatch, capsys):
    out = tmp_path / "captures"
    minimal_tree(out)
    monkeypatch.setattr(dataset.subprocess, "run", lambda *a, **k: pytest.fail("must not upload"))
    monkeypatch.setattr(dataset, "repo_visibility", lambda repo: "public")
    assert dataset.upload(out, "me/test", dry_run=True) == 1
    assert "me/test exists and is PUBLIC" in capsys.readouterr().err
    monkeypatch.setattr(dataset, "repo_visibility", lambda repo: "not visible")  # New, or hidden: hf creates it private.
    assert dataset.upload(out, "me/test", dry_run=True) == 0
    monkeypatch.setattr(dataset, "hf_version", lambda: (0, 36, 2))  # Keeps only the last --include.
    assert dataset.upload(out, "me/test", dry_run=True) == 1
    assert "install huggingface_hub>=1.0" in capsys.readouterr().err
    monkeypatch.setattr(dataset, "hf_version", lambda: None)
    assert dataset.upload(out, "me/test", dry_run=True) == 1
    assert "does not run" in capsys.readouterr().err

    def unreachable(repo):
        raise OSError("name resolution failed")

    monkeypatch.setattr(dataset, "hf_version", lambda: (1, 28, 0))
    monkeypatch.setattr(dataset, "repo_visibility", unreachable)
    assert dataset.upload(out, "me/test", dry_run=True) == 1
    assert "cannot check whether me/test is private" in capsys.readouterr().err


def test_repo_visibility_reads_the_hub_answer(monkeypatch):
    """The Hub's dataset endpoint answers 200 with ``private`` for a repo the token can
    see, 401/404 otherwise; only a visible public repo is a reason to refuse."""
    import io
    import urllib.error

    monkeypatch.undo()  # The autouse fixture stubs repo_visibility; this test wants the real one.
    answers = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def urlopen(request, timeout):
        answers["url"] = request.full_url
        answers["auth"] = request.get_header("Authorization")
        code, body = answers["next"]
        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        return Response(json.dumps(body).encode())

    monkeypatch.setattr(dataset.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(dataset, "hf_token", lambda: "tok")
    monkeypatch.setenv("HF_ENDPOINT", "https://hub.example/")
    answers["next"] = (200, {"private": False})
    assert dataset.repo_visibility("org/data") == "public"
    assert answers["url"] == "https://hub.example/api/datasets/org/data" and answers["auth"] == "Bearer tok"
    answers["next"] = (200, {"private": True})
    assert dataset.repo_visibility("org/data") == "private"
    answers["next"] = (401, None)
    assert dataset.repo_visibility("org/data") == "not visible"
    answers["next"] = (500, None)
    with pytest.raises(urllib.error.HTTPError):
        dataset.repo_visibility("org/data")


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
        "pick_015": ("camera video file missing: camera/video.mp4", lambda s: (
            write_episode(s, webcam_manifest(), 1), (s / "camera" / "video.mp4").unlink())),
        "pick_016": ("no manifest.json", lambda s: (
            (s / "camera").mkdir(parents=True), (s / "camera" / "video.mp4").write_text("partial"))),
        "pick_017": ("right episode folder missing", lambda s: write_episode(s, {
            **webcam_manifest(), "gloves": [{"side": "right", "episode": "gloves/right/ep_0001"}]}, 1)),
        "pick_018": ("manifest.json unreadable", lambda s: (
            s.mkdir(parents=True), (s / "manifest.json").write_text("{not json"))),
    }
    for name, (_, make) in cases.items():
        make(out / "pick" / name)
    rows = dataset.write_index(out)
    assert [r["session"] for r in rows] == ["pick_001"]
    err = capsys.readouterr().err
    for name, (reason, _) in cases.items():
        assert f"pick/{name}: {reason}" in err
    assert "aligned when collect.py runs again" in err
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
    assert not dataset.stray_files(out, rows)
    sent = {p.relative_to(out).as_posix() for p in dataset.upload_files(out, rows)}
    assert all(f.startswith(("pick/pick_001/", "gloves/")) or f in ("README.md", "episodes.jsonl") for f in sent)


def test_hidden_folders_are_not_tasks_and_linked_episodes_are_held(tmp_path, capsys):
    out = tmp_path / "captures"
    minimal_tree(out)  # .git/ and .cache/ are in it already
    elsewhere = tmp_path / "other_disk" / "pick_002"
    write_episode(elsewhere, webcam_manifest(), frames=1)
    (out / "pick" / "pick_002").symlink_to(elsewhere, target_is_directory=True)
    rows = dataset.write_index(out)
    assert [r["session"] for r in rows] == ["pick_001"]
    err = capsys.readouterr().err
    assert "pick/pick_002: symbolic link" in err and ".git" not in err and ".cache" not in err


def test_a_readme_that_dataset_py_did_not_write_is_kept(tmp_path, capsys):
    out = tmp_path / "captures"
    minimal_tree(out)
    (out / "README.md").write_text("# my own notes\n")
    with pytest.raises(RuntimeError, match="was not written by dataset.py"):
        dataset.write_index(out)
    assert (out / "README.md").read_text() == "# my own notes\n"
    assert dataset.main(["index", "--out", str(out)]) == 1
    assert "not written by dataset.py" in capsys.readouterr().err
    (out / "README.md").unlink()
    dataset.write_index(out)
    assert dataset.CARD_MARKER in (out / "README.md").read_text()
    assert not list(out.glob(".*.tmp"))  # Both root files are replaced atomically.


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
                       "last_host_received_ns": 1_000_000_000 + 89 * 33_333_333,  # 30 fps exactly
                       "video": "camera/cam_ego.mp4", "timestamps": "camera/timestamps.jsonl",
                       "native_stereo_metadata": "camera/cam_ego.stereo.jsonl",
                       "calibration": "camera/cam_ego.calibration.json",
                       "imu": "camera/cam_ego.imu.jsonl", "accel": "camera/cam_ego.accel.jsonl",
                       "gyro": "camera/cam_ego.gyro.jsonl", "mag": "camera/cam_ego.mag.jsonl",
                       "sync_point": "camera/sync_point.json", "finalization": "camera/finalization.json",
                       "native_artifacts": ["camera/cam_ego.calibration.yaml", "camera/cam_ego.calibration.bin"]},
            "gloves": []}


def test_index_and_card_describe_the_camera_actually_used(tmp_path):
    out = tmp_path / "captures"
    minimal_tree(out)  # pick_001: an OpenCV episode (no camera IMU)
    rows = dataset.write_index(out)
    card = dataset.dataset_card(out, rows)
    assert "camera IMU is not recorded" in card and "camera/video.mp4" in card
    assert "through OpenCV (1280x720, mp4v at 30 fps)" in card  # What the manifests say, not a fixed claim.
    assert "H.265" not in card and "head-mounted" not in card
    assert "camera/cam_ego.mp4" not in card and "3200x1200" not in card
    assert card == dataset.dataset_card(out, rows)  # No timestamp: the same tree gives the same card.
    assert dataset.CARD_MARKER in card
    assert "within 50 ms" in card

    write_episode(out / "pick" / "pick_002", ovision_manifest(), frames=90)
    rows = dataset.write_index(out)
    assert [(r["session"], r["camera_kind"], r["camera_imu"], r["fps"], r["codec"], r["duration_s"], r["measured_fps"])
            for r in rows] == [("pick_001", "usb_webcam", False, 30, "mp4v", None, None),
                               ("pick_002", "ovision", True, 30, "h264_passthrough", 2.967, 30.0)]
    card = (out / "README.md").read_text()
    assert "or, per episode," in card and "`camera_kind` and `camera_imu`" in card
    assert "camera/cam_ego.imu.jsonl" in card and "camera/video.mp4" in card
    assert "H.264 stream at 30.0 fps" in card  # 89 intervals over 2.967 s: the rate actually recorded.
    assert "camera_kind" in card and "camera_imu" in card and "measured_fps" in card  # Listed with the other fields.

    write_episode(out / "pick" / "pick_001", ovision_manifest(), frames=90)
    card = dataset.dataset_card(out, dataset.write_index(out))
    assert "through its native backend" in card and "camera/cam_ego.mp4" in card
    assert "camera/video.mp4" not in card and "or, per episode," not in card


def test_an_ovision_episode_missing_a_native_file_is_held(tmp_path, capsys):
    """The gate checks every file the manifest names, so camera_imu is never claimed
    for an episode whose IMU file is gone."""
    out = tmp_path / "captures"
    write_episode(out / "pick" / "pick_001", ovision_manifest(), frames=90)
    assert [r["camera_imu"] for r in dataset.write_index(out)] == [True]
    (out / "pick" / "pick_001" / "camera" / "cam_ego.imu.jsonl").unlink()
    assert dataset.write_index(out) == []
    assert "pick/pick_001: camera imu file missing: camera/cam_ego.imu.jsonl" in capsys.readouterr().err
    (out / "pick" / "pick_001" / "camera" / "cam_ego.imu.jsonl").write_text("x")
    (out / "pick" / "pick_001" / "camera" / "cam_ego.calibration.bin").unlink()
    assert dataset.write_index(out) == []
    assert "camera native_artifacts file missing: camera/cam_ego.calibration.bin" in capsys.readouterr().err


def test_card_reading_example_picks_the_glove_by_side(tmp_path):
    out = tmp_path / "captures"
    minimal_tree(out)
    card = dataset.dataset_card(out, dataset.write_index(out))
    assert 'glove["side"] == "right"' in card and "gloves\"][0]" not in card
    assert "not on PyPI" in card and "pip install oglo" not in card
