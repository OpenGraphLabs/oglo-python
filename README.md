# OGLO Python SDK

Read touch and motion data from OGLO gloves. Record it, replay it, or capture it
alongside webcam or OVISION video.

> This is the `0.1.0rc8.dev2` development candidate. Managed firmware and
> JSONL recording are still under physical qualification; fleet rollout is not approved.
> See [managed updates](docs/10_managed_firmware.md).

Requires Python 3.10+, firmware 0.9.10+ with schema 6, and a USB data cable.

## Install

For automatic USB firmware updates on macOS/Linux, run this in the Python
environment used by your collection program:

```sh
curl -fsSL https://github.com/OpenGraphLabs/oglo-python/releases/download/v0.1.0rc8.dev2/install.py | python - --auto-firmware
```

The same SDK serves every lab. Signed firmware is included; no glove list,
separate firmware file or configuration editing is needed. Installation does not
flash devices; compatible gloves are prepared at the next `connect()` or
`connect_pair()`. See [compatibility and disabling updates](docs/10_managed_firmware.md).

From this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
For a packaged build, use the [download guide](docs/08_candidate_status.md#evaluate-the-package).

## Quick start: record with OGLO Studio

Use Studio for a guided recording with **two USB OGLO gloves (left and right)** and
one camera on the same computer. Plug them in with USB data cables and close any
other app using the gloves or camera, including OGLO Viewer.

From this repository, with the virtual environment activated:

```bash
python -m pip install -e '.[studio]'
oglo doctor
oglo studio --output ./captures/studio
```

If `oglo doctor` reports a failure, fix the connection before recording.

Open **http://127.0.0.1:8765/**. If that port is in use, start Studio with
`oglo studio --port 8766 --output ./captures/studio` and open
`http://127.0.0.1:8766/` instead. Studio is a Python app; no `npm run dev` is
needed. Leave the terminal running while you record, then press Ctrl+C to stop it.

Follow the five steps in the page:

1. **Connect and check.** Select the camera and each glove, then click **Connect
   selected devices**. A USB camera such as OVISION may appear under a generic
   UVC name. Confirm the correct view appears, both glove frame counters advance,
   and a brief fingertip touch lights up each hand's taxels.
2. **Calibrate.** Choose **Use saved calibration** if it still matches the glove
   fit, or run a fresh five-second sweep. For a fresh sweep, repeatedly open and
   close both hands without touching anything, including your own fingertips.
   Studio shows a countdown, verifies both baselines, and displays the taxel
   spread and contact threshold. Check fingertip response before continuing.
3. **Set up a button (optional).** Test a keyboard-style USB pedal and map its
   keys, or continue with the on-screen controls.
4. **Record and review.** Enter a task description, click **Start recording**,
   perform the task, and click **Stop**. Wait for validation, review the video,
   then choose **Keep** or **Discard**. Repeat for another take if needed.
5. **Export.** Click **Export kept takes**, then **Download dataset ZIP**. The ZIP
   contains the video, camera timestamps, both gloves' sensor data, calibration,
   and checksums. The source episodes remain under `./captures/studio/`, and the
   exported ZIP is also saved in `./captures/studio/exports/`.

Studio's built-in camera path records host-timed video, including when an
OVISION camera is selected as a USB camera. This produces a **source archive**;
native OVISION exposure timestamps and stereo metadata require the separate
[OVISION capture path](examples/camera_glove/OVISION.md) on Linux with Python
3.12+. See the [Studio guide](docs/10_studio.md) for pedal setup, data checks,
and delivery limits.

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

For repeated camera and glove episodes, use the Studio walkthrough above.

## More

[Calibration](docs/03_calibration.md) · [Troubleshooting](docs/05_troubleshooting.md) ·
[All guides](docs/README.md) · [Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md) · [License](LICENSE)
