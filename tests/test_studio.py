"""Exercise the local collection controller with synthetic USB and real MP4 files."""

import json
from dataclasses import dataclass
import sys
import threading
import time
import zipfile

import oglo
import oglo.studio as studio_module
import numpy as np
import pytest
from types import SimpleNamespace

cv2 = pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from oglo.studio import Studio, _load_episode, create_app
from oglo.collection import CameraSelection, Collection
from test_camera_glove import Camera
from studio_fake_devices import simulated_studio_glove
import fake_realsense

REALSENSE_SERIAL = "123456789012"
REALSENSE_CHOICE = {"index": 0, "mode": "realsense", "name": REALSENSE_SERIAL,
                    "label": "Intel RealSense D455 · color + camera IMU · serial 123456789012"}


@pytest.fixture
def studio_client(tmp_path, monkeypatch):
    camera = Camera()
    original = cv2.VideoCapture
    monkeypatch.setattr(cv2, "VideoCapture", lambda source: camera if isinstance(source, int) else original(source))
    monkeypatch.setattr(oglo, "connect_pair", lambda: (
        simulated_studio_glove("left"), simulated_studio_glove("right")))
    studio = Studio(tmp_path / "captures")
    try:
        yield studio, camera
    finally:
        studio._close_devices()


def _wait_for_result(studio, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = studio.status()
        if status["state"] in {"review", "error"}:
            return status
        time.sleep(0.05)
    raise AssertionError("recording did not finish")


def _wait_for_live(studio, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = studio.live()
        if (live["camera"] and live["camera"]["ready"] and not live["camera"].get("dark") and
            live["camera"]["age_ms"] < 1000 and not live["errors"] and
            set(live["gloves"]) == {"left", "right"} and
            all(live["gloves"][side]["age_ms"] < 1000 for side in ("left", "right"))):
            return live
        time.sleep(0.05)
    raise AssertionError("camera and both glove streams did not become live")


def test_record_keep_and_export_complete_source_episode(studio_client, tmp_path):
    studio, _ = studio_client
    assert create_app(studio.root, studio=studio).title == "OGLO Studio"
    status = studio.connect(0)
    assert status["state"] == "ready"
    live = _wait_for_live(studio)
    assert all(len(live["gloves"][side]["values"]) == 80 for side in ("left", "right"))
    assert all(live["gloves"][side]["display_mode"] == "derived_clean" for side in ("left", "right"))
    before = {side: live["gloves"][side]["seq"] for side in ("left", "right")}
    studio.start("Pick up a cup")
    time.sleep(0.5)
    during = studio.live()["gloves"]
    assert all(during[side]["seq"] > before[side] for side in ("left", "right"))
    time.sleep(1.2)
    studio.stop()
    result = _wait_for_result(studio)
    assert result["state"] == "review", result.get("error")
    episode = result["episodes"][0]
    assert episode["complete"] is True
    assert episode["stop_reason"] == "user_stop"
    assert episode["delivery_validation"]["source_archive"] == "ready"
    assert "blocked" in episode["delivery_validation"]["annotation_handoff"]
    assert episode["camera"]["frames_decoded"] >= 2
    assert episode["validation"]["coverage_fraction"] >= 0.9
    assert [item["action"] for item in episode["actions"]] == ["start", "stop"]
    assert episode["gloves"][0]["calibration"].endswith("tactile_left.calibration.json")
    assert episode["gloves"][0]["device_config"]["side"] == "left"
    review_video = studio.review_video(episode["id"])
    assert review_video.is_file() and review_video.parent == studio.root / "review"
    assert studio.review_poster(episode["id"]).is_file()
    review_decoder = cv2.VideoCapture(str(review_video))
    try:
        assert review_decoder.isOpened() and review_decoder.read()[0]
    finally:
        review_decoder.release()
    glove = oglo.replay(studio.root / episode["id"] / episode["gloves"][0]["episode"])
    assert glove.meta["complete"] and glove.meta["stream_clean"] is False
    for side in ("left", "right"):
        assert (studio.root / episode["id"] / f"gloves/{side}/ep_0001/tactile_{side}.raw.jsonl").is_file()
    studio.select(episode["id"], "kept")
    archive = studio.export()
    target = tmp_path / "dataset.zip"
    target.write_bytes(archive.read_bytes())
    with zipfile.ZipFile(target) as zipped:
        names = zipped.namelist()
        assert f"{episode['id']}/camera/video.mp4" in names
        assert f"{episode['id']}/camera/timestamps.jsonl" in names
        assert f"{episode['id']}/inventory.json" in names
        for side in ("left", "right"):
            assert f"{episode['id']}/gloves/{side}/ep_0001/tactile_{side}.raw.jsonl" in names
        assert json.loads(zipped.read("dataset.json"))["episodes"] == [episode["id"]]
        index = json.loads(zipped.read("dataset.json"))
        assert "manifest.json" in index["checksums"][episode["id"]]
        assert "inventory.json" in index["checksums"][episode["id"]]
        assert zipped.testzip() is None


def test_collection_sdk_timed_take_review_and_export(tmp_path, monkeypatch):
    camera = Camera()
    original = cv2.VideoCapture
    monkeypatch.setattr(cv2, "VideoCapture", lambda source: camera if isinstance(source, int) else original(source))
    monkeypatch.setattr(oglo, "connect_pair", lambda: (
        simulated_studio_glove("left"), simulated_studio_glove("right")))
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [
        {"index": 0, "mode": "default", "label": "Synthetic camera"}])
    monkeypatch.setattr(studio_module, "list_candidates", lambda: [])
    with Collection(tmp_path / "sdk-captures") as collection:
        selected = CameraSelection.from_choice(collection.devices()["cameras"][0])
        assert selected == CameraSelection(index=0)
        assert collection.connect_camera(selected)["state"] == "ready"
        _wait_for_live(collection)
        episode = collection.take("SDK timed take", seconds=1.2)
        assert episode["complete"] and episode["selection"] == "unreviewed"
        assert collection.review_video(episode["id"]).is_file()
        collection.select(episode["id"], "kept")
        with zipfile.ZipFile(collection.export("source_archive")) as archive:
            assert f"{episode['id']}/camera/video.mp4" in archive.namelist()
    assert camera.released


def test_collection_context_finishes_active_take_before_releasing_devices(studio_client):
    collection, camera = studio_client
    with collection:
        collection.connect(0)
        _wait_for_live(collection)
        episode_id = collection.start("Exit while recording")["current"]
        time.sleep(1.2)
    assert collection.status()["state"] == "disconnected"
    assert camera.released
    episode = _load_episode(collection.root, episode_id)
    assert episode["complete"] and episode["stop_reason"] == "user_stop"


def test_incomplete_episode_cannot_be_kept_or_exported(studio_client):
    studio, camera = studio_client
    studio.connect(0)
    _wait_for_live(studio)
    studio.start("Camera failure")
    camera.fail_after = camera.count + 5
    result = _wait_for_result(studio)
    assert result["state"] == "error"
    episode = result["episodes"][0]
    assert not episode["complete"]
    with pytest.raises(ValueError, match="incomplete"):
        studio.select(episode["id"], "kept")
    with pytest.raises(ValueError, match="incomplete"):
        studio.review_video(episode["id"])
    with pytest.raises(ValueError, match="no kept"):
        studio.export()
    assert (studio.root / episode["id"] / "manifest.json").is_file()


def test_origin_and_path_controls(studio_client):
    studio, _ = studio_client
    with pytest.raises(ValueError, match="invalid episode ID"):
        _load_episode(studio.root, "../other")
    with pytest.raises(ValueError, match="no kept"):
        studio.export()


def test_one_attached_glove_cannot_connect(studio_client, monkeypatch):
    studio, _ = studio_client

    def one_glove_only():
        raise oglo.UsbError("connect_pair needs two gloves; found 1")

    monkeypatch.setattr(oglo, "connect_pair", one_glove_only)
    with pytest.raises(oglo.UsbError, match="needs two gloves"):
        studio.connect(0)
    assert studio.status()["state"] == "disconnected"
    assert studio.status()["gloves"] == []


def test_close_releases_connected_devices(studio_client):
    studio, camera = studio_client
    studio.connect(0)
    _wait_for_live(studio)
    studio.close()
    assert studio.status()["state"] == "disconnected"
    assert studio.status()["gloves"] == []
    assert studio.camera is None
    assert not studio._monitor_threads
    assert camera.released


def test_dark_camera_cannot_start_a_take(tmp_path, monkeypatch):
    camera = Camera()
    camera.read = lambda: (True, np.zeros((48, 64, 3), dtype=np.uint8))
    monkeypatch.setattr(cv2, "VideoCapture", lambda _: camera)
    monkeypatch.setattr(oglo, "connect_pair", lambda: (
        simulated_studio_glove("left"), simulated_studio_glove("right")))
    studio = Studio(tmp_path / "captures")
    try:
        studio.connect(0)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not studio.live()["camera"]["dark"]:
            time.sleep(0.05)
        assert studio.live()["camera"]["dark"]
        with pytest.raises(ValueError, match="too dark"):
            studio.start("Dark scene")
    finally:
        studio.close()


def test_discovered_gloves_can_be_selected_by_verified_side(studio_client, monkeypatch):
    studio, _ = studio_client
    ports = [SimpleNamespace(device="/dev/oglo-left", product="OGLO"),
             SimpleNamespace(device="/dev/oglo-right", product="OGLO")]
    monkeypatch.setattr(studio_module, "list_candidates", lambda: ports)
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [
        {"index": 2, "label": "OVISION USB camera"}])
    opened = []

    def connect(*, port, timeout=6.0):
        opened.append(port)
        return simulated_studio_glove("left" if port.endswith("left") else "right")

    monkeypatch.setattr(oglo, "connect", connect)
    found = studio.devices()
    assert found["cameras"] == [{"index": 2, "label": "OVISION USB camera"}]
    assert [(item["port"], item["side"]) for item in found["gloves"]] == [
        ("/dev/oglo-left", "left"), ("/dev/oglo-right", "right")]
    assert len(opened) == 2

    with pytest.raises(ValueError, match="expected left"):
        studio.connect(2, "/dev/oglo-right", "/dev/oglo-left")
    assert studio.status()["gloves"] == []
    with pytest.raises(ValueError, match="distinct"):
        studio.connect(2, "/dev/oglo-left", "/dev/oglo-left")
    assert studio.connect(2, "/dev/oglo-left", "/dev/oglo-right")["state"] == "ready"
    assert studio.glove_ports == {"left": "/dev/oglo-left", "right": "/dev/oglo-right"}
    assert len(opened) == 5
    assert [item["side"] for item in studio.devices()["gloves"]] == ["left", "right"]
    assert len(opened) == 5  # Discovery never reopens live gloves.


