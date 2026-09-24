"""oglo.studio_realsense on the fake pyrealsense2: files, timing, and failure rules."""

import json
import threading
import time

import pytest

cv2 = pytest.importorskip("cv2")

import fake_realsense  # noqa: E402 - after the cv2 skip, like the other camera tests
from fake_realsense import Hardware
import oglo.studio_realsense as studio_realsense
from oglo.studio_realsense import RealSenseCameraWorker

SERIAL = "123456789012"
SMALL = (320, 240)


def open_worker(monkeypatch, hardware=None, **kwargs):
    fake_realsense.install(monkeypatch, *([hardware] if hardware else []))
    return RealSenseCameraWorker(0, name=SERIAL, size=SMALL, **kwargs)


def record(worker, folder, seconds=0.6, stop=None):
    stop = stop or threading.Event()
    worker.begin(folder, stop)
    time.sleep(seconds)
    return worker.finish()


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_take_saves_color_imu_calibration_and_camera_clock_timing(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch)
    try:
        meta = record(worker, tmp_path / "camera")
    finally:
        worker.close()
    folder = tmp_path / "camera"
    frames = rows(folder / "timestamps.jsonl")
    assert meta["kind"] == "realsense" and meta["frames_submitted"] == len(frames) >= 2
    assert [row["frame_index"] for row in frames] == list(range(len(frames)))
    first = frames[0]
    assert first["host_read_started_ns"] is None and type(first["host_received_ns"]) is int
    assert type(first["device_timestamp"]) is int and first["device_timestamp_unit"] == "us"
    assert first["device_clock_domain"] == "realsense_hw_clock"
    assert first["device_timestamp_meaning"] == "sensor_timestamp"
    assert "native_frame_number" in first
    accel, gyro = rows(folder / "realsense.accel.jsonl"), rows(folder / "realsense.gyro.jsonl")
    assert meta["accel_samples"] == len(accel) >= 2 and meta["gyro_samples"] == len(gyro) >= 2
    assert set(accel[0]) == {"frame_number", "host_received_ns", "device_timestamp_us", "x", "y", "z"}
    assert accel[0]["y"] == pytest.approx(-9.81)
    # One camera clock: IMU time falls inside the color frames' camera-time range.
    stamps = [row["device_timestamp"] for row in frames]
    assert stamps[0] < accel[len(accel) // 2]["device_timestamp_us"] < stamps[-1] + 10_000
    calibration = json.loads((folder / "realsense.calibration.json").read_text(encoding="utf-8"))
    assert calibration["device"]["serial"] == SERIAL
    assert calibration["color"]["intrinsics"]["model"] == "inverse_brown_conrady"
    assert len(calibration["accel"]["extrinsics_from_color"]["rotation"]) == 9
    assert calibration["accel"]["hz"] == 250 and calibration["gyro"]["hz"] == 400
    assert calibration["accel"]["intrinsics"]["data"][0][3] == pytest.approx(0.01)
    assert (meta["accel_hz"], meta["gyro_hz"], meta["accel_unit"], meta["gyro_unit"]) == (250, 400, "m/s^2", "rad/s")
    assert meta["native_frames_dropped"] == 0 and meta["device_clock_domain"] == "realsense_hw_clock"
    assert meta["model"] == "Intel RealSense D455" and meta["usb_serial"] == SERIAL
    assert (meta["width"], meta["height"]) == SMALL
    video = cv2.VideoCapture(str(folder / "video.mp4"))
    decoded = 0
    while video.read()[0]:
        decoded += 1
    video.release()
    assert decoded == meta["frames_submitted"]


def test_global_time_is_switched_off_so_timestamps_stay_on_the_camera_clock(monkeypatch):
    hardware = Hardware()
    worker = open_worker(monkeypatch, hardware)
    worker.close()
    assert hardware.global_time_set and set(hardware.global_time_set) == {0.0}


def test_without_a_sensor_timestamp_the_hardware_frame_timestamp_is_used(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch, Hardware(sensor_timestamp=False))
    try:
        record(worker, tmp_path / "camera")
    finally:
        worker.close()
    frames = rows(tmp_path / "camera" / "timestamps.jsonl")
    assert {row["device_timestamp_meaning"] for row in frames} == {"frame_timestamp"}


def test_frames_without_camera_clock_time_fail_the_take(tmp_path, monkeypatch):
    # Without UVC metadata a D400 stamps color with host system time, not the camera clock.
    worker = open_worker(monkeypatch, Hardware(metadata=False))
    try:
        with pytest.raises(RuntimeError, match="lack camera-clock time"):
            record(worker, tmp_path / "camera")
    finally:
        worker.close()
    first = rows(tmp_path / "camera" / "timestamps.jsonl")[0]
    assert first["device_timestamp"] is None and first["device_clock_domain"] is None


def test_frame_counter_gaps_are_counted_not_fatal(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch, Hardware(drop_every=5))
    try:
        meta = record(worker, tmp_path / "camera", seconds=0.8)
    finally:
        worker.close()
    assert meta["native_frames_dropped"] >= 2


def test_a_color_stall_stops_the_take_and_marks_the_worker_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(studio_realsense, "STALL_NS", 400_000_000)
    hardware = Hardware()
    worker = open_worker(monkeypatch, hardware)
    stop = threading.Event()
    try:
        worker.begin(tmp_path / "camera", stop)
        time.sleep(0.2)
        hardware.color_stalled.set()
        assert stop.wait(3), "the watchdog should stop the take (and the gloves)"
        with pytest.raises(RuntimeError, match="five seconds"):
            worker.finish()
        assert worker.error and worker.live_status()["ready"] is False
    finally:
        worker.close()


def test_finish_leaves_the_callers_stop_event_alone(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch)
    stop = threading.Event()
    try:
        record(worker, tmp_path / "camera", stop=stop)
    finally:
        worker.close()
    assert not stop.is_set()


def test_one_worker_records_consecutive_takes(tmp_path, monkeypatch):
    hardware = Hardware()
    worker = open_worker(monkeypatch, hardware)
    for take in ("one", "two"):
        (tmp_path / take).mkdir()
    try:
        first = record(worker, tmp_path / "one" / "camera", seconds=0.4)
        second = record(worker, tmp_path / "two" / "camera", seconds=0.4)
    finally:
        worker.close()
    assert hardware.starts == 1
    assert first["frames_submitted"] >= 2 and second["frames_submitted"] >= 2
    assert first["last_host_received_ns"] < second["first_host_received_ns"]


def test_the_writer_factory_encodes_the_video(tmp_path, monkeypatch):
    opened = []

    def factory(path, fps, size):
        opened.append((path.name, fps, size))
        return cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)

    worker = open_worker(monkeypatch, writer_factory=factory, codec="libx264", video_quality=28)
    try:
        meta = record(worker, tmp_path / "camera")
    finally:
        worker.close()
    assert opened == [("video.mp4", 30.0, SMALL)]
    assert (meta["codec"], meta["video_quality"]) == ("libx264", 28)


