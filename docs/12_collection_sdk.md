# Collect camera and glove data from Python

Use `oglo.collection.Collection` when you want the same workflow as the [Studio web page](10_studio.md) in a Python script. One `Collection` owns the camera and **both USB gloves**, so preview and recording do not compete for device samples. It saves a take, checks the files, and exports the takes you choose to keep. You do not need FastAPI or a running Studio server.

## Install

| Computer and camera | Install from this repository |
| --- | --- |
| Webcam on macOS, Windows, or Linux; OVISION on macOS | `python -m pip install -e '.[collection]'` |
| Native OVISION on Linux | `python -m pip install -e '.[collection,studio-ovision]'` |

Native OVISION needs Python 3.12+, `v4l2-ctl` (`v4l-utils`), SyncField 0.8.14, and valid camera flash calibration. The `collection` extra supplies camera and video dependencies; the `studio` extra adds the optional web page. macOS OVISION saves both eyes but lacks native camera exposure and IMU data.

## Your first timed take

Connect exactly one left and one right OGLO glove and one camera to the same computer. Close other apps using them. Place the camera where it sees both hands and the task.

```python
from oglo.collection import CameraSelection, Collection

profile = "source_archive"  # use "og_center_postprocessing" only with native OVISION on Linux

with Collection("captures/studio") as collection:
    cameras = collection.devices()["cameras"]
    for number, choice in enumerate(cameras):
        print(number, choice["label"])
    number = int(input("Choose the camera number: "))
    camera = CameraSelection.from_choice(cameras[number])
    collection.connect_camera(camera)  # finds and checks both glove sides

    input("Wear both gloves. Keep hands clear; press Enter for a fresh 5-second sweep.")
    collection.calibrate(threshold=70)
    input("Aim the camera at both hands and the task; press Enter to record.")
    episode = collection.take("Pick up a cup and put it down", seconds=10, profile=profile)

    video = collection.review_video(episode["id"])
    print("Open and watch this video before deciding:", video)
    keep = input("Keep this take? [y/N] ").strip().lower() == "y"
    collection.select(episode["id"], "kept" if keep else "discarded")
    if keep:
        print("Exported:", collection.export(profile))
```

A fresh sweep **replaces the baseline stored on each glove**. If both existing calibrations still fit and `connect_camera()` returns state `"ready"`, you may deliberately reuse them by omitting `calibrate()`. Check live fingertip response before recording. A take remains **unreviewed** until you call `select()`; `take()` never keeps it automatically in the example above.

Choose the camera by its displayed label, not by assuming index 0 is OVISION. On macOS, `ovision_left` and `ovision_right` choose one preview eye while saving packed stereo video. On Linux, `ovision_native_left` and `ovision_native_right` use the full native OVISION capture path. To choose known glove ports, pass `left_port=` and `right_port=` to `connect_camera()`; `devices()["gloves"]` lists verified sides and ports.

## Start and stop yourself

Use these calls when a person or pedal decides when the take ends:

```python
started = collection.start("Pick up a cup", profile="source_archive")
# Perform the task, then stop from your program's control flow.
collection.stop()
episode = collection.wait(started["current"])
if not episode["complete"]:
    raise RuntimeError(episode["error"])
```

Use `wait(episode_id, timeout=...)` if finalization may take more than its 60-second default. The `with Collection(...)` block finishes an active take before releasing devices. For an incomplete take, inspect its `manifest.json` under `captures/studio/ep_.../`; incomplete takes cannot be kept or exported.

## Which export profile?

| Profile | Use it for |
| --- | --- |
| `source_archive` | A portable recording with video, host timing, and both gloves. |
| `annotation_handoff` | A recording with real native camera frame time. |
| `og_center_postprocessing` | A Linux native OVISION recording with packed stereo, camera exposure/IMU files, calibration, and both gloves. |

`export(profile)` includes only complete, kept takes that passed that profile. It verifies the saved file inventory and SHA-256 checksums, writes a ZIP under `captures/studio/exports/`, and returns its path. Keep the whole ZIP for downstream work.

The OG Center profile checks **sensor information**, not whether hands, objects, and contact are visible. It does not upload to OG Center; that import is a separate workflow. For exact fields and units, see the [data specification](09_data_specification.md). For camera or button adapter development, see the [adapter guide](11_studio_adapters.md).
