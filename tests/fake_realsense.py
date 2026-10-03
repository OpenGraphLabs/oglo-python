"""A fake ``pyrealsense2`` for the RealSense tests: one simulated D455, no hardware.

Only the names ``oglo.studio_realsense`` and ``examples/camera_glove/realsense.py`` use
exist, spelled as the v2.58.4 Python bindings spell them. ``install(monkeypatch)``
puts the module in ``sys.modules`` and returns the :class:`Hardware` that drives it:
color at the configured rate as framesets and accel / gyro as single motion frames,
all stamped from one simulated camera clock, with switches for the failures the
worker must notice.
"""

from __future__ import annotations

import enum
import sys
import threading
import time
import types

import numpy as np


class _Enum(enum.Enum):
    def __str__(self):  # pybind11 enums print as "stream.color".
        return f"{type(self).__name__}.{self.name}"


stream = _Enum("stream", "color accel gyro depth infrared")
format = _Enum("format", "bgr8 rgb8 motion_xyz32f z16")  # noqa: A001 - the module's own name
camera_info = _Enum("camera_info", "name serial_number firmware_version recommended_firmware_version "
                                   "physical_port usb_type_descriptor product_line")
option = _Enum("option", "global_time_enabled enable_motion_correction exposure")
timestamp_domain = _Enum("timestamp_domain", "hardware_clock system_time global_time")
frame_metadata_value = _Enum("frame_metadata_value", "frame_timestamp sensor_timestamp")
distortion = _Enum("distortion", "none inverse_brown_conrady brown_conrady")


class vector:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class intrinsics:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.fx, self.fy = 640.0, 641.0
        self.ppx, self.ppy = width / 2, height / 2
        self.model = distortion.inverse_brown_conrady
        self.coeffs = [0.0, 0.0, 0.0, 0.0, 0.0]


class motion_device_intrinsic:
    def __init__(self):
        self.data = [[1.0, 0.0, 0.0, 0.01], [0.0, 1.0, 0.0, 0.02], [0.0, 0.0, 1.0, 0.03]]
        self.noise_variances = [1e-4, 1e-4, 1e-4]
        self.bias_variances = [1e-6, 1e-6, 1e-6]


class extrinsics:
    def __init__(self):
        self.rotation = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]  # column-major
        self.translation = [-0.0302, 0.0074, 0.0160]


class stream_profile:
    def __init__(self, hardware, kind, fmt, fps, width=0, height=0):
        self.hardware, self.kind, self.fmt, self.rate = hardware, kind, fmt, fps
        self._width, self._height = width, height

    def stream_type(self):
        return self.kind

    def format(self):
        return self.fmt

    def fps(self):
        return self.rate

    def width(self):
        return self._width

    def height(self):
        return self._height

    def as_video_stream_profile(self):
        return self

    def as_motion_stream_profile(self):
        return self

    def get_intrinsics(self):
        return intrinsics(self._width, self._height)

    def get_motion_intrinsics(self):
        if not self.hardware.imu_intrinsics:
            raise RuntimeError("Motion intrinsics are not available for this device")
        return motion_device_intrinsic()

    def get_extrinsics_to(self, other):
        return extrinsics()


class sensor:
    def __init__(self, hardware, profiles, motion=False):
        self.hardware, self.profiles = hardware, profiles
        self.options = {option.global_time_enabled: 1.0}
        if motion:
            self.options[option.enable_motion_correction] = 1.0

    def get_stream_profiles(self):
        return list(self.profiles)

    def supports(self, key):
        return key in self.options

    def set_option(self, key, value):
        self.options[key] = float(value)
        if key == option.global_time_enabled:
            self.hardware.global_time_set.append(float(value))

    def get_option(self, key):
        return self.options[key]


class device:
    def __init__(self, hardware):
        self.hardware = hardware
        color = [stream_profile(hardware, stream.color, format.bgr8, fps, width, height)
                 for width, height, fps in hardware.color_modes]
        motion = ([stream_profile(hardware, stream.accel, format.motion_xyz32f, hz) for hz in hardware.accel_rates]
                  + [stream_profile(hardware, stream.gyro, format.motion_xyz32f, hz) for hz in hardware.gyro_rates]
                  if hardware.imu else [])
        self.sensors = [sensor(hardware, color)] + ([sensor(hardware, motion, motion=True)] if motion else [])

    def supports(self, key):
        return key in self.hardware.info

    def get_info(self, key):
        if key not in self.hardware.info:
            raise RuntimeError(f"info {key} not supported")
        return self.hardware.info[key]

    def query_sensors(self):
        return list(self.sensors)


