"""Single-operator localhost collection service.

The camera worker is the only camera reader. Each glove is owned by one SDK
recorder while an episode is active; the UI never drains sensor samples.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import oglo
from oglo._usb import list_candidates


@dataclass(frozen=True)
class CameraSelection:
    """One device choice returned by :meth:`Collection.devices`."""

    index: int
    mode: str = "default"
    name: str | None = None

    @classmethod
    def from_choice(cls, choice: dict) -> CameraSelection:
        """Turn one ``devices()['cameras']`` row into a stable selection."""
        return cls(index=choice["index"], mode=choice.get("mode", "default"),
                   name=choice.get("name"))


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path) -> list[dict]:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise RuntimeError("recording folder contains a symlink")
    return [
        {"path": file.relative_to(root).as_posix(), "size": file.stat().st_size,
         "sha256": _sha256(file)}
        for file in sorted(root.rglob("*"))
        if file.is_file() and file.name not in {"manifest.json", "inventory.json"}
    ]


def _load_episode(root: Path, episode_id: str) -> dict:
    if not re.fullmatch(r"ep_[0-9a-f]{16}", episode_id):
        raise ValueError("invalid episode ID")
    path = root / episode_id / "manifest.json"
    if not path.is_file():
        raise ValueError("episode not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _episode_video_path(root: Path, episode_id: str) -> Path:
    manifest = _load_episode(root, episode_id)
    if not manifest.get("complete"):
        raise ValueError("episode is incomplete")
    relative = manifest.get("camera", {}).get("video")
    if type(relative) is not str or not relative.startswith("camera/") or not relative.endswith(".mp4"):
        raise ValueError("episode video is unavailable")
    folder = (root / episode_id).resolve()
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        raise ValueError("episode video is unavailable")
    return path


def _review_eye_crop(manifest: dict) -> tuple[int, int, int] | None:
    """Return the selected-eye crop for a packed source video."""
    camera = manifest.get("camera", {})
    if camera.get("kind") not in {"ovision_uvc_stereo_host_timed", "ovision_native_stereo"}:
        return None
    width, height = camera.get("width"), camera.get("height")
    eye = camera.get("eye")
    if type(width) is not int or type(height) is not int or width % 2 or eye not in {"left", "right"}:
        raise ValueError("packed OVISION review metadata is invalid")
    return width // 2, height, 0 if eye == "left" else width // 2


def _realsense_choices() -> list[dict]:
    """List RealSense cameras with accel+gyro, without opening a stream."""
    try:
        from oglo.studio_realsense import list_devices
        devices = list_devices()
    except Exception:
        return []
    return [{"index": index, "mode": "realsense", "name": device["serial"],
             "label": f"{device['name']} · color + camera IMU · serial {device['serial']}"}
            for index, device in enumerate(devices) if device.get("has_imu")]


def _camera_choices() -> list[dict]:
    """List camera modes without opening a device that may already be recording."""
    cameras = []
    if sys.platform.startswith("linux"):
        cameras.extend(_realsense_choices())
        for node in sorted(Path("/sys/class/video4linux").glob("video*"),
                           key=lambda path: int(path.name[5:]) if path.name[5:].isdigit() else 99):
            if node.name[5:].isdigit():
                index = int(node.name[5:])
                if index <= 32 and Path(f"/dev/{node.name}").exists():
                    try:
                        name = (node / "name").read_text(encoding="utf-8").strip()
                    except OSError:
                        name = "USB camera"
                    device = f"/dev/{node.name}"
                    try:
                        formats = subprocess.run(
                            ["v4l2-ctl", "--device", device, "--list-formats-ext"],
                            capture_output=True, text=True, timeout=2, check=False)
                        native = (formats.returncode == 0 and
                                  re.search(r"'H264'.*?3840x1080", formats.stdout, re.S))
                    except (OSError, subprocess.TimeoutExpired):
                        native = False
                    if native:
                        for eye in ("left", "right"):
                            cameras.append({"index": index, "mode": f"ovision_native_{eye}",
                                            "name": device,
                                            "label": f"{name} · native OVISION · {eye} preview · {device}"})
                    cameras.append({"index": index, "mode": "default",
                                    "label": f"{name} · {device}"})
    elif sys.platform == "darwin":
        # OpenCV indexes AVFoundation's video devices followed by muxed devices.
        # FFmpeg's numeric indexes can differ, so stereo capture uses the name.
        script = """import AVFoundation
