"""stereo_preview.py on real H.264: every frame, both eyes, in a separate decoder process."""

import importlib.util
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

import numpy as np
import pytest

av = pytest.importorskip("av")
if "libx264" not in av.codecs_available:
    pytest.skip("needs an H.264 encoder to make test video", allow_module_level=True)
if not sys.platform.startswith("linux"):
    pytest.skip("the preview shares memory through /dev/shm", allow_module_level=True)

HERE = Path(__file__).resolve().parents[1] / "examples" / "camera_glove"
spec = importlib.util.spec_from_file_location("camera_glove_stereo_preview", HERE / "stereo_preview.py")
stereo_preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stereo_preview)

SOURCE = (384, 108)  # the camera's 3840x1080 at a tenth: left eye | right eye
LEFT, RIGHT = (0, 0, 200), (200, 0, 0)


def packets(count, gop=10):
    """Annex B H.264 packets as the camera sends them: (bytes, is_keyframe)."""
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width, encoder.height = SOURCE
    encoder.pix_fmt, encoder.framerate, encoder.gop_size = "yuv420p", 30, gop
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
    out = []
    for i in range(count):
        image = np.zeros((SOURCE[1], SOURCE[0], 3), np.uint8)
        image[:, : SOURCE[0] // 2] = LEFT
        image[:, SOURCE[0] // 2:] = RIGHT
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = i
        out += [(bytes(p), p.is_keyframe) for p in encoder.encode(frame)]
    out += [(bytes(p), p.is_keyframe) for p in encoder.encode(None)]
    return out


class Stream:
    """The adapter's surface the preview touches: the per-packet hook it replaces, and
    the left-eye keyframe the adapter's own preview last decoded."""

    def __init__(self):
        self.class_hook_calls = 0
        self.latest_frame = None

    def _queue_preview(self, encoded, is_keyframe):
        self.class_hook_calls += 1


SIZE = stereo_preview.preview_size(SOURCE, 192)  # (192, 54)


def collect_frames(preview):
    frames = []

    def watch():
        while preview.running:
            if preview.wait(0.1):
                frames.append(preview.latest_frame)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return frames, thread


def settle(preview, expected, timeout=10.0):
    """Until the decoder returned ``expected`` frames. The window only ever copies the
    newest one, so a busy machine shows fewer; the count is the decoder's."""
    deadline = time.monotonic() + timeout
    while preview.decoded < expected and time.monotonic() < deadline:
        time.sleep(0.02)


def feed(stream, items):
    for data, keyframe in items:
        stream._queue_preview(data, keyframe)
        time.sleep(0.005)  # About camera pace, so the packet queue never fills.


def record_queued(preview, full_at=()):
    """The packets ``_offer`` hands to the decoder queue, in order; the ``full_at``-th
    attempts (counting from 0) find it full."""
    queued, attempts = [], [0]
    put = preview._packets.put_nowait

    def put_nowait(item):
        attempts[0] += 1
        if attempts[0] - 1 in full_at:
            raise queue.Full
        queued.append(item)
        put(item)

    preview._packets.put_nowait = put_nowait
    return queued


def test_preview_size_fits_a_width_and_a_height_and_never_enlarges():
    assert stereo_preview.preview_size((3840, 1080), 1920) == (1920, 540)
    assert stereo_preview.preview_size((3840, 1080), 1920, 720) == (1920, 540)
    assert stereo_preview.preview_size((1920, 1080), 1920, 720) == (1280, 720)
    assert stereo_preview.preview_size((3840, 1080), 5000) == (3840, 1080)
    assert stereo_preview.preview_size((640, 480), 1920, 720) == (640, 480)


def test_every_frame_arrives_with_both_eyes_at_the_preview_size():
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SIZE)
    try:
        assert preview.size == (192, 54) and preview.latest_frame is None
        frames, _ = collect_frames(preview)
        video = packets(30)
        feed(stream, video)
        settle(preview, len(video))
        assert preview.decoded == len(video) and stream.class_hook_calls == 0
        deadline = time.monotonic() + 5
        while not frames and time.monotonic() < deadline:
            time.sleep(0.02)
        frame = preview.latest_frame
        assert frame.shape == (54, 192, 3)
        assert np.abs(frame[27, 20].astype(int) - LEFT).max() < 12
        assert np.abs(frame[27, 170].astype(int) - RIGHT).max() < 12
        assert frames and len({id(f) for f in frames}) == len(frames)  # A fresh array each time.
    finally:
        preview.close()
    assert "_queue_preview" not in stream.__dict__  # The adapter's own hook is back.
    assert preview._process.returncode == 0 and not preview.running


def test_decoding_starts_at_a_keyframe():
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SIZE)
    queued = record_queued(preview)
    try:
        video = packets(25, gop=10)  # Keyframes at 0, 10, 20.
        feed(stream, video[3:])  # Joined mid-GOP: frames 3..9 cannot be decoded and are not sent.
        assert queued == [data for data, _ in video[10:]]
        settle(preview, 15)
        time.sleep(0.2)
        assert preview.decoded == 15 and preview.running
    finally:
        preview.close()