class context:
    def query_devices(self):
        return [hardware.device for hardware in Hardware.attached if hardware.plugged]


class config:
    def __init__(self):
        self.serial = None
        self.streams = []

    def enable_device(self, serial):
        self.serial = serial

    def enable_stream(self, kind, *args):
        self.streams.append((kind, *args))

    def resolve(self, wrapper):
        return pipeline_profile(self._hardware(), self)

    def _hardware(self):
        for hardware in Hardware.attached:
            if hardware.plugged and hardware.serial == self.serial:
                return hardware
        raise RuntimeError("No device connected")


class pipeline_profile:
    def __init__(self, hardware, cfg):
        self.hardware, self.cfg = hardware, cfg

    def get_device(self):
        return self.hardware.device

    def get_stream(self, kind):
        for entry in self.cfg.streams:
            if entry[0] == kind:
                if kind == stream.color:
                    _, width, height, fmt, fps = entry
                    return stream_profile(self.hardware, kind, fmt, fps, width, height)
                _, fmt, fps = entry
                return stream_profile(self.hardware, kind, fmt, fps)
        raise RuntimeError(f"{kind} is not enabled")


class pipeline_wrapper:
    def __init__(self, pipe):
        self.pipe = pipe


class pipeline:
    def __init__(self, ctx=None):
        self.hardware = None

    def start(self, cfg, callback):
        self.hardware = cfg._hardware()
        self.hardware.start(cfg, callback)
        return pipeline_profile(self.hardware, cfg)

    def stop(self):
        if self.hardware is None:
            raise RuntimeError("stop() cannot be called before start()")
        if not self.hardware.plugged:
            raise RuntimeError("Device disconnected")
        self.hardware.stop()


class _Frame:
    def __init__(self, hardware, profile, number, hw_us, data=None):
        self.hardware, self.profile, self.number, self.hw_us, self.data = hardware, profile, number, hw_us, data

    def is_frameset(self):
        return False

    def get_profile(self):
        return self.profile

    def get_frame_number(self):
        return self.number

    def get_data(self):
        return self.data

    def get_frame_timestamp_domain(self):
        # D400 color without UVC metadata is stamped with host system time
        # (ds-timestamp.cpp); IMU and color with metadata use the camera clock.
        if self.profile.kind == stream.color and not self.hardware.metadata:
            return timestamp_domain.system_time
        return timestamp_domain.global_time if self.hardware.global_time else timestamp_domain.hardware_clock

    def get_timestamp(self):  # milliseconds, as librealsense reports them
        domain = self.get_frame_timestamp_domain()
        if domain == timestamp_domain.system_time:
            return time.time() * 1000.0
        offset = self.hardware.global_offset_ms if domain == timestamp_domain.global_time else 0.0
        return self.hw_us / 1000.0 + offset

    def supports_frame_metadata(self, key):
        if self.profile.kind != stream.color or not self.hardware.metadata:
            return False
        return key != frame_metadata_value.sensor_timestamp or self.hardware.sensor_timestamp

    def get_frame_metadata(self, key):
        if not self.supports_frame_metadata(key):
            raise RuntimeError("metadata not available")
        # Mid-exposure: 5 ms before readout started.
        return self.hw_us - 5000 if key == frame_metadata_value.sensor_timestamp else self.hw_us

    def as_motion_frame(self):
        return self

    def get_motion_data(self):
        return self.data


class _Frameset(_Frame):
    def __init__(self, frames):
        self.frames = frames

    def is_frameset(self):
        return True

    def as_frameset(self):
        return self

    def __iter__(self):
        return iter(self.frames)


