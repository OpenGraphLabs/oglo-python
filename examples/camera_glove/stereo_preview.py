#!/usr/bin/env python3
"""Both eyes of the OVISION camera at its full frame rate, for the collect.py window.

SyncField's adapter decodes one keyframe at most twice a second and keeps the left eye
only (about 1 fps on this firmware), so that decoding never competes with capture. This
module shows what the recording holds instead, every frame, both eyes, without touching
the recording: the adapter's capture thread hands each H.264 packet it already read to
``StereoPreview`` (the hook the adapter's own preview uses), a separate niced process
decodes and scales it, and the frames come back through shared memory. Decoding stays
out of the collector's interpreter, where it would hold the GIL against the glove
readers (align.py is a separate process for the same reason). The recorded packets,
the MP4 and every sidecar are exactly what they were.

When the decoder cannot keep up, packets are dropped up to the next keyframe (an
H.264 frame needs the ones before it); when it dies, the adapter's own left-eye
keyframe preview takes over again and recording carries on.

Run as a script it is that decoder: length-prefixed Annex B packets on stdin, the
index of each finished frame's shared-memory slot on stdout.
"""

import argparse
import mmap
import os
import queue
import signal
import struct
import subprocess
import sys
import tempfile
import threading

import numpy as np

HEADER = struct.Struct("<I")  # packet length on stdin, slot index on stdout
SLOTS = 4  # frames in shared memory; the collector copies the newest before the decoder laps it
QUEUED_PACKETS = 30  # about a second of video waiting for the decoder before packets are dropped
NICE = 10  # below the collector and the camera, like align.py
SHM = "/dev/shm"


def preview_size(source, width):
    """``source`` (w, h) shrunk to ``width`` (never enlarged), aspect kept."""
    source_w, source_h = source
    width = min(width, source_w)
    return width, max(1, round(source_h * width / source_w))


