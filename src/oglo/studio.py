"""Single-operator localhost collection service.

The camera worker is the only camera reader. Each glove is owned by one SDK
recorder while an episode is active; the UI never drains sensor samples.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import oglo
from oglo._usb import list_candidates
from fastapi import Request


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


def _camera_choices() -> list[dict]:
    """List OpenCV camera indices, with OS names when their index mapping is known."""
    cameras = []
    if sys.platform.startswith("linux"):
        for node in sorted(Path("/sys/class/video4linux").glob("video*"),
                           key=lambda path: int(path.name[5:]) if path.name[5:].isdigit() else 99):
            if node.name[5:].isdigit():
                index = int(node.name[5:])
                if index <= 32 and Path(f"/dev/{node.name}").exists():
                    try:
                        name = (node / "name").read_text(encoding="utf-8").strip()
                    except OSError:
                        name = "USB camera"
                    cameras.append({"index": index, "label": f"{name} · /dev/{node.name}"})
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=5, check=False)
            section = result.stderr.split("AVFoundation video devices:", 1)[1].split(
                "AVFoundation audio devices:", 1)[0]
            for index, name in re.findall(r"\[(\d+)\] ([^\n]+)", section):
                if int(index) <= 32:
                    cameras.append({"index": int(index), "label": name.strip()})
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
    return cameras or [{"index": index, "label": f"Camera {index} · verify live view"}
                       for index in range(5)]


class CameraWorker:
    """Continuously reads one webcam for preview and bounded episode capture."""

    native_device_timestamps = False

    def __init__(self, index: int, fps: float = 30.0) -> None:
        import cv2

        self.cv2 = cv2
        self.index = index
        self.fps = fps
        self.device = cv2.VideoCapture(index)
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
        self._thread.join(timeout=2)
        # Avoid releasing a device while a blocked driver read still owns it.
        if not self._thread.is_alive():
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
                "kind": "usb_webcam", "index": self.index,
                "video": "camera/video.mp4", "timestamps": "camera/timestamps.jsonl",
                "codec": "mp4v", "playback_fps": self.fps,
                "requested_fps": self.fps,
                "fps_request_accepted": self.fps_request_accepted,
                "backend": self.backend,
                "host_timestamp_meaning": "OpenCV read-return time, not exposure time",
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
                with self._lock:
                    self._last_frame_ns = received
                now = time.monotonic()
                if now - last_preview >= 0.2:
                    encoded, jpeg = self.cv2.imencode(".jpg", frame)
                    if encoded:
                        with self._lock:
                            self._jpeg = jpeg.tobytes()
                    last_preview = now
                if capture is None:
                    continue
                height, width = frame.shape[:2]
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


class Studio:
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
        self._live_tactile: dict = {}
        self._baselines: dict = {}
        self._preview_errors: dict = {}
        self._monitor_stop: threading.Event | None = None
        self._monitor_threads: list[threading.Thread] = []

    def status(self) -> dict:
        with self._lock:
            episodes = []
            for path in sorted(self.root.glob("ep_*/manifest.json")):
                try:
                    episodes.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            return {"state": self.state, "current": self.current, "error": self.error,
                    "gloves": [{"side": g.info.side, "serial": g.info.serial,
                                "zero_valid": g.info.zero_valid,
                                "threshold": g.info.stream_thr,
                                "stream_clean": g.info.stream_clean} for g in self.gloves],
                    "camera": {"index": self.camera.index, "error": self.camera.error,
                               "native_device_timestamps": self.camera.native_device_timestamps} if self.camera else None,
                    "calibration": self.last_calibration,
                    "episodes": episodes,
                    "delivery_profiles": {
                        "source_archive": "available with a complete validated episode",
                        "annotation_handoff": "available with native camera timing" if
                        self.camera and self.camera.native_device_timestamps else
                        "requires a camera with native frame timestamps",
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
                right_port: str | None = None) -> dict:
        with self._lock:
            if self.state not in {"disconnected", "ready", "error"}:
                raise ValueError("stop and finish the active episode before reconnecting")
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
                        glove = oglo.connect(port=port)
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
                self.camera = self.camera_factory(camera_index)
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
        """Stop active device streams before the local server exits."""
        with self._lock:
            self._close_devices()
            self.state = "disconnected"

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
            if live["errors"] or any(side not in live["gloves"] or live["gloves"][side]["age_ms"] > 1000
                                     for side in ("left", "right")):
                raise ValueError("both glove live streams must be healthy")
            if not task.strip():
                raise ValueError("enter a task description")
            if profile not in {"source_archive", "annotation_handoff"}:
                raise ValueError("unknown delivery profile")
            if profile == "annotation_handoff" and not self.camera.native_device_timestamps:
                raise ValueError("annotation handoff needs a camera with native frame timestamps")
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
                    "annotation_handoff": "pending" if profile == "annotation_handoff"
                    else "blocked: camera has no native device timestamp"},
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
            if manifest["capture_profile"] == "annotation_handoff":
                manifest["delivery_validation"]["annotation_handoff"] = "ready"
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
        video = folder / camera["video"]
        timestamps = folder / camera["timestamps"]
        decoder = cv2.VideoCapture(str(video))
        if not decoder.isOpened():
            raise RuntimeError("saved camera video cannot be decoded")
        try:
            frames = 0
            while decoder.read()[0]:
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
                if manifest["capture_profile"] == "annotation_handoff" and (
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
            if profile not in {"source_archive", "annotation_handoff"}:
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
                                     "Webcam frames have no native device timestamp; alignment_validated is false.\n")
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


def create_app(root: Path | str, studio: Studio | None = None):
    """Create the optional FastAPI app without making web dependencies mandatory."""
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, Response
    from starlette.concurrency import run_in_threadpool
    from urllib.parse import urlparse

    service = studio or Studio(Path(root))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await run_in_threadpool(service.close)

    app = FastAPI(title="OGLO Studio", lifespan=lifespan)
    static = Path(__file__).with_name("studio_web")

    @app.middleware("http")
    async def same_origin(request: Request, next_handler):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return Response(status_code=403, content="cross-origin control is disabled")
        return await next_handler(request)

    def call(action, *args):
        try:
            return action(*args)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def source_of(body: dict) -> str:
        source = body.get("source", "onscreen")
        if type(source) is not str or source not in {"onscreen", "keyboard", "focus_loss", "external"}:
            raise HTTPException(status_code=422, detail="invalid input source")
        return source

    def input_event_of(body: dict) -> dict | None:
        event = body.get("input_event")
        if event is None:
            return None
        if (type(event) is not dict or set(event) - {"control_id", "host_received_ns"}
            or type(event.get("control_id")) is not str
            or not 1 <= len(event["control_id"]) <= 64
            or (event.get("host_received_ns") is not None and
                (type(event["host_received_ns"]) is not int or event["host_received_ns"] < 0))):
            raise HTTPException(status_code=422, detail="invalid button input event")
        return event

    async def json_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail="expected a JSON object") from exc
        if type(body) is not dict:
            raise HTTPException(status_code=422, detail="expected a JSON object")
        return body

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    @app.get("/studio.js")
    def script():
        return FileResponse(static / "studio.js", media_type="text/javascript")

    @app.get("/studio.css")
    def style():
        return FileResponse(static / "studio.css", media_type="text/css")

    @app.get("/guide")
    def guide():
        return FileResponse(static / "guide.html", media_type="text/html")

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/live")
    def live():
        return service.live()

    @app.get("/api/devices")
    async def devices():
        return await run_in_threadpool(call, service.devices)

    @app.post("/api/connect")
    async def connect(request: Request):
        body = await json_object(request)
        index = body.get("camera_index", 0)
        if type(index) is not int or not 0 <= index <= 32:
            raise HTTPException(status_code=422, detail="invalid camera selection")
        left_port = body.get("left_port")
        right_port = body.get("right_port")
        if any(port is not None and (type(port) is not str or not port or len(port) > 256)
               for port in (left_port, right_port)):
            raise HTTPException(status_code=422, detail="invalid glove selection")
        if body.get("pair", True) is not True or body.get("serial") is not None:
            raise HTTPException(status_code=422, detail="Studio requires both left and right OGLO gloves")
        return await run_in_threadpool(call, service.connect, index, left_port, right_port)

    @app.post("/api/calibrate")
    async def calibrate(request: Request):
        body = await json_object(request)
        threshold = body.get("threshold")
        if threshold is not None and (type(threshold) is not int or not 0 <= threshold <= 500):
            raise HTTPException(status_code=422, detail="calibration threshold must be 0..500")
        return await run_in_threadpool(call, service.calibrate, threshold)

    @app.post("/api/start")
    async def start(request: Request):
        body = await json_object(request)
        task = body.get("task")
        if type(task) is not str or not 1 <= len(task) <= 500:
            raise HTTPException(status_code=422, detail="enter a task description")
        return await run_in_threadpool(call, service.start, task, source_of(body),
                                       body.get("profile", "source_archive"), body.get("mapping"),
                                       input_event_of(body))

    @app.post("/api/stop")
    async def stop(request: Request):
        body = await json_object(request)
        return await run_in_threadpool(call, service.stop, source_of(body), input_event_of(body))

    @app.post("/api/episodes/{episode_id}/selection")
    async def select(episode_id: str, request: Request):
        body = await json_object(request)
        return await run_in_threadpool(call, service.select, episode_id,
                                       body.get("selection"), source_of(body), input_event_of(body))

    @app.get("/api/preview")
    def preview():
        image = service.camera.preview() if service.camera else None
        if image is None:
            return Response(status_code=204)
        return Response(content=image, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/episodes/{episode_id}/video")
    def video(episode_id: str):
        manifest = call(_load_episode, service.root, episode_id)
        if not manifest.get("complete"):
            raise HTTPException(status_code=409, detail="episode is incomplete")
        camera = manifest.get("camera", {})
        relative = camera.get("video")
        if type(relative) is not str or not relative.startswith("camera/") or not relative.endswith(".mp4"):
            raise HTTPException(status_code=409, detail="episode video is unavailable")
        folder = (service.root / episode_id).resolve()
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise HTTPException(status_code=409, detail="episode video is unavailable")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/export")
    async def export(request: Request):
        body = await json_object(request)
        path = await run_in_threadpool(call, service.export, body.get("profile", "source_archive"))
        return {"download": f"/api/download/{path.name}", "filename": path.name}

    @app.get("/api/download/{name}")
    def download(name: str):
        if not re.fullmatch(r"oglo-dataset-[0-9a-f]{32}\.zip", name):
            raise HTTPException(status_code=404, detail="dataset not found")
        path = service.root / "exports" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="dataset not found")
        return FileResponse(path, media_type="application/zip", filename=name)

    return app
