#!/usr/bin/env python3
"""Record both gloves plus your own camera as one session OpenGraph can align.

The SDK speaks only to the gloves. The camera is yours: any device, any library.
What post-processing needs from the camera is small, and this file shows all of it
once, top to bottom:

  1. CAMERA SKELETON    a FrameSource with three methods. Replace `read()` with
                        your camera; a VideoSink stores the frames you hand back.
  2. REFERENCE CAMERAS  OpenCV for any UVC camera (a webcam, an iPhone, an OVISION)
                        and a FakeCamera so the flow runs before your camera is ready.
  3. CAMERA RECORDER    stamps every frame with the host clock the gloves use and
                        writes `video.timestamps.jsonl` next to the video.
  4. THE SESSION        both gloves through `oglo.record()`, the camera, tap
                        markers, and one `session.json` tying everything together.

Run it:

    python3 examples/03_full_session.py --seconds 120             # webcam 0
    python3 examples/03_full_session.py --device 1 --seconds 120  # another camera
    python3 examples/03_full_session.py                           # until Ctrl-C
    python3 examples/03_full_session.py --camera fake --seconds 5 # gloves only, no camera

What it writes:

    sessions/session_20260920T101500Z/
      session.json                   schema oglo.session.v1: who, when, which files
      left/ep_0001/                  oglo.record() output for the left glove
      right/ep_0001/                 oglo.record() output for the right glove
      camera/video.mp4               your frames (absent with --camera fake)
      camera/video.timestamps.jsonl  {"frame_number", "host_t_ns", "wall_ns"} per frame
      markers.jsonl                  the moments you were asked to tap a taxel

Three rules keep a session usable later:

  RAW        The gloves stream raw ADC counts and nothing here calls zero().
             Preserve raw values for baseline analysis without replacing the
             glove's existing calibration. Already-cleaned counts lose information.
  ONE CLOCK  Host timestamps use time.monotonic_ns() on this computer, the value
             the SDK stores as host_t_ns. session.json records the wall clock at
             start, so wall ~= started_wall + (host_t_ns - started_monotonic_ns) / 1e9.
  TAP        When prompted, tap one taxel the camera can see, three times. The tap
             shows up in the video and in the tactile stream, so the offset between
             camera and gloves can be measured instead of guessed.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

import numpy as np

import oglo

# ---------------------------------------------------------------------------
# 1. CAMERA SKELETON -- the only part you need to adapt
# ---------------------------------------------------------------------------


@dataclass
class CameraInfo:
    """What a camera says about itself once opened. Copied into session.json."""

    backend: str
    device: str
    width: int
    height: int
    fps: float  # what the camera claims; the timestamps file is the truth


@dataclass
class CameraFrame:
    """A camera image and optional native timing; missing device fields stay null."""

    image: np.ndarray
    host_t_ns: Optional[int] = None
    device_timestamp: Optional[float] = None
    device_timestamp_unit: Optional[str] = None
    device_clock_domain: Optional[str] = None
    device_timestamp_meaning: Optional[str] = None


class FrameSource(Protocol):
    """Your camera, in three methods.

    Return CameraFrame(image) for an ordinary webcam: the recorder timestamps the
    read return with time.monotonic_ns(). This shares the glove's host clock, but
    includes camera buffering/decoding latency; it is NOT exposure time or the
    same observation boundary as the glove's USB read.

    If your camera API provides native timestamps, keep them separately. Example
    pseudocode (replace these methods with your camera SDK's actual API):

        packet = camera.wait_for_frame()
        received_ns = time.monotonic_ns()  # BEFORE image conversion/encoding
        return CameraFrame(
            image=packet.to_numpy(), host_t_ns=received_ns,
            device_timestamp=packet.timestamp_us, device_timestamp_unit="us",
            device_clock_domain="camera_boot",
            device_timestamp_meaning="exposure_start",  # only if documented
        )

    Do not invent device time from FPS, frame number, CAP_PROP_POS_MSEC or wall
    time. Native time units/domain must come from the camera documentation.
    None means the stream ended; ending before the session requests stop is an
    error. Implement a bounded read timeout in a real camera adapter. close() is
    called by the same worker that reads, never concurrently from another thread.
    """

    def open(self) -> CameraInfo: ...

    def read(self) -> Optional[CameraFrame]: ...

    def close(self) -> None: ...


class VideoSink(Protocol):
    """open() returns its output Path beneath the supplied camera directory, or None.

    Absolute paths are accepted. Keep output inside this directory so the whole
    session remains portable. write() must preserve frame order.
    """

    def open(self, path: Path, info: CameraInfo) -> Optional[Path]: ...

    def write(self, frame: np.ndarray) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# 2. REFERENCE CAMERAS
# ---------------------------------------------------------------------------


class OpenCVCamera:
    """Any UVC camera through OpenCV.

    A laptop webcam, an iPhone over Continuity Camera, or an OVISION: pass
    `--width 3840 --height 1080` for the OVISION so both eyes arrive side by side.
    OpenCV is imported lazily; the SDK itself does not depend on it.
    """

    def __init__(self, device: str = "0", width: Optional[int] = None,
                 height: Optional[int] = None, fps: Optional[float] = None) -> None:
        self.device = device
        self.width, self.height, self.fps = width, height, fps
        self._cap: Any = None

    def open(self) -> CameraInfo:
        import cv2  # noqa: PLC0415 -- optional dependency of the example only

        index: Any = int(self.device) if self.device.isdigit() else self.device
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError(f"could not open camera {self.device!r}")
        for prop, value in ((cv2.CAP_PROP_FRAME_WIDTH, self.width),
                            (cv2.CAP_PROP_FRAME_HEIGHT, self.height),
                            (cv2.CAP_PROP_FPS, self.fps)):
            if value:
                self._cap.set(prop, value)
        return CameraInfo(
            backend="opencv",
            device=self.device,
            width=int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            fps=float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0),
        )

    def read(self) -> Optional[CameraFrame]:
        ok, frame = self._cap.read()
        received_ns = time.monotonic_ns()
        return CameraFrame(frame, host_t_ns=received_ns) if ok else None

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()


class OpenCVVideoSink:
    """Re-encodes frames into an MP4 with OpenCV.

    The container's frame rate is nominal; `video.timestamps.jsonl` is the truth.
    If your camera already delivers encoded H.264, prefer a sink that stores the
    packets without decoding (for example ffmpeg with `-c copy`). The contract does
    not change: one file, one timestamp line per frame.
    """

    def __init__(self, codec: str = "mp4v") -> None:
        self.codec = codec
        self._writer: Any = None
        self._size: Optional[tuple] = None

    def open(self, path: Path, info: CameraInfo) -> Optional[Path]:
        import cv2  # noqa: PLC0415

        if (info.width <= 0 or info.height <= 0 or info.width % 2 or info.height % 2):
            raise ValueError("video dimensions must be positive and even; verify the camera mode")
        if not math.isfinite(info.fps):
            raise ValueError("camera reported a non-finite frame rate")
        self._size = (info.height, info.width, 3)
        fps = info.fps if info.fps > 0 else 30.0
        self._writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*self.codec), fps, (info.width, info.height)
        )
        if not self._writer.isOpened():
            raise RuntimeError(f"could not open video writer for {path}")
        return path

    def write(self, frame: np.ndarray) -> None:
        if frame.shape != self._size or frame.dtype != np.uint8:
            raise ValueError("video frame must match the configured size and uint8 BGR format")
        self._writer.write(frame)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()


class FakeCamera:
    """Paced synthetic frames so a session runs before your camera is ready."""

    def __init__(self, fps: float = 30.0, width: int = 64, height: int = 48) -> None:
        self.fps, self.width, self.height = fps, width, height
        self._next = 0.0
        self._n = 0

    def open(self) -> CameraInfo:
        self._next = time.monotonic()
        return CameraInfo("fake", "synthetic", self.width, self.height, self.fps)

    def read(self) -> Optional[CameraFrame]:
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next += 1.0 / self.fps
        self._n += 1
        return CameraFrame(np.full((self.height, self.width, 3), self._n % 256, dtype=np.uint8))

    def close(self) -> None:
        pass


class NullVideoSink:
    """Counts frames and writes no file. Pair it with FakeCamera for a dry run."""

    def open(self, path: Path, info: CameraInfo) -> Optional[Path]:
        return None

    def write(self, frame: np.ndarray) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 3. CAMERA RECORDER -- one thread, one clock, one line per frame
# ---------------------------------------------------------------------------


CAMERA_TIMEOUT_S = 5.0


class CameraRecorder:
    """One worker owns camera reads, writes and cleanup; errors reach the session."""

    def __init__(self, source: FrameSource, sink: VideoSink, directory: Path,
                 stem: str = "video") -> None:
        self.source, self.sink, self.directory, self.stem = source, sink, directory, stem
        self.info: Optional[CameraInfo] = None
        self.video_path: Optional[Path] = None
        self.timestamps_path = directory / f"{stem}.timestamps.jsonl"
        self.frames = 0
        self.error: Optional[str] = None
        self.last_progress = time.monotonic()
        self.first_host_t_ns: Optional[int] = None
        self.last_host_t_ns: Optional[int] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._abandoned = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> CameraInfo:
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()
        if not self._ready.wait(CAMERA_TIMEOUT_S):
            raise RuntimeError("camera did not deliver a first frame within the startup timeout")
        if self.error or self.frames == 0:
            raise RuntimeError(self.error or "camera ended without a frame")
        return self.info

    def _fail(self, message: str) -> None:
        self.error = f"{self.error}; {message}" if self.error else message

    def _run(self) -> None:
        try:
            info = self.source.open()
            if not math.isfinite(info.fps) or info.fps < 0:
                raise ValueError("camera must report a finite non-negative nominal fps")
            self.info = info
            if self._stop.is_set():
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            output = self.sink.open(self.directory / f"{self.stem}.mp4", self.info)
            if output is not None:
                output = Path(output).resolve()
                if not output.is_relative_to(self.directory.resolve()):
                    raise ValueError("video sink output must be inside the camera directory")
                self.video_path = output
            with self.timestamps_path.open("x", encoding="utf-8") as timestamps:
                while not self._stop.is_set():
                    frame = self.source.read()
                    received_ns = time.monotonic_ns()
                    wall_ns = time.time_ns()
                    if self._stop.is_set():
                        break
                    if frame is None:
                        raise RuntimeError("camera stream ended before the session requested stop")
                    host_t_ns = received_ns if frame.host_t_ns is None else frame.host_t_ns
                    if (type(host_t_ns) is not int or host_t_ns < 0 or host_t_ns > received_ns
                            or (self.last_host_t_ns is not None and host_t_ns < self.last_host_t_ns)):
                        raise ValueError("camera host_t_ns must be monotonic host nanoseconds at receipt")
                    native = frame.device_timestamp
                    if native is not None and (
                        isinstance(native, bool) or not isinstance(native, (int, float))
                        or not math.isfinite(native)
                        or not frame.device_timestamp_unit or not frame.device_clock_domain
                    ):
                        raise ValueError("native camera time requires a finite value, unit and clock domain")
                    self.sink.write(frame.image)
                    if self._abandoned.is_set():
                        break  # A timed-out encoder must not publish a later timestamp.
                    timestamps.write(json.dumps({
                        "frame_number": self.frames, "host_t_ns": host_t_ns,
                        "wall_ns": wall_ns,
                        "device_timestamp": native,
                        "device_timestamp_unit": frame.device_timestamp_unit,
                        "device_clock_domain": frame.device_clock_domain,
                        "device_timestamp_meaning": frame.device_timestamp_meaning,
                    }, allow_nan=False) + "\n")
                    timestamps.flush()
                    if self._abandoned.is_set():
                        break
                    if self.first_host_t_ns is None:
                        self.first_host_t_ns = host_t_ns
                    self.last_host_t_ns = host_t_ns
                    self.frames += 1
                    self.last_progress = time.monotonic()
                    self._ready.set()
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            # Attempt every close even when one fails. A timed-out reader remains
            # the sole owner: stop() must not release a native handle under it.
            for close in (self.sink.close, self.source.close):
                try:
                    close()
                except Exception as exc:
                    self._fail(f"camera cleanup: {type(exc).__name__}: {exc}")
            self._ready.set()

    def health_error(self) -> Optional[str]:
        if self.error:
            return self.error
        if self._thread is not None and not self._thread.is_alive():
            return "camera worker ended during capture"
        if time.monotonic() - self.last_progress > CAMERA_TIMEOUT_S:
            return "camera stopped delivering frames"
        return None

    @property
    def finalized(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    def stop(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CAMERA_TIMEOUT_S)
            if self._thread.is_alive():
                self._abandoned.set()
                self._fail("camera thread did not stop within the shutdown timeout")
        return self.frames


# ---------------------------------------------------------------------------
# 4. THE SESSION
# ---------------------------------------------------------------------------

TAP_LEAD_S = 5.0  # ask for the closing tap this long before a timed session ends


class Markers:
    """Append-only `markers.jsonl`: when the operator was asked to do something."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.labels: list = []
        self._file = path.open("w", encoding="utf-8")

    def add(self, label: str) -> None:
        row = {"label": label, "host_t_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}
        self._file.write(json.dumps(row) + "\n")
        self._file.flush()
        self.labels.append(label)

    def close(self) -> None:
        self._file.close()


def new_session_dir(root: Path, started_wall: float) -> Path:
    stamp = datetime.fromtimestamp(started_wall, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1000):
        path = root / (f"session_{stamp}" if n == 0 else f"session_{stamp}_{n}")
        try:
            path.mkdir(parents=True, exist_ok=False)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"could not reserve a session directory under {root}")


def _episode_summary(session: Path, glove: oglo.Glove, outcome: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"serial": glove.info.serial, "fw_rev": glove.info.fw_rev}
    episode = outcome.get("episode")
    if episode is not None:
        meta = json.loads((Path(episode) / "meta.json").read_text())
        summary.update(
            episode=str(Path(episode).relative_to(session)),
            complete=bool(meta.get("complete")),
            stream_clean=meta.get("stream_clean"),
            stop_reason=meta.get("stop_reason"),
        )
    else:
        summary.update(episode=None, complete=None)
    if "error" in outcome:
        summary["error"] = outcome["error"]
    return summary


def write_manifest(session: Path, manifest: Dict[str, Any]) -> None:
    staging = session / ".session.json.tmp"
    staging.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    staging.replace(session / "session.json")


def run_session(out_root: Path, left: oglo.Glove, right: oglo.Glove, camera: FrameSource,
                sink: VideoSink, *, seconds: Optional[float], stream: str = "raw",
                stop_event: Optional[threading.Event] = None) -> Path:
    """Seal a session even on failure. Inspect complete/errors before using it.

    seconds=None runs until stop_event or Ctrl-C. The caller owns the gloves.
    RAW changes are registered for restoration BEFORE attempting each mutation.
    """
    if stream not in ("raw", "keep"):
        raise ValueError("stream must be 'raw' or 'keep'")
    if seconds is not None and (
        isinstance(seconds, bool) or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds) or seconds <= 0
    ):
        raise ValueError("seconds must be None or a positive finite number")
    if left.info.side != "left" or right.info.side != "right":
        raise ValueError("run_session requires a left and a right glove")
    stop_event = stop_event if stop_event is not None else threading.Event()
    started_wall, started_mono_ns = time.time(), time.monotonic_ns()
    session = new_session_dir(Path(out_root).resolve(), started_wall)
    errors: list = []
    manifest = {
        "schema": "oglo.session.v1", "sdk_version": oglo.__version__,
        "example": "examples/03_full_session.py", "complete": False,
        "alignment_validated": False, "synthetic_camera": isinstance(camera, FakeCamera),
        "started_wall": started_wall, "started_monotonic_ns": started_mono_ns,
        "seconds_requested": seconds, "errors": errors,
        "clock": ("host_t_ns is time.monotonic_ns on one PC; camera read return and "
                  "glove USB receive are different observation boundaries; "
                  "wall ~= started_wall + (host_t_ns - started_monotonic_ns) / 1e9"),
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "gloves": {side: {"serial": g.info.serial, "episode": None, "complete": False}
                   for side, g in (("left", left), ("right", right))},
        "camera": {"video": None, "timestamps": None, "frames": 0},
        "markers": "markers.jsonl", "stop_reason": "in_progress",
    }
    write_manifest(session, manifest)
    recorder = CameraRecorder(camera, sink, session / "camera")
    markers = None
    restores = []
    outcomes: Dict[str, Dict[str, Any]] = {}
    futures = {}
    try:
        markers = Markers(session / "markers.jsonl")
        for glove in (left, right):
            if stream == "raw" and glove.info.stream_clean:
                restores.append((glove, glove.info.stream_thr))
                glove.raw()
        recorder.start()  # waits for the first saved camera frame, not just open()
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                for glove in (left, right):
                    futures[glove.info.side] = pool.submit(
                        oglo.record, session / glove.info.side, seconds,
                        glove=glove, stop_event=stop_event,
                    )
                deadline = None if seconds is None else time.monotonic() + seconds
                closing_tap = seconds is not None and seconds > 2 * TAP_LEAD_S
                while not all(f.done() for f in futures.values()):
                    camera_error = recorder.health_error()
                    if camera_error:
                        raise RuntimeError(camera_error)
                    if any(f.done() and f.exception() is not None for f in futures.values()):
                        stop_event.set()
                        break
                    # record() publishes an initial meta.json when capture starts.
                    # Wait for both markers so the opening taps reach both streams.
                    if "tap_start" not in markers.labels and all(
                        any((session / side).glob("ep_*/meta.json")) for side in futures
                    ) and not stop_event.is_set():
                        markers.add("tap_start")
                        print("recording. Tap one visible taxel three times on EACH hand, now.")
                    if (closing_tap and deadline is not None and not stop_event.is_set()
                            and "tap_start" in markers.labels
                            and time.monotonic() >= deadline - TAP_LEAD_S):
                        markers.add("tap_end")
                        print("almost done. Repeat the visible taps on each hand, now.")
                        closing_tap = False
                    time.sleep(0.05)
            finally:
                # Any exceptional exit cancels siblings before executor shutdown
                # waits for them, including a right-hand failure or camera loss.
                if not all(f.done() for f in futures.values()):
                    stop_event.set()
    except KeyboardInterrupt:
        print("stopping; sealing both episodes and the video.")
        stop_event.set()
    except Exception as exc:
        stop_event.set()
        errors.append(f"session: {type(exc).__name__}: {exc}")
    finally:
        for side, future in futures.items():
            try:
                outcomes[side] = {"episode": future.result()}
            except Exception as exc:
                outcomes[side] = {"episode": getattr(exc, "partial_episode", None),
                                  "error": f"{type(exc).__name__}: {exc}"}
                errors.append(f"{side}: {type(exc).__name__}: {exc}")
        try:
            recorder.stop()
            if recorder.error:
                errors.append(f"camera: {recorder.error}")
            if recorder.frames < 2:
                errors.append("camera: fewer than two saved frames")
        except Exception as exc:
            errors.append(f"camera cleanup: {type(exc).__name__}: {exc}")
        for glove, threshold in reversed(restores):
            try:
                glove.clean(threshold=threshold)
            except Exception as exc:
                errors.append(f"restore {glove.info.serial}: {type(exc).__name__}: {exc}")
        if markers is not None:
            try:
                if stop_event.is_set():
                    markers.add("stop_requested")
                markers.close()
            except Exception as exc:
                errors.append(f"markers cleanup: {type(exc).__name__}: {exc}")
        for side, glove in (("left", left), ("right", right)):
            try:
                manifest["gloves"][side] = _episode_summary(session, glove, outcomes.get(side, {}))
            except Exception as exc:
                errors.append(f"{side} metadata: {type(exc).__name__}: {exc}")
            if manifest["gloves"][side].get("complete") is not True:
                errors.append(f"{side}: episode missing or incomplete")
        camera_finalized = recorder.finalized
        manifest["camera"] = {
            **(asdict(recorder.info) if recorder.info else {}),
            "video": str(recorder.video_path.relative_to(session)) if recorder.video_path else None,
            "timestamps": str(recorder.timestamps_path.relative_to(session))
                          if recorder.timestamps_path.exists() else None,
            "finalized": camera_finalized,
            "frames": recorder.frames if camera_finalized else None,
            "frames_observed_at_stop": recorder.frames,
            "first_host_t_ns": recorder.first_host_t_ns if camera_finalized else None,
            "last_host_t_ns": recorder.last_host_t_ns if camera_finalized else None,
            "error": recorder.error,
        }
        manifest.update(ended_wall=time.time(), ended_monotonic_ns=time.monotonic_ns(),
                        stop_reason="error" if errors else "cancelled" if stop_event.is_set() else "duration",
                        complete=not errors)
        write_manifest(session, manifest)
    return session


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Record both gloves and your camera as one session.")
    p.add_argument("--out", default="sessions", help="where session directories go")
    p.add_argument("--seconds", type=float, default=None, help="omit to record until Ctrl-C")
    p.add_argument("--camera", choices=("opencv", "fake"), default="opencv")
    p.add_argument("--device", default="0", help="OpenCV index or path, e.g. 0 or /dev/video2")
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--fps", type=float, default=None)
    p.add_argument("--stream", choices=("raw", "keep"), default="raw",
                   help="raw: switch a clean glove to raw for the session and restore it after; "
                        "keep: record the glove as it is")
    a = p.parse_args(argv)

    if a.camera == "fake":
        camera: FrameSource = FakeCamera()
        sink: VideoSink = NullVideoSink()
    else:
        camera = OpenCVCamera(a.device, a.width, a.height, a.fps)
        sink = OpenCVVideoSink()

    if a.seconds is not None and (not math.isfinite(a.seconds) or a.seconds <= 0):
        p.error("--seconds must be positive and finite")
    for name, value in (("width", a.width), ("height", a.height), ("fps", a.fps)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            p.error(f"--{name} must be positive and finite")
    gloves = []
    close_errors = []
    try:
        left, right = oglo.connect_pair()
        gloves = [left, right]
        print(f"left  {left.info.serial}  fw {left.info.fw_rev}  clean={left.info.stream_clean}")
        print(f"right {right.info.serial}  fw {right.info.fw_rev}  clean={right.info.stream_clean}")
        session = run_session(Path(a.out), left, right, camera, sink,
                              seconds=a.seconds, stream=a.stream)
    except Exception as exc:
        print(f"could not record session: {exc}", file=sys.stderr)
        return 1
    finally:
        for glove in gloves:
            try:
                glove.close()
            except Exception as exc:
                close_errors.append(f"close {glove.info.serial}: {exc}")
                print(close_errors[-1], file=sys.stderr)

    manifest = json.loads((session / "session.json").read_text())
    if close_errors:
        manifest["errors"].extend(close_errors)
        manifest.update(complete=False, stop_reason="error")
        write_manifest(session, manifest)
    print(f"wrote {session}")
    for problem in manifest["errors"]:
        print("problem:", problem, file=sys.stderr)
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
