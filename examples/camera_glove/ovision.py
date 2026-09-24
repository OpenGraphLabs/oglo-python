#!/usr/bin/env python3
"""Case 2: native OVISION v1 stereo + camera IMU + OGLO capture on Linux.

Requires Python 3.12+, SyncField 0.8.14, and OVISION H.264/YCTC firmware.
See OVISION.md. Glove recording and the session format are shared with capture.py.
``collect.py`` reuses :class:`OvisionCapture` with one stream kept live across episodes.
"""

import argparse
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import socket
import sys
import time

import cv2
from oglo.data import CameraData, CameraFrameData

from capture import capture, positive_number, write_json

SYNCFIELD_VERSION = "0.8.14"
READY_SECONDS = 10  # wait for the first live frame with valid stereo/IMU metadata


def make_join_timestamps(output, expected_frames):
    """Add a common-schema sidecar without modifying native OVISION metadata."""
    count = 0
    first = last = None
    with (output / "cam_ego.stereo.jsonl").open(encoding="utf-8") as source:
        with (output / "timestamps.jsonl").open("x", encoding="utf-8") as destination:
            for line in source:
                row = json.loads(line)
                host = row["capture_ns"]
                device = row["device_timestamp_ns"]
                if (row["frame_number"] != count or type(host) is not int or host < 0
                        or (last is not None and host < last)):
                    raise ValueError("OVISION frame numbers or host timestamps are invalid")
                if (type(device) is not int or device < 0
                        or device != row["left_exposure_start_ns"]):
                    raise ValueError("OVISION device timestamp must be the left exposure start")
                timing: CameraFrameData = {
                    "frame_index": count,
                    # capture_ns in this adapter is host packet-arrival time,
                    # even though native stereo rows say clock_source=device_monotonic.
                    "host_received_ns": host, "host_read_started_ns": None,
                    "device_timestamp": device, "device_timestamp_unit": "ns",
                    "device_clock_domain": "ovision_camera",
                    "device_timestamp_meaning": "left_exposure_start",
                    "native_metadata": "cam_ego.stereo.jsonl",
                    "native_frame_number": row["frame_number"],
                }
                destination.write(json.dumps(timing) + "\n")
                if first is None:
                    first = host
                last = host
                count += 1
    if count < 2 or count != expected_frames:
        raise ValueError(f"OVISION metadata has {count} frames; expected {expected_frames}, at least two")
    return {"frames_submitted": count, "first_host_received_ns": first,
            "last_host_received_ns": last}


# -- the SyncField stream ------------------------------------------------------------

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


def wait_ready(stream, seconds=READY_SECONDS):
    deadline = time.monotonic() + seconds
    while not stream.capture_ready():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"OVISION did not produce valid stereo/IMU metadata within {seconds} seconds")
        time.sleep(0.05)


def open_stream(video_device, camera_serial, output):
    """A live ``OvisionCameraStream`` that will write under ``output`` once told to record.

    Reads per-unit flash calibration and applies the adapter's verified
    exposure/gain/bitrate profile. No OpenCV camera read/re-encode path. The stream
    owns the V4L2 node until ``disconnect()``; nothing else can open the camera meanwhile.
    """
    if version("syncfield") != SYNCFIELD_VERSION:
        raise RuntimeError(f"Install requirements-ovision.txt: this example targets SyncField {SYNCFIELD_VERSION}")
    from syncfield.adapters.ovision_camera import OvisionCameraStream

    stream = OvisionCameraStream(
        "cam_ego", output, video_device=video_device, usb_serial=camera_serial,
        width=3840, height=1080, fps=30,
    )
    stream.prepare()
    stream.connect()
    wait_ready(stream)
    return stream


def reconnect_stream(stream):
    """Bring back a stream whose capture died: the adapter re-reads calibration and
    reopens the node (``disconnect`` + ``connect``), then the same readiness wait."""
    stream.reconnect()
    wait_ready(stream)


