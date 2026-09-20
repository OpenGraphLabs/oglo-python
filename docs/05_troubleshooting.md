# Troubleshooting

Start with:

```bash
oglo doctor
```

It checks the connection, device health, and delivered sample rate. If a USB
command fails or writes only part of its message, close the connection and reconnect.

## No glove found

1. Try a USB data cable. A charge-only cable can power the glove without carrying data.
2. Close other programs using the glove's serial port.
3. Check that the board runs supported OGLO firmware.

Supported firmware identifies itself as `OGLO` from `OpenGraphLabs`. An older
`XIAO_ESP32S3` / `Espressif Systems` build needs an update. The SDK also checks
`GET CONFIG`, so another board with the same USB identifier is not accepted as a glove.

## Port already held

A `PortBusyError` means another process owns the glove. Close the viewer,
notebook kernel, or Python session named in the error, then reconnect.

## The board answers nothing at all

For custom serial clients, set **DTR high and RTS low**. TinyUSB requires DTR
before transmitting. Setting both high can request a reset on USB-UART bridges.
The SDK handles this for connections it opens.

## Everything reads about 550 and nothing is pressed

You are probably reading RAW values, which include a baseline offset.
Use `glove.clean(threshold=30)` to apply the stored baseline.
If `glove.info.zero_valid` is false, first follow the
[calibration guide](03_calibration.md).

`frame.residual` works only in CLEAN mode. A stored baseline alone does not make
it available in RAW mode.

## Making a fist lights up every taxel

A still-hand calibration may be treating finger bending as contact. Recalibrate
while opening and closing your hand, without touching anything:

```python
glove.zero(sweep=5)
```

This replaces the saved baseline and requires USB. See [calibration](03_calibration.md).

## The rate is lower than 250 Hz

`doctor` compares delivered and expected rates and fails below about 85%.
Check for:

- Another program reading the port.
- An unpowered or overloaded USB hub.
- A busy host computer.
- A slow application loop. `overflow_*` counters indicate SDK queue overflow.

## BLE delivers a fraction of what it should

BLE delivery depends on the host and radio environment. Try one advertising glove,
restart Bluetooth on the host, and stop unused USB streaming with `glove.close()`.
Use USB for captures that need reliable rate or timing.

To isolate an SDK problem, compare with a raw Bleak subscription to
`4652535f-424c-4500-0001-000000000001` on the same machine. If both are slow, the
limit is below the SDK.

## The IMU is slower over BLE than over USB

BLE carries one IMU reading per tactile sample. It does not deliver the independent
USB IMU stream, and magnetic values may repeat. This is expected for the BLE format.

## `rates(imu=400)` is refused

IMU delivery uses whole-millisecond periods: rates are `1000 / n`, such as 1000,
500, about 333, and 250 packets/s. A 400 Hz request cannot be represented exactly,
so the SDK rejects it and names nearby choices.

`rates(mag=...)` is also refused. Firmware schedules magnetometer reads through
IMU loop cycles; it does not expose an independent magnetic-rate setting.

## `frame.orientation` raises

The SDK does not provide a fused orientation. Use `accel_frame` and `gyro_frame`
for measured board axes. Reliable heading requires separate magnetometer axis
and magnetic-interference calibration; see [sensor axes](02_data_reference.md#sensor-axes).

## Two-hand connection is refused

`connect_pair()` requires opposite sides and distinct configured serials.
Check the reported identities, then correct `SET SERIAL` or `SET SIDE` on the
misconfigured glove. For example, `glove.send("SET SIDE left")` changes its side.

## Recording reports missing raw values or calibration

Backend motion channels require the original integer IMU/magnetometer values.
The recorder cannot reconstruct them from scaled values.

RAW tactile also needs a valid stored zero recipe to create the CLEAN stream.
Follow the [calibration guide](03_calibration.md), then record again.
Failed captures remain available for diagnosis with `complete: false`.

## Something else

`glove.send("...")` sends a firmware command directly, including `DIAG` commands.
For a bug report, include the SDK/firmware versions, host OS, `doctor` output, and
steps to reproduce it. Remove private device identifiers and recordings first.
