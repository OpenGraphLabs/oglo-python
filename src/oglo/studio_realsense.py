"""RealSense D455 color + camera IMU for Studio and the camera examples.

This module is imported only for the RealSense camera choice. ``pyrealsense2``
delivers color frames and accelerometer / gyroscope samples on the camera's own
clock; the worker keeps that clock (global time is switched off), stamps host
arrival time in the librealsense callback and never encodes there. One worker stays
open for a whole Studio or ``collect.py`` session; each take records into its own
``camera/`` folder between ``begin`` and ``finish``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import queue
import threading
import time

PYREALSENSE2_VERSION = "2.58.4.10922"
INSTALL_HINT = "pip install -r examples/camera_glove/requirements-realsense.txt (Studio: '.[studio,studio-realsense]')"
COLOR_SIZE = (1280, 720)
CLOCK_DOMAIN = "realsense_hw_clock"
ACCEL_FILE, GYRO_FILE, CALIBRATION_FILE = (
    "realsense.accel.jsonl", "realsense.gyro.jsonl", "realsense.calibration.json")
STALL_NS = 5_000_000_000  # No color frame for this long ends a take, as the OVISION worker does.
CLOCK_WRAP_US = 1 << 32  # The camera clock is a 32-bit microsecond counter: it wraps every ~71.6 min.
IMU_EDGE_US = 500_000  # The IMU must start and end within this of the color frames' camera time...
IMU_GAP_US = 250_000  # ...and never pause longer than this, or the take lacks camera IMU in places.
QUEUE_FRAMES = 64  # About two seconds of color waiting for the encoder; beyond that frames drop.


def _pyrealsense2():
    try:
        import pyrealsense2
    except ImportError as exc:
        raise RuntimeError(f"pyrealsense2 is not installed: {INSTALL_HINT}") from exc
    return pyrealsense2


def installed_version() -> str | None:
    try:
        return version("pyrealsense2")
    except PackageNotFoundError:
        return None


def _info(rs, device, key):
    field = getattr(rs.camera_info, key)
    return device.get_info(field) if device.supports(field) else None


def _enum_name(value) -> str:
    return str(value).rsplit(".", 1)[-1]


def list_devices() -> list[dict]:
    """Every attached RealSense, described without starting a stream."""
    rs = _pyrealsense2()
    found = []
    for device in rs.context().query_devices():
        streams = {profile.stream_type() for sensor in device.query_sensors()
                   for profile in sensor.get_stream_profiles()}
        found.append({
            "name": _info(rs, device, "name"), "serial": _info(rs, device, "serial_number"),
            "firmware": _info(rs, device, "firmware_version"),
            "recommended_firmware": _info(rs, device, "recommended_firmware_version"),
            "usb_type": _info(rs, device, "usb_type_descriptor"),
            "physical_port": _info(rs, device, "physical_port"),
            "product_line": _info(rs, device, "product_line"),
            "has_imu": rs.stream.accel in streams and rs.stream.gyro in streams,
        })
    return found


def _mp4v_writer(path: Path, fps: float, size: tuple[int, int]):
    import cv2

    return cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)


def _vector(values) -> list[float]:
    return [float(value) for value in values]


class TooFewFrames(RuntimeError):
    """The take was stopped before two color frames arrived (``g`` then ``h`` at once).

    Nothing to keep and nothing broke: the camera and the gloves are fine.
    """


class _Take:
    """One take's routing target: the callback fills it, the writer thread drains it."""

    def __init__(self, folder: Path, stop: threading.Event) -> None:
        self.folder, self.stop = folder, stop
        self.frames: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self.imu: dict[str, list[dict]] = {"accel": [], "gyro": []}
        self.closed = False  # Set under the worker lock; nothing is routed afterwards.
        self.ending = threading.Event()
        self.error: str | None = None
        self.started_ns = time.monotonic_ns()
        self.count = 0
        self.size: tuple[int, int] | None = None
        self.first = self.last = None
        self.dropped = 0
        self.untimed = 0  # Color frames without camera-clock time.
        self.color_device: list[int] = []  # [first, last] camera time of color.
        self.samples = {"accel": 0, "gyro": 0}
        self.imu_device: dict[str, list[int]] = {"accel": [], "gyro": []}
        self.imu_max_gap = {"accel": 0, "gyro": 0}  # Largest camera-time step between samples, us.
        self.imu_untimed = {"accel": 0, "gyro": 0}  # IMU samples without camera-clock time.
        self.backwards: list[str] = []  # Streams whose camera time did not strictly increase.
        self.writer_thread: threading.Thread | None = None


