# Contributing

Use [OpenGraphLabs/oglo-python](https://github.com/OpenGraphLabs/oglo-python) as
the source repository. Branch from `main` or fork it.

## Set up

```bash
git clone https://github.com/OpenGraphLabs/oglo-python.git
cd oglo-python
python3 -m venv .venv
```

Activate `.venv` with `source .venv/bin/activate` on macOS/Linux, or
`.venv\Scripts\Activate.ps1` in Windows PowerShell. Then:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

The default tests must work without a glove and must not change attached devices.

## Camera tests

Use Python 3.12+ to include simulated webcam and OVISION tests:

```bash
python -m pip install -r examples/camera_glove/requirements-ovision.txt
python -m pytest tests/test_camera_glove.py tests/test_collect.py tests/test_dataset.py tests/test_realsense.py
```

These tests exercise simulated devices, real video encoding/decoding, data
alignment, the multi-episode collector, the dataset index / upload commands and
the RealSense worker on a fake `pyrealsense2` (any OS; no RealSense needed).
The `--codec` test needs the system `ffmpeg`; the OVISION tests run the SDK's native
worker on a simulated stream and need Linux. The Linux camera CI job installs both
and requires all four files to run without skips.

## Hardware tests

These commands use attached devices. Firmware must be 0.9.10+ with schema 6.

| Attached hardware | Command |
| --- | --- |
| Exactly one left/right USB pair | `python -m pytest -m hardware --hardware-seconds 5` |
| Exactly one glove | Add `--hardware-single` to that command |

One-glove runs skip the three checks that require a pair. Both RAW and CLEAN
starting modes are supported.

To test setting changes:

```bash
python -m pytest -m hardware_mutation --hardware-mutations
```

Add `--hardware-single` for one glove. The tests attempt to restore settings,
including the original threshold in RAW mode. They do not perform a physical
zero sweep; see [calibration](docs/03_calibration.md).

## Before a pull request

- Keep packet decoders independent of device I/O and reject malformed data.
- Add focused tests for behavior changes.
- Preserve original values, clocks, and loss information.
- Update the affected guides and `CHANGELOG.md` for user-facing changes.
- Exclude recordings, real device identifiers, credentials, coverage files, and
  operating-system files from commits.

For hardware changes, report firmware/schema, host OS, duration, and loss counters
before and after testing. Clearly distinguish simulated tests from physical tests.
Test packets captured from hardware must use the supported protocol and redact
real serial numbers.

After CI passes, its package job provides a wheel, matching source/examples,
commit details, and checksums. See [package evaluation](docs/08_candidate_status.md#evaluate-the-package).

Contributions are licensed under Apache-2.0.
