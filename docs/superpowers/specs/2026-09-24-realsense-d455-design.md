# RealSense D455 + Ubuntu: color, camera IMU, and gloves

Date: 2026-09-24 · Status: design approved in conversation, awaiting spec review
Branch: `sungman-cho/realsense-d455`, based on `main` at `1d05a71` (PRs #18 and #20 merged).

## Why

NTU-MARS Lab has one OVISION camera and needs at least two collection stations. The
second station uses a RealSense D455. On macOS they read its color stream through
OpenCV, and the camera IMU is unreachable there: librealsense's macOS guide says
"Motion sensors (IMU) are disabled on macOS in the current release", and
`pyrealsense2` ships no macOS wheels. On Ubuntu, `pyrealsense2` gives color and IMU on
the camera's own clock plus factory calibration.

## Goal and success criteria

A partner can follow one guide on Ubuntu and record, with one or two OGLO gloves:

- D455 color video, the D455 accelerometer and gyroscope, per-frame camera-clock
  timestamps, and the unit's factory calibration;
- through `realsense.py` (one session), `collect.py` (many episodes, published by the
  existing `dataset.py` check and HF upload), and OGLO Studio (localhost UI, export ZIP).

Done means all four test layers below pass. The hardware status stays "not yet tested
on hardware" until the partner's physical run (layer 3) is reported.

## Decisions

| Question | Decision |
| --- | --- |
| Scope | Guide plus a backend in `collect.py` and a camera choice in OGLO Studio |
| Streams | Color + accel + gyro. No depth, no IR |
| Architecture | One SDK worker kept open for the whole session, shared by Studio and the examples (the OVISION pattern) |
| Physical verification | The partner (Celeste, NTU-MARS) runs the checklist. We ship unit tests and a simulated Studio run |

## Non-goals

Depth or IR streams; macOS support; OG Center ingestion or a new OG Center processing
profile; more than one RealSense per station; fusing accel and gyro into one stream;
firmware-update tooling. The guide does not mention OG Center. The Slack reply sets
that expectation: D455 episodes are single-view color, not OVISION stereo.

## Architecture

```
oglo.studio_realsense.RealSenseCameraWorker   (SDK, new module)
  owns the pyrealsense2 pipeline, callback, encoder, IMU files, calibration, watchdog
  implements Studio's camera contract: preview · live_status · begin · finish · close
        │                                        │
  OGLO Studio (studio.py)                  examples/camera_glove/realsense.py
  lists it, connects it, validates         RealSenseCapture: capture.py's camera contract
  its files, exports them                  (prepare · record(stop) · close · metadata),
                                           problem(), --check, one-session main()
                                                 │
                                           collect.py: one worker kept open per session
```

This mirrors `oglo.studio_ovision.NativeOvisionCameraWorker`, which Studio,
`ovision.py` and `collect.py` already share. There is one recorder implementation.

### `oglo.studio_realsense` (SDK)

- `RealSenseCameraWorker(index, *, mode="realsense", name=<serial>, fps=30.0,
  writer_factory=None)`.
  - `native_device_timestamps = True`, `postprocessing_capable = False`.
- **Open:** find the device by serial and require accel and gyro. Turn off
  `global_time_enabled` on every sensor that has it, so timestamps stay on the camera
  clock. Enable:
  - color 1280×720 BGR8 at `fps`; if that mode isn't offered, raise and list the modes
    that are;
  - accel and gyro `motion_xyz32f`, each at the highest rate the device offers.

  Start the pipeline with a callback.
- **Callback** (librealsense thread, copies data only):
  - Color: stamp `time.monotonic_ns()` on arrival, read the device time (below) and
    the frame counter, and copy the BGR array. Keep the newest frame for the preview.
    While recording, also put it on a bounded queue (64 frames). When the queue is
    full, drop the frame; the frame-counter gap counts it.
  - Motion: while recording, append `(frame_number, host_received_ns,
    device_timestamp_us, x, y, z)` to the matching stream's buffer, under a lock.
- **Device time for color:** `sensor_timestamp` metadata (µs, mid-exposure) with
  meaning `"sensor_timestamp"`. If that metadata is missing, use the frame's
  hardware-clock timestamp with meaning `"frame_timestamp"` (readout start). If neither
  is in the hardware-clock domain, `device_timestamp` is `null`; the episode then fails
  its completeness check.
- **Device time for IMU:** the motion frame's hardware-clock timestamp, as integer µs.
- **Clock wrap:** the camera clock is a 32-bit µs counter (wraps every ~71.6 min;
  librealsense trims HID timestamps to 32 bits to match UVC). Every stream is
  unwrapped against one worker-wide reference, so saved values only increase within
  a session (`device_clock_unwrapped: true`).
- **`begin(folder, stop)`:** create `camera/`, open the video writer (`writer_factory`
  if given, else OpenCV `mp4v`, as Studio's webcam worker does), and start one writer
  thread. The thread drains the color queue into the video and `timestamps.jsonl`, and
  flushes the IMU buffers to their JSONL files.
- **Watchdog:** if no color frame arrives for 5 s while recording, set `error` and set
  `stop`, as Studio's camera contract requires. `live_status()` reports the age of the
  last frame the whole time.
- **`finish()`:**
  1. Stop routing frames to the episode, join the writer thread, and close the files.
  2. Write `realsense.calibration.json`.
  3. Run the completeness checks below.
  4. Return metadata: file paths, counts, first/last host ns, `native_frames_dropped`.

  Raise on any check failure.
- **`close()`:** stop the pipeline. If a recording is still running, it is stopped
  first.
- **The Linux-only check lives in discovery** (`problem()`, `_camera_choices()`), not
  in the worker. That keeps the worker testable on any OS with a fake `pyrealsense2`.

### OGLO Studio (`studio.py`)

- `_camera_choices()` on Linux: if `pyrealsense2` imports, add one choice per RealSense
  that has accel and gyro, listed before that camera's plain V4L2 nodes:
  `{"index": 0, "mode": "realsense", "name": <serial>, "label":
  "<device name> · color + camera IMU · serial <serial>"}`. As with native OVISION,
  `index` is for display only; the worker selects the device by serial.
- `connect()` accepts mode `realsense`, finds the choice again by serial, and creates
  `RealSenseCameraWorker`.
- `_validate()` calls a new `_validate_realsense()` for `kind == "realsense"`. It checks
  that the accel, gyro and calibration files exist and parse, and that every frame has
  device timing. Existing rules still apply: **Annotation handoff** needs device
  timestamps, and **OG Center sensor source** stays OVISION-only.
- No change to `studio_web/` (the page already passes camera mode and name through).
- `pyproject.toml` gains the extra `studio-realsense = ["pyrealsense2==2.58.4.10922"]`.

### Examples (`examples/camera_glove/`)

- `realsense.py`, shaped like `ovision.py`:
  - `problem(camera)` returns why this machine can't record, or `None`. Reasons: not
    Linux; `pyrealsense2` missing or not the pinned version; no RealSense or more than
    one; no accel/gyro; USB 2 connection (plug into USB 3).
  - `open_worker(...)` opens the SDK worker.
  - `RealSenseCapture(args, output, worker=None, tick=None, progress=None)`, like
    `OvisionCapture`: `begin`, a loop that ticks the preview until the deadline or
    `stop`, then `finish`. It passes `capture.open_writer` with `--codec` and
    `--video-quality` as `writer_factory`.
  - `--check`: prints device name, serial, firmware vs `recommended_firmware_version`,
    USB type, physical port (this shows which USB backend is in use), color modes, and
    IMU rates. Then it records 3 s and reports measured color/accel/gyro rates, the
    clock domain, and whether `sensor_timestamp` metadata is present. Non-zero exit with
    the reason on failure.
  - `main()`: one session through `capture.capture(camera_factory=RealSenseCapture)`.
- `requirements-realsense.txt`: `-r requirements.txt` + `pyrealsense2==2.58.4.10922`.
- `collect.py`:
  - `--camera-backend` choices become `auto, ovision, realsense, opencv`.
  - `choose_backend` in `auto`:
    1. If the V4L2 card name of `--camera` contains `RealSense`: `realsense` when
       `realsense.problem()` is `None`, else `opencv` with that reason. The idle
       window shows the reason, so a RealSense episode never loses its IMU
       silently. A RealSense is never probed as an OVISION.
    2. Otherwise OVISION if it answers (unchanged).
    3. Otherwise `opencv`.

    An explicit `--camera-backend realsense` raises instead of falling back.
  - One worker stays open for the session, with an idle preview source like
    `OvisionIdleSource`. The camera factory in `record_episode` returns
    `RealSenseCapture(worker=...)`.
  - Recovery reuses the existing reopen path: after a watchdog failure or unplug,
    reopen by serial and wait for a replug while the gloves keep their idle readers.
  - The glove/camera shared-USB-controller check keeps working through the V4L2 index.
- `dataset.py`:
  - `CAMERA_SENTENCE["realsense"]` and its `CAMERA_LAYOUT` lines;
  - `FIELDS["camera_kind"]` text;
  - `camera_imu = bool(camera.get("imu") or (camera.get("accel") and camera.get("gyro")))`.
- `scripts/workstation.env.example`: a commented `CAMERA=RealSense` example.

### SDK types (`src/oglo/data.py`)

- `CameraData.kind` adds `"realsense"`.
- New optional fields: `firmware_version`, `recommended_firmware_version`, `usb_type`,
  `physical_port`, `pyrealsense2_version`, `accel_hz`, `gyro_hz`, `accel_unit`,
  `gyro_unit`, `accel_samples`, `gyro_samples`, `native_frames_dropped`,
  `device_clock_domain`.
- The `CameraFrameData` docstring no longer says native references are OVISION-only.

## Data contract (episode `camera/` folder, `kind: "realsense"`)

| File | Contents |
| --- | --- |
| `video.mp4` | Color 1280×720 at `--fps`; `mp4v` or the `--codec` given to `collect.py` / `realsense.py` |
| `timestamps.jsonl` | One row per encoded frame, in the common format: `frame_index`, `host_read_started_ns: null`, `host_received_ns`, `device_timestamp` (int µs), `device_timestamp_unit: "us"`, `device_clock_domain: "realsense_hw_clock"`, `device_timestamp_meaning` (`"sensor_timestamp"` or `"frame_timestamp"`), `native_frame_number` |
| `realsense.accel.jsonl` | One row per sample: `frame_number`, `host_received_ns`, `device_timestamp_us`, `x`, `y`, `z` in m/s² |
| `realsense.gyro.jsonl` | Same shape; `x`, `y`, `z` in rad/s |
| `realsense.calibration.json` | Device name, serial, firmware, recommended firmware, USB type, physical port, `pyrealsense2` version; color stream (size, fps, format) and intrinsics (fx, fy, ppx, ppy, model, coeffs); accel and gyro rates; color→accel and color→gyro extrinsics (rotation as librealsense's 9-element **column-major** list, translation in m); IMU intrinsics when the unit has them, else `null` with the reason |

The manifest camera block names these files (`video`, `timestamps`, `accel`, `gyro`,
`calibration`). `dataset.py` already requires every file the manifest names, so
`publishable()` covers them. There is no combined `imu` file: accel and gyro sample at
different rates, and merging them means interpolating.

**Completeness**, in addition to the existing checks (decoded frames = submitted =
timestamp rows, glove replay, host-time overlap):

1. At least 2 accel and 2 gyro samples.
2. Every color frame has an integer `device_timestamp`.
3. Device timestamps strictly increase within color, within accel, and within gyro.
4. The IMU device-time range overlaps the color device-time range, starts and ends
   within 0.5 s of it, and never pauses longer than 0.25 s (so `camera_imu: true`
   is never claimed for a take whose IMU stopped partway). The metadata records
   expected vs. actual samples and the largest gap per stream.

`native_frames_dropped` (gaps in the color frame counter) is recorded, not a failure.

## Tutorial: `examples/camera_glove/REALSENSE.md`

Same style as `OVISION.md`. `README.md` gets a pointer next to the OVISION one.

0. Status line: "Tested with a simulated camera; not yet run on a D455." Removed after
   the partner's report.
1. What you need: Ubuntu 22.04/24.04 (x86-64 or ARM64), Python 3.10+, a D455 on a USB
   3 port with its own cable, gloves on another USB controller. One line on why macOS
   can't record the D455 IMU.
2. Install: venv, `pip install -e .`, `pip install -r
   examples/camera_glove/requirements-realsense.txt` (Studio: `'.[studio,studio-realsense]'`).
   udev rules from librealsense `v2.58.4` (`config/99-realsense-libusb.rules`, which
   lists the D455 `0b5c`) into `/etc/udev/rules.d/`, reload, replug. No kernel-driver
   package: the stock `uvcvideo` driver carries RealSense metadata, and the DKMS package
   only supports kernels 5.15/5.19/6.5. Firmware: `--check` compares with the SDK's
   recommended version. Update with RealSense Viewer on any OS if needed.
3. Check: `python examples/camera_glove/realsense.py --check`, with sample passing
   output and what each line means.
4. One session: `realsense.py --seconds 30 --pair --preview --output … --task …`, with
   an options table.
5. Many episodes: `scripts/collect.sh --camera RealSense --pair --task …`; keys and
   behavior link to README section 5.
6. With Studio instead: select "… · color + camera IMU"; Annotation handoff is
   available.
7. Files to send: folder tree and the data-contract table.
8. Which times to compare: camera ↔ glove by host time; camera ↔ camera IMU by camera
   clock. Units table (RealSense m/s², rad/s vs OGLO g, deg/s, raw counts). Axes differ;
   the extrinsics' rotation is column-major.
9. Troubleshooting: no device (udev, cable), USB 2, no IMU (firmware, model), no
   `sensor_timestamp` (metadata path), `native_frames_dropped` > 0 (shared controller,
   CPU, codec), glove drops (move gloves), macOS.
10. First-run checklist: `--check` passes. One 30 s session is `complete` with glove
    `dropped: 0`. One `collect.py` episode is listed by `dataset.py index` with
    `camera_kind: realsense` and `camera_imu: true`. The `docs/13` Studio run passes.
    Send the check output, the manifests and the ZIP check result to OpenGraph.

Also updated:
- `docs/09_data_specification.md`: the `realsense` kind and its fields.
- `docs/10_studio.md`: a camera-setup row and the install extra.
- `docs/13_localhost_ui_test.md`: a RealSense install line and a `realsense` branch in
  the ZIP check that also requires the accel, gyro and calibration files.
- `docs/11_studio_adapters.md`: the RealSense worker as a second native reference.
- `CHANGELOG.md`.

## Testing

| Layer | What | Where |
| --- | --- | --- |
| 1. Unit (CI) | `tests/fake_realsense.py`: a fake `pyrealsense2` (context, device, sensors, options, pipeline with callback, color framesets, and accel/gyro at their own rates on one hardware clock), with switches for missing metadata, frame-counter gaps, a stall, and an unplug. Tests: worker files and fields; each completeness rule, including a negative fixture with missing device timing; drop counting; watchdog sets `stop` and `error`; `writer_factory`; `problem()` reasons; `collect.py` backend choice, one worker across episodes, reopen after failure; `dataset.py` publish, `camera_imu`, card; Studio choice listing, connect, `_validate_realsense`, Annotation handoff ready, export ZIP contains the RealSense files; docs link test | This Mac |
| 2. Local Studio, simulated | `tests/studio_browser_harness.py` gains a simulated D455 choice backed by the fake. Driven through the Aside browser: connect → calibrate → record (Annotation handoff) → review → keep → export. Then the `docs/13` ZIP check with its new `realsense` branch. Report at `artifacts/studio-realsense-browser-smoke-<date>.md`, labeled simulated | This Mac |
| 3. Physical | The partner runs the guide's first-run checklist, including `docs/13` on the real D455 and gloves | NTU Ubuntu PC |
| 4. Labels | Guide status line and Studio docs say "not yet tested on hardware" until layer 3 reports, per `docs/11`'s rule | Docs |

## Rollout

PR from `sungman-cho/realsense-d455` into `main`. After it
is up: a drafted Slack reply for the partner thread with the guide link. The user
sends it.

## Risks to confirm on hardware

- **Which USB backend the pip wheel uses** (libusb vs kernel video driver) is not
  documented. The udev rules cover both, and `--check` prints the physical port, which
  shows the backend. If metadata is missing, episodes fail loudly with guidance instead
  of recording without camera time.
- **Same clock for color and IMU.** Color `sensor_timestamp` and IMU hardware timestamps
  are expected to share the camera's µs counter. The overlap check catches a gross
  mismatch; finer agreement is not tested.
- **CPU and GIL contention with the glove readers.** The callback only copies data, and
  encoding runs on the writer thread. The checklist requires glove `dropped: 0`.
- **IMU rates differ between D455 units.** Rates are read from the device, never
  hardcoded.
