# Data reference

OGLO provides touch, acceleration, rotation, and magnetic measurements. For saved
file layouts, see the [data specification](09_data_specification.md).

## The three streams

| Python method | Data | Default USB delivery |
| --- | --- | --- |
| `glove.tactile()` | Touch values in a `Frame` | About 250 packets/s |
| `glove.imu()` | Acceleration and rotation in an `ImuSample` | About 500 packets/s |
| `glove.mag()` | Magnetic field in a `MagSample` | About 125 packets/s, when available |

Each USB stream has its own sequence numbers and device timestamps. Keep the
streams separate: their sample counts normally differ.

The IMU sensor itself measures at 200 Hz. Firmware sends its latest value about
500 times per second, so adjacent packets can contain the same measurement.

### Over BLE

BLE packets combine tactile, IMU, and optional magnetic data under one packet
sequence. IMU and magnetic values arrive at the tactile rate; values may repeat.
The SDK preserves the IMU capture-time offset carried by the packet.

Actual delivery depends on the host and radio link. Use USB when rate or timing
matters, and measure delivery with `oglo doctor`.

## Identity and side

| Field | Meaning |
| --- | --- |
| `glove.info.serial` | Configured glove serial; use this with `connect(serial=...)` |
| `glove.info.side` | `left` or `right` |
| `glove.info.channels` | Finger names in the order sent by this glove |
| `glove.info.has_mag` | Magnetometer was detected at boot |

The configured serial differs from a USB descriptor serial or BLE address.
`connect_pair()` requires one left glove, one right glove, and distinct serials.

`has_mag` cannot distinguish an absent magnetometer from one that failed boot
setup. Firmware also lacks a counter for stale magnetic readings. A healthy
status alone does not prove that every magnetic value is fresh.

## `Frame`

| Field | Meaning |
| --- | --- |
| `counts` | `(5, 4, 4)` uint16 touch values: RAW ADC or firmware CLEAN, depending on mode |
| `residual` | CLEAN values as float32; raises in RAW mode |
| `seq` | Sequence number; gaps indicate missing samples |
| `t_us` | Raw 32-bit device time in microseconds; wraps about every 71.6 minutes |
| `device_time_us` | Device time extended across rollover |
| `host_t` / `host_t_ns` | Host arrival time in seconds / integer nanoseconds |
| `host_received_ns` | The same arrival time, named explicitly in recordings |
| `dropped` | Missing samples since the previous accepted sequence |

Values are **ADC counts, not force in newtons**. No force conversion is available.
RAW values can be around 550 even with nothing pressed. See
[calibration](03_calibration.md) for interpreting RAW and CLEAN data.

### Finger order

`counts[finger][row][column]` follows `glove.info.channels`. Typical orders are:

```text
right: thumb, index, middle, ring, pinky
left:  pinky, ring, middle, index, thumb
```

Read the reported order rather than hardcoding it.

### One physical layout for both hands: `oriented_counts`

```python
from oglo import oriented_counts

physical = oriented_counts(frame.counts, glove.info.channels, glove.info.side)
# Equivalent: physical = frame.oriented(glove.info)
```

The result is `(5, 4, 4)`, ordered thumb to pinky, with column 0 at every fingertip.
It leaves the original `frame.counts` unchanged.

The function flips the left thumb's columns. Rows remain as scanned because
their physical direction is unverified.

## `ImuSample` and `MagSample`

| Field | Unit | Full scale | Raw scale |
| --- | --- | --- | --- |
| `accel` | g | ±8 g | 4096 integer counts/g |
| `gyro` | degrees/second | ±2000 degrees/second | 16.4 integer counts per degree/second |
| `field` | gauss | ±4 gauss | 6842 integer counts/gauss |

### Sensor axes

`accel` and `gyro` use the IMU sensor's axes. Use `accel_frame` and `gyro_frame`
for the measured board axes:

```text
+X: toward the USB-C connector
+Z: out of the face carrying the XIAO module
+Y: +Z cross +X

board X = sensor y
board Y = sensor x
board Z = -sensor z
```

Magnetometer axes are unverified. `field` stays in the sensor's own axes.
The SDK provides no fused orientation; `frame.orientation` raises an error.

## Loss and sequence anomalies are never merged

| Counter | What happened |
| --- | --- |
| `frame.dropped` | A sequence gap; loss may be in the device queue or transport |
| `glove.dropped["overflow_*"]` | The SDK's queue filled because the application did not read it fast enough |
| `glove.dropped["duplicate_*"]` | A repeated sequence number |
| `glove.dropped["backward_*"]` | A sequence number moved backwards |
| `glove.dropped["transport_malformed_usb"]` | Invalid USB packet type or length |
| `glove.dropped["transport_malformed_ble"]` | Invalid BLE packet |
| `glove.status().tag_dropped` | Device queue-drop counter |

An ignored stream can fill its queue. For example, reading only tactile samples
may produce IMU overflow counts. This prevents the unused queue growing forever.

`glove.info.device_dropped` is a connection-time snapshot. Recording stores start
and end status plus their differences; use those to assess a capture.

## Timestamps

Use **device time within one glove** and **host arrival time across streams on one
computer**.

- `device_time_us` handles `t_us` rollover. Compare differences, not its arbitrary
  starting value.
- Samples from one USB read or BLE notification can share a host timestamp.
- Host time marks receipt, not the exact instant the sensor measured the value.
  USB/BLE buffering and host scheduling add delay.
- Separate devices have separate clocks. Separate computers also have separate
  monotonic clocks. Do not subtract these clocks to infer synchronization.

Use integer nanosecond fields to avoid losing precision. Recordings also keep
wall-clock times for identifying when a session happened.

## Integrity limit of supported firmware

USB packets lack a payload checksum. The SDK detects malformed packets and
sequence problems, but cannot detect every changed payload bit.

See [compatibility](06_compatibility.md) for supported versions and test coverage.
