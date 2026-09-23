"""Drive the collect.py state machine with simulated devices and no window."""

import argparse
import json
import time
from pathlib import Path

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
    values = dict(out=tmp_path / "captures", task=TASK, camera=0, seconds=0.5,
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
    args = argparse.Namespace(output=tmp_path / "session", camera=0, seconds=0.5, fps=30,
                              task="proxy", serial=None, pair=False, preview=False,
                              codec="mp4v", video_quality=23)
    root = collect.capture.capture(args, stop=control.stop, camera_factory=lambda a, o: (
        collect.OverlayCamera(a, o, display, control, "session")))
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True and manifest["stop_reason"] == "duration"
    assert control.outcome is None
    assert display.shown == manifest["camera"]["frames_submitted"] - 1  # Previous frame each read.
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


def test_parser_defaults_to_gpu_h265_and_a_long_cap():
    args = collect.build_parser().parse_args(["--out", "x", "--task", "t"])
    assert (args.codec, args.video_quality, args.seconds) == ("hevc_nvenc", 23, 600)
    with pytest.raises(SystemExit):  # Packing is gone with the per-task layout.
        collect.build_parser().parse_args(["--out", "x", "--task", "t", "--no-pack"])


def test_sessions_are_numbered_per_task_and_never_reused(tmp_path):
    out = tmp_path / "captures"
    task = out / "pick_up_a_cup"
    assert collect.next_session_dir(out, "Pick up a cup!") == task / "pick_up_a_cup_001"
    (task / "pick_up_a_cup_001").mkdir(parents=True)
    (task / "_discarded" / "pick_up_a_cup_002").mkdir(parents=True)
    (task / "_failed" / "pick_up_a_cup_003").mkdir(parents=True)
    assert collect.next_session_dir(out, "Pick up a cup!") == task / "pick_up_a_cup_004"
    assert collect.next_session_dir(out, "Other task") == out / "other_task" / "other_task_001"
    assert collect.task_slug("!!!") == "session"
    assert collect.task_slug("Gloves") == "gloves_task"  # gloves/ is reserved for per-glove files


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

    def broken_align(*args, **kwargs):
        raise RuntimeError("no overlap")

    monkeypatch.setattr(collect.align, "align", broken_align)
    collector = collect.Collector(make_args(tmp_path, seconds=5),
                                  display=ScriptedDisplay(["g"] + [None] * 15 + ["h"]))
    episodes = collector.run()
    out = tmp_path / "captures"
    moved = out / SLUG / "_failed" / f"{SLUG}_001"
    assert [(e["outcome"], e["session"]) for e in episodes] == [("failed", moved)]
    assert (moved / "manifest.json").is_file() and not (out / SLUG / f"{SLUG}_001").exists()
    assert "align FAILED" in collector.status and "no overlap" in collector.status
    assert index_rows(out) == []


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
    assert index_rows(out) == []  # run() still closed devices and refreshed the index.


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


def test_devices_stay_open_across_episodes_and_grids_are_drawn(tmp_path, monkeypatch):
    """One camera and one glove connection serve idle and every episode; the overlay shows tactile."""
    opened = []
    connects = []
    original = cv2.VideoCapture
    monkeypatch.setattr(collect.cv2, "VideoCapture", lambda source: (
        opened.append(source) or Camera()) if isinstance(source, int) else original(source))
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
    assert opened == [0] and connects == [1]  # Never reopened between episodes.
    assert shapes == {((5, 4, 4), (5, 4, 4))}
    assert grids_while_recording and any(patch.std() > 0 for patch in grids_while_recording)
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
    collect.Collector(make_args(tmp_path, pair=False, serial="OGLO-R-00114"), display=ScriptedDisplay("")).run()
    assert chosen == ["OGLO-R-00114"]


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
    seen = []

    class Watching(ScriptedDisplay):
        collector = None

        def show(self, image, listen=True):
            if listen:  # An idle frame: the window thread never called read_batch itself.
                seen.append(tuple(g.latest is not None for g in self.collector.gloves))
                seen.append(tuple(g._thread is not None and g._thread.is_alive() for g in self.collector.gloves))
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
    assert (True, True) in seen  # tactile frames arrived while the window only drew
    assert seen[-1] == (True, True)  # readers were restarted after the episode
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
