# Case 2: OVISION v1 + OGLO

Use this example for **OVISION v1 (OVISION-EGO-V1)**: the SC233HGS stereo module
with a USB 2.0 connection and 3840×1080 side-by-side video at 30 FPS. This example
selects its H.264 stream with YCTC metadata. Connect **OVISION and the
OGLO glove(s) to the same Linux computer** (a Linux PC or Raspberry Pi). OVISION
does not connect through a glove. This case uses SyncField's native OVISION
adapter; [case 1](README.md) uses an ordinary USB webcam through OpenCV.

| | USB webcam | OVISION v1 |
| --- | --- | --- |
| Capture entry point | `capture.py` | `ovision.py` |
| Camera connection | OpenCV camera index | Linux V4L2 device path |
| Camera recording | RGB video, encoded to MP4 | Original H.264 stereo, muxed without re-encoding |
| Timing | Host read-return timestamp | Host H.264 packet-arrival timestamp plus native exposure times |
| Additional data | No native camera IMU/calibration capture | Stereo metadata, camera IMU/magnetometer, per-device calibration |
| Glove data | Original SDK episodes | Original SDK episodes |
| Offline join | `align.py`, using host arrival time | Same `align.py`; native device times remain available for later refinement |

## 1. Install and select OVISION

This case requires **Linux and Python 3.12+**. From the OGLO repository root:

```bash
python3 -m pip install -e .
python3 -m pip install -r examples/camera_glove/requirements-ovision.txt
oglo doctor
v4l2-ctl --list-devices
v4l2-ctl --device /dev/video0 --list-formats-ext
```

`v4l2-ctl` is provided by `v4l-utils` on Debian/Ubuntu/Raspberry Pi OS. Locate the
OVISION video node and confirm that it offers 3840×1080 H.264 at 30 FPS. Replace
`/dev/video0` in the commands with that node. Prefer a stable `/dev/v4l/by-id/…`
path when available. Multiple `/dev/video*` entries may belong to one device;
select its video capture node, not a metadata-only node. The OS account needs
access to the video and glove serial devices.

Use a suitable USB data cable/port and close any other camera or recording app.
Resolve doctor errors first. The native adapter requires valid per-unit flash
calibration, INTERNAL stereo FSYNC mode, and readable YCTC exposure/IMU metadata;
it refuses to start if these are unavailable. This example does not change the
FSYNC mode or flash calibration. Mac/Windows webcam access alone does not provide
the Linux UVC extension-unit path used here.

The dependency is pinned to `syncfield[ovision]==0.8.14`, whose adapter API and
sidecar fields this example uses. During setup it applies and verifies its image
profile: 10,000 µs exposure, 1× gain, and 15,360 kbps bitrate. The read-back profile
is saved with camera calibration. OGLO calibration/mode/rates remain unchanged.

## 2. Record the complete session

```bash
python3 examples/camera_glove/ovision.py \
  --video-device /dev/video0 --seconds 30 --pair --preview \
  --output captures/ovision_001 \
  --task "Pick up a cup and put it down"
```

Omit `--pair` for one glove; add `--serial YOUR_GLOVE_SERIAL` to select that glove.
`--camera-serial YOUR_CAMERA_USB_SERIAL` optionally records a camera inventory
label; **the device path selects the camera**, and that label is not independently
verified. Actual per-unit calibration is always read from the selected device.

The preview is a low-rate left-eye view; it does not represent the recording
frame rate. Keep hands and contact surfaces visible. Make visible fingertip taps
near the beginning and end to help assess camera-to-glove timing later. Let the
run and final file checks finish. `q` or Ctrl-C leaves an incomplete session.

## 3. Files to send

Send the entire new session directory, optionally zipped:

```text
captures/ovision_001/
  manifest.json
  camera/
    cam_ego.mp4                 original 3840×1080 side-by-side H.264
    cam_ego.stereo.jsonl        native frame and exposure metadata
    cam_ego.imu.jsonl           camera-board acceleration and angular velocity
    cam_ego.accel.jsonl         separate camera acceleration samples
    cam_ego.gyro.jsonl          separate camera gyroscope samples
    cam_ego.mag.jsonl           camera magnetometer samples
    cam_ego.calibration.json    per-unit geometry, identity, capture profile
    cam_ego.calibration.yaml    original Kalibr calibration text
    cam_ego.calibration.bin     original calibration blob
    sync_point.json            camera recording's host/wall-clock anchor
    finalization.json          native adapter's result and health report
    timestamps.jsonl           common-format references for the join example
  gloves/left/
    calibration.json
    ep_0001/{meta.json, tactile.npz, imu.npz, mag.npz}
  gloves/right/                present with --pair; same glove layout
```

