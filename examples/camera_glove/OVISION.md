# OVISION v1 + OGLO

Record OVISION stereo video and glove data on **the same Linux computer**.
This guide is for OVISION-EGO-V1, the SC233HGS USB stereo module. It uses the
native SyncField adapter to keep video, exposure timing, motion data, and calibration.

For an ordinary webcam, use the [webcam guide](README.md).

## 1. Install and select OVISION

Use **Linux and Python 3.12+** in a virtual environment. From the repository root:

```bash
python -m pip install -e .
python -m pip install -r examples/camera_glove/requirements-ovision.txt
oglo doctor
v4l2-ctl --list-devices
v4l2-ctl --device /dev/video0 --list-formats-ext
```

If you installed a candidate wheel, use its matching examples and skip
`pip install -e .`.

`v4l2-ctl` comes from `v4l-utils` on Debian, Ubuntu, and Raspberry Pi OS.
Find the OVISION **video capture node** that offers **3840×1080 H.264 at 30 FPS**.
Replace `/dev/video0` with that path. Prefer `/dev/v4l/by-id/…` when available.
A metadata-only node is not the video source.

Close other recording apps. Your OS account needs access to the camera and glove
serial devices. Resolve `doctor` errors first.

The camera must have valid calibration in flash, INTERNAL stereo FSYNC mode,
and readable YCTC exposure/IMU metadata. The adapter checks these before starting;
it does not change FSYNC mode or flash calibration. This capture path requires
Linux even if the camera appears as a webcam on macOS or Windows.

The example pins `syncfield[ovision]==0.8.14`. Setup applies and verifies:

| Camera setting | Value |
| --- | --- |
| Exposure | 10,000 µs |
| Gain | 1× |
| Bitrate | 15,360 kbps |

The verified profile is saved with camera calibration. Glove settings stay unchanged.

## 2. Record the complete session

```bash
python examples/camera_glove/ovision.py \
  --video-device /dev/video0 --seconds 30 --pair --preview \
  --output captures/ovision_001 \
  --task "Pick up a cup and put it down"
```

- Omit `--pair` for one glove. Use `--serial YOUR_GLOVE_SERIAL` to select it.
- Use a new `--output` folder for every attempt.
- `--camera-serial` adds an inventory label only, saved as `usb_serial` in
  `manifest.json`. The device path selects the camera; the label is not checked.

The camera side is the SDK's own native OVISION worker (`oglo.studio_ovision`, the
one OGLO Studio records with): it reads the calibration, keeps the stream connected,
ends a recording whose capture dies or delivers no frame for five seconds, writes
`finalization.json` and `timestamps.jsonl` and checks that every native file is there.

The preview is a slow left-eye view, not a measure of recording rate. Keep hands
and contact surfaces visible, and make visible fingertip taps near the start and
end for timing checks. Let final file checks finish. `q` or Ctrl-C leaves an
incomplete session.

For many episodes in one sitting, `collect.py` ([README section 5](README.md#5-collect-many-episodes-collectpy))
drives this same worker, kept live across episodes and reopened when the camera dies
or is replugged; each episode folder holds the files listed below.

## 3. Files to send

Send the whole session folder, optionally zipped:

```text
captures/ovision_001/
  manifest.json
  camera/
    cam_ego.mp4
    cam_ego.stereo.jsonl
    cam_ego.imu.jsonl
    cam_ego.accel.jsonl
    cam_ego.gyro.jsonl
    cam_ego.mag.jsonl
    cam_ego.calibration.json
    cam_ego.calibration.yaml
    cam_ego.calibration.bin
    sync_point.json
    finalization.json
    timestamps.jsonl
  gloves/left/
    calibration.json
    ep_0001/              sensor JSONL and metadata
  gloves/right/           same layout when using --pair
```

| Camera files | Contents |
| --- | --- |
| `cam_ego.mp4` | Original H.264; left then right view, each 1920×1080 |
| `cam_ego.stereo.jsonl` | Frame numbers and per-eye exposure timing |
| `cam_ego.imu/accel/gyro/mag.jsonl` | Camera motion and magnetic samples |
| `cam_ego.calibration.*` | This camera's geometry, identity, and capture profile |
| `sync_point.json`, `finalization.json` | Clock anchor and capture report |
| `timestamps.jsonl` | Common camera timing format used by `align.py` |

Keep the packed video and every accompanying file, including empty magnetic
files. Use only the calibration saved from that camera.

Glove files follow the [same layout as the webcam example](README.md#3-keep-the-complete-output-folder).
Their JSONL rows match `og-skill` sensor fields. The overall session remains the
SDK's `oglo-camera-example.v2` format; a full production upload needs its own
session descriptors and checks.

## 4. Fields that link OVISION and OGLO

| OVISION field | Common camera field | Meaning |
| --- | --- | --- |
| `frame_number` | `frame_index` | Index of the packed video frame |
| `capture_ns` | `host_received_ns` | Host arrival time of the H.264 packet |
| `device_timestamp_ns` / `left_exposure_start_ns` | `device_timestamp` | Left exposure time on the camera's clock |

Compare camera `host_received_ns` with glove JSONL `capture_ns`. Do not compare camera and glove device clocks directly.

For example, `capture_ns: 125000000000` is a host time. An exposure timestamp of
`9000000000` is a different clock. Only the host time can be directly compared
with glove arrival times on that computer.

In the pinned adapter, `clock_source: "device_monotonic"` describes the available
device clock. Its `capture_ns` field still holds host arrival time; the label does
not turn that field into exposure time.

Run an offline arrival-time preview:

```bash
python examples/camera_glove/align.py captures/ovision_001 --max-delta-ms 50
```

See the [join walkthrough](README.md#run-the-offline-join-preview) to read matches.
The 50 ms tolerance is a search setting, not a synchronization guarantee.

### Camera and glove motion units differ

| Data | Acceleration | Gyroscope |
| --- | --- | --- |
| OVISION combined IMU JSONL | m/s² | rad/s |
| OVISION separate acceleration JSONL | g | — |
| OGLO replay `accel` / `gyro` | g | degrees/s |
| OGLO JSONL | Raw integer sensor counts | Raw integer sensor counts |

The camera and gloves have separate sensors and axes. Camera IMU `capture_ns`
values are estimated from frame arrival and device-time offsets, not independently
measured USB arrivals for each IMU sample. Keep device timestamps and unit fields.

## Checks and limitations

The example checks required files, stereo metadata order/counts, decoded video
frame count, glove replay, and overlapping host time ranges. Failures leave
`complete: false` and an error in `manifest.json`.

OVISION synchronizes its two eyes internally, not the camera with the gloves.
H.264 encoding and USB buffering add delay. Keep native exposure times and visible
contact events for later alignment. File checks do not prove image quality,
exposure alignment, or zero sensor loss; `alignment_validated` stays `false`.
Native drivers can also stall. Try a short run before long collection.
