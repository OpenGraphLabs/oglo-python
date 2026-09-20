# OGLO Python SDK

[![CI](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Connect to OGLO tactile gloves, read touch and motion data, and record or replay
sessions. Each hand has 80 taxels in a `(5, 4, 4)` array, with nominal USB rates of
250 tactile, 500 IMU, and 125 magnetometer packets/s when fitted.

> This checkout is `0.1.0rc4`. The team reports successful functionality testing
> on firmware 0.9.16, with the previously reported problems resolved. See
> [candidate status](docs/08_candidate_status.md) for package and validation details.

## Install

Requires Python 3.10+ and glove firmware 0.9.10+ with CONFIG schema 6.
Download the commit-specific candidate handoff using the
[package instructions](docs/08_candidate_status.md#evaluate-the-package), unpack it,
and verify its checksums. From that directory, install in a virtual environment:

```bash
python3 -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install ./oglo-0.1.0rc4-py3-none-any.whl
```

Keep `handoff.json` and `SHA256SUMS.txt` with your project to identify the exact SDK build.
Published packages are listed in [GitHub Releases](https://github.com/OpenGraphLabs/oglo-python/releases).

## Connect and read

Connect a glove over USB-C and check it:

```bash
oglo doctor
```

Resolve reported errors before recording. Then read ten frames without changing
calibration or stream settings:

```python
from itertools import islice

import oglo

with oglo.connect() as glove:
    print(glove.info.serial, glove.info.side, glove.info.fw_rev)
    for frame in islice(glove.tactile(), 10):
        print(frame.seq, frame.counts.shape, int(frame.counts.max()))
```

Counts are raw or device-cleaned ADC values depending on the current mode, not
force in Newtons. Finger order comes from `glove.info.channels`.

Select a glove with `oglo.connect(serial="OGLO-L-TEST01")`, using its logical
CONFIG serial rather than its USB descriptor serial. An explicit `port=` still
verifies the requested identity. BLE is available through
`oglo.connect(transport="ble")` but remains experimental; use USB for timing-sensitive work.

## Record and replay

```python
import oglo

episode_path = oglo.record("out/", seconds=60)
episode = oglo.replay(episode_path)
print(episode.summary())
```

Replay works without hardware. The [recording guide](docs/04_recording.md) covers
file contents, cancellation, and incomplete captures.

For two hands, `oglo.connect_pair()` returns `(left, right)` after verifying their
sides and distinct logical serials. Record each hand on its own thread; see the
[two-hand example](examples/04_two_hands.py). Device clocks are independent: host
timestamps provide approximate alignment, not hardware synchronisation.

## Collect data for OGLO post-processing

The [collection data specification](docs/09_data_specification.md) defines what
to collect, required fields, array shapes, units, and example records.
`from oglo import OGLData` provides the typed session-manifest contract.

Choose the example for your camera. In both cases, connect the
**camera and OGLO glove(s) to the same computer**, record them together, and send
the complete session folder for post-processing. The camera does not connect to
the glove.

| Case | Camera data preserved | Setup and example |
| --- | --- | --- |
| 1. USB webcam | RGB video and per-frame host timestamps | [Webcam guide](examples/camera_glove/README.md), [capture.py](examples/camera_glove/capture.py) |
| 2. OVISION v1 | Native H.264 stereo, exposure timestamps, camera IMU/magnetometer, per-unit calibration | [OVISION v1 guide](examples/camera_glove/OVISION.md), [ovision.py](examples/camera_glove/ovision.py) |

### Case 1: USB webcam

From a repository checkout, after installing the SDK:

```bash
python3 -m pip install -r examples/camera_glove/requirements.txt
oglo doctor
python3 examples/camera_glove/capture.py \
  --camera 0 --seconds 30 --preview \
  --output captures/session_001 \
  --task "Pick up a cup and put it down"
```

Resolve doctor errors first and allow camera access in your OS. `--camera 0` may
select a built-in camera; use `--preview` to check the view and try another index
for the USB camera. Add `--pair` for both hands, or `--serial YOUR_GLOVE_SERIAL`
for a specific glove. Use a new output directory for every run.

### Case 2: OVISION v1

Use **Linux and Python 3.12+** for the native OVISION v1 capture path. Select the
OVISION H.264 video node with `v4l2-ctl --list-devices`; replace `/dev/video0` below
with that node. See the [OVISION setup guide](examples/camera_glove/OVISION.md) for
required firmware/mode, device permissions, and the camera profile applied:

```bash
python3 -m pip install -r examples/camera_glove/requirements-ovision.txt
oglo doctor
python3 examples/camera_glove/ovision.py \
  --video-device /dev/video0 --seconds 30 --pair --preview \
  --output captures/ovision_001 \
  --task "Pick up a cup and put it down"
```

Omit `--pair` for one glove, or use `--serial YOUR_GLOVE_SERIAL`. The OVISION
example keeps the packed stereo video, native camera sensor/timing files, and
calibration. It adds the common timestamp sidecar for the offline join example.

### What to send and how the fields link

Both examples record each glove independently and preserve its current
calibration, mode, rates and original SDK episode files. A one-hand webcam
session looks like:

```text
captures/session_001/
  manifest.json                 task, stream paths, clock definition, run status
  camera/video.mp4              camera frames in order
  camera/timestamps.jsonl       frame_index + host_received_ns for every frame
  gloves/left/calibration.json  existing glove zero recipe
  gloves/left/ep_0001/
    meta.json                   device identity, settings, clocks, capture status
    tactile.npz                 counts: uint16 (N_tactile, 5, 4, 4), timing and loss
    imu.npz                     accel, gyro, raw values, timing and loss
    mag.npz                     field, raw values, timing and loss
```

For OVISION, `camera/` instead contains `cam_ego.mp4`, native stereo/IMU and
calibration files, and the common `timestamps.jsonl` sidecar. See its
[complete file list](examples/camera_glove/OVISION.md#3-files-to-send).

**The linking field is `host_received_ns`**: compare the camera sidecar's time
with that field in each glove NPZ. Both use integer monotonic nanoseconds on the
same host. `frame_index` identifies a video frame; an NPZ row index identifies a
sensor sample. They are independent, since camera, tactile, IMU, and magnetometer
streams have different rates. Webcam timestamps mark image read-return time;
OVISION maps native `capture_ns` (host H.264 packet arrival) to this field.
This gives approximate arrival-time alignment, not exposure synchronization.

The [offline join example](examples/camera_glove/align.py) demonstrates the mapping
and writes source row indices and time differences without changing the recordings:

```bash
python3 examples/camera_glove/align.py captures/session_001 --max-delta-ms 50
# For case 2, use captures/ovision_001 instead.
```

See the [webcam guide](examples/camera_glove/README.md) and
[OVISION field mapping](examples/camera_glove/OVISION.md#4-fields-that-link-ovision-and-oglo)
for exact fields, example rows, and timing limits. This is a versioned
example handoff format; integration with an existing production importer needs to
use that importer's camera/session contract.

**Send the entire session directory unchanged**, optionally zipped, including
both hand folders when using `--pair`. Our team will align and post-process the
streams. Keep the original video, timestamp sidecar, calibration, manifest, and
all four files per glove episode, including an empty `mag.npz` on gloves without
a magnetometer.

- Keep filenames, array keys, shapes, dtypes, units, and sample order intact.
  Do not trim the video, replace NPZs with CSV, resample, filter, normalize, or
  reorder fingers or sensor axes.
- Preserve every timing and loss field: `seq`, `t_us`, `device_time_us`, `host_t`,
  `host_t_ns`, `host_received_ns`, and `dropped`. Do not align using frame number,
  nominal FPS, or separate devices' clocks.
- Keep `meta.json` intact, especially `schema`, `sdk_version`, `channels`,
  `stream_clean`, and `stream_thr`. These describe how to interpret the saved
  values. Put additional labels or notes in a separate file.
- Check `manifest.json` for `complete: true`. This confirms the example's file
  and overlap checks passed; `alignment_validated` remains false. Keep failed or
  interrupted runs separately for diagnosis and retry without editing their status.

For glove-only capture, use [03_collect_data.py](examples/03_collect_data.py) or
the [two-hand example](examples/04_two_hands.py). The [recording format](docs/04_recording.md)
and [data reference](docs/02_data_reference.md) describe the SDK fields and units.

## Calibration and device checks

`glove.zero()`, `clean()`, `raw()`, and `rates()` change device state. Read the
[calibration guide](docs/03_calibration.md) before using them. A new zero requires
wearing and moving the glove and overwrites its stored baseline.

`oglo acceptance` checks an attached USB pair and writes a report. Use `--single`
for one glove. The default preserves calibration and settings; interactive,
state-changing, and long-soak checks are described in the
[acceptance guide](docs/07_acceptance.md).

## Reference and development

- [Data reference](docs/02_data_reference.md): samples, units, axes, clocks, and loss
- [Compatibility](docs/06_compatibility.md): supported protocol and validation scope
- [Troubleshooting](docs/05_troubleshooting.md)
- [Documentation index](docs/README.md)

```bash
git clone https://github.com/OpenGraphLabs/oglo-python.git
cd oglo-python
python3 -m pip install -e ".[dev]"
python3 -m pytest
```

The default tests do not open hardware. See [CONTRIBUTING.md](CONTRIBUTING.md) for
hardware tests and contribution rules. This repository is the canonical SDK source.
Licensed under [Apache-2.0](LICENSE); report security issues through [SECURITY.md](SECURITY.md).
