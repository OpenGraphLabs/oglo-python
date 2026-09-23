"""Exercise the camera example with simulated devices and real video files."""

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo._device import Glove
from oglo._usb import UsbTransport
from oglo._jsonl import stream_filename, channel_names

cv2 = pytest.importorskip("cv2")
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/camera_glove"


def load_example(name):
    spec = importlib.util.spec_from_file_location(f"camera_glove_{name}", EXAMPLE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_example("capture")
alignment = load_example("align")


class Camera:
    def __init__(self, fail_after=None):
        self.count = 0
        self.fail_after = fail_after
        self.released = False

    def isOpened(self):
        return True

    def set(self, *_):
        return False  # A backend may ignore an FPS request.

    def getBackendName(self):
        return "synthetic"

    def read(self):
        time.sleep(0.02)
        self.count += 1
        if self.fail_after is not None and self.count > self.fail_after:
            return False, None
        return True, np.full((48, 64, 3), self.count % 256, dtype=np.uint8)

    def release(self):
        self.released = True


def simulated_glove(side, clean=True):
    config = {**CFG_V6, "side": side, "serial": f"OGLO-{side}-CAMERA-TEST", "stream_clean": clean}
    if side == "right":
        config["channels"] = list(reversed(config["channels"]))
    serial = FakeSerial(config, stream=tagged_burst(60), hz=250)
    transport = UsbTransport(serial)
    info, caps = transport.read_config(interval=0.01, drain=0)
    return Glove(transport, info, caps)


def setup_capture(tmp_path, monkeypatch, pair=False, fail_after=None, clean=True,
                  codec="mp4v", quality=23):
    camera = Camera(fail_after=fail_after)
    original = cv2.VideoCapture
    monkeypatch.setattr(capture.cv2, "VideoCapture",
                        lambda source: camera if isinstance(source, int) else original(source))
    monkeypatch.setattr(capture.oglo, "connect", lambda **_: simulated_glove("left", clean))
    monkeypatch.setattr(capture.oglo, "connect_pair",
                        lambda: (simulated_glove("left", clean), simulated_glove("right", clean)))
    args = argparse.Namespace(output=tmp_path / "session", camera=0, seconds=0.5,
                              fps=30, task="synthetic contact", serial=None, pair=pair,
                              preview=False, codec=codec, video_quality=quality)
    return args, camera


def test_camera_timestamp_is_taken_before_any_conversion(monkeypatch):
    ticks = iter([100, 200])
    monkeypatch.setattr(capture.time, "monotonic_ns", lambda: next(ticks))
    _, timing = capture.read_camera(Camera())
    assert timing["host_read_started_ns"] == 100
    assert timing["host_received_ns"] == 200
    assert all(timing[key] is None for key in (
        "device_timestamp", "device_timestamp_unit", "device_clock_domain",
        "device_timestamp_meaning",
    ))


@pytest.mark.parametrize("pair,clean", [(False, True), (True, True), (True, False)])
def test_capture_decode_and_join_preserve_source_files(tmp_path, monkeypatch, pair, clean):
    args, camera = setup_capture(tmp_path, monkeypatch, pair=pair, clean=clean)
    root = capture.capture(args)
    assert camera.released
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["stop_reason"] == "duration"
    assert manifest["alignment_validated"] is False
    assert manifest["camera"]["fps_request_accepted"] is False
    assert manifest["camera"]["frames_decoded"] >= 2
    assert len(manifest["gloves"]) == (2 if pair else 1)
    rows = [json.loads(line) for line in (root / "camera/timestamps.jsonl").read_text().splitlines()]
    assert len(rows) == manifest["camera"]["frames_decoded"]
    assert [row["frame_index"] for row in rows] == list(range(len(rows)))
    original = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    output = root / "alignment.preview.jsonl"
    assert alignment.align(root, output, max_delta_ms=50) == len(rows)
    joined = [json.loads(line) for line in output.read_text().splitlines()]
    for hand in manifest["gloves"]:
        assert (root / hand["calibration"]).is_file()
        episode = capture.oglo.replay(root / hand["episode"])
        data = episode.arrays("tactile")
        samples = [json.loads(line) for line in
                   (episode.dir / stream_filename("tactile", episode.info)).read_text().splitlines()]
        assert len(samples) == len(data["seq"])
        labels = channel_names("tactile", episode.info)
        assert [samples[0]["channels"][label] for label in labels] == data["counts"][0].reshape(80).tolist()
        assert samples[0]["capture_ns"] == int(data["host_received_ns"][0])
        calibration = json.loads((episode.dir / episode.meta["calibration"]).read_text())
        assert calibration["transform"]["mode"] == ("firmware_clean" if clean else "host_clean_from_raw")
        clean_file = episode.dir / f"tactile_{hand['side']}.jsonl"
        assert clean_file.is_file()
        assert "jsonl" not in hand  # One primary recording, no extra conversion directory.
        if not clean:
            cleaned = json.loads(clean_file.read_text().splitlines()[0])
            baseline = calibration["zero"]["baseline"]
            expected = [max(0, samples[0]["channels"][label] - base) for label, base in zip(labels, baseline)]
            expected = [v if v >= calibration["transform"]["threshold_counts"] else 0 for v in expected]
            assert [cleaned["channels"][label] for label in labels] == expected
        matches = [glove["tactile"] for frame in joined for glove in frame["gloves"]
                   if glove["serial"] == hand["serial"] and glove["tactile"] is not None]
        assert matches
        for match in matches:
            row = match["row_index"]
            assert int(data["seq"][row]) == match["seq"]
            assert int(data["host_received_ns"][row]) == match["host_received_ns"]
            assert abs(match["delta_ns"]) <= 50_000_000
    assert all(p.read_bytes() == contents for p, contents in original.items())
    with pytest.raises(FileExistsError):
        alignment.align(root, output, max_delta_ms=50)
    with pytest.raises(RuntimeError, match="decoded frames"):
        capture.verify_video(root / "camera/video.mp4", len(rows) + 1)


def test_external_stop_ends_capture_complete(tmp_path, monkeypatch):
    args, camera = setup_capture(tmp_path, monkeypatch, pair=True)
    args.seconds = 10  # Only a cap: the event below ends the session first.
    stop = threading.Event()
    threading.Timer(0.6, stop.set).start()  # Device setup eats part of this.
    started = time.monotonic()
    root = capture.capture(args, stop=stop)
    assert time.monotonic() - started < 5
    assert camera.released
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True and manifest["error"] is None
    assert manifest["stop_reason"] == "cancelled"
    assert manifest["requested_duration_s"] == 10
    rows = (root / "camera/timestamps.jsonl").read_text().splitlines()
    assert 2 <= len(rows) == manifest["camera"]["frames_decoded"]
    assert len(manifest["gloves"]) == 2
    for hand in manifest["gloves"]:
        assert hand["summary"]["tactile"]["n"] > 0
        assert capture.oglo.replay(root / hand["episode"]).meta["stop_reason"] == "cancelled"
    assert alignment.align(root, root / "alignment.preview.jsonl", max_delta_ms=50) == len(rows)


def test_ffmpeg_codec_keeps_one_decoded_frame_per_submitted_frame(tmp_path, monkeypatch):
    if shutil.which(capture.FFMPEG) is None:
        pytest.skip("needs the ffmpeg binary")
    problem = capture.probe_encoder("libx264", 28)
    if problem:
        pytest.skip(f"ffmpeg has no working libx264: {problem}")
    args, camera = setup_capture(tmp_path, monkeypatch, codec="libx264", quality=28)
    root = capture.capture(args)
    assert camera.released
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["camera"]["codec"] == "libx264"
    assert manifest["camera"]["video_quality"] == 28
    assert manifest["camera"]["frames_decoded"] == manifest["camera"]["frames_submitted"] >= 2
    rows = (root / "camera/timestamps.jsonl").read_text().splitlines()
    assert len(rows) == manifest["camera"]["frames_decoded"]


def test_missing_ffmpeg_leaves_session_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "FFMPEG", "ffmpeg-that-does-not-exist")
    args, camera = setup_capture(tmp_path, monkeypatch, codec="libx264")
    with pytest.raises(RuntimeError, match="not found"):
        capture.capture(args)
    assert camera.released
    manifest = json.loads((tmp_path / "session/manifest.json").read_text())
    assert manifest["complete"] is False and "not found" in manifest["error"]
    assert capture.probe_encoder("libx264") and "not found" in capture.probe_encoder("libx264")
    assert capture.probe_encoder("mp4v") is None


