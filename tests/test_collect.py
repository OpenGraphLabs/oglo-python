"""Drive the collect.py state machine with simulated devices and no window."""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import oglo
import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo._device import Glove
from oglo._usb import UsbTransport
from test_camera_glove import Camera, load_example

cv2 = pytest.importorskip("cv2")
collect = load_example("collect")

TASK = "Synthetic contact"
SLUG = "synthetic_contact"


def simulated_glove(side, clean=True, **config_overrides):
    """collect.py keeps one glove open across calibration and every episode, so the
    stream is switched on several times; FakeSerial keeps counting across sessions
    like a real board (measured 2026-09-23)."""
    config = {**CFG_V6, "side": side, "serial": f"OGLO-{side}-COLLECT-TEST", "stream_clean": clean,
              **config_overrides}
    if side == "right":
        config["channels"] = list(reversed(config["channels"]))
    transport = UsbTransport(FakeSerial(config, stream=tagged_burst(60), hz=250))
    info, caps = transport.read_config(interval=0.01, drain=0)
    return Glove(transport, info, caps)


class ScriptedDisplay:
    """Hands out one scripted key per listening frame; ``None`` means no key that frame.

    Once the script runs out it answers ``q`` so a test can never hang in the idle loop;
    during recording that ``q`` must be ignored, which the tests rely on.
    """

    def __init__(self, keys):
        self.keys = list(keys)
        self.shown = 0
        self.closed = False

    def show(self, image, listen=True):
        assert isinstance(image, np.ndarray) and image.ndim == 3
        self.shown += 1
        if not listen:
            return -1
        if not self.keys:
            return ord("q")
        key = self.keys.pop(0)
        return -1 if key is None else ord(key)

    def flush(self):
        pass  # Scripted keys are the test input; never drop them.

    def close(self):
        self.closed = True


