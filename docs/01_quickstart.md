# Quickstart

## Install a release

Download the wheel from the matching
[GitHub Release](https://github.com/OpenGraphLabs/oglo-python/releases), then:

```bash
python3 -m pip install ./oglo-0.1.0rc3-py3-none-any.whl
```

Or install the immutable source tag:

```bash
python3 -m pip install \
  "oglo @ git+https://github.com/OpenGraphLabs/oglo-python.git@v0.1.0rc3"
```

Python 3.10 or newer is required. Supported live gloves run firmware 0.9.10 or
newer with schema 6. The current golden firmware for new flashes is 0.9.11;
deployed 0.9.10 gloves remain supported. `0.1.0rc3` rejects older firmware in both
live connections and recorded episodes.

## Diagnose before collecting data

Connect a glove over USB-C and run:

```bash
oglo doctor
```

That measures identity, firmware, stream delivery, sequence gaps, malformed data,
and host-side overflow. Resolve a failure before relying on a recording.

## Read ten frames without changing the glove

```python
from itertools import islice

import oglo

with oglo.connect() as glove:
    print(glove.info.serial, glove.info.side, glove.info.fw_rev)

    for frame in islice(glove.tactile(), 10):
        print(frame.seq, frame.counts.shape, int(frame.counts.max()))
```

`frame.counts` is a `(5, 4, 4)` array of raw or device-cleaned ADC counts,
depending on the glove's current stream setting. It is not force.

To select one of several gloves, use the logical serial stored in device CONFIG:

```python
glove = oglo.connect(serial="OGLO-L-TEST01")
```

That is not the USB descriptor serial, port path, BLE address, or advertisement
name. When an explicit `port=` is supplied, the SDK still reads CONFIG and refuses
to return a device whose logical serial does not match.

## Calibration is an explicit state change

Do not run this merely to check that installation worked:

```python
glove.zero(sweep=5)       # overwrites the stored per-taxel zero
glove.clean(threshold=30)
```

Wear the glove, open and close the hand through its full motion range for the five
seconds, and touch nothing. Bending a finger presses the sensor by itself, so a
still-hand baseline creates false contacts during later motion.

The SDK verifies the active recipe immediately after capture. Supported firmware does
not expose enough information to prove that the flash write survived a power cycle;
reboot and read it back when persistence is a release or factory gate.
See [Calibration](03_calibration.md) before changing an externally supplied glove.

## Two hands

```python
left, right = oglo.connect_pair()
try:
    print(left.info.serial, right.info.serial)
finally:
    left.close()
    right.close()
```

Which device is left or right comes from CONFIG, not cable order. The devices must
report opposite sides and distinct logical serials.

Relate two hands on `host_t`, never on `t_us`. Each glove has an independent device
clock and there is no hardware synchronisation contract. Read each hand on its own
thread so one stream does not throttle the other; see
[`examples/04_two_hands.py`](../examples/04_two_hands.py).

## BLE is experimental

```python
glove = oglo.connect(transport="ble")
```

BLE uses the same tactile schema, but notification throughput depends on the host,
antenna, and radio environment. It does not deliver the independent USB IMU packet
cadence, and sweep zero is USB-only. Use USB whenever rate, timing, or release
qualification matters.

## Where to next

| Question | Document |
| --- | --- |
| What is currently supported? | [Compatibility](06_compatibility.md) |
| What do the numbers mean? | [Data reference](02_data_reference.md) |
| How do zero and thresholds work? | [Calibration](03_calibration.md) |
| How do I save and replay data? | [Recording and replay](04_recording.md) |
| What should I do when something fails? | [Troubleshooting](05_troubleshooting.md) |