def retarget(stream, output):
    """Point a connected stream at the next episode's ``camera/`` folder.

    SyncField 0.8.14 fixes the output folder when the stream is built and moves it only
    by segment rotation, which works mid-recording. Between recordings these two private
    attributes are all ``start_recording`` reads (the adapter's own rotation sets the
    same two); the exact version pin in :func:`open_stream` is what keeps this honest.
    """
    if stream._recording:
        raise RuntimeError("OVISION stream is still recording; stop it before the next episode")
    stream._output_dir = Path(output)
    stream._file_path = stream._output_dir / f"{stream.id}.mp4"


def session_clock():
    """The SyncField clock every recording is anchored to; saved as sync_point.json."""
    from syncfield.clock import SessionClock
    from syncfield.types import SyncPoint

    return SessionClock(SyncPoint.create_now(socket.gethostname()),
                        recording_armed_ns=time.monotonic_ns())


class OvisionCapture:
    """capture.py camera backend on the native stream.

    Without ``stream`` it opens the camera in ``prepare()`` and releases it in
    ``close()``, one session per process. With ``stream`` (collect.py) the caller's
    live stream is retargeted at this session's folder and left connected afterwards,
    so the next episode starts without re-reading calibration or renegotiating video.
    ``tick(frame)`` is called about every 50 ms while recording with the newest
    left-eye preview frame (or None); collect.py draws its window and reads keys there.
    """

    def __init__(self, args, output, stream=None, tick=None):
        self.args, self.output = args, output
        self.stream, self.shared = stream, stream is not None
        self.tick = tick
        self.recording = False
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
        if self.shared:
            retarget(self.stream, self.output)
            if not self.stream.capture_ready():
                raise RuntimeError("OVISION stream is not live; reconnect it before recording")
            return
        self.stream = open_stream(self.args.video_device, self.args.camera_serial, self.output)

    def finish(self):
        self.recording = False
        report = self.stream.stop_recording()
        # Paths/enums in the native report are preserved as their string forms.
        write_json(self.output / "finalization.json",
                   json.loads(json.dumps(asdict(report), default=str)))
        return report

    def record(self, stop):
        clock = session_clock()
        write_json(self.output / "sync_point.json", clock.sync_point.to_dict())
        self.recording = True
        try:
            self.stream.start_recording(clock)
            deadline = time.monotonic() + self.args.seconds
            while time.monotonic() < deadline and not stop.is_set():
                if not self.stream.capture_ready():
                    raise RuntimeError("OVISION capture failed; see camera/finalization.json")
                if self.args.preview and self.stream.latest_frame is not None:
                    cv2.imshow("OVISION left-eye preview (q stops capture)", self.stream.latest_frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        raise RuntimeError("Capture stopped early from the preview")
                if self.tick is not None:
                    self.tick(self.stream.latest_frame)
                stop.wait(0.05)
        finally:
            report = self.finish()
            if self.args.preview:
                cv2.destroyAllWindows()
        if report.status != "completed" or report.error:
            raise RuntimeError(f"OVISION finalization failed: {report.error or report.status}")
        required = ["mp4", "stereo.jsonl", "imu.jsonl", "accel.jsonl", "gyro.jsonl",
                    "mag.jsonl", "calibration.json", "calibration.yaml", "calibration.bin"]
        for suffix in required:
            path = self.output / f"cam_ego.{suffix}"
            if not path.is_file() or (suffix != "mag.jsonl" and path.stat().st_size == 0):
                raise RuntimeError(f"OVISION artifact missing or empty: {path.name}")
        self.metadata["native_artifacts"] = [f"camera/cam_ego.{suffix}" for suffix in required]
        return make_join_timestamps(self.output, report.frame_count)

    def close(self):
        if self.stream is None:
            return
        try:
            if self.recording:
                self.finish()
        finally:
            if not self.shared:
                self.stream.disconnect()


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