def make_args(tmp_path, pair=True, **overrides):
    values = dict(out=tmp_path / "captures", task=TASK, camera=0, camera_backend="opencv", seconds=0.5,
                  fps=30, serial=None, pair=pair, sweep=1, clean=None, countdown=0,
                  max_delta_ms=50, skip_doctor=True, codec="mp4v", video_quality=23,
                  allow_shared_usb=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def patch_devices(monkeypatch, pair=True, clean=True):
    original = cv2.VideoCapture
    monkeypatch.setattr(collect.cv2, "VideoCapture",
                        lambda source: Camera() if isinstance(source, int) else original(source))
    monkeypatch.setattr(collect.oglo, "list_candidates", lambda: ["one"])  # Never the real USB bus.
    monkeypatch.setattr(collect.oglo, "connect", lambda **_: simulated_glove("left", clean))
    monkeypatch.setattr(collect.oglo, "connect_pair",
                        lambda: (simulated_glove("left", clean), simulated_glove("right", clean)))


def index_rows(out):
    return [json.loads(line) for line in (out / "episodes.jsonl").read_text().splitlines()]


def test_calibrate_record_to_cap_align_and_index(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    display = ScriptedDisplay("zg")  # No h: the 0.5 s cap ends the episode.
    collector = collect.Collector(make_args(tmp_path), display=display)
    episodes = collector.run()

    assert display.closed and not display.keys
    assert len(collector.last_calibration) == 2
    assert all(len(r["baseline"]) == 80 for r in collector.last_calibration.values())
    assert [(e["complete"], e["outcome"]) for e in episodes] == [(True, "saved")]

    out = tmp_path / "captures"
    session = out / SLUG / f"{SLUG}_001"
    assert episodes[0]["session"] == session
    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["stop_reason"] == "duration"
    assert len(manifest["gloves"]) == 2
    frames = manifest["camera"]["frames_decoded"]
    rows = (session / "camera/timestamps.jsonl").read_text().splitlines()
    assert len(rows) == frames
    preview = (session / "alignment.preview.jsonl").read_text().splitlines()
    assert len(preview) == frames
    assert not list(out.rglob("*.tar*"))  # Episodes are plain folders, nothing is packed.
    assert [(r["session"], r["frames"], r["aligned"]) for r in index_rows(out)] == [(f"{SLUG}_001", frames, True)]
    assert SLUG in (out / "README.md").read_text()
    assert (out / SLUG / collect.COUNTER).read_text().strip() == "1"


def test_alignment_runs_in_its_own_process(tmp_path, monkeypatch):
    """The in-process align() would hold the GIL against the glove readers and the camera
    of the next episode; collect.py runs align.py as a child instead."""
    patch_devices(monkeypatch)
    monkeypatch.setattr(collect.align, "align", lambda *a, **k: pytest.fail("align() ran in the collector"))
    spawned = []
    real = collect.subprocess.run

    def run(command, **kwargs):
        spawned.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(collect.subprocess, "run", run)
    collector = collect.Collector(make_args(tmp_path), display=ScriptedDisplay("g"))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved"]
    (command,) = spawned
    assert sys.executable in command and command[-1] == "--json"
    assert str(collect.HERE / "align.py") in command
    assert collector.status == f"{SLUG}_001 ok"
    assert (tmp_path / "captures" / SLUG / f"{SLUG}_001" / "alignment.preview.jsonl").is_file()


def test_pedal_stop_saves(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    display = ScriptedDisplay(["g"] + [None] * 15 + ["h"])
    collector = collect.Collector(make_args(tmp_path, seconds=5), display=display)
    episodes = collector.run()

    assert not display.keys and display.closed
    assert [e["outcome"] for e in episodes] == ["saved"]
    session = tmp_path / "captures" / SLUG / f"{SLUG}_001"
    manifest = json.loads((session / "manifest.json").read_text())
    assert manifest["complete"] is True and manifest["error"] is None
    assert manifest["stop_reason"] == "cancelled"
    assert manifest["requested_duration_s"] == 5
    frames = manifest["camera"]["frames_decoded"]
    assert 10 <= frames < 60  # Stopped by h, long before the 5 s cap.
    assert len((session / "alignment.preview.jsonl").read_text().splitlines()) == frames
    for entry in manifest["gloves"]:
        assert entry["summary"]["tactile"]["n"] > 0
    assert collector.status == f"{SLUG}_001 ok"


def test_pedal_discard_moves_session(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    collector = collect.Collector(make_args(tmp_path, seconds=5),
                                  display=ScriptedDisplay(["g"] + [None] * 10 + ["x"]))
    episodes = collector.run()

    out = tmp_path / "captures"
    assert [(e["complete"], e["outcome"]) for e in episodes] == [(False, "discarded")]
    moved = out / SLUG / "_discarded" / f"{SLUG}_001"
    assert episodes[0]["session"] == moved
    assert (moved / "manifest.json").is_file()
    assert not (out / SLUG / f"{SLUG}_001").exists()
    assert "discarded" in collector.status
    assert index_rows(out) == []  # Discarded episodes are not part of the dataset.
    assert collect.next_session_dir(out, TASK) == out / SLUG / f"{SLUG}_002"


def test_keys_are_ignored_while_recording(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    display = ScriptedDisplay(["g", "q", "z", "g"] + [None] * 10 + ["h"])
    collector = collect.Collector(make_args(tmp_path, seconds=5), display=display)
    episodes = collector.run()

    assert [e["outcome"] for e in episodes] == ["saved"]
    assert not collector.last_calibration  # z during recording did nothing.
    assert display.closed and not display.keys
    task = tmp_path / "captures" / SLUG
    assert (task / f"{SLUG}_001" / "alignment.preview.jsonl").is_file()
    assert not (task / f"{SLUG}_002").exists()


def test_preview_proxy_keeps_capture_output_intact(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False)
    display = ScriptedDisplay([None] * 1000)  # Listening, but never a decision.
    control = collect.RecordingControl()
    overlay = collect.RecordingOverlay(display, control, "session", "proxy", 0.5)
    args = argparse.Namespace(output=tmp_path / "session", camera=0, seconds=0.5, fps=30,
                              task="proxy", serial=None, pair=False, preview=False,
                              codec="mp4v", video_quality=23)
    root = collect.capture.capture(args, stop=control.stop, camera_factory=lambda a, o: (
        collect.OverlayCamera(a, o, overlay)))
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True and manifest["stop_reason"] == "duration"
    assert control.outcome is None
    # The previous frame is shown before each read; the sizing frame is the first shown.
    assert display.shown == manifest["camera"]["frames_submitted"]
    rows = [json.loads(line) for line in (root / "camera/timestamps.jsonl").read_text().splitlines()]
    assert all(row["host_read_started_ns"] < row["host_received_ns"] for row in rows)


def test_recording_control_takes_the_first_decision_only():
    control = collect.RecordingControl()
    control.press(ord("q"))
    control.press(ord("g"))
    assert control.outcome is None and not control.stop.is_set()
    control.press(ord("x"))
    control.press(ord("h"))
    assert control.outcome == "discard" and control.stop.is_set()


def test_parser_defaults_to_a_portable_codec_and_a_long_cap():
    args = collect.build_parser().parse_args(["--out", "x", "--task", "t"])
    assert (args.codec, args.video_quality, args.seconds) == ("mp4v", 23, 600)  # GPU codecs: workstation.env
    with pytest.raises(SystemExit):  # Packing is gone with the per-task layout.
        collect.build_parser().parse_args(["--out", "x", "--task", "t", "--no-pack"])
    with pytest.raises(SystemExit):  # A zero tolerance would fail every alignment.
        collect.build_parser().parse_args(["--out", "x", "--task", "t", "--max-delta-ms", "0"])


def test_sessions_are_numbered_per_task_and_never_reused(tmp_path):
    out = tmp_path / "captures"
    task = out / "pick_up_a_cup"
    assert collect.next_session_dir(out, "Pick up a cup!") == task / "pick_up_a_cup_001"
    (task / "pick_up_a_cup_001").mkdir(parents=True)
    (task / "_discarded" / "pick_up_a_cup_002").mkdir(parents=True)
    (task / "_failed" / "pick_up_a_cup_003").mkdir(parents=True)
    assert collect.next_session_dir(out, "Pick up a cup!") == task / "pick_up_a_cup_004"
    assert collect.next_session_dir(out, "Other task") == out / "other_task" / "other_task_001"
    # Numbers are handed out past the highest ever used: an episode deleted locally may
    # already be on the Hub, and a new one under its name would land in its folder there.
    collect.claim_session(task / "pick_up_a_cup_004")
    (task / "_failed" / "pick_up_a_cup_003").rmdir()
    assert collect.next_session_dir(out, "Pick up a cup!") == task / "pick_up_a_cup_005"
    (task / "pick_up_a_cup_001").rmdir()
    (task / "_discarded" / "pick_up_a_cup_002").rmdir()
    assert collect.next_session_dir(out, "Pick up a cup!") == task / "pick_up_a_cup_005"


def test_task_slug_keeps_distinct_tasks_apart():
    assert collect.task_slug("Pick up a cup!") == "pick_up_a_cup"
    assert collect.task_slug("Gloves") == "gloves_task"  # gloves/ is reserved for per-glove files
    assert collect.task_slug("") == "session"
    # Tasks in another script keep their identity through a hash instead of merging into "session".
    cup, bottle = collect.task_slug("컵 들기"), collect.task_slug("병 들기")
    assert cup != bottle and cup.startswith("task_") and len(cup) == len("task_") + 6
    assert collect.task_slug("컵 들기 ") == cup and collect.task_slug("cup 컵") != collect.task_slug("cup 병")
    # So do names the 40-character cut would merge, and names made of symbols only.
    sink = collect.task_slug("pick up the red cup from the table and place it in the sink")
    shelf = collect.task_slug("pick up the red cup from the table and place it on the shelf")
    assert sink != shelf and sink.startswith("pick_up_the_red_cup_from_the_table_and_p_")
    assert collect.task_slug("!!!") != collect.task_slug("???")
    assert collect.task_slug("!!!").startswith("task_")


def test_a_task_folder_belongs_to_one_wording(tmp_path):
    out = tmp_path / "captures"
    collect.check_task_folder(out, "Pick up a cup!")  # Nothing there yet.
    session = out / "pick_up_a_cup" / "_failed" / "pick_up_a_cup_001"
    session.mkdir(parents=True)
    (session / "manifest.json").write_text(json.dumps({"task_description": "Pick up a cup!", "complete": False}))
    collect.check_task_folder(out, "Pick up a cup!")
    collect.check_task_folder(out, "Pick up a cup!  ")
    with pytest.raises(RuntimeError, match="already holds episodes of 'Pick up a cup!'"):
        collect.check_task_folder(out, "pick-up a CUP")  # Same folder, another activity name.
    with pytest.raises(RuntimeError):
        collect.Collector(make_args(tmp_path, task="pick up a cup"), display=ScriptedDisplay("")).run()


def test_task_folder_checks_every_episode_wording(tmp_path):
    out = tmp_path / "captures"
    folder = out / "pick_up_a_cup"
    for number, wording in ((1, "Pick up a cup!"), (2, "pick-up a CUP")):
        session = folder / f"pick_up_a_cup_{number:03d}"
        session.mkdir(parents=True)
        (session / "manifest.json").write_text(json.dumps({"task_description": wording}))
    with pytest.raises(RuntimeError, match="already holds episodes of 'pick-up a CUP'"):
        collect.check_task_folder(out, "Pick up a cup!")


def test_a_failure_after_x_is_a_failure_not_a_discard(tmp_path, monkeypatch):
    patch_devices(monkeypatch)

    class DiscardingControl(collect.RecordingControl):
        def __init__(self, on_view=None):
            super().__init__(on_view)
            self.press(collect.KEY_DISCARD)  # x was pressed; then the capture dies.

    monkeypatch.setattr(collect, "RecordingControl", DiscardingControl)
    monkeypatch.setattr(collect.capture, "capture", failing_capture(oglo.DeviceError("port vanished after x")))
    collector = collect.Collector(make_args(tmp_path), display=ScriptedDisplay("g"))
    episodes = collector.run()
    out = tmp_path / "captures"
    assert [(e["outcome"], e["session"]) for e in episodes] == [("failed", out / SLUG / "_failed" / f"{SLUG}_001")]
    assert not (out / SLUG / "_discarded").exists()


def test_alignment_worker_shuts_down_and_the_publish_gate_holds_a_short_alignment(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    real_align = collect.Collector.align_session

    def short_align(self, session):
        result = real_align(self, session)
        output = session / collect.dataset.ALIGNMENT
        output.write_text(output.read_text().splitlines()[0] + "\n")  # One row for many frames.
        return {**result, "frames": 1}

    monkeypatch.setattr(collect.Collector, "align_session", short_align)
    collector = collect.Collector(make_args(tmp_path), display=ScriptedDisplay("g"))
    episodes = collector.run()
    out = tmp_path / "captures"
    assert [(e["outcome"], e["session"]) for e in episodes] == [("failed", out / SLUG / "_failed" / f"{SLUG}_001")]
    assert "not publishable after alignment: alignment has 1 rows" in collector.status
    assert index_rows(out) == []
    assert not collector._worker.is_alive()  # Explicit shutdown, not a daemon left behind.


def failing_capture(error):
    """A capture that dies mid-episode after writing an incomplete manifest, like capture.py."""
    def run(args, **kwargs):
        args.output.mkdir(parents=True)
        (args.output / "manifest.json").write_text(json.dumps({"complete": False, "error": str(error)}))
        raise error
    return run


def test_failed_episode_moves_to_failed_and_is_not_indexed(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    monkeypatch.setattr(collect.capture, "capture",
                        failing_capture(oglo.DeviceError("no '#TZERO ' from the board within 4s")))
    collector = collect.Collector(make_args(tmp_path), display=ScriptedDisplay("g"))
    episodes = collector.run()
    out = tmp_path / "captures"
    moved = out / SLUG / "_failed" / f"{SLUG}_001"
    assert [(e["complete"], e["outcome"], e["session"]) for e in episodes] == [(False, "failed", moved)]
    manifest = json.loads((moved / "manifest.json").read_text())
    assert manifest["complete"] is False and manifest["error"]
    assert not (out / SLUG / f"{SLUG}_001").exists()
    assert "FAILED" in collector.status
    assert index_rows(out) == []
    assert collect.next_session_dir(out, TASK) == out / SLUG / f"{SLUG}_002"


def test_another_collector_taking_the_folder_first_moves_nothing_aside(tmp_path, monkeypatch):
    """Two collectors on one --out: the second one's folder must not be filed as our failure."""
    patch_devices(monkeypatch)
    out = tmp_path / "captures"
    theirs = out / SLUG / f"{SLUG}_001"
    theirs.mkdir(parents=True)
    (theirs / "manifest.json").write_text(json.dumps({"task_description": TASK, "complete": False}))
    keys = ["g", "g"] + [None] * 15 + ["h"]
    collector = collect.Collector(make_args(tmp_path, seconds=5), display=ScriptedDisplay(keys))
    episodes = collector.run()
    assert [(e["outcome"], e["session"].name) for e in episodes] == [("saved", f"{SLUG}_002")]
    assert theirs.is_dir() and not (out / SLUG / "_failed").exists()


def test_dead_glove_is_waited_for_and_recording_resumes(tmp_path, monkeypatch, capsys):
    """After a failed episode the gloves are reconnected; a glove that needs a replug
    keeps the session alive until it answers again."""
    patch_devices(monkeypatch, pair=False)
    connects = []

    def connect(**_):
        connects.append(1)
        if 2 <= len(connects) <= 3:  # The first reconnect attempts find a hung board.
            raise oglo.UsbError("no #CONFIG from the board within 6s")
        return simulated_glove("right", clean=True)

    monkeypatch.setattr(collect.oglo, "connect", connect)
    real_capture = collect.capture.capture
    captures = iter([failing_capture(oglo.DeviceError("no '#TZERO ' within 4s")), real_capture])
    monkeypatch.setattr(collect.capture, "capture", lambda *a, **k: next(captures)(*a, **k))
    keys = ["g", None, "g"] + [None] * 15 + ["h"]

    reconnecting = []

    class PatientDisplay(ScriptedDisplay):
        def show(self, image, listen=True):  # Nobody touches the pedal while reconnecting.
            if listen and reconnecting:
                self.shown += 1
                return -1
            return super().show(image, listen)

    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=5), display=PatientDisplay(keys))
    collector.reconnect_pause = 0
    reconnect = collector.reconnect_gloves

    def tracked_reconnect():
        reconnecting.append(1)
        try:
            return reconnect()
        finally:
            reconnecting.clear()

    collector.reconnect_gloves = tracked_reconnect
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["failed", "saved"]
    assert len(connects) == 4  # open, two failed reconnects, the one that worked
    err = capsys.readouterr().err
    assert "reconnect attempt 1 failed" in err and "reconnect attempt 2 failed" in err
    assert "reconnect attempt 3 failed" not in err
    assert [r["session"] for r in index_rows(tmp_path / "captures")] == [f"{SLUG}_002"]


def test_quitting_while_the_glove_is_dead_ends_cleanly(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False)
    connects = []

    def connect(**_):
        connects.append(1)
        if len(connects) > 1:
            raise oglo.UsbError("no #CONFIG from the board within 6s")
        return simulated_glove("right", clean=True)

    monkeypatch.setattr(collect.oglo, "connect", connect)
    monkeypatch.setattr(collect.capture, "capture", failing_capture(oglo.DeviceError("dead")))
    display = ScriptedDisplay("g")  # Then q, from the exhausted script, while reconnecting.
    collector = collect.Collector(make_args(tmp_path, pair=False), display=display)
    collector.reconnect_pause = 0
    episodes = collector.run()  # No exception: the operator gave up.
    assert [e["outcome"] for e in episodes] == ["failed"]
    assert display.closed and collector.gloves == () and len(connects) >= 2
    assert index_rows(tmp_path / "captures") == []


def test_alignment_failure_also_moves_the_episode_aside(tmp_path, monkeypatch):
    patch_devices(monkeypatch)

    def broken_align(self, session):
        raise RuntimeError("no overlap")

    monkeypatch.setattr(collect.Collector, "align_session", broken_align)
    collector = collect.Collector(make_args(tmp_path, seconds=5),
                                  display=ScriptedDisplay(["g"] + [None] * 15 + ["h"]))
    episodes = collector.run()
    out = tmp_path / "captures"
    moved = out / SLUG / "_failed" / f"{SLUG}_001"
    assert [(e["outcome"], e["session"]) for e in episodes] == [("failed", moved)]
    assert (moved / "manifest.json").is_file() and not (out / SLUG / f"{SLUG}_001").exists()
    assert "align FAILED" in collector.status and "no overlap" in collector.status
    assert index_rows(out) == []


def test_a_failing_align_child_is_reported_by_its_last_line(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    monkeypatch.setattr(collect.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="Traceback...\nValueError: Unordered imu host timestamps\n"))
    collector = collect.Collector(make_args(tmp_path), display=ScriptedDisplay("g"))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["failed"]
    assert collector.status.endswith("align FAILED: ValueError: Unordered imu host timestamps")


def test_ctrl_c_during_recording_moves_the_episode_aside(tmp_path, monkeypatch):
    patch_devices(monkeypatch)

    def interrupted_capture(args, **kwargs):
        args.output.mkdir(parents=True)
        (args.output / "manifest.json").write_text(json.dumps({"complete": False}))
        raise KeyboardInterrupt

    monkeypatch.setattr(collect.capture, "capture", interrupted_capture)
    collector = collect.Collector(make_args(tmp_path, seconds=5), display=ScriptedDisplay("g"))
    with pytest.raises(KeyboardInterrupt):
        collector.run()
    out = tmp_path / "captures"
    moved = out / SLUG / "_failed" / f"{SLUG}_001"
    assert (moved / "manifest.json").is_file() and not (out / SLUG / f"{SLUG}_001").exists()
    assert [(e["outcome"], e["session"]) for e in collector.episodes] == [("failed", moved)]
    assert collector.interrupted_session == moved
    assert collector.display.closed
    assert index_rows(out) == []  # run() still closed devices and refreshed the index.


def test_ctrl_c_during_the_alignment_wait_keeps_the_episode_for_the_next_run(tmp_path, monkeypatch, capsys):
    """A complete episode whose alignment was interrupted stays where it is, unaligned
    and unindexed, and the next collect.py run aligns it before anything else."""
    patch_devices(monkeypatch)
    out = tmp_path / "captures"
    session = out / SLUG / f"{SLUG}_001"
    interrupted = []

    def killed_child(self, session):  # The align.py child got the same Ctrl-C.
        interrupted.append(session)
        raise collect.AlignmentInterrupted(session)

    monkeypatch.setattr(collect.Collector, "align_session", killed_child)
    collector = collect.Collector(make_args(tmp_path), display=ScriptedDisplay("g"))

    def interrupt():
        raise KeyboardInterrupt

    collector._jobs.join = interrupt
    with pytest.raises(KeyboardInterrupt):
        collector.run()
    assert collector.unaligned == [session] and collector.interrupted_session is None
    assert json.loads((session / "manifest.json").read_text())["complete"] is True
    assert not (session / "alignment.preview.jsonl").exists() and not (out / SLUG / "_failed").exists()
    assert collector.display.closed and index_rows(out) == []
    assert not collector._worker.is_alive()
    assert "not aligned yet" in capsys.readouterr().err

    monkeypatch.undo()
    patch_devices(monkeypatch)
    again = collect.Collector(make_args(tmp_path), display=ScriptedDisplay(""))
    # Nothing new recorded; the leftover was aligned and counts as saved by this run.
    assert [(e["outcome"], e["session"]) for e in again.run()] == [("saved", session)]
    assert "complete but not aligned yet" in capsys.readouterr().out
    assert (session / "alignment.preview.jsonl").is_file()
    assert [r["session"] for r in index_rows(out)] == [f"{SLUG}_001"]


def test_preview_is_shrunk_but_the_recording_keeps_full_size(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    monkeypatch.setattr(collect, "PREVIEW_WIDTH", 32)  # Camera frames are 64x48.

    class SizeCheckingDisplay(ScriptedDisplay):
        def show(self, image, listen=True):
            assert image.shape[:2] == (24, 32)
            return super().show(image, listen)

    collector = collect.Collector(make_args(tmp_path, seconds=5),
                                  display=SizeCheckingDisplay(["z", "g"] + [None] * 10 + ["h"]))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved"]
    manifest = json.loads((tmp_path / "captures" / SLUG / f"{SLUG}_001" / "manifest.json").read_text())
    assert (manifest["camera"]["width"], manifest["camera"]["height"]) == (64, 48)
    assert collect.fit_preview(np.zeros((48, 64, 3), np.uint8)).shape == (24, 32, 3)
    monkeypatch.setattr(collect, "PREVIEW_WIDTH", 1280)
    assert collect.fit_preview(np.zeros((48, 64, 3), np.uint8)).shape == (48, 64, 3)  # Never enlarged.
    args = collect.build_parser().parse_args(["--out", "x", "--task", "t"])
    assert args.preview_width == 1280


class BigCamera(Camera):
    """Frames large enough to carry the finger grids; counts FPS requests."""

    def __init__(self):
        super().__init__()
        self.fps_requests = 0

    def set(self, *_):
        self.fps_requests += 1
        return False

    def read(self):
        ok, image = super().read()
        return ok, np.full((480, 640, 3), self.count % 256, dtype=np.uint8)


def test_devices_stay_open_across_episodes_and_grids_are_drawn(tmp_path, monkeypatch):
    """One camera and one glove connection serve idle and every episode; the overlay shows tactile."""
    opened = []
    connects = []
    original = cv2.VideoCapture
    monkeypatch.setattr(collect.cv2, "VideoCapture", lambda source: (
        opened.append(BigCamera()) or opened[-1]) if isinstance(source, int) else original(source))
    monkeypatch.setattr(collect.oglo, "list_candidates", lambda: ["one"])
    monkeypatch.setattr(collect.oglo, "connect",
                        lambda **_: connects.append(1) or simulated_glove("left"))

    grids_while_recording = []
    shapes = set()

    class GridDisplay(ScriptedDisplay):
        collector = None

        def show(self, image, listen=True):
            if self.keys and self.keys[0] in ("h", None):  # Recording frames only.
                grids_while_recording.append(image[-collect.GRID_H - 10:-10, 10:10 + collect.GRID_W].copy())
                glove = self.collector.gloves[0]
                if glove.latest is not None:  # Filled by the recorder's own reads.
                    shapes.add((glove.latest.counts.shape, self.collector.grid_values(glove).shape))
            return super().show(image, listen)

    display = GridDisplay(["g"] + [None] * 15 + ["h", "g"] + [None] * 15 + ["h"])  # >= one fake burst each
    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=5), display=display)
    display.collector = collector
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved", "saved"]
    assert len(opened) == 1 and connects == [1]  # Never reopened between episodes.
    assert opened[0].fps_requests == 1  # Asked once, before the camera streamed; not per episode.
    manifest = json.loads((tmp_path / "captures" / SLUG / f"{SLUG}_001" / "manifest.json").read_text())
    assert manifest["camera"]["fps_request_accepted"] is False and manifest["camera"]["backend"] == "synthetic"
    assert shapes == {((5, 4, 4), (5, 4, 4))}
    assert grids_while_recording and all(patch.shape == (collect.GRID_H, collect.GRID_W, 3) for patch in grids_while_recording)
    assert any(patch.std() > 0 for patch in grids_while_recording)  # Drawn grids, not the flat camera frame.
    assert collect.finger_grids(np.zeros((5, 4, 4))).shape == (collect.GRID_H, collect.GRID_W, 3)
    # Numbered cells: a hot taxel is bright, a value under thr is drawn like zero,
    # and a negative one (below the calibrated zero) is blue, never black.
    values = np.zeros((5, 4, 4))
    values[0, 0, 0], values[1, 0, 0], values[2, 0, 0] = 1400, 50, -30
    image = collect.finger_grids(values, thr=70)
    # oriented (row 0, col 0) lands top-right on screen: tip at the top, row 0 on the right.
    cell = lambda finger: image[collect.CELL // 2, finger * (4 * collect.CELL + collect.GRID_GAP) + 3 * collect.CELL + 2]
    assert cell(0).sum() > 500 and np.array_equal(cell(1), cell(3))
    assert cell(2)[0] > cell(2)[2]  # BGR: blue dominates.
    # The heat scale is per count, not per cell: a light touch after a hot cell stays dim.
    values = np.zeros((5, 4, 4))
    values[0, 0, 0], values[0, 1, 0] = 1400, 5
    image = collect.finger_grids(values)
    hot, light = image[3, 3 * collect.CELL + 3], image[3, 2 * collect.CELL + 3]  # cell corners, no text
    assert int(hot.sum()) > int(light.sum()) + 300
    assert [r["session"] for r in index_rows(tmp_path / "captures")] == [f"{SLUG}_001", f"{SLUG}_002"]


def test_two_attached_gloves_are_recorded_as_a_pair_unless_a_serial_is_given(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False)
    monkeypatch.setattr(collect.oglo, "list_candidates", lambda: ["a", "b"])
    monkeypatch.setattr(collect.oglo, "connect", lambda **_: pytest.fail("two gloves must pair"))
    collector = collect.Collector(make_args(tmp_path, pair=False), display=ScriptedDisplay("g"))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved"]
    manifest = json.loads((tmp_path / "captures" / SLUG / f"{SLUG}_001" / "manifest.json").read_text())
    assert sorted(g["side"] for g in manifest["gloves"]) == ["left", "right"]

    chosen = []
    monkeypatch.setattr(collect.oglo, "connect", lambda **kw: chosen.append(kw["serial"]) or simulated_glove("right"))
    monkeypatch.setattr(collect.oglo, "connect_pair", lambda: pytest.fail("--serial picks one glove"))
    collect.Collector(make_args(tmp_path, pair=False, serial="OGLO-R-TEST01"), display=ScriptedDisplay("")).run()
    assert chosen == ["OGLO-R-TEST01"]


def test_every_glove_stream_is_stopped_before_an_episode_starts(tmp_path, monkeypatch):
    """A glove nobody reads while its sibling is commanded wedges the firmware."""
    patch_devices(monkeypatch, pair=True)
    streaming_at_capture = []
    real_capture = collect.capture.capture

    def capture(namespace, **kwargs):
        streaming_at_capture.append([(g._started, g._thread is not None) for g in kwargs["gloves"]])
        return real_capture(namespace, **kwargs)

    monkeypatch.setattr(collect.capture, "capture", capture)
    keys = ["g"] + [None] * 15 + ["h"]
    collector = collect.Collector(make_args(tmp_path, seconds=5), display=ScriptedDisplay(keys))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved"]
    assert streaming_at_capture == [[(False, False), (False, False)]]  # no stream, no reader thread


def test_idle_gloves_are_drained_by_reader_threads_not_the_window(tmp_path, monkeypatch):
    """Linux holds 4095 bytes per tty; a window thread busy with the camera cannot be the reader."""
    patch_devices(monkeypatch, pair=True)
    frames_seen, readers_seen = [], []

    class Watching(ScriptedDisplay):
        collector = None

        def show(self, image, listen=True):
            if listen:  # An idle frame: the window thread never called read_batch itself.
                frames_seen.append(tuple(g.latest is not None for g in self.collector.gloves))
                readers_seen.append(tuple(g._thread is not None and g._thread.is_alive() for g in self.collector.gloves))
            return super().show(image, listen)

    display = Watching([None] * 12 + ["z"] + [None] * 5 + ["g"] + [None] * 15 + ["h"] + [None] * 5)
    collector = Watching.collector = collect.Collector(make_args(tmp_path, seconds=5, countdown=0, sweep=1),
                                                       display=display)
    real_zero = oglo.Glove.zero
    threads_at_zero = []

    def zero(self, **kwargs):  # The sweep runs with no reader thread alive on any glove.
        threads_at_zero.append([g._thread for g in collector.gloves])
        return real_zero(self, **kwargs)

    monkeypatch.setattr(oglo.Glove, "zero", zero)
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved"]
    assert (True, True) in frames_seen  # tactile frames arrived while the window only drew
    assert all(readers_seen)  # a reader thread was alive on every idle frame
    assert frames_seen[-1] == (True, True) and readers_seen[-1] == (True, True)  # restarted after the episode
    assert threads_at_zero == [[None, None], [None, None]]  # and stopped for the sweep
    assert all(g._thread is None for g in collector.gloves) or collector.gloves == ()


def test_a_reader_failure_surfaces_on_the_window_thread(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False)
    glove = collect.TactilePeek(simulated_glove("left"))
    glove.start_reader()
    for _ in range(200):
        if glove.latest is not None:
            break
        time.sleep(0.01)
    assert glove.latest is not None
    glove.check()
    glove._glove.close()  # The port vanished under the reader.
    for _ in range(200):
        if glove.error is not None:
            break
        time.sleep(0.01)
    with pytest.raises(Exception):
        glove.check()
    glove.stop_reader()
    assert glove._thread is None


def test_a_glove_on_the_camera_usb_controller_is_refused_unless_allowed(tmp_path, monkeypatch, capsys):
    assert collect.usb_bus("/nonexistent/sysfs/link") is None  # Not Linux USB: no check, no crash.
    patch_devices(monkeypatch)
    monkeypatch.setattr(collect.oglo, "list_candidates",
                        lambda: ["/dev/ttyACM1"])  # Plain device paths are accepted too.
    monkeypatch.setattr(collect, "usb_bus", lambda link: "usb1")  # Camera and glove: same hub.
    with pytest.raises(RuntimeError, match="/dev/ttyACM1 .*shares its USB controller with camera 0"):
        collect.Collector(make_args(tmp_path), display=ScriptedDisplay([])).run()
    args = make_args(tmp_path, allow_shared_usb=True)
    assert collect.Collector(args, display=ScriptedDisplay([])).run() == []


def test_screen_grid_matches_studio_layout():
    # Studio (viewer-core/usb.html drawGlove): idx = f*16 + row*4 + col; col is the
    # vertical axis with col 0 (fingertip) at the top, row is horizontal with row 0
    # on the right. So wire (row 0, col 0) is top-right and (row 3, col 3) bottom-left.
    block = np.arange(16).reshape(4, 4)  # block[row][col] = row*4 + col
    screen = collect.screen_grid(block)
    assert screen[0, 3] == 0 and screen[3, 0] == 15
    assert screen[0, 0] == 12  # row 3, col 0: fingertip, left edge
    assert list(screen[:, 3]) == [0, 1, 2, 3]  # walking down the right edge = along col


def test_g_is_refused_until_every_glove_has_a_zero(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False)
    monkeypatch.setattr(collect.oglo, "connect",
                        lambda **_: simulated_glove("left", clean=False, zero_valid=False))
    display = ScriptedDisplay("gg")  # then q
    collector = collect.Collector(make_args(tmp_path, pair=False), display=display)
    episodes = collector.run()
    assert episodes == []
    assert collector.status.startswith("refused: no zero on OGLO-left-COLLECT-TEST")
    assert not (tmp_path / "captures" / SLUG).exists()


def test_c_toggles_the_raw_view_without_touching_the_recording(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False, clean=False)
    display = ScriptedDisplay("c")  # then q
    collector = collect.Collector(make_args(tmp_path, pair=False), display=display)
    assert collector.show_raw is False
    collector.run()
    assert collector.show_raw is True

    # Grid values follow the view; the same frame, two readings.
    glove = simulated_glove("left", clean=False)
    peek = collect.TactilePeek(glove)
    glove.start()
    frame = next(iter(glove.tactile(timeout=1.0)))
    peek.latest = frame
    collector.gloves = (peek,)
    collector.baselines = {glove.info.serial: np.full(80, 500, dtype=np.float32)}
    collector.show_raw = False
    cal = collector.grid_values(peek)
    collector.show_raw = True
    raw = collector.grid_values(peek)
    assert np.allclose(raw - cal, 500)
    # The sweep zero is an envelope, so a resting taxel sits under it: the calibrated
    # view clamps at 0 like the CLEAN file, the raw view still shows the ADC as sent.
    collector.baselines = {glove.info.serial: raw.reshape(80) + 40}
    collector.show_raw = False
    assert np.all(collector.grid_values(peek) == 0)
    collector.show_raw = True
    assert np.allclose(collector.grid_values(peek), raw)
    label, values, thr, scale = collector.glove_grids()[0]
    assert label == "LEFT RAW ADC" and thr == 0 and scale == collect.RAW_FULL
    glove.close()

    # During an episode c is display-only: never a stop decision.
    toggled = []
    control = collect.RecordingControl(on_view=lambda: toggled.append(True))
    control.press(ord("c"))
    assert toggled == [True] and control.outcome is None and not control.stop.is_set()


def test_z_switching_a_raw_glove_to_clean_never_reads_the_old_frame(tmp_path, monkeypatch):
    """--clean on a glove that streams RAW: z flips the mode on the device. The frame
    the reader saw before the sweep is RAW; read back as a residual it raises
    CleanStreamError and ends the session. On hardware the window draws its next
    frame (~33 ms) before the restarted reader's first read returns (~50 ms), so a
    stopped stream must keep no newest frame. ``_calibrate`` (readers left stopped)
    is that ordering made deterministic."""
    patch_devices(monkeypatch, pair=False, clean=False)
    collector = collect.Collector(make_args(tmp_path, pair=False, clean=70),
                                  display=ScriptedDisplay(""))
    collector.open_devices()
    try:
        glove = collector.gloves[0]
        deadline = time.monotonic() + 2.0
        while glove.latest is None and time.monotonic() < deadline:  # The idle reader is at work.
            time.sleep(0.01)
        assert glove.latest is not None and not glove.info.stream_clean
        collector._calibrate()
        assert glove.info.stream_clean and collector.status == "calibrated left"
        assert collector.grid_values(glove) is None  # Nothing to show until a CLEAN frame arrives.
    finally:
        collector.close_devices()


def test_z_leaves_the_glove_raw_unless_clean_is_given(tmp_path, monkeypatch, capsys):
    """After the sweep the glove is put in RAW, as OGLO Studio leaves it: the recorder
    keeps RAW and derives CLEAN. That persists on the device, so the tool says so."""
    patch_devices(monkeypatch, pair=False, clean=True)
    collector = collect.Collector(make_args(tmp_path, pair=False), display=ScriptedDisplay(""))
    collector.open_devices()
    try:
        glove = collector.gloves[0]
        assert glove.info.stream_clean
        collector._calibrate()
        assert not glove.info.stream_clean
        assert "SET STREAM RAW" in glove._glove._t._s.commands
        assert "mode RAW (kept on the glove)" in capsys.readouterr().out
    finally:
        collector.close_devices()
    assert "Default: RAW, as OGLO Studio leaves it" in collect.build_parser().format_help()


def test_camera_is_resolved_by_v4l2_name_to_its_capture_node(tmp_path):
    sysfs = tmp_path / "video4linux"
    for index, card in [(0, "ZXCZ SC233HGS Dual: UVC Camera"), (1, "ZXCZ SC233HGS Dual: UVC Camera"),
                        (2, "UGREEN Camera 4K: UGREEN Camera"), (3, "UGREEN Camera 4K: UGREEN Camera")]:
        (sysfs / f"video{index}").mkdir(parents=True)
        (sysfs / f"video{index}" / "name").write_text(card + "\n")
    capture = lambda index: index in (0, 2)  # odd nodes are UVC metadata, like real hardware
    assert collect.resolve_camera("sc233", sysfs, capture) == (0, "ZXCZ SC233HGS Dual: UVC Camera")
    assert collect.resolve_camera("UGREEN", sysfs, capture) == (2, "UGREEN Camera 4K: UGREEN Camera")
    assert collect.resolve_camera("2", sysfs, capture) == (2, None)  # a number is taken as given
    with pytest.raises(SystemExit, match="no capture node with that name; found: video0 = ZXCZ"):
        collect.resolve_camera("OVISION", sysfs, capture)
    with pytest.raises(SystemExit):
        collect.resolve_camera("-1", sysfs, capture)
    # After a replug the same name can come back on another node; find_camera is what reconnects use.
    (sysfs / "video0").rename(sysfs / "video4")
    match, seen = collect.find_camera("sc233", sysfs, lambda index: index in (2, 4))
    assert match == (4, "ZXCZ SC233HGS Dual: UVC Camera") and len(seen) == 4
    assert collect.find_camera("OVISION", sysfs, capture)[0] is None


# -- the native OVISION backend ------------------------------------------------------

def patch_ovision(monkeypatch, **stream_kwargs):
    """collect.py's OVISION backend on FakeOvisionStream, under the SDK's real worker;
    returns the streams the worker built (one per camera open)."""
    if sys.platform != "linux":
        pytest.skip("the SDK's native OVISION worker is Linux only")
    native = pytest.importorskip("syncfield.adapters.ovision_camera")
    from fake_ovision import fake_stream_class

    streams = []
    monkeypatch.setattr(native, "OvisionCameraStream", fake_stream_class(streams, **stream_kwargs))
    monkeypatch.setattr(collect.ovision, "problem", lambda device: None)
    monkeypatch.setattr(collect, "OVISION_IDLE_PERIOD", 0.01)
    return streams


def test_ovision_backend_keeps_one_worker_and_saves_the_camera_imu_per_episode(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    streams = patch_ovision(monkeypatch)
    keys = (["g"] + [None] * 15 + ["h"] + ["g"] + [None] * 15 + ["x"] + ["g"] + [None] * 15 + ["h"])
    display = ScriptedDisplay(keys)
    collector = collect.Collector(make_args(tmp_path, seconds=5, camera_backend="ovision"), display=display)
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved", "discarded", "saved"]

    (stream,) = streams  # Opened once; every episode records through it and leaves it live.
    assert stream.connects == 1 and stream.disconnects == 1 and not stream.connected  # Closed on quit.
    assert stream.video_device == Path("/dev/video0") and stream.usb_serial is None
    out = tmp_path / "captures"
    saved = [out / SLUG / f"{SLUG}_001", out / SLUG / f"{SLUG}_003"]
    discarded = out / SLUG / "_discarded" / f"{SLUG}_002"
    assert len(stream.recordings) == 3 and len(set(stream.recordings)) == 3
    assert (discarded / "camera" / "cam_ego.mp4").is_file()
    assert not (out / ".ovision-pending").exists()  # The worker's placeholder folder is never written.
    for session in saved:
        manifest = json.loads((session / "manifest.json").read_text())
        camera = manifest["camera"]
        assert manifest["complete"] is True and camera["kind"] == "ovision"
        assert (camera["codec"], camera["width"], camera["height"]) == ("h264_passthrough", 3840, 1080)
        assert camera["video_device"] == "/dev/video0" and camera["backend"] == "SyncField OVISION 0.8.14 V4L2"
        for key in ("video", "timestamps", "native_stereo_metadata", "calibration", "imu", "accel",
                    "gyro", "mag", "sync_point", "finalization"):
            assert (session / camera[key]).is_file(), key
        assert all((session / path).is_file() for path in camera["native_artifacts"])
        frames = camera["frames_decoded"]
        assert frames >= 2
        assert len((session / "camera/timestamps.jsonl").read_text().splitlines()) == frames
        assert len((session / "camera/cam_ego.stereo.jsonl").read_text().splitlines()) == frames
        assert len((session / "camera/cam_ego.imu.jsonl").read_text().splitlines()) == frames
        assert len((session / "alignment.preview.jsonl").read_text().splitlines()) == frames
        report = json.loads((session / "camera/finalization.json").read_text())
        assert report["status"] == "completed" and report["health_events"] == []
    rows = index_rows(out)
    assert [(r["session"], r["camera_kind"], r["camera_imu"], r["fps"], r["codec"], r["width"]) for r in rows] == [
        (f"{SLUG}_001", "ovision", True, 30.0, "h264_passthrough", 3840),
        (f"{SLUG}_003", "ovision", True, 30.0, "h264_passthrough", 3840)]
    card = (out / "README.md").read_text()
    assert "through its native backend" in card and "camera/cam_ego.imu.jsonl" in card
    assert "3200x1200" not in card
    assert "camera/video.mp4" not in card  # An OVISION-only dataset describes only what it holds.


def test_ovision_camera_failure_moves_the_episode_aside_and_reopens_the_camera(tmp_path, monkeypatch):
    patch_devices(monkeypatch, pair=False)
    streams = patch_ovision(monkeypatch, fail_after=5)  # The "USB device" vanishes 5 frames in.
    keys = ["g"] + [None] * 60 + ["g"] + [None] * 15 + ["h"]
    display = ScriptedDisplay(keys)
    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=5, camera_backend="ovision"),
                                  display=display)
    collector.reconnect_pause = 0
    reconnect = collector.reconnect_camera

    def reconnect_a_healthy_camera(reason):
        back = reconnect(reason)
        streams[-1].fail_after = None  # The camera that came back does not vanish again.
        return back

    collector.reconnect_camera = reconnect_a_healthy_camera
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["failed", "saved"]
    dead, live = streams  # The SDK worker keeps its error, so a fresh one was opened, before the gloves were.
    assert dead.disconnects == 1 and live.recordings and not live.connected
    out = tmp_path / "captures"
    failed = out / SLUG / "_failed" / f"{SLUG}_001"
    manifest = json.loads((failed / "manifest.json").read_text())
    assert manifest["complete"] is False and "OVISION finalization failed" in manifest["error"]
    report = json.loads((failed / "camera/finalization.json").read_text())
    assert report["status"] == "failed" and "vanished" in report["error"]
    assert report["health_events"] == [{"kind": "error", "detail": "fake USB device vanished"}]
    saved = json.loads((out / SLUG / f"{SLUG}_002" / "manifest.json").read_text())
    assert saved["complete"] is True and saved["camera"]["frames_decoded"] >= 2
    assert json.loads((out / SLUG / f"{SLUG}_002" / "camera/finalization.json").read_text())["health_events"] == []
    assert [r["session"] for r in index_rows(out)] == [f"{SLUG}_002"]


def test_ovision_camera_that_stops_delivering_frames_fails_the_episode(tmp_path, monkeypatch):
    """A wedged camera keeps its capture thread alive and reports no error; the SDK
    worker's watchdog ends the recording after five silent seconds and the episode is
    not published with a video shorter than its gloves."""
    patch_devices(monkeypatch, pair=False)
    streams = patch_ovision(monkeypatch, stall_after=5)
    display = ScriptedDisplay(["g"] + [None] * 200)  # 10 s of listening frames, then q
    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=30, camera_backend="ovision"),
                                  display=display)
    collector.reconnect_pause = 0
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["failed"]
    assert "five seconds" in collector.status or "five seconds" in json.loads(
        (tmp_path / "captures" / SLUG / "_failed" / f"{SLUG}_001" / "manifest.json").read_text())["error"]
    assert len(streams) == 2 and streams[0].disconnects == 1  # Reopened after the failure.


def test_ovision_camera_that_stops_while_idle_is_reopened(tmp_path, monkeypatch, capsys):
    patch_devices(monkeypatch, pair=False)
    streams = patch_ovision(monkeypatch)
    monkeypatch.setattr(collect, "OVISION_STALL_SECONDS", 0.3)

    class StallingDisplay(ScriptedDisplay):
        def show(self, image, listen=True):
            if listen and self.shown == 30:
                streams[0].stall_after = 0  # From now on the camera sends no keyframe.
            return super().show(image, listen)

    collector = collect.Collector(make_args(tmp_path, pair=False, camera_backend="ovision"),
                                  display=StallingDisplay([None] * 120))
    collector.reconnect_pause = 0
    assert collector.run() == []
    assert len(streams) == 2 and streams[0].disconnects == 1 and streams[1].disconnects == 1
    assert "no keyframe from the camera for 0.3 s" in capsys.readouterr().err
    assert collector.status == "camera reconnected on /dev/video0"


def test_ovision_stop_before_the_first_keyframe_is_a_discard_not_a_failure(tmp_path, monkeypatch):
    """The adapter records from the first IDR after start (up to a second on this
    firmware); h or x before it leaves 0 or 1 frames, which is nothing to keep, not a
    device failure: no episode under _failed/, no camera or glove reconnect."""
    patch_devices(monkeypatch, pair=False)
    connects = []
    monkeypatch.setattr(collect.oglo, "connect", lambda **_: connects.append(1) or simulated_glove("left"))
    streams = patch_ovision(monkeypatch, keyframe_every=10_000)
    keys = ["g", None, "x", "g", None, "h"]
    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=5, camera_backend="ovision"),
                                  display=ScriptedDisplay(keys))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["discarded", "discarded"]
    out = tmp_path / "captures"
    assert sorted(p.name for p in (out / SLUG / "_discarded").iterdir()) == [f"{SLUG}_001", f"{SLUG}_002"]
    assert not (out / SLUG / "_failed").exists()
    assert "0 camera frame(s) before the stop" in collector.status
    assert len(streams) == 1 and connects == [1]  # Nothing was reconnected.
    report = json.loads((out / SLUG / "_discarded" / f"{SLUG}_002" / "camera/finalization.json").read_text())
    assert report["frame_count"] == 0 and report["error"] is None