import CoreMedia
let devices = AVCaptureDevice.devices(for: .video) + AVCaptureDevice.devices(for: .muxed)
for (index, device) in devices.enumerated() {
    let stereo = device.formats.contains {
        let size = CMVideoFormatDescriptionGetDimensions($0.formatDescription)
        return size.width == 3840 && size.height == 1080
    }
    print("\\(index)|\\(device.localizedName)|\\(stereo ? \"stereo\" : \"\")")
}
"""
        try:
            result = subprocess.run(
                ["swift", "-e", script], capture_output=True, text=True,
                timeout=10, check=False)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    index, name, capability = line.split("|", 2)
                    if not index.isdigit() or int(index) > 32:
                        continue
                    camera_index = int(index)
                    if capability == "stereo" and ":" not in name:
                        for eye in ("left", "right"):
                            cameras.append({"index": camera_index, "mode": f"ovision_{eye}",
                                            "name": name,
                                            "label": f"{name} · OVISION {eye} preview · saves both eyes"})
                    cameras.append({"index": camera_index, "mode": "default",
                                    "label": f"{name} · standard view · index {camera_index}"})
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return cameras or [{"index": index, "mode": "default",
                       "label": f"Camera {index} · verify live view"}
                       for index in range(5)]


def _ffmpeg_executable() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg")


class FFmpegEyeCapture:
    """Read the complete OVISION frame; the selected eye is only for preview."""

    width, height = 3840, 1080

    def __init__(self, name: str, eye: str) -> None:
        executable = _ffmpeg_executable()
        if not executable:
            raise RuntimeError("OVISION eye capture requires FFmpeg; reinstall OGLO Studio extras")
        if eye not in {"left", "right"}:
            raise ValueError("OVISION eye must be left or right")
        self.process = subprocess.Popen([
            executable, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-video_size", "3840x1080", "-framerate", "30",
            "-i", f"{name}:none", "-an",
            "-pix_fmt", "bgr24", "-r", "30", "-f", "rawvideo", "pipe:1",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
           bufsize=0)

    def isOpened(self) -> bool:
        return self.process.poll() is None

    def getBackendName(self) -> str:
        return "FFmpeg AVFoundation OVISION eye"

    def set(self, *_args) -> bool:
        return False

    def read(self):
        import numpy as np

        size = self.width * self.height * 3
        frame = bytearray(size)
        view = memoryview(frame)
        offset = 0
        while offset < size:
            count = self.process.stdout.readinto(view[offset:])
            if not count:
                return False, None
            offset += count
        return True, np.frombuffer(frame, dtype=np.uint8).reshape(self.height, self.width, 3)

    def release(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process.stdout.close()


class CameraWorker:
    """Continuously reads one webcam for preview and bounded episode capture."""

    native_device_timestamps = False
    postprocessing_capable = False

    def __init__(self, index: int, fps: float = 30.0, *, mode: str = "default",
                 name: str | None = None) -> None:
        import cv2

        self.cv2 = cv2
        self.index = index
        self.mode = mode
        self.name = name
        self.size: tuple[int, int] | None = None
        self.fps = fps
        self.eye = mode.removeprefix("ovision_") if mode.startswith("ovision_") else None
        if self.eye and (self.eye not in {"left", "right"} or not name):
            raise ValueError("a named OVISION left or right eye is required")
        self.device = FFmpegEyeCapture(name, self.eye) if self.eye and name else cv2.VideoCapture(index)
        if not self.device.isOpened():
            self.device.release()
            raise RuntimeError(f"camera {index} could not be opened")
        self.fps_request_accepted = bool(self.device.set(cv2.CAP_PROP_FPS, fps))
        self.backend = self.device.getBackendName()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._capture = None
        self._last_capture = None
        self._jpeg = None
        self._last_frame_ns = None
        self._brightness: float | None = None
        self._dark_samples = 0
        self.error = None
        self._thread = threading.Thread(target=self._run, name="oglo-camera", daemon=True)
        self._thread.start()

    def preview(self) -> bytes | None:
        with self._lock:
            return self._jpeg

    def live_status(self) -> dict:
        with self._lock:
            last = self._last_frame_ns
            return {"ready": self._jpeg is not None and self.error is None,
                    "age_ms": round((time.monotonic_ns() - last) / 1_000_000) if last else None,
                    "dark": self._dark_samples >= 3,
                    "brightness": self._brightness,
                    "error": self.error}

    def begin(self, folder: Path, stop: threading.Event) -> None:
        folder.mkdir()
        capture = {"folder": folder, "stop": stop, "done": threading.Event(),
                   "writer": None, "rows": None, "count": 0, "first": None,
                   "last": None, "size": None, "error": None, "result": None}
        with self._lock:
            if self.error or self._capture is not None:
                raise RuntimeError(self.error or "camera is already capturing")
            self._capture = capture
            self._last_capture = capture

    def finish(self, timeout: float = 10.0) -> dict:
        with self._lock:
            capture = self._last_capture
        if capture is None:
            raise RuntimeError("camera capture was not started")
        capture["stop"].set()
        if not capture["done"].wait(timeout):
            raise RuntimeError("camera did not finish after Stop; capture is incomplete")
        if capture["error"]:
            raise RuntimeError(capture["error"])
        return capture["result"]

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            if self._capture is not None:
                self._capture["stop"].set()
        if isinstance(self.device, FFmpegEyeCapture):
            self.device.release()  # Unblock a pipe read before joining the worker.
        self._thread.join(timeout=2)
        # Avoid releasing a device while a blocked driver read still owns it.
        if not self._thread.is_alive() and not isinstance(self.device, FFmpegEyeCapture):
            self.device.release()

    def _seal(self, capture: dict) -> None:
        try:
            if capture["rows"] is not None:
                capture["rows"].close()
            if capture["writer"] is not None:
                capture["writer"].release()
            if capture["count"] < 2 and not capture["error"]:
                capture["error"] = "camera captured fewer than two frames"
            capture["result"] = {
                "kind": "ovision_uvc_stereo_host_timed" if self.eye else "usb_webcam",
                "index": self.index, "mode": self.mode, "name": self.name,
                "eye": self.eye, "source_eye_order": ["left", "right"] if self.eye else None,
                "source_width": (capture["size"] or (0, 0))[0] if self.eye else None,
                "source_height": (capture["size"] or (0, 0))[1] if self.eye else None,
                "preview_width": (capture["size"] or (0, 0))[0] // 2 if self.eye else None,
                "preview_height": (capture["size"] or (0, 0))[1] if self.eye else None,
                "video": "camera/video.mp4", "timestamps": "camera/timestamps.jsonl",
                "codec": "mp4v", "playback_fps": self.fps,
                "requested_fps": self.fps,
                "fps_request_accepted": self.fps_request_accepted,
                "backend": self.backend,
                "host_timestamp_meaning": (
                    "FFmpeg pipe read-return time, not exposure time" if self.eye
                    else "OpenCV read-return time, not exposure time"),
                "native_device_timestamps": False,
                "frames_submitted": capture["count"], "width": (capture["size"] or (0, 0))[0],
                "height": (capture["size"] or (0, 0))[1],
                "first_host_received_ns": capture["first"],
                "last_host_received_ns": capture["last"],
            }
        finally:
            with self._lock:
                self._capture = None
            capture["done"].set()

    def _run(self) -> None:
        last_preview = 0.0
        try:
            while not self._closed.is_set():
                with self._lock:
                    capture = self._capture
                if capture is not None and capture["stop"].is_set():
                    self._seal(capture)
                    continue
                started = time.monotonic_ns()
                ok, frame = self.device.read()
                received = time.monotonic_ns()
                if not ok or frame is None:
                    raise RuntimeError("camera stopped returning frames")
                height, width = frame.shape[:2]
                if self.eye and width % 2:
                    raise RuntimeError("OVISION packed frame width must be even")
                preview = (frame[:, :width // 2] if self.eye == "left" else
                           frame[:, width // 2:] if self.eye == "right" else frame)
                with self._lock:
                    self._last_frame_ns = received
                    self.size = (preview.shape[1], preview.shape[0])
                now = time.monotonic()
                if now - last_preview >= 0.2:
                    brightness = float(preview[::16, ::16].mean())
                    encoded, jpeg = self.cv2.imencode(".jpg", preview)
                    with self._lock:
                        self._brightness = round(brightness, 1)
                        self._dark_samples = min(3, self._dark_samples + 1) if brightness < 18 else 0
                        if encoded:
                            self._jpeg = jpeg.tobytes()
                    last_preview = now
                if capture is None:
                    continue
                if capture["writer"] is None:
                    if width % 2 or height % 2:
                        raise RuntimeError("camera dimensions must be even for MP4")
                    capture["size"] = (width, height)
                    capture["writer"] = self.cv2.VideoWriter(
                        str(capture["folder"] / "video.mp4"),
                        self.cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (width, height))
                    if not capture["writer"].isOpened():
                        raise RuntimeError("camera MP4 writer could not be opened")
                    capture["rows"] = (capture["folder"] / "timestamps.jsonl").open("x", encoding="utf-8")
                if capture["size"] != (width, height):
                    raise RuntimeError("camera dimensions changed during recording")
                capture["writer"].write(frame)
                capture["rows"].write(json.dumps({
                    "frame_index": capture["count"], "host_read_started_ns": started,
                    "host_received_ns": received, "device_timestamp": None,
                    "device_timestamp_unit": None, "device_clock_domain": None,
                    "device_timestamp_meaning": None,
                }) + "\n")
                capture["count"] += 1
                capture["last"] = received
                if capture["first"] is None:
                    capture["first"] = received
        except BaseException as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                capture = self._capture
            if capture is not None:
                capture["error"] = self.error
                capture["stop"].set()
                self._seal(capture)


class Collection:
    """Capture, validate, review, and export a paired-glove camera session.

    The same controller backs the localhost UI and the Python SDK. Camera and
    glove streams have one owner, so a script and the UI cannot accidentally
    drain the same device independently.
    """

    def __init__(self, root: Path, camera_factory=CameraWorker) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.gloves = []
        self.glove_ports: dict[str, str] = {}
        self.camera: CameraWorker | None = None
        self.state = "disconnected"
        self.current: str | None = None
        self.stop_event: threading.Event | None = None
        self._active_manifest: dict | None = None
        self.error: str | None = None
        self.last_calibrated_wall_ns: int | None = None
        self.last_calibration: dict | None = None
        self.camera_factory = camera_factory
        self._preview_lock = threading.Lock()
        self._review_lock = threading.Lock()
        self._live_tactile: dict = {}
        self._baselines: dict = {}
        self._preview_errors: dict = {}
        self._monitor_stop: threading.Event | None = None
        self._monitor_threads: list[threading.Thread] = []

    def __enter__(self) -> Collection:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def status(self) -> dict:
        with self._lock:
            episodes = []
            for path in sorted(self.root.glob("ep_*/manifest.json")):
                try:
                    episodes.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            episodes.sort(key=lambda episode: (episode.get("started_wall_time_ns", 0), episode.get("id", "")))
            return {"state": self.state, "current": self.current, "error": self.error,
                    "gloves": [{"side": g.info.side, "serial": g.info.serial,
                                "zero_valid": g.info.zero_valid,
                                "threshold": g.info.stream_thr,
                                "stream_clean": g.info.stream_clean} for g in self.gloves],
                    "camera": {"index": self.camera.index, "mode": self.camera.mode,
                               "name": self.camera.name, "eye": getattr(self.camera, "eye", None),
                               "size": self.camera.size,
                               "error": self.camera.error,
                               "native_device_timestamps": self.camera.native_device_timestamps,
                               "postprocessing_capable": getattr(self.camera, "postprocessing_capable", False)} if self.camera else None,
                    "calibration": self.last_calibration,
                    "episodes": episodes,
                    "delivery_profiles": {
                        "source_archive": "available with a complete validated episode",
                        "annotation_handoff": "available with native camera timing" if
                        self.camera and self.camera.native_device_timestamps else
                        "requires a camera with native frame timestamps",
                        "og_center_postprocessing": "available with native OVISION stereo, IMU, and calibration" if
                        self.camera and getattr(self.camera, "postprocessing_capable", False) else
                        "requires Linux native OVISION capture",
                    }}

    def live(self) -> dict:
        with self._preview_lock:
            gloves = {side: dict(value) for side, value in self._live_tactile.items()}
            errors = dict(self._preview_errors)
        now = time.monotonic_ns()
        for side, value in gloves.items():
            value["age_ms"] = round((now - value["host_received_ns"]) / 1_000_000)
        return {"camera": self.camera.live_status() if self.camera else None,
                "gloves": gloves, "errors": errors}

    def devices(self) -> dict:
        """Discover choices without disturbing already connected streams."""
        with self._lock:
            cameras = _camera_choices()
            if self.gloves:
                gloves = [{"port": self.glove_ports.get(glove.info.side, ""),
                           "side": glove.info.side, "serial": glove.info.serial,
                           "label": f"{glove.info.serial} · connected"}
                          for glove in self.gloves]
            else:
                gloves = []
                for candidate in list_candidates():
                    choice = {"port": candidate.device, "side": None, "serial": None,
                              "label": f"{candidate.product or 'OGLO USB'} · {candidate.device}"}
                    try:
                        glove = oglo.connect(port=candidate.device, timeout=3.0)
                        try:
                            choice.update(side=glove.info.side, serial=glove.info.serial,
                                          label=f"{glove.info.serial} · {candidate.device}")
                        finally:
                            glove.close()
                    except Exception as exc:
                        choice["label"] += f" · {type(exc).__name__}"
                    gloves.append(choice)
            return {"cameras": cameras, "gloves": gloves}

    def _observe_tactile(self, side: str, frame, info) -> None:
        raw = frame.counts.reshape(80).tolist()
        with self._preview_lock:
            baseline = self._baselines.get(side)
            if frame._stream_clean:
                values, mode = raw, "clean"
            elif baseline is not None:
                residual = [max(0, value - zero) for value, zero in zip(raw, baseline)]
                values = [value if value >= info.stream_thr else 0 for value in residual]
                mode = "derived_clean"
            else:
                values, mode = raw, "raw_uncalibrated"
            self._live_tactile[side] = {
                "side": side, "serial": info.serial, "fingers": list(info.channels),
                "shape": [5, 4, 4], "values": values, "display_mode": mode,
                "peak": max(values), "seq": int(frame.seq),
                "host_received_ns": int(frame.host_received_ns),
            }
            self._preview_errors.pop(side, None)

    def _monitor_glove(self, glove, stopped: threading.Event) -> None:
        side = glove.info.side
        try:
            while not stopped.is_set():
                batch = glove.read_batch(timeout=0.05)
                if batch.tactile:
                    self._observe_tactile(side, batch.tactile[-1], glove.info)
        except Exception as exc:
            with self._preview_lock:
                self._preview_errors[side] = f"{type(exc).__name__}: {exc}"

    def _start_monitors(self) -> None:
        if self._monitor_threads or not self.gloves:
            return
        stopped = threading.Event()
        self._monitor_stop = stopped
        self._monitor_threads = [threading.Thread(
            target=self._monitor_glove, args=(glove, stopped),
            name=f"oglo-preview-{glove.info.side}", daemon=True) for glove in self.gloves]
        for thread in self._monitor_threads:
            thread.start()

    def _stop_monitors(self) -> None:
        if self._monitor_stop:
            self._monitor_stop.set()
        for thread in self._monitor_threads:
            thread.join(timeout=1)
            if thread.is_alive():
                raise RuntimeError("glove preview reader did not stop")
        self._monitor_threads = []
        self._monitor_stop = None

    def _read_baseline(self, glove) -> None:
        if not glove.info.zero_valid:
            return
        line = glove.send("GET ZERO", expect="#TZERO ", timeout=4.0)
        recipe = json.loads(line.removeprefix("#TZERO "))
        if recipe.get("valid") is not True or len(recipe.get("baseline", [])) != 80:
            raise RuntimeError(f"{glove.info.side} calibration is invalid")
        with self._preview_lock:
            self._baselines[glove.info.side] = list(recipe["baseline"])

    def connect(self, camera_index: int, left_port: str | None = None,
                right_port: str | None = None, camera_mode: str = "default",
                camera_name: str | None = None) -> dict:
        with self._lock:
            if self.state not in {"disconnected", "ready", "error"}:
                raise ValueError("stop and finish the active episode before reconnecting")
            if camera_mode not in {"default", "ovision_left", "ovision_right",
                                   "ovision_native_left", "ovision_native_right", "realsense"}:
                raise ValueError("invalid camera mode")
            if camera_mode != "default":
                selected = next((choice for choice in _camera_choices()
                                 if choice.get("mode") == camera_mode
                                 and choice.get("name") == camera_name), None)
                if (selected is None and camera_mode == "realsense" and self.camera is not None
                        and getattr(self.camera, "mode", None) == "realsense"
                        and self.camera.name == camera_name):
                    # A streaming RealSense may be hidden from a second device listing
                    # (librealsense's libusb backend claims it); it is the camera in use.
                    selected = {"index": self.camera.index}
                if selected is None:
                    raise ValueError("selected camera is no longer available")
                camera_index = selected["index"]  # Display only; capture uses the name.
            if (left_port is None) != (right_port is None):
                raise ValueError("select both glove ports")
            if left_port is not None:
                available = {candidate.device for candidate in list_candidates()}
                if left_port == right_port or {left_port, right_port} - available:
                    raise ValueError("select two distinct attached OGLO USB ports")
            self._close_devices()
            try:
                if left_port is None:
                    self.gloves = list(oglo.connect_pair())
                else:
                    for side, port in (("left", left_port), ("right", right_port)):
                        try:
                            glove = oglo.connect(port=port)
                        except Exception as exc:
                            raise RuntimeError(f"{side} glove at {port}: {exc}") from exc
                        self.gloves.append(glove)
                        if glove.info.side != side:
                            raise ValueError(f"{port} reports {glove.info.side}, expected {side}")
                    if self.gloves[0].info.serial.casefold() == self.gloves[1].info.serial.casefold():
                        raise ValueError("both gloves report the same logical serial")
                    self.glove_ports = {"left": left_port, "right": right_port}
                if {g.info.side for g in self.gloves} != {"left", "right"}:
                    raise RuntimeError("both left and right OGLO gloves are required")
                for glove in self.gloves:
                    glove.raw()
                    self._read_baseline(glove)
                if camera_mode.startswith("ovision_native_"):
                    from oglo.studio_ovision import NativeOvisionCameraWorker
                    self.camera = NativeOvisionCameraWorker(
                        camera_index, mode=camera_mode, name=camera_name, root=self.root)
                elif camera_mode == "realsense":
                    from oglo.studio_realsense import RealSenseCameraWorker
                    self.camera = RealSenseCameraWorker(
                        camera_index, mode=camera_mode, name=camera_name)
                elif camera_mode == "default":
                    self.camera = self.camera_factory(camera_index)
                else:
                    self.camera = self.camera_factory(camera_index, mode=camera_mode,
                                                      name=camera_name)
                self._start_monitors()
                self.state = "ready" if all(g.info.zero_valid for g in self.gloves) else "needs_calibration"
                self.error = None
                self.last_calibrated_wall_ns = None
                self.last_calibration = None
            except BaseException:
                self._close_devices()
                self.state = "disconnected"
                raise
            return self.status()

    def connect_camera(self, camera: CameraSelection, *, left_port: str | None = None,
                       right_port: str | None = None) -> dict:
        """Connect a discovered camera and the verified left/right glove pair."""
        if not isinstance(camera, CameraSelection):
            raise TypeError("camera must be a CameraSelection")
        return self.connect(camera.index, left_port, right_port, camera.mode, camera.name)

    def _close_devices(self) -> None:
        self._stop_monitors()
        if self.camera:
            self.camera.close()
            self.camera = None
        for glove in self.gloves:
            glove.close()
        self.gloves = []
        self.glove_ports = {}
        self.last_calibration = None
        with self._preview_lock:
            self._live_tactile.clear()
            self._baselines.clear()
            self._preview_errors.clear()

    def close(self) -> None:
        """Finish an active take before releasing its camera and gloves."""
        with self._lock:
            active, state = self.current, self.state
        if active and state == "recording":
            self.stop(source="external")
        if active and state in {"recording", "finalizing"}:
            self.wait(active, timeout=60)
        with self._lock:
            self._close_devices()
            self.state = "disconnected"

    def wait(self, episode_id: str, *, timeout: float = 60.0) -> dict:
        """Wait until a started take has sealed its manifest and inventory."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        _load_episode(self.root, episode_id)
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                pending = self.current == episode_id and self.state in {"recording", "finalizing"}
            if not pending:
                return _load_episode(self.root, episode_id)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"episode {episode_id} did not finish within {timeout} seconds")
            time.sleep(0.05)

    def take(self, task: str, *, seconds: float, profile: str = "source_archive",
             selection: str | None = None) -> dict:
        """Record one timed take and optionally keep or discard it.

        Connect and calibrate first. A failed or incomplete take raises after
        its diagnostic manifest has been saved; it is never auto-kept.
        """
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("seconds must be a positive finite duration")
        if selection not in {None, "kept", "discarded"}:
            raise ValueError("selection must be kept, discarded, or None")
        episode_id = self.start(task, source="external", profile=profile)["current"]
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with self._lock:
                if self.state != "recording" or self.current != episode_id:
                    break
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
        with self._lock:
            still_recording = self.state == "recording" and self.current == episode_id
        if still_recording:
            self.stop(source="external")
        episode = self.wait(episode_id)
        if not episode.get("complete"):
            raise RuntimeError(f"take {episode_id} is incomplete: {episode.get('error')}")
        if selection:
            self.select(episode_id, selection, source="external")
            episode = _load_episode(self.root, episode_id)
        return episode

    def review_video(self, episode_id: str) -> Path:
        """Make a browser-playable H.264 copy outside the immutable source episode."""
        source = _episode_video_path(self.root, episode_id)
        crop = _review_eye_crop(_load_episode(self.root, episode_id))
        target_dir = self.root / "review"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / f"{episode_id}.mp4"
        with self._review_lock:
            if target.is_file() and target.stat().st_size and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
                return target
            executable = _ffmpeg_executable()
            if not executable:
                raise RuntimeError("video review needs FFmpeg; reinstall OGLO Studio extras")
            temporary = target_dir / f".{episode_id}.{uuid4().hex}.mp4"
            try:
                command = [
                    executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source), "-an",
                ]
                if crop:
                    command += ["-vf", f"crop={crop[0]}:{crop[1]}:{crop[2]}:0"]
                command += [
                    "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "25", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(temporary),
                ]
                result = subprocess.run(command, capture_output=True, text=True,
                                        timeout=180, check=False)
                if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
                    detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "FFmpeg failed"
                    raise RuntimeError(f"could not prepare review video: {detail}")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def review_poster(self, episode_id: str) -> Path:
        """Show the first saved frame while browser video metadata loads."""
        source = _episode_video_path(self.root, episode_id)
        crop = _review_eye_crop(_load_episode(self.root, episode_id))
        target_dir = self.root / "review"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / f"{episode_id}.jpg"
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target
        import cv2

        capture = cv2.VideoCapture(str(source))
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok:
            raise RuntimeError("could not read a frame for video review")
        if crop:
            frame = frame[:, crop[2]:crop[2] + crop[0]]
        encoded, jpeg = cv2.imencode(".jpg", frame)
        if not encoded:
            raise RuntimeError("could not encode the review poster")
        temporary = target_dir / f".{episode_id}.{uuid4().hex}.jpg"
        try:
            temporary.write_bytes(jpeg.tobytes())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def calibrate(self, threshold: int | None = None) -> dict:
        if threshold is not None and (type(threshold) is not int or not 0 <= threshold <= 500):
            raise ValueError("calibration threshold must be 0..500 ADC counts")
        with self._lock:
            if self.state not in {"ready", "needs_calibration"} or not self.gloves:
                raise ValueError("connect gloves and finish the current episode first")
            self.state = "calibrating"
            self.last_calibration = None
            self._stop_monitors()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                sweeps = [pool.submit(glove.zero, sweep=5) for glove in self.gloves]
                recipes = [sweep.result() for sweep in sweeps]
            for glove in self.gloves:
                if threshold is not None:
                    glove.clean(threshold=threshold)
                glove.raw()
                self._read_baseline(glove)
            with self._lock:
                self.last_calibrated_wall_ns = time.time_ns()
                self.last_calibration = {
                    "completed_wall_ns": self.last_calibrated_wall_ns,
                    "gloves": {glove.info.side: {
                        "serial": glove.info.serial,
                        "fingers": list(glove.info.channels),
                        "noise": recipe["noise"],
                        "threshold": glove.info.stream_thr,
                    } for glove, recipe in zip(self.gloves, recipes)},
                }
                self.state = "ready"
                self.error = None
            return self.status()
        except BaseException as exc:
            with self._lock:
                self.state = "needs_calibration"
                self.error = str(exc)
            raise
        finally:
            with self._lock:
                self._start_monitors()

    def start(self, task: str, source: str = "onscreen", profile: str = "source_archive",
              mapping: dict | None = None, input_event: dict | None = None) -> dict:
        with self._lock:
            if self.state != "ready" or not self.camera or {g.info.side for g in self.gloves} != {"left", "right"}:
                raise ValueError("connect and calibrate both gloves and the camera first")
            if self.camera.error:
                raise ValueError(self.camera.error)
            live = self.live()
            if not live["camera"] or not live["camera"]["ready"] or live["camera"]["age_ms"] > 1000:
                raise ValueError("camera live stream is not healthy")
            if live["camera"].get("dark"):
                raise ValueError("camera image is too dark; check the lens, lighting, and exposure")
            if live["errors"] or any(side not in live["gloves"] or live["gloves"][side]["age_ms"] > 1000
                                     for side in ("left", "right")):
                raise ValueError("both glove live streams must be healthy")
            if not task.strip():
                raise ValueError("enter a task description")
            if profile not in {"source_archive", "annotation_handoff", "og_center_postprocessing"}:
                raise ValueError("unknown delivery profile")
            if profile == "annotation_handoff" and not self.camera.native_device_timestamps:
                raise ValueError("annotation handoff needs a camera with native frame timestamps")
            if profile == "og_center_postprocessing" and not getattr(self.camera, "postprocessing_capable", False):
                raise ValueError("OG Center post processing needs native OVISION stereo, IMU, and calibration")
            if mapping is not None and (
                type(mapping) is not dict or
                any(key not in {"toggle", "confirm", "discard"} or
                    type(value) is not str or len(value) > 32
                    for key, value in mapping.items())
            ):
                raise ValueError("invalid button mapping")
            if not all(g.info.zero_valid for g in self.gloves):
                raise ValueError("every glove needs valid calibration")
            self._stop_monitors()
            try:
                for glove in self.gloves:
                    glove.stop()
                    if glove.info.stream_clean:
                        glove.raw()
                    if glove.info.stream_clean:
                        raise RuntimeError("could not verify RAW glove mode")
            except BaseException:
                self._start_monitors()
                raise
            episode_id = f"ep_{uuid4().hex[:16]}"
            folder = self.root / episode_id
            try:
                folder.mkdir()
                (folder / "gloves").mkdir()
            except BaseException:
                self._start_monitors()
                raise
            stop = threading.Event()
            started_wall_ns = time.time_ns()
            started_host_ns = time.monotonic_ns()
            manifest = {
                "schema": "oglo-studio-source.v1", "id": episode_id,
                "sdk_version": oglo.__version__, "capture_profile": profile,
                "trigger_mapping": mapping or {},
                "task_description": task.strip(), "complete": False,
                "selection": "unreviewed", "delivery_validation": {"source_archive": "pending",
                    "annotation_handoff": "pending" if self.camera.native_device_timestamps
                    else "blocked: camera has no native device timestamp",
                    "og_center_postprocessing": "pending" if getattr(self.camera, "postprocessing_capable", False)
                    else "blocked: camera lacks native OVISION stereo, IMU, and calibration"},
                "alignment_validated": False, "stop_reason": "recording", "error": None,
                "started_wall_time_ns": started_wall_ns,
                "started_host_monotonic_ns": started_host_ns,
                "host_clock": "time.monotonic_ns", "same_host": True,
                "actions": [{"action": "start", "source": source, "input_event": input_event,
                             "host_monotonic_ns": started_host_ns,
                             "wall_time_ns": started_wall_ns}],
                "camera": {}, "gloves": [{"side": g.info.side, "serial": g.info.serial,
                                            "firmware": g.info.fw_rev,
                                            "device_config": g.info.raw,
                                            "last_calibrated_wall_ns": self.last_calibrated_wall_ns,
                                            "episode": None} for g in self.gloves],
            }
            try:
                _atomic_json(folder / "manifest.json", manifest)
                self.camera.begin(folder / "camera", stop)
            except BaseException:
                self._start_monitors()
                raise
            self.current, self.stop_event, self.state, self.error = episode_id, stop, "recording", None
            self._active_manifest = manifest
            try:
                threading.Thread(target=self._finish_episode, args=(folder, manifest, stop),
                                 name=f"oglo-{episode_id}", daemon=True).start()
            except BaseException:
                stop.set()
                self.state = "error"
                self._start_monitors()
                raise
            return self.status()

    def stop(self, source: str = "onscreen", input_event: dict | None = None) -> dict:
        with self._lock:
            if self.state != "recording" or self.stop_event is None:
                raise ValueError("no recording is active")
            folder = self.root / self.current
            manifest = self._active_manifest
            manifest["actions"].append({"action": "stop", "source": source,
                                        "input_event": input_event,
                                        "host_monotonic_ns": time.monotonic_ns(),
                                        "wall_time_ns": time.time_ns()})
            try:
                _atomic_json(folder / "manifest.json", manifest)
            finally:
                self.stop_event.set()
                self.state = "finalizing"
            return self.status()

    def _finish_episode(self, folder: Path, manifest: dict, stop: threading.Event) -> None:
        failure = None
        try:
            with ThreadPoolExecutor(max_workers=len(self.gloves)) as pool:
                futures = [pool.submit(oglo.record, folder / "gloves" / g.info.side,
                                       glove=g, stop_event=stop,
                                       on_tactile=lambda frame, side=g.info.side, info=g.info:
                                       self._observe_tactile(side, frame, info)) for g in self.gloves]
                while not stop.wait(0.05):
                    if any(f.done() for f in futures):
                        stop.set()
                for entry, future in zip(manifest["gloves"], futures):
                    try:
                        entry["episode"] = future.result().relative_to(folder).as_posix()
                    except BaseException as exc:
                        partial = getattr(exc, "partial_episode", None)
                        if partial:
                            entry["episode"] = Path(partial).relative_to(folder).as_posix()
                        failure = failure or f"{type(exc).__name__}: {exc}"
            manifest["camera"] = self.camera.finish() if self.camera else {}
            if failure:
                raise RuntimeError(failure)
            self._validate(folder, manifest)
            manifest["complete"] = True
            manifest["stop_reason"] = "user_stop"
            manifest["delivery_validation"]["source_archive"] = "ready"
            if self.camera and self.camera.native_device_timestamps:
                manifest["delivery_validation"]["annotation_handoff"] = "ready"
            if (self.camera and getattr(self.camera, "postprocessing_capable", False) and
                    manifest["camera"].get("kind") == "ovision_native_stereo"):
                manifest["delivery_validation"]["og_center_postprocessing"] = "ready"
        except BaseException as exc:
            manifest["error"] = f"{type(exc).__name__}: {exc}"
            manifest["stop_reason"] = "error"
            manifest["delivery_validation"]["source_archive"] = "failed"
        finally:
            stop.set()
            manifest["ended_wall_time_ns"] = time.time_ns()
            manifest["ended_host_monotonic_ns"] = time.monotonic_ns()
            try:
                inventory = _inventory(folder)
                _atomic_json(folder / "inventory.json", {"schema": "oglo-studio-inventory.v1",
                                                          "files": inventory})
            except BaseException as exc:
                manifest["complete"] = False
                manifest["error"] = f"could not seal file inventory: {exc}"
                manifest["delivery_validation"]["source_archive"] = "failed"
            try:
                _atomic_json(folder / "manifest.json", manifest)
            except OSError as exc:
                manifest["complete"] = False
                manifest["error"] = f"could not publish manifest: {exc}"
            with self._lock:
                self.state = "review" if manifest["complete"] else "error"
                self.error = manifest["error"]
                self.current = None
                self.stop_event = None
                self._active_manifest = None
                self._start_monitors()

    def _validate(self, folder: Path, manifest: dict) -> None:
        import cv2

        if {entry["side"] for entry in manifest["gloves"]} != {"left", "right"}:
            raise RuntimeError("capture requires both left and right gloves")
        camera = manifest["camera"]
        if (manifest["capture_profile"] == "og_center_postprocessing" and
                camera.get("kind") != "ovision_native_stereo"):
            raise RuntimeError("OG Center post processing needs a native OVISION stereo recording")
        video = folder / camera["video"]
        timestamps = folder / camera["timestamps"]
        decoder = cv2.VideoCapture(str(video))
        if not decoder.isOpened():
            raise RuntimeError("saved camera video cannot be decoded")
        try:
            frames = 0
            while True:
                decoded, frame = decoder.read()
                if not decoded:
                    break
                if frames == 0 and camera.get("kind") == "ovision_native_stereo" and frame.shape[:2] != (1080, 3840):
                    raise RuntimeError("native OVISION video did not decode as 3840×1080 stereo")
                frames += 1
        finally:
            decoder.release()
        first = last = None
        max_camera_gap = 0
        rows = 0
        with timestamps.open(encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row["frame_index"] != rows or type(row["host_received_ns"]) is not int:
                    raise RuntimeError("camera timestamps have a missing or invalid frame")
                stamp = row["host_received_ns"]
                if last is not None and stamp < last:
                    raise RuntimeError("camera host timestamps went backwards")
                if last is not None:
                    max_camera_gap = max(max_camera_gap, stamp - last)
                if (camera.get("kind") in {"ovision_native_stereo", "realsense"} or
                    manifest["capture_profile"] in {"annotation_handoff", "og_center_postprocessing"}) and (
                    type(row.get("device_timestamp")) is not int or
                    not row.get("device_timestamp_unit") or
                    not row.get("device_clock_domain") or
                    not row.get("device_timestamp_meaning")
                ):
                    raise RuntimeError("camera frame lacks native device timing")
                first = stamp if first is None else first
                last = stamp
                rows += 1
        if frames < 2 or frames != rows or rows != camera["frames_submitted"]:
            raise RuntimeError("camera video and timing rows do not match")
        camera["frames_decoded"] = frames
        camera["observed_fps"] = round((frames - 1) * 1_000_000_000 / (last - first), 2) if last > first else 0.0
        if camera.get("kind", "").startswith("ovision") and camera["observed_fps"] > 60:
            raise RuntimeError("OVISION eye frames exceeded 60 FPS; capture timing is invalid")
        if camera.get("kind") == "ovision_native_stereo":
            self._validate_native_ovision(folder, camera, frames)
        elif camera.get("kind") == "realsense":
            self._validate_realsense(folder, camera)
        starts, ends = [first], [last]
        stream_metrics = {"camera": {"frames": frames, "max_gap_ns": max_camera_gap}}
        for entry in manifest["gloves"]:
            if not entry["episode"]:
                raise RuntimeError(f"missing {entry['side']} glove recording")
            episode = oglo.replay(folder / entry["episode"])
            if not episode.meta["complete"] or episode.meta["stop_reason"] != "cancelled":
                raise RuntimeError(f"{entry['side']} glove recording did not stop cleanly")
            if episode.meta["stream_clean"]:
                raise RuntimeError(f"{entry['side']} glove RAW data was not preserved")
            entry["summary"] = episode.summary()
            entry["calibration"] = f"{entry['episode']}/{episode.meta['calibration']}"
            stream_metrics[entry["side"]] = {}
            for stream in ("tactile", "imu", "mag"):
                values = episode.arrays(stream)["host_received_ns"]
                if len(values):
                    starts.append(int(values[0]))
                    ends.append(int(values[-1]))
                    max_gap = max((int(b) - int(a) for a, b in zip(values[:-1], values[1:])), default=0)
                    stream_metrics[entry["side"]][stream] = {"samples": len(values),
                                                                "max_gap_ns": max_gap}
        for side, streams in stream_metrics.items():
            for name, metrics in (streams.items() if side != "camera" else [("frames", streams)]):
                if metrics["max_gap_ns"] > 500_000_000:
                    raise RuntimeError(f"{side} {name} has a gap over 0.5 seconds")
        overlap = [max(starts), min(ends)]
        if overlap[1] - overlap[0] < 200_000_000:
            raise RuntimeError("camera and glove share under 0.2 seconds of captured time")
        if max(starts) - min(starts) > 1_000_000_000 or max(ends) - min(ends) > 1_000_000_000:
            raise RuntimeError("camera and glove capture boundaries differ by over one second")
        coverage = (overlap[1] - overlap[0]) / (max(ends) - min(starts))
        if coverage < 0.9:
            raise RuntimeError("camera and glove share under 90% of captured time")
        manifest["overlap_host_received_ns"] = overlap
        manifest["validation"] = {"schema": "oglo-studio-validation.v1", "profile": manifest["capture_profile"],
                                  "coverage_fraction": round(coverage, 5), "streams": stream_metrics}

    @staticmethod
    def _validate_realsense(folder: Path, camera: dict) -> None:
        def named_file(key: str) -> Path:
            relative = camera.get(key)
            if type(relative) is not str:
                raise RuntimeError(f"RealSense metadata is missing its {key} file")
            path = folder / relative
            if not path.is_file():
                raise RuntimeError(f"RealSense {key} file is missing: {relative}")
            return path

        for key in ("accel", "gyro"):
            count = 0
            with named_file(key).open(encoding="utf-8") as source:
                for line in source:
                    row = json.loads(line)
                    if type(row.get("device_timestamp_us")) is not int:
                        raise RuntimeError(f"RealSense {key} sample lacks an integer device_timestamp_us")
                    count += 1
            if count < 2:
                raise RuntimeError(f"RealSense {key} file has fewer than two samples")
        calibration = json.loads(named_file("calibration").read_text(encoding="utf-8"))
        if not calibration.get("device", {}).get("serial"):
            raise RuntimeError("RealSense calibration is missing device.serial")

    @staticmethod
    def _validate_native_ovision(folder: Path, camera: dict, frames: int) -> None:
        if (camera.get("width"), camera.get("height")) != (3840, 1080):
            raise RuntimeError("native OVISION video must preserve the 3840×1080 stereo frame")
        root = folder / "camera"
        required = ("cam_ego.mp4", "cam_ego.stereo.jsonl", "cam_ego.imu.jsonl",
                    "cam_ego.accel.jsonl", "cam_ego.gyro.jsonl", "cam_ego.mag.jsonl",
                    "cam_ego.calibration.json", "cam_ego.calibration.yaml",
                    "cam_ego.calibration.bin", "sync_point.json")
        for name in required:
            path = root / name
            if not path.is_file() or (name != "cam_ego.mag.jsonl" and path.stat().st_size == 0):
                raise RuntimeError(f"native OVISION source is missing {name}")
        calibration = json.loads((root / "cam_ego.calibration.json").read_text(encoding="utf-8"))
        stereo = calibration.get("stereo", {})
        streams = calibration.get("streams", {})
        def matrix(value, size):
            return (isinstance(value, list) and len(value) == size and
                    all(isinstance(row, list) and len(row) == size and
                        all(type(number) in (int, float) and math.isfinite(number)
                            for number in row) for row in value))

        right_from_left = stereo.get("T_right_left")
        baseline_valid = (matrix(right_from_left, 4) and
                          sum(right_from_left[row][3] ** 2 for row in range(3)) > 0)
        def eye_geometry(eye):
            spec = streams.get(eye, {})
            intrinsics = spec.get("intrinsics")
            distortion = spec.get("distortion_coeffs")
            return (spec.get("resolution") == [1920, 1080] and
                    matrix(intrinsics, 3) and intrinsics[0][0] > 0 and intrinsics[1][1] > 0 and
                    isinstance(distortion, list) and 4 <= len(distortion) <= 8 and
                    all(type(value) in (int, float) and math.isfinite(value) for value in distortion) and
                    matrix(spec.get("T_cam_imu"), 4))

        if (calibration.get("schema") != "syncfield.ovision_calibration.v1" or
            stereo.get("layout") != "side_by_side" or
            stereo.get("packed_resolution") != [3840, 1080] or
            stereo.get("eye_order") != ["left", "right"] or
            stereo.get("synchronization") != "internal_fsync" or
            not baseline_valid or
            not all(eye_geometry(eye) for eye in ("left", "right"))):
            raise RuntimeError("native OVISION calibration lacks stereo/IMU geometry")
        with (root / "cam_ego.stereo.jsonl").open(encoding="utf-8") as source:
            if sum(1 for _ in source) != frames:
                raise RuntimeError("native OVISION stereo metadata does not match video frames")
        channels_by_file = {
            "cam_ego.imu.jsonl": {"accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z"},
            "cam_ego.accel.jsonl": {"accel_x", "accel_y", "accel_z"},
            "cam_ego.gyro.jsonl": {"gyro_x", "gyro_y", "gyro_z"},
        }
        for name, channels in channels_by_file.items():
            count = 0
            with (root / name).open(encoding="utf-8") as source:
                for line in source:
                    row = json.loads(line)
                    capture_ns = row.get("capture_ns")
                    if (row.get("frame_number") != count or
                        type(capture_ns) is not int or
                        type(row.get("device_timestamp_ns")) is not int or
                        not channels <= set(row.get("channels") or {})):
                        raise RuntimeError(f"native OVISION motion stream is invalid: {name}")
                    count += 1
            if not count:
                raise RuntimeError(f"native OVISION motion stream is empty: {name}")

    def select(self, episode_id: str, selection: str, source: str = "onscreen",
               input_event: dict | None = None) -> dict:
        if selection not in {"kept", "discarded"}:
            raise ValueError("selection must be kept or discarded")
        with self._lock:
            if self.state in {"recording", "finalizing", "calibrating"}:
                raise ValueError("wait for the active operation to finish")
            manifest = _load_episode(self.root, episode_id)
            if not manifest["complete"] and selection == "kept":
                raise ValueError("an incomplete episode cannot be kept")
            manifest["selection"] = selection
            manifest.setdefault("actions", []).append({
                "action": selection, "source": source, "input_event": input_event,
                "host_monotonic_ns": time.monotonic_ns(), "wall_time_ns": time.time_ns()})
            _atomic_json(self.root / episode_id / "manifest.json", manifest)
            if self.state == "review":
                self.state = "ready"
            return self.status()

    def export(self, profile: str = "source_archive") -> Path:
        with self._lock:
            if self.state in {"recording", "finalizing", "calibrating"}:
                raise ValueError("finish the active operation before exporting")
            if profile not in {"source_archive", "annotation_handoff", "og_center_postprocessing"}:
                raise ValueError("unknown delivery profile")
            chosen = [item for item in self.status()["episodes"]
                      if item.get("complete") and item.get("selection") == "kept"
                      and {entry.get("side") for entry in item.get("gloves", [])} == {"left", "right"}
                      and item.get("delivery_validation", {}).get(profile) == "ready"]
            if not chosen:
                raise ValueError("there are no kept, validated episodes")
            export_dir = self.root / "exports"
            export_dir.mkdir(exist_ok=True)
            destination = export_dir / f"oglo-dataset-{uuid4().hex}.zip"
            temporary = destination.with_suffix(".tmp")
            checksums = {}
            try:
                for item in chosen:
                    folder = self.root / item["id"]
                    inventory = json.loads((folder / "inventory.json").read_text(encoding="utf-8"))
                    expected = {entry["path"] for entry in inventory["files"]}
                    actual = {file.relative_to(folder).as_posix() for file in folder.rglob("*") if file.is_file()}
                    if actual != expected | {"manifest.json", "inventory.json"}:
                        raise RuntimeError(f"source file set changed after validation: {item['id']}")
                    checksums[item["id"]] = {}
                    for entry in inventory["files"]:
                        file = folder / entry["path"]
                        if file.is_symlink() or not file.is_file() or file.stat().st_size != entry["size"] or _sha256(file) != entry["sha256"]:
                            raise RuntimeError(f"source file changed after validation: {file}")
                    for relative in sorted(actual):
                        file = folder / relative
                        if file.is_symlink():
                            raise RuntimeError(f"source file became a symlink: {file}")
                        checksums[item["id"]][relative] = _sha256(file)
                index = {"schema": "oglo-studio-dataset.v1", "profile": profile,
                         "checksum_algorithm": "sha256", "episodes": [item["id"] for item in chosen],
                         "checksums": checksums}
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                                     allowZip64=True) as archive:
                    archive.writestr("dataset.json", json.dumps(index, indent=2) + "\n")
                    archive.writestr("README.txt", "OGLO Studio source archive v1\n"
                                     "Each episode contains video, timing, SDK glove data, calibration, "
                                     "manifest, and inventory. Verify SHA-256 files against dataset.json.\n"
                                     "Native Linux OVISION episodes retain both eyes, exposure metadata, "
                                     "camera IMU, and calibration. Selected-eye previews do not alter source video.\n"
                                     "Mac OVISION and ordinary webcam episodes lack native camera metadata; "
                                     "alignment_validated is false. OG Center ingestion is a separate step.\n")
                    for episode_id, files in checksums.items():
                        for relative in files:
                            archive.write(self.root / episode_id / relative,
                                          f"{episode_id}/{relative}")
                with zipfile.ZipFile(temporary) as archive:
                    if archive.testzip() is not None:
                        raise RuntimeError("export archive failed its CRC check")
                    for episode_id, files in checksums.items():
                        for relative, expected_hash in files.items():
                            digest = hashlib.sha256()
                            with archive.open(f"{episode_id}/{relative}") as source:
                                for block in iter(lambda: source.read(1024 * 1024), b""):
                                    digest.update(block)
                            if digest.hexdigest() != expected_hash:
                                raise RuntimeError("export archive did not preserve source bytes")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination



# Existing callers of oglo.studio.Studio continue to use the SDK controller.
Studio = Collection


def create_app(root: Path | str, studio: Collection | None = None):
    """Load the optional web adapter only when the localhost UI is requested."""
    from .studio_server import create_app as create_web_app

    return create_web_app(root, studio=studio)