class RealSenseCameraWorker:
    """Keep one RealSense streaming color + accel + gyro; record takes on demand."""

    native_device_timestamps = True
    postprocessing_capable = False

    def __init__(self, index: int = 0, *, mode: str = "realsense", name: str, fps: float = 30.0,
                 writer_factory=None, codec: str = "mp4v", video_quality: int | None = None,
                 size: tuple[int, int] = COLOR_SIZE, ready_timeout: float = 10.0) -> None:
        if mode != "realsense" or not name:
            raise ValueError("select a RealSense camera by its serial number")
        rs = _pyrealsense2()
        import cv2

        self.rs, self.cv2 = rs, cv2
        self.index, self.mode, self.name = index, mode, name
        self.fps, self.size = float(fps), tuple(size)
        self.writer_factory = writer_factory or _mp4v_writer
        self.codec, self.video_quality = codec, video_quality
        self.error: str | None = None
        self._lock = threading.Lock()
        self._latest = None
        self._last_frame_ns: int | None = None
        self._jpeg: bytes | None = None
        self._jpeg_frame = None
        self._jpeg_at = 0.0
        self._brightness: float | None = None
        self._dark_samples = 0
        self._take: _Take | None = None
        self._last_take: _Take | None = None
        self._clock_ref: int | None = None  # Newest unwrapped camera time, shared by every stream.
        self.arrived = {"color": 0, "accel": 0, "gyro": 0}  # Frames seen since start, for diagnosis.

        device = next((candidate for candidate in rs.context().query_devices()
                       if _info(rs, candidate, "serial_number") == name), None)
        if device is None:
            raise RuntimeError(f"RealSense {name} is not connected")
        self.device_info = {
            "model": _info(rs, device, "name"), "usb_serial": name,
            "firmware_version": _info(rs, device, "firmware_version"),
            "recommended_firmware_version": _info(rs, device, "recommended_firmware_version"),
            "usb_type": _info(rs, device, "usb_type_descriptor"),
            "physical_port": _info(rs, device, "physical_port"),
            "pyrealsense2_version": installed_version(),
        }
        self.accel_hz, self.gyro_hz = self._check_profiles(device)
        pipeline = rs.pipeline(rs.context())
        config = rs.config()
        config.enable_device(name)
        config.enable_stream(rs.stream.color, self.size[0], self.size[1], rs.format.bgr8, int(round(self.fps)))
        config.enable_stream(rs.stream.accel, rs.format.motion_xyz32f, self.accel_hz)
        config.enable_stream(rs.stream.gyro, rs.format.motion_xyz32f, self.gyro_hz)
        resolved = config.resolve(rs.pipeline_wrapper(pipeline))
        # Camera-clock time, not librealsense's host-mapped "global time" (a clock
        # other than time.monotonic_ns, which would quietly corrupt alignment).
        for sensor in resolved.get_device().query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 0)
        self.calibration = self._calibration(resolved)
        self.pipeline = pipeline
        self.pipeline.start(config, self._on_frame)
        try:
            deadline = time.monotonic() + ready_timeout
            while self.latest_frame is None:
                if self.error:
                    raise RuntimeError(self.error)
                if time.monotonic() >= deadline:
                    seen = ", ".join(f"{stream} {count}" for stream, count in self.arrived.items())
                    raise RuntimeError(
                        f"RealSense {name} delivered no color frame within {ready_timeout:g} s (frames seen: "
                        f"{seen}). librealsense holds color back until the accelerometer and gyroscope "
                        "have sent a sample, so a silent IMU looks like this: check the udev rules, the "
                        "firmware, and that no other program has the camera open")
                time.sleep(0.02)
        except BaseException:
            self._stop_pipeline()
            raise

    # -- setup ---------------------------------------------------------------------

    def _check_profiles(self, device) -> tuple[int, int]:
        rs = self.rs
        color_modes, rates = set(), {"accel": set(), "gyro": set()}
        for sensor in device.query_sensors():
            for profile in sensor.get_stream_profiles():
                stream = profile.stream_type()
                if stream == rs.stream.color and profile.format() == rs.format.bgr8:
                    video = profile.as_video_stream_profile()
                    color_modes.add((video.width(), video.height(), profile.fps()))
                elif stream in (rs.stream.accel, rs.stream.gyro) and profile.format() == rs.format.motion_xyz32f:
                    rates["accel" if stream == rs.stream.accel else "gyro"].add(profile.fps())
        if not rates["accel"] or not rates["gyro"]:
            raise RuntimeError("this RealSense has no accelerometer + gyroscope (a D455 has both)")
        wanted = (self.size[0], self.size[1], int(round(self.fps)))
        if wanted not in color_modes:
            offered = ", ".join(f"{w}x{h}@{f}" for w, h, f in sorted(color_modes)) or "none"
            raise RuntimeError(f"RealSense color {wanted[0]}x{wanted[1]} BGR8 at {wanted[2]} fps is not "
                               f"offered; offered: {offered}")
        self.offered_rates = {stream: sorted(values) for stream, values in rates.items()}
        return max(rates["accel"]), max(rates["gyro"])

    def _calibration(self, profile) -> dict:
        rs = self.rs
        color = profile.get_stream(rs.stream.color)
        intrinsics = color.as_video_stream_profile().get_intrinsics()
        result = {
            "schema": "oglo-realsense-calibration.v1",
            "device": {"name": self.device_info["model"], "serial": self.name,
                       "firmware_version": self.device_info["firmware_version"],
                       "recommended_firmware_version": self.device_info["recommended_firmware_version"],
                       "usb_type": self.device_info["usb_type"],
                       "physical_port": self.device_info["physical_port"],
                       "pyrealsense2_version": self.device_info["pyrealsense2_version"]},
            "color": {"width": intrinsics.width, "height": intrinsics.height, "fps": int(round(self.fps)),
                      "format": "bgr8",
                      "intrinsics": {"fx": intrinsics.fx, "fy": intrinsics.fy, "ppx": intrinsics.ppx,
                                     "ppy": intrinsics.ppy, "model": _enum_name(intrinsics.model),
                                     "coeffs": _vector(intrinsics.coeffs)}},
            "extrinsics_note": "rotation is librealsense's column-major 3x3; translation in meters",
            "motion_correction": self._motion_correction(profile),
        }
        for stream, rate, unit in (("accel", self.accel_hz, "m/s^2"), ("gyro", self.gyro_hz, "rad/s")):
            motion = profile.get_stream(getattr(rs.stream, stream))
            entry = {"hz": rate, "offered_hz": self.offered_rates[stream], "unit": unit,
                     "intrinsics": None, "intrinsics_unavailable": None}
            try:
                values = motion.as_motion_stream_profile().get_motion_intrinsics()
                entry["intrinsics"] = {"data": [_vector(row) for row in values.data],
                                       "noise_variances": _vector(values.noise_variances),
                                       "bias_variances": _vector(values.bias_variances)}
            except Exception as exc:  # Units without an IMU calibration table raise here.
                entry["intrinsics_unavailable"] = f"{type(exc).__name__}: {exc}"
            extrinsics = color.get_extrinsics_to(motion)
            entry["extrinsics_from_color"] = {"rotation": _vector(extrinsics.rotation),
                                              "translation": _vector(extrinsics.translation)}
            result[stream] = entry
        return result

    def _motion_correction(self, profile) -> dict:
        """Whether librealsense already applied the IMU intrinsics to the delivered samples."""
        rs = self.rs
        option = getattr(rs.option, "enable_motion_correction", None)
        for sensor in profile.get_device().query_sensors():
            if option is not None and sensor.supports(option):
                enabled = bool(sensor.get_option(option))
                return {"enabled": enabled,
                        "note": ("samples already have the IMU intrinsics (scale, bias) applied by "
                                 "librealsense; do not apply them again") if enabled else
                                ("samples are raw; apply the IMU intrinsics below to correct them")}
        return {"enabled": None, "note": "this device reports no enable_motion_correction option"}

    def _unwrap(self, raw: int) -> int:
        """Lift a 32-bit camera time onto one continuous timeline for the whole session.

        Color and IMU wrap together, but reach the host at different delays, so every
        stream is lifted to the wrap count nearest the newest time seen on any stream
        (they are never 35 minutes apart). Call under ``self._lock``.
        """
        if self._clock_ref is not None:
            raw += CLOCK_WRAP_US * round((self._clock_ref - raw) / CLOCK_WRAP_US)
        if self._clock_ref is None or raw > self._clock_ref:
            self._clock_ref = raw
        return raw

    # -- the librealsense callback thread: copy, stamp, route; never encode ---------

    def _on_frame(self, frame) -> None:
        received = time.monotonic_ns()
        try:
            frames = frame.as_frameset() if frame.is_frameset() else (frame,)
            for item in frames:
                stream = item.get_profile().stream_type()
                if stream == self.rs.stream.color:
                    self._on_color(item, received)
                elif stream in (self.rs.stream.accel, self.rs.stream.gyro):
                    self._on_motion(item, stream, received)
        except Exception as exc:  # librealsense would swallow it; make it visible instead.
            self.error = f"RealSense callback failed: {type(exc).__name__}: {exc}"

    def _color_time(self, frame) -> tuple[int | None, str | None]:
        rs = self.rs
        if frame.supports_frame_metadata(rs.frame_metadata_value.sensor_timestamp):
            return int(frame.get_frame_metadata(rs.frame_metadata_value.sensor_timestamp)), "sensor_timestamp"
        if frame.get_frame_timestamp_domain() == rs.timestamp_domain.hardware_clock:
            return int(round(frame.get_timestamp() * 1000)), "frame_timestamp"
        return None, None

    def _on_color(self, frame, received: int) -> None:
        import numpy as np

        image = np.asanyarray(frame.get_data()).copy()  # librealsense recycles the buffer.
        device, meaning = self._color_time(frame)
        with self._lock:
            self.arrived["color"] += 1
            if device is not None:
                device = self._unwrap(device)
            item = (image, received, device, meaning, frame.get_frame_number())
            self._latest = image
            self._last_frame_ns = received
            take = self._take
            if take is not None and not take.closed and not take.stop.is_set():
                try:
                    take.frames.put_nowait(item)
                except queue.Full:
                    pass  # Counted as a gap in the frame counter.

    def _on_motion(self, frame, stream, received: int) -> None:
        rs = self.rs
        device = (int(round(frame.get_timestamp() * 1000))
                  if frame.get_frame_timestamp_domain() == rs.timestamp_domain.hardware_clock else None)
        data = frame.as_motion_frame().get_motion_data()
        name = "accel" if stream == rs.stream.accel else "gyro"
        with self._lock:
            self.arrived[name] += 1
            if device is not None:
                device = self._unwrap(device)
            row = {"frame_number": frame.get_frame_number(), "host_received_ns": received,
                   "device_timestamp_us": device, "x": data.x, "y": data.y, "z": data.z}
            take = self._take
            if take is not None and not take.closed and not take.stop.is_set():
                take.imu[name].append(row)

    # -- Studio camera contract ------------------------------------------------------

    @property
    def latest_frame(self):
        with self._lock:
            return self._latest

    @property
    def last_frame_ns(self) -> int | None:
        """Host time of the newest color frame; a cheap liveness check for idle loops."""
        with self._lock:
            return self._last_frame_ns

    def _update_preview(self) -> None:
        with self._lock:
            frame = self._latest
            if frame is None or frame is self._jpeg_frame or time.monotonic() - self._jpeg_at < 0.2:
                return
        brightness = float(frame[::16, ::16].mean())
        encoded, jpeg = self.cv2.imencode(".jpg", frame)
        with self._lock:
            self._jpeg_frame, self._jpeg_at = frame, time.monotonic()
            self._brightness = round(brightness, 1)
            self._dark_samples = min(3, self._dark_samples + 1) if brightness < 18 else 0
            if encoded:
                self._jpeg = jpeg.tobytes()

    def preview(self) -> bytes | None:
        self._update_preview()
        with self._lock:
            return self._jpeg

    def live_status(self) -> dict:
        self._update_preview()
        with self._lock:
            last = self._last_frame_ns
            age = round((time.monotonic_ns() - last) / 1_000_000) if last else None
            return {"ready": bool(self._jpeg and age is not None and age <= 1000 and not self.error),
                    "age_ms": age, "dark": self._dark_samples >= 3,
                    "brightness": self._brightness, "error": self.error}

    def begin(self, folder: Path, stop: threading.Event) -> None:
        with self._lock:
            last = self._last_frame_ns
            busy = self._take is not None
        if self.error or busy:
            raise RuntimeError(self.error or "RealSense camera is already recording")
        if last is None or time.monotonic_ns() - last > 1_000_000_000:
            raise RuntimeError("RealSense camera is not delivering frames")
        folder.mkdir()
        (folder / CALIBRATION_FILE).write_text(json.dumps(self.calibration, indent=2) + "\n", encoding="utf-8")
        take = _Take(folder, stop)
        take.writer_thread = threading.Thread(target=self._write, args=(take,), name="realsense-writer",
                                              daemon=True)
        with self._lock:
            self._take = self._last_take = take
        take.writer_thread.start()

    def finish(self, timeout: float = 10.0) -> dict:
        """Stop routing, close the files, check the take, and return its metadata.

        Routing already stops when the caller's stop event is set; this also ends a take
        that reached its deadline. It never sets that event itself: the examples' gloves
        finish their own duration.
        """
        take = self._last_take
        if take is None or take.writer_thread is None:
            raise RuntimeError("RealSense capture was not started")
        take.ending.set()
        take.writer_thread.join(timeout)
        if take.writer_thread.is_alive():
            # A writer stuck in the encoder never reaches its own cleanup: mark the worker
            # failed so Studio and collect.py replace it instead of refusing every take.
            self.error = "RealSense writer did not finish after Stop; capture is incomplete"
            self._stop_routing(take)
            self._last_take = None
            raise RuntimeError(self.error)
        with self._lock:
            if self._take is take:
                self._take = None
        self._last_take = None
        if take.error:
            raise RuntimeError(take.error)
        self._check(take)
        return self._metadata(take)

    def close(self) -> None:
        take = self._take
        if take is not None:
            take.ending.set()
            if take.writer_thread is not None:
                take.writer_thread.join(timeout=2)
        self._stop_pipeline()

    def _stop_pipeline(self) -> None:
        try:
            self.pipeline.stop()
        except Exception:  # An unplugged camera cannot be stopped; nothing is left to release.
            pass

    # -- per-take writer thread -------------------------------------------------------

    def _write(self, take: _Take) -> None:
        writer = rows = None
        files = {}
        try:
            files = {name: (take.folder / filename).open("x", encoding="utf-8")
                     for name, filename in (("accel", ACCEL_FILE), ("gyro", GYRO_FILE))}
            rows = (take.folder / "timestamps.jsonl").open("x", encoding="utf-8")
            previous_number = None
            while True:
                # The shared stop (Studio's Stop, collect.py's h, a glove failure) ends the
                # take at the same instant as the gloves; finish() ends it at a deadline.
                if take.ending.is_set() or take.stop.is_set():
                    self._stop_routing(take)
                try:
                    image, received, device, meaning, number = take.frames.get(timeout=0.1)
                except queue.Empty:
                    self._flush_imu(take, files)
                    if take.closed:
                        break
                    last = self._last_frame_ns or take.started_ns
                    if time.monotonic_ns() - max(last, take.started_ns) > STALL_NS:
                        raise RuntimeError("RealSense stopped returning color frames for five seconds")
                    continue
                height, width = image.shape[:2]
                if writer is None:
                    if width % 2 or height % 2:
                        raise RuntimeError("camera dimensions must be even for MP4")
                    take.size = (width, height)
                    writer = self.writer_factory(take.folder / "video.mp4", self.fps, take.size)
                    if not writer.isOpened():
                        raise RuntimeError(f"could not open the {self.codec} video encoder")
                if (width, height) != take.size:
                    raise RuntimeError("camera dimensions changed during recording")
                writer.write(image)
                if previous_number is not None and number > previous_number + 1:
                    take.dropped += number - previous_number - 1
                previous_number = number
                if device is None:
                    take.untimed += 1
                else:
                    if take.color_device and device <= take.color_device[1]:
                        take.backwards.append("color")
                    take.color_device = [take.color_device[0] if take.color_device else device, device]
                rows.write(json.dumps({
                    "frame_index": take.count, "host_read_started_ns": None, "host_received_ns": received,
                    "device_timestamp": device, "device_timestamp_unit": "us" if device is not None else None,
                    "device_clock_domain": CLOCK_DOMAIN if device is not None else None,
                    "device_timestamp_meaning": meaning, "native_frame_number": number,
                }) + "\n")
                take.first = received if take.first is None else take.first
                take.last = received
                take.count += 1
                if take.count % 10 == 0:
                    self._flush_imu(take, files)
        except BaseException as exc:
            take.error = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
            self.error = take.error
            self._stop_routing(take)
            take.stop.set()  # Also stops the gloves, as the OVISION watchdog does.
        finally:
            try:
                self._flush_imu(take, files)
            except Exception as exc:
                take.error = take.error or f"writing camera IMU failed: {exc}"
            for handle in (*files.values(), rows):
                if handle is not None:
                    handle.close()
            if writer is not None:
                writer.release()
                if getattr(writer, "error", None) and not take.error:
                    take.error = writer.error

    def _stop_routing(self, take: _Take) -> None:
        with self._lock:
            take.closed = True
            if self._take is take:
                self._take = None

    def _flush_imu(self, take: _Take, files: dict) -> None:
        for name, handle in files.items():
            with self._lock:
                pending, take.imu[name] = take.imu[name], []
            for row in pending:
                device = row["device_timestamp_us"]
                seen = take.imu_device[name]
                if device is None:
                    take.imu_untimed[name] += 1
                else:
                    if seen and device <= seen[1]:
                        take.backwards.append(name)
                    if seen:
                        take.imu_max_gap[name] = max(take.imu_max_gap[name], device - seen[1])
                    take.imu_device[name] = [seen[0] if seen else device, device]
                handle.write(json.dumps(row) + "\n")
                take.samples[name] += 1
            if pending:
                handle.flush()

    # -- completeness ----------------------------------------------------------------

    def _check(self, take: _Take) -> None:
        if take.count < 2:
            raise TooFewFrames(f"{take.count} color frame(s) before the stop: nothing to keep")
        if take.untimed:
            raise RuntimeError(f"{take.untimed} color frame(s) lack camera-clock time: the camera's frame "
                               "metadata is not reaching librealsense (check the udev rules; through the "
                               "kernel video driver a D455 needs Linux 6.5 or newer)")
        for name in ("accel", "gyro"):
            if take.imu_untimed[name]:
                raise RuntimeError(f"{take.imu_untimed[name]} {name} sample(s) lack camera-clock time")
            if take.samples[name] < 2:
                raise RuntimeError(f"RealSense recorded {take.samples[name]} {name} sample(s); need at least two")
        if take.backwards:
            streams = ", ".join(sorted(set(take.backwards)))
            raise RuntimeError(f"camera-clock time did not strictly increase in: {streams}")
        start, end = take.color_device
        for name in ("accel", "gyro"):
            first, last = take.imu_device[name]
            if max(start, first) >= min(end, last):
                raise RuntimeError(f"{name} camera-clock time does not overlap the color frames")
            if first - start > IMU_EDGE_US or end - last > IMU_EDGE_US:
                raise RuntimeError(f"{name} covers {first - start} us after the first and {end - last} us "
                                   "before the last color frame only; the camera IMU stopped or started late")
            if take.imu_max_gap[name] > IMU_GAP_US:
                raise RuntimeError(f"{name} paused for {take.imu_max_gap[name]} us of camera time during "
                                   "the take; the camera IMU stopped delivering samples")

    def _metadata(self, take: _Take) -> dict:
        return {
            "kind": "realsense", "index": self.index, "mode": self.mode, "name": self.name,
            **self.device_info,
            "video": "camera/video.mp4", "timestamps": "camera/timestamps.jsonl",
            "accel": f"camera/{ACCEL_FILE}", "gyro": f"camera/{GYRO_FILE}",
            "calibration": f"camera/{CALIBRATION_FILE}",
            "native_artifacts": [f"camera/{name}" for name in (ACCEL_FILE, GYRO_FILE, CALIBRATION_FILE)],
            "codec": self.codec, "video_quality": self.video_quality,
            "playback_fps": self.fps, "requested_fps": self.fps,
            "backend": f"pyrealsense2 {self.device_info['pyrealsense2_version']}",
            "host_timestamp_meaning": "host monotonic time when librealsense delivered the frame; "
                                      "not exposure time",
            "device_clock_domain": CLOCK_DOMAIN, "native_device_timestamps": True,
            "accel_hz": self.accel_hz, "gyro_hz": self.gyro_hz,
            "accel_unit": "m/s^2", "gyro_unit": "rad/s",
            "frames_submitted": take.count, "width": take.size[0], "height": take.size[1],
            "first_host_received_ns": take.first, "last_host_received_ns": take.last,
            "native_frames_dropped": take.dropped,
            "accel_samples": take.samples["accel"], "gyro_samples": take.samples["gyro"],
            "accel_expected_samples": self._expected(take, self.accel_hz),
            "gyro_expected_samples": self._expected(take, self.gyro_hz),
            "accel_max_gap_us": take.imu_max_gap["accel"], "gyro_max_gap_us": take.imu_max_gap["gyro"],
            "device_clock_unwrapped": True,
        }

    @staticmethod
    def _expected(take: _Take, hz: int) -> int:
        """Samples a stream at ``hz`` would have delivered over the color frames' camera time."""
        start, end = take.color_device
        return int((end - start) * hz / 1_000_000) + 1
