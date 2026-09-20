# USB webcam + OGLO

Record a webcam and one or two gloves on **the same computer**. You get video,
frame timestamps, glove recordings in JSONL.

Using an OVISION stereo camera? Follow the [OVISION guide](OVISION.md) instead.

A webcam gives RGB video. This example does not collect depth or camera calibration.
Use a camera-specific SDK if your project needs those.

## 1. Install and connect

Use Python 3.10+ in a virtual environment. From the repository root:

```bash
python -m pip install -e .
python -m pip install -r examples/camera_glove/requirements.txt
```

Connect the camera and gloves by USB, close other apps using them, then run:

```bash
oglo doctor
```

Resolve errors before recording. Allow camera access in your OS when asked.
`--camera 0` may select the laptop camera; try `--camera 1` for another device.
Use the preview to check that hands and contact surfaces are visible.

If you installed a candidate wheel, use its matching source/examples archive and
skip `pip install -e .`. See [package setup](../../docs/08_candidate_status.md#evaluate-the-package).

## 2. Record video and gloves together

```bash
python examples/camera_glove/capture.py \
  --camera 0 --seconds 30 --fps 30 --preview \
  --output captures/cup_001 \
  --task "Pick up a cup and put it down"
```

| Option | Use |
| --- | --- |
| `--pair` | Record both hands |
| `--serial YOUR_GLOVE_SERIAL` | Select one glove by the serial from `oglo doctor` |
| `--output` | A new folder for this attempt |
| `--task` | Describe the action you will perform |
| `--fps` | Requested camera rate and MP4 playback rate; the camera may ignore the request |

Use `--pair` or `--serial`, not both. The script reads the existing glove recipe
and preserves calibration, mode, threshold, and rates.

Make a few visible fingertip taps near the start and end to help check timing
later. Let recording and file checks finish. Pressing `q` or Ctrl-C stops early
and leaves the session incomplete.

## 3. Keep the complete output folder

For one left glove:

```text
captures/cup_001/
  manifest.json           session details, file paths, and completion status
  camera/
    video.mp4
    timestamps.jsonl      one timing row per video frame
  gloves/left/
    calibration.json      existing GET ZERO recipe
    ep_0001/              meta.json + tactile/motion JSONL + calibration
```

With `--pair`, there is also a `gloves/right/` folder.

Each episode contains the backend's sensor JSONL files. See the
[file reference](../../docs/09_data_specification.md#glove-files)
for RAW/CLEAN files, channels, and units.

Check `complete` in the session's `manifest.json`. Send the **whole folder**,
optionally zipped. Keep video, calibration, timestamps, and JSONL together,
including empty magnetic files. Put extra notes in a separate file. Keep failed
attempts separately; do not edit status flags or alter the original data.

## 4. Which fields combine camera and OGLO data?

Compare arrival times on the same host:

| Data | Arrival-time field |
| --- | --- |
| `camera/timestamps.jsonl` | `host_received_ns` |
| Glove JSONL | `capture_ns` |

These are integer nanoseconds. For example, a camera frame at `125000000000` and
a tactile sample at `125002000000` arrived **2 ms apart**.

Frame indices are separate: camera frame 12 need not match glove sample 12.
Each hand and sensor stream has its own sample count and rate.

### Run the offline join preview

After recording, no hardware is needed:

```bash
python examples/camera_glove/align.py captures/cup_001 --max-delta-ms 50
```

This creates `alignment.preview.jsonl`. For each video frame, it finds the nearest
JSONL sample in each glove stream and saves:

| Field | Meaning |
| --- | --- |
| `row_index` | Index of the original sensor sample |
| `seq` | Its device sequence number |
| `host_received_ns` | Its arrival time |
| `delta_ns` | Glove arrival time minus camera arrival time |

A match is `null` if the stream is empty, the camera time is outside its range,
or the nearest sample is farther than the tolerance. Ties use the first row at
the chosen timestamp; equal-distance timestamps prefer the earlier one.

The preview preserves the recordings and refuses to overwrite an existing output.
The default 50 ms is a search tolerance, not a claim of synchronization accuracy.

## Timing and completion limits

**Arrival time is approximate alignment.** The webcam is timestamped when OpenCV
returns an image. Gloves are timestamped when the host receives bytes. Buffering
adds unknown delay, and several glove samples can share one timestamp.

Use recorded arrival times, not nominal FPS, video playback time, frame indices,
or different devices' clocks. Separate computers' monotonic clocks also cannot
be compared directly.

`complete: true` means recording finished, video frame counts matched, glove files
passed replay checks, and the recorded time ranges overlapped. It does not
prove exposure alignment, zero camera loss, full-duration overlap, or image quality.
`alignment_validated` stays `false`.

Camera or glove failures stop peer capture and leave an error. A hung webcam driver
can still block because OpenCV reads have no portable timeout. Encoding and disk
writes can delay camera reads. Try a short run on the intended hardware first.
The webcam video uses lossy MP4 encoding.