def test_preview_and_live_status_follow_the_stream(monkeypatch):
    worker = open_worker(monkeypatch)
    try:
        deadline = time.monotonic() + 2
        while not worker.live_status()["ready"] and time.monotonic() < deadline:
            time.sleep(0.05)
        status = worker.live_status()
        assert status["ready"] and status["error"] is None and status["age_ms"] < 1000
        assert worker.preview().startswith(b"\xff\xd8")
    finally:
        worker.close()


def test_a_camera_without_imu_is_refused(monkeypatch):
    fake_realsense.install(monkeypatch, Hardware(imu=False))
    with pytest.raises(RuntimeError, match="no accelerometer"):
        RealSenseCameraWorker(0, name=SERIAL, size=SMALL)


def test_a_color_mode_the_camera_does_not_offer_is_refused_with_the_offered_modes(monkeypatch):
    fake_realsense.install(monkeypatch)
    with pytest.raises(RuntimeError, match=r"offered: 320x240@30, 1280x720@15, 1280x720@30"):
        RealSenseCameraWorker(0, name=SERIAL, size=(640, 480))


def test_a_missing_serial_is_reported(monkeypatch):
    fake_realsense.install(monkeypatch)
    with pytest.raises(RuntimeError, match="not connected"):
        RealSenseCameraWorker(0, name="000000000000", size=SMALL)


