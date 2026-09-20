# Contributing

Thank you for improving the OGLO Python SDK.

[`OpenGraphLabs/oglo-python`](https://github.com/OpenGraphLabs/oglo-python) is the
sole canonical repository. Fork and branch from its `main`; do not use a private,
staging, or pre-public repository as an upstream.

## Set up

```bash
git clone https://github.com/OpenGraphLabs/oglo-python.git
cd oglo-python
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

The default suite is hardware-free. It must remain safe to run without a glove and
must not mutate attached devices.

To include camera capture, video decoding, alignment, and the simulated OVISION
adapter tests, use Python 3.12+ and install the example dependencies:

```bash
python -m pip install -r examples/camera_glove/requirements-ovision.txt
python -m pytest tests/test_camera_glove.py
```

The dedicated Linux CI camera job runs these tests and rejects skipped tests.
After all test jobs pass, the package job uploads a commit-specific handoff with
the wheel, source/examples archive, source commit, and checksums. See the
[candidate guide](docs/08_candidate_status.md#evaluate-the-package).

## Pull requests

- keep protocol decoders pure and fail closed on malformed or unknown data
- add focused tests for behavioral changes
- preserve raw device values, timestamps, and loss information instead of silently
  normalising or interpolating them
- update public documentation and `CHANGELOG.md` for user-facing changes
- do not commit recordings, real device identifiers, credentials, local coverage
  databases, or operating-system metadata

Hardware-specific changes should include the firmware revision, schema, host OS,
test duration, and before/after loss counters. Do not present an automated test as
physical validation unless a physical glove was actually exercised. Live-device
qualification requires firmware 0.9.10 or newer with schema 6. Checked-in wire
vectors must come from that same supported contract and redact real device serials.

## Hardware tests

With exactly one left/right USB pair attached:

```bash
python3 -m pytest -m hardware --hardware-seconds 5
```

For exactly one attached glove, add `--hardware-single`; the three tests requiring
both hands are reported as skipped. RAW and CLEAN starting modes are both supported.

State-changing checks require a separate explicit flag and restore the settings they
change:

```bash
python3 -m pytest -m hardware_mutation --hardware-mutations
```

The same `--hardware-single` option applies to the mutation test. Its cleanup
restores the original threshold even when the starting mode was RAW.

Neither command performs a physical zero sweep. See
[`docs/03_calibration.md`](docs/03_calibration.md).

By submitting a contribution, you agree that it is licensed under Apache-2.0.