def test_ovision_unplugged_camera_is_waited_for_and_found_again(tmp_path, monkeypatch, capsys):
    patch_devices(monkeypatch, pair=False)
    unplugged = {"now": False}
    streams = patch_ovision(monkeypatch, fail_after=5, unplugged=unplugged)
    monkeypatch.setattr(collect.ovision, "problem",
                        lambda device: f"{device} did not answer" if unplugged["now"] else None)
    reconnect_frames = []

    class ReplugDisplay(ScriptedDisplay):
        def show(self, image, listen=True):
            if listen and unplugged["now"]:
                reconnect_frames.append(1)
                if len(reconnect_frames) >= 12:
                    unplugged["now"] = False  # Plugged back in, on the same node.
                self.shown += 1
                return -1
            return super().show(image, listen)

    keys = ["g"] + [None] * 60 + ["g"] + [None] * 15 + ["h"]
    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=5, camera_backend="ovision"),
                                  display=ReplugDisplay(keys))
    collector.reconnect_pause = 0
    reconnect = collector.reconnect_camera

    def unplug_then_reconnect(reason):
        unplugged["now"] = True  # The failure was the cable coming out.
        back = reconnect(reason)
        for stream in streams:
            stream.fail_after = None  # The replugged camera is fine.
        return back

    collector.reconnect_camera = unplug_then_reconnect
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["failed", "saved"]
    assert len(streams) == 2  # No stream was built while the node was gone; problem() said so first.
    err = capsys.readouterr().err
    assert "camera reconnect attempt 1 failed: /dev/video0 did not answer" in err
    assert "Camera is back." in capsys.readouterr().out or len(reconnect_frames) >= 12


