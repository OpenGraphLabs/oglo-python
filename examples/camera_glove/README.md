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

## Video codec

`video.mp4` comes from OpenCV's `mp4v` encoder unless `--codec` says otherwise.
`--codec hevc_nvenc` (NVIDIA GPU), `h264_nvenc`, `libx265` or `libx264` pipe every frame
to the system `ffmpeg` instead, and `--video-quality N` is that encoder's CRF / CQ value
(default 23; lower means a larger file). The script checks the encoder before it opens
any device. On the SC233 stereo camera through OpenCV (its default 3200x1200 mode at
30 fps), `mp4v` writes about 325 MB per 30 s, `hevc_nvenc` at 23 about 93 MB and at 28
about 50 MB. The manifest records `codec` and `video_quality`; the frame count check
and OpenCV playback work the same for every codec. None of this applies to the native
OVISION backend, which keeps the camera's own H.264 (see [OVISION.md](OVISION.md)).

## 5. Collect many episodes: `collect.py`

`capture.py` records one session. `collect.py` keeps the camera and the gloves open
and records episode after episode into one dataset tree, driven by the keyboard or a
foot switch that types the same keys. `scripts/collect.sh` runs it with this
workstation's interpreter and defaults: copy `scripts/workstation.env.example` to
`scripts/workstation.env` (ignored by git) and set the interpreter, the camera name,
the codec for the OpenCV backend and the Hugging Face repo there; the script adds
`--camera`, `--codec` and `--out` from it (your arguments win).
`--camera` takes an OpenCV index or part of the camera's V4L2 name; the name is
stable across reboots, the index is not:

```bash
scripts/collect.sh --pair --task "pick up a cup"          # both hands
scripts/collect.sh --serial OGLO-R-00001 --task "cup"     # one glove, by CONFIG serial
```

The dataset lives beside the checkouts, never inside one. The folder that holds the
main checkout is the project folder; its `hf-data/` is the one local copy of the Hub
repo, shared by every worktree. `collect.sh` records into it, `hf_upload.sh` sends it
and `hf_download.sh` fills it, so a worktree can be removed without losing data:

```
<project>/
  oglo-python/      main checkout
  my-task/          worktrees, beside it
  hf-data/          $OGLO_DATA: recordings, and the Hub repo's local copy
```

`OGLO_DATA` in `scripts/workstation.env` (or the shell) moves it elsewhere.

The scripts also put the checkout's own `src/` first on `PYTHONPATH`, so one
environment serves every checkout and worktree, whichever of them it was installed
from with `pip install -e`. A new worktree needs only its ignored file:

```bash
git worktree add ../my-task -b my-task
cp scripts/workstation.env ../my-task/scripts/    # from a checkout that already has one
../my-task/scripts/doctor.sh
```

| Key | Action |
| --- | --- |
| `g` | start an episode (the gloves stop their idle stream, read their zero table, then record). Refused while any glove has no valid sweep zero: press `z` first |
| `h` | stop and save the episode |
| `x` | stop and discard it (moved to `<task>/_discarded/`) |
| `z` | run a zero sweep on every glove (`--sweep` seconds, `--countdown` before it), then leave the glove in RAW as OGLO Studio does (the recorder keeps RAW and derives CLEAN); `--clean THR` switches it to CLEAN instead. Either mode persists on the glove for every client |
| `c` | toggle the on-screen grids between counts above the zero (clamped at 0, like the CLEAN file; the sweep zero is an envelope, so a resting hand sits below it) and raw ADC (RAW stream only). Display only: a RAW-stream episode always saves both `tactile_<side>.raw.jsonl` and the derived CLEAN file |
| `q` | quit; the dataset index and card are rewritten on the way out |

The task folder is the ASCII letters and digits of `--task`; a task written in another
script, one longer than 40 characters or one made of symbols keeps its identity through
a short hash of its text. One folder means one task: `collect.py` refuses to start when
the folder already holds episodes recorded under a different wording, so the index and
the card never merge two activities. Episode numbers count past the highest one the
task has used (`<task>/.next_session` remembers it), so an episode deleted locally after
an upload is never overwritten on the Hub by a new one under its name.

A saved episode is aligned by `align.py` running as a separate, low-priority process,
so the next `g` never waits and alignment never competes with the glove readers or the
camera for the interpreter (in-process it starved the serial port and dropped camera
frames during the next episode). `q` waits for the queued alignments; Ctrl-C during
that wait ends the child too and leaves those episodes complete but unaligned, which
the next `collect.py` run aligns before anything else (`dataset.py index` names them
until then). Ctrl-C during a recording moves that episode to `_failed/`.

Output layout, one folder per task and one numbered session per episode:

```
hf-data/
  README.md, episodes.jsonl          rebuilt by dataset.py on every quit / upload
  <task>/<task>_001/                 exactly what capture.py writes, unchanged
  <task>/<task>_001/derived/         reserved for per-episode files that come back later
  <task>/_discarded/, <task>/_failed/   never uploaded
  gloves/<serial>/                   reserved for per-glove files that come back later
```