def test_selected_glove_connection_error_names_the_side(studio_client, monkeypatch):
    studio, _ = studio_client
    monkeypatch.setattr(studio_module, "list_candidates", lambda: [
        SimpleNamespace(device="/dev/left"), SimpleNamespace(device="/dev/right")])

    def failed_connect(*, port):
        raise oglo.UsbError("no #CONFIG from the board")

    monkeypatch.setattr(oglo, "connect", failed_connect)
    with pytest.raises(RuntimeError, match="left glove at /dev/left: no #CONFIG"):
        studio.connect(0, "/dev/left", "/dev/right")


def test_avfoundation_camera_names_are_exposed_as_choices(monkeypatch):
    monkeypatch.setattr(studio_module.sys, "platform", "darwin")
    monkeypatch.setattr(studio_module.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="0|Built-in Camera|\n"
                                        "1|OVISION EGO V1|stereo\n"))
    assert studio_module._camera_choices() == [
        {"index": 0, "mode": "default", "label": "Built-in Camera · standard view · index 0"},
        {"index": 1, "mode": "ovision_left", "name": "OVISION EGO V1",
         "label": "OVISION EGO V1 · OVISION left preview · saves both eyes"},
        {"index": 1, "mode": "ovision_right", "name": "OVISION EGO V1",
         "label": "OVISION EGO V1 · OVISION right preview · saves both eyes"},
        {"index": 1, "mode": "default", "label": "OVISION EGO V1 · standard view · index 1"},
    ]