def test_ovision_idle_source_paces_the_window_and_reports_a_dead_stream():
    from fake_ovision import FakeOvisionStream

    stream = FakeOvisionStream("cam_ego", Path("/nonexistent"), keyframe_every=2, frame_hz=200)
    worker = SimpleNamespace(stream=stream, error=None, close=stream.disconnect)
    source = collect.OvisionIdleSource(worker, period=0.001, stall_seconds=0.3)
    assert source.read() == (False, None)  # Not connected: reads like an unplugged camera.
    stream.connect()
    for _ in range(200):  # Ready, then the first keyframe: a placeholder keeps the window alive until then.
        ok, image = source.read()
        if ok and image.shape == (1080, 1920, 3):
            break
        assert not ok or image.shape == (720, 1280, 3)
    else:
        pytest.fail("no keyframe arrived")
    stream.error = stream._capture_error = "vanished"  # As the adapter records a dead capture thread.
    assert source.read() == (False, None) and source.reason == "vanished"
    stream.error = stream._capture_error = None
    worker.error = "OVISION stopped returning frames for five seconds"  # The SDK worker's own verdict.
    assert source.read() == (False, None) and "five seconds" in source.reason
    worker.error = None
    stream.stall_after = 0  # Alive, no error, but no new keyframe: a wedged camera.
    time.sleep(0.4)
    assert source.read() == (False, None) and "no keyframe" in source.reason
    source.release()
    assert not stream.connected


