# RealSense D455 + Ubuntu + OGLO

Record Intel RealSense D455 color video, the camera's own accelerometer and
gyroscope, and one or two OGLO gloves on **the same Ubuntu computer**. The
D455 gives color and camera IMU on the camera's own clock plus factory
calibration.

Status: Tested with a simulated camera; not yet run on a D455.

For a plain webcam, use the [webcam guide](README.md). For an OVISION stereo
camera, use the [OVISION guide](OVISION.md).

## 1. What you need

- Ubuntu 22.04 or 24.04. On x86-64, `pyrealsense2` 2.58.4 has wheels for Python
  3.10 to 3.14; on ARM64 only for Python 3.9, 3.10 and 3.12 (Ubuntu's own 3.10 on
  22.04 and 3.12 on 24.04 both work).
- Python 3.10 or newer, in a virtual environment.
- An Intel RealSense D455 on its own USB 3 port with its own cable.
- One or two OGLO gloves on a **different** USB controller from the camera.

Why not macOS: in librealsense 2.58.4 the motion-sensor code is compiled out on
macOS (it raises "Motion sensors are not supported on macOS"), and its macOS
guide lists the IMU as disabled. Building from source does not bring it back.
`pyrealsense2` has never shipped macOS wheels. Releases around 2.42 (2021) could
read the IMU on macOS; librealsense removed that path in late 2025. Color can
still stream on macOS through OpenCV, but without the camera IMU or camera-clock
time.

## 2. Install

From the repository root:

```bash
python -m pip install -e .
python -m pip install -r examples/camera_glove/requirements-realsense.txt
```

`requirements-realsense.txt` pins `pyrealsense2==2.58.4.10922`. For OGLO
Studio, install `'.[studio,studio-realsense]'` instead of adding the
requirements file.

Install the udev rules that let a non-root user open the D455, matching the
pinned librealsense tag `v2.58.4`:

```bash
sudo curl -fsSL https://raw.githubusercontent.com/realsenseai/librealsense/v2.58.4/config/99-realsense-libusb.rules -o /etc/udev/rules.d/99-realsense-libusb.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the camera.

No kernel-driver (DKMS) package is needed; it only builds for kernels
5.15/5.19/6.5. The camera-clock time of each color frame comes from the D455's
frame metadata. If librealsense reads the camera through the kernel video
driver, that driver passes D455 metadata on from **Linux 6.5**: Ubuntu 24.04, or
22.04 with its newer (HWE) kernel. `uname -r` shows yours; on an older kernel
every take fails with "lack camera-clock time". Firmware: `realsense.py --check` compares the camera's firmware
with the SDK's recommended version. If it needs updating, use RealSense
Viewer on Windows or Linux (librealsense does not support the Viewer on macOS).

## 3. Check

```bash
python examples/camera_glove/realsense.py --check
```

Sample passing output (your values will differ):

```text
RealSense check
  device     Intel RealSense D455  serial 123456789012
  firmware   5.16.0.1 (recommended 5.16.0.1)
  usb        3.2  port 2-1
  color      1280x720 bgr8 @ 30 fps
  accel      250 Hz (offered 63, 250)
  gyro       400 Hz (offered 200, 400)
Recording 3 s...
  color      90 frames, 30.0 fps, device time sensor_timestamp (realsense_hw_clock)
  accel      750 samples, 250.0 Hz
  gyro       1200 samples, 400.0 Hz
  dropped    0 color frames
OK: this camera can record color + camera IMU.
```

| Line | Meaning |
| --- | --- |
| `firmware` | Installed vs. the SDK's recommended version; a mismatch is not fatal, only a note |
| `usb` | Version and physical port; shows which USB backend is in use |
| `color` | The color mode the worker will use |
| `accel` / `gyro` | Rates the device offers, then measured rates from a 3 s recording |
| `device time` | Which clock metadata is present (`sensor_timestamp` or `frame_timestamp`), and its clock domain |
| `dropped` | Color frames lost from the frame counter during the 3 s check |

A non-zero exit prints the reason: not Linux, `pyrealsense2` missing or the
wrong version, no RealSense attached (or more than one), no accel/gyro, a
USB 2 connection, or frames without camera-clock time.

## 4. One session

```bash
python examples/camera_glove/realsense.py --seconds 30 --pair --preview --output captures/rs_001 --task "Pick up a cup and put it down"
```

| Option | Use |
| --- | --- |
| `--check` | Run the device check above instead of recording |
| `--output PATH` | A new folder for this attempt |
| `--seconds` | Recording duration, default 30 |
| `--fps` | Requested color rate, default 30 |
| `--task TEXT` | Describe the action you will perform |
| `--serial GLOVE_SERIAL` | Select one glove by serial |
| `--pair` | Record both hands (not with `--serial`) |
| `--preview` | Show a live preview window |
| `--codec {mp4v,hevc_nvenc,h264_nvenc,libx265,libx264}` | Video encoder, default `mp4v` |
| `--video-quality N` | Encoder CRF/CQ, default 23 |

Only one RealSense per computer is supported; a second attached device is
refused. The camera side is the SDK's own `oglo.studio_realsense.RealSenseCameraWorker`
(the same worker OGLO Studio records with): it keeps the pipeline open,
watches for a stalled stream, writes `realsense.calibration.json`, and checks
that every color frame carries device time before it reports complete.

Make a few visible fingertip taps near the start and end of the session for
timing checks. Let final file checks finish; `q` or Ctrl-C leaves an
incomplete session.

## 5. Many episodes

```bash
scripts/collect.sh --camera RealSense --pair --task "pick up a cup"
```

`--camera-backend auto` (the default) picks the `realsense` backend when the
V4L2 name of `--camera` contains `RealSense` and the device passes its own
check; otherwise it falls back to `opencv` and prints why. `--camera-backend
realsense` forces the RealSense backend and raises instead of falling back.
One worker stays open for the whole session, shared across episodes, the same
pattern `collect.py` already uses for OVISION. Keys and general behavior are
in [README section 5](README.md#5-collect-many-episodes-collectpy).

## 6. With Studio instead

Install Studio with `python -m pip install -e '.[studio,studio-realsense]'`, keep
the udev rules from Section 2, and start it as the [Studio guide](../../docs/10_studio.md)
describes. Select the camera choice labeled `<device name> · color + camera IMU · serial
<serial>` in OGLO Studio's device list. **Annotation handoff** is available
for a RealSense recording, because every color frame carries real device
time.

## 7. Files to send

Send the whole session folder, optionally zipped:

```text
captures/rs_001/
  manifest.json
  camera/
    video.mp4
    timestamps.jsonl
    realsense.accel.jsonl
    realsense.gyro.jsonl
    realsense.calibration.json
  gloves/left/
    calibration.json
    ep_0001/
  gloves/right/           present with --pair