def test_unknown_codec_is_rejected(tmp_path, monkeypatch):
    args, _ = setup_capture(tmp_path, monkeypatch, codec="webm")
    with pytest.raises(RuntimeError, match="Unknown codec"):
        capture.capture(args)
    assert json.loads((tmp_path / "session/manifest.json").read_text())["complete"] is False


def test_camera_failure_keeps_incomplete_manifest_and_partial_glove(tmp_path, monkeypatch):
    args, camera = setup_capture(tmp_path, monkeypatch, fail_after=3)
    with pytest.raises(RuntimeError, match="Camera did not return"):
        capture.capture(args)
    assert camera.released
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert manifest["complete"] is False
    assert "Camera did not return" in manifest["error"]
    assert manifest["gloves"][0]["episode"] is not None
    assert (args.output / manifest["gloves"][0]["episode"] / "meta.json").is_file()
    with pytest.raises(ValueError, match="complete"):
        alignment.align(args.output, args.output / "preview.jsonl", 50)


def test_glove_failure_stops_camera_and_preserves_error(tmp_path, monkeypatch):
    args, camera = setup_capture(tmp_path, monkeypatch)

    def fail(*_, **__):
        raise RuntimeError("synthetic USB disconnect")

    monkeypatch.setattr(capture.oglo, "record", fail)
    with pytest.raises(RuntimeError):
        capture.capture(args)
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert not manifest["complete"]
    assert "USB disconnect" in manifest["gloves"][0]["error"]
    assert camera.count <= 3
    assert camera.released