def test_recording_overlay_draws_the_last_frame_or_a_placeholder_and_routes_keys():
    display = ScriptedDisplay([None, "c", "h", None])
    views = []
    control = collect.RecordingControl(on_view=lambda: views.append(1))
    overlay = collect.RecordingOverlay(display, control, "s_001", "task", 5)
    overlay.show(None)  # No frame yet: still shown, still listening.
    overlay.show(np.zeros((48, 64, 3), np.uint8))
    overlay.show(None)  # Keeps the last real frame.
    overlay.progress(30, 300)  # The post-stop video check keeps the window alive too.
    assert display.shown == 4 and views == [1] and control.outcome == "save" and control.stop.is_set()


def test_camera_backend_is_chosen_from_syncfield_and_the_camera(monkeypatch):
    real_problem = collect.ovision.problem
    monkeypatch.setattr(collect.ovision, "problem", lambda device: f"{device} is a webcam")
    assert collect.choose_backend("opencv", 0) == ("opencv", None)
    assert collect.choose_backend("auto", 3) == ("opencv", "/dev/video3 is a webcam")
    with pytest.raises(RuntimeError, match="webcam"):
        collect.choose_backend("ovision", 3)
    monkeypatch.setattr(collect.ovision, "problem", lambda device: None)
    assert collect.choose_backend("auto", 0) == ("ovision", None)
    # Exercise the Linux-only version checks independently of the CI host OS.
    monkeypatch.setattr(collect.ovision.sys, "platform", "linux")
    monkeypatch.setattr(collect.ovision, "version", lambda name: "0.9.0")
    assert "targets 0.8.14" in real_problem(Path("/dev/video0"))

    def missing(name):
        raise collect.ovision.PackageNotFoundError(name)

    monkeypatch.setattr(collect.ovision, "version", missing)
    assert "requirements-ovision.txt" in real_problem(Path("/dev/video0"))


