# Case 1: USB webcam + OGLO

There are two collection cases: **USB webcam** (this guide) and
**[OVISION v1 stereo + camera IMU](OVISION.md)**. Use the OVISION case when you have
that module so its native exposure timestamps, inertial data and per-unit
calibration are retained. Both cases keep the OGLO episode files unchanged and
use the same `align.py` walkthrough below.

This example records one USB webcam and one or two OGLO gloves on **the same
computer**, then demonstrates which fields link the recordings for post-processing.
The webcam connects to the computer, not to the glove. Each glove also connects
to that computer over USB-C. One worker per glove drains its sensor streams while
the main thread captures the camera.

```text
USB webcam ──────────────┐                  camera/video.mp4 + timestamps.jsonl
                         ├─ same computer ─ manifest.json
OGLO glove(s), USB-C ────┘                  gloves/<side>/ep_0001/{meta.json, *.npz}
                                                  ↓
                                 align.py → alignment.preview.jsonl
                                                  ↓
                              send original session folder to OGLO
```

This defines `oglo-camera-example.v1`, an example handoff format. It preserves the
SDK's episode format; an existing production importer may need an adapter for the
camera/session files. One webcam provides RGB video, not calibrated stereo or
metric 3D. If your project requires depth, camera intrinsics, distortion, or
camera-to-camera poses, collect those with your camera's SDK and agree on their
format before a collection campaign. A glove IMU is not a camera IMU.

## 1. Install and connect

From the repository root, with Python 3.10+:

```bash
python3 -m pip install -e .
python3 -m pip install -r examples/camera_glove/requirements.txt
oglo doctor
```

Connect the webcam and glove(s) to USB ports with enough power and bandwidth.
Close other applications using either device. Grant camera access to your
terminal/Python application in your OS settings when requested. Resolve the
doctor errors before recording and read the SDK's
[candidate status](../../docs/08_candidate_status.md).

`--camera 0` selects the default OpenCV camera, which may be the laptop's built-in
camera. Try `--camera 1` or another index for your USB camera. Indices can change
after reconnecting devices. Use `--preview` in a short trial to check the view;
both fingertips and contacted objects should remain visible. Use a fresh output
directory for every attempt.

## 2. Record video and gloves together

```bash
python3 examples/camera_glove/capture.py \
  --camera 0 --seconds 30 --fps 30 --preview \
  --output captures/cup_001 \
  --task "Pick up a cup and put it down"
```

For a specific glove, add `--serial YOUR_GLOVE_SERIAL` using its logical CONFIG
serial from `oglo doctor`. For **both hands**, add `--pair` instead. The script
verifies left/right identity and records each hand independently in its own folder.
It reads the existing glove zero recipe and preserves current calibration,
raw/clean mode, thresholds, and rates.

Perform a few distinct fingertip taps on a visible surface near the beginning
and end. These provide events the OGLO team can inspect for timing offset and
drift. Let capture finish, including video decoding checks. Pressing `q` in the
preview or Ctrl-C stops early and leaves the session incomplete.

`--fps` requests a camera rate and sets the MP4 playback rate. A camera/backend
may ignore that request. The script records actual arrival timestamps for every
saved frame; nominal FPS is never used to compute acquisition timestamps.

## 3. Keep the complete output folder

For one left glove (with `--pair`, there is also a `gloves/right/` directory):

```text
captures/cup_001/
  manifest.json
  camera/
    video.mp4
    timestamps.jsonl
  gloves/left/
    calibration.json
    ep_0001/
      meta.json
      tactile.npz
      imu.npz
      mag.npz
```

| File | Fields/data to preserve | Used for |
| --- | --- | --- |
| `manifest.json` | `schema`, task, SDK/OpenCV versions, host clock, camera settings, glove serials/sides and relative paths, `complete`, error, overlap range | Identifying streams belonging to one session and checking its outcome |
| `camera/video.mp4` | Original encoded frames, in order | Visual observations; decoded frame 0 corresponds to timestamp row 0 |
| `camera/timestamps.jsonl` | `frame_index`, `host_received_ns`, read-start time, optional native device time fields | Linking each video frame to the shared host timeline |
| `gloves/<side>/calibration.json` | Existing `GET ZERO` read-back | Baseline/calibration context, without changing the device |
| `gloves/<side>/ep_0001/meta.json` | Entire SDK metadata, including `schema`, `channels`, `stream_clean`, `stream_thr`, clocks and loss/status counters | Interpreting tactile values and device identity |
| `tactile.npz` | `counts`: uint16 `(N, 5, 4, 4)`; sequence, timing and loss arrays | Tactile ADC values in original wire order |
| `imu.npz` | `accel` (g), `gyro` (deg/s): float32 `(N, 3)`; `raw`, `raw_valid`; sequence, timing and loss arrays | Glove motion in sensor axes |
| `mag.npz` | `field` (gauss): float32 `(N, 3)`; `raw`, `raw_valid`; sequence, timing and loss arrays | Magnetic data when fitted; retain the file even if empty |

Each NPZ preserves `seq`, `t_us`, `device_time_us`, `host_t`, `host_t_ns`,
`host_received_ns`, and `dropped`. The three streams have different sample counts.
Keep the complete folder, optionally zipped. Do not rename fields, reorder fingers,
resample, normalize, trim the video, rewrite timestamps, or replace the NPZs with
CSV exports. Put annotations in a separate file and reference `frame_index` or a
host time. The OGLO team can derive processed arrays from the originals.

