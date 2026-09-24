# Record with OGLO Studio

OGLO Studio is a local web page for recording **two USB OGLO gloves and one camera on the same computer**. It guides you through connection, calibration, recording, review, and export. You do not need `npm run dev` or a second server.

## Choose your camera setup

| Setup | What Studio saves | Export to choose |
| --- | --- | --- |
| USB webcam on macOS, Windows, or Linux | Video, host timing, both gloves | **Portable source archive** |
| OVISION on macOS | Both eyes in one video; host timing; both gloves | **Portable source archive** |
| Native OVISION on Linux | Original stereo video, camera exposure timing, camera IMU, calibration, both gloves | **OG Center sensor source** (experimental) |

For OVISION, **left/right eye selects the preview and review view**. New recordings keep both eyes in the source video. macOS capture cannot save OVISION's native exposure/IMU metadata, so it cannot use the OG Center sensor profile. The Linux native path needs the camera's valid flash calibration and stereo metadata. Its code has simulated-device tests; physical Linux capture is still awaiting validation.

## Prepare and start

1. Connect both gloves with USB data cables and connect the camera to the same computer. Close OGLO Viewer and other apps using those devices.
2. Put the camera where it clearly shows **both hands, the objects, and the contact surface**. Check lighting and the selected lens before recording.
3. From this repository, install and start Studio:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -e '.[studio]'
   oglo doctor
   oglo studio --output ./captures/studio
   ```

   On Windows PowerShell, use `py -3 -m venv .venv`, then activate with `.venv\Scripts\Activate.ps1`. For **native OVISION on Linux**, create the environment with Python 3.12 or newer (for example, `python3.12 -m venv .venv`), install `v4l2-ctl` (`v4l-utils`), and install `'.[studio,studio-ovision]'` in place of `'.[studio]'`.

4. Open **http://127.0.0.1:8765/** in your browser. Keep the terminal running while you collect data.

If `oglo doctor` reports a failure, resolve the glove connection before recording.

If port 8765 is in use, run `oglo studio --port 8766 --output ./captures/studio` and open **http://127.0.0.1:8766/**. A `501 Unsupported method ('GET')` page means that another program is serving that address; use Studio's printed URL. Restart Studio after installing updated code, once any active take has finished.

## Complete one take

1. **Connect & check.** Click **Refresh devices** if needed. Select the camera and the left and right OGLO gloves, then click **Connect selected devices**. Check the live video and make sure both glove frame counters advance. Briefly touch a fingertip on each glove; taxels should light up. Zero contact at rest is normal.
2. **Calibrate.** Choose **Use saved calibration** only if both verified baselines still fit how you are wearing the gloves. Otherwise run a fresh five-second sweep: keep both hands clear of everything and repeatedly open and close them. A fresh sweep **replaces the baseline stored on each glove**. Afterward, touch a fingertip on each glove again to check the response.
3. **Set up button (optional).** Test a keyboard-style USB pedal and assign its keys, or continue with the on-screen controls. Keep the page focused when using a keyboard pedal.
4. **Record.** Enter a task description and choose a [delivery profile](#delivery-profiles). Click **Start recording**, perform the task with your hands in view, then click **Stop**. If the task should involve touch, watch for taxel response during the take. Wait for file validation to finish before starting another take.
5. **Review.** Select the take and play its video. Check that the hands, task, and important contacts are visible. For OVISION, review shows the selected eye; use **Download original packed stereo video** if you need to inspect both eyes. Choose **Keep** or **Discard**. An incomplete take cannot be kept.
6. **Export.** Open **Export**, then click **Download dataset ZIP**. Only complete, kept takes that satisfy the selected delivery profile are included.

## Delivery profiles

| Profile in Studio | When it is available | What the check means |
| --- | --- | --- |
| **Portable source archive** | Any supported camera | Video, camera host times, both glove recordings, calibration, and checksums were saved and validated. |
| **Annotation handoff** | A camera adapter with real per-frame device time | Native camera timing is present in addition to the source files. |
| **OG Center sensor source** | Native OVISION on Linux; experimental until physical testing | Packed stereo video, native exposure/IMU files, stereo calibration, and both gloves are present and validated. |

**OG Center sensor source checks the recorded sensor information.** It does not confirm that the footage shows a useful task or that touch happened. It also does not upload to OG Center; importing the ZIP into OG Center is a separate workflow. Try a short native recording and review its source files before relying on it for a larger collection.

**A few terms:** A *taxel* is one touch-sensing square on a glove. **RAW** keeps the original sensor counts; **CLEAN** subtracts the saved baseline and applies the contact threshold. *Host timing* marks when the computer received a camera frame; *native exposure timing* comes from the camera itself. *Packed stereo* stores the left and right eye side by side in one video frame.

## Find your files

Studio saves each take under `./captures/studio/ep_.../`. The **Review** page creates a browser-playable copy under `./captures/studio/review/`; that copy is separate from the source recording. Exported ZIPs are saved under `./captures/studio/exports/` as well as downloaded by the browser.

Keep the **whole ZIP**. It contains a manifest, file inventory, checksums, camera video and timestamps, and both gloves' original RAW and derived CLEAN tactile data, wrist motion, and calibration. Native Linux OVISION takes also contain stereo exposure, camera IMU, and camera calibration files. If you need exact field names and units, see the [data specification](09_data_specification.md).

## Optional USB pedal

A pedal that sends keyboard keys can control Studio when the page has focus. Click **Connect / test USB button** and press each pedal button. If a key appears, click **Change key** beside an action and press that button. Defaults are F9 for Start/Stop, F10 for Calibrate/Keep, and F11 for Discard. A one-button pedal can control Start/Stop while Keep/Discard stay on screen.

If no key appears, check the pedal's keyboard-emulation mode and your operating system's device settings. HID or serial pedals need a separate adapter; the browser cannot identify or read an unknown pedal protocol automatically. See the [adapter guide](11_studio_adapters.md) if you are building one.

## If something does not work

| What you see | What to check |
| --- | --- |
| Camera or glove missing | Reconnect its USB data cable, close other apps using it, then click **Refresh devices**. Both gloves must be connected at the same time. |
| Camera preview is dark or shows the ceiling | Check the selected camera/eye, remove any lens cover, improve lighting, and aim at the hands and work surface. |
| Taxels stay at zero when you touch a glove | Check glove fit and saved calibration, then run a fresh sweep if the baseline no longer fits. Check fingertip response again before recording. |
| **Start recording** is disabled | Finish device check and calibration; check all live streams. For **OG Center sensor source**, select a native OVISION option on Linux. |
| Capture needs attention | Read the error shown for that take. Reconnect any stopped device, run a new short take, and keep only a complete recording. |
| Error mentions `no #STATUS` after Stop | A glove did not answer the final status check. Reconnect it, verify both glove streams are live, and record a new short take. The failed take is incomplete. |
| Review video will not play | Use **Download original video** to inspect the source file. The Review copy may still be preparing. |
| Export is disabled or has zero takes | Keep a complete take and choose a delivery profile that the take passed. |

For scripted collection without the web page, use the [collection SDK guide](12_collection_sdk.md). For raw glove errors, see [troubleshooting](05_troubleshooting.md).

For a repeatable physical test of this page and the exported ZIP, follow the [localhost UI test checklist](13_localhost_ui_test.md).
