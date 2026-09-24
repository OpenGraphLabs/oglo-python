"""A stand-in for SyncField's ``OvisionCameraStream``: same public surface, no camera, no SyncField."""

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np

SIZE = (64, 48)  # written video frames; the preview frame keeps the real left-eye size


@dataclass
class FakeReport:
    """The fields of ``syncfield.types.FinalizationReport`` that ovision.py reads or saves."""

    stream_id: str
    status: str
    frame_count: int
    error: str | None
    file_path: str | None
    first_sample_at_ns: int | None
    last_sample_at_ns: int | None


def fake_clock():
    """What ``ovision.session_clock()`` returns, without SyncField."""
    now = time.monotonic_ns()
    point = SimpleNamespace(to_dict=lambda: {
        "monotonic_ns": now, "wall_clock_ns": time.time_ns(), "host_id": "test",
        "timestamp_ms": time.time_ns() // 1_000_000, "iso_datetime": "2026-01-01T00:00:00+00:00"})
    return SimpleNamespace(sync_point=point, recording_armed_ns=now)


class FakeOvisionStream:
    """Public surface of ``syncfield.adapters.ovision_camera.OvisionCameraStream`` (0.8.14).

    Lives like the adapter: ``connect`` starts a capture thread that keeps a left-eye
    ``latest_frame`` while idle and, between ``start_recording`` and ``stop_recording``,
    writes a real mp4 (capture.py decodes it back) plus native-looking sidecars at
    ``frame_hz`` under ``_output_dir`` / ``_file_path``, the two attributes
    ``ovision.retarget`` moves. ``fail_after`` frames into a recording the thread dies
    like on a lost USB device; ``reconnect`` (disconnect + connect) brings it back.
    """

    def __init__(self, id, output_dir, *, video_device="/dev/video0", usb_serial=None,
                 width=3840, height=1080, fps=30.0, frame_hz=50, fail_after=None):
        self.id = id
        self._output_dir = Path(output_dir)
        self._file_path = self._output_dir / f"{id}.mp4"
        self._recording = False
        self.video_device, self.usb_serial = video_device, usb_serial
        self.frame_hz, self.fail_after = frame_hz, fail_after
        self.connected = False
        self.error = None
        self.recordings = []  # camera folders, in start_recording order
        self.frames = 0       # frames written into the current or last recording
        self.connects = self.disconnects = 0
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread = None
        self._sinks = None
        self._frame = np.full((height, width // 2, 3), 90, dtype=np.uint8)

    @property
    def latest_frame(self):
        return self._frame if self.connected else None

    def capture_ready(self):
        return (self.connected and self._thread is not None and self._thread.is_alive()
                and self.error is None)

    def prepare(self):
        pass

    def connect(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self.connected = True
        self.error = None
        self.connects += 1
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"fake-ovision-{self.id}")
        self._thread.start()

    def start_recording(self, clock):
        assert clock.recording_armed_ns is not None
        self._output_dir.mkdir(parents=True, exist_ok=True)
        for suffix in ("json", "yaml", "bin"):
            (self._output_dir / f"{self.id}.calibration.{suffix}").write_text("fake calibration")
        writer = cv2.VideoWriter(str(self._file_path), cv2.VideoWriter_fourcc(*"mp4v"), 30, SIZE)
        assert writer.isOpened()
        files = {name: (self._output_dir / f"{self.id}.{name}.jsonl").open("w", encoding="utf-8")
                 for name in ("stereo", "imu", "accel", "gyro", "mag")}
        with self._lock:
            self._sinks = (writer, files)
            self.frames = 0
            self.first = self.last = None
            self.recordings.append(self._output_dir)
            self._recording = True

    def stop_recording(self):
        with self._lock:
            self._recording = False
            sinks, self._sinks = self._sinks, None
        if sinks is not None:
            writer, files = sinks
            writer.release()
            for handle in files.values():
                handle.close()
        status = "failed" if self.error or not self.frames else "completed"
        return FakeReport(self.id, status, self.frames, self.error,
                          str(self._file_path) if self.frames else None, self.first, self.last)

    def disconnect(self):
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.connected = False
        self.disconnects += 1

    def reconnect(self):
        self.disconnect()
        self.fail_after = None  # The "device" is back for good.
        self.connect()

    def _loop(self):
        period = 1 / self.frame_hz
        while not self._halt.wait(period):
            with self._lock:
                if not self._recording or self._sinks is None:
                    continue
                if self.fail_after is not None and self.frames >= self.fail_after:
                    self.error = "fake USB device vanished"
                    return
                self._write_frame()

    def _write_frame(self):
        writer, files = self._sinks
        now = time.monotonic_ns()
        n = self.frames
        device = 1_000_000_000 + n * 33_333_000
        writer.write(np.full((SIZE[1], SIZE[0], 3), n % 256, dtype=np.uint8))
        common = {"frame_number": n, "capture_ns": now, "clock_source": "device_monotonic",
                  "device_timestamp_ns": device}
        rows = {
            "stereo": {**common, "left_exposure_start_ns": device, "right_exposure_start_ns": device + 20_000,
                       "stereo_skew_us": 20, "left_exposure_time_us": 10_000, "right_exposure_time_us": 10_000,
                       "user_data_seq": n},
            "imu": {**common, "uncertainty_ns": 500_000, "accel_unit": "m_s2", "gyro_unit": "rad_s",
                    "channels": {"gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0,
                                 "accel_x": 0.0, "accel_y": 0.0, "accel_z": 9.81}},
            "accel": {**common, "unit": "g", "channels": {"accel_x": 0.0, "accel_y": 0.0, "accel_z": 1.0}},
            "gyro": {**common, "unit": "rad_s", "channels": {"gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0}},
        }
        for name, row in rows.items():
            files[name].write(json.dumps(row) + "\n")
        if self.first is None:
            self.first = now
        self.last = now
        self.frames += 1
