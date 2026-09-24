# Test the Studio localhost UI

This checklist tests the **visible Studio page** with one camera and both USB OGLO gloves. It covers connect → calibrate → record → review → export and checks the resulting ZIP. It does not upload data to OG Center. Use a short, well-lit task with visible hand contact; a dark scene or zero tactile contact cannot qualify the recording for later post processing.

## 1. Start a fresh local session

Finish any active take and stop older Studio processes from their own terminals first. A different process may occupy port 8765, and an old Studio process may still hold the camera or gloves. Do not unplug a device during an active recording.

From an updated checkout of this repository on macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[studio]'
mkdir -p ./captures/local-ui-test
export OGLO_STATE_DIR="$PWD/captures/local-ui-test/.state"
oglo doctor
oglo studio --port 18768 --output ./captures/local-ui-test
```

For **native OVISION on Linux**, use Python 3.12+, install `v4l2-ctl` (`v4l-utils`), and install `'.[studio,studio-ovision]'` instead. On Windows PowerShell, use `py -3 -m venv .venv` and `.venv\Scripts\Activate.ps1`; set `$env:OGLO_STATE_DIR = "$PWD/captures/local-ui-test/.state"` before `oglo doctor`.

Open the exact URL printed by Studio, normally **http://127.0.0.1:18768/**. Only this Python server is needed; do not run `npm run dev`. If the port is occupied, choose another with `--port`. A `501 Unsupported method ('GET')` page means you opened a different service. On macOS or Linux, `lsof -nP -iTCP:18768 -sTCP:LISTEN` shows which process owns that example port.

**Pass:** the page opens and shows **Connect & check**. **Stop here** if `oglo doctor` reports a failed glove or the page cannot reach Studio.

## 2. Exercise the complete UI flow

| Step | Action | Pass condition |
| --- | --- | --- |
| **Connect & check** | Select the intended camera and one left plus one right glove. Click **Connect selected devices**. Touch a fingertip on each glove. | Camera preview is live; both glove frame counters advance; touched taxels light up. Both hands and the work surface are visible. |
| **Calibrate** | Explicitly choose **Use saved calibration** if the fit still matches, or run a new five-second sweep with hands clear of contact. Touch each fingertip afterward. | Both baselines show verified; fingertips respond. A new sweep replaces the baselines stored on the gloves. |
| **Set up button** | Skip a pedal or test/map a keyboard-style USB pedal. | On-screen controls remain available, or the pedal keys appear and map correctly. |
| **Record** | Enter a task description. On macOS choose **Portable source archive**; on native OVISION/Linux choose **OG Center sensor source** for the experimental sensor check. Record about 10 seconds with visible hand/object interaction, then click **Stop**. | The recording finalizes without **Capture needs attention**. Video and both tactile streams remained live during the take. |
| **Review** | Play the take. For OVISION, use **Download original packed stereo video** to inspect both eyes if needed. Click **Keep** only if the task is visible. | Review video plays; selected eye is correct; the take changes to **KEPT**. Incomplete takes cannot be kept. |
| **Export** | Click **Export kept takes**, then **Download dataset ZIP**. | A ZIP downloads, and a copy appears under `captures/local-ui-test/exports/`. |

On macOS, choosing an OVISION eye changes **preview and review only**; the saved source video should contain both eyes at 3840×1080. The **OG Center sensor source** option being unavailable on macOS is expected: this path lacks native exposure/IMU metadata. Native Linux OVISION capture is still **experimental until it passes a physical camera-and-glove test**.

## 3. Check the exported ZIP

Run this standard-library check from the repository root. It selects the newest ZIP in this test folder, verifies CRC and every declared SHA-256, and checks the camera and two glove payloads:

```bash
python - ./captures/local-ui-test/exports <<'PY'
from pathlib import Path
import hashlib
import json
import sys
import zipfile

exports = Path(sys.argv[1])
archives = sorted(exports.glob("oglo-dataset-*.zip"), key=lambda path: path.stat().st_mtime)
assert archives, f"No Studio ZIP in {exports}"
archive_path = archives[-1]
with zipfile.ZipFile(archive_path) as archive:
    assert archive.testzip() is None, "ZIP CRC failed"
    index = json.loads(archive.read("dataset.json"))
    assert index["episodes"], "No kept takes in export"
    for episode_id in index["episodes"]:
        manifest = json.loads(archive.read(f"{episode_id}/manifest.json"))
        assert manifest["complete"] and manifest["selection"] == "kept"
        for relative, expected in index["checksums"][episode_id].items():
            actual = hashlib.sha256(archive.read(f"{episode_id}/{relative}")).hexdigest()
            assert actual == expected, f"Changed file: {relative}"
        camera = manifest["camera"]
        for field in ("video", "timestamps"):
            assert f"{episode_id}/{camera[field]}" in archive.namelist()
        if camera["kind"] in {"ovision_uvc_stereo_host_timed", "ovision_native_stereo"}:
            assert (camera["width"], camera["height"]) == (3840, 1080)
        if camera["kind"] == "ovision_native_stereo":
            for relative in camera["native_artifacts"] + [camera["sync_point"]]:
                assert f"{episode_id}/{relative}" in archive.namelist()
        assert {hand["side"] for hand in manifest["gloves"]} == {"left", "right"}
        for hand in manifest["gloves"]:
            side = hand["side"]
            prefix = f"{episode_id}/{hand['episode']}"
            for name in (f"tactile_{side}.jsonl", f"tactile_{side}.raw.jsonl",
                         f"wrist_imu_{side}.jsonl", f"wrist_mag_{side}.jsonl"):
                assert f"{prefix}/{name}" in archive.namelist(), name
            row = json.loads(archive.read(f"{prefix}/tactile_{side}.jsonl").splitlines()[0])
            assert len(row["channels"]) == 80, f"{side} taxel count is wrong"
        print(episode_id, camera["kind"], camera["frames_decoded"], "frames: PASS")
print("Verified:", archive_path)
PY
```

This proves file integrity and basic schema, **not** exposure-level camera/glove alignment, visible task quality, or OG Center ingestion. If the check fails, keep the incomplete source episode and report the exact assertion or Studio error.

## Report the result

Send the tester's **host OS and Python version**, camera mode/selected eye, glove firmware/schema, take duration, pass/fail for each UI step, and the exact error text if any. Include frame/loss counters from the episode manifest when available. State whether the test used **real hardware** or simulated devices. Share the ZIP or raw video only through an approved private channel; avoid putting real serial numbers or footage in a public issue.

For normal collection instructions, see the [Studio guide](10_studio.md). For Python-driven collection, see the [SDK guide](12_collection_sdk.md).