## 4. Which fields combine camera and OGLO data?

**Join `camera/timestamps.jsonl.host_received_ns` with each OGLO NPZ's
`host_received_ns`.** Both are integer nanoseconds from `time.monotonic_ns()` on
the same computer during the same session. Use the manifest to select the glove
episode and the camera's `frame_index` to select the corresponding video frame.

An illustrative camera row is:

```json
{"frame_index": 12, "host_read_started_ns": 124966000000, "host_received_ns": 125000000000, "device_timestamp": null, "device_timestamp_unit": null, "device_clock_domain": null, "device_timestamp_meaning": null}
```

| Camera field | OGLO field | Relationship |
| --- | --- | --- |
| `host_received_ns = 125000000000` | `tactile.npz["host_received_ns"][250] = 125002000000` | Tactile row 250 arrived 2 ms after camera frame 12 |
| `frame_index = 12` | `row_index = 250` in the derived join | Independent indices; never assume camera frame 12 means tactile row 12 |
| `device_timestamp = null` | `t_us`, `device_time_us` | Glove-local times are preserved, but cannot be directly compared with a camera clock |
| No common sequence counter | `seq` | Device sequence identifies loss/order within that glove stream, not a video-frame match |

For motion, repeat the timestamp lookup independently in `imu.npz` and `mag.npz`.
For two gloves, repeat it for each hand. Do not merge rows by index or assume one
sample per camera frame. Several tactile samples can belong to one camera interval.

### Run the offline join preview

No hardware is needed after recording:

```bash
python3 examples/camera_glove/align.py captures/cup_001 --max-delta-ms 50
```

This creates a new `alignment.preview.jsonl`; it never changes the recordings or
overwrites an existing preview. For each camera frame it gives the nearest source
row **in each glove stream**, plus its `seq`, arrival timestamp, and signed
`delta_ns` (glove minus camera). It writes `null` if the stream is absent, the camera
time is outside the stream's recorded range, or the distance exceeds the tolerance.
The default 50 ms is an illustrative filter, not a measured accuracy guarantee.

For example, one glove entry might contain:

```json
{"serial": "OGLO-L-TEST01", "side": "left", "episode": "gloves/left/ep_0001", "tactile": {"row_index": 250, "seq": 9031, "host_received_ns": 125002000000, "delta_ns": 2000000}, "imu": null, "mag": null}
```

To access the linked values:

```python
import json
from pathlib import Path
import oglo

session = Path("captures/cup_001")
with (session / "alignment.preview.jsonl").open() as rows:
    for line in rows:
        frame = json.loads(line)
        hand = frame["gloves"][0]
        match = hand["tactile"]
        if match is not None:
            episode = oglo.replay(session / hand["episode"])
            counts = episode.arrays("tactile")["counts"][match["row_index"]]
            print(frame["frame_index"], counts.shape, episode.info.channels)
            break
```

This join is a teaching/inspection step. Keep all original samples for later
windowing, offset estimation, resampling, or sensor fusion by the OGLO team.

## Timing and completion limits

The camera timestamp marks **when OpenCV returned the decoded image**, before
encoding or writing. It is not the exposure time. OGLO stamps the USB receive
boundary, and several samples from one USB read can share a timestamp. The preview
chooses the first row at a tied timestamp; it cannot resolve order within a batch
from host timing alone. Neither endpoint has hardware synchronization here.

Do not use `frame_index / FPS`, MP4 playback time, `CAP_PROP_POS_MSEC`, wall time,
or independent stream-relative zero times to align the data. Device clocks have
separate origins and drift. Do not collect on separate computers and subtract
their monotonic timestamps.

If replacing `read_camera()` with a vendor SDK, stamp receipt immediately after
the frame API returns, before conversion/encoding. Preserve native timestamp,
unit, clock domain and documented meaning; preserve native frame IDs too when
available. Leave those fields `null` when unknown rather than inventing values.
Document camera intrinsics/depth/extrinsics in additional files if needed.

`manifest.complete: true` means both capture paths finished, saved video decodes
to the submitted frame count, glove episodes passed replay checks, and their host
time ranges overlap. It does **not** prove exposure alignment, zero camera drops,
full-duration overlap, or image quality. `alignment_validated` remains `false`.
The common recorded range is saved as `overlap_host_received_ns`.

Reported camera/glove failures stop peer capture and leave `complete: false` plus
an error. Keep failed folders for diagnosis; never edit status to make them look
complete. OpenCV's webcam reads have no portable timeout: a hung driver can block
this example. Encoding and disk writes also run in the camera loop and can delay
the next read. Use a short trial to assess the actual camera/backend/storage setup
before longer collection. MP4 uses lossy `mp4v` encoding.

API references: [OpenCV camera reads](https://docs.opencv.org/4.x/d8/dfe/classcv_1_1VideoCapture.html),
[OpenCV video writing](https://docs.opencv.org/4.x/dd/d9e/classcv_1_1VideoWriter.html),
and [Python monotonic time](https://docs.python.org/3/library/time.html#time.monotonic_ns).
