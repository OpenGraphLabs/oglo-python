#!/usr/bin/env python3
"""Case 2: native OVISION v1 stereo + camera IMU + OGLO capture on Linux.

Requires Python 3.12+, SyncField 0.8.14, and OVISION H.264/YCTC firmware.
See OVISION.md. Glove recording and the session format are shared with capture.py.

The camera side is the SDK's own ``oglo.studio_ovision.NativeOvisionCameraWorker``,
the worker OGLO Studio records with: it keeps the SyncField stream connected between
recordings, watches a recording for a capture that died or stopped delivering frames,
writes ``finalization.json`` and the common ``timestamps.jsonl`` and checks that every
native file is there. ``collect.py`` reuses :class:`OvisionCapture` with one worker kept
live across episodes.
"""

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys
import time

import cv2
from oglo.data import CameraData

from capture import capture, positive_number

SYNCFIELD_VERSION = "0.8.14"
MODE = "ovision_native_left"  # The eye the worker's own keyframe preview shows; both eyes are recorded.


class TooFewFrames(RuntimeError):
    """The recording was stopped before the camera delivered two frames.

    The adapter opens the MP4 at the first IDR after ``start_recording`` (about one per
    second on this firmware), so a stop within that time leaves nothing worth keeping.
    Not a device failure: the camera and the gloves are fine.
    """


# -- the SDK worker -------------------------------------------------------------------

def problem(video_device):
    """None when the native backend can record ``video_device`` here, else why not.

    The adapter's calibration read is the identity check: only OVISION firmware answers
    the UVC extension unit with a valid flash calibration, so a webcam, a metadata node,
    or a camera without calibration is reported instead of failing at the first episode.
    """
    if sys.platform != "linux":
        return "native OVISION capture requires Linux V4L2/UVC"
    try:
        installed = version("syncfield")
    except PackageNotFoundError:
        return "syncfield is not installed (pip install -r examples/camera_glove/requirements-ovision.txt)"
    if installed != SYNCFIELD_VERSION:
        return f"syncfield {installed} is installed; this example targets {SYNCFIELD_VERSION}"
    from syncfield.adapters.ovision_calibration import read_ovision_calibration

    try:
        read_ovision_calibration(video_device)
    except Exception as exc:  # OvisionCalibrationError, OSError: wrong node, unplugged, no permission
        return f"{video_device} did not answer as an OVISION camera: {exc}"
    return None


def open_worker(video_device, root, index=0):
    """The SDK's native OVISION worker on ``video_device``, connected and delivering frames.

    It reads the unit's flash calibration, applies the adapter's verified
    exposure/gain/bitrate profile and owns the V4L2 node until ``close()``. It raises,
    and releases the node, when the camera does not deliver valid stereo/IMU metadata
    within ten seconds. ``root`` is where the adapter would write without a recording
    folder; nothing is created there. ``index`` is recorded for reference only.
    """
    from oglo.studio_ovision import NativeOvisionCameraWorker

    return NativeOvisionCameraWorker(index, mode=MODE, name=str(video_device), root=Path(root))


def live_frame(stream, preview=None):
    """The newest frame to show: from ``preview`` (both eyes, every frame) while it runs,
    else the adapter's own left-eye keyframe. After the preview failed, the frame the
    adapter held when the preview took over is stale (its decoder sat idle meanwhile):
    None until the adapter decodes a new one."""
    if preview is None:
        return stream.latest_frame
    if preview.running:
        return preview.latest_frame
    frame = stream.latest_frame
    return None if frame is preview.superseded_frame else frame


def wait_frame(stop, preview, timeout):
    """Until the preview has a new frame, ``stop`` is set, or ``timeout`` passed; at once
    when ``stop`` is set already (``tick`` just pressed h or x)."""
    if stop.is_set():
        return
    if preview is not None and preview.running:
        preview.wait(timeout)
    else:
        stop.wait(timeout)


