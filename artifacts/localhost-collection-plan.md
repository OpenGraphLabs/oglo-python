# Localhost glove and camera collection plan

Historical planning draft · 2026-09-23. The implemented Studio now requires a left/right OGLO pair, uses five guided steps, and shows both tactile streams and the camera live before and during recording. See [current user guide](../docs/10_studio.md) for actual behavior; earlier one-glove options below are superseded.

## Outcome and scope

Launch a local web page from the SDK, connect a left/right OGLO pair, a camera, and a USB trigger, calibrate, collect repeated episodes without touching a keyboard, and export selected recordings as a dataset.

The [request](https://opengraph-labs.slack.com/archives/C0C30LKMSEL/p1790151160882389) explicitly includes ego video plus tactile data. The [earlier discussion](https://opengraph-labs.slack.com/archives/C0C30LKMSEL/p1790150350622939) confirms a three-button pedal used for start/stop and save/discard. The linked [OGLO Studio](https://oglo-viewer.vercel.app/) was inspected: it exposes glove connection, RAW/calibrated views, tactile visualization, calibration, and sensor recording. Treat it as a visual reference; its source and hardware behavior have not been verified.

Confirmed first-release scope: camera video plus glove data, with one local operator, USB gloves, one camera, configurable trigger input, guided calibration, repeated episode capture, review/keep/discard, and ZIP export. No account or cloud service is required. Camera adapter selection must satisfy the intended delivery requirements: the existing ordinary-webcam adapter supports local capture but lacks the native camera timestamps requested for annotation handoff. Preserve a separate native-camera path such as OVISION; do not silently substitute webcam capture for its stereo/timing/calibration capabilities.

Open-source requirement: users bring different pedals, USB buttons, and operating systems. A particular pedal model or host OS is not a prerequisite for building the application. Ship a keyboard-input baseline, guided mapping, connection documentation, and an extension interface for additional input protocols. Publish tested configurations separately from intended support.

## User journey

1. Run the proposed `oglo studio --output ./captures` command. It serves bundled assets on loopback and opens the page. Optional dependencies install through `oglo[studio]`; users do not need Node.js.
2. **Connect:** choose detected gloves and camera. Show glove side/serial, calibration state, camera preview, and stream health. Enter the task description and dataset name once.
3. **Connect trigger:** start with “Press a button to detect keyboard input,” so users do not need to know their device protocol. If no input appears, offer the connection guide and available device adapters. Teach each mapping and test it without starting a recording. Show the action each input will perform. Keyboard input is labeled “Input tested,” not a verified physical USB connection. Save named mappings locally and allow configuration export/import; retest before arming a new setup.
4. **Calibrate:** show “Wear gloves, touch nothing, open and close your hands.” Run the SDK's five-second sweep with countdown and completion cues. Verify the recipe for every selected glove; display a tactile preview for an operator check. Explain that recalibration replaces the stored baseline. Recalibrate on demand between episodes, not automatically before every take.
5. **Record:** check the delivery profile's required streams, timing capabilities, and calibration before arming; show missing capabilities before the user collects data. Give a short configurable countdown, then show recording state, elapsed time, stream health, and episode number. Verify RAW glove mode, snapshot the recipe and device configuration, persist episode identity and a host clock anchor, then record original samples and derive CLEAN output using that saved recipe.
6. **Stop and review:** a pedal press ends capture and starts finalization/validation. Once ready, show duration, camera playback, sample/loss summary, and Keep / Discard. Capture data is already on disk; Keep accepts it into the dataset. Discard initially excludes it with an undo option.
7. **Repeat:** Keep or Discard returns to Ready without reconnecting devices or typing. Use distinct visual/audio cues so the operator can understand transitions while looking at the task.
8. **Export:** select kept episodes that pass the chosen delivery profile and download one ZIP containing all required measurements, sidecars, metadata, and validation results. Show the local dataset path, episode count, and export size/progress. Allow explicit source-archive export for other complete recordings with their limitations; do not label them delivery ready.

## Trigger behavior

Proposed optional preset for the reported three-button pedal; setup must also accept one, two, or more buttons:

| State | Button 1 | Button 2 | Button 3 |
| --- | --- | --- | --- |
| Needs calibration | No action | Calibrate | No action |
| Ready | Start countdown | Recalibrate | No action |
| Recording | Stop | No action | No action |
| Finalizing | No action | No action | No action |
| Review | No action | Keep and return to Ready | Discard and return to Ready |

All mappings are configurable. One-button devices can use sequential start/stop/keep with onscreen calibration/discard; a fully hands-free one-button gesture scheme is a separate decision. Avoid ambiguous double-tap/hold behavior in the first version.

Use a common action interface (`calibrate`, `start`, `stop`, `keep`, `discard`) with input adapters:

- **Keyboard-emulating USB pedal:** learn key codes in the page. It requires page focus, cannot distinguish an identical key from a physical keyboard, and cannot reliably detect physical disconnection. Disable actions while editing text. On focus/control loss, disarm starts; if recording, stop gracefully after a short documented grace period and mark it interrupted for review.
- **Device-specific HID or serial button:** provide a documented adapter interface and example skeleton. Add supported profiles incrementally as contributors supply device details and verification. Use the Python service if background input or device identity is required. Enumerating a USB device is insufficient; its report format and permissions must be tested. The first release does not depend on implementing an unidentified device's protocol.
- **Onscreen controls:** route through the same action handler for setup, fallback, and testing.

Do not promise generic WebHID support for every USB pedal. Chrome blocks access to protected generic keyboard/mouse HID collections; keyboard-emulating pedals need a different input path. See [Chrome's WebHID documentation](https://developer.chrome.com/docs/capabilities/hid).

Debounce edges, ignore key auto-repeat, require release before re-arming, and reject stale/duplicate commands by action ID and expected state. A held button must not become a new action when the workflow changes state. Reconnection never restarts capture automatically.

Adapters emit normalized button press/release and connection/error events with an adapter ID, control ID, and host receipt time. Mapping and the session controller translate those events into allowed actions; adapters never write recordings or bypass state checks. Describe capabilities explicitly, including whether physical identity, disconnect detection, and background input are available. Keep any native dependencies optional.

## Connection documentation to ship

The setup guide is part of the first release, linked directly from **Connect trigger**:

1. **Quick start:** plug in the USB device, open the input tester, press/release a button, assign actions, test the mapping, and arm collection. Include one-button and three-button examples without assuming a brand.
2. **Identify input mode:** explain keyboard emulation versus HID/serial adapters in plain language. If no key appears, explain how to find the device's documented input mode and report its model/interface; do not imply automatic discovery can decode every device.
3. **Platform setup:** separate macOS, Windows, and Linux instructions for available adapters, dependencies, device permissions, and troubleshooting. Give permission instructions only for the adapter that needs them, verified on that platform.
4. **Input behavior:** document page-focus requirements, reserved key conflicts, text-field behavior, repeat/held-button handling, focus-loss interruption, and re-arming after reconnect. Background operation is an optional adapter capability, not a baseline promise.
5. **Compatibility matrix:** list input mode, device/profile, OS/browser, setup requirements, limitations, verification date, and status (tested, community-reported, or untested). Keep product claims tied to evidence.
6. **Contributor guide:** document adapter events/lifecycle, configuration schema, a minimal adapter example, fake-input tests, and the physical verification checklist. Provide an issue template for model, OS/browser, input mode, observed press/release behavior, and reproduction steps.

The keyboard path can be developed and tested with an ordinary keyboard before a pedal is available. Physical pedal compatibility remains a separate, recorded test result. Imported profiles contain configuration only, never executable code.

## Architecture and reuse

```text
Local browser: setup, previews, controls, episode list, export
       | HTTP commands + WebSocket state/preview updates
Python service: session controller, trigger adapters, dataset manager
       | single capture owner per device
OGLO SDK + camera adapter -> local recording folders -> ZIP export
```

Propose FastAPI/Uvicorn with a small bundled HTML/CSS/JavaScript frontend. Keep web and camera dependencies optional. Bind to loopback, validate request origins, and accept dataset/episode IDs rather than arbitrary client-supplied filesystem paths. One controller owns the active session; additional tabs observe or explicitly take control.

Reuse these existing components:

- `src/oglo/_device.py`: USB connection, verified zero sweep, configuration, and recording ownership.
- `src/oglo/_record.py`: bounded recording buffers, RAW/CLEAN derivation, partial-recording preservation, stop events, and finalization.
- `src/oglo/_replay.py`: saved-data validation.
- `examples/camera_glove/capture.py`: webcam capture, frame timestamps, video verification, and combined session layout.
- `examples/camera_glove/ovision.py`: separate OVISION adapter when required by the actual rig.

Integration work that cannot be treated as a thin UI wrapper:

1. The camera example is duration-based and rejects early glove stops. Extract a reusable session controller that distinguishes intentional user stop from faults. Preserve the SDK's current `stop_reason: cancelled` behavior and record the controller's reason separately; only validated user-ended captures may become complete.
2. Recording owns glove reads. Add a bounded, nonblocking observation path from the owning capture loop for preview/status. Never run a second reader against the same glove. A slow browser drops preview updates, not recorded samples.
3. Make camera capture start/stop reusable across episodes. Track readiness and first/last sample times; a shared start command does not prove simultaneous sensor capture. Isolate potentially blocking camera reads in a supervised worker process so a hung driver cannot freeze Stop or the server.
4. The backend owns state, including `Disconnected`, `Needs calibration`, `Calibrating`, `Ready`, `Countdown`, `Recording`, `Finalizing`, `Review`, and `Error`. Commands are serialized. Calibration/configuration changes are rejected during capture. Reloads reconstruct UI state from the backend; a restart never reports an unfinished episode as complete.

## Data and export contract

The recorder owns delivery completeness. The [recording-to-delivery audit](recording-delivery-contract-audit.md) records the inspected requirements, source inventory, integration gaps, and acceptance tests. The [annotation handoff request](https://opengraph-labs.slack.com/archives/C0C30LKMSEL/p1789810726242279) specifically asks for both host and device timestamps on every video frame and tactile sample. Current webcam capture writes null native camera timing and does not fully satisfy that request. Native camera timing must come from a capable adapter, never an FPS-derived substitute.

Implement a versioned capture/delivery profile that declares required streams, hands, camera capabilities, calibration, timing fields, and quality checks. Use it for preflight, capture, finalization, and export. Preserve camera-specific geometry/calibration and native stereo/IMU artifacts whenever supported or required. Persist a paired host monotonic/wall-clock anchor before streaming, all original per-sample clocks and sequence fields, device identities, task metadata, and a sealed file inventory with validation results. A profile requiring two hands or stereo cannot pass with one hand or mono video.

Preserve existing schema-3 glove JSONL, calibration snapshots, raw measurements, integer timestamps, camera video, and per-frame timing. Reuse the existing camera-session layout where applicable. Add a separately versioned dataset index and per-episode collection metadata for task text, acceptance/discard state, trigger mapping, action timestamps, calibration identity, interruption reason, and validation results. Define/version any changes needed for indefinite camera-session duration rather than silently changing the existing format.

Keep data integrity (`complete`), operator selection (`kept`, `discarded`, `unreviewed`), and profile-specific delivery readiness separate. Never relabel an incomplete capture as complete or delivery ready because the operator pressed Keep. Files from failed captures remain available for diagnosis and are excluded from ordinary dataset export.

Delivery export includes complete, kept episodes that pass the selected profile. Include an index, the original episode folders, format README, validation report, and checksums. Freeze the selection during export; write/stream ZIP files with bounded memory and ZIP64 support. Never omit calibration/timestamp sidecars or convert nanosecond integers through JavaScript floating-point numbers. CSV can be added as a derived convenience format later; JSONL remains canonical.

On Stop, validate video decoding against per-frame timing, glove replay, exact RAW/CLEAN derivation, channel/hand identity, clock continuity, per-stream gaps/loss, and required time coverage before publishing readiness. Valid files and some overlapping timestamps alone do not demonstrate full episode coverage. Record failures explicitly rather than repairing source evidence to pass.

The portable SDK archive, platform upload/registration, and processed customer delivery are separate contracts. The existing SDK manifest is not automatically accepted as a platform session descriptor. Establish a versioned importer/mapping and prove it with an exported fixture before claiming backend compatibility. Customer-format annotation, derived poses, synchronization proofs, and review remain downstream; the recorder preserves their required source inputs.

Camera and glove times provide approximate same-host alignment. Preserve the current `alignment_validated: false` semantics unless synchronization is separately measured. The UI should not claim hardware synchronization.

## Delivery sequence and acceptance

| Phase | Deliverable | Exit check |
| --- | --- | --- |
| 0. Capture-to-delivery contract | Versioned required-source profile, timing/camera capability checks, source-to-delivery mapping, reference fixture | Required fields traced to recorder outputs; missing native camera time/calibration fails the appropriate profile; consumer validation defined |
| 1. Define input support | Keyboard baseline, adapter contract, mapping presets, connection-guide outline, compatibility matrix | Input tester handles press/release and explains unsupported input without needing a particular pedal |
| 2. Local service | `oglo studio`, bundled page, state controller, mock devices, episode storage | Start, stop, reload/reconnect, and failure paths work without hardware |
| 3. Calibration and capture | Left/right pair, capable camera adapter, verified RAW mode/calibration, timing anchor, preview, clean pedal-ended episodes | Saved files pass source and delivery-profile checks; full inventory captured; no competing readers |
| 4. Hands-free collection | Trigger setup/test, saved mappings, state-aware mapping, debounce, cues, Keep/Discard, connection and contributor guides | Repeatable simulated-input flow; ten consecutive real episodes including discard/retry for each hardware configuration claimed as tested |
| 5. Dataset export | Selection, ZIP, index, checksums, validation report, recovery listing, setup guide | Record → export → extract passes replay/decode and selected profile validation; any claimed backend mapping passes its importer with the same fixture |

Focused automated checks should cover state transitions, duplicate/held trigger input, calibration failure on one hand, clean user stop versus device failure, disconnection, camera stall, disk-write errors, reloads, and export inclusion rules. Use fake devices for deterministic fault injection.

Exercise the real setup → trigger test → calibration → record → stop → keep/discard → export journey in visible Aside. Browser mocks verify the workflow but do not qualify hardware compatibility. Validate available pedal/camera/glove configurations with a sustained capture run; check bounded memory, loss counters, and exported data integrity. Record those results in the compatibility matrix. Unknown community hardware does not block core development and must not be labeled tested.

## Remaining scope and follow-up

- Device diversity is resolved as a product requirement: no fixed pedal or OS. Start with keyboard-emulating devices and document the extension path for other inputs.
- Camera plus gloves is confirmed for the first release. Deliverable timing capabilities determine the camera adapter; a webcam source archive must not be presented as satisfying the full annotation handoff when native timestamps are absent.
- A concrete import mapping and accepted fixture remain qualification work before claiming platform delivery compatibility. Local source review identified requirements and gaps; it did not execute an end-to-end delivery.
- Jiaxiao's fork URL and branch would help reuse existing work but are not a prerequisite; the inspected Slack threads mention the fork without linking it.
- Baseline trigger input requires page focus. Document background-capable native adapters as an extension rather than blocking the first version on them.

Camera capture is explicitly confirmed by the user. No hardware qualification or application implementation was performed during this planning pass. The requested `ao` skill was not found in installed skill locations; the plan was prepared directly.
