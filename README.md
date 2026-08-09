# OGLO Python SDK

[![CI](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Firmware 0.9.10](https://img.shields.io/badge/firmware-0.9.10-5C2D91.svg)](docs/06_compatibility.md)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Python access to the OGLO five-finger tactile glove: 80 taxels per hand at a
nominal 250 Hz over USB, plus accelerometer, gyroscope, and optional magnetometer
streams.

> **Release candidate:** `0.1.0rc2` is a USB-first research SDK for the supported
> live-glove baseline, firmware 0.9.10/schema 6. The decoder retains historical
> 0.9.9/schema-6 vector compatibility, but 0.9.9 is not a deployment target. BLE
> is available as an experimental transport and is not release-qualified.

This public repository is the sole canonical source for the SDK. Development,
issues, pull requests, tags, and releases all belong under
[`OpenGraphLabs/oglo-python`](https://github.com/OpenGraphLabs/oglo-python); no
private or staging repository is an active upstream.

## What is included

- single- and dual-glove discovery with verified logical identity and side
- typed tactile, IMU, and magnetometer samples
- device and host timestamps, sequence gaps, and transport-health counters
- bounded-memory recording and hardware-free replay
- explicit calibration and stream controls
- `oglo doctor` for connection, rate, and integrity checks
- pure decoders, captured golden vectors, and hardware opt-in tests

## Install

Download the wheel from the matching [GitHub Release](https://github.com/OpenGraphLabs/oglo-python/releases), then install it locally:

```bash
python3 -m pip install ./oglo-0.1.0rc2-py3-none-any.whl
```

To install the tagged source instead:

```bash
python3 -m pip install \
  "oglo @ git+https://github.com/OpenGraphLabs/oglo-python.git@v0.1.0rc2"
```

Python 3.10 or newer is required.

## Check the glove first

Connect one or two gloves over USB-C and run:

```bash
oglo doctor
```

`doctor` measures the attached device and host rather than assuming the nominal
rates. Resolve any reported identity, firmware, loss, or throughput failure before
recording data. Upgrade any live glove that does not report firmware 0.9.10 and
schema 6.

## Read one glove

This first example is read-only: it does not change calibration or stream settings.

```python
from itertools import islice

import oglo

with oglo.connect() as glove:
    print(glove.info.serial, glove.info.side, glove.info.fw_rev)

    for frame in islice(glove.tactile(), 10):
        print(frame.seq, frame.counts.shape, int(frame.counts.max()))
```

`frame.counts` has shape `(5, 4, 4)` and contains 12-bit ADC counts, not force.
There is no Newton conversion in this release.

## Use two hands

```python
left, right = oglo.connect_pair()
try:
    print(left.info.serial, right.info.serial)
finally:
    left.close()
    right.close()
```

The devices must report opposite sides and distinct logical serials. Samples from
two gloves are not hardware-synchronised; see the [two-hand
example](examples/04_two_hands.py) before aligning a dataset.

## Calibration changes device state

`glove.zero(sweep=5)`, `glove.clean(...)`, `glove.raw()`, and `glove.rates(...)`
change device state. Do not put them in a first-connect smoke test. Read the
[calibration guide](docs/03_calibration.md) and wear the glove through its full
motion range before capturing a new zero.

## Documentation

- [Quickstart](docs/01_quickstart.md)
- [Compatibility and validation scope](docs/06_compatibility.md)
- [Data reference](docs/02_data_reference.md)
- [Calibration](docs/03_calibration.md)
- [Recording and replay](docs/04_recording.md)
- [Troubleshooting](docs/05_troubleshooting.md)
- [Test your own glove pair](docs/07_acceptance.md)

## Test the complete physical pair

The owner-facing acceptance runner exercises the installed SDK's public API and
writes a Markdown/JSON evidence bundle. Its default is read-only with respect to
device calibration and settings:

```bash
oglo acceptance
```

Add `--interactive` for guided finger/IMU actions, `--mutations` for reversible
RAW/CLEAN/rate changes, `--zero` to deliberately replace calibration, or
`--soak 75m` for the two-hand device-clock rollover gate. See the
[acceptance guide](docs/07_acceptance.md) before enabling state-changing options.

## Development

```bash
git clone https://github.com/OpenGraphLabs/oglo-python.git
cd oglo-python
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

The default test suite never opens hardware. With exactly one left/right USB pair
attached, the opt-in integration tests exercise every stream, repeated reconnects,
two-hand capture, recording, replay, and diagnostics:

```bash
python3 -m pytest -m hardware --hardware-seconds 5
python3 -m pytest -m hardware_mutation --hardware-mutations
```

The mutation suite restores stream settings but deliberately does not run
`zero()`, because a valid sweep needs a person wearing the glove and overwrites the
stored calibration.

## License and security

Licensed under [Apache-2.0](LICENSE). Please report security issues according to
[SECURITY.md](SECURITY.md), not through a public issue containing sensitive details.
