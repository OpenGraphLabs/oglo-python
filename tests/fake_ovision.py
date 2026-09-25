"""A stand-in for SyncField's ``OvisionCameraStream``: same public surface and failure modes, no camera."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time

import cv2
import numpy as np

SIZE = (64, 48)  # written video frames; the preview frame keeps the real left-eye size


@dataclass
class FakeReport:
    """The fields of ``syncfield.types.FinalizationReport`` that the SDK worker reads or saves."""

    stream_id: str
    status: str
    frame_count: int
    error: str | None
    file_path: str | None
    first_sample_at_ns: int | None
    last_sample_at_ns: int | None
    health_events: list = field(default_factory=list)
    jitter_p95_ns: int | None = None
    jitter_p99_ns: int | None = None
    recording_anchor: None = None


class FakeOvisionStream:
    """What ``syncfield.adapters.ovision_camera.OvisionCameraStream`` (0.8.14) does, without a device.

    Like the adapter: ``connect`` starts a capture thread and ``capture_ready()`` turns true
    only once that thread has seen a first packet, not at ``connect``; every packet goes
    through ``_queue_preview`` (recording or not), which a ``StereoPreview`` replaces per
    instance; the class's own makes the left-eye ``latest_frame`` a fresh array on every
    keyframe, one keyframe every ``keyframe_every`` ticks (about one per second on the
    real firmware), so it stops changing while a preview holds the hook; a recording
    writes nothing until the first keyframe after ``start_recording``, then a real mp4
    (capture.py decodes it back) plus native-looking sidecars under ``_output_dir`` /
    ``_file_path``, the two attributes the SDK worker retargets; ``_last_at`` and
    ``_capture_error`` are kept because the worker's watchdog reads them.

    Failure modes: ``fail_after`` frames into a recording the thread dies with an error
    (a lost USB device); ``stall_after`` frames it stays alive and error-free but delivers
    nothing more (a wedged camera); while ``unplugged["now"]`` is true, ``prepare`` and
    ``connect`` raise FileNotFoundError like ``av.open`` on a node that is gone.
    """

    def __init__(self, id, output_dir, *, video_device="/dev/video0", usb_serial=None,
                 width=3840, height=1080, fps=30.0, preview_interval_s=0.5,
                 frame_hz=50, keyframe_every=10, fail_after=None, stall_after=None, unplugged=None):
        self.id = id
        self._output_dir = Path(output_dir)
        self._file_path = self._output_dir / f"{id}.mp4"
        self._recording = False
        self._last_at = None
        self._capture_error = None
        self.video_device, self.usb_serial = Path(video_device), usb_serial
        self.frame_hz, self.keyframe_every = frame_hz, keyframe_every
        self.fail_after, self.stall_after = fail_after, stall_after
        self.unplugged = unplugged if unplugged is not None else {"now": False}
        self.connected = False
        self.ready = False
        self.error = None
        self.recordings = []  # camera folders, in start_recording order
        self.frames = 0       # frames written into the current or last recording
        self.keyframes = 0    # preview frames decoded since connect
        self.connects = self.disconnects = 0
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread = None
        self._sinks = None
        self._started_on_keyframe = False
        self._ticks = 0
        self._frame = None
        self._shape = (height, width // 2, 3)

    @property
    def latest_frame(self):
        with self._lock:
            return self._frame

    def capture_ready(self):
        return (self.connected and self.ready and self._thread is not None
                and self._thread.is_alive() and self.error is None)

    def _check_plugged(self):
        if self.unplugged.get("now"):
            raise FileNotFoundError(2, "No such file or directory", str(self.video_device))

    def prepare(self):
        self._check_plugged()

    def connect(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._check_plugged()
        self.connected = True
        self.ready = False
        self.error = self._capture_error = None
        self._frame = None
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
            self._last_at = None
            self._started_on_keyframe = False
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
        status = "completed" if self.frames and not self.error else "failed"
        events = [{"kind": "error", "detail": self.error}] if self.error else []
        return FakeReport(self.id, status, self.frames, self.error,
                          str(self._file_path) if self.frames else None, self.first, self.last, events)

    def disconnect(self):
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.connected = self.ready = False
        self.disconnects += 1

    def reconnect(self):
        self.disconnect()
        self.connect()

    def _loop(self):
        period = 1 / self.frame_hz
        while not self._halt.wait(period):
            with self._lock:
                self._ticks += 1
                self.ready = True  # The first live packet carried valid metadata.
                stalled = self.stall_after is not None and self.frames >= self.stall_after
                keyframe = self._ticks % self.keyframe_every == 0 and not stalled
                if not stalled:
                    self._queue_preview(b"packet", keyframe)
                if not self._recording or self._sinks is None or stalled:
                    continue
                if self.fail_after is not None and self.frames >= self.fail_after:
                    self.error = self._capture_error = "fake USB device vanished"
                    return
                if not self._started_on_keyframe:
                    if not keyframe:  # MP4 cannot begin on a predictive frame.
                        continue
                    self._started_on_keyframe = True
                self._write_frame()

    def _queue_preview(self, encoded, is_keyframe):
        """The adapter's own preview: keyframes only, a fresh array each, as its decoder hands one out."""
        if is_keyframe:
            self._frame = np.full(self._shape, 90 + self.keyframes % 100, dtype=np.uint8)
            self.keyframes += 1

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
        self.last = self._last_at = now
        self.frames += 1


class FakeStereoPreview:
    """What ``stereo_preview.StereoPreview`` offers collect.py, without H.264 or a decoder
    process: like the real one it takes over the stream's per-packet ``_queue_preview``,
    and every packet the stream reads becomes a fresh both-eye frame of ``size`` (w, h),
    so a stream that stops sending stops the preview too. With ``decoding`` False the
    decoder takes packets and returns nothing (wedged); ``fail`` is the real one giving
    up on it or finding it dead: the stream's own keyframe preview takes over again."""

    def __init__(self, stream, size):
        self.size = size
        self.stream = stream
        self.error = None
        self.closed = False
        self.frames = 0
        self.decoding = True
        self.offered_at = None
        self.superseded_frame = stream.latest_frame
        self._latest = None
        self._fresh = threading.Event()
        stream._queue_preview = self._offer

    @property
    def running(self):
        return self.error is None and not self.closed

    @property
    def latest_frame(self):
        return self._latest

    def _offer(self, encoded, is_keyframe):
        self.offered_at = time.monotonic()
        if not self.running or not self.decoding:
            return
        width, height = self.size
        self._latest = np.full((height, width, 3), self.frames % 256, dtype=np.uint8)
        self.frames += 1
        self._fresh.set()

    def wait(self, timeout):
        fresh = self._fresh.wait(timeout)
        self._fresh.clear()
        return fresh

    def fail(self, reason="preview decoder exited (fake)"):
        self.error = reason
        self.stream.__dict__.pop("_queue_preview", None)

    def close(self):
        self.closed = True
        self.stream.__dict__.pop("_queue_preview", None)


def fake_stream_class(streams, **defaults):
    """A subclass the SDK worker can derive from (it subclasses ``OvisionCameraStream``);
    every instance gets ``defaults`` and is appended to ``streams``."""

    class Configured(FakeOvisionStream):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **{**kwargs, **defaults})
            streams.append(self)

    return Configured