class Hardware:
    """One simulated camera. Class-level ``attached`` is what ``context`` enumerates."""

    attached: list["Hardware"] = []

    def __init__(self, serial="123456789012", *, name="Intel RealSense D455", usb_type="3.2",
                 imu=True, imu_intrinsics=True, metadata=True, honor_global_time=True,
                 color_modes=((320, 240, 30), (1280, 720, 30), (1280, 720, 15)),
                 accel_rates=(63, 250), gyro_rates=(200, 400), drop_every=None,
                 firmware="5.16.0.1", recommended="5.16.0.1", physical_port="2-1",
                 sensor_timestamp=True, clock32=False, clock_start_us=9_000_000_000,
                 imu_silent=False, imu_stop_after=None):
        self.serial, self.imu, self.imu_intrinsics = serial, imu, imu_intrinsics
        self.metadata, self.honor_global_time = metadata, honor_global_time
        self.color_modes, self.accel_rates, self.gyro_rates = color_modes, accel_rates, gyro_rates
        self.drop_every = drop_every
        # The real camera clock is a 32-bit microsecond counter (clock32); the default
        # epoch lies above 2**32 so tests that ignore wrapping never meet one.
        self.sensor_timestamp, self.clock32, self.clock_start_us = sensor_timestamp, clock32, clock_start_us
        # imu_silent: the IMU never starts, and librealsense's aggregator then holds color back too.
        self.imu_silent, self.imu_stop_after = imu_silent, imu_stop_after
        self.info = {camera_info.name: name, camera_info.serial_number: serial,
                     camera_info.firmware_version: firmware,
                     camera_info.recommended_firmware_version: recommended,
                     camera_info.physical_port: physical_port,
                     camera_info.usb_type_descriptor: usb_type, camera_info.product_line: "D400"}
        self.plugged = True
        self.global_time_set: list[float] = []
        self.global_offset_ms = 1.7e12  # Global time is host wall time, far from the camera clock.
        self.color_stalled = threading.Event()
        self.starts = 0
        self._thread = None
        self._stop = threading.Event()
        self.device = device(self)

    @property
    def global_time(self):
        return not (self.honor_global_time and self.global_time_set and self.global_time_set[-1] == 0.0)

    def start(self, cfg, callback):
        self.stop()
        self._stop = threading.Event()
        self.starts += 1
        rates = {}
        for entry in cfg.streams:
            if entry[0] == stream.color:
                _, width, height, _fmt, fps = entry
                rates[stream.color] = fps
                size = (width, height)
            else:
                rates[entry[0]] = entry[2]
        self._thread = threading.Thread(target=self._run, args=(callback, rates, size, self._stop),
                                        name="fake-realsense", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def unplug(self):
        self.plugged = False
        self._stop.set()

    def replug(self):
        self.plugged = True

    def _run(self, callback, rates, size, stop):
        t0 = time.monotonic()
        numbers = {kind: 0 for kind in rates}
        due = {kind: 0.0 for kind in rates}
        profiles = {kind: stream_profile(self, kind, format.bgr8 if kind == stream.color else format.motion_xyz32f,
                                         rate, *(size if kind == stream.color else (0, 0)))
                    for kind, rate in rates.items()}
        while not stop.is_set() and self.plugged:
            now = time.monotonic() - t0
            for kind, rate in rates.items():
                if now < due[kind]:
                    continue
                due[kind] += 1.0 / rate
                numbers[kind] += 1
                hw_us = self.clock_start_us + int(now * 1_000_000)
                if self.clock32:
                    hw_us %= 1 << 32
                if self.imu_silent:
                    continue
                if kind != stream.color and self.imu_stop_after is not None and now > self.imu_stop_after:
                    continue
                if kind == stream.color:
                    if self.color_stalled.is_set():
                        continue
                    if self.drop_every and numbers[kind] % self.drop_every == 0:
                        numbers[kind] += 1  # The camera skipped a frame number.
                    image = np.full((size[1], size[0], 3), 40 + numbers[kind] % 150, np.uint8)
                    callback(_Frameset([_Frame(self, profiles[kind], numbers[kind], hw_us, image)]))
                else:
                    value = vector(0.01, -9.81, 0.02) if kind == stream.accel else vector(0.001, 0.002, -0.003)
                    callback(_Frame(self, profiles[kind], numbers[kind], hw_us, value))
            stop.wait(0.001)


def _module():
    module = types.ModuleType("pyrealsense2")
    for name in ("stream", "format", "camera_info", "option", "timestamp_domain", "frame_metadata_value",
                 "distortion", "vector", "context", "config", "pipeline", "pipeline_wrapper"):
        setattr(module, name, globals()[name])
    return module


def install(monkeypatch, *hardware, version="2.58.4.10922"):
    """Install the fake as ``pyrealsense2`` with ``hardware`` attached (one D455 by default)."""
    attached = list(hardware) or [Hardware()]
    monkeypatch.setattr(Hardware, "attached", attached)
    monkeypatch.setitem(sys.modules, "pyrealsense2", _module())
    import oglo.studio_realsense as studio_realsense

    monkeypatch.setattr(studio_realsense, "installed_version", lambda: version)
    return attached[0]


def install_globally(*hardware, version="2.58.4.10922"):
    """:func:`install` for scripts such as the browser harness: nothing is restored."""
    Hardware.attached = list(hardware) or [Hardware()]
    sys.modules["pyrealsense2"] = _module()
    import oglo.studio_realsense as studio_realsense

    studio_realsense.installed_version = lambda: version
    return Hardware.attached[0]
