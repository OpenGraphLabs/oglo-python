# Data reference

## The three streams

Over USB each tagged stream has its own rate, sequence number and timestamp. BLE
schema 6 instead carries tactile, IMU and optional mag in one outer packet sequence;
the SDK restores the signed IMU capture-time offset but cannot invent independent
sensor sequence numbers that are not on the wire.

| Stream | Rate over USB | Yields |
| --- | --- | --- |
| `g.tactile()` | 250 Hz | `Frame` |
| `g.imu()` | about 500 packets/s | `ImuSample` |
| `g.mag()` | 125 Hz | `MagSample` |

Different packet rates are normal, not a quirk. Every sensor has its own physics,
and forcing a common rate either fabricates data for the slow one or discards it from
the fast one.

The IMU packet cadence is not the physical sensor ODR. Firmware configures the
accelerometer/gyroscope at 200 Hz but polls/emits its latest value on a nominal 2 ms
schedule, so adjacent 500-packet/s records may contain the same physical measurement.

The supported live contract is firmware 0.9.10/schema 6. `0.1.0rc2` retains parser
tolerance for historical 0.9.9/schema-6 vectors and recordings, but live collection
must use 0.9.10. Other schemas and older firmware fail closed instead of inviting a
best-effort packet guess.

## Identity and side

`g.info.serial` is the logical glove serial reported by CONFIG. It is distinct from
the USB chip/descriptor serial and from a BLE address or advertisement name.
`oglo.connect(serial=...)` matches this logical value and verifies it after opening a
specific `port=` or BLE address.

`g.info.side` chooses left versus right. `connect_pair()` requires one left glove,
one right glove, and distinct logical serials.

`g.info.has_mag` means firmware successfully initialised the magnetometer at boot.
Firmware 0.9.10 cannot distinguish an intentionally absent part from one that failed
boot detection, and it has no runtime read-failure/freshness counter. Therefore a
clean status snapshot is not proof that every magnetometer value is fresh; applications
that require heading-quality data need a firmware freshness flag and a physical field
sanity test.

### Over BLE

BLE carries one IMU and one magnetometer reading per tactile sample, so those two
arrive at the tactile rate rather than their own, and the magnetometer repeats.

**BLE throughput is not something to assume from the packet format.** Notifications
can arrive below their nominal cadence because of the host and radio link. Measure
the actual setup with `oglo doctor`; use USB for a capture whose rate or timing
matters.

## `Frame`

| Field | Meaning |
| --- | --- |
| `counts` | `(5, 4, 4)` uint16, **raw 12-bit ADC, not force** |
| `residual` | counts above the zero baseline, float32 |
| `seq` | per-stream sample number; a gap is loss |
| `t_us` | raw device u32 microseconds; wraps about every 71.6 minutes |
| `device_time_us` | the same clock unwrapped to a continuous 64-bit timeline |
| `host_t` / `host_t_ns` | host monotonic time at the USB-read/BLE-notify boundary, in seconds/nanoseconds |
| `host_received_ns` | the same observed receive boundary, kept explicitly in recordings |
| `dropped` | samples missing since the previous frame |

`counts` is indexed `[finger][row][col]`. **Finger order comes from
`g.info.channels`, per hand.** The left hand is reversed:

```
right  ['thumb', 'index', 'middle', 'ring', 'pinky']
left   ['pinky', 'ring', 'middle', 'index', 'thumb']
```

A hardcoded list mislabels every left-hand dataset, and the numbers look perfectly
fine while it happens.

**An untouched taxel reads around 550, not 0.** Use `residual`, or turn on a clean
stream. See [calibration](03_calibration.md).

There is no newtons conversion. Nobody has run the calibration that would produce
one, and inventing a factor would be worse than not having it.

## `ImuSample` and `MagSample`

| Field | Unit | Full scale |
| --- | --- | --- |
| `accel` | g | +/-8 g, 4096 LSB/g |
| `gyro` | deg/s | +/-2000 deg/s, 16.4 LSB/(deg/s) |
| `field` | gauss | +/-4 gauss, 6842 LSB/gauss |

