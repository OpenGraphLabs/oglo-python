#!/usr/bin/env python3
"""One-window session tool: calibrate, record webcam + gloves, align, index.

Keys in the window (a USB foot switch that types letters works the same way):

    g  start recording          h  stop and save          x  stop and discard
    z  calibrate (idle only)    q  quit (idle only; waits for background alignment)

Every episode goes to ``<out>/<task>/<task>_NNN/``, numbered per task. A saved episode
gets ``alignment.preview.jsonl`` from a background thread so the next ``g`` never
waits. A discarded episode moves to ``<out>/<task>/_discarded/`` and a failed one to
``<out>/<task>/_failed/``, so ``<task>/`` only ever holds complete, aligned episodes.
On quit ``dataset.py`` rewrites the root ``episodes.jsonl`` and ``README.md``.
``--seconds`` is only a safety cap; reaching it counts as save.

Recording itself is ``capture.py``; this file adds the state machine around it and
hands it a stop event. On an OVISION-EGO-V1 with SyncField 0.8.14 installed the camera
goes through the native backend in ``ovision.py`` (original H.264, camera IMU, exposure
timing, calibration); any other camera goes through OpenCV and ``--codec``.
``--camera-backend`` forces either. An episode is published (indexed, uploaded) only
once ``dataset.publishable`` accepts it: complete manifest, files present, alignment
with one row per frame.
Overlay text is ASCII because OpenCV's Hershey fonts have no CJK.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time

import cv2
import numpy as np
import oglo
from oglo._doctor import FAIL, doctor

HERE = Path(__file__).resolve().parent


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(f"camera_glove_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load_sibling("capture")
align = load_sibling("align")
dataset = load_sibling("dataset")
sys.modules.setdefault("capture", capture)  # ovision.py does ``from capture import``: share one copy.
ovision = load_sibling("ovision")  # Imports SyncField only inside the functions that need it.

WINDOW = "OGLO collect  (g record, h save, x discard, z calibrate, q quit)"
BACKENDS = ("auto", "opencv", "ovision")
OVISION_IDLE_PERIOD = 0.05  # seconds per idle window refresh when the OVISION stream feeds it
FONT = cv2.FONT_HERSHEY_SIMPLEX
HEAT_SCALE = 1400.0  # counts above baseline that saturate a cell (OGLO Studio "Taxel" view scale)
HEAT_GAMMA = 0.55  # same gamma as OGLO Studio: light touches still show
CELL = 18  # pixels per taxel; 3 digits fit, 4 digits shrink
DISCARDED = "_discarded"  # under <out>/<task>/: episodes ended with x
FAILED = "_failed"  # under <out>/<task>/: episodes that did not complete or align
PREVIEW_WIDTH = 1280  # on-screen width; set from --preview-width, never touches the recording
KEY_START, KEY_SAVE, KEY_DISCARD, KEY_ZERO, KEY_QUIT, KEY_VIEW = (ord(k) for k in "ghxzqc")
RAW_FULL = 4095.0  # a raw ADC cell saturates at the converter limit (Studio RAW view)


# -- display ---------------------------------------------------------------------

class WindowDisplay:
    """The one OpenCV window. ``listen=False`` shows a frame but ignores keys."""

    def show(self, image, listen=True):
        cv2.imshow(WINDOW, image)
        key = cv2.waitKey(1) & 0xFF
        return key if listen else -1

    def flush(self, limit=100):
        """Drop keys typed, or auto-repeated by a held pedal, during the last phase."""
        for _ in range(limit):
            if cv2.waitKey(1) == -1:
                return

    def close(self):
        cv2.destroyAllWindows()


def fit_preview(image):
    """A copy of the frame shrunk to PREVIEW_WIDTH for the window (never enlarged)."""
    height, width = image.shape[:2]
    if width <= PREVIEW_WIDTH:
        return image.copy()
    size = (PREVIEW_WIDTH, max(1, round(height * PREVIEW_WIDTH / width)))
    return cv2.resize(image, size, interpolation=cv2.INTER_AREA)


def put_lines(image, lines, origin=(10, 24), color=(255, 255, 255), scale=0.55):
    x, y = origin
    for line in lines:
        cv2.putText(image, line, (x + 1, y + 1), FONT, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(image, line, (x, y), FONT, scale, color, 1, cv2.LINE_AA)
        y += int(26 * scale / 0.55)
    return y


def placeholder(text, size=(720, 1280)):
    """A dark frame carrying one message, shown while the camera has no image yet."""
    image = np.full((*size, 3), 30, dtype=np.uint8)
    put_lines(image, [text], origin=(20, size[0] // 2), color=(0, 220, 255), scale=0.7)
    return image


GRID_GAP = 6  # pixels between the five finger grids
GRID_W = 5 * 4 * CELL + 4 * GRID_GAP
GRID_H = 4 * CELL


def screen_grid(block):
    """One finger's ``(row, col)`` block -> the 4x4 as OGLO Studio draws it.

    Wire index inside a finger is ``row * 4 + col``. Studio (viewer-core/usb.html,
    ``drawGlove``) puts ``col`` on the vertical axis with col 0 (the fingertip) at
    the top and ``row`` on the horizontal axis with row 0 at the RIGHT, for both
    hands. Returned as ``screen[y][x]``.
    """
    return np.asarray(block).T[:, ::-1]


def finger_grids(values, thr=0, side="right", scale=HEAT_SCALE):
    """(5, 4, 4) counts above baseline -> one image with a numbered 4x4 grid per finger.

    Same drawing as the OGLO Studio "Taxel" view: every cell shows its rounded
    count, cells below ``thr`` are shown as 0, the colour follows
    ``(count / HEAT_SCALE) ** HEAT_GAMMA``.  :meth:`Collector.grid_values` clamps
    the calibrated view at 0 (the sweep zero is an envelope, so a resting hand sits
    below it); a negative value can only reach here from another caller and is drawn
    as a blue cell with the signed number.

    ``values`` is in ``oglo.oriented_counts`` order (thumb first, col 0 = tip on
    every finger). Each grid is laid out by :func:`screen_grid`: fingertip at the
    top, row 0 on the right. Finger order on screen follows Studio too: a right
    hand thumb..pinky left to right, a left hand mirrored (pinky..thumb).
    """
    grid = np.asarray(values, dtype=np.float32).reshape(5, 4, 4)
    order = range(5) if side != "left" else range(4, -1, -1)
    image = np.full((GRID_H, GRID_W, 3), 40, dtype=np.uint8)
    for slot, finger in enumerate(order):
        x0 = slot * (4 * CELL + GRID_GAP)
        screen = screen_grid(grid[finger])
        for row in range(4):
            for col in range(4):
                value = float(screen[row, col])
                shown = 0 if 0 <= value < thr else int(round(value))
                level = min(1.0, max(0.0, shown / scale)) ** HEAT_GAMMA if shown > 0 else 0.0
                if shown < 0:
                    colour = (120, 60, 20)  # BGR: dark blue for "below zero"
                elif level < 0.015:
                    colour = (60, 60, 60)
                else:
                    colour = tuple(int(c) for c in cv2.applyColorMap(
                        np.array([[int(level * 255)]], dtype=np.uint8), cv2.COLORMAP_INFERNO)[0, 0])
                x, y = x0 + col * CELL, row * CELL
                cv2.rectangle(image, (x + 1, y + 1), (x + CELL - 2, y + CELL - 2), colour, -1)
                text = str(shown)
                font_scale = 0.3
                (tw, th), _ = cv2.getTextSize(text, FONT, font_scale, 1)
                if tw > CELL - 3:  # 4 digits or a minus sign: shrink to fit the cell.
                    font_scale *= (CELL - 3) / tw
                    (tw, th), _ = cv2.getTextSize(text, FONT, font_scale, 1)
                ink = (255, 255, 255) if level < 0.55 else (20, 20, 20)
                cv2.putText(image, text, (x + (CELL - tw) // 2, y + (CELL + th) // 2), FONT,
                            font_scale, ink, 1, cv2.LINE_AA)
    return image


def paste(image, patch, x, y):
    h, w = patch.shape[:2]
    if y + h <= image.shape[0] and x + w <= image.shape[1]:
        image[y:y + h, x:x + w] = patch


def draw_gloves(frame, items):
    """Bottom-left finger grids, one block per glove.

    ``items`` = [(label, values, thr)] or [(label, values, thr, scale)]; ``scale`` is
    the count that saturates a cell (``HEAT_SCALE`` unless given).
    """
    x = 10
    y = frame.shape[0] - GRID_H - 10
    for label, values, thr, *rest in items:
        if values is not None:
            side = "left" if label.lower().startswith("l") else "right"
            paste(frame, finger_grids(values, thr, side, rest[0] if rest else HEAT_SCALE), x, y)
            names = oglo.FINGERS if side == "right" else oglo.FINGERS[::-1]
            for finger, name in enumerate(names):
                cv2.putText(frame, name[:2], (x + finger * (4 * CELL + GRID_GAP) + 2, y - 4),
                            FONT, 0.35, (200, 200, 200), 1, cv2.LINE_AA)
            peak = int(np.max(values))
            label = f"{label}  peak {peak}"
        cv2.putText(frame, label, (x, y - 18), FONT, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        x += GRID_W + 24
    return frame


def draw_recording(image, elapsed, cap_seconds, label, task, gloves=()):
    frame = fit_preview(image)
    draw_gloves(frame, gloves)
    cv2.circle(frame, (18, 18), 8, (0, 0, 255), -1)
    put_lines(frame, [f"REC {elapsed:6.1f} s  (max {cap_seconds:g})   {label}",
                      f"task: {task}", "h = stop and save     x = stop and discard     c = raw/cal view"],
              origin=(34, 24))
    return frame


# -- recording camera with a live preview ----------------------------------------

class RecordingControl:
    """Shared by the state machine and the preview proxy of one episode."""

    def __init__(self, on_view=None):
        self.stop = threading.Event()  # Handed to capture.capture(); set ends the episode.
        self.outcome = None            # "save" or "discard" once a key decided it.
        self.on_view = on_view         # c: display-only toggle, never a decision.

    def press(self, key):
        if key == KEY_VIEW:
            if self.on_view is not None:
                self.on_view()
            return
        if self.stop.is_set():
            return  # Whatever comes after the first decision is a repeat.
        if key == KEY_SAVE:
            self.outcome = "save"
        elif key == KEY_DISCARD:
            self.outcome = "discard"
        else:
            return
        self.stop.set()


class RecordingOverlay:
    """The REC window of one episode: draws the newest frame, routes its key to the control.

    The camera backend calls ``show`` whenever it has a moment: the webcam proxy before
    each read, the OVISION backend on every 50 ms tick with its latest preview frame
    (None until a keyframe was decoded; the last one, or a placeholder, is drawn then so
    the window keeps listening for h / x).
    """

    def __init__(self, display, control, label, task, cap_seconds, gloves=lambda: ()):
        self._display, self._control = display, control
        self._gloves = gloves  # () -> [(label, (5, 4, 4) values or None)]
        self._label, self._task, self._cap = label, task, cap_seconds
        self.last = None
        self._started = None

    def show(self, image=None):
        if image is not None:
            self.last = image
        if self._started is None:
            self._started = time.monotonic()
        elapsed = time.monotonic() - self._started
        frame = self.last if self.last is not None else placeholder("waiting for the first camera keyframe")
        key = self._display.show(draw_recording(frame, elapsed, self._cap,
                                                self._label, self._task, self._gloves()))
        self._control.press(key)


class _PreviewProxy:
    """Wrap ``cv2.VideoCapture`` so each ``read()`` first shows the previous frame.

    Showing happens before the real read, so ``host_received_ns`` (taken right after
    ``read()`` returns in capture.py) never includes display time. Only the informational
    ``host_read_started_ns`` absorbs the ~1-3 ms of ``imshow``/``waitKey``. The key that
    ``show`` returns goes to the episode's ``RecordingControl``.
    """

    def __init__(self, camera, overlay):
        self._camera = camera
        self._overlay = overlay
        self._last = None

    def read(self):
        if self._last is not None:
            self._overlay.show(self._last)
        ok, image = self._camera.read()
        if ok and image is not None:
            self._last = image
        return ok, image

    def release(self):
        self._camera.release()

    def __getattr__(self, name):
        return getattr(self._camera, name)


class OverlayCamera(capture.WebcamCapture):
    """capture.py camera backend that reuses the idle window's open camera.

    ``camera`` is the Collector's ``cv2.VideoCapture``; reopening it would cost about
    1.5 s per episode on a UVC stereo camera. The Collector owns it, so ``close()``
    leaves it open. Without ``camera`` it opens one itself like the parent.
    """

    def __init__(self, args, output, display, control, label, camera=None, gloves=lambda: ()):
        super().__init__(args, output)
        self._overlay = RecordingOverlay(display, control, label, args.task, args.seconds, gloves)
        self._shared = camera

    def prepare(self):
        if self._shared is None:
            super().prepare()  # Opens the camera and discards the setup frame.
        else:
            self.camera = self._shared
            self.metadata["fps_request_accepted"] = bool(self.camera.set(cv2.CAP_PROP_FPS, self.args.fps))
            self.metadata["backend"] = self.camera.getBackendName()
        self.camera = _PreviewProxy(self.camera, self._overlay)

    def close(self):
        if self._shared is None:
            super().close()


class OvisionIdleSource:
    """``read()`` for the idle window from a live OVISION stream (the same ``read``
    contract as ``cv2.VideoCapture``, so every idle loop stays as it is).

    The adapter decodes a left-eye preview on keyframes only (about one per second on
    this firmware), so ``latest_frame`` is a snapshot rather than a blocking read and
    the window is paced here instead of by the camera. A stream whose capture thread
    died reads as a failed camera, which ends the session like an unplugged webcam.
    """

    def __init__(self, stream, period=None):
        self.stream = stream
        self.period = OVISION_IDLE_PERIOD if period is None else period
        self.last = None

    def read(self):
        time.sleep(self.period)
        if not self.stream.capture_ready():
            return False, None
        frame = self.stream.latest_frame
        if frame is not None:
            self.last = frame
        if self.last is None:
            return True, placeholder("waiting for the first camera keyframe")
        return True, self.last

    def release(self):
        self.stream.disconnect()


def choose_backend(requested, camera_index):
    """``--camera-backend`` resolved to ``(backend, reason)``.

    ``auto`` takes the native OVISION backend when SyncField 0.8.14 is installed and the
    camera answers the adapter's calibration read, otherwise OpenCV with ``reason`` saying
    why, so that episodes without camera IMU never happen silently. Explicit ``ovision``
    raises instead of falling back.
    """
    if requested == "opencv":
        return "opencv", None
    reason = ovision.problem(Path(f"/dev/video{camera_index}"))
    if reason is None:
        return "ovision", None
    if requested == "ovision":
        raise RuntimeError(f"--camera-backend ovision: {reason}")
    return "opencv", reason


class TactilePeek:
    """A glove whose every ``read_batch`` also remembers the newest tactile frame.

    ``oglo.record`` drains the glove itself while an episode runs, so the window
    cannot poll it; wrapping the glove lets the overlay see what is being recorded
    without touching the SDK. Everything else is delegated unchanged.

    Between episodes a reader thread keeps calling ``read_batch`` so the port is
    drained every ~50 ms whatever the window thread is doing. Linux buffers only
    4095 bytes per tty (~85 ms of stream); once that is full the kernel stops taking
    data and the next command written wedges the firmware until a replug (measured
    2026-09-23). The window thread only reads ``latest``. Stop the reader before
    any command, calibration, recording, or close: ``read_batch`` restarts a stopped
    stream, and the SDK is not thread-safe.
    """

    def __init__(self, glove):
        self._glove = glove
        self.latest = None
        self._thread = None
        self._halt = threading.Event()
        self.error = None

    def __getattr__(self, name):
        return getattr(self._glove, name)

    def read_batch(self, **kwargs):
        batch = self._glove.read_batch(**kwargs)
        if batch.tactile:
            self.latest = batch.tactile[-1]
        return batch

    def start_reader(self):
        if self._thread is not None:
            return
        self._halt.clear()
        self.error = None
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name=f"read-{self._glove.info.side}")
        self._thread.start()

    def _pump(self):
        try:
            while not self._halt.is_set():
                if not self.read_batch():  # A real read blocks ~50 ms; a fake may not.
                    time.sleep(0.005)
        except Exception as exc:
            self.error = exc

    def stop_reader(self):
        if self._thread is None:
            return
        self._halt.set()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            # A read that never returns means the port is dead. Keep the thread
            # recorded so start_reader() cannot add a second reader on top of it,
            # and refuse to let the caller send commands beside it.
            raise oglo.DeviceError(
                f"{self._glove.info.side} glove: the reader thread did not stop within 5 s; "
                "the port is not answering (unplug the glove, wait 10 s, plug it back in)")
        self._thread = None

    def check(self):
        """Raise on the calling thread whatever ended the reader."""
        if self.error is not None:
            error, self.error = self.error, None
            raise error


# -- USB topology ----------------------------------------------------------------

def usb_bus(sys_link):
    """Root hub name ('usb1') of a /sys/class/... device link; None when not a Linux USB device."""
    try:
        real = Path(sys_link).resolve(strict=True)
    except OSError:
        return None
    match = re.search(r"/(usb\d+)/", f"{real}/")
    return match.group(1) if match else None


V4L2_SYSFS = Path("/sys/class/video4linux")
_VIDIOC_QUERYCAP = 0x80685600
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001


def is_capture_node(index):
    """True when /dev/video<index> is a video capture node, not a UVC metadata node.

    Every UVC camera enumerates two nodes; OpenCV can only open the capture one.
    """
    import fcntl  # Linux only; a numeric --camera never gets here.

    buf = bytearray(104)  # struct v4l2_capability; device_caps at byte 88
    try:
        with open(f"/dev/video{index}", "rb", buffering=0) as node:
            fcntl.ioctl(node, _VIDIOC_QUERYCAP, buf)
    except OSError:
        return False
    return bool(int.from_bytes(buf[88:92], "little") & _V4L2_CAP_VIDEO_CAPTURE)


def resolve_camera(spec, sysfs=V4L2_SYSFS, capture_node=is_capture_node):
    """``--camera`` as an OpenCV index: a number as given, otherwise a camera name.

    /dev/video numbering follows USB enumeration order and changes across reboots
    (the stereo camera was video2 one boot and video0 the next), so a name (a
    case-insensitive substring of the V4L2 card name, such as ``SC233``) is the
    stable way to say which camera. The lowest capture node of the matching camera
    wins. Returns (index, card name).
    """
    text = str(spec).strip()
    if text.lstrip("-").isdigit():
        index = int(text)
        if index < 0:
            raise SystemExit("--camera must be a nonnegative device index or a camera name")
        return index, None
    names = []
    for node in sorted(sysfs.glob("video*"), key=lambda n: int(n.name[5:]) if n.name[5:].isdigit() else 1 << 30):
        try:
            card = (node / "name").read_text().strip()
        except OSError:
            continue
        index = int(node.name[5:])
        names.append((index, card))
        if text.lower() in card.lower() and capture_node(index):
            return index, card
    listing = ", ".join(f"video{i} = {c}" for i, c in names) or "no /dev/video nodes"
    raise SystemExit(f"--camera {spec!r}: no capture node with that name; found: {listing}")


def gloves_sharing_camera_bus(camera_index, candidates):
    """Attached gloves on the same USB root hub as the camera: [(device, bus)].

    A 3200x1200 stereo camera saturates a 480 Mbit/s root hub; a full-speed glove
    behind the same hub gets its IN transfers starved, the firmware TX task blocks
    holding the serial mutex, and the next text command is never answered
    (only a replug recovers it). Measured 2026-09-23: 20/20 stop cycles fine on
    both gloves without the camera, the shared-hub glove dead at cycle 15 with it.
    """
    camera = usb_bus(f"/sys/class/video4linux/video{camera_index}/device")
    if camera is None:
        return []
    devices = [getattr(c, "device", str(c)) for c in candidates]
    return [(d, camera) for d in devices if usb_bus(f"/sys/class/tty/{Path(d).name}/device") == camera]


# -- session folders ---------------------------------------------------------------

def task_slug(task):
    """Folder name of a task: its ASCII letters and digits, plus a hash when that loses text.

    Two wordings that differ only in case, spacing or punctuation share a folder. A
    task written in another script (Korean, Chinese, ...) keeps its identity through
    six hex digits of its text, so distinct tasks never merge into one ``session``
    folder; :func:`check_task_folder` catches the remaining collisions.
    """
    text = task.strip()
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
    if any(ch.isalnum() and not ch.isascii() for ch in text):
        slug = f"{slug or 'task'}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]}"
    if slug in dataset.RESERVED:  # gloves/ is the per-glove folder; its episodes would never index.
        slug += "_task"
    return slug or "session"


def check_task_folder(out, task):
    """Refuse a task folder that already holds episodes recorded under another wording.

    The manifests, not the folder name, carry the task text; one folder must mean one
    task or the dataset card and index would merge two activities.
    """
    folder = out / task_slug(task)
    if not folder.is_dir():
        return
    for manifest in sorted(folder.glob("*/manifest.json")) + sorted(folder.glob("_*/*/manifest.json")):
        try:
            recorded = json.loads(manifest.read_text(encoding="utf-8")).get("task_description")
        except (OSError, ValueError):
            continue
        if recorded is not None and recorded.strip() != task.strip():
            raise RuntimeError(f"task folder {folder} already holds episodes of {recorded!r}; "
                               f"use that exact wording for --task, or another --out")
        return  # One manifest is enough: every episode in the folder passed this check.


def next_session_dir(out, task):
    """First ``<out>/<slug>/<slug>_NNN`` that is not a saved, discarded, or failed episode."""
    slug = task_slug(task)
    folder = out / slug
    number = 1
    while True:
        name = f"{slug}_{number:03d}"
        taken = (folder / name, folder / DISCARDED / name, folder / FAILED / name)
        if not any(path.exists() for path in taken):
            return folder / name
        number += 1


def alignment_hit_rates(preview_path):
    """Fraction of camera frames with a tactile / imu neighbour, per glove side."""
    hits, total = {}, 0
    for line in preview_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        total += 1
        for glove in row["gloves"]:
            entry = hits.setdefault(glove["side"], {"tactile": 0, "imu": 0})
            for stream in ("tactile", "imu"):
                if glove[stream] is not None:
                    entry[stream] += 1
    return {side: {k: v / total for k, v in counts.items()} for side, counts in hits.items()}, total


# -- the state machine -----------------------------------------------------------

class Collector:
    def __init__(self, args, display=None):
        self.args = args
        self.display = display or WindowDisplay()
        self.gloves = ()
        self.camera = None    # cv2.VideoCapture, or OvisionIdleSource over ``stream``
        self.stream = None    # the live OVISION stream when the backend is ovision
        self.baselines = {}   # serial -> np.ndarray(80) or None
        self.last_calibration = {}  # serial -> GET ZERO recipe from the last sweep
        self.episodes = []    # dicts: session, complete, outcome (saved / discarded / failed)
        self.status = "ready"
        self.show_raw = False  # c toggles the grids between raw ADC and counts above zero
        self.reconnect_pause = 1.0  # seconds between connect attempts to a dead glove
        self._jobs = queue.Queue()  # saved sessions waiting for alignment
        self._worker = None

    # devices ---------------------------------------------------------------

    def open_devices(self):
        """One glove when one is attached, a verified pair when two are, unless told otherwise.

        Two gloves of the same side are refused by ``connect_pair`` (SET SIDE on the
        device is the only fix); they are never recorded together.
        """
        pair = self.args.pair
        candidates = oglo.list_candidates()
        shared = gloves_sharing_camera_bus(self.args.camera, candidates)
        if shared and not self.args.allow_shared_usb:
            ports = ", ".join(f"{device} (root hub {bus})" for device, bus in shared)
            raise RuntimeError(
                f"glove on {ports} shares its USB controller with camera {self.args.camera}. "
                "The camera's traffic starves the glove and its firmware locks up mid-session "
                "(only a replug recovers it). Move that glove to a USB port on another "
                "controller, or pass --allow-shared-usb to record anyway.")
        if not pair and self.args.serial is None and len(candidates) == 2:
            print("Two gloves attached: recording them as a left + right pair.", flush=True)
            pair = True
        self.args.pair = pair  # capture.py sees the same decision in its manifest.
        self.open_gloves()
        if self.args.camera_backend == "ovision":
            # One live stream for the whole session: idle preview and every episode.
            self.stream = ovision.open_stream(self.video_device, None, self.args.out)
            self.camera = OvisionIdleSource(self.stream)
            return
        self.camera = cv2.VideoCapture(self.args.camera)
        if not self.camera.isOpened():
            raise RuntimeError("Cannot open camera; check --camera and OS camera permission")
        capture.read_camera(self.camera)  # Check connection; discard this setup frame.

    @property
    def video_device(self):
        return Path(f"/dev/video{self.args.camera}")

    def open_gloves(self):
        pair = self.args.pair
        gloves = oglo.connect_pair() if pair else (oglo.connect(serial=self.args.serial),)
        self.gloves = tuple(TactilePeek(glove) for glove in gloves)
        for glove in self.gloves:
            self.baselines[glove.info.serial] = self._read_baseline(glove)
        self.start_readers()

    def start_readers(self):
        for glove in self.gloves:
            glove.start_reader()

    def stop_readers(self):
        for glove in self.gloves:
            glove.stop_reader()

    def stop_streams(self):
        """Quiet every glove, each read until the moment its own STREAM TAG OFF goes out.

        Stopping one glove takes ~0.2 s of drain; a sibling whose reader was already
        halted would fill the kernel buffer (4095 bytes, ~85 ms) meanwhile.
        """
        for glove in self.gloves:
            glove.stop_reader()
            glove.stop()
            # z may switch the glove RAW <-> CLEAN before the stream restarts; a frame
            # of the old kind read as the new one raises (residual on a RAW frame).
            glove.latest = None

    def close_gloves(self):
        for glove in self.gloves:  # Each glove is read until its own close, as in stop_streams().
            try:
                glove.stop_reader()
            except Exception as exc:  # A stuck reader: closing the port is what ends it.
                print(f"{exc}", file=sys.stderr, flush=True)
            try:
                glove.close()
            except Exception:
                pass
        self.gloves = ()

    def close_devices(self):
        self.close_gloves()
        if self.camera is not None:
            self.camera.release()  # For the OVISION source this disconnects the stream.
            self.camera = None
            self.stream = None

    def recover_camera(self):
        """After a failed episode: an OVISION stream whose capture died is reconnected.

        ``capture_ready()`` stays false after a camera-side error until the adapter
        reconnects, and true when a glove was the cause, so this only reopens the camera
        when the camera failed. The OpenCV camera needs nothing here; its failure
        resurfaces on the next idle read. Raises when the camera does not come back.
        """
        if self.stream is not None and not self.stream.capture_ready():
            print("camera stopped during the episode; reconnecting it", file=sys.stderr, flush=True)
            ovision.reconnect_stream(self.stream)

    def reconnect_gloves(self):
        """Fresh glove handles after a failed episode; wait for a replug if they are dead.

        The firmware can stop answering commands altogether (seen once right after a
        calibration): every reconnect then times out until the glove is power-cycled.
        Only unplugging it helps -- never a serial-line reset, which puts the board into
        ROM download mode. The camera stays live so the operator sees the message;
        ``q`` gives up. Returns False when the operator quit.
        """
        self.close_gloves()
        attempt = 0
        while True:
            attempt += 1
            result = {}

            def connect(result=result):
                try:
                    self.open_gloves()
                except Exception as exc:
                    result["error"] = exc

            worker = threading.Thread(target=connect, name="reconnect", daemon=True)
            worker.start()
            while True:  # At least one listening frame per attempt, however fast it fails.
                frame = self.read_frame()
                lines = ["GLOVE NOT RESPONDING" if attempt > 1 else "Reconnecting the gloves...",
                         "Unplug the glove, wait 10 s, plug it back in.  q = quit",
                         f"reconnect attempt {attempt}   last: {self.status}"]
                put_lines(frame, lines, color=(0, 80, 255))
                if self.display.show(frame) == KEY_QUIT:
                    worker.join()
                    self.close_gloves()
                    return False
                if not worker.is_alive():
                    break
            worker.join()
            if "error" not in result:
                if attempt > 1:
                    print("Gloves are back.", flush=True)
                return True
            print(f"reconnect attempt {attempt} failed: {result['error']}", file=sys.stderr, flush=True)
            self.close_gloves()
            time.sleep(self.reconnect_pause)

    @staticmethod
    def _read_baseline(glove):
        if not glove.info.zero_valid:
            return None
        try:
            reply = glove.send("GET ZERO", expect="#TZERO ", timeout=4.0)
            recipe = json.loads(reply.removeprefix("#TZERO "))
            return np.asarray(recipe["baseline"], dtype=np.float32)
        except Exception:
            return None

    def read_frame(self):
        """A display-sized copy; idle frames are never recorded."""
        image, _ = capture.read_camera(self.camera)
        return fit_preview(image)

    def poll_gloves(self):
        """The readers run on their own; surface a glove that stopped answering."""
        for glove in self.gloves:
            glove.check()

    # drawing ---------------------------------------------------------------

    def glove_line(self, glove):
        info = glove.info
        rates = glove.rates_seen
        mode = f"CLEAN thr={info.stream_thr}" if info.stream_clean else "RAW"
        zero = "zero:ok" if info.zero_valid else "zero:NONE (press z)"
        return (f"{info.side.upper():5s} {info.serial}  {zero}  {mode}  "
                f"tactile {rates['tactile']:.0f} Hz  imu {rates['imu']:.0f} Hz")

    def toggle_view(self):
        """c: show raw ADC counts or counts above the zero. The recording is untouched:
        a RAW-stream episode always saves both files whatever the screen shows."""
        self.show_raw = not self.show_raw

    def raw_view(self, info):
        """Raw ADC can only be shown while the glove streams raw; CLEAN never sends it."""
        return self.show_raw and not info.stream_clean

    def grid_values(self, glove):
        """Newest tactile frame as (5, 4, 4) physical-layout counts.

        Default view, counts above the zero:
        CLEAN stream: the residual the firmware already subtracted (never below 0).
        RAW stream: max(0, counts minus the saved sweep zero), the same clamp the CLEAN
        file gets. The zero is the sweep envelope (each taxel's highest value while the
        hand opened and closed), so a resting hand sits below it; the negative part is
        not contact and is not shown.
        Raw view (c, RAW stream only): the ADC counts exactly as sent, ~550 untouched.
        """
        frame = glove.latest
        if frame is None:
            return None
        info = glove.info
        if info.stream_clean:
            values = frame.residual.astype(np.float32)
        elif self.raw_view(info):
            values = frame.counts.astype(np.float32)
        else:
            counts = frame.counts.astype(np.float32)
            baseline = self.baselines.get(info.serial)
            baseline = counts.min() if baseline is None else baseline.reshape(counts.shape)
            values = np.maximum(counts - baseline, 0.0)
        return oglo.oriented_counts(values, info.channels, info.side)

    def grid_thr(self, glove):
        """Cells under this show 0, like OGLO Studio: the stream deadband, or none on RAW."""
        info = glove.info
        return info.stream_thr if info.stream_clean else 0

    def grid_label(self, glove):
        info = glove.info
        if self.raw_view(info):
            return f"{info.side.upper()} RAW ADC"
        if self.show_raw:
            return f"{info.side.upper()} cal (CLEAN stream has no raw)"
        return f"{info.side.upper()} cal"

    def glove_grids(self):
        return [(self.grid_label(g), self.grid_values(g), self.grid_thr(g),
                 RAW_FULL if self.raw_view(g.info) else HEAT_SCALE) for g in self.gloves]

    def draw_idle(self, image, next_session):
        frame = image.copy()  # Already preview-sized by read_frame().
        lines = ["IDLE   g = record   z = calibrate   c = raw/cal view   q = quit"]
        lines += [self.glove_line(g) for g in self.gloves]
        pending = self._jobs.unfinished_tasks
        lines += [f"task: {self.args.task}   next: {next_session.name}"
                  + (f"   aligning: {pending}" if pending else ""),
                  f"last: {self.status}"]
        put_lines(frame, lines)
        return draw_gloves(frame, self.glove_grids())

    def show_for(self, seconds, lines, color=(0, 220, 255)):
        """Keep the camera live while a message is up. Keys are ignored."""
        end = time.monotonic() + seconds
        while True:
            frame = self.read_frame()
            remaining = max(0.0, end - time.monotonic())
            put_lines(frame, [line.format(remaining=remaining) for line in lines], color=color)
            self.display.show(frame, listen=False)
            if remaining <= 0:
                return

    # phases ----------------------------------------------------------------

    def calibrate(self):
        try:
            self._calibrate()
        finally:
            self.start_readers()

    def _calibrate(self):
        self.stop_streams()  # No stream to lose while one hand sweeps.
        if self.args.countdown > 0:
            self.show_for(self.args.countdown, [
                "CALIBRATION in {remaining:.0f} s",
                "Wear the gloves. Touch nothing. Keep opening and closing your hands."])
        for glove in self.gloves:
            side = glove.info.side.upper()
            result = {}

            def sweep(glove=glove, result=result):
                try:
                    result["recipe"] = glove.zero(sweep=self.args.sweep)
                except Exception as exc:  # Reported on screen; the loop goes on.
                    result["error"] = exc

            worker = threading.Thread(target=sweep, name=f"zero-{side}", daemon=True)
            worker.start()
            while worker.is_alive():
                frame = self.read_frame()
                put_lines(frame, [f"{side}: open and close the hand for {self.args.sweep} s",
                                  "touch nothing"], color=(0, 220, 255))
                self.display.show(frame, listen=False)
            worker.join()
            if "error" in result:
                self.status = f"{side} zero failed: {result['error']}"
                print(self.status, file=sys.stderr, flush=True)
                return
            recipe = result["recipe"]
            self.last_calibration[glove.info.serial] = recipe
            self.baselines[glove.info.serial] = np.asarray(recipe["baseline"], dtype=np.float32)
            try:
                if self.args.clean is None:
                    glove.raw()
                else:
                    glove.clean(threshold=self.args.clean)
            except Exception as exc:
                self.status = f"{side} mode change failed: {exc}"
                print(self.status, file=sys.stderr, flush=True)
                return
            print(f"{side} calibrated: 80 taxels, mode "
                  f"{'CLEAN' if glove.info.stream_clean else 'RAW'}", flush=True)
        self.status = "calibrated " + " + ".join(g.info.side for g in self.gloves)

    def zero_ready(self):
        """g is refused until every glove holds a valid sweep zero.

        Without one the episode would save raw ADC only: no CLEAN file can be derived
        and the backend cannot use it. Calibration lives on the glove, so a zero from
        an earlier session (or from OGLO Studio) counts; z replaces it.
        """
        missing = [g.info.serial for g in self.gloves if not g.info.zero_valid]
        if not missing:
            return True
        self.status = f"refused: no zero on {', '.join(missing)} -- press z first"
        print(self.status, flush=True)
        self.show_for(1.5, ["NOT RECORDING: no sweep zero on " + ", ".join(missing),
                            "press z to calibrate first"], color=(0, 0, 255))
        return False

    def record_episode(self):
        """One episode; returns False only when the operator quit while the gloves were dead."""
        session = next_session_dir(self.args.out, self.args.task)
        self.args.out.mkdir(parents=True, exist_ok=True)
        namespace = argparse.Namespace(
            output=session, camera=self.args.camera, seconds=self.args.seconds,
            fps=self.args.fps, task=self.args.task, serial=self.args.serial,
            pair=self.args.pair, preview=False, codec=self.args.codec,
            video_quality=self.args.video_quality,
            video_device=self.video_device, camera_serial=None,
        )
        control = RecordingControl(on_view=self.toggle_view)
        if self.stream is not None:
            overlay = RecordingOverlay(self.display, control, session.name, self.args.task,
                                       self.args.seconds, self.glove_grids)

            def camera_factory(args, output):
                return ovision.OvisionCapture(args, output, stream=self.stream, tick=overlay.show)
        else:
            def camera_factory(args, output):
                return OverlayCamera(args, output, self.display, control, session.name,
                                     camera=self.camera, gloves=self.glove_grids)
        print(f"Recording {session.name}: h = save, x = discard, c = view, cap {self.args.seconds:g} s",
              flush=True)
        failure = None
        try:
            # Stop every stream before any command goes out, as calibrate() does. The
            # SDK pauses only the glove it is talking to; a second glove left
            # streaming into a port nobody reads is the wedge TactilePeek describes.
            self.stop_streams()
            # The idle camera and gloves stay open: reopening them costs ~3 s per episode.
            capture.capture(namespace, stop=control.stop, gloves=self.gloves,
                            camera_factory=camera_factory)
        except KeyboardInterrupt:  # Ends the session; the partial episode is kept aside.
            self.fail(session, "interrupted with Ctrl-C")
            raise
        except Exception as exc:
            failure = exc
        if failure is not None:  # Before the discard: an x followed by a crash is still a failure.
            self.fail(session, failure)
        elif control.outcome == "discard":
            self.discard(session)
        else:
            self.status = f"{session.name} aligning..."
            self._jobs.put(session)
        if failure is not None:  # The camera or a glove may be the cause; start over with fresh handles.
            self.recover_camera()
            return self.reconnect_gloves()
        self.start_readers()
        return True

    @staticmethod
    def set_aside(session, subfolder):
        """Move ``<task>/<name>`` to ``<task>/<subfolder>/<name>`` if it was written at all."""
        target = session.parent / subfolder / session.name
        if session.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(session), str(target))
        return target

    def discard(self, session):
        target = self.set_aside(session, DISCARDED)
        self.status = f"{session.name} discarded"
        self.episodes.append({"session": target, "complete": False, "outcome": "discarded"})
        print(f"{session.name} discarded -> {target}", flush=True)

    def fail(self, session, error, stage="capture"):
        """A capture or alignment error: keep the files for diagnosis, out of the dataset."""
        target = self.set_aside(session, FAILED)
        self.status = f"{session.name} {stage} FAILED: {error}"
        self.episodes.append({"session": target, "complete": False, "outcome": "failed"})
        print(f"{self.status} -> {target}", file=sys.stderr, flush=True)

    def postprocess(self, session):
        manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
        preview = session / dataset.ALIGNMENT
        frames = align.align(session, preview, self.args.max_delta_ms)
        reason = dataset.publishable(session, manifest)  # The same gate the index and upload apply.
        if reason is not None:
            raise RuntimeError(f"not publishable after alignment: {reason}")
        rates, _ = alignment_hit_rates(preview)
        overlap = manifest["overlap_host_received_ns"]
        parts = [f"{session.name}: {frames} frames, overlap {(overlap[1] - overlap[0]) / 1e9:.1f} s, "
                 f"stop {manifest.get('stop_reason')}"]
        for entry in manifest["gloves"]:
            summary = entry.get("summary", {})
            hit = rates.get(entry["side"], {})
            parts.append(f"{entry['side']} tactile {summary.get('tactile', {}).get('n', '?')} rows "
                         f"(hit {hit.get('tactile', 0):.0%}), imu "
                         f"{summary.get('imu', {}).get('n', '?')} rows (hit {hit.get('imu', 0):.0%})")
        self.episodes.append({"session": session, "complete": True, "outcome": "saved"})
        self.status = f"{session.name} ok"
        print("; ".join(parts), flush=True)

    # background alignment ----------------------------------------------------

    def _drain_jobs(self):
        while True:
            session = self._jobs.get()
            if session is None:  # run() is over: leave after the queue is drained.
                self._jobs.task_done()
                return
            try:
                self.postprocess(session)
            except Exception as exc:
                try:
                    self.fail(session, exc, stage="align")
                except Exception as move_exc:  # The worker must live on, or quit hangs in join().
                    print(f"{session.name} align FAILED: {exc}; could not move it aside: {move_exc}",
                          file=sys.stderr, flush=True)
            finally:
                self._jobs.task_done()

    # main loop -------------------------------------------------------------

    def run(self):
        check_task_folder(self.args.out, self.args.task)
        self._worker = threading.Thread(target=self._drain_jobs, name="postprocess", daemon=True)
        self._worker.start()
        try:
            self.open_devices()  # Inside the try: a camera failure must still close open gloves.
            # Only an episode can take the next number; between them the folder scan
            # (three stat() calls per existing episode) would otherwise run every frame.
            next_session = next_session_dir(self.args.out, self.args.task)
            while True:
                image = self.read_frame()
                self.poll_gloves()
                key = self.display.show(self.draw_idle(image, next_session))
                if key == KEY_QUIT:
                    break
                if key == KEY_VIEW:
                    self.toggle_view()
                    continue
                if key == KEY_ZERO:
                    self.calibrate()
                elif key == KEY_START:
                    if not self.zero_ready():
                        continue
                    if not self.record_episode():
                        break  # q while the gloves were unreachable.
                    next_session = next_session_dir(self.args.out, self.args.task)
                else:
                    continue
                self.display.flush()  # A held pedal auto-repeats; never chain two phases.
        finally:
            self.close_devices()
            pending = self._jobs.unfinished_tasks
            if pending:
                print(f"Waiting for {pending} session(s) to finish aligning...", flush=True)
            self._jobs.put(None)  # Explicit shutdown: the worker exits once everything queued is done.
            self._jobs.join()
            self._worker.join(timeout=5)
            self.display.close()
            self.write_index()
        return self.episodes

    def write_index(self):
        """Refresh the root episodes.jsonl and README.md; never fatal after a session."""
        if not self.args.out.is_dir():
            return
        try:
            rows = dataset.write_index(self.args.out)
        except Exception as exc:
            print(f"index not updated: {exc}", file=sys.stderr, flush=True)
            return
        print(f"{len(rows)} episodes indexed in {self.args.out / dataset.INDEX}", flush=True)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="parent folder for numbered sessions")
    parser.add_argument("--task", required=True, help="description of the activity")
    parser.add_argument("--camera", default="0",
                        help="OpenCV device index, or part of the camera's V4L2 name such as "
                             "SC233 (stable across reboots; the index is not). Default: 0")
    parser.add_argument("--camera-backend", choices=BACKENDS, default="auto",
                        help="ovision = the native OVISION-EGO-V1 backend (ovision.py): original "
                             "H.264, camera IMU, exposure timing and calibration per episode; "
                             "opencv = any webcam through OpenCV, no camera IMU. auto (default) "
                             "takes ovision when SyncField 0.8.14 is installed and the camera "
                             "answers as an OVISION, and says so when it falls back")
    parser.add_argument("--seconds", type=capture.positive_number, default=600,
                        help="maximum episode length; h or x stop earlier (default: 600)")
    parser.add_argument("--fps", type=capture.positive_number, default=30,
                        help="requested camera FPS and MP4 playback FPS (default: 30)")
    parser.add_argument("--codec", choices=capture.CODECS, default="mp4v",
                        help="video encoder for the OpenCV backend: mp4v = OpenCV's writer, "
                             "hevc_nvenc = H.265 on an NVIDIA GPU via ffmpeg, libx265 = H.265 on the "
                             "CPU (default: mp4v; scripts/workstation.env sets the workstation's choice)")
    parser.add_argument("--video-quality", type=int, default=23, metavar="N",
                        help="CRF / CQ for the ffmpeg codecs, lower = larger file; 23 keeps sensor "
                             "noise, 28 is about half the size (default: 23)")
    gloves = parser.add_mutually_exclusive_group()
    gloves.add_argument("--serial", help="one glove's logical CONFIG serial")
    gloves.add_argument("--pair", action="store_true", help="record verified left and right gloves")
    parser.add_argument("--sweep", type=int, default=5, help="zero sweep seconds, 1..30 (default: 5)")
    parser.add_argument("--clean", type=int, default=None, metavar="THR",
                        help="after calibrating switch the device to CLEAN with this threshold "
                             "(default: stay RAW; the recorder derives CLEAN files itself)")
    parser.add_argument("--countdown", type=float, default=3, help="seconds before a sweep starts")
    parser.add_argument("--max-delta-ms", type=float, default=50,
                        help="alignment preview tolerance, not a sync guarantee (default: 50)")
    parser.add_argument("--skip-doctor", action="store_true", help="do not run oglo doctor first")
    parser.add_argument("--allow-shared-usb", action="store_true",
                        help="record even when a glove shares a USB controller with the camera "
                             "(the glove firmware then locks up now and then; replug to recover)")
    parser.add_argument("--preview-width", type=int, default=PREVIEW_WIDTH, metavar="PX",
                        help="on-screen window width in pixels; the recording keeps the camera's full size")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.camera, card = resolve_camera(args.camera)
    if card is not None:
        print(f"camera: /dev/video{args.camera} ({card})", flush=True)
    if not 1 <= args.sweep <= 30:
        raise SystemExit("--sweep must be 1..30 seconds")
    if args.preview_width < 160:
        raise SystemExit("--preview-width must be at least 160")
    global PREVIEW_WIDTH
    PREVIEW_WIDTH = args.preview_width
    if not 0 <= args.video_quality <= 51:
        raise SystemExit("--video-quality must be 0..51")
    try:
        args.camera_backend, reason = choose_backend(args.camera_backend, args.camera)
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr, flush=True)
        return 2
    if args.camera_backend == "ovision":
        print("camera backend: native OVISION (H.264 passthrough at 3840x1080, 30 fps, camera IMU); "
              "--codec, --video-quality and --fps do not apply", flush=True)
    else:
        if reason:
            print(f"camera backend: OpenCV, camera IMU is not recorded: {reason}", flush=True)
        problem = capture.probe_encoder(args.codec, args.video_quality)
        if problem:
            print(f"--codec {args.codec} does not work here: {problem}\n"
                  "Try --codec libx265 (CPU) or --codec mp4v.", file=sys.stderr, flush=True)
            return 2
    if not args.skip_doctor:
        report = doctor(seconds=3.0)
        print(report, flush=True)
        if report.worst == FAIL:
            print("Fix the FAIL lines above, then run again.", flush=True)
            return 2
    print("Click the window once so it has keyboard focus; the foot switch types into it.",
          flush=True)
    collector = Collector(args)
    try:
        episodes = collector.run()
    except KeyboardInterrupt:
        print(f"Stopped. An interrupted episode is under {FAILED}/ with complete=false.", flush=True)
        return 130
    except (oglo.UsbError, oglo.DeviceError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    counts = {name: sum(1 for episode in episodes if episode["outcome"] == name)
              for name in ("saved", "discarded", "failed")}
    print(f"{counts['saved']} saved, {counts['discarded']} discarded, {counts['failed']} failed "
          f"episode(s) under {args.out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
