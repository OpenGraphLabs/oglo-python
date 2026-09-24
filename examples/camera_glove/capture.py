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
import subprocess
import tempfile
import threading
import time

import cv2
import numpy as np
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


FFMPEG = "ffmpeg"  # The system binary; OpenCV's own writer knows no H.264 / H.265.
ENCODERS = {
    # --codec name -> ffmpeg arguments; {q} is --video-quality (CRF / CQ, lower = larger file).
    "hevc_nvenc": ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", "0", "-cq", "{q}",
                   "-tag:v", "hvc1"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", "0", "-cq", "{q}"],
    "libx265": ["-c:v", "libx265", "-preset", "veryfast", "-crf", "{q}",
                "-x265-params", "log-level=none", "-tag:v", "hvc1"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "{q}"],
}
CODECS = ("mp4v", *ENCODERS)


class FfmpegWriter:
    """Pipe BGR frames to the system ffmpeg, which encodes the file in real time.

    Same three methods as ``cv2.VideoWriter``. ``-fps_mode passthrough`` keeps exactly
    one output frame per submitted frame, so ``verify_video`` still counts them. An
    encoder problem is kept in ``error`` for the caller to raise, never swallowed.
    """

    def __init__(self, path, fps, size, codec, quality):
        if codec not in ENCODERS:
            raise RuntimeError(f"Unknown codec {codec!r}; choose from {', '.join(CODECS)}")
        encoder = [part.format(q=quality) for part in ENCODERS[codec]]
        command = [FFMPEG, "-hide_banner", "-nostats", "-loglevel", "error", "-y",
                   "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{size[0]}x{size[1]}",
                   "-framerate", f"{fps:g}", "-i", "-",
                   *encoder, "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", str(path)]
        self.codec = codec
        self.error = None
        self._released = False
        self._log = tempfile.TemporaryFile()  # ffmpeg's stderr; a pipe could block it.
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=self._log)
        except OSError as exc:  # Not found, not executable, fork failure: no process, no log.
            self._log.close()
            if isinstance(exc, FileNotFoundError):
                raise RuntimeError(f"{FFMPEG} not found; install ffmpeg or use --codec mp4v")
            raise

    def isOpened(self):
        return self._process.poll() is None

    def write(self, image):
        try:
            self._process.stdin.write(image.tobytes())
        except OSError:  # Broken pipe: ffmpeg died, usually at encoder start.
            self.release()
            raise RuntimeError(self.error or f"ffmpeg ({self.codec}) stopped accepting frames")

    def release(self):
        if self._released:
            return
        self._released = True
        try:
            self._process.stdin.close()
        except OSError:
            pass
        code = self._process.wait()
        self._log.seek(0)
        tail = self._log.read()[-2000:].decode(errors="replace").strip()
        self._log.close()
        if code != 0:
            self.error = f"ffmpeg ({self.codec}) exited with status {code}: {tail or 'no message'}"


def open_writer(path, fps, size, codec, quality):
    if codec == "mp4v":
        return cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    return FfmpegWriter(path, fps, size, codec, quality)


def probe_encoder(codec, quality=23, size=(256, 128)):
    """Encode two blank frames; None when the codec works on this machine, else why not."""
    if codec == "mp4v":
        return None
    with tempfile.TemporaryDirectory() as folder:
        try:
            writer = FfmpegWriter(Path(folder) / "probe.mp4", 30, size, codec, quality)
            blank = np.zeros((size[1], size[0], 3), dtype=np.uint8)
            writer.write(blank)
            writer.write(blank)
            writer.release()
        except RuntimeError as exc:
            return str(exc)
        return writer.error


def record_camera(camera, output, seconds, fps, stop, preview=False, codec="mp4v", quality=23):
    """Record ``camera`` into ``output/`` until ``seconds`` pass or ``stop`` is set.

    The encoder is opened on one frame that is not recorded, before the timed loop:
    an ffmpeg / NVENC start takes a few hundred milliseconds, during which the camera
    would otherwise buffer frames that then get read back stale with fresh timestamps.
    """
    image, _ = read_camera(camera)
    height, width = image.shape[:2]
    if width % 2 or height % 2:
        raise RuntimeError("Use even camera dimensions to avoid encoder cropping")
    size = (width, height)
    writer = open_writer(output / "video.mp4", fps, size, codec, quality)
    count = 0
    first = last = None
    try:
        if not writer.isOpened():
            raise RuntimeError(f"Could not open the {codec} video encoder")
        deadline = time.monotonic() + seconds
        with (output / "timestamps.jsonl").open("x", encoding="utf-8") as sidecar:
            while time.monotonic() < deadline and not stop.is_set():
                image, timing = read_camera(camera)
                if image.shape[1::-1] != size:
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
        writer.release()
        if preview:
            cv2.destroyAllWindows()
    if getattr(writer, "error", None):
        raise RuntimeError(writer.error)
    if count < 2:
        raise RuntimeError("Fewer than two camera frames were received")
    return {"frames_submitted": count, "width": size[0], "height": size[1],
            "first_host_received_ns": first, "last_host_received_ns": last}


