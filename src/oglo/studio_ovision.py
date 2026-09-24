"""Linux OVISION stereo-inertial camera for Studio's full-fidelity source path.

This module is imported only for the native Linux camera choice. SyncField's
0.8.14 adapter owns capture, passthrough H.264, exposure timing, IMU and flash
calibration; Studio only selects the preview eye and seals its episode metadata.
"""

from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import socket
import sys
import threading
import time


def write_join_timestamps(folder: Path, expected_frames: int) -> tuple[int, int]:
    """Derive Studio's common clock from native rows without changing them."""
    first = last = None
    count = 0
    with (folder / "cam_ego.stereo.jsonl").open(encoding="utf-8") as source:
        with (folder / "timestamps.jsonl").open("x", encoding="utf-8") as target:
            for line in source:
                row = json.loads(line)
                host = row.get("capture_ns")
                device = row.get("device_timestamp_ns")
                if (row.get("frame_number") != count or type(host) is not int or host < 0
                        or (last is not None and host < last)):
                    raise ValueError("OVISION frame numbers or host timestamps are invalid")
                if (type(device) is not int or device < 0 or
                        device != row.get("left_exposure_start_ns")):
                    raise ValueError("OVISION native exposure timestamp is invalid")
                target.write(json.dumps({
                    "frame_index": count, "host_read_started_ns": None,
                    "host_received_ns": host, "device_timestamp": device,
                    "device_timestamp_unit": "ns", "device_clock_domain": "ovision_camera",
                    "device_timestamp_meaning": "left_exposure_start",
                    "native_metadata": "cam_ego.stereo.jsonl", "native_frame_number": count,
                }) + "\n")
                first = host if first is None else first
                last = host
                count += 1
    if count < 2 or count != expected_frames:
        raise ValueError(f"OVISION metadata has {count} frames; expected {expected_frames}")
    return first, last


