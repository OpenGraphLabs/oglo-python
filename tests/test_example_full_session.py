"""The full-session example must write a session OpenGraph can align, without hardware.

`examples/03_full_session.py` is the one file a researcher reads to see the whole
interface: both gloves through the SDK, their own camera through a three-method
skeleton, and one session directory tying the two together on one host clock.
These tests drive `run_session()` with the fake serial gloves and the example's
own `FakeCamera`, so the layout it promises is checked on every CI run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo._device import Glove
from oglo._usb import UsbTransport

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "03_full_session.py"


def load_example():
    if not EXAMPLE.exists():
        pytest.fail(f"{EXAMPLE.relative_to(ROOT)} is missing")
    spec = importlib.util.spec_from_file_location("full_session_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def fake_glove(side: str, serial: str) -> Glove:
    cfg = dict(CFG_V6, side=side, serial=serial)
    transport = UsbTransport(FakeSerial(cfg, stream=tagged_burst(60), hz=250))
    info, caps = transport.read_config(interval=0.01, drain=0)
    return Glove(transport, info, caps)


def fake_pair():
    return fake_glove("left", "OGLO-L-TEST01"), fake_glove("right", "OGLO-R-TEST02")


def run(tmp_path, ex, left, right, **kw):
    camera = ex.FakeCamera(fps=60)
    try:
        return ex.run_session(tmp_path, left, right, camera, sink=ex.NullVideoSink(), **kw)
    finally:
        left.close()
        right.close()


def read_jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_session_directory_holds_both_episodes_the_camera_and_a_manifest(tmp_path):
    ex = load_example()
    left, right = fake_pair()
    session = run(tmp_path, ex, left, right, seconds=0.6)

    manifest = json.loads((session / "session.json").read_text())
    assert manifest["schema"] == "oglo.session.v1"
    for rel in (
        manifest["gloves"]["left"]["episode"],
        manifest["gloves"]["right"]["episode"],
        manifest["camera"]["timestamps"],
        manifest["markers"],
    ):
        assert (session / rel).exists(), rel
        assert "\\" not in rel, "manifest paths must also be readable on a different OS"
    assert (session / manifest["gloves"]["left"]["episode"] / "tactile.npz").exists()
    assert manifest["gloves"]["left"]["serial"] == "OGLO-L-TEST01"
    assert manifest["gloves"]["right"]["serial"] == "OGLO-R-TEST02"
    assert manifest["gloves"]["left"]["complete"] is True
    assert manifest["gloves"]["right"]["complete"] is True
    assert manifest["camera"]["frames"] > 0
    assert manifest["camera"]["video"] is None, "NullVideoSink writes no file"
    assert manifest["complete"] is True
    assert manifest["alignment_validated"] is False
    assert manifest["synthetic_camera"] is True


def test_camera_frames_share_the_host_clock_with_the_gloves(tmp_path):
    ex = load_example()
    left, right = fake_pair()
    session = run(tmp_path, ex, left, right, seconds=0.6)
    manifest = json.loads((session / "session.json").read_text())

    rows = read_jsonl(session / manifest["camera"]["timestamps"])
    assert len(rows) == manifest["camera"]["frames"]
    assert manifest["camera"]["finalized"] is True
    assert [r["frame_number"] for r in rows] == list(range(len(rows)))
    host = np.array([r["host_t_ns"] for r in rows], dtype=np.int64)
    assert np.all(np.diff(host) >= 0), "host_t_ns must never run backwards"
    assert all(isinstance(r["wall_ns"], int) for r in rows)
    assert all(r["device_timestamp"] is None for r in rows)
    opening_tap = next(m for m in read_jsonl(session / "markers.jsonl") if m["label"] == "tap_start")
    for side in ("left", "right"):
        meta = json.loads((session / manifest["gloves"][side]["episode"] / "meta.json").read_text())
        assert opening_tap["host_t_ns"] >= int(meta["started_monotonic"] * 1e9)
    assert manifest["started_monotonic_ns"] <= host[0]
    assert host[-1] <= manifest["ended_monotonic_ns"]

    glove_host = np.load(session / manifest["gloves"]["left"]["episode"] / "tactile.npz")["host_t_ns"]
    assert max(host[0], glove_host.min()) < min(host[-1], glove_host.max()), (
        "camera and glove host_t_ns ranges must overlap: same clock, same session"
    )


def test_the_session_records_raw_counts_and_never_touches_the_zero(tmp_path):
    ex = load_example()
    left, right = fake_pair()
    assert left.info.stream_clean, "the fake glove starts in the clean mode a shipped glove may be in"
    session = run(tmp_path, ex, left, right, seconds=0.6)

    for side in ("left", "right"):
        meta = json.loads((session / side / "ep_0001" / "meta.json").read_text())
        assert meta["stream_clean"] is False, f"{side} episode must be raw ADC counts"
    for glove in (left, right):
        sent = [c.upper() for c in glove._t._s.commands]
        assert not any("ZERO" in c for c in sent), "the example must never overwrite the stored zero"
        assert "SET STREAM RAW" in sent
        assert sent.index("SET STREAM RAW") < sent.index("SET STREAM CLEAN"), "restored to clean on exit"
    assert left.info.stream_clean and left.info.stream_thr == CFG_V6["stream_thr"]


def test_a_stop_event_ends_an_open_ended_session_cleanly(tmp_path, monkeypatch):
    ex = load_example()
    left, right = fake_pair()
    stop = threading.Event()
    original_add = ex.Markers.add
    timers = []
    watchdog_fired = []

    def add_and_schedule_stop(markers, label):
        original_add(markers, label)
        if label == "tap_start":
            timer = threading.Timer(0.15, stop.set)
            timers.append(timer)
            timer.start()

    def watchdog():
        watchdog_fired.append(True)
        stop.set()

    monkeypatch.setattr(ex.Markers, "add", add_and_schedule_stop)
    guard = threading.Timer(5, watchdog)
    guard.start()
    try:
        session = run(tmp_path, ex, left, right, seconds=None, stop_event=stop)
    finally:
        guard.cancel()
        for timer in timers:
            timer.cancel()
    assert not watchdog_fired, "capture did not reach the opening prompt"

    manifest = json.loads((session / "session.json").read_text())
    assert manifest["seconds_requested"] is None
    for side in ("left", "right"):
        meta = json.loads((session / side / "ep_0001" / "meta.json").read_text())
        assert meta["stop_reason"] == "cancelled"
    markers = read_jsonl(session / manifest["markers"])
    labels = [m["label"] for m in markers]
    assert labels[0] == "tap_start"
    assert "stop_requested" in labels
    assert all(manifest["started_monotonic_ns"] <= m["host_t_ns"] <= manifest["ended_monotonic_ns"] for m in markers)


def test_empty_camera_is_a_failed_session_and_cli_exit(tmp_path, monkeypatch):
    ex = load_example()

    class EmptyCamera(ex.FakeCamera):
        def read(self):
            return None

    left, right = fake_pair()
    monkeypatch.setattr(ex.oglo, "connect_pair", lambda: (left, right))
    monkeypatch.setattr(ex, "OpenCVCamera", lambda *args: EmptyCamera())
    monkeypatch.setattr(ex, "OpenCVVideoSink", ex.NullVideoSink)
    assert ex.main(["--out", str(tmp_path), "--seconds", "0.6"]) == 1
    manifest = json.loads(next(tmp_path.glob("*/session.json")).read_text())
    assert not manifest["complete"]
    assert manifest["camera"]["frames"] == 0
    assert any("camera" in error for error in manifest["errors"])
    assert left.info.stream_clean and right.info.stream_clean


@pytest.mark.parametrize("failed_side", ["left", "right"])
def test_glove_failure_cancels_peer_without_an_external_stop(tmp_path, monkeypatch, failed_side):
    ex = load_example()
    left, right = fake_pair()
    stop = threading.Event()
    watchdog_fired = []
    original_record = ex.oglo.record

    def fail_one(path, seconds, *, glove, stop_event):
        if glove.info.side == failed_side:
            raise RuntimeError(f"{failed_side} disconnected")
        return original_record(path, seconds, glove=glove, stop_event=stop_event)

    def watchdog():
        watchdog_fired.append(True)
        stop.set()

    monkeypatch.setattr(ex.oglo, "record", fail_one)
    timer = threading.Timer(5, watchdog)
    timer.start()
    try:
        session = run(tmp_path, ex, left, right, seconds=None, stop_event=stop)
    finally:
        timer.cancel()
    manifest = json.loads((session / "session.json").read_text())
    assert not watchdog_fired, "failed glove did not cancel its peer"
    assert stop.is_set()
    assert not manifest["complete"]
    assert "disconnected" in manifest["gloves"][failed_side]["error"]


def test_mid_capture_camera_end_cancels_open_ended_gloves(tmp_path):
    ex = load_example()
    left, right = fake_pair()
    stop = threading.Event()
    watchdog_fired = []

    class EndingCamera(ex.FakeCamera):
        def read(self):
            return None if self._n >= 12 else super().read()

    def watchdog():
        watchdog_fired.append(True)
        stop.set()

    timer = threading.Timer(5, watchdog)
    timer.start()
    try:
        session = ex.run_session(tmp_path, left, right, EndingCamera(fps=30),
                                 ex.NullVideoSink(), seconds=None, stop_event=stop)
    finally:
        timer.cancel()
        left.close()
        right.close()
    manifest = json.loads((session / "session.json").read_text())
    assert not watchdog_fired
    assert stop.is_set()
    assert not manifest["complete"]
    assert "ended before" in manifest["camera"]["error"]
    assert left.info.stream_clean and right.info.stream_clean


def test_partial_second_raw_failure_restores_both_gloves(tmp_path, monkeypatch):
    ex = load_example()
    left, right = fake_pair()
    original_raw = right.raw

    def partial_raw():
        original_raw()
        raise RuntimeError("right RAW acknowledgement lost")

    monkeypatch.setattr(right, "raw", partial_raw)
    session = run(tmp_path, ex, left, right, seconds=0.6)
    manifest = json.loads((session / "session.json").read_text())
    assert not manifest["complete"]
    assert any("acknowledgement lost" in error for error in manifest["errors"])
    for glove in (left, right):
        assert glove.info.stream_clean
        assert glove.info.stream_thr == CFG_V6["stream_thr"]


def test_camera_startup_failure_preserves_manifest_and_restores(tmp_path):
    ex = load_example()
    left, right = fake_pair()

    class BrokenCamera(ex.FakeCamera):
        def open(self):
            raise RuntimeError("camera permission denied")

    try:
        session = ex.run_session(tmp_path, left, right, BrokenCamera(),
                                 ex.NullVideoSink(), seconds=0.6)
    finally:
        left.close()
        right.close()
    manifest = json.loads((session / "session.json").read_text())
    assert not manifest["complete"]
    assert any("permission denied" in error for error in manifest["errors"])
    assert left.info.stream_clean and right.info.stream_clean


def test_camera_cleanup_failure_does_not_skip_glove_restoration(tmp_path):
    ex = load_example()
    left, right = fake_pair()

    class BrokenClose(ex.NullVideoSink):
        def close(self):
            raise RuntimeError("encoder close failed")

    try:
        session = ex.run_session(tmp_path, left, right, ex.FakeCamera(),
                                 BrokenClose(), seconds=0.6)
    finally:
        left.close()
        right.close()
    manifest = json.loads((session / "session.json").read_text())
    assert not manifest["complete"]
    assert any("encoder close failed" in error for error in manifest["errors"])
    assert left.info.stream_clean and right.info.stream_clean


def test_restore_failure_is_an_error_not_just_a_warning(tmp_path, monkeypatch):
    ex = load_example()
    left, right = fake_pair()

    def broken_restore(*, threshold):
        raise RuntimeError("restore rejected")

    monkeypatch.setattr(left, "clean", broken_restore)
    session = run(tmp_path, ex, left, right, seconds=0.6)
    manifest = json.loads((session / "session.json").read_text())
    assert not manifest["complete"]
    assert any("restore rejected" in error for error in manifest["errors"])
    assert right.info.stream_clean


def test_native_camera_timestamp_metadata_and_receipt_time_are_preserved(tmp_path):
    ex = load_example()
    left, right = fake_pair()

    class NativeCamera(ex.FakeCamera):
        def __init__(self):
            super().__init__()
            self.receipts = []

        def read(self):
            sample = super().read()
            sample.host_t_ns = ex.time.monotonic_ns()
            self.receipts.append(sample.host_t_ns)
            sample.device_timestamp = 1_000_000 + self._n * 33_333
            sample.device_timestamp_unit = "us"
            sample.device_clock_domain = "camera_boot"
            sample.device_timestamp_meaning = "exposure_start"
            return sample

    camera = NativeCamera()
    try:
        session = ex.run_session(tmp_path, left, right, camera, ex.NullVideoSink(), seconds=0.6)
    finally:
        left.close()
        right.close()
    rows = read_jsonl(session / "camera/video.timestamps.jsonl")
    assert [row["host_t_ns"] for row in rows] == camera.receipts[:len(rows)]
    assert rows[0]["device_timestamp"] == 1_033_333
    assert all(row["device_timestamp_unit"] == "us" for row in rows)
    assert all(row["device_clock_domain"] == "camera_boot" for row in rows)


def test_blocked_camera_retains_worker_ownership_until_read_returns(tmp_path, monkeypatch):
    ex = load_example()
    entered = threading.Event()
    release = threading.Event()
    closed = []

    class BlockedCamera(ex.FakeCamera):
        def read(self):
            if self._n:
                entered.set()
                release.wait(timeout=5)
                return None
            return super().read()

        def close(self):
            closed.append(threading.current_thread().name)

    recorder = ex.CameraRecorder(BlockedCamera(), ex.NullVideoSink(), tmp_path)
    try:
        recorder.start()
        assert entered.wait(timeout=2)
        monkeypatch.setattr(ex, "CAMERA_TIMEOUT_S", 0.1)
        recorder.stop()
        assert "shutdown timeout" in recorder.error
        assert closed == [], "main thread closed a camera while its reader still owned it"
    finally:
        release.set()
        recorder._thread.join(timeout=2)
    assert closed == ["camera"]


def test_native_timestamp_requires_its_unit_and_clock_domain(tmp_path):
    ex = load_example()

    class BadTimestamp(ex.FakeCamera):
        def read(self):
            frame = super().read()
            frame.device_timestamp = 42
            return frame

    recorder = ex.CameraRecorder(BadTimestamp(), ex.NullVideoSink(), tmp_path)
    try:
        with pytest.raises(RuntimeError, match="unit and clock domain"):
            recorder.start()
    finally:
        recorder.stop()


def test_opencv_adapter_writes_decodable_frames_with_matching_timestamp_rows(tmp_path):
    cv2 = pytest.importorskip("cv2", reason="optional camera example dependency")
    ex = load_example()
    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 48))
    assert writer.isOpened()
    for i in range(6):
        writer.write(np.full((48, 64, 3), i * 30, np.uint8))
    writer.release()
    recorder = ex.CameraRecorder(ex.OpenCVCamera(str(source)), ex.OpenCVVideoSink(), tmp_path / "out")
    try:
        try:
            recorder.start()
        except RuntimeError as exc:
            # A file source can reach EOF before start() resumes; EOF is correctly
            # an error for a live recorder, but all encoded frames remain inspectable.
            assert "ended before" in str(exc)
        recorder._thread.join(timeout=3)
    finally:
        recorder.stop()
    rows = read_jsonl(recorder.timestamps_path)
    decoder = cv2.VideoCapture(str(recorder.video_path))
    try:
        for _ in range(6):
            assert decoder.read()[0]
        assert not decoder.read()[0]
    finally:
        decoder.release()
    assert len(rows) == recorder.frames == 6
    assert all(row["device_timestamp"] is None for row in rows)


def test_keep_mode_preserves_mixed_raw_clean_inputs_without_mutations(tmp_path):
    ex = load_example()
    left, right = fake_pair()
    right.raw()
    before = {g.info.side: len(g._t._s.commands) for g in (left, right)}
    session = run(tmp_path, ex, left, right, seconds=0.6, stream="keep")
    for glove, expected_clean in ((left, True), (right, False)):
        commands = glove._t._s.commands[before[glove.info.side]:]
        assert not any(command.startswith("SET STREAM") for command in commands)
        meta = json.loads((session / glove.info.side / "ep_0001/meta.json").read_text())
        assert meta["stream_clean"] is expected_clean
        assert glove.info.stream_clean is expected_clean


@pytest.mark.parametrize("outside", [False, True])
def test_custom_sink_absolute_path_is_portable_or_recorded_as_error(tmp_path, monkeypatch, outside):
    ex = load_example()
    left, right = fake_pair()
    monkeypatch.chdir(tmp_path)

    class AbsoluteSink(ex.NullVideoSink):
        def open(self, path, info):
            output = (tmp_path / "outside.mp4") if outside else path.resolve()
            output.touch()
            return output

    try:
        session = ex.run_session(Path("relative-sessions"), left, right, ex.FakeCamera(),
                                 AbsoluteSink(), seconds=0.6)
    finally:
        left.close()
        right.close()
    manifest = json.loads((session / "session.json").read_text())
    if outside:
        assert not manifest["complete"]
        assert any("inside the camera directory" in error for error in manifest["errors"])
    else:
        assert manifest["complete"]
        assert manifest["camera"]["video"] == "camera/video.mp4"
        assert (session / manifest["camera"]["video"]).is_file()


def test_timed_out_encoder_does_not_publish_late_timestamp_rows(tmp_path, monkeypatch):
    ex = load_example()
    entered = threading.Event()
    release = threading.Event()

    class BlockedSink(ex.NullVideoSink):
        def __init__(self):
            self.count = 0

        def write(self, frame):
            self.count += 1
            if self.count == 2:
                entered.set()
                release.wait(timeout=5)

    recorder = ex.CameraRecorder(ex.FakeCamera(), BlockedSink(), tmp_path)
    try:
        recorder.start()
        assert entered.wait(timeout=2)
        monkeypatch.setattr(ex, "CAMERA_TIMEOUT_S", 0.1)
        recorder.stop()
        assert not recorder.finalized
        assert recorder.frames == 1
        rows_before = recorder.timestamps_path.read_text()
    finally:
        release.set()
        recorder._thread.join(timeout=2)
    assert recorder.timestamps_path.read_text() == rows_before
    assert recorder.frames == 1


def test_invalid_camera_fps_still_produces_a_failure_manifest(tmp_path):
    ex = load_example()
    left, right = fake_pair()

    class InvalidInfo(ex.FakeCamera):
        def open(self):
            info = super().open()
            info.fps = float("nan")
            return info

    try:
        session = ex.run_session(tmp_path, left, right, InvalidInfo(),
                                 ex.NullVideoSink(), seconds=0.6)
    finally:
        left.close()
        right.close()
    manifest = json.loads((session / "session.json").read_text())
    assert not manifest["complete"]
    assert any("finite non-negative" in error for error in manifest["errors"])
    assert left.info.stream_clean and right.info.stream_clean
