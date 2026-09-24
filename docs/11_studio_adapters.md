# Extending OGLO Studio inputs and cameras

Studio's first built-in input path accepts USB buttons that emulate keyboard keys. A different button protocol or camera can be added without changing the SDK's RAW/CLEAN recording format. This page describes the current extension seams and the evidence needed before calling a device supported.

## Button input contract

The page maps key codes to five state-checked actions: calibrate, start, stop, keep, and discard. F9/F10/F11 are defaults, not hard-coded pedal identities. Browser key events require the page to stay focused. A device that emits HID or serial messages needs a small native helper or browser adapter that translates its press/release events to those actions.

An adapter should report:

| Field | Meaning |
| --- | --- |
| `source` | `external` for a native helper, or `keyboard` for the built-in path |
| `control_id` | A stable ID for a physical switch, scoped to that device |
| `host_received_ns` | Host monotonic time when the adapter received the event |
| `pressed` | True for a new press, false for release |
| `connected` / `error` | Device availability and failure state, when the protocol exposes it |

Adapters should send actions only on a fresh press edge, after a release. Do not send repeated Start/Stop requests while a switch is held. Read `GET /api/status` before deciding which action a switch means. Studio rejects actions that are not legal in its current state with HTTP 409. POST JSON to `/api/calibrate`, `/api/start` (task, profile, optional mapping, `source: "external"`), `/api/stop` (`source: "external"`), or `/api/episodes/{id}/selection` (`selection: "kept"` or `"discarded"`, `source: "external"`). Start/Stop/selection may include `input_event: {"control_id": "pedal-1", "host_received_ns": 123456789}`; the server also records its own action receipt time. Only send `host_received_ns` when the helper really measured it on the same host monotonic clock. The service binds to loopback; do not expose those control routes on a network interface.

`POST /api/connect` is for gloves and cameras. The built-in **Connect / test USB button** tests a keyboard-style device in the browser. A native helper manages its own HID/serial connection and should show that status in its own setup or a contributed UI adapter. The base service does not decode unidentified HID reports. Document the device's vendor/product ID, protocol, report format, press/release behavior, permissions, and disconnect behavior with any new adapter. Keep its dependency optional.

On macOS, Windows, and Linux, first check whether the device already supports keyboard emulation and test it in the page. For a native adapter, follow the device's documented driver/permission requirements on each platform; do not grant broad USB access merely to make discovery succeed. Verify reconnects and that a held switch cannot trigger a second action after the state changes.

## Camera adapter contract

`oglo.studio.Studio(root, camera_factory=...)` accepts a factory called with the selected camera index for standard capture. For an OVISION preview choice on macOS, the factory also receives `mode="ovision_left"` or `"ovision_right"` and the AVFoundation device `name`. The built-in native Linux path uses `NativeOvisionCameraWorker` with `mode="ovision_native_left"` or `"ovision_native_right"` and a `/dev/video*` node. A camera instance supplies:

- `index`, `error`, `native_device_timestamps`, and `postprocessing_capable` properties;
- `preview() -> bytes | None` (JPEG), `live_status() -> {ready, age_ms, error}`, `begin(folder, stop_event)`, `finish() -> dict`, and `close()`;
- exclusive camera reads while connected. `begin` receives the episode's `camera/` folder and shared stop event. Device failure sets the event and an error. `finish` waits for writers to close and returns metadata only after the video and timestamp sidecars are complete.

The returned metadata names session-relative `video` and `timestamps` files; `frames_submitted`, `first_host_received_ns`, and `last_host_received_ns` are required. Keep a playable MP4 for the review UI. Each timestamp row needs consecutive `frame_index`, integer `host_received_ns`, and `device_timestamp`, `device_timestamp_unit`, `device_clock_domain`, and `device_timestamp_meaning`. Unknown native time must be JSON `null`. Set `native_device_timestamps = True` only when every recorded frame has actual camera time with unit/domain/meaning. Studio's annotation profile checks those fields and refuses a webcam that cannot provide them.

Preserve additional native video, IMU, stereo, and calibration sidecars in the same episode folder; the source inventory and archive include them. Write camera identity, resolution, codec, calibration provenance, and any known intrinsics/eye order in adapter metadata. Do not derive a device clock from nominal FPS. The provided `CameraWorker` saves packed OVISION stereo video on macOS but has host timing only; its `postprocessing_capable` is false. The native Linux worker uses the pinned SyncField adapter and sets `postprocessing_capable` true only for the path that writes packed stereo H.264, exposure/IMU sidecars, flash calibration, and a sync point. Studio validates those files before marking an episode's `og_center_postprocessing` sensor profile ready. The UI previews and reviews one eye without cropping the saved stereo source. [`examples/camera_glove/ovision.py`](../examples/camera_glove/ovision.py) remains the standalone reference.

## Verification before listing compatibility

Add fake-device tests for press/release, duplicate and held reports, disconnect, invalid state, calibration, clean stop, partial capture, and export inclusion. Run the full visible setup-to-export journey with Aside, then repeat it on the actual OS, switch, gloves, and camera. Save a short source archive and verify its ZIP checksums, video frames, timestamp rows, glove replay, and calibration. For a native camera, include a fixture with real device timestamp semantics and a negative fixture with missing timing. Label a combination **tested** only after the physical capture/export check; otherwise label it **community reported** or **untested**.

This adapter seam creates source archives and the requested timing gate. Production platform ingestion and customer-specific formats require a separate versioned importer and acceptance fixture.
