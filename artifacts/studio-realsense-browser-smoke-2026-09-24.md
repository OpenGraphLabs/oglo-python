# OGLO Studio + RealSense D455 browser test (simulated)

2026-09-24 · Aside visible browser · `tests/studio_browser_harness.py` on port 18792. The D455 is the fake `pyrealsense2` (`tests/fake_realsense.py`) under the real `oglo.studio_realsense.RealSenseCameraWorker`. The gloves are simulated. **This does not qualify physical hardware.** The partner's first-run checklist in `examples/camera_glove/REALSENSE.md` is the hardware gate.

| Step | What happened | Result |
| --- | --- | --- |
| Connect & check | The camera list offered "Intel RealSense D455 · color + camera IMU · serial 123456789012" alongside the existing choices. Connect showed `123456789012 · 1280×720`, "Live · 9 ms ago", and both glove counters advancing | Pass |
| Calibrate | Chose **Use saved calibration** for both gloves | Pass |
| Set up button | Skipped the pedal and continued with on-screen controls | Pass |
| Record | Chose **Annotation handoff**; the page said "Native frame timing is available for this adapter." Recorded about 3.5 s, then Stop | Pass: 115 frames, 3.8 s |
| Review | Played the take in the page: 1280×720, readyState 4, currentTime advanced to 1.19 s of 3.83 s, no media error. Kept it | Pass |
| Export | "1 kept episode · Annotation handoff ready"; the ZIP downloaded and its copy was saved under `exports/` | Pass |

The `docs/13_localhost_ui_test.md` ZIP check, run exactly as written, printed `ep_a3c47eb8ef954914 realsense 115 frames: PASS`. That covers CRC, every SHA-256, and the new RealSense branch: the accel, gyro and calibration files are present, and every timestamp row has an integer `device_timestamp`.

What the episode manifest recorded:
- `frames_submitted` = `frames_decoded` = 115, `observed_fps` 30.0, `native_frames_dropped` 0.
- `accel_samples` 959 at 250 Hz and `gyro_samples` 1,535 at 400 Hz, clock domain `realsense_hw_clock`.
- Camera/glove coverage 0.99336.
- `delivery_validation`: `source_archive` ready, `annotation_handoff` ready, `og_center_postprocessing` "blocked: camera lacks native OVISION stereo, IMU, and calibration".

The coverage figure confirms a fix made during this work. The worker now stops a take the moment the shared stop event fires, not when Studio later calls `finish()`. Before that fix, a RealSense take recorded past the gloves' end and fell below Studio's 90% coverage rule.

The simulated video, sensor files and ZIP stayed in the session scratchpad and were not added to the repository. Still unverified, and only the physical run can check it: the real `pyrealsense2` wheel's USB backend and metadata; whether color and IMU really share one clock on a D455; USB bandwidth with the gloves attached; and the IMU rates of the lab's unit.
