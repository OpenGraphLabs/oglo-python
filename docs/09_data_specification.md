# Recording files and fields

Recordings use the `og-skill` backend's JSONL sensor format. Each line is one
sample. For commands, see [recording](04_recording.md),
[webcam capture](../examples/camera_glove/README.md), or
[OVISION capture](../examples/camera_glove/OVISION.md).

## Glove files

A left-hand episode contains:

```text
ep_0001/
  meta.json
  tactile_left.jsonl
  tactile_left.raw.jsonl          only for RAW captures
  wrist_imu_left.jsonl
  wrist_mag_left.jsonl
  tactile_left.calibration.json   stored recipe, when available
```

Right-hand files use `_right`. Magnetic files remain present when empty.
Keep the entire episode folder.

| File | Values |
| --- | --- |
| `tactile_<side>.jsonl` | CLEAN tactile values, in ADC counts |
| `tactile_<side>.raw.jsonl` | Original RAW ADC counts |
| `wrist_imu_<side>.jsonl` | Signed integer `ax`, `ay`, `az`, `gx`, `gy`, `gz` |
| `wrist_mag_<side>.jsonl` | Signed integer `mx`, `my`, `mz` |

RAW capture preserves its original file and derives CLEAN from the saved baseline
and threshold. A value equal to the threshold survives. Without a valid recipe,
the recording remains incomplete. Firmware CLEAN capture does not create RAW data.

## Sensor rows

These fields match the backend sensor contract:

| Field | Meaning |
| --- | --- |
| `frame_number` | Row index within this stream, starting at zero |
| `capture_ns` | Integer host arrival time in nanoseconds |
| `clock_source` | `"host_monotonic"` |
| `clock_domain` | Host label; defaults to `"local_host"` |
| `uncertainty_ns` | Timing assumption; defaults to 500,000 ns, not measured sync accuracy |
| `device_timestamp_ns` | Raw wrapping device counter in nanoseconds |
| `channels` | Named integer sensor values |
| `oglo` | Additional SDK fields needed for exact replay |

Tactile names are `<finger>_<row>_<column>`, such as `thumb_0_0`. There are 80
channels. Finger names follow the captured device configuration.

Example IMU row, expanded here for readability:

```json
{
  "frame_number": 0,
  "capture_ns": 125000000000,
  "clock_source": "host_monotonic",
  "clock_domain": "local_host",
  "uncertainty_ns": 500000,
  "device_timestamp_ns": 2000000,
  "channels": {"ax": 0, "ay": 0, "az": 4096, "gx": 0, "gy": 0, "gz": 0},
  "oglo": {
    "seq": 42,
    "t_us": 2000,
    "device_time_us": 4294969296,
    "host_t": 125.0,
    "host_t_ns": 125000000000,
    "host_received_ns": 125000000000,
    "dropped": 0,
    "raw_valid": true,
    "accel": [0.0, 0.0, 1.0],
    "gyro": [0.0, 0.0, 0.0]
  }
}
```

A file stores that object on one line. Preserve integer timestamps: converting
large integers to floating point can lose precision.

### Replay fields

The backend reads the standard fields. The additional `oglo` object preserves
information that those fields do not contain:

| Field | Meaning |
| --- | --- |
| `seq` | Original device sequence number |
| `t_us` | Raw device microseconds, wrapping at 2³² |
| `device_time_us` | Device time extended across rollover |
| `host_t` | Host arrival in seconds |
| `host_t_ns`, `host_received_ns` | Exact integer arrival times; equal to `capture_ns` |
| `dropped` | Missing samples since the previous accepted sequence |
| `raw_valid` | IMU/mag only: whether original integer values were available |
| `accel`, `gyro` | IMU only: original scaled values in g and degrees/s |
| `field` | Magnetometer only: original scaled values in gauss |

Keep this object for SDK replay. `device_timestamp_ns` equals `t_us * 1000`.
`frame_number` is not a device sequence number or a video-frame index.

A partial recording can contain an IMU/mag row with `raw_valid: false` and empty
`channels`. A completed recording cannot. Such partial rows preserve the available
scaled values for diagnosis and must not be treated as valid backend measurements.

### Metadata and calibration

`meta.json` uses **schema 3**. It saves identity, firmware, mode, threshold, finger
order, stream rates, clock settings, sample counts, device/host loss, and status
snapshots. It also names the calibration and CLEAN files when present.

Check `complete`, `stop_reason`, and `error`. A missing or unfinished metadata file
is not a completed capture. An early stop may be complete but have
`stop_reason: "cancelled"`.

`tactile_<side>.calibration.json` uses the backend's
`syncfield.oglo_tactile_calibration.v1` structure: `sample` describes channel order,
`transform` describes RAW/CLEAN handling, and `zero` keeps the `GET ZERO` response.
The read-back does not claim that a new sweep or quality test was performed.

Replay checks row numbering, types, clocks, counts, sequences, loss, and any
RAW/CLEAN pair. Malformed, duplicate-key, or truncated JSON rows are rejected.
Earlier recording schemas are not supported.

