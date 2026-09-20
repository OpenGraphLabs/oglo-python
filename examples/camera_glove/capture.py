#!/usr/bin/env python3
"""Record a USB webcam and one or two OGLO gloves on the same computer.

See README.md in this directory for setup, file fields, and timing limitations.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import math
from pathlib import Path
import threading
import time

import cv2
import oglo
from oglo.data import CameraData, CameraFrameData, CameraTiming


def write_json(path, value):
    """Publish metadata atomically; a crashed run keeps its incomplete marker."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_camera(camera):
    started = time.monotonic_ns()
    ok, image = camera.read()
    received = time.monotonic_ns()  # Before encoding, display, or disk writes.
    if not ok or image is None:
        raise RuntimeError("Camera did not return an image; check its USB connection")
    timing: CameraTiming = {
        "host_read_started_ns": started,
        "host_received_ns": received,
        # OpenCV webcams have no portable exposure-time API. With a vendor SDK,
        # preserve its native time, unit, domain, and meaning here as well.
        "device_timestamp": None,
        "device_timestamp_unit": None,
        "device_clock_domain": None,
        "device_timestamp_meaning": None,
    }
    return image, timing


def record_camera(camera, output, seconds, fps, stop, preview=False):
    writer = None
    count = 0
    first = last = None
    size = None
    deadline = time.monotonic() + seconds
    try:
        with (output / "timestamps.jsonl").open("x", encoding="utf-8") as sidecar:
            while time.monotonic() < deadline and not stop.is_set():
                image, timing = read_camera(camera)
                height, width = image.shape[:2]
                if writer is None:
                    size = (width, height)
                    if width % 2 or height % 2:
                        raise RuntimeError("Use even camera dimensions to avoid encoder cropping")
                    writer = cv2.VideoWriter(
                        str(output / "video.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, size
                    )
                    if not writer.isOpened():
                        raise RuntimeError("Could not open the mp4v video encoder")
                if (width, height) != size:
                    raise RuntimeError("Camera dimensions changed during capture")
                writer.write(image)
                row: CameraFrameData = {"frame_index": count, **timing}
                sidecar.write(json.dumps(row) + "\n")
                sidecar.flush()
                if first is None:
                    first = timing["host_received_ns"]
                last = timing["host_received_ns"]
                count += 1
                if preview:
                    cv2.imshow("OGLO camera (q stops capture)", image)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        raise RuntimeError("Capture stopped early from the preview")
    finally:
        if writer is not None:
            writer.release()
        if preview:
            cv2.destroyAllWindows()
    if count < 2:
        raise RuntimeError("Fewer than two camera frames were received")
    return {"frames_submitted": count, "width": size[0], "height": size[1],
            "first_host_received_ns": first, "last_host_received_ns": last}


def verify_video(path, expected_frames):
    """Decode the saved video; VideoWriter.write() has no success return value."""
    decoder = cv2.VideoCapture(str(path))
    count = 0
    try:
        if not decoder.isOpened():
            raise RuntimeError(f"Cannot decode {path}")
        while decoder.read()[0]:
            count += 1
    finally:
        decoder.release()
    if count != expected_frames:
        raise RuntimeError(f"Video has {count} decoded frames, expected {expected_frames}")
    return count


class WebcamCapture:
    """OpenCV webcam backend; OVISION uses its native backend in ovision.py."""

    def __init__(self, args, output):
        self.args, self.output = args, output
        self.camera = None
        self.metadata: CameraData = {
            "kind": "usb_webcam", "index": args.camera, "video": "camera/video.mp4",
            "timestamps": "camera/timestamps.jsonl", "codec": "mp4v",
            "playback_fps": args.fps, "requested_fps": args.fps,
            "host_timestamp_meaning": "OpenCV read-return time; not exposure time",
        }

    def prepare(self):
        self.camera = cv2.VideoCapture(self.args.camera)
        if not self.camera.isOpened():
            raise RuntimeError("Cannot open camera; check --camera and OS camera permission")
        self.metadata["fps_request_accepted"] = bool(
            self.camera.set(cv2.CAP_PROP_FPS, self.args.fps)
        )
        self.metadata["backend"] = self.camera.getBackendName()
        read_camera(self.camera)  # Check connection; discard this setup frame.

    def record(self, stop):
        return record_camera(self.camera, self.output, self.args.seconds,
                             self.args.fps, stop, self.args.preview)

    def close(self):
        if self.camera is not None:
            self.camera.release()


def record_glove(glove, output, seconds, start, stop, entry, root):
    start.wait()
    try:
        episode = oglo.record(output, seconds=seconds, glove=glove, stop_event=stop)
        entry["episode"] = episode.relative_to(root).as_posix()
        return episode
    except BaseException as exc:
        partial = getattr(exc, "partial_episode", None)
        if partial is not None:
            entry["episode"] = Path(partial).relative_to(root).as_posix()
        entry["error"] = f"{type(exc).__name__}: {exc}"
        stop.set()  # Stop peer capture if USB fails.
        raise


def capture(args, camera_factory=WebcamCapture):
    root = args.output
    root.mkdir(parents=True, exist_ok=False)  # Never mix sessions or overwrite files.
    camera_dir = root / "camera"
    camera_dir.mkdir()
    manifest: oglo.OGLData = {
        "schema": "oglo-camera-example.v1", "task_description": args.task,
        "sdk_version": oglo.__version__, "opencv_version": cv2.__version__,
        "requested_duration_s": args.seconds,
        "host_clock": "time.monotonic_ns", "same_host": True,
        "started_wall_time_ns": time.time_ns(),
        "started_host_monotonic_ns": time.monotonic_ns(),
        "complete": False, "alignment_validated": False, "error": None,
        "camera": {},
        "gloves": [],
    }
    metadata = root / "manifest.json"
    write_json(metadata, manifest)
    start, stop = threading.Event(), threading.Event()
    try:
        with ExitStack() as stack:
            camera = camera_factory(args, camera_dir)
            stack.callback(camera.close)
            manifest["camera"] = camera.metadata
            camera.prepare()

            gloves = oglo.connect_pair() if args.pair else (oglo.connect(serial=args.serial),)
            for glove in gloves:
                stack.callback(glove.close)
            for glove in gloves:
                folder = root / "gloves" / glove.info.side
                folder.mkdir(parents=True)
                calibration = folder / "calibration.json"
                # Read the existing zero recipe; never recalibrate during a run.
                reply = glove.send("GET ZERO", expect="#TZERO ", timeout=4.0)
                write_json(calibration, json.loads(reply.removeprefix("#TZERO ")))
                manifest["gloves"].append({
                    "serial": glove.info.serial, "side": glove.info.side,
                    "calibration": calibration.relative_to(root).as_posix(), "episode": None,
                })
            write_json(metadata, manifest)
            print("Recording camera and gloves. Keep hands and contacts visible.", flush=True)
            with ThreadPoolExecutor(max_workers=len(gloves)) as pool:
                try:
                    futures = [pool.submit(
                        record_glove, glove, root / "gloves" / glove.info.side,
                        args.seconds, start, stop, entry, root,
                    ) for glove, entry in zip(gloves, manifest["gloves"])]
                    start.set()
                    manifest["camera"].update(camera.record(stop))
                    # Every glove drains independently. Wait for its own duration
                    # boundary instead of cancelling it when the camera finishes.
                    for future in futures:
                        future.result()
                finally:
                    stop.set()
                    start.set()  # Also release workers if submission failed.

        print("Checking saved video and glove files...", flush=True)
        manifest["camera"]["frames_decoded"] = verify_video(
            root / manifest["camera"]["video"], manifest["camera"]["frames_submitted"]
        )
        starts = [manifest["camera"]["first_host_received_ns"]]
        ends = [manifest["camera"]["last_host_received_ns"]]
        for entry in manifest["gloves"]:
            episode = oglo.replay(root / entry["episode"])
            if not episode.meta["complete"] or episode.meta["stop_reason"] != "duration":
                raise RuntimeError(f"Glove {entry['serial']} was incomplete or stopped early")
            entry["summary"] = episode.summary()  # Validate all saved arrays.
            for stream in ("tactile", "imu", "mag"):
                stamps = episode.arrays(stream)["host_received_ns"]
                if len(stamps):
                    starts.append(int(stamps[0]))
                    ends.append(int(stamps[-1]))
        overlap = [max(starts), min(ends)]
        if overlap[0] >= overlap[1]:
            raise RuntimeError("Camera and glove streams have no common host-time overlap")
        manifest["overlap_host_received_ns"] = overlap
        manifest["complete"] = True
    except BaseException as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        stop.set()
        manifest["ended_wall_time_ns"] = time.time_ns()
        write_json(metadata, manifest)
    print(f"Send this entire folder unchanged: {root.resolve()}")
    return root


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new session directory")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV webcam index (default: 0)")
    parser.add_argument("--seconds", type=positive_number, default=30)
    parser.add_argument("--fps", type=positive_number, default=30,
                        help="requested camera FPS and MP4 playback FPS (default: 30)")
    parser.add_argument("--task", required=True, help="description of the activity")
    gloves = parser.add_mutually_exclusive_group()
    gloves.add_argument("--serial", help="one glove's logical CONFIG serial")
    gloves.add_argument("--pair", action="store_true", help="record verified left and right gloves")
    parser.add_argument("--preview", action="store_true", help="show camera view; q stops early")
    args = parser.parse_args()
    if args.camera < 0:
        parser.error("--camera must be a nonnegative device index")
    capture(args)


if __name__ == "__main__":
    main()