def test_avfoundation_ovision_reader_keeps_both_eyes(monkeypatch):
    command = []
    monkeypatch.setattr(studio_module, "_ffmpeg_executable", lambda: "/usr/bin/ffmpeg")

    def popen(args, **_kwargs):
        command.extend(args)
        return SimpleNamespace()

    monkeypatch.setattr(studio_module.subprocess, "Popen", popen)
    device = studio_module.FFmpegEyeCapture("OVISION USB camera", "right")
    assert (device.width, device.height) == (3840, 1080)
    assert "crop" not in " ".join(command)
    assert "3840x1080" in command


@pytest.mark.parametrize("eye,channel", [("left", 1), ("right", 2)])
def test_ovision_preview_eye_preserves_packed_source(studio_client, monkeypatch, eye, channel):
    studio, _ = studio_client

    class PackedCamera:
        def __init__(self, name, selected_eye):
            assert selected_eye == eye
            self.frame = np.zeros((48, 128, 3), dtype=np.uint8)
            self.frame[:, :64, 1] = 180
            self.frame[:, 64:, 2] = 180

        def isOpened(self):
            return True

        def set(self, *_args):
            return False

        def getBackendName(self):
            return "simulated packed OVISION"

        def read(self):
            time.sleep(0.03)
            return True, self.frame.copy()

        def release(self):
            pass

    monkeypatch.setattr(studio_module, "FFmpegEyeCapture", PackedCamera)
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [
        {"index": 1, "mode": f"ovision_{eye}", "name": "OVISION USB camera"}])
    studio.connect(1, camera_mode=f"ovision_{eye}", camera_name="OVISION USB camera")
    _wait_for_live(studio)
    assert studio.status()["camera"]["size"] == (64, 48)
    studio.start("Packed source test")
    time.sleep(1.15)
    studio.stop()
    result = _wait_for_result(studio)
    assert result["state"] == "review", result.get("error")
    episode = result["episodes"][0]
    assert episode["camera"]["kind"] == "ovision_uvc_stereo_host_timed"
    assert episode["camera"]["width"] == 128
    assert episode["camera"]["preview_width"] == 64
    assert episode["delivery_validation"]["og_center_postprocessing"].startswith("blocked")
    source = cv2.VideoCapture(str(studio.root / episode["id"] / "camera/video.mp4"))
    try:
        ok, frame = source.read()
        assert ok and frame.shape[1] == 128
        assert frame[:, :64, 1].mean() > 130
        assert frame[:, 64:, 2].mean() > 130
    finally:
        source.release()
    review = cv2.VideoCapture(str(studio.review_video(episode["id"])))
    try:
        ok, frame = review.read()
        assert ok and frame.shape[1] == 64
        assert frame[:, :, channel].mean() > 130
    finally:
        review.release()
    studio.select(episode["id"], "kept")
    with pytest.raises(ValueError, match="no kept"):
        studio.export("og_center_postprocessing")
    with zipfile.ZipFile(studio.export("source_archive")) as archived:
        assert archived.read(f"{episode['id']}/camera/video.mp4") == (
            studio.root / episode["id"] / "camera/video.mp4").read_bytes()