def test_existing_session_is_not_overwritten(tmp_path, monkeypatch):
    args, _ = setup_capture(tmp_path, monkeypatch)
    args.output.mkdir()
    marker = args.output / "manifest.json"
    marker.write_text("original")
    with pytest.raises(FileExistsError):
        capture.capture(args)
    assert marker.read_text() == "original"


def test_jsonl_finalization_failure_keeps_session_incomplete(tmp_path, monkeypatch):
    import oglo._record as recording

    args, camera = setup_capture(tmp_path, monkeypatch, clean=False)

    def fail_clean(*args):
        raise OSError("JSONL disk full")

    monkeypatch.setattr(recording, "derive_clean", fail_clean)
    with pytest.raises(capture.oglo.RecordError, match="JSONL disk full"):
        capture.capture(args)
    manifest = json.loads((args.output / "manifest.json").read_text())
    assert camera.released
    assert manifest["complete"] is False
    assert "JSONL disk full" in manifest["error"]
    for hand in manifest["gloves"]:
        assert capture.oglo.replay(args.output / hand["episode"]).meta["complete"] is False


def test_nearest_sample_preserves_integer_precision_and_resolves_ties():
    base = 2**53
    times = [base, base + 10, base + 10, base + 30]
    sequences = np.array([100, 101, 102, 103], dtype=np.uint32)
    assert alignment.nearest_sample(times, sequences, base + 11, 5) == {
        "row_index": 1, "seq": 101, "host_received_ns": base + 10, "delta_ns": -1,
    }
    assert alignment.nearest_sample(times, sequences, base + 20, 10)["row_index"] == 1
    assert alignment.nearest_sample(times, sequences, base + 9, 5)["delta_ns"] == 1
    assert alignment.nearest_sample(times, sequences, base + 20, 5) is None
    assert alignment.nearest_sample(times, sequences, base - 1, 50) is None
    assert alignment.nearest_sample(times, sequences, base + 31, 50) is None
    assert alignment.nearest_sample([], [], base, 50) is None


