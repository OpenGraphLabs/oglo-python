# Record a full session for OpenGraph

The SDK talks to the gloves. Your camera is your own.
[`examples/03_full_session.py`](../examples/03_full_session.py) shows both gloves,
an editable camera interface, frame timestamps and one session manifest. It is a
collection example, not a firmware fix or a hardware qualification. Review the
[candidate status](08_candidate_status.md) before collecting research data.

```bash
# Install the maintainer-supplied SDK candidate first; the example needs rc4's stop_event.
python3 -m pip install opencv-python                    # optional webcam adapter
python3 examples/03_full_session.py --seconds 120        # webcam 0, two minutes
python3 examples/03_full_session.py                      # until Ctrl-C
python3 examples/03_full_session.py --camera fake        # real gloves, synthetic camera
```

The fake camera produces timestamp rows but no video file. Its manifest has
`synthetic_camera=true`; it is useful for checking the flow, not research evidence.

## What a session is

```text
sessions/session_20260920T101500Z/
  session.json                   identity, times, file references, completion/errors
  left/ep_0001/                  unchanged oglo.record() output
  right/ep_0001/                 unchanged oglo.record() output
  camera/video.mp4               absent when using the fake camera
  camera/video.timestamps.jsonl  one line per submitted video frame
  markers.jsonl                  prompt times, NOT measured physical contact times
```

The two glove episodes use the [SDK recording format](04_recording.md). An
initial `complete=false` manifest is published before camera setup or RAW changes.
Startup/capture/cleanup failures remain in `errors` and the CLI exits nonzero.
Each episode may still be useful even when the overall session is incomplete.

## The camera contract

Adapt `FrameSource.open/read/close` and, when necessary, `VideoSink`. All camera
operations and cleanup belong to one worker. `read()` must have a bounded timeout
in your real adapter; OpenCV's generic webcam API cannot guarantee one on every OS.

```python
class FrameSource(Protocol):
    def open(self) -> CameraInfo: ...
    def read(self) -> Optional[CameraFrame]: ...
    def close(self) -> None: ...
```

For an ordinary webcam, return `CameraFrame(image)`. The recorder calls
`time.monotonic_ns()` immediately after `read()` returns, before encoding or disk
writes. The OpenCV reference adapter stamps inside `read()` at the same boundary.

```json
{"frame_number": 0, "host_t_ns": 123456789000, "wall_ns": 1790000000000000000, "device_timestamp": null, "device_timestamp_unit": null, "device_clock_domain": null, "device_timestamp_meaning": null}
```

If your camera has a native timestamp API, the following is **pseudocode** to adapt
using your camera documentation; these are not OpenCV or vendor-specific methods:

```python
packet = my_camera.wait_for_frame()
received_ns = time.monotonic_ns()  # before image conversion or encoding
return CameraFrame(
    image=packet.to_numpy(), host_t_ns=received_ns,
    device_timestamp=packet.timestamp_us,
    device_timestamp_unit="us", device_clock_domain="camera_boot",
    device_timestamp_meaning="exposure_start",  # ONLY if the camera documents this
)
```

Leave native fields `None`/JSON `null` when unavailable. Preserve actual units and
clock domain. Never invent device time from nominal FPS, frame number,
`CAP_PROP_POS_MSEC` or `time.time()`. Never substitute it for host time.

Camera and glove `host_t_ns` use **the same PC clock**, but observe different
boundaries: camera read-return can include buffering/decoding latency, while the
SDK stamps a USB receive boundary. Neither guarantees exact exposure/sensor time.
`frame_number` is zero-based video frame order. Keep the timestamp sidecar with the
video: the encoded MP4 has nominal playback FPS even if frame arrival is irregular.
Do not align the recordings using `frame_number / fps`.

The reference adapter accepts a UVC webcam. For an OVISION exposing a side-by-side
3840x1080 UVC mode, request `--width 3840 --height 1080`, then verify the actual mode
and image with your hardware. Driver acceptance of a setting is not a calibration
or stereo-sync guarantee. Encoded packet passthrough requires its own adapter and
correct presentation-frame mapping; `ffmpeg -c copy` alone does not create that map.

## Three rules

**Capture raw and preserve existing zero calibration.** The default changes clean
gloves to RAW and restores their original clean mode/threshold afterwards. It
never calls `zero()`. RAW retains ADC values for later baseline analysis; a clean
recording has already had baseline/threshold processing applied and cannot recover
all raw values. `zero()` itself stores a baseline; it is not applied while the
stream is RAW. `--stream keep` explicitly preserves the current mode instead.
Both paths retain mode/threshold metadata; counts are not Newton force. Baseline
analysis and the suitable processing route must be agreed with OpenGraph.

**One host clock.** Keep the camera and both gloves on the same PC. Host timestamps
use monotonic nanoseconds, while `wall_ns` is the separately observed wall clock.
An approximate date can be reconstructed from the session's start anchor:

```text
wall_seconds ~= started_wall + (host_t_ns - started_monotonic_ns) / 1e9
```

This is a wall-clock estimate, not precise time synchronisation. The two anchor
reads are not atomic and wall clocks can be adjusted. Do not reset each stream's
first timestamp to zero or compare device-clock origins across gloves/cameras.
Separate PCs require a separately established clock mapping.

**Tap visible taxels when prompted.** The opening prompt waits for a saved camera
frame and both SDK capture-start metadata files. Tap a visible taxel three times
on each hand, one hand at a time. Timed sessions longer than ten seconds also
prompt near the end. `markers.jsonl` records when prompts appeared, not when you
actually touched anything. Find the corresponding physical contacts in video and
tactile data to estimate residual offset and drift. This is why
`alignment_validated` remains false. Preserve both stream timelines and inspect
visibility, timing coverage and alignment before a larger collection campaign.

## Failure handling and what to send

Either glove failing, unexpected camera EOF, or camera inactivity requests a stop
for the other recorders. Ctrl-C and an external `stop_event` also seal the session.
Restoration is attempted for every changed glove, even when later setup fails;
restoration failures are recorded and prevent a successful CLI exit. Fix reported
settings before using the glove again. A blocked camera is reported after bounded
waiting; its worker retains exclusive ownership of its handles until the driver
returns, and the session is incomplete. It is never closed concurrently by the
main thread.

Send the whole directory, including partial files when reporting a failure. Check:

- session `complete`, `errors`, `stop_reason`, and `synthetic_camera`;
- each glove's `complete`, `stream_clean`, recorded identity and loss counters;
- decoded video frame count against timestamp-row count;
- overlapping capture ranges and visible contact events for **both** hands.

A cancelled session can contain complete, usable shortened episodes; its
`stop_reason=cancelled` does not demonstrate the requested duration. Session
`complete=true` means the recorder observed no reported failure, not that the
camera dropped no exposures, that encoded video has been independently verified,
or that synchronisation/force/3D accuracy is validated. `--camera fake` never
produces a research-ready video even if its software checks succeed.

This is an external handoff format, not an automatic production Clip import.
Single-webcam data does not satisfy the existing stereo+tactile processing route.
OpenGraph must review a sample and define the import/annotation path. If metric
3D is needed, also agree on camera intrinsics/distortion, geometry and required
stereo/depth/camera-IMU data; a glove IMU is not a camera IMU.