def test_imu_intrinsics_absent_are_recorded_with_the_reason(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch, Hardware(imu_intrinsics=False))
    try:
        record(worker, tmp_path / "camera", seconds=0.3)
    finally:
        worker.close()
    calibration = json.loads((tmp_path / "camera" / "realsense.calibration.json").read_text(encoding="utf-8"))
    assert calibration["gyro"]["intrinsics"] is None
    assert "not available" in calibration["gyro"]["intrinsics_unavailable"]


def test_list_devices_describes_each_camera_without_streaming(monkeypatch):
    fake_realsense.install(monkeypatch, Hardware(), Hardware("222", imu=False, name="Intel RealSense D415"))
    devices = studio_realsense.list_devices()
    assert [(d["serial"], d["has_imu"]) for d in devices] == [(SERIAL, True), ("222", False)]
    assert devices[0]["usb_type"] == "3.2" and devices[0]["recommended_firmware"] == "5.16.0.1"


def test_nothing_is_recorded_after_the_shared_stop(tmp_path, monkeypatch):
    """Studio's Stop, collect.py's h and a glove failure set the shared event; the
    camera's take ends there, not when finish() is called later."""
    worker = open_worker(monkeypatch)
    stop = threading.Event()
    try:
        worker.begin(tmp_path / "camera", stop)
        time.sleep(0.4)
        stop.set()
        stopped = time.monotonic_ns()
        time.sleep(0.5)  # Studio waits for the gloves before it calls finish().
        meta = worker.finish()
    finally:
        worker.close()
    assert meta["last_host_received_ns"] <= stopped
    accel = rows(tmp_path / "camera" / "realsense.accel.jsonl")
    assert accel[-1]["host_received_ns"] <= stopped


def test_a_take_across_the_32_bit_camera_clock_wrap_stays_continuous(tmp_path, monkeypatch):
    """The camera clock is a 32-bit microsecond counter; a take recorded across its wrap
    (every ~71.6 min of camera uptime) keeps one increasing timeline for every stream."""
    worker = open_worker(monkeypatch, Hardware(clock32=True, clock_start_us=(1 << 32) - 400_000))
    try:
        meta = record(worker, tmp_path / "camera", seconds=0.9)
    finally:
        worker.close()
    assert meta["device_clock_unwrapped"] is True
    frames = [row["device_timestamp"] for row in rows(tmp_path / "camera" / "timestamps.jsonl")]
    assert frames == sorted(set(frames)) and frames[-1] > 1 << 32  # Past the wrap, still increasing.
    for stream in ("accel", "gyro"):
        stamps = [row["device_timestamp_us"] for row in rows(tmp_path / "camera" / f"realsense.{stream}.jsonl")]
        assert stamps == sorted(set(stamps)) and stamps[-1] > 1 << 32


def test_an_imu_that_stops_during_the_take_fails_it(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch, Hardware(imu_stop_after=0.3))
    try:
        with pytest.raises(RuntimeError, match="camera IMU stopped"):
            record(worker, tmp_path / "camera", seconds=1.2)
    finally:
        worker.close()


def test_a_complete_take_reports_imu_coverage(tmp_path, monkeypatch):
    worker = open_worker(monkeypatch)
    try:
        meta = record(worker, tmp_path / "camera", seconds=0.8)
    finally:
        worker.close()
    assert 0 < meta["accel_max_gap_us"] <= studio_realsense.IMU_GAP_US
    assert meta["accel_expected_samples"] > 0 and meta["gyro_expected_samples"] > meta["accel_expected_samples"]
    calibration = json.loads((tmp_path / "camera" / "realsense.calibration.json").read_text(encoding="utf-8"))
    assert calibration["motion_correction"]["enabled"] is True
    assert "do not apply them again" in calibration["motion_correction"]["note"]