Each recorded video frame contains left and right views in that order, each
1920×1080. Keep the packed video intact; do not crop/split/re-encode it for handoff.
Preserve every native sidecar and its field names, values, units and ordering.
The JSON calibration includes `streams.left/right` intrinsics/distortion,
`stereo.T_right_left`, and camera/IMU geometry. Do not substitute another unit's
calibration. Glove finger order and raw/clean interpretation still come from
each episode's `meta.json`.

The shared session schema remains `oglo-camera-example.v1`, with
`camera.kind: "ovision"` and paths to the native artifacts. This is an example
handoff bundle, not a full SyncField-orchestrator episode. An existing production
importer must use its own session contract; the native camera files are retained
so an adapter can consume them without reconstructing missing sensor data.

## 4. Fields that link OVISION and OGLO

| Native OVISION field | Common example field | How OGLO uses it |
| --- | --- | --- |
| `cam_ego.stereo.jsonl.frame_number` | `timestamps.jsonl.frame_index` | Zero-based index into the packed video; both eyes belong to that frame |
| `cam_ego.stereo.jsonl.capture_ns` | `timestamps.jsonl.host_received_ns` | Compare against each glove NPZ's `host_received_ns` on the same Linux host |
| `device_timestamp_ns` / `left_exposure_start_ns` | `device_timestamp`, unit `ns`, meaning `left_exposure_start` | Camera-local exposure time; preserve for clock fitting, not direct subtraction from a glove clock |
| `right_exposure_start_ns`, `stereo_skew_us`, per-eye exposure durations | Retained in native stereo JSONL | Inspect left/right timing and exposure intervals |
| `user_data_seq`, per-eye GPIO trigger indices | Retained in native stereo JSONL | Native metadata/trigger evidence; not OGLO sample indices |

For example, a native frame could contain (excerpt):

```json
{"frame_number": 12, "capture_ns": 125000000000, "device_timestamp_ns": 9000000000, "left_exposure_start_ns": 9000000000, "right_exposure_start_ns": 9000020000, "stereo_skew_us": 20}
```

The added `timestamps.jsonl` row uses `frame_index: 12`,
`host_received_ns: 125000000000` and `device_timestamp: 9000000000`.
Only **125000000000** is directly comparable with OGLO's host receive timestamps.
The native stereo row is kept unchanged. In the pinned adapter, its
`clock_source: "device_monotonic"` label refers to the available device clock;
`capture_ns` itself is still stamped with host `time.monotonic_ns()` at packet
arrival. Do not reinterpret it as exposure time based on that label.

Run the same offline preview:

```bash
python3 examples/camera_glove/align.py captures/ovision_001 --max-delta-ms 50
```

The output references the nearest tactile, glove IMU and glove magnetometer row
for each video frame, independently for each hand. It reports signed time deltas
and leaves unmatched samples `null`. See the [join walkthrough](README.md#4-which-fields-combine-camera-and-oglo-data)
for reading those references. The 50 ms tolerance is illustrative, not a measured
synchronization bound.

**The camera's IMU and a glove's IMU are different sensors in different frames.**
Preserve both. OVISION's combined IMU sidecar stores acceleration in m/s² and gyro
in rad/s; its separate acceleration sidecar uses g. OGLO stores acceleration in g
and gyro in deg/s. Native camera IMU `capture_ns` values are estimated from frame
arrival plus device-time offsets; they are not independently measured USB arrival
times for each IMU sample. Preserve their `device_timestamp_ns` and unit fields.

OVISION internally synchronizes its stereo pair; that does not hardware-sync it
with OGLO. H.264 encoding and USB buffering can add camera arrival delay. Do not
apply a fixed latency correction from another setup or compare camera and glove
device clocks directly. The OGLO team can use native exposure times, shared-host
arrivals, and visible contact events to refine alignment. `alignment_validated`
stays false even when file checks pass.

## Checks and limitations

The example waits for valid native stereo/IMU metadata, finalizes the adapter,
checks required camera artifacts, verifies frame metadata count and ordering,
decodes the MP4 to check frame count, replays the OGLO files, and checks host-time
overlap. Failures keep `manifest.complete: false` and an error. Keep those folders
for diagnosis. Native drivers can still stall; file checks cannot prove image
quality, exposure alignment, or zero undetected sensor loss.

Adapter reference: [SyncField OVISION source](https://github.com/OpenGraphLabs/syncfield-python/blob/535fb85c5f19fc7f6dc51e01f07ef38e2deb6549/src/syncfield/adapters/ovision_camera.py).
The example's adapter calls were checked against the published
[SyncField 0.8.14 package](https://pypi.org/project/syncfield/0.8.14/).
