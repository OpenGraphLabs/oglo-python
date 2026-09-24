#!/usr/bin/env python3
"""Case 3: RealSense D455 color + camera IMU + OGLO capture on Ubuntu.

Requires pyrealsense2 2.58.4.10922 on Linux; see REALSENSE.md. Glove recording and the
session format are shared with capture.py.

The camera side is the SDK's ``oglo.studio_realsense.RealSenseCameraWorker``, the
worker OGLO Studio records with: it keeps color, accelerometer and gyroscope streaming
on the camera's own clock, writes each take's video, timestamps, IMU files and factory
calibration, and ends a take whose color stops for five seconds. ``collect.py`` reuses
:class:`RealSenseCapture` with one worker kept live across episodes. ``--check`` tests
the camera alone.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

import cv2
from oglo.data import CameraData
from oglo.studio_realsense import PYREALSENSE2_VERSION, installed_version, list_devices

from capture import CODECS, capture, open_writer, positive_number, probe_encoder

INSTALL = "pip install -r examples/camera_glove/requirements-realsense.txt"
HOST_MEANING = "host monotonic time when librealsense delivered the frame; not exposure time"
STUDIO_ONLY = ("index", "mode", "name", "native_device_timestamps")  # Studio's view of the worker.
COLOR_SIZE = (1280, 720)  # The color mode requested from the camera.


# -- the SDK worker -------------------------------------------------------------------

def problem(platform=None):
    """None when this machine can record the one attached RealSense with its IMU, else why not."""
    if (platform or sys.platform) != "linux":
        return ("RealSense camera IMU capture needs Ubuntu/Linux: librealsense 2.58.4 compiles its "
                "motion sensors out on macOS and pyrealsense2 has no macOS wheels (see REALSENSE.md)")
    installed = installed_version()
    if installed is None:
        return f"pyrealsense2 is not installed ({INSTALL})"
    if installed != PYREALSENSE2_VERSION:
        return f"pyrealsense2 {installed} is installed; this example targets {PYREALSENSE2_VERSION}"
    try:
        devices = list_devices()
    except Exception as exc:  # A broken install or a USB permission problem.
        return f"pyrealsense2 could not list cameras: {exc}"
    if not devices:
        return "no RealSense camera is connected (check the USB 3 cable and the udev rules in REALSENSE.md)"
    if len(devices) > 1:
        serials = ", ".join(str(device["serial"]) for device in devices)
        return f"{len(devices)} RealSense cameras are connected ({serials}); this backend records exactly one"
    device = devices[0]
    if not device["has_imu"]:
        return f"{device['name']} has no accelerometer + gyroscope (a D455 has both)"
    if not str(device["usb_type"] or "").startswith("3"):
        return f"{device['name']} is on a USB {device['usb_type']} connection; plug it into a USB 3 port"
    return None


def open_worker(fps=30, codec="mp4v", video_quality=23):
    """The SDK worker on the one attached RealSense, streaming; ``--codec`` encodes its video."""
    from oglo.studio_realsense import RealSenseCameraWorker

    (device,) = list_devices()
    quality = None if codec == "mp4v" else video_quality
    return RealSenseCameraWorker(
        0, name=device["serial"], fps=fps, codec=codec, video_quality=quality, size=COLOR_SIZE,
        writer_factory=lambda path, rate, size: open_writer(path, rate, size, codec, video_quality))


class RealSenseCapture:
    """capture.py camera backend on the SDK's RealSense worker.

    Without ``worker`` it opens the camera in ``prepare()`` and releases it in
    ``close()``, one session per process. With ``worker`` (collect.py) the caller's live
    worker records this session under ``output`` and keeps streaming afterwards.
    ``tick(frame)`` is called about every 50 ms while recording with the newest color
    frame; collect.py draws its window and reads keys there. ``progress(decoded,
    expected)`` is called while the saved video is decoded back.
    """

    def __init__(self, args, output, worker=None, tick=None, progress=None):
        self.args, self.output = args, output
        self.worker, self.shared = worker, worker is not None
        self.tick = tick
        self.verify_progress = progress
        codec = getattr(args, "codec", "mp4v")
        self.metadata: CameraData = {
            "kind": "realsense", "video": "camera/video.mp4", "timestamps": "camera/timestamps.jsonl",
            "codec": codec, "video_quality": None if codec == "mp4v" else getattr(args, "video_quality", 23),
            "playback_fps": args.fps, "requested_fps": args.fps, "host_timestamp_meaning": HOST_MEANING,
        }

    def prepare(self):
        if not self.shared:
            self.worker = open_worker(self.args.fps, getattr(self.args, "codec", "mp4v"),
                                      getattr(self.args, "video_quality", 23))
        elif self.worker.error:
            raise RuntimeError(f"RealSense camera is not live: {self.worker.error}")

    def record(self, stop):
        # ``begin`` creates camera/, writes the calibration and starts a watchdog that
        # sets ``stop`` (which also stops the gloves) when color stops for five seconds.
        self.worker.begin(self.output, stop)
        try:
            deadline = time.monotonic() + self.args.seconds
            while time.monotonic() < deadline and not stop.is_set():
                frame = self.worker.latest_frame
                if self.args.preview and frame is not None:
                    cv2.imshow("RealSense color (q stops capture)", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        raise RuntimeError("Capture stopped early from the preview")
                if self.tick is not None:
                    self.tick(frame)
                stop.wait(0.05)
        except BaseException:
            try:
                self.worker.finish()
            except Exception:
                pass  # The loop's own error (Ctrl-C, preview q) is the one to report.
            raise
        finally:
            if self.args.preview:
                cv2.destroyAllWindows()
        result = self.worker.finish()
        self.metadata.update({key: value for key, value in result.items() if key not in STUDIO_ONLY})
        return {key: result[key] for key in ("frames_submitted", "first_host_received_ns",
                                             "last_host_received_ns", "width", "height")}

    def close(self):
        if self.worker is not None and not self.shared:
            self.worker.close()


# -- --check ----------------------------------------------------------------------

def _rate(count, first, last, per_second):
    return (count - 1) * per_second / (last - first) if count > 1 and last > first else 0.0


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def check(seconds=3.0, fps=30, out=print):
    """Describe the attached RealSense, record ``seconds`` without gloves, and say whether
    color + camera IMU work here. Returns the process exit code."""
    reason = problem()
    if reason:
        out(f"FAIL: {reason}")
        return 1
    (device,) = list_devices()
    worker = None
    try:
        worker = open_worker(fps)
        out("RealSense check")
        out(f"  device     {device['name']}  serial {device['serial']}")
        firmware = f"  firmware   {device['firmware']} (recommended {device['recommended_firmware']})"
        if device["firmware"] != device["recommended_firmware"]:
            firmware += " -- update with RealSense Viewer before collecting"
        out(firmware)
        out(f"  usb        {device['usb_type']}  port {device['physical_port']}")
        out(f"  color      {worker.size[0]}x{worker.size[1]} bgr8 @ {int(round(worker.fps))} fps")
        for stream, rate in (("accel", worker.accel_hz), ("gyro", worker.gyro_hz)):
            offered = ", ".join(str(value) for value in worker.offered_rates[stream])
            out(f"  {stream:<10} {rate} Hz (offered {offered})")
        out(f"Recording {seconds:g} s...")
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch) / "camera"
            worker.begin(folder, threading.Event())
            time.sleep(seconds)
            meta = worker.finish()
            frames = _read_jsonl(folder / "timestamps.jsonl")
            meanings = sorted({row["device_timestamp_meaning"] for row in frames})
            imu = {stream: _read_jsonl(folder / f"realsense.{stream}.jsonl") for stream in ("accel", "gyro")}
        out(f"  color      {len(frames)} frames, "
            f"{_rate(len(frames), frames[0]['device_timestamp'], frames[-1]['device_timestamp'], 1e6):.1f} fps, "
            f"device time {'/'.join(meanings)} ({meta['device_clock_domain']})")
        for stream, rows in imu.items():
            hz = _rate(len(rows), rows[0]["device_timestamp_us"], rows[-1]["device_timestamp_us"], 1e6)
            out(f"  {stream:<10} {len(rows)} samples, {hz:.1f} Hz")
        out(f"  dropped    {meta['native_frames_dropped']} color frames")
    except RuntimeError as exc:
        out(f"FAIL: {exc}")
        return 1
    finally:
        if worker is not None:
            worker.close()
    out("OK: this camera can record color + camera IMU.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="describe the camera, record 3 s without gloves, and exit")
    parser.add_argument("--output", type=Path, help="new session directory")
    parser.add_argument("--seconds", type=positive_number, default=30)
    parser.add_argument("--fps", type=positive_number, default=30, help="color rate and MP4 playback rate")
    parser.add_argument("--task", help="description of the activity")
    gloves = parser.add_mutually_exclusive_group()
    gloves.add_argument("--serial", help="one OGLO glove's logical CONFIG serial")
    gloves.add_argument("--pair", action="store_true", help="record both gloves")
    parser.add_argument("--preview", action="store_true", help="show the color stream while recording")
    parser.add_argument("--codec", choices=CODECS, default="mp4v",
                        help="video encoder: mp4v = OpenCV's writer; the others pipe to the system ffmpeg")
    parser.add_argument("--video-quality", type=int, default=23, metavar="N",
                        help="CRF / CQ for the ffmpeg codecs, 0-51, lower = larger file (default: 23)")
    args = parser.parse_args(argv)
    if args.check:
        return check(fps=args.fps)
    if args.output is None or args.task is None:
        parser.error("--output and --task are required unless --check is given")
    if not 0 <= args.video_quality <= 51:
        parser.error("--video-quality must be 0..51")
    reason = problem()
    if reason:
        parser.error(reason)
    encoder = probe_encoder(args.codec, args.video_quality)  # Before any device opens.
    if encoder:
        parser.error(f"--codec {args.codec} does not work here: {encoder}")
    capture(args, camera_factory=RealSenseCapture)
    return 0


if __name__ == "__main__":
    sys.exit(main())