def test_a_silent_imu_is_named_when_no_color_arrives(monkeypatch):
    """librealsense holds color framesets back until the IMU has sent a sample."""
    fake_realsense.install(monkeypatch, Hardware(imu_silent=True))
    with pytest.raises(RuntimeError, match=r"frames seen: color 0, accel 0, gyro 0\).*silent IMU"):
        RealSenseCameraWorker(0, name=SERIAL, size=SMALL, ready_timeout=0.5)


def test_an_encoder_that_will_not_open_fails_the_take_and_stops_the_gloves(tmp_path, monkeypatch):
    class Closed:
        def isOpened(self):
            return False

        def release(self):
            pass

    worker = open_worker(monkeypatch, writer_factory=lambda path, fps, size: Closed(), codec="hevc_nvenc")
    stop = threading.Event()
    try:
        worker.begin(tmp_path / "camera", stop)
        assert stop.wait(2)
        with pytest.raises(RuntimeError, match="could not open the hevc_nvenc video encoder"):
            worker.finish()
        assert worker.error
    finally:
        worker.close()


def test_frames_the_encoder_cannot_keep_up_with_are_counted_as_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(studio_realsense, "QUEUE_FRAMES", 2)

    def slow(path, fps, size):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
        original = writer.write

        class Slow:
            def isOpened(self):
                return writer.isOpened()

            def write(self, image):
                time.sleep(0.08)
                original(image)

            def release(self):
                writer.release()

        return Slow()

    worker = open_worker(monkeypatch, writer_factory=slow)
    try:
        meta = record(worker, tmp_path / "camera", seconds=1.0)
    finally:
        worker.close()
    assert meta["native_frames_dropped"] > 5


def test_close_during_a_take_ends_it_without_hanging(tmp_path, monkeypatch):
    hardware = Hardware()
    worker = open_worker(monkeypatch, hardware)
    worker.begin(tmp_path / "camera", threading.Event())
    time.sleep(0.3)
    started = time.monotonic()
    worker.close()
    assert time.monotonic() - started < 3
    assert hardware._thread is None  # The pipeline was stopped.
    assert (tmp_path / "camera" / "timestamps.jsonl").is_file()


def test_a_stop_before_two_color_frames_is_too_few_frames_not_a_failure(tmp_path, monkeypatch):
    hardware = Hardware()
    worker = open_worker(monkeypatch, hardware)
    try:
        hardware.color_stalled.set()  # g then h before the next color frame arrives.
        worker.begin(tmp_path / "camera", threading.Event())
        with pytest.raises(studio_realsense.TooFewFrames, match="nothing to keep"):
            worker.finish()
        assert worker.error is None  # The camera is fine; the next take may start.
        hardware.color_stalled.clear()
        (tmp_path / "next").mkdir()
        assert record(worker, tmp_path / "next" / "camera", seconds=0.4)["frames_submitted"] >= 2
    finally:
        worker.close()


def test_a_writer_stuck_after_stop_marks_the_worker_failed(tmp_path, monkeypatch):
    release = threading.Event()

    class Stuck:
        def isOpened(self):
            return True

        def write(self, image):
            release.wait()  # An encoder pipe that stopped draining.

        def release(self):
            pass

    worker = open_worker(monkeypatch, writer_factory=lambda path, fps, size: Stuck())
    try:
        worker.begin(tmp_path / "camera", threading.Event())
        time.sleep(0.3)
        with pytest.raises(RuntimeError, match="did not finish after Stop"):
            worker.finish(timeout=0.3)
        assert worker.error and worker.live_status()["ready"] is False
        (tmp_path / "next").mkdir()
        with pytest.raises(RuntimeError, match="did not finish after Stop"):
            worker.begin(tmp_path / "next" / "camera", threading.Event())
    finally:
        release.set()
        worker.close()