def test_main_resolves_the_backend_before_any_device_is_opened(tmp_path, monkeypatch, capsys):
    seen = {}

    class StubCollector:
        def __init__(self, args):
            seen["args"] = args

        def run(self):
            return []

    monkeypatch.setattr(collect, "Collector", StubCollector)
    monkeypatch.setattr(collect.capture, "probe_encoder",
                        lambda *a: pytest.fail("the OVISION backend never probes an encoder"))
    monkeypatch.setattr(collect.ovision, "problem", lambda device: None)
    argv = ["--out", str(tmp_path), "--task", "t", "--camera", "4", "--skip-doctor"]
    assert collect.main(argv) == 0
    assert seen["args"].camera_backend == "ovision" and seen["args"].camera == 4
    assert seen["args"].camera_spec == "4"  # Kept as typed, for a reconnect after a replug.
    assert "native OVISION" in capsys.readouterr().out

    monkeypatch.setattr(collect.ovision, "problem", lambda device: "/dev/video4 is not an OVISION")
    assert collect.main(argv + ["--camera-backend", "ovision"]) == 2
    assert "not an OVISION" in capsys.readouterr().err
    monkeypatch.setattr(collect.capture, "probe_encoder", lambda *a: None)
    assert collect.main(argv) == 0
    assert seen["args"].camera_backend == "opencv"
    assert seen["args"].backend_reason == "/dev/video4 is not an OVISION"  # Shown in the idle window too.
    assert "camera IMU is not recorded: /dev/video4 is not an OVISION" in capsys.readouterr().out
    assert collect.build_parser().parse_args(["--out", "x", "--task", "t"]).camera_backend == "auto"


