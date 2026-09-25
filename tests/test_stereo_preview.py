"""stereo_preview.py on real H.264: every frame, both eyes, in a separate decoder process."""

import importlib.util
from pathlib import Path
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
    """The adapter's surface the preview touches: the per-packet hook it replaces."""

    def __init__(self):
        self.class_hook_calls = 0

    def _queue_preview(self, encoded, is_keyframe):
        self.class_hook_calls += 1


def collect_frames(preview):
    frames = []

    def watch():
        while preview.running:
            if preview.wait(0.1):
                frames.append(preview.latest_frame)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return frames, thread


def settle(frames, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(frames) < expected and time.monotonic() < deadline:
        time.sleep(0.02)


def feed(stream, items):
    for data, keyframe in items:
        stream._queue_preview(data, keyframe)
        time.sleep(0.005)  # About camera pace, so the newest-frame copy sees each frame.


def test_every_frame_arrives_with_both_eyes_at_the_preview_size():
    assert stereo_preview.preview_size((3840, 1080), 1920) == (1920, 540)
    assert stereo_preview.preview_size((3840, 1080), 5000) == (3840, 1080)  # Never enlarged.
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SOURCE, 192)
    try:
        assert preview.size == (192, 54) and preview.latest_frame is None
        frames, _ = collect_frames(preview)
        video = packets(30)
        feed(stream, video)
        settle(frames, len(video))
        assert len(frames) == len(video) and stream.class_hook_calls == 0
        frame = frames[-1]
        assert frame.shape == (54, 192, 3)
        assert np.abs(frame[27, 20].astype(int) - LEFT).max() < 12
        assert np.abs(frame[27, 170].astype(int) - RIGHT).max() < 12
        assert len({id(f) for f in frames}) == len(frames)  # A fresh array each time.
    finally:
        preview.close()
    assert "_queue_preview" not in stream.__dict__  # The adapter's own hook is back.
    assert preview._process.returncode == 0 and not preview.running


def test_decoding_starts_at_a_keyframe():
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SOURCE, 192)
    try:
        frames, _ = collect_frames(preview)
        video = packets(25, gop=10)  # Keyframes at 0, 10, 20.
        feed(stream, video[3:])  # Joined mid-GOP: frames 3..9 cannot be decoded and are not sent.
        settle(frames, 15)
        time.sleep(0.2)
        assert len(frames) == 15 and preview.running
    finally:
        preview.close()


def test_a_dead_decoder_hands_the_window_back_to_the_adapter():
    stream = Stream()
    preview = stereo_preview.StereoPreview(stream, SOURCE, 192)
    try:
        frames, _ = collect_frames(preview)
        feed(stream, packets(5))
        settle(frames, 5)
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