class StereoPreview:
    """Full-rate stereo preview of one ``OvisionCameraStream`` (SyncField 0.8.14).

    ``latest_frame`` is the newest decoded frame, both eyes side by side at ``size``, a
    fresh array each time (None until the first keyframe); ``wait(timeout)`` returns once
    a new one is there. ``running`` turns False for good when the decoder failed
    (``error`` says why); the stream's own preview is back in place by then.
    """

    def __init__(self, stream, source_size, width):
        self.size = preview_size(source_size, width)
        w, h = self.size
        self.error = None
        self._stream = stream
        self._frame_bytes = w * h * 3
        self._latest = None
        self._fresh = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._packets = queue.Queue(maxsize=QUEUED_PACKETS)
        self._need_keyframe = True  # H.264 decoding starts at a keyframe, and resumes at one after a drop.
        # An unnamed file in RAM, shared by descriptor: nothing to clean up after a crash.
        with tempfile.TemporaryFile(dir=SHM if os.path.isdir(SHM) else None) as memory:
            fd = memory.fileno()
            os.ftruncate(fd, self._frame_bytes * SLOTS)
            self._memory = mmap.mmap(fd, self._frame_bytes * SLOTS)
            self._slots = [np.ndarray((h, w, 3), np.uint8, self._memory, i * self._frame_bytes)
                           for i in range(SLOTS)]
            self._process = subprocess.Popen(
                [sys.executable, __file__, "--fd", str(fd), "--width", str(w), "--height", str(h)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, pass_fds=(fd,))
        self._feeder = threading.Thread(target=self._feed, name="stereo-preview-feed", daemon=True)
        self._reader = threading.Thread(target=self._read, name="stereo-preview-read", daemon=True)
        self._feeder.start()
        self._reader.start()
        # The adapter calls this for every packet on its capture thread, recording or not;
        # its class method would pass keyframes on to its own left-eye decoder.
        stream._queue_preview = self._offer

    @property
    def running(self):
        return self.error is None and not self._closed

    @property
    def latest_frame(self):
        with self._lock:
            return self._latest

    def wait(self, timeout):
        """Block until a frame newer than the last ``wait`` arrived, or ``timeout`` passed."""
        fresh = self._fresh.wait(timeout)
        self._fresh.clear()
        return fresh

    def _offer(self, encoded, is_keyframe):
        """Capture thread: queue the packet, never block, never raise."""
        if not self.running:
            return
        if self._need_keyframe and not is_keyframe:
            return
        try:
            self._packets.put_nowait(encoded)
            self._need_keyframe = False
        except queue.Full:
            self._need_keyframe = True

    def _feed(self):
        """The only writer of the decoder's stdin, and the one that closes it."""
        pipe = self._process.stdin.fileno()
        try:
            while True:
                packet = self._packets.get()
                if packet is None:
                    return
                for chunk in (HEADER.pack(len(packet)), packet):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(pipe, view):]
        except OSError as exc:  # BrokenPipeError: the decoder is gone.
            self._fail(f"preview decoder stopped taking video: {exc}")
        finally:
            try:
                self._process.stdin.close()  # EOF: the decoder exits.
            except OSError:
                pass

    def _read(self):
        pipe = self._process.stdout.fileno()
        pending = b""
        while True:
            try:
                data = os.read(pipe, 4096)
            except OSError as exc:
                data, error = b"", exc
            else:
                error = None
            if not data:
                if not self._closed:
                    status = self._process.wait()
                    self._fail(f"preview decoder exited ({error or f'status {status}'})")
                return
            pending += data
            usable = len(pending) - len(pending) % HEADER.size
            if not usable:
                continue
            # Only the newest finished frame is copied; the decoder writes the next slot meanwhile.
            (slot,) = HEADER.unpack_from(pending, usable - HEADER.size)
            pending = pending[usable:]
            frame = self._slots[slot].copy()
            with self._lock:
                self._latest = frame
            self._fresh.set()

    def _fail(self, reason):
        if self.error is not None or self._closed:
            return
        self.error = reason
        self._stream.__dict__.pop("_queue_preview", None)  # The adapter's left-eye preview again.
        print(f"{reason}; the window shows the left eye at keyframes only from now on",
              file=sys.stderr, flush=True)

    def close(self):
        if self._closed:
            return
        self._closed = True  # _offer queues nothing more.
        self._stream.__dict__.pop("_queue_preview", None)
        while True:  # Whatever still waits is not worth decoding; the end marker must fit.
            try:
                self._packets.get_nowait()
            except queue.Empty:
                break
        self._packets.put_nowait(None)
        self._feeder.join(timeout=2)
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()  # A decoder that hung; its pipe breaks and the feeder ends too.
            self._process.wait()
        self._feeder.join(timeout=1)
        self._reader.join(timeout=1)


# -- the decoder process ----------------------------------------------------------------

def read_exact(source, size):
    data = source.read(size)
    return data if len(data) == size else None


def decode(args):
    import av

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C is the collector's; stdin EOF ends this.
    os.nice(NICE)
    frame_bytes = args.width * args.height * 3
    memory = mmap.mmap(args.fd, frame_bytes * SLOTS)
    slots = [np.ndarray((args.height, args.width, 3), np.uint8, memory, i * frame_bytes)
             for i in range(SLOTS)]
    decoder = av.CodecContext.create("h264", "r")
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    slot = 0
    while True:
        header = read_exact(source, HEADER.size)
        if header is None:
            return 0
        packet = read_exact(source, HEADER.unpack(header)[0])
        if packet is None:
            return 0
        try:
            frames = decoder.decode(av.Packet(packet))
        except av.error.FFmpegError:
            continue  # A frame whose references were dropped; the next keyframe recovers.
        for frame in frames:
            image = frame.reformat(width=args.width, height=args.height, format="bgr24",
                                   interpolation="AREA").to_ndarray()
            slots[slot][...] = image
            sink.write(HEADER.pack(slot))
            sink.flush()
            slot = (slot + 1) % SLOTS


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fd", type=int, required=True, help="shared-memory file descriptor")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    try:
        return decode(parser.parse_args())
    except BrokenPipeError:  # The collector is gone.
        return 0


if __name__ == "__main__":
    sys.exit(main())