An episode is *publishable* when `dataset.py` finds nothing wrong with it: a complete
manifest, every camera file it names present (for OVISION episodes that includes the
IMU, calibration, clock anchor and capture report), its glove folders present, and
`alignment.preview.jsonl` with exactly one row per decoded frame (`align.py` publishes
that file in one step, exclusively, so a partial or half-overwritten one never exists).
That one test decides what `episodes.jsonl` lists and what may leave the machine:
`dataset.py index --out ../hf-data` rebuilds the index from the publishable episodes and
names every folder it held back; `scripts/hf_upload.sh` (`--dry-run` to list first)
refuses while any folder under a task is not a publishable episode or any file under
a task belongs to no indexed episode, then runs `hf upload` twice: first the indexed
episodes and `gloves/` (an `--include` per episode, so a recording that starts
meanwhile is not swept up), then `episodes.jsonl` and `README.md`, so the index on the
Hub never lists an episode whose files are not there yet. The repo comes from
`OGLO_HF_REPO` in `scripts/workstation.env` (or `--repo`) and must be private: `hf
upload --private` only applies to a repo it creates, so an existing public repo is
refused before anything is sent. Needs `hf` 1.0 or newer (older ones keep only the last
`--include`); the logged-in token needs write access to that repo's organization.
Files deleted locally stay on the Hub until removed there.

`scripts/hf_download.sh` pulls the repo into the same folder, on a new machine or
after another one uploaded; `hf download` options such as `--include "<task>/*"` pass
through. It skips `episodes.jsonl` and `README.md`, which `dataset.py` derives from the
manifests (run `dataset.py index` or the next upload to rebuild them), and overwrites
local files that differ from the Hub, so upload first on a machine that records.

### Camera backend and the camera IMU

`collect.py` records the camera through one of two backends, `--camera-backend`
(default `auto`):

- **ovision**, the SDK's native OVISION worker (`oglo.studio_ovision`, what OGLO
  Studio records with) through `ovision.py`, for an OVISION-EGO-V1 (the SC233HGS
  module with H.264/YCTC firmware). One worker stays live for the whole session and
  every episode gets the same files as a single `ovision.py` run: the original
  3840x1080 H.264 (`camera/cam_ego.mp4`), the camera's own IMU and magnetometer
  (`cam_ego.imu/accel/gyro/mag.jsonl`), per-eye exposure timing
  (`cam_ego.stereo.jsonl`), the unit's calibration (`cam_ego.calibration.*`),
  `sync_point.json`, `finalization.json` and the common `timestamps.jsonl`, as
  [OVISION.md](OVISION.md) describes them. Needs Linux and
  `pip install -r examples/camera_glove/requirements-ovision.txt` (SyncField 0.8.14).
  `--codec`, `--video-quality` and `--fps` do not apply. The camera image in the window
  refreshes about once a second (the adapter decodes keyframes only) while the tactile
  grids keep their usual rate, and each episode's video starts at the first keyframe
  after `g`, up to a second after the gloves; `h` or `x` before that keyframe leaves
  nothing to keep and counts as a discard. The worker's watchdog ends an episode whose
  camera dies or delivers no frame for five seconds (it is recorded as failed, never
  saved with a video shorter than its gloves), and a camera that stops while idle is
  noticed within five seconds too; in both cases the camera is reopened, waiting for a
  replug if needed (`--camera` given as a name is resolved again, since the device can
  come back as another `/dev/video` number), while the gloves keep their idle readers.
- **opencv**: any webcam through OpenCV, encoded with `--codec`; no camera IMU. The
  idle window says so, with the reason `auto` fell back.

`auto` takes `ovision` when SyncField 0.8.14 is installed and the camera answers the
adapter's calibration read, and prints why when it falls back to OpenCV, so an
episode without camera IMU never happens silently. `episodes.jsonl` records
`camera_kind` and `camera_imu` per episode, and the dataset card describes the camera
actually used.

### Checking the taxel layout

The grids in the preview follow OGLO Studio's `drawGlove`: each finger is a 4x4
with the fingertip at the top and wire row 0 on the right; a right hand runs
thumb..pinky left to right, a left hand is mirrored. To check a glove against the
app without the camera, `scripts/taxel_map.sh --side left` prints the same layout
in the terminal with the SDK `(slot, row, col)` and Studio CSV `taxel_N` index of
the peak.

### How the gloves are driven between episodes

A glove streams about 48 kB/s over USB and Linux buffers only 4095 bytes per tty:
a host that stops reading for ~85 ms makes the kernel throttle the device. So the
gloves are never left streaming unread:

- **Idle** (between episodes): each glove has its own reader thread that drains the
  stream and keeps the last frame for the preview. The window thread never reads a
  glove.
- **Command** (`g`, `z`, quit): the reader thread is stopped first, then the stream is
  stopped, then commands are sent. `oglo.Glove.send()` is never called while a reader
  thread is alive.
- **Recording**: `capture.py` owns the gloves; `oglo.record()` reads on its own thread
  per glove and stops the stream as soon as it returns, whether it succeeded or
  raised (the SDK resumes the stream before it raises).
- **After the episode**: reader threads start again.

### When a glove stops answering

Symptoms: `no '#TZERO ' from the board within 4s`, `no #CONFIG from the board`, or an
episode marked failed with `status_error`. Check `journalctl -k` first: a line like
`xhci_hcd ...: WARN Set TR Deq Ptr cmd failed due to incorrect slot or ep state` at
the same second means the host controller failed to recover the glove's endpoint,
not the glove. That happened on every failure with the gloves on an ASMedia ASM4242
USB4 (Type-C) controller and never on the CPU's own USB ports, so plug the gloves
into plain USB-A ports on a different host controller from the camera.

Never toggle DTR/RTS or open the port at 1200 baud to "reset" a glove: that puts the
ESP32-S3 into its ROM download mode. Unplug it, wait ten seconds, plug it back in.
