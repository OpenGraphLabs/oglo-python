# Troubleshooting

Run this first. It measures your machine rather than asking you to read a table.

```bash
oglo doctor
```

## Everything reads about 550 and nothing is pressed

Correct. Those are raw ADC counts and an untouched taxel sits near its idle offset,
not at zero. Use `f.residual`, or `g.clean(threshold=30)` to have the board do it.

If `residual` raises, the device has no zero yet. Run `g.zero(sweep=5)`.

## Making a fist lights up every taxel

The zero was captured with a still hand. Bending a finger presses the sensor by
itself, so a still-hand baseline is only valid for a still hand. Redo it:

```python
g.zero(sweep=5)     # open and close your hand for the whole five seconds
```

## No glove found

```
UsbError: no glove found. Is it plugged in and running OGLO firmware?
```

In order of likelihood:

1. **A charge-only USB-C cable.** It powers the board and enumerates nothing. Try a
   different cable first; this is the most common cause by a wide margin.
2. The board is not running OGLO firmware.
3. Something else already holds the port. `doctor` lists non-glove serial devices it
   saw and skipped, which is often the clue.

Supported firmware 0.9.10 or newer appears to the OS as `OGLO` from `OpenGraphLabs`. A glove
that still appears as `XIAO_ESP32S3` from `Espressif Systems` is running an older
build and must be upgraded. Discovery still proves identity with `GET CONFIG`; a
different XIAO using the same VID can briefly appear as a candidate, but it is
rejected when the handshake does not return the strict OGLO schema.

## Port already held

```
PortBusyError: /dev/cu.usbmodem... is already held by PID 1234
```

A USB glove has exactly **one** owner. A viewer, a notebook kernel or a stale session
still has it open. If you have used a Wuji glove this will surprise you: theirs is a
network device and serves several subscribers at once.

## The board answers nothing at all

If you are writing your own serial code rather than using this SDK: **assert DTR.**
Supported firmware uses TinyUSB, which will not transmit until the host raises DTR.
With DTR low the board returns literally zero bytes and looks dead. It is not.

Keep RTS low. The two together are what a USB-UART bridge decodes as a reset request.

## The rate is lower than 250 Hz

`doctor` reports delivered against expected as a percentage. Below ~85% it fails.

- another program reading the same port
- a USB hub, especially an unpowered one
- a machine under heavy load
- your own loop being slower than the stream, which shows up as
  `dropped["overflow_*"]` rather than as wire loss

## BLE delivers a fraction of what it should

BLE notification delivery can vary with the host and radio environment. Before
suspecting your code, compare the SDK with a raw bleak subscription on the same
machine. If both are slow, the bottleneck is below the SDK:

```python
# minimal: subscribe to 4652535f-424c-4500-0001-000000000001 and count
```

Things that have mattered:

- **more than one glove advertising.** Advertising costs airtime whether or not
  anything is connected to that board.
- **macOS Bluetooth in a bad state.** Toggling it off and on has recovered this.
- the board also streaming over USB to nothing. `g.close()` stops it; a crashed
  program does not.

**If a capture matters, use USB.** BLE throughput on this hardware is not something we
can currently promise a number for.

## The IMU is slower over BLE than over USB

Expected. BLE carries one IMU reading per tactile sample, so roughly half of the
500 Hz stream arrives and the magnetometer repeats. Tactile is unaffected.

**Use USB when IMU rate or timing matters.** This is the format, not a bug.

## `rates(imu=400)` is refused

The device sets the IMU by whole-millisecond period, so the reachable rates are
1000/n: 1000, 500, 333, 250. 400 Hz does not exist. Rounding it silently to 333 would
put a number in your dataset that you never chose, so it raises and names the nearest.

`rates(mag=...)` is refused for a different reason: firmware targets a roughly
125 Hz magnetometer cadence and expresses it in IMU loop cycles. It is not an
independent setpoint and is not generally one quarter of a custom IMU rate.

## `frame.orientation` raises

There is no fused orientation. Accelerometer/gyro axes were measured and are exposed
through `accel_frame`/`gyro_frame`; the magnetometer axes and magnetic environment are
not validated. A plausible wrong quaternion is worse than none.

Use `accel`, `gyro` and `field` only after independently calibrating the
magnetometer's axes and hard/soft-iron error. Mounting geometry alone is not enough
to establish a trustworthy heading.

## Two hands report the same side

```
UsbError: both gloves report the same side
```

Side is stored on the device. Fix it there:

```python
g.send("SET SIDE left")
```

## Two-hand connection is refused

`connect_pair()` refuses duplicate logical serials or two devices reporting the same
side. Correct `SET SERIAL` or `SET SIDE` on the affected glove before reconnecting.

## Something else

`g.send("...")` reaches any firmware command directly, including the `DIAG` family.
That is the escape hatch for everything this SDK does not wrap.