def test_ovision_eye_uses_device_name_when_index_changes(studio_client, monkeypatch):
    studio, _ = studio_client
    monkeypatch.setattr(studio_module, "list_candidates", lambda: [
        SimpleNamespace(device="/dev/left"), SimpleNamespace(device="/dev/right")])
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [
        {"index": 2, "mode": "ovision_left", "name": "UVC Camera 1"}])
    monkeypatch.setattr(oglo, "connect", lambda *, port: simulated_studio_glove(
        "left" if port.endswith("left") else "right"))
    chosen = []

    def camera_factory(index, **options):
        chosen.append((index, options))
        return studio_module.CameraWorker(index)

    studio.camera_factory = camera_factory
    studio.connect(0, "/dev/left", "/dev/right", "ovision_left", "UVC Camera 1")
    assert chosen == [(2, {"mode": "ovision_left", "name": "UVC Camera 1"})]


def test_annotation_handoff_is_rejected_before_webcam_capture(studio_client):
    studio, _ = studio_client
    studio.connect(0)
    _wait_for_live(studio)
    with pytest.raises(ValueError, match="native frame timestamps"):
        studio.start("Not deliverable", profile="annotation_handoff")
    assert list(studio.root.glob("ep_*")) == []


def test_og_center_profile_is_rejected_without_native_camera(studio_client):
    studio, _ = studio_client
    studio.connect(0)
    _wait_for_live(studio)
    with pytest.raises(ValueError, match="native OVISION stereo"):
        studio.start("Not sensor complete", profile="og_center_postprocessing")
    assert list(studio.root.glob("ep_*")) == []