## Camera sessions

A session holds camera files and one or two glove episodes:

```text
session/
  manifest.json
  camera/
  gloves/left/
    calibration.json     recipe read before capture
    ep_0001/             sensor JSONL and episode metadata
  gloves/right/          present for two hands
```

Paths in the session manifest are relative to this folder.
`OGLData` and the nested types in [`oglo.data`](../src/oglo/data.py) describe these
ordinary dictionaries; they do not validate JSON at runtime.

### Session fields: `OGLData`

| Field | Meaning |
| --- | --- |
| `schema` | `"oglo-camera-example.v2"` |
| `task_description` | Recorded action |
| `sdk_version`, `opencv_version` | Software versions |
| `requested_duration_s` | Requested duration |
| `host_clock`, `same_host` | `"time.monotonic_ns"` and `true` |
| `started_wall_time_ns`, `ended_wall_time_ns` | Unix start/end times; end may be absent after a crash |
| `started_host_monotonic_ns` | Host monotonic start time |
| `camera`, `gloves` | Camera description and one entry per hand |
| `complete`, `error` | Capture result |
| `stop_reason` | `"duration"`, or `"cancelled"` when the caller's stop event ended the session early; `null` until complete |
| `alignment_validated` | Always `false`; file checks do not certify synchronization |
| `overlap_host_received_ns` | Common recorded interval `[start_ns, end_ns]` |

### Glove fields: `GloveData`

| Field | Meaning |
| --- | --- |
| `serial`, `side` | Configured identity and hand |
| `calibration` | Saved pre-capture `GET ZERO` response |
| `episode` | JSONL episode folder; `null` until saved |
| `summary` | Counts, rates, settings, and loss after replay checks |
| `error` | Glove capture error, when present |

### Camera fields: `CameraData`

Completed sessions include `kind`, `video`, `timestamps`, `codec`, `requested_fps`,
`host_timestamp_meaning`, `width`, `height`, `frames_submitted`, `frames_decoded`,
`first_host_received_ns`, and `last_host_received_ns`. Frame counts must agree and
be at least two. Width includes both eyes for the native OVISION stereo adapter.

Webcams also save `index`, `playback_fps`, `fps_request_accepted`, `backend`, and
`video_quality` (the CRF / CQ of an ffmpeg codec; `null` for `mp4v`).
OVISION also saves `model`, `video_device`, optional `usb_serial`,
`syncfield_version`, `native_stereo_metadata`, `calibration`, `eye_order`,
`eye_width`, `eye_height`, and `native_artifacts`. Keep all
[native OVISION files](../examples/camera_glove/OVISION.md#3-files-to-send).

Studio's macOS UVC option uses `kind: "ovision_uvc_stereo_host_timed"` and saves
the full 3840×1080 packed frame. The selected `eye` (`left` or `right`) controls
preview and review only. Its camera metadata also records `name`, `mode`,
`source_width`, `source_height`, `preview_width`, `preview_height`, and
`source_eye_order: ["left", "right"]`. It uses host pipe-read timing and has no
native exposure time, camera IMU, or calibration sidecars; it remains a source
archive. Older Studio episodes with `kind: "ovision_uvc_eye"` contain only one eye.

Studio's Linux native option uses `kind: "ovision_native_stereo"` and saves
`camera/cam_ego.mp4` plus the native stereo/IMU/calibration sidecars and
`camera/sync_point.json`. Its selected eye is likewise a preview choice; the
original H.264 preserves both eyes. Native exposure time appears in the common
timestamp file and in `cam_ego.stereo.jsonl`.

### Per-video-frame fields: `CameraFrameData`

Each line of `camera/timestamps.jsonl` contains:

| Field | Meaning |
| --- | --- |
| `frame_index` | Consecutive video-frame index, starting at zero |
| `host_received_ns` | Integer host arrival time |
| `host_read_started_ns` | OpenCV/FFmpeg read-start time; `null` for native OVISION |
| `device_timestamp` | Native camera time; `null` for webcam or macOS UVC OVISION |
| `device_timestamp_unit` | `"ns"` for native OVISION |
| `device_clock_domain` | `"ovision_camera"` for native OVISION |
| `device_timestamp_meaning` | `"left_exposure_start"` for native OVISION |

Use `null` for unknown native timing. OVISION also includes `native_metadata` and
`native_frame_number`. The native metadata path is relative to the timestamp file.

## Read a session

```python
import json
from pathlib import Path
import oglo

root = Path("captures/cup_001")
session = json.loads((root / "manifest.json").read_text())
if session["schema"] != "oglo-camera-example.v2" or not session["complete"]:
    raise ValueError("Expected a completed camera/glove session")

hand = session["gloves"][0]
episode = oglo.replay(root / hand["episode"])
print(episode.summary())
```

For approximate camera/glove matching, use the
[offline preview](../examples/camera_glove/README.md#run-the-offline-join-preview).
Only compare host timestamps from the same computer and session.

Sensor rows and names match backend readers. Camera sessions retain the SDK's
folder manifest; backend upload and session registration remain separate operations.
