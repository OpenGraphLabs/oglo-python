# Local collection page

OGLO Studio runs on your computer and records one camera plus a **required left/right pair of USB OGLO gloves**. It keeps video, original RAW tactile data, derived CLEAN tactile data, motion streams, calibration, timestamps, and a validated file inventory per episode. The guided page lets you repeat takes with a USB foot pedal or another button that sends keyboard keys. A single glove cannot be connected or recorded in Studio.

## Start

```bash
python -m pip install -e '.[studio]'
oglo doctor
oglo studio --output ./captures/studio
```

Open `http://127.0.0.1:8765/`. The server binds only to your computer's loopback interface. Chrome, Edge, Safari, and Firefox can use the keyboard input path; device-specific HID/serial adapters are future extensions. The page and button input must remain focused for keyboard-emulating pedals.

Connect both gloves and the camera to the same computer. Close other apps that are using them. In step 1, click **Refresh devices** if needed, select the USB camera (such as OVISION) and each OGLO glove, then click **Connect selected devices**. Studio identifies glove sides from the devices and checks them again when connecting. Camera names appear where the OS exposes a reliable index; otherwise select an index and verify it in the live preview. The page shows the left/right glove serials, a live camera preview, and an 80-taxel tactile display for each hand. Confirm that both glove frame counters advance, then briefly touch a fingertip on each glove and check that taxels respond. A well-calibrated hand at rest may show zero contact while still streaming. A camera or glove that disconnects ends or invalidates an active episode; reconnect before the next take.

## Calibrate and collect

In step 2, explicitly choose **Use saved calibration** if both verified baselines still match the current fit, or start a fresh five-second sweep. For a fresh sweep, set a contact threshold (70 ADC counts by default, matching OGLO Viewer), wear both gloves, keep hands clear of objects, and repeatedly open and close both hands without touching anything, including fingertip to fingertip. The page counts down while both gloves sweep together, applies the threshold to both gloves, verifies the saved recipes, and shows a per-taxel spread map for each hand (green below 50 counts, orange 50–149, red 150 or more). The threshold gates derived CLEAN contact values; recorded RAW measurements remain unchanged. A large spread can reduce light-touch sensitivity in that area; repeat the sweep if the result does not fit the task. Briefly touch a fingertip on each glove to check the live response before continuing. A new sweep replaces the baseline stored on each glove. Calibration is snapshotted with each episode; recalibrate when glove fit changes, not automatically for every take.

Step 3 maps and tests a pedal, or lets you continue with on-screen controls. In step 4, enter a task description and start recording. The camera and both tactile displays remain live during collection. Studio switches both gloves to RAW mode before capture, so the original ADC values are saved. The SDK derives CLEAN tactile data from each saved baseline and threshold. While recording, use **Stop** or the mapped pedal button; wait for finalization before keeping or discarding. An intentional stop is valid only when video, timestamps, both glove replays, and coverage checks pass. Incomplete recordings stay on disk for diagnosis and cannot enter the ordinary export. Step 5 exports the kept pair episodes.

The dataset ZIP contains only complete, kept pair episodes. Each episode includes `manifest.json`, `inventory.json`, `camera/video.mp4`, `camera/timestamps.jsonl`, and both hands' schema-3 SDK episodes (including RAW/CLEAN, wrist streams, and calibration). `dataset.json` lists included episodes. Keep the whole extracted directory; do not send only the MP4 or a tactile CSV.

## Connect a USB pedal or button

Many USB pedals emulate keyboard keys. Press each physical button in **Connect / test USB button**. If the page shows a key code, click **Change key** on an action and press that button. The default three-button mapping is:

| Key | While ready | While recording | While reviewing |
| --- | --- | --- | --- |
| F9 | Start | Stop | — |
| F10 | Calibrate | — | Keep |
| F11 | — | — | Discard |

The mapping is stored in that browser and copied into each episode manifest. Test it before collecting and keep the page focused. Key auto-repeat is ignored, a release is required before re-arming, and rapid presses are debounced. If the page loses focus during recording, Studio stops that episode for review; reconnect/arm before the next take. A one-button device can map Start/Stop and use on-screen Keep/Discard; this first version does not assign a long-press gesture.

If no key appears, check the pedal manual or operating-system device settings for its input mode. Some pedals expose HID or serial reports rather than keyboard events; they need a device-specific adapter. Do not assume that a visible USB device is compatible with this keyboard path. When reporting a compatible device, include its model, mode, host OS, browser, emitted key codes, and whether press/release and reconnection were tested. Keep a community compatibility list with **tested**, **community reported**, or **untested** status.

- **macOS:** Check System Settings → Keyboard and any vendor pedal configuration tool. If the pedal sends a key to another text field, it should appear in the page's input test when the page has focus.
- **Windows:** Check Device Manager and the pedal vendor's configuration software. Select keyboard-emulation mode if the device offers it, then use the page's input test.
- **Linux:** Check the desktop input settings or the vendor's documented mode. Keyboard-emulating pedals should work in the focused browser without giving the webpage raw USB device access. HID/serial adapters may need device permissions, which must be documented for each adapter rather than applied broadly.

Avoid keys that the browser or operating system reserves. Studio ignores pedal actions while an input field is focused, so finish editing the task description and click elsewhere before collecting. The keyboard path cannot verify device identity, physical disconnection, or background operation.

## Data quality and delivery limits

The **Delivery profile** selector checks camera timing capability before recording. The ordinary webcam adapter saves a host read-start and host receipt time for each frame. It has no portable native camera exposure timestamp, so `device_timestamp` and related fields are `null`. The annotation handoff discussed with the research partner asks for a native camera timestamp as well as a host timestamp. Webcam episodes remain **source archives**, not annotation-ready under that stricter requirement. Use a camera adapter that supplies real native frame timing before claiming that handoff. Never infer device time from frame index or nominal FPS. The MP4 uses a requested playback rate that a webcam may not actually deliver; use recorded frame timestamps for timing analysis.

Glove rows retain the device counter and host arrival timestamp. Camera and glove host times on one computer allow approximate alignment; they do not prove exposure-level synchronization. `alignment_validated` remains false. Studio checks decoded frame counts, ordered timestamp rows, glove replay and RAW mode, maximum stream gaps, common coverage, and capture boundaries. The SDK also checks device status, loss, and calibration. The ZIP includes SHA-256 checksums for source files, the manifest, and the inventory. Keep the original files for any downstream import.

This ZIP is a portable source package. Production platform ingestion and customer-specific dataset formats require a separately tested importer and additional downstream checks. See [the data specification](09_data_specification.md) and [the implementation audit](../artifacts/recording-delivery-contract-audit.md).
