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

Supported firmware identifies itself as `OGLO` from `OpenGraphLabs`. The SDK also
checks `GET CONFIG`, so another board with the same USB identifier is not accepted as
a glove. If the glove is on firmware the SDK will not update automatically, or shows up
as `303a:1001`, see the next section.

## Glove shows as `303a:1001`, or its firmware is too old to update

`lsusb` (Linux) or System Information (macOS) listing the glove as
`303a:1001 Espressif USB JTAG/serial debug unit` means it is sitting in the ESP32-S3
bootloader instead of running its firmware, so the SDK cannot see it. Automatic updates
also only start from 0.9.16, 0.9.17 or 0.9.18, so an older glove (for example 0.9.12)
is never updated by `connect()`.

Both are fixed by one command. Plug in **only the glove to restore**, then:

```bash
python -m pip install "oglo[recover]"   # adds esptool 4.5.1, once
oglo recover
```

It writes the current signed firmware the same way the factory does, except that it
never writes the area holding the glove's serial, hand, pairing and calibration, so
those survive. A running glove is checked first: the command refuses any hardware other
than `RDR02_FLEX5_REV_D_TIA` and does nothing if the glove is already current. A glove
in the bootloader cannot be asked, so the command asks you to type `RECOVER` before
writing.

When it says so, **unplug the glove, wait three seconds and plug it back in** (switch it
off as well if it has a power switch). A reset alone does not leave the bootloader; only
removing power does. The command then waits for the glove, reads it again and reports
the new firmware version. `oglo doctor` names any glove it sees in the bootloader.

If the glove still shows as `303a:1001` after the replug, contact OpenGraph Labs; that
is a hardware fault no software step repairs. On Linux, a permission error on
`/dev/ttyACM0` means your user needs the `dialout` group
(`sudo usermod -aG dialout $USER`, then log out and back in).

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