```

| Camera file | Contents |
| --- | --- |
| `video.mp4` | Color 1280x720 at the requested fps; `mp4v` or the `--codec` given |
| `timestamps.jsonl` | One row per encoded frame: `frame_index`, `host_received_ns`, `device_timestamp` (integer µs), `device_timestamp_unit: "us"`, `device_clock_domain: "realsense_hw_clock"`, `device_timestamp_meaning` (`sensor_timestamp` or `frame_timestamp`), `native_frame_number` |
| `realsense.accel.jsonl` | One row per sample: `frame_number`, `host_received_ns`, `device_timestamp_us`, `x`, `y`, `z` in m/s² |
| `realsense.gyro.jsonl` | Same shape; `x`, `y`, `z` in rad/s |
| `realsense.calibration.json` | Device name, serial, firmware, recommended firmware, USB type, physical port, `pyrealsense2` version, color stream size/fps/format and intrinsics, accel and gyro rates, color→accel and color→gyro extrinsics, IMU intrinsics when available, and whether librealsense already applied them (`motion_correction`) |

Glove files follow the [same layout as the webcam example](README.md#3-keep-the-complete-output-folder).

## 8. Which times to compare

Compare camera `host_received_ns` with glove JSONL `capture_ns`: both are
host arrival times on the same computer. Compare the color device timestamp
with the accel/gyro device timestamps directly: all three share the camera's
own microsecond clock (`realsense_hw_clock`). That clock is a 32-bit counter
that wraps every 71.6 minutes; the recorder unwraps it, so within one session the
saved values only increase (they can exceed 2^32). Do not compare a camera device
timestamp with a glove device timestamp; those are different clocks on
different hardware.

librealsense already applies the IMU's own calibration (scale and bias) to the
saved accelerometer and gyroscope samples when `enable_motion_correction` is on,
which is the default. `realsense.calibration.json` records that setting under
`motion_correction`; when it says `enabled: true`, do not apply the IMU intrinsics
in that file a second time.

The accelerometer and gyroscope share the IMU's axes, which differ from the
color camera's axes and from each glove's; use the `realsense.calibration.json`
extrinsics to relate the IMU to the color camera. The extrinsics'
rotation is librealsense's 9-element **column-major** list; translation is in
meters.

| Data | Acceleration | Gyroscope |
| --- | --- | --- |
| RealSense IMU JSONL | m/s² | rad/s |
| OGLO replay `accel` / `gyro` | g | degrees/s |
| OGLO JSONL | Raw integer sensor counts | Raw integer sensor counts |

## 9. Troubleshooting

| Symptom | Check |
| --- | --- |
| No device found | Re-run the udev rules step and replug; check the cable and port |
| "delivered no color frame within 10 s (frames seen: color 0, accel 0, gyro 0)" | librealsense holds color back until the accelerometer and gyroscope start, so this usually means the IMU is silent: re-run the udev rules step, update the firmware, and close other programs using the camera |
| Every take fails with "lack camera-clock time" | The frame metadata is not reaching librealsense: check the udev rules and, through the kernel video driver, a Linux 6.5+ kernel (Section 2) |
| A take fails with "the camera IMU stopped" | The accelerometer or gyroscope paused for more than 0.25 s, or stopped before the video did; replug the camera and record again |
| USB 2 connection | Move the camera to a USB 3 port and cable; `--check` prints the USB version |
| No accel/gyro reported | Confirm the unit is a D455 (not a depth-only model) and its firmware is current |
| No `sensor_timestamp` in `--check` | Firmware or USB backend may not expose that metadata path; the worker falls back to `frame_timestamp`, or fails loudly if neither is present |
| `native_frames_dropped` > 0 | Camera and gloves likely share a USB controller, or CPU/codec load is too high; try `mp4v` first and a different controller |
| Glove drops during a RealSense session | Move the gloves to a different USB controller from the camera |
| Recording on macOS | Not supported for the camera IMU; see [Section 1](#1-what-you-need) |

## 10. First-run checklist

1. `realsense.py --check` passes.
2. One 30 s session (Section 4) is `complete` with glove `dropped: 0`.
3. One `collect.py` episode is listed by `dataset.py index` with
   `camera_kind: realsense` and `camera_imu: true`.
4. The [`docs/13` Studio run](../../docs/13_localhost_ui_test.md) passes with
   this camera.

Send the `--check` output, the manifests, and the ZIP check result to
OpenGraph.
