"""Public sample types. What a user actually holds.

`_wire` produces decoder-shaped records; this is the layer a researcher touches, so
the names and units are the ones the documentation promises.

Two refusals are deliberate and are enforced here rather than documented and hoped for:

- `residual` exists only for a device-clean stream. There is no host-side baseline
  path that can disagree with the glove.
- `orientation` raises. The IMU axes are now measured, but a full orientation also
  needs the magnetometer's, and those are not: see `R_FRAME_FROM_IMU`. Use
  `accel_frame` / `gyro_frame`, which rest on the part that was measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

from ._wire import NUM_COLS, NUM_FINGERS, ROWS_PER_FINGER, TAXELS

SHAPE = (NUM_FINGERS, ROWS_PER_FINGER, NUM_COLS)


#: Sensor axes -> the landmark frame (+Z out of the module side, +X toward USB-C,
#: +Y = Z x X). **Measured, not derived**: six gravity poses on OGLO-R-TEST04 and
#: OGLO-L-TEST01 on 2026-08-07 gave byte-identical matrices, determinant +1.
#:
#:     frame X = +sensor y      frame Z = -sensor z
#:     frame Y = +sensor x
#:
#: As a rotation that is 180 degrees about (1,1,0), which is a 90-degree footprint
#: rotation composed with the back-face mount -- the arrangement the hardware
#: documentation predicts, arrived at here from gravity alone.
R_FRAME_FROM_IMU = np.array([[0, 1, 0],
                             [1, 0, 0],
                             [0, 0, -1]], dtype=np.float32)

#: The magnetometer is NOT included. Two boards measured minutes apart put its
#: out-of-plane axis in different places, and |B| swung 0.70-1.34 G across poses when
#: Earth's field is a constant ~0.5 G. The readings were contaminated; nothing about
#: the magnetometer's axes is known.

_NO_ORIENTATION = (
    "OGLO does not publish a fused orientation. The accelerometer/gyroscope axis "
    "rotation is measured and available through accel_frame/gyro_frame, but the "
    "magnetometer axes and magnetic environment are not validated. A fused quaternion "
    "would therefore look plausible without being trustworthy."
)


class CleanStreamError(RuntimeError):
    """Raised when host-side baseline subtraction would double-subtract."""


@dataclass(frozen=True)
class Frame:
    """One tactile sample.

    `counts` are raw 12-bit ADC counts in wire order `(finger, row, col)`, **not
    force**. An untouched taxel reads around 550, not 0. Finger order comes from
    `info.channels`; the left hand is reversed.
    """

    seq: int
    #: Low u32 of device microseconds. **Never align two gloves on this.**
    t_us: int
    #: Host receive-boundary monotonic seconds. It relates devices approximately,
    #: but USB/BLE buffering means it is not hardware-synchronised sample time.
    host_t: float
    counts: np.ndarray  # (5, 4, 4) uint16
    #: Host-observed sequence gap since the previous frame. Distinct from what the
    #: device discarded itself, which is exposed by ``glove.status()``.
    dropped: int = 0

    #: Native TAG v2 u64 time, or TAG v1 time unwrapped across u32 rollover. Its
    #: origin is device-local; use ordering/differences, not cross-glove alignment.
    device_time_us: Optional[int] = None
    #: Host monotonic timestamp at the USB-read/BLE-notify boundary, in nanoseconds.
    #: Samples decoded from the same transport batch intentionally share it.
    host_t_ns: Optional[int] = None
    #: Actual host monotonic timestamp at the USB-read/BLE-notify boundary.
    host_received_ns: Optional[int] = None

    _stream_clean: bool = field(default=False, repr=False)

    @property
    def residual(self) -> np.ndarray:
        """Counts above the zero baseline, as float32.

        When the device streams clean it has already subtracted its baseline and
        applied the deadband, so the counts ARE the residual and this returns them
        unchanged. A raw stream has no residual by definition and raises rather than
        returning ADC counts under a processed-data name.
        """
        if self._stream_clean:
            return self.counts.astype(np.float32)
        raise CleanStreamError(
            "the device is streaming raw ADC counts. Call glove.zero(sweep=5) and "
            "glove.clean(), or use .counts for raw ADC."
        )

    @property
    def orientation(self):
        raise NotImplementedError(_NO_ORIENTATION)

    def finger(self, index: int) -> np.ndarray:
        """The 4x4 grid for one finger, by wire position."""
        return self.counts[index]

    def __post_init__(self) -> None:
        _validate_wire_header(self.seq, self.t_us)
        if self.counts.shape != SHAPE:
            raise ValueError(f"counts must be {SHAPE}, got {self.counts.shape}")
        if self.counts.size and (int(self.counts.min()) < 0 or int(self.counts.max()) > 4095):
            raise ValueError("counts contain a value outside the 12-bit ADC range 0..4095")
        if self.device_time_us is None:
            object.__setattr__(self, "device_time_us", int(self.t_us) & 0xFFFFFFFF)
        if self.host_t_ns is None:
            object.__setattr__(self, "host_t_ns", int(round(self.host_t * 1_000_000_000)))
        if self.host_received_ns is None:
            object.__setattr__(self, "host_received_ns", self.host_t_ns)

    @property
    def device_time_ns(self) -> int:
        return int(self.device_time_us) * 1000


@dataclass(frozen=True)
class ImuSample:
    """Accelerometer and gyroscope, in the IMU's own sensing frame.

    Datasheet-confirmed scales: +/-8 g at 4096 LSB/g, +/-2000 deg/s at 16.4 LSB/(deg/s)
    (ICM-42688-P, DS-000347 Rev 1.2).
    """

    seq: int
    t_us: int
    host_t: float
    accel: Tuple[float, float, float]  # g
    gyro: Tuple[float, float, float]  # deg/s
    dropped: int = 0
    raw: Optional[Tuple[int, ...]] = None
    device_time_us: Optional[int] = None
    host_t_ns: Optional[int] = None
    host_received_ns: Optional[int] = None

    def __post_init__(self) -> None:
        _validate_wire_header(self.seq, self.t_us)
        if self.device_time_us is None:
            object.__setattr__(self, "device_time_us", int(self.t_us) & 0xFFFFFFFF)
        if self.host_t_ns is None:
            object.__setattr__(self, "host_t_ns", int(round(self.host_t * 1_000_000_000)))
        if self.host_received_ns is None:
            object.__setattr__(self, "host_received_ns", self.host_t_ns)

    @property
    def device_time_ns(self) -> int:
        return int(self.device_time_us) * 1000

    @property
    def orientation(self):
        raise NotImplementedError(_NO_ORIENTATION)

    @property
    def accel_frame(self) -> Tuple[float, float, float]:
        """Acceleration in the landmark frame rather than the sensor's own.

        Safe to use: the rotation was measured on two boards, not inferred. See
        `R_FRAME_FROM_IMU`.
        """
        return tuple(float(x) for x in R_FRAME_FROM_IMU @ np.asarray(self.accel, dtype=np.float32))

    @property
    def gyro_frame(self) -> Tuple[float, float, float]:
        """Angular rate in the landmark frame. Same rotation as `accel_frame`."""
        return tuple(float(x) for x in R_FRAME_FROM_IMU @ np.asarray(self.gyro, dtype=np.float32))


@dataclass(frozen=True)
class MagSample:
    """Magnetometer, in its own sensing frame.

    +/-4 gauss at 6842 LSB/gauss (LIS3MDL, DocID024204 Rev 4, Table 3). Note the
    magnetometer is on F.Cu and the IMU on B.Cu, so their Z axes oppose: fusing the
    two raw triads without correcting for that yields a confident wrong heading.
    """

    seq: int
    t_us: int
    host_t: float
    field: Tuple[float, float, float]  # gauss
    dropped: int = 0
    raw: Optional[Tuple[int, int, int]] = None
    device_time_us: Optional[int] = None
    host_t_ns: Optional[int] = None
    host_received_ns: Optional[int] = None

    def __post_init__(self) -> None:
        _validate_wire_header(self.seq, self.t_us)
        if self.device_time_us is None:
            object.__setattr__(self, "device_time_us", int(self.t_us) & 0xFFFFFFFF)
        if self.host_t_ns is None:
            object.__setattr__(self, "host_t_ns", int(round(self.host_t * 1_000_000_000)))
        if self.host_received_ns is None:
            object.__setattr__(self, "host_received_ns", self.host_t_ns)

    @property
    def device_time_ns(self) -> int:
        return int(self.device_time_us) * 1000

    @property
    def magnitude(self) -> float:
        """Field strength in gauss. Earth's is roughly 0.5 G, so this is a cheap
        sanity check that the scale factor and the part are both right."""
        x, y, z = self.field
        return float((x * x + y * y + z * z) ** 0.5)


def counts_to_grid(counts) -> np.ndarray:
    """Wire-order list of 80 -> `(5, 4, 4)` uint16."""
    arr = np.asarray(counts, dtype=np.uint16)
    if arr.size != TAXELS:
        raise ValueError(f"expected {TAXELS} taxels, got {arr.size}")
    return arr.reshape(SHAPE)


def _validate_wire_header(seq: int, t_us: int) -> None:
    if not 0 <= int(seq) <= 0xFFFFFFFF:
        raise ValueError("seq must be a raw unsigned 32-bit value")
    if not 0 <= int(t_us) <= 0xFFFFFFFF:
        raise ValueError("t_us must be a raw unsigned 32-bit value")