class NativeOvisionCameraWorker:
    """Keep native capture connected between Studio takes; preview one eye."""

    native_device_timestamps = True
    postprocessing_capable = True

    def __init__(self, index: int, *, mode: str, name: str, root: Path) -> None:
        if sys.platform != "linux":
            raise RuntimeError("native OVISION metadata capture requires Linux V4L2")
        if mode not in {"ovision_native_left", "ovision_native_right"} or not name:
            raise ValueError("select a native OVISION eye and video device")
        try:
            installed = version("syncfield")
        except PackageNotFoundError as exc:
            raise RuntimeError("install native OVISION capture: pip install -e '.[collection,studio-ovision]'") from exc
        if installed != "0.8.14":
            raise RuntimeError(f"native OVISION capture requires SyncField 0.8.14; found {installed}")

        import av
        import cv2
        from syncfield.adapters.ovision_camera import OvisionCameraStream

        eye = mode.removeprefix("ovision_native_")

        class SelectedEyeStream(OvisionCameraStream):
            """The pinned adapter previews left only; select right without touching its writer."""

            def _preview_loop(self):
                while not self._stop_event.is_set():
                    if not self._preview_wake.wait(timeout=0.5):
                        continue
                    self._preview_wake.clear()
                    encoded, self._preview_packet = self._preview_packet, None
                    if encoded is None:
                        continue
                    try:
                        decoder = av.CodecContext.create("h264", "r")
                        frames = decoder.decode(av.Packet(encoded))
                        if frames:
                            packed = frames[-1].to_ndarray(format="bgr24")
                            half = packed.shape[1] // 2
                            with self._frame_lock:
                                self._latest_frame = packed[:, :half] if eye == "left" else packed[:, half:]
                    except Exception:
                        # A skipped preview must never interrupt native recording.
                        continue

        self.cv2 = cv2
        self.index, self.mode, self.name, self.eye = index, mode, name, eye
        self.size = (1920, 1080)
        self.error: str | None = None
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._last_frame = None
        self._last_frame_ns: int | None = None
        self._brightness: float | None = None
        self._dark_samples = 0
        self._folder: Path | None = None
        self._recording = False
        self._recording_started_ns: int | None = None
        self._stop_signal: threading.Event | None = None
        self._monitor: threading.Thread | None = None
        self.stream = SelectedEyeStream("cam_ego", Path(root) / ".ovision-pending",
                                        video_device=name, width=3840, height=1080, fps=30)
        try:
            self.stream.prepare()
            self.stream.connect()
            deadline = time.monotonic() + 10
            while not self.stream.capture_ready():
                if time.monotonic() >= deadline:
                    raise RuntimeError("OVISION did not provide valid stereo/IMU metadata within 10 seconds")
                time.sleep(0.05)
        except BaseException:
            self.stream.disconnect()
            raise

    def _update_preview(self) -> None:
        frame = self.stream.latest_frame
        if frame is None:
            return
        with self._lock:
            if frame is self._last_frame:
                return
            self._last_frame = frame
            self.size = (frame.shape[1], frame.shape[0])
            brightness = float(frame[::16, ::16].mean())
            encoded, jpeg = self.cv2.imencode(".jpg", frame)
            self._brightness = round(brightness, 1)
            self._dark_samples = min(3, self._dark_samples + 1) if brightness < 18 else 0
            if encoded:
                self._jpeg = jpeg.tobytes()
                self._last_frame_ns = time.monotonic_ns()

    def preview(self) -> bytes | None:
        self._update_preview()
        with self._lock:
            return self._jpeg

    def live_status(self) -> dict:
        self._update_preview()
        with self._lock:
            last = self._last_frame_ns
            age = round((time.monotonic_ns() - last) / 1_000_000) if last else None
            error = self.error or getattr(self.stream, "_capture_error", None)
            return {"ready": bool(self._jpeg and self.stream.capture_ready() and age is not None
                                  and age <= 1000 and not error),
                    "age_ms": age, "dark": self._dark_samples >= 3,
                    "brightness": self._brightness, "error": error}

    def begin(self, folder: Path, stop: threading.Event) -> None:
        if self._recording or not self.stream.capture_ready():
            raise RuntimeError("native OVISION camera is not ready to record")
        from syncfield.clock import SessionClock
        from syncfield.types import SyncPoint

        folder.mkdir()
        clock = SessionClock(SyncPoint.create_now(socket.gethostname()),
                             recording_armed_ns=time.monotonic_ns())
        (folder / "sync_point.json").write_text(
            json.dumps(clock.sync_point.to_dict(), indent=2) + "\n", encoding="utf-8")
        # 0.8.14 reads these paths only in start_recording; the connected stream
        # stays on the same camera so preview and capture never contend for USB.
        self.stream._output_dir = folder
        self.stream._file_path = folder / "cam_ego.mp4"
        self.stream.start_recording(clock)
        self._folder = folder
        self._recording = True
        self._recording_started_ns = time.monotonic_ns()
        self._stop_signal = stop
        self._monitor = threading.Thread(target=self._monitor_capture, name="ovision-studio-health",
                                         daemon=True)
        self._monitor.start()

    def _monitor_capture(self) -> None:
        stop = self._stop_signal
        while stop is not None and not stop.wait(0.2) and self._recording:
            if not self.stream.capture_ready():
                self.error = getattr(self.stream, "_capture_error", None) or "OVISION capture stopped"
            else:
                last = getattr(self.stream, "_last_at", None)
                started = self._recording_started_ns
                if started and time.monotonic_ns() - (last or started) > 5_000_000_000:
                    self.error = "OVISION stopped returning frames for five seconds"
            if self.error:
                stop.set()
                return

    def finish(self, timeout: float = 10.0) -> dict:
        if not self._recording or self._folder is None:
            raise RuntimeError("native OVISION capture was not started")
        folder = self._folder
        report = self.stream.stop_recording()
        self._recording = False
        if self._monitor:
            self._monitor.join(timeout=1)
            self._monitor = None
        (folder / "finalization.json").write_text(
            json.dumps(asdict(report), default=str, indent=2) + "\n", encoding="utf-8")
        if report.status != "completed" or report.error or self.error:
            raise RuntimeError(f"OVISION finalization failed: {self.error or report.error or report.status}")
        required = ("mp4", "stereo.jsonl", "imu.jsonl", "accel.jsonl", "gyro.jsonl",
                    "mag.jsonl", "calibration.json", "calibration.yaml", "calibration.bin")
        for suffix in required:
            path = folder / f"cam_ego.{suffix}"
            if not path.is_file() or (suffix != "mag.jsonl" and path.stat().st_size == 0):
                raise RuntimeError(f"OVISION artifact missing or empty: {path.name}")
        first, last = write_join_timestamps(folder, report.frame_count)
        self._folder = None
        return {
            "kind": "ovision_native_stereo", "index": self.index, "mode": self.mode,
            "name": self.name, "eye": self.eye, "source_eye_order": ["left", "right"],
            "video": "camera/cam_ego.mp4", "timestamps": "camera/timestamps.jsonl",
            "native_stereo_metadata": "camera/cam_ego.stereo.jsonl",
            "native_artifacts": [f"camera/cam_ego.{suffix}" for suffix in required],
            "calibration": "camera/cam_ego.calibration.json",
            "sync_point": "camera/sync_point.json", "finalization": "camera/finalization.json",
            "codec": "h264_passthrough", "playback_fps": 30.0, "requested_fps": 30.0,
            "backend": "SyncField OVISION 0.8.14 V4L2", "syncfield_version": "0.8.14",
            "host_timestamp_meaning": "H.264 packet arrival, not exposure time",
            "native_device_timestamps": True,
            "frames_submitted": report.frame_count, "width": 3840, "height": 1080,
            "preview_width": 1920, "preview_height": 1080,
            "first_host_received_ns": first, "last_host_received_ns": last,
        }

    def close(self) -> None:
        try:
            if self._recording:
                if self._stop_signal:
                    self._stop_signal.set()
                self.stream.stop_recording()
                self._recording = False
            if self._monitor:
                self._monitor.join(timeout=1)
                self._monitor = None
        finally:
            self.stream.disconnect()
