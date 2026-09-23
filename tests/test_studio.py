"""Exercise the local collection controller with synthetic USB and real MP4 files."""

import json
import time
import zipfile

import oglo
import oglo.studio as studio_module
import pytest
from types import SimpleNamespace

cv2 = pytest.importorskip("cv2")
pytest.importorskip("fastapi")

from oglo.studio import Studio, _load_episode, create_app
from test_camera_glove import Camera
from studio_fake_devices import simulated_studio_glove


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
        if (live["camera"] and live["camera"]["ready"] and
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


def test_avfoundation_camera_names_are_exposed_as_choices(monkeypatch):
    monkeypatch.setattr(studio_module.sys, "platform", "darwin")
    monkeypatch.setattr(studio_module.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stderr="AVFoundation video devices:\n"
                                        "[AVFoundation indev] [0] Built-in Camera\n"
                                        "[AVFoundation indev] [1] OVISION EGO V1\n"
                                        "AVFoundation audio devices:\n"))
    assert studio_module._camera_choices() == [
        {"index": 0, "label": "Built-in Camera"},
        {"index": 1, "label": "OVISION EGO V1"},
    ]


def test_annotation_handoff_is_rejected_before_webcam_capture(studio_client):
    studio, _ = studio_client
    studio.connect(0)
    _wait_for_live(studio)
    with pytest.raises(ValueError, match="native frame timestamps"):
        studio.start("Not deliverable", profile="annotation_handoff")
    assert list(studio.root.glob("ep_*")) == []


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