def test_og_center_profile_rejects_incomplete_capable_adapter(studio_client, monkeypatch):
    studio, _ = studio_client
    import oglo.studio_ovision as native

    class IncompleteNative(studio_module.CameraWorker):
        native_device_timestamps = True
        postprocessing_capable = True

        def __init__(self, index, *, mode, name, root):
            super().__init__(index)
            self.mode, self.name, self.eye = mode, name, "left"

    monkeypatch.setattr(native, "NativeOvisionCameraWorker", IncompleteNative)
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [
        {"index": 1, "mode": "ovision_native_left", "name": "/dev/video1"}])
    studio.connect(1, camera_mode="ovision_native_left", camera_name="/dev/video1")
    _wait_for_live(studio)
    studio.start("Incomplete native fixture", profile="og_center_postprocessing")
    time.sleep(1.15)
    studio.stop()
    result = _wait_for_result(studio)
    assert result["state"] == "error"
    assert "native OVISION stereo recording" in result["episodes"][0]["error"]
    assert result["episodes"][0]["delivery_validation"]["og_center_postprocessing"] != "ready"


def test_native_ovision_worker_retains_all_sensor_sidecars(tmp_path, monkeypatch):
    pytest.importorskip("syncfield.adapters.ovision_camera")
    import oglo.studio_ovision as native
    import syncfield.adapters.ovision_camera as adapter

    @dataclass
    class Report:
        status: str = "completed"
        error: str | None = None
        frame_count: int = 2

    class FakeNativeStream:
        def __init__(self, id, output_dir, **_kwargs):
            self._output_dir = output_dir
            self._file_path = output_dir / f"{id}.mp4"
            self.latest_frame = np.full((48, 64, 3), 90, dtype=np.uint8)

        def prepare(self):
            pass

        def connect(self):
            pass

        def capture_ready(self):
            return True

        def start_recording(self, _clock):
            folder = self._output_dir
            first = time.monotonic_ns()
            rows = [{"frame_number": n, "capture_ns": first + n * 33_333_333,
                     "device_timestamp_ns": 1000 + n * 33_333_333,
                     "left_exposure_start_ns": 1000 + n * 33_333_333}
                    for n in range(2)]
            self._file_path.write_bytes(b"packed stereo fixture")
            (folder / "cam_ego.stereo.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
            motion_channels = {
                "imu": {name: 0 for name in ("accel_x", "accel_y", "accel_z",
                                              "gyro_x", "gyro_y", "gyro_z")},
                "accel": {name: 0 for name in ("accel_x", "accel_y", "accel_z")},
                "gyro": {name: 0 for name in ("gyro_x", "gyro_y", "gyro_z")},
                "mag": {"mag_x_raw": 0},
            }
            for stream, channels in motion_channels.items():
                rows = [{
                    "frame_number": 0, "capture_ns": first,
                    "device_timestamp_ns": 1000, "channels": channels,
                }]
                if stream != "mag":
                    # Native packet jitter can reverse estimated host IMU time
                    # while the measured device clock continues forward.
                    rows.append({"frame_number": 1, "capture_ns": first - 10,
                                 "device_timestamp_ns": 2000, "channels": channels})
                (folder / f"cam_ego.{stream}.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows))
            identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
            right_from_left = [row.copy() for row in identity]
            right_from_left[0][3] = 0.06
            calibration = {"schema": "syncfield.ovision_calibration.v1",
                           "stereo": {"layout": "side_by_side", "packed_resolution": [3840, 1080],
                                      "eye_order": ["left", "right"], "synchronization": "internal_fsync",
                                      "T_right_left": right_from_left},
                           "streams": {eye: {"resolution": [1920, 1080],
                                             "intrinsics": [[600, 0, 960], [0, 600, 540], [0, 0, 1]],
                                             "distortion_coeffs": [0.1, 0, 0, 0],
                                             "T_cam_imu": identity}
                                       for eye in ("left", "right")}}
            (folder / "cam_ego.calibration.json").write_text(json.dumps(calibration))
            (folder / "cam_ego.calibration.yaml").write_text("stereo: true\n")
            (folder / "cam_ego.calibration.bin").write_bytes(b"calibration")

        def stop_recording(self):
            return Report()

        def disconnect(self):
            pass

    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native, "version", lambda _: "0.8.14")
    monkeypatch.setattr(adapter, "OvisionCameraStream", FakeNativeStream)
    worker = native.NativeOvisionCameraWorker(
        1, mode="ovision_native_right", name="/dev/video1", root=tmp_path)
    try:
        episode_id = "ep_" + "1" * 16
        folder = tmp_path / episode_id / "camera"
        folder.parent.mkdir()
        worker.begin(folder, threading.Event())
        result = worker.finish()
        assert result["kind"] == "ovision_native_stereo"
        assert result["eye"] == "right" and result["frames_submitted"] == 2
        Studio._validate_native_ovision(folder.parent, result, 2)
        rows = [json.loads(line) for line in (folder / "timestamps.jsonl").read_text().splitlines()]
        assert rows[0]["device_timestamp_meaning"] == "left_exposure_start"
        assert rows[1]["device_timestamp"] == 33_334_333
        manifest = {"id": episode_id, "complete": True, "selection": "kept",
                    "delivery_validation": {"og_center_postprocessing": "ready"},
                    "gloves": [{"side": "left"}, {"side": "right"}]}
        (folder.parent / "manifest.json").write_text(json.dumps(manifest))
        (folder.parent / "inventory.json").write_text(json.dumps({
            "files": studio_module._inventory(folder.parent)}))
        with zipfile.ZipFile(Studio(tmp_path).export("og_center_postprocessing")) as archive:
            names = set(archive.namelist())
            assert {f"{episode_id}/camera/{name}" for name in (
                "cam_ego.mp4", "cam_ego.stereo.jsonl", "cam_ego.imu.jsonl",
                "cam_ego.accel.jsonl", "cam_ego.gyro.jsonl", "cam_ego.mag.jsonl",
                "cam_ego.calibration.json", "cam_ego.calibration.yaml",
                "cam_ego.calibration.bin", "sync_point.json", "timestamps.jsonl",
            )} <= names
        calibration_path = folder / "cam_ego.calibration.json"
        calibration = json.loads(calibration_path.read_text())
        without_baseline = json.loads(calibration_path.read_text())
        without_baseline["stereo"].pop("T_right_left")
        calibration_path.write_text(json.dumps(without_baseline))
        with pytest.raises(RuntimeError, match="stereo/IMU geometry"):
            Studio._validate_native_ovision(folder.parent, result, 2)
        calibration_path.write_text(json.dumps(calibration))
        (folder / "cam_ego.gyro.jsonl").unlink()
        with pytest.raises(RuntimeError, match="cam_ego.gyro"):
            Studio._validate_native_ovision(folder.parent, result, 2)
        second = tmp_path / "second" / "camera"
        second.parent.mkdir()
        worker.begin(second, threading.Event())
        assert worker.finish()["frames_submitted"] == 2
        assert (second / "cam_ego.mp4").is_file()
    finally:
        worker.close()


def test_export_rejects_modified_recording_bytes(studio_client):
    studio, _ = studio_client
    studio.connect(0)
    _wait_for_live(studio)
    studio.start("Pick up a cup")
    time.sleep(1.1)
    studio.stop()
    result = _wait_for_result(studio)
    assert result["state"] == "review", result.get("error")
    episode = result["episodes"][0]
    studio.select(episode["id"], "kept")
    video = studio.root / episode["id"] / "camera/video.mp4"
    with video.open("ab") as output:
        output.write(b"tampered")
    with pytest.raises(RuntimeError, match="source file changed"):
        studio.export()


def test_pair_and_trigger_provenance(studio_client, monkeypatch):
    studio, _ = studio_client
    monkeypatch.setattr(oglo, "connect_pair", lambda: (
        simulated_studio_glove("left"), simulated_studio_glove("right")))
    status = studio.connect(0)
    assert [entry["side"] for entry in status["gloves"]] == ["left", "right"]
    _wait_for_live(studio)
    with pytest.raises(ValueError, match="threshold"):
        studio.calibrate(501)
    calibrated = studio.calibrate(70)
    assert calibrated["state"] == "ready"
    assert set(calibrated["calibration"]["gloves"]) == {"left", "right"}
    assert all(len(calibrated["calibration"]["gloves"][side]["noise"]) == 80
               for side in ("left", "right"))
    assert all(glove["threshold"] == 70 for glove in calibrated["gloves"])
    assert all(calibrated["calibration"]["gloves"][side]["threshold"] == 70
               for side in ("left", "right"))
    _wait_for_live(studio)
    studio.start("Two-hand transfer", source="keyboard", mapping={
        "toggle": "F9", "confirm": "F10", "discard": "F11"})
    time.sleep(1.2)
    studio.stop("keyboard")
    result = _wait_for_result(studio)
    assert result["state"] == "review", result.get("error")
    episode = result["episodes"][0]
    assert episode["trigger_mapping"]["toggle"] == "F9"
    assert [entry["source"] for entry in episode["actions"]] == ["keyboard", "keyboard"]
    assert {entry["side"] for entry in episode["gloves"]} == {"left", "right"}
    for entry in episode["gloves"]:
        assert (studio.root / episode["id"] / entry["calibration"]).is_file()


def test_realsense_choice_is_listed_first_on_linux(monkeypatch):
    monkeypatch.setattr(studio_module.sys, "platform", "linux")
    fake_realsense.install(monkeypatch)
    choices = studio_module._camera_choices()
    assert choices[0] == REALSENSE_CHOICE


def test_no_realsense_choice_without_pyrealsense2(monkeypatch):
    monkeypatch.setattr(studio_module.sys, "platform", "linux")
    monkeypatch.delitem(sys.modules, "pyrealsense2", raising=False)
    choices = studio_module._camera_choices()
    assert all(choice.get("mode") != "realsense" for choice in choices)


def test_realsense_take_is_ready_for_annotation_handoff_and_exports_imu_files(studio_client, monkeypatch):
    studio, _ = studio_client
    fake_realsense.install(monkeypatch)
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [REALSENSE_CHOICE])
    studio.connect(0, camera_mode="realsense", camera_name=REALSENSE_SERIAL)
    _wait_for_live(studio)
    studio.start("RealSense annotation take", profile="annotation_handoff")
    time.sleep(1.2)
    studio.stop()
    result = _wait_for_result(studio)
    assert result["state"] == "review", result.get("error")
    episode = result["episodes"][0]
    assert episode["complete"] is True
    assert episode["camera"]["kind"] == "realsense"
    assert episode["delivery_validation"]["annotation_handoff"] == "ready"
    assert episode["delivery_validation"]["og_center_postprocessing"].startswith("blocked")
    studio.select(episode["id"], "kept")
    with zipfile.ZipFile(studio.export("annotation_handoff")) as archive:
        names = archive.namelist()
        assert f"{episode['id']}/camera/realsense.accel.jsonl" in names
        assert f"{episode['id']}/camera/realsense.gyro.jsonl" in names
        assert f"{episode['id']}/camera/realsense.calibration.json" in names