def verify_video(path, expected_frames, progress=None):
    """Decode the saved video; VideoWriter.write() has no success return value.

    ``progress(decoded, expected)`` is called every 30 frames: a full decode of a long
    episode takes seconds, and a window that is not redrawn meanwhile looks hung.
    """
    decoder = cv2.VideoCapture(str(path))
    count = 0
    try:
        if not decoder.isOpened():
            raise RuntimeError(f"Cannot decode {path}")
        while decoder.read()[0]:
            count += 1
            if progress is not None and count % 30 == 0:
                progress(count, expected_frames)
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
        self.codec = getattr(args, "codec", "mp4v")
        self.quality = getattr(args, "video_quality", 23)
        if self.codec not in CODECS:
            raise RuntimeError(f"Unknown codec {self.codec!r}; choose from {', '.join(CODECS)}")
        self.metadata: CameraData = {
            "kind": "usb_webcam", "index": args.camera, "video": "camera/video.mp4",
            "timestamps": "camera/timestamps.jsonl", "codec": self.codec,
            "video_quality": None if self.codec == "mp4v" else self.quality,
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
        self.output.mkdir()  # The backend owns camera/; the native OVISION worker insists on creating it.
        return record_camera(self.camera, self.output, self.args.seconds,
                             self.args.fps, stop, self.args.preview, self.codec, self.quality)

    def close(self):
        if self.camera is not None:
            self.camera.release()


def record_glove(glove, output, seconds, start, stop, entry, root):
    """One glove's episode on its own thread; the stream is stopped whichever way it ends.

    ``oglo.record`` leaves the stream running when it returns, and resumes it before it
    raises. A glove nobody reads must not be left like that: Linux buffers 4095 bytes
    per tty (~85 ms of stream), then throttles the device, and the next command written
    to it wedges the firmware until a replug. The peer glove finishing, the video check
    and the caller's error handling all take longer than that, so the stop happens here.
    """
    start.wait()
    try:
        calibration = json.loads((root / entry["calibration"]).read_text(encoding="utf-8"))
        episode = oglo.record(output, seconds=seconds, glove=glove, stop_event=stop,
                              calibration=calibration)
        entry["episode"] = episode.relative_to(root).as_posix()
        glove.stop()
        return episode
    except BaseException as exc:
        partial = getattr(exc, "partial_episode", None)
        if partial is not None:
            entry["episode"] = Path(partial).relative_to(root).as_posix()
        entry["error"] = f"{type(exc).__name__}: {exc}"
        stop.set()  # Stop peer capture if USB fails.
        try:
            glove.stop()
        except Exception:  # A port that is dead already; the recording error is the one to report.
            pass
        raise


def capture(args, camera_factory=WebcamCapture, stop=None, gloves=None):
    """Record one session under ``args.output`` and return that folder.

    ``stop`` is an optional ``threading.Event``. The caller may set it to end the
    session before ``args.seconds``; the result is still complete and its manifest
    says ``stop_reason: "cancelled"``. Without it every stream runs ``args.seconds``.
    ``gloves`` may hold already-open ``oglo`` gloves; they are used as they are and
    left open for the caller. Without it the gloves are opened and closed here.
    """
    external_stop = stop is not None
    stopped_early = False
    root = args.output
    root.mkdir(parents=True, exist_ok=False)  # Never mix sessions or overwrite files.
    camera_dir = root / "camera"  # Created by the camera backend when it starts recording.
    manifest: oglo.OGLData = {
        "schema": "oglo-camera-example.v2", "task_description": args.task,
        "sdk_version": oglo.__version__, "opencv_version": cv2.__version__,
        "requested_duration_s": args.seconds,
        "host_clock": "time.monotonic_ns", "same_host": True,
        "started_wall_time_ns": time.time_ns(),
        "started_host_monotonic_ns": time.monotonic_ns(),
        "complete": False, "alignment_validated": False, "error": None,
        "stop_reason": None,
        "camera": {},
        "gloves": [],
    }
    metadata = root / "manifest.json"
    write_json(metadata, manifest)
    start = threading.Event()
    stop = stop if external_stop else threading.Event()
    try:
        with ExitStack() as stack:
            camera = camera_factory(args, camera_dir)
            stack.callback(camera.close)
            manifest["camera"] = camera.metadata
            camera.prepare()

            if gloves is None:
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
                    stopped_early = stop.is_set()  # By the caller, or by a glove failure.
                    # Every glove drains independently. Wait for its own duration
                    # boundary instead of cancelling it when the camera finishes.
                    for future in futures:
                        future.result()
                finally:
                    stop.set()
                    start.set()  # Also release workers if submission failed.

        print("Checking saved video and glove files...", flush=True)
        manifest["camera"]["frames_decoded"] = verify_video(
            root / manifest["camera"]["video"], manifest["camera"]["frames_submitted"],
            progress=getattr(camera, "verify_progress", None),
        )
        starts = [manifest["camera"]["first_host_received_ns"]]
        ends = [manifest["camera"]["last_host_received_ns"]]
        for entry in manifest["gloves"]:
            episode = oglo.replay(root / entry["episode"])
            allowed = {"duration", "cancelled"} if external_stop else {"duration"}
            if not episode.meta["complete"] or episode.meta["stop_reason"] not in allowed:
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
        manifest["stop_reason"] = "cancelled" if stopped_early else "duration"
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
    parser.add_argument("--codec", choices=CODECS, default="mp4v",
                        help="mp4v uses OpenCV's writer; the others pipe frames to the system "
                             "ffmpeg (default: mp4v)")
    parser.add_argument("--video-quality", type=int, default=23, metavar="N",
                        help="CRF / CQ for the ffmpeg codecs, 0-51, lower = larger file (default: 23)")
    args = parser.parse_args()
    if args.camera < 0:
        parser.error("--camera must be a nonnegative device index")
    if not 0 <= args.video_quality <= 51:
        parser.error("--video-quality must be 0..51")
    problem = probe_encoder(args.codec, args.video_quality)
    if problem:
        parser.error(f"--codec {args.codec} does not work here: {problem}")
    capture(args)


if __name__ == "__main__":
    main()
