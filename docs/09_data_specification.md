# Collection data specification: `OGLData`

Collect **one session containing a camera recording and one or two independent
glove episodes**. `OGLData` describes its `manifest.json`; the manifest points to
the video, camera timestamps, calibration, and sensor arrays. It is a session,
not a synchronized row containing one value from every sensor.

The Python definition is [`oglo/data.py`](../src/oglo/data.py). `OGLData` and its
nested types use `TypedDict`: they remain ordinary JSON-compatible dictionaries
with editor/type-checker guidance. They do **not** validate JSON at runtime.
This formalizes the existing `oglo-camera-example.v1` example handoff format;
it does not change the SDK's separate glove episode schema (`schema: 2`).

## What the collector supplies

| Input | Required? | Example / how to collect |
| --- | --- | --- |
| Task description | Yes | `--task "Pick up a cup and put it down"`; describe the action actually performed |
| New output folder | Yes | `--output captures/cup_001`; use one folder per attempt |
| Camera source | Yes | Webcam `--camera 0`, or OVISION `--video-device /dev/video0` |
| Glove selection | Yes | Default connected glove, `--serial OGLO-L-TEST01`, or `--pair` |
| Duration | Yes; CLI has a default | `--seconds 30`; positive, finite seconds |
| Video and all fitted glove streams | Yes | Captured automatically by the examples; camera and gloves on the same host |
| Glove calibration and settings | Yes | Scripts read existing calibration and preserve episode metadata automatically |
| Native camera calibration, stereo timing, camera IMU/mag | OVISION | Script preserves native files; keep empty mag files when no values exist |
| Extra task labels or notes | Optional | Separate file referencing video frame indices or host timestamps |

Use the [webcam](../examples/camera_glove/README.md) or
[OVISION](../examples/camera_glove/OVISION.md) capture command. Collectors do not
need to construct dictionaries or enter sensor measurements manually. For
glove-only collection, use [03_collect_data.py](../examples/03_collect_data.py):
it produces the same glove episode fields below without an `OGLData` manifest.

## Session fields: `OGLData`

All paths in the manifest are relative to the session directory. Integer
nanosecond values must remain integers when reading or exporting JSON.

| Field | Type | Meaning / example |
| --- | --- | --- |
| `schema` | string | Exactly `"oglo-camera-example.v1"` |
| `task_description` | string | Action supplied by the collector |
| `sdk_version`, `opencv_version` | strings | Versions used for capture |
| `requested_duration_s` | number | Requested duration in seconds, e.g. `30` |
| `host_clock` | string | Exactly `"time.monotonic_ns"` |
| `same_host` | boolean | `true`; every stream was recorded on the same computer |
| `started_wall_time_ns` | integer | Session-start Unix time in ns; provenance only |
| `started_host_monotonic_ns` | integer | Session-start host monotonic time in ns |
| `ended_wall_time_ns` | integer | Added during finalization; may be absent after a crash |
| `camera` | `CameraData` object | Camera files, settings, and observed frame counts |
| `gloves` | list of `GloveData` | One entry per hand, two for a pair |
| `complete` | boolean | Starts `false`; becomes `true` only after capture/file/overlap checks |
| `alignment_validated` | boolean | Always `false` in this capture format |
| `error` | string or null | Failure description, or `null` |
| `overlap_host_received_ns` | two integers | Added after successful checks: `[start_ns, end_ns]`, start < end |

An in-progress or failed manifest may have `camera: {}`, `gloves: []`, or a
glove `episode: null`. Keep it as incomplete. A completed session must have a
camera, one or two distinct glove serials/sides, saved episode paths, an end
time, an overlap interval, and no capture error.

### Camera fields: `CameraData`

These common fields are required in a **completed** session. The Python type
allows missing keys so it also represents setup failures and unfinished runs.

