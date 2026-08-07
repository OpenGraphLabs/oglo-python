# Contributing

Thank you for improving the OGLO Python SDK.

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
physical validation unless a physical glove was actually exercised.

## Hardware tests

With exactly one left/right USB pair attached:

```bash
python3 -m pytest -m hardware --hardware-seconds 5
```

State-changing checks require a separate explicit flag and restore the settings they
change:

```bash
python3 -m pytest -m hardware_mutation --hardware-mutations
```

Neither command performs a physical zero sweep. See
[`docs/03_calibration.md`](docs/03_calibration.md).

By submitting a contribution, you agree that it is licensed under Apache-2.0.