def test_after_a_full_queue_decoding_resumes_at_the_next_keyframe():
    """A packet dropped for a full queue breaks the frames after it until the next
    keyframe (a decoder that has its SPS and PPS does not complain, it shows garbage):
    none of them is queued."""
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SIZE)
    queued = record_queued(preview, full_at=(12,))  # Packet 12 finds the queue full.
    try:
        video = packets(30, gop=10)  # Keyframes at 0, 10, 20.
        feed(stream, video)
        assert queued == [data for data, _ in video[:12] + video[20:]]
        settle(preview, 22)
        time.sleep(0.2)
        assert preview.decoded == 22 and preview.running
        frame = preview.latest_frame
        assert np.abs(frame[27, 20].astype(int) - LEFT).max() < 12
        assert np.abs(frame[27, 170].astype(int) - RIGHT).max() < 12
    finally:
        preview.close()


def test_a_decoder_that_takes_video_and_returns_nothing_is_given_up(monkeypatch):
    """Stopped, stuck in libavcodec or starved: alive, so no pipe breaks. The preview
    fails and hands the window back, and the camera is never blamed for it."""
    monkeypatch.setattr(stereo_preview, "DECODER_STALL_SECONDS", 0.5)
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SIZE)
    try:
        video = packets(60, gop=10)
        feed(stream, video[:10])
        settle(preview, 10)
        os.kill(preview._process.pid, signal.SIGSTOP)
        deadline = time.monotonic() + 5
        index = 10
        while preview.running and time.monotonic() < deadline:
            stream._queue_preview(*video[index % len(video)])
            index += 1
            preview.wait(0.03)
        assert not preview.running and "no frame" in preview.error
        assert "_queue_preview" not in stream.__dict__
    finally:
        preview.close()
    assert preview._process.returncode is not None  # Killed, not left stopped.


def test_a_camera_that_goes_quiet_does_not_fail_the_preview(monkeypatch):
    """No video in, no frame out: that is the camera's silence, for the collector's own
    stall check to judge, not a decoder failure."""
    monkeypatch.setattr(stereo_preview, "DECODER_STALL_SECONDS", 0.3)
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SIZE)
    try:
        feed(stream, packets(10))
        settle(preview, 10)
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            preview.wait(0.05)
        assert preview.running
    finally:
        preview.close()


def test_a_dead_decoder_hands_the_window_back_to_the_adapter():
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SIZE)
    try:
        frames, _ = collect_frames(preview)
        feed(stream, packets(5))
        settle(preview, 5)
        preview._process.kill()
        deadline = time.monotonic() + 5
        while preview.running and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not preview.running and "exited" in preview.error
        assert "_queue_preview" not in stream.__dict__
        stream._queue_preview(b"", True)  # Packets go to the adapter's own preview again.
        assert stream.class_hook_calls == 1
        assert preview.latest_frame is not None  # The last frame stays until the caller switches.
    finally:
        preview.close()