| Field | Type / example | Meaning |
| --- | --- | --- |
| `kind` | `"usb_webcam"` or `"ovision"` | Selects the camera-specific fields below |
| `video` | string, `"camera/video.mp4"` | Original encoded video; OVISION uses `camera/cam_ego.mp4` |
| `timestamps` | string, `"camera/timestamps.jsonl"` | One timing row per decoded video frame |
| `codec` | string | `mp4v` for webcam, `h264_passthrough` for OVISION |
| `requested_fps` | number | Requested camera rate; not a measurement of delivery rate |
| `host_timestamp_meaning` | string | Webcam image read-return or OVISION H.264 packet arrival |
| `width`, `height` | integers | Encoded image size in pixels; OVISION width includes both eyes |
| `frames_submitted`, `frames_decoded` | integers | Must agree and be at least 2 after video verification |
| `first_host_received_ns`, `last_host_received_ns` | integers | First/last recorded camera arrival times |

Webcam additionally saves `index` (integer), `playback_fps` (number),
`fps_request_accepted` (boolean), and `backend` (string). Playback FPS is an
encoder setting; use recorded timestamps for temporal analysis.

OVISION additionally saves `model`, `video_device`, `syncfield_version`
(strings), `usb_serial` (string or null when not supplied),
`native_stereo_metadata` and `calibration` (paths), `eye_order` (`["left", "right"]`),
`eye_width`/`eye_height` (integer pixels), and `native_artifacts` (list of paths).
Preserve the entire camera directory, including `sync_point.json` and
`finalization.json`. Its [native file list and units](../examples/camera_glove/OVISION.md#3-files-to-send)
are part of the OVISION collection requirements.

### Glove fields: `GloveData`

| Field | Type | Meaning |
| --- | --- | --- |
| `serial` | string | Logical CONFIG identity, e.g. `OGLO-L-TEST01` |
| `side` | `"left"` or `"right"` | Device-reported hand |
| `calibration` | string | Path to existing `GET ZERO` read-back |
| `episode` | string or null | SDK episode directory; required non-null for completed sessions |
| `summary` | object | Added after replay checks; identity, settings, counts/rates/loss per stream |
| `error` | string | Present if that glove capture failed |

Example entry before replay verification:

```json
{
  "serial": "OGLO-L-TEST01",
  "side": "left",
  "calibration": "gloves/left/calibration.json",
  "episode": "gloves/left/ep_0001"
}
```

## Per-video-frame fields: `CameraFrameData`

Each line of `camera/timestamps.jsonl` is one JSON object. All seven common
keys below are required; use `null` for unknown device timing, not zero.

| Field | Type | Meaning |
| --- | --- | --- |
| `frame_index` | integer | Consecutive zero-based decoded video index |
| `host_received_ns` | integer | Host monotonic arrival time, captured before encoding/writing |
| `host_read_started_ns` | integer or null | Webcam read-start time; null for OVISION |
| `device_timestamp` | integer or null | Native camera time; null for webcam |
| `device_timestamp_unit` | string or null | `"ns"` for OVISION |
| `device_clock_domain` | string or null | `"ovision_camera"` for OVISION |
| `device_timestamp_meaning` | string or null | `"left_exposure_start"` for OVISION |

OVISION also requires `native_metadata: "cam_ego.stereo.jsonl"` (relative to
the timestamp file's directory) and `native_frame_number` (integer source index).

Example webcam row:

```json
{"frame_index": 0, "host_received_ns": 125000000000, "host_read_started_ns": 124966000000, "device_timestamp": null, "device_timestamp_unit": null, "device_clock_domain": null, "device_timestamp_meaning": null}
```

## Glove arrays: exactly what to preserve

Each episode contains `meta.json`, `tactile.npz`, `imu.npz`, and `mag.npz`.
`N` is that file's sample count; the three counts are independent. Empty streams
retain their files, columns, and shapes with `N = 0`.

| Column in every NPZ | Recorded dtype | Shape | Unit / meaning |
| --- | --- | --- | --- |
| `seq` | uint32 | `(N,)` | Device sequence; per stream over USB, outer packet over BLE |
| `t_us` | uint32 | `(N,)` | Raw device microseconds, wraps at 2³² |
| `device_time_us` | uint64 | `(N,)` | Unwrapped device microseconds; arbitrary origin per glove |
| `host_t` | float64 | `(N,)` | Host monotonic receive time in seconds |
| `host_t_ns` | uint64 | `(N,)` | Same receive time as integer ns |
| `host_received_ns` | uint64 | `(N,)` | Explicit receive-boundary ns, equal to `host_t_ns` |
| `dropped` | uint32 | `(N,)` | Missing samples since previous accepted sequence |

| File-specific column | Recorded dtype | Shape | Unit / meaning |
| --- | --- | --- | --- |
| `tactile.counts` | uint16 | `(N, 5, 4, 4)` | ADC counts 0–4095, raw or device-cleaned according to metadata |
| `imu.accel` | float32 | `(N, 3)` | Acceleration in g, sensor x/y/z |
| `imu.gyro` | float32 | `(N, 3)` | Angular rate in deg/s, sensor x/y/z |
| `imu.raw` | int16 | `(N, 6)` | Native accelerometer xyz followed by gyroscope xyz |
| `imu.raw_valid` | bool | `(N,)` | Whether the corresponding raw row is available |
| `mag.field` | float32 | `(N, 3)` | Magnetic field in gauss, sensor x/y/z |
| `mag.raw` | int16 | `(N, 3)` | Native magnetometer xyz |
| `mag.raw_valid` | bool | `(N,)` | Whether the corresponding raw row is available |

`tactile.counts` above means the `counts` key in `tactile.npz`, not a literal
dotted key. Ignore `raw` placeholders when `raw_valid` is false. Counts are not
force. Finger order comes from `meta.json.channels`; preserve wire order during
collection. See the [data reference](02_data_reference.md) for layout and axes.

Preserve **all** of `meta.json`, including identity (`serial`, `side`, `hw_rev`,
`fw_rev`), `schema`, `sdk_version`, `transport`, `channels`, `has_mag`,
`zero_valid`, `stream_clean`, `stream_thr`, `rate_hz`, `imu_period_ms`,
start/end wall and monotonic clocks, `counts`, loss/status snapshots and deltas,
`complete`, `stop_reason`, and `error`. The recorder populates these; copying
only selected fields loses calibration and capture-quality evidence.

## Reading the data in Python

```python
import json
from pathlib import Path
from oglo import OGLData, replay
from oglo.data import CameraFrameData

root = Path("captures/cup_001")
data: OGLData = json.loads((root / "manifest.json").read_text())
if data["schema"] != "oglo-camera-example.v1" or not data["complete"]:
    raise ValueError("Expected a completed camera/glove session")

with (root / data["camera"]["timestamps"]).open() as rows:
    frame: CameraFrameData = json.loads(next(rows))
hand = data["gloves"][0]
if hand["episode"] is None:
    raise ValueError("Glove episode has not been saved")
episode = replay(root / hand["episode"])
tactile = episode.arrays("tactile")  # Replay validates this stream's arrays.
print(frame["frame_index"], frame["host_received_ns"])
print(tactile["counts"].shape, episode.info.channels)
```

Type annotations alone do not check untrusted input. The capture scripts check
saved video, replay every glove stream, and check common coverage before setting
`complete`. Use [align.py](../examples/camera_glove/align.py) to inspect nearest
arrival-time matches; camera frame indices and sensor row indices are different.
Compare `host_received_ns` only on the same host/session. Device clocks cannot
be compared directly across devices, and arrival matches do not establish
exposure synchronization. No resampled observations, forces, poses, or fused
orientations are required collection fields.

Send the whole original session directory. Keep incomplete attempts separately;
do not rewrite status flags, trim video, resample arrays, or discard timing fields.