@pytest.mark.parametrize("tolerance", [0, -1, float("nan"), float("inf")])
def test_invalid_alignment_tolerance_creates_no_output(tmp_path, tolerance):
    output = tmp_path / "preview.jsonl"
    with pytest.raises(ValueError, match="finite"):
        alignment.align(tmp_path, output, tolerance)
    assert not output.exists()


@pytest.fixture
def ovision(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    return load_example("ovision")


def native_rows():
    return [{"frame_number": i, "capture_ns": 2**53 + i * 33_333_333,
             "clock_source": "device_monotonic", "device_timestamp_ns": i * 33_333_000,
             "left_exposure_start_ns": i * 33_333_000,
             "right_exposure_start_ns": i * 33_333_000 + 20_000,
             "user_data_seq": 400 + i} for i in range(3)]


def test_ovision_join_keeps_native_bytes_and_clock_domains(tmp_path, ovision):
    source = tmp_path / "cam_ego.stereo.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in native_rows()))
    original = source.read_bytes()
    result = ovision.make_join_timestamps(tmp_path, 3)
    assert result["frames_submitted"] == 3
    rows = [json.loads(line) for line in (tmp_path / "timestamps.jsonl").read_text().splitlines()]
    assert source.read_bytes() == original
    assert rows[1]["host_received_ns"] == 2**53 + 33_333_333
    assert rows[1]["device_timestamp"] == 33_333_000
    assert rows[1]["device_timestamp_meaning"] == "left_exposure_start"
    assert rows[1]["host_read_started_ns"] is None
    with pytest.raises(FileExistsError):
        ovision.make_join_timestamps(tmp_path, 3)


@pytest.mark.parametrize("fault", ["sequence", "host_order", "device_time", "count"])
def test_ovision_rejects_inconsistent_native_metadata(tmp_path, ovision, fault):
    rows = native_rows()
    if fault == "sequence":
        rows[1]["frame_number"] = 7
    elif fault == "host_order":
        rows[1]["capture_ns"] = 0
    elif fault == "device_time":
        rows[1]["device_timestamp_ns"] += 1
    (tmp_path / "cam_ego.stereo.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    with pytest.raises(ValueError):
        ovision.make_join_timestamps(tmp_path, 4 if fault == "count" else 3)


def test_ovision_capture_uses_published_sidecars_and_common_join(tmp_path, monkeypatch, ovision):
    native = pytest.importorskip("syncfield.adapters.ovision_camera")
    from syncfield.adapters.ovision_metadata import OvisionImuSample
    from syncfield.types import SensorSample

    class SimulatedOvision(native.OvisionCameraStream):
        """Keep the real adapter serializer/finalization; replace only device IO."""

        def prepare(self):
            self.ready = True

        def connect(self):
            pass

        def capture_ready(self):
            return self.ready

        def start_recording(self, clock):
            self._begin_recording_window(clock)
            writer = cv2.VideoWriter(str(self._file_path), cv2.VideoWriter_fourcc(*"mp4v"),
                                     30, (64, 48))
            assert writer.isOpened()
            self._writer = SimpleNamespace(
                write_packet=lambda *_: writer.write(np.zeros((48, 64, 3), dtype=np.uint8)),
                close=writer.release,
            )
            self._sinks = native._SidecarSinks.open(self._output_dir, self.id)
            for suffix in ("json", "yaml", "bin"):
                (self._output_dir / f"cam_ego.calibration.{suffix}").write_text("test calibration")
            self.stopping = threading.Event()

            def produce():
                while not self.stopping.wait(0.02):
                    stamp = 1_000_000 + self._frame_count * 33_333
                    sample = OvisionImuSample((1, 2, 3), 0, stamp)
                    metadata = SimpleNamespace(
                        left_exposure_start_pts_us=stamp, right_exposure_start_pts_us=stamp + 20,
                        stereo_exposure_start_skew_us=20, left_start_line_rx_pts_us=stamp + 100,
                        right_start_line_rx_pts_us=stamp + 120, left_exposure_time_us=10_000,
                        right_exposure_time_us=10_000, left_gpio_trigger_index=self._frame_count,
                        right_gpio_trigger_index=self._frame_count, user_data_seq=self._frame_count,
                        frame_meta_generation=1, gyro=(sample,), accel=(sample,), mag=(),
                    )
                    self._record_packet(object(), metadata, time.monotonic_ns(), True)

            self.producer = threading.Thread(target=produce)
            self.producer.start()

        def stop_recording(self):
            self.stopping.set()
            self.producer.join(timeout=2)
            assert not self.producer.is_alive()
            return super().stop_recording()

        def disconnect(self):
            self.ready = False

    monkeypatch.setattr(native, "OvisionCameraStream", SimulatedOvision)
    args, _ = setup_capture(tmp_path, monkeypatch, pair=True)
    args.video_device = Path("/dev/synthetic-ovision")
    args.camera_serial = "test-camera"
    root = ovision.capture(args, camera_factory=ovision.OvisionCapture)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["camera"]["kind"] == "ovision"
    assert manifest["camera"]["frames_decoded"] > 2
    assert len(manifest["gloves"]) == 2
    for hand in manifest["gloves"]:
        episode = root / hand["episode"]
        assert (episode / f"tactile_{hand['side']}.jsonl").stat().st_size > 0
        assert (episode / f"wrist_imu_{hand['side']}.jsonl").stat().st_size > 0
        assert (episode / f"wrist_mag_{hand['side']}.jsonl").is_file()
        assert json.loads((episode / "meta.json").read_text())["schema"] == 3
        for path in episode.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                assert SensorSample.from_dict(row).to_dict() == {
                    key: value for key, value in row.items() if key != "oglo"
                }
    original = {p: p.read_bytes() for p in (root / "camera").iterdir()}
    count = alignment.align(root, root / "alignment.preview.jsonl", 50)
    assert count == manifest["camera"]["frames_decoded"]
    assert all(p.read_bytes() == content for p, content in original.items())
    native_row = json.loads((root / "camera/cam_ego.stereo.jsonl").read_text().splitlines()[0])
    common_row = json.loads((root / "camera/timestamps.jsonl").read_text().splitlines()[0])
    assert native_row["capture_ns"] == common_row["host_received_ns"]
    assert native_row["device_timestamp_ns"] == common_row["device_timestamp"]
    imu = json.loads((root / "camera/cam_ego.imu.jsonl").read_text().splitlines()[0])
    assert imu["accel_unit"] == "m_s2" and imu["gyro_unit"] == "rad_s"


def test_preopened_gloves_are_used_and_left_open(tmp_path, monkeypatch):
    args, _ = setup_capture(tmp_path, monkeypatch)
    monkeypatch.setattr(capture.oglo, "connect", lambda **_: pytest.fail("must not reconnect"))
    glove = simulated_glove("left")
    root = capture.capture(args, gloves=(glove,))
    assert glove._started is False  # Not left streaming into a port nobody reads.
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["complete"] is True
    assert manifest["gloves"][0]["serial"] == glove.info.serial
    assert glove.read_batch() is not None  # Still open for the caller.
    glove.close()