Datasheet-confirmed against the ranges the firmware actually programs: ICM-42688-P
DS-000347 Rev 1.2, LIS3MDL DocID024204 Rev 4. `MagSample.magnitude` should read
around 0.5 G outdoors, which is a cheap check that nothing is scaled wrong.

### Sensor axes

`accel` and `gyro` are in the IMU's own frame. To get them into a frame you can point
at, use `accel_frame` and `gyro_frame`:

```
+Z   out of the face the XIAO module is on
+X   toward the USB-C connector
+Y   +Z cross +X
```

The rotation is **measured, not derived**: six gravity poses on two boards
(`OGLO-R-TEST04` and `OGLO-L-TEST01`) gave identical matrices.

```
frame X = +sensor y     frame Y = +sensor x     frame Z = -sensor z
```

Reproduce it with `python3 tools/measure_axes.py`.

**The magnetometer axes are not known.** The same procedure gave different answers on
the two boards, and `|B|` swung between 0.70 and 1.34 G across poses when Earth's field
is a constant ~0.5 G, so the readings were contaminated by something local. Treat
`field` as being in the sensor's own unknown frame.

**There is no fused orientation.** `frame.orientation` raises. Roll and pitch would be
available from the accelerometer, but heading needs the magnetometer, and its axes are
exactly the part that is not measured. A quaternion built on a guessed axis looks
plausible and is wrong, which is the worst way to be wrong.

## Loss and sequence anomalies are never merged

| Where | Meaning |
| --- | --- |
| `frame.dropped` | end-to-end sequence gap; device queue and transport loss can both contribute |
| `g.dropped["overflow_*"]` | **we** discarded it, because nobody was reading that stream |
| `g.dropped["duplicate_*"]`, `g.dropped["backward_*"]` | anomalies, never miscounted as billions of drops |
| `g.dropped["transport_malformed_usb"]` | a USB `TAG` magic was followed by an impossible type/length header |
| `g.dropped["transport_malformed_ble"]` | a BLE notification could not satisfy the schema-6 packet contract |
| `g.status().tag_dropped` | device queue-drop snapshot |

They need different fixes, so they are reported separately. Overflow in particular is
not a fault: iterate `tactile()` and ignore `imu()` and the IMU queue fills and drops,
by design, rather than growing without limit.

`g.info.device_dropped` is only a connect/config snapshot and may be zero on firmware
that exposes the counter only through status. `oglo.record()` stores start/end status
and a capture-window delta.

## Timestamps

`t_us` is the raw 32-bit device counter. Use `device_time_us` to order samples and
measure spacing within one glove across rollover; both are **meaningless across two
gloves**. The unwrapped value deliberately starts with one spare 32-bit epoch so an
older IMU packet arriving just after a tactile rollover can still be represented
without a negative integer. Its absolute number is therefore arbitrary; use ordering
and differences, not its origin.

`host_t`, `host_t_ns` and `host_received_ns` mark the observed transport receive
boundary. The SDK does not move samples backwards from that boundary using device
time, because buffered reads can make such estimates run backwards. Every sample
decoded from one USB read or BLE notification can therefore share the same host
timestamp. Use the integer nanosecond fields when float precision matters.

Host timestamps relate arrival events on one computer, but they are not a
hardware-sync proof or exact sensor-capture times: USB/BLE buffering and host
scheduling still contribute unknown delay.

Recordings store both, plus a wall-clock anchor, because each answers a question the
others cannot.

## Integrity limit of firmware 0.9.10

The 0.9.10 tagged USB frame has a magic and length but no checksum/CRC. The TinyUSB
whole-frame queue removes the known pre-0.9.9 truncation path, but the SDK cannot
mathematically prove that every plausible payload bit is intact. A future protocol
needs framed CRC protection for that guarantee.
