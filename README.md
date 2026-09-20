# OGLO Python SDK

Read touch and motion data from OGLO gloves. Record it, replay it, or capture it
alongside webcam or OVISION video.

Requires Python 3.10+, firmware 0.9.10+ with schema 6, and a USB data cable.

## Install

From this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
oglo doctor
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
For a packaged build, use the [download guide](docs/08_candidate_status.md#evaluate-the-package).

## Read a glove

```python
from itertools import islice
import oglo

with oglo.connect() as glove:
    print(glove.info.side, glove.info.channels)
    for frame in islice(glove.tactile(), 10):
        print(frame.counts)
```

Touch data has shape `(5 fingers, 4 rows, 4 columns)`. Finger order comes from
`glove.info.channels`. Values are sensor counts, not force in newtons.

## Record and replay

```python
import oglo

path = oglo.record("out/", seconds=60)
episode = oglo.replay(path)
print(episode.summary())
```

Recording writes JSONL directly in the backend's sensor format. Replay reads
those same files. See [recording](docs/04_recording.md) for RAW/CLEAN handling.

## Record with a camera

Connect camera and gloves to the same computer:

- [USB webcam](examples/camera_glove/README.md)
- [OVISION v1](examples/camera_glove/OVISION.md) — Linux, Python 3.12+

Send the whole output folder. The camera guides explain how to check it.

## More

[Calibration](docs/03_calibration.md) · [Troubleshooting](docs/05_troubleshooting.md) ·
[All guides](docs/README.md) · [Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md) · [License](LICENSE)