def camera_report(folder):
    """``finalization.json`` as the worker wrote it, or None."""
    try:
        return json.loads((folder / "finalization.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class OvisionCapture:
    """capture.py camera backend on the SDK's native OVISION worker.

    Without ``worker`` it opens the camera in ``prepare()`` and releases it in
    ``close()``, one session per process. With ``worker`` (collect.py) the caller's
    live worker records this session under ``output`` and stays connected afterwards,
    so the next episode starts without re-reading calibration or renegotiating video.
    ``tick(frame)`` is called while recording with the newest preview frame (or None):
    every frame, both eyes, when ``preview`` is collect.py's running ``StereoPreview``,
    otherwise the worker's left-eye keyframe about every 50 ms; collect.py draws its
    window and reads keys there. ``progress(decoded, expected)`` is called while the
    saved video is decoded back.
    """

    def __init__(self, args, output, worker=None, tick=None, progress=None, preview=None):
        self.args, self.output = args, output
        self.worker, self.shared = worker, worker is not None
        self.tick = tick
        self.preview = preview
        self.verify_progress = progress
        self.metadata: CameraData = {
            "kind": "ovision", "model": "OVISION-EGO-V1", "video_device": str(args.video_device),
            "usb_serial": args.camera_serial, "syncfield_version": SYNCFIELD_VERSION,
            "video": "camera/cam_ego.mp4", "timestamps": "camera/timestamps.jsonl",
            "native_stereo_metadata": "camera/cam_ego.stereo.jsonl",
            "calibration": "camera/cam_ego.calibration.json",
            "imu": "camera/cam_ego.imu.jsonl", "accel": "camera/cam_ego.accel.jsonl",
            "gyro": "camera/cam_ego.gyro.jsonl", "mag": "camera/cam_ego.mag.jsonl",
            "sync_point": "camera/sync_point.json", "finalization": "camera/finalization.json",
            "codec": "h264_passthrough", "width": 3840, "height": 1080,
            "eye_order": ["left", "right"], "eye_width": 1920, "eye_height": 1080,
            "requested_fps": 30,
            "host_timestamp_meaning": "H.264 packet arrival; not exposure time",
        }

    def prepare(self):
        if not self.shared:
            self.worker = open_worker(self.args.video_device, self.output.parent)
        elif self.worker.error or not self.worker.stream.capture_ready():
            raise RuntimeError(f"OVISION camera is not live: {self.worker.error or 'capture stopped'}")

    def record(self, stop):
        # ``begin`` creates camera/, writes sync_point.json, starts the recording and a
        # watchdog that sets ``stop`` (which also stops the gloves) when the capture dies
        # or delivers no frame for five seconds.
        self.worker.begin(self.output, stop)
        try:
            deadline = time.monotonic() + self.args.seconds
            while time.monotonic() < deadline and not stop.is_set():
                frame = live_frame(self.worker.stream, self.preview)
                if self.args.preview and frame is not None:
                    cv2.imshow("OVISION left-eye preview (q stops capture)", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        raise RuntimeError("Capture stopped early from the preview")
                if self.tick is not None:
                    self.tick(frame)
                wait_frame(stop, self.preview, 0.05)
        except BaseException:
            self._finish(quiet=True)  # The loop's own error (Ctrl-C, preview q) is the one to report.
            raise
        finally:
            if self.args.preview:
                cv2.destroyAllWindows()
        return self._finish()

    def _finish(self, quiet=False):
        """``worker.finish()``: stop the recording, write finalization.json and the common
        timestamps.jsonl, check the native files; raises on any failure."""
        try:
            result = self.worker.finish()
        except (RuntimeError, ValueError) as exc:
            if quiet:
                return None
            report = camera_report(self.output) or {}
            frames = report.get("frame_count")
            if (not report.get("error") and self.worker.error is None
                    and isinstance(frames, int) and frames < 2):
                raise TooFewFrames(
                    f"{frames} camera frame(s) before the stop: the video starts at the first "
                    "keyframe, up to a second after the recording began") from exc
            raise
        self.metadata["native_artifacts"] = result["native_artifacts"]
        self.metadata["backend"] = result["backend"]
        return {key: result[key] for key in ("frames_submitted", "first_host_received_ns",
                                             "last_host_received_ns")}

    def close(self):
        if self.worker is not None and not self.shared:
            self.worker.close()  # Stops a recording still running (an error before _finish), then disconnects.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new session directory")
    parser.add_argument("--video-device", type=Path, default=Path("/dev/video0"))
    parser.add_argument("--camera-serial", help="USB serial label from your device inventory")
    parser.add_argument("--seconds", type=positive_number, default=30)
    parser.add_argument("--task", required=True)
    gloves = parser.add_mutually_exclusive_group()
    gloves.add_argument("--serial", help="one OGLO glove's logical CONFIG serial")
    gloves.add_argument("--pair", action="store_true", help="record both gloves")
    parser.add_argument("--preview", action="store_true", help="show a low-rate left-eye preview")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Native OVISION capture requires Linux V4L2/UVC; use a Linux PC or Raspberry Pi")
    capture(args, camera_factory=OvisionCapture)


if __name__ == "__main__":
    main()