def test_main_reports_device_errors_of_every_kind_without_a_traceback(tmp_path, monkeypatch, capsys):
    class Failing:
        def __init__(self, args):
            pass

        def run(self):
            raise FileNotFoundError(2, "No such file or directory", "/dev/video4")

    monkeypatch.setattr(collect, "Collector", Failing)
    monkeypatch.setattr(collect.ovision, "problem", lambda device: None)
    assert collect.main(["--out", str(tmp_path), "--task", "t", "--camera", "4", "--skip-doctor"]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


# -- RealSense backend (fake pyrealsense2; runs on every OS, never skipped) ------------

def patch_realsense(monkeypatch, hardware=None):
    """collect.py's RealSense backend on the fake pyrealsense2 with small color frames;
    returns the simulated camera."""
    import fake_realsense

    hardware = fake_realsense.install(monkeypatch, *([hardware] if hardware else []))
    monkeypatch.setattr(collect.realsense, "problem", lambda platform=None: None)
    monkeypatch.setattr(collect.realsense, "COLOR_SIZE", (320, 240))
    monkeypatch.setattr(collect, "REALSENSE_IDLE_PERIOD", 0.01)
    return hardware


def test_realsense_backend_keeps_one_worker_and_saves_the_camera_imu_per_episode(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    hardware = patch_realsense(monkeypatch)
    keys = (["g"] + [None] * 15 + ["h"] + ["g"] + [None] * 15 + ["x"] + ["g"] + [None] * 15 + ["h"])
    collector = collect.Collector(make_args(tmp_path, seconds=5, camera_backend="realsense"),
                                  display=ScriptedDisplay(keys))
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["saved", "discarded", "saved"]
    assert hardware.starts == 1  # One worker for the whole session.
    out = tmp_path / "captures"
    for session in (out / SLUG / f"{SLUG}_001", out / SLUG / f"{SLUG}_003"):
        manifest = json.loads((session / "manifest.json").read_text())
        camera = manifest["camera"]
        assert manifest["complete"] is True and camera["kind"] == "realsense"
        assert (camera["width"], camera["height"], camera["codec"]) == (320, 240, "mp4v")
        assert camera["usb_serial"] == "123456789012" and camera["device_clock_domain"] == "realsense_hw_clock"
        for key in ("video", "timestamps", "accel", "gyro", "calibration"):
            assert (session / camera[key]).is_file(), key
        assert camera["accel_samples"] >= 2 and camera["gyro_samples"] >= 2
        frames = camera["frames_decoded"]
        rows = [json.loads(line) for line in (session / "camera/timestamps.jsonl").read_text().splitlines()]
        assert len(rows) == frames >= 2 and all(type(row["device_timestamp"]) is int for row in rows)
        assert len((session / "alignment.preview.jsonl").read_text().splitlines()) == frames
        assert not any(key in camera for key in ("index", "mode", "name"))  # Studio-only fields stay out.
    rows = index_rows(out)
    assert [(r["session"], r["camera_kind"], r["camera_imu"]) for r in rows] == [
        (f"{SLUG}_001", "realsense", True), (f"{SLUG}_003", "realsense", True)]
    card = (out / "README.md").read_text()
    assert "RealSense D455 recorded through pyrealsense2" in card and "camera/realsense.accel.jsonl" in card
    assert "camera IMU is not recorded" not in card


def test_realsense_color_stall_fails_the_episode_and_reopens_the_camera(tmp_path, monkeypatch):
    import oglo.studio_realsense as studio_realsense

    patch_devices(monkeypatch, pair=False)
    hardware = patch_realsense(monkeypatch)
    monkeypatch.setattr(studio_realsense, "STALL_NS", 300_000_000)

    class StallDisplay(ScriptedDisplay):
        def show(self, image, listen=True):
            if listen and self.shown == 8 and hardware.starts == 1:
                hardware.color_stalled.set()  # The first episode's camera stops sending color.
            return super().show(image, listen)

    keys = ["g"] + [None] * 60 + ["g"] + [None] * 15 + ["h"]
    collector = collect.Collector(make_args(tmp_path, pair=False, seconds=5, camera_backend="realsense"),
                                  display=StallDisplay(keys))
    collector.reconnect_pause = 0
    reconnect = collector.reconnect_camera

    def reconnect_a_healthy_camera(reason):
        hardware.color_stalled.clear()  # The reopened camera streams again.
        return reconnect(reason)

    collector.reconnect_camera = reconnect_a_healthy_camera
    episodes = collector.run()
    assert [e["outcome"] for e in episodes] == ["failed", "saved"]
    assert hardware.starts == 2  # The failed worker was replaced.
    failed = tmp_path / "captures" / SLUG / "_failed" / f"{SLUG}_001" / "manifest.json"
    assert "five seconds" in json.loads(failed.read_text())["error"]
    saved = json.loads((tmp_path / "captures" / SLUG / f"{SLUG}_002" / "manifest.json").read_text())
    assert saved["complete"] is True and saved["camera"]["kind"] == "realsense"


def test_realsense_unplugged_while_idle_is_waited_for_and_found_again(tmp_path, monkeypatch, capsys):
    patch_devices(monkeypatch, pair=False)
    hardware = patch_realsense(monkeypatch)
    monkeypatch.setattr(collect, "REALSENSE_STALL_SECONDS", 0.3)
    monkeypatch.setattr(collect.realsense, "problem",
                        lambda platform=None: None if hardware.plugged else "no RealSense camera is connected")
    replug_frames = []
    reconnecting = []

    class ReplugDisplay(ScriptedDisplay):
        def show(self, image, listen=True):
            if listen and self.shown == 20 and hardware.starts == 1:
                hardware.unplug()
            if listen and not hardware.plugged:
                if reconnecting:  # Plugged back in only once the collector is waiting for it.
                    replug_frames.append(1)
                if len(replug_frames) >= 12:
                    hardware.replug()
                self.shown += 1
                return -1
            return super().show(image, listen)

    collector = collect.Collector(make_args(tmp_path, pair=False, camera_backend="realsense"),
                                  display=ReplugDisplay([None] * 120))
    collector.reconnect_pause = 0
    reconnect = collector.reconnect_camera
    collector.reconnect_camera = lambda reason: reconnecting.append(1) or reconnect(reason)
    assert collector.run() == []
    assert hardware.starts == 2
    err = capsys.readouterr().err
    assert "no color frame from the RealSense for 0.3 s" in err
    assert "camera reconnect attempt 1 failed: no RealSense camera is connected" in err


def test_realsense_idle_source_reports_a_worker_error_and_a_stall():
    worker = SimpleNamespace(error=None, last_frame_ns=time.monotonic_ns(), latest_frame=np.zeros((4, 4, 3)))
    source = collect.RealSenseIdleSource(worker, period=0, stall_seconds=0.2)
    ok, frame = source.read()
    assert ok and frame is worker.latest_frame
    worker.last_frame_ns -= 1_000_000_000
    assert source.read() == (False, None) and "no color frame" in source.reason
    worker.error = "RealSense stopped returning color frames for five seconds"
    assert source.read() == (False, None) and source.reason == worker.error


def test_realsense_is_chosen_by_camera_name_and_never_probed_as_an_ovision(monkeypatch, tmp_path):
    monkeypatch.setattr(collect.ovision, "problem", lambda device: pytest.fail("a RealSense is not an OVISION"))
    monkeypatch.setattr(collect.realsense, "problem", lambda platform=None: None)
    card = "Intel(R) RealSense(TM) Depth Ca"
    assert collect.choose_backend("auto", 2, card=card) == ("realsense", None)
    assert collect.choose_backend("realsense", 2) == ("realsense", None)
    monkeypatch.setattr(collect.realsense, "problem", lambda platform=None: "pyrealsense2 is not installed")
    assert collect.choose_backend("auto", 2, card=card) == (
        "opencv", "a RealSense recorded without its IMU: pyrealsense2 is not installed")
    with pytest.raises(RuntimeError, match="--camera-backend realsense: pyrealsense2 is not installed"):
        collect.choose_backend("realsense", 2)
    # The card comes from sysfs when the caller does not pass it.
    (tmp_path / "video2").mkdir()
    (tmp_path / "video2" / "name").write_text(card + "\n")
    monkeypatch.setattr(collect, "V4L2_SYSFS", tmp_path)
    assert collect.camera_card(2) == card and collect.camera_card(7) is None


def test_main_names_the_realsense_backend_and_probes_its_encoder(tmp_path, monkeypatch, capsys):
    seen = {}

    class StubCollector:
        def __init__(self, args):
            seen["args"] = args

        def run(self):
            return []

    probed = []
    monkeypatch.setattr(collect, "Collector", StubCollector)
    monkeypatch.setattr(collect.capture, "probe_encoder", lambda *a: probed.append(a) or None)
    monkeypatch.setattr(collect.realsense, "problem", lambda platform=None: None)
    argv = ["--out", str(tmp_path), "--task", "t", "--camera", "2", "--skip-doctor",
            "--camera-backend", "realsense", "--codec", "libx264"]
    assert collect.main(argv) == 0
    assert seen["args"].camera_backend == "realsense" and probed == [("libx264", 23)]
    assert "camera backend: RealSense (color 1280x720 at 30 fps through libx264" in capsys.readouterr().out


def test_realsense_problem_names_what_is_missing(monkeypatch):
    import fake_realsense
    from fake_realsense import Hardware

    realsense = collect.realsense
    assert "macOS" in realsense.problem(platform="darwin")
    monkeypatch.setattr(realsense, "installed_version", lambda: None)
    assert "not installed" in realsense.problem(platform="linux")
    monkeypatch.setattr(realsense, "installed_version", lambda: "2.55.1")
    assert "targets 2.58.4.10922" in realsense.problem(platform="linux")
    monkeypatch.setattr(realsense, "installed_version", lambda: "2.58.4.10922")
    for hardware, expected in (
        ((), "no RealSense camera is connected"),
        ((Hardware(), Hardware("999")), "2 RealSense cameras are connected (123456789012, 999)"),
        ((Hardware(imu=False, name="Intel RealSense D415"),), "D415 has no accelerometer"),
        ((Hardware(usb_type="2.1"),), "USB 2.1 connection; plug it into a USB 3 port"),
    ):
        fake_realsense.install(monkeypatch, *hardware)
        if not hardware:
            fake_realsense.Hardware.attached = []
        assert expected in realsense.problem(platform="linux")
    fake_realsense.install(monkeypatch)
    assert realsense.problem(platform="linux") is None


def test_realsense_check_describes_the_camera_and_records_a_few_seconds(monkeypatch):
    import fake_realsense
    from fake_realsense import Hardware

    fake_realsense.install(monkeypatch, Hardware(recommended="5.17.0.0"))
    monkeypatch.setattr(collect.realsense, "problem", lambda platform=None: None)
    monkeypatch.setattr(collect.realsense, "COLOR_SIZE", (320, 240))
    lines = []
    assert collect.realsense.check(seconds=0.5, out=lines.append) == 0
    text = "\n".join(lines)
    assert "device     Intel RealSense D455  serial 123456789012" in text
    assert "firmware   5.16.0.1 (recommended 5.17.0.0) -- update with RealSense Viewer" in text
    assert "accel      250 Hz (offered 63, 250)" in text and "gyro       400 Hz (offered 200, 400)" in text
    assert "device time sensor_timestamp (realsense_hw_clock)" in text
    assert lines[-1] == "OK: this camera can record color + camera IMU."

    fake_realsense.install(monkeypatch, Hardware(metadata=False, honor_global_time=False))
    lines.clear()
    assert collect.realsense.check(seconds=0.3, out=lines.append) == 1
    assert lines[-1].startswith("FAIL:") and "camera-clock time" in lines[-1]


def test_realsense_single_session_records_both_gloves_for_the_full_duration(tmp_path, monkeypatch):
    patch_devices(monkeypatch)
    patch_realsense(monkeypatch)
    output = tmp_path / "rs_001"
    assert collect.realsense.main(["--output", str(output), "--task", "t", "--seconds", "0.8", "--pair"]) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["complete"] is True and manifest["stop_reason"] == "duration"
    assert manifest["camera"]["kind"] == "realsense" and len(manifest["gloves"]) == 2
    assert manifest["camera"]["frames_decoded"] == manifest["camera"]["frames_submitted"] >= 2


def test_the_realsense_backend_checks_the_usb_controller_of_the_realsense_itself(tmp_path, monkeypatch):
    for index, card in ((0, "Integrated Camera"), (4, "Intel(R) RealSense(TM) Depth Ca")):
        (tmp_path / f"video{index}").mkdir()
        (tmp_path / f"video{index}" / "name").write_text(card + "\n")
    monkeypatch.setattr(collect, "V4L2_SYSFS", tmp_path)
    monkeypatch.setattr(collect, "is_capture_node", lambda index: True)
    assert collect.locate_realsense(4, "4") == (4, "4", None)
    index, spec, note = collect.locate_realsense(0, "0")
    assert (index, spec) == (4, "RealSense") and "--camera 0 is not the RealSense" in note
    (tmp_path / "video4" / "name").write_text("Some Webcam\n")
    index, spec, note = collect.locate_realsense(0, "0")
    assert (index, spec) == (0, "0") and "no /dev/video node is named RealSense" in note


def test_realsense_py_refuses_a_bad_quality_and_a_missing_encoder_before_opening_devices(monkeypatch, capsys):
    monkeypatch.setattr(collect.realsense, "problem", lambda platform=None: None)
    monkeypatch.setattr(collect.realsense, "capture", lambda *a, **k: pytest.fail("no device may open"))
    base = ["--output", "unused", "--task", "t"]
    with pytest.raises(SystemExit):
        collect.realsense.main(base + ["--video-quality", "60"])
    assert "--video-quality must be 0..51" in capsys.readouterr().err
    monkeypatch.setattr(collect.realsense, "probe_encoder", lambda codec, quality: "no NVENC device")
    with pytest.raises(SystemExit):
        collect.realsense.main(base + ["--codec", "hevc_nvenc"])
    assert "--codec hevc_nvenc does not work here: no NVENC device" in capsys.readouterr().err