def test_realsense_og_center_profile_is_refused(studio_client, monkeypatch):
    studio, _ = studio_client
    fake_realsense.install(monkeypatch)
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [REALSENSE_CHOICE])
    studio.connect(0, camera_mode="realsense", camera_name=REALSENSE_SERIAL)
    _wait_for_live(studio)
    with pytest.raises(ValueError, match="native OVISION stereo"):
        studio.start("Not sensor complete", profile="og_center_postprocessing")
    assert list(studio.root.glob("ep_*")) == []


def test_realsense_take_without_camera_clock_time_ends_incomplete(studio_client, monkeypatch):
    studio, _ = studio_client
    fake_realsense.install(monkeypatch, fake_realsense.Hardware(metadata=False, honor_global_time=False))
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: [REALSENSE_CHOICE])
    studio.connect(0, camera_mode="realsense", camera_name=REALSENSE_SERIAL)
    _wait_for_live(studio)
    studio.start("No camera clock", profile="source_archive")
    time.sleep(0.6)
    studio.stop()
    result = _wait_for_result(studio)
    assert result["state"] == "error"
    assert "camera-clock time" in result["episodes"][0]["error"]


def test_server_rejects_unknown_camera_mode_but_accepts_realsense(tmp_path):
    import uvicorn
    import requests

    studio = Studio(tmp_path / "server-captures")
    app = create_app(studio.root, studio=studio)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        bad = requests.post(f"{base}/api/connect", json={"camera_mode": "not-a-mode"})
        assert bad.status_code == 422
        accepted = requests.post(f"{base}/api/connect", json={
            "camera_mode": "realsense", "camera_name": REALSENSE_SERIAL})
        assert accepted.status_code != 422
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        studio.close()


def test_reconnecting_the_realsense_in_use_works_when_listing_hides_it(studio_client, monkeypatch):
    """librealsense's libusb backend can hide a camera it is streaming from a second
    device listing; reconnecting the RealSense already in use must still work."""
    studio, _ = studio_client
    hardware = fake_realsense.install(monkeypatch)
    listed = {"choices": [REALSENSE_CHOICE]}
    monkeypatch.setattr(studio_module, "_camera_choices", lambda: listed["choices"])
    studio.connect(0, camera_mode="realsense", camera_name=REALSENSE_SERIAL)
    _wait_for_live(studio)
    listed["choices"] = []  # The streaming camera no longer shows up.
    (choice,) = [c for c in studio.devices()["cameras"] if c.get("mode") == "realsense"]
    assert choice["name"] == REALSENSE_SERIAL and choice["label"].endswith("· connected")
    studio.connect(0, camera_mode="realsense", camera_name=REALSENSE_SERIAL)
    _wait_for_live(studio)
    assert hardware.starts == 2 and studio.camera.name == REALSENSE_SERIAL
    other = "000000000000"
    with pytest.raises(ValueError, match="no longer available"):
        studio.connect(0, camera_mode="realsense", camera_name=other)
