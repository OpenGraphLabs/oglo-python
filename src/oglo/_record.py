"""Writing an episode to disk.

Three streams at three rates, stored as three arrays. **Nothing is resampled onto a
common clock.** Forcing one rate either fabricates samples for the slow stream or
throws them away from the fast one, and both are lies a dataset carries forever. Each
stream keeps its own sequence, its own device timestamp and its own host timestamp,
and a consumer that wants them aligned does the aligning with the numbers in front of
it.

Layout:

    ep_0001/
      meta.json     device identity, calibration in force, rates, wall-clock anchor
      tactile.npz   seq, t_us, host_t, counts (N,5,4,4) uint16, dropped
      imu.npz       seq/times, accel, gyro, raw/raw_valid, dropped
      mag.npz       seq/times, field, raw/raw_valid, dropped

`.npz` because numpy is already a dependency, the files stay small, and anyone can
open one without this SDK. A text format at 250 Hz x 80 taxels would be neither.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
import zipfile
from copy import deepcopy
from dataclasses import asdict
from numbers import Real
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from uuid import uuid4

import numpy as np

from ._frame import Frame, ImuSample, MagSample

SCHEMA = 2
# A recording must not wait forever on a transport that remains open but emits
# no usable sensor frames. This is deliberately far above normal USB jitter and
# the fitted stream periods, while short enough to stop a bad episode before a
# field operator assumes it is still collecting data.
RECORDING_STREAM_SILENCE_S = 3.0


class RecordError(RuntimeError):
    pass


def next_episode_dir(root: Path) -> Path:
    """Atomically reserve ``ep_0001``, ``ep_0002``, ... without overwriting."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        candidate = root / f"ep_{n:04d}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            n += 1


class _Buffer:
    """Fixed-size live columns backed by immutable episode-local chunks.

    A recorder used to double these arrays whenever they filled. That is efficient
    for short captures but still consumes all available RAM if a capture is left
    running. Here ``cap`` is a hard ceiling: a full block is sealed as one
    uncompressed ``.npz`` below the episode's hidden working directory, then the
    arrays are reused. Final NPZ creation streams those chunks and the last live block; it never
    joins the complete episode in memory.
    """

    __slots__ = (
        "n", "cap", "seq", "t_us", "device_time_us", "host_t", "host_t_ns",
        "host_received_ns", "dropped", "a", "b", "raw", "raw_valid",
        "_ashape", "_bshape", "_rawshape", "_used", "_chunk_count", "_work_dir",
        "_sealed",
    )

    def __init__(self, work_dir: Path, ashape=(), bshape=None, rawshape=None,
                 cap: int = 4096) -> None:
        if cap <= 0:
            raise ValueError("recording chunk size must be positive")
        self.n = 0
        self.cap = cap
        self._ashape = ashape
        self._bshape = bshape
        self._rawshape = rawshape
        self._used = 0
        self._chunk_count = 0
        self._work_dir = Path(work_dir)
        self._sealed = False
        self.seq = np.empty(cap, dtype=np.uint32)
        self.t_us = np.empty(cap, dtype=np.uint32)
        self.device_time_us = np.empty(cap, dtype=np.uint64)
        self.host_t = np.empty(cap, dtype=np.float64)
        self.host_t_ns = np.empty(cap, dtype=np.uint64)
        self.host_received_ns = np.empty(cap, dtype=np.uint64)
        self.dropped = np.empty(cap, dtype=np.uint32)
        self.a = np.empty((cap,) + ashape, dtype=np.uint16 if ashape == (5, 4, 4) else np.float32)
        self.b = np.empty((cap,) + bshape, dtype=np.float32) if bshape else None
        self.raw = np.empty((cap,) + rawshape, dtype=np.int16) if rawshape else None
        self.raw_valid = np.empty(cap, dtype=np.bool_) if rawshape else None

    @property
    def live_samples(self) -> int:
        """Rows currently resident in RAM (always at most ``cap``)."""
        return self._used

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    def _columns(self) -> Iterator[Tuple[str, np.ndarray]]:
        for name in (
            "seq", "t_us", "device_time_us", "host_t", "host_t_ns",
            "host_received_ns", "dropped", "a", "b", "raw", "raw_valid",
        ):
            arr = getattr(self, name)
            if arr is not None:
                yield name, arr

    def _flush(self) -> None:
        if not self._used:
            return
        self._work_dir.mkdir(parents=True, exist_ok=True)
        final = self._work_dir / f"chunk_{self._chunk_count:08d}.npz"
        tmp = self._work_dir / f".{final.name}.{uuid4().hex}.tmp"
        try:
            # One uncompressed container and one fsync, rather than 8-10 separate
            # fsyncs on the sole reader thread. The latter can pause an SD card long
            # enough to overflow the device/host receive queue. Final compression is
            # deferred until acquisition has stopped.
            with tmp.open("xb") as f:
                np.savez(f, **{
                    name: arr[: self._used] for name, arr in self._columns()
                })
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        self._chunk_count += 1
        self._used = 0

    def add(self, seq, t_us, device_time_us, host_t, host_t_ns, host_received_ns,
            dropped, a, b=None, raw=None) -> None:
        if self._sealed:
            raise RecordError("cannot add samples after this recording was finalized")
        if self._used == self.cap:
            self._flush()
        i = self._used
        self.seq[i] = seq
        self.t_us[i] = t_us
        self.device_time_us[i] = device_time_us
        self.host_t[i] = host_t
        self.host_t_ns[i] = host_t_ns
        self.host_received_ns[i] = host_received_ns
        self.dropped[i] = dropped
        self.a[i] = a
        if b is not None and self.b is not None:
            self.b[i] = b
        if self.raw is not None and self.raw_valid is not None:
            if raw is None:
                self.raw[i].fill(0)
                self.raw_valid[i] = False
            else:
                values = np.asarray(raw)
                if values.shape != self._rawshape:
                    raise ValueError(
                        f"raw sensor sample must have shape {self._rawshape}, got {values.shape}"
                    )
                if not np.issubdtype(values.dtype, np.integer):
                    raise ValueError("raw sensor sample must contain integers")
                if values.size and (int(values.min()) < -32768 or int(values.max()) > 32767):
                    raise ValueError("raw sensor sample is outside signed int16 range")
                self.raw[i] = values
                self.raw_valid[i] = True
        self._used += 1
        self.n += 1

    def shape(self, name: str) -> Tuple[int, ...]:
        arr = getattr(self, name)
        if arr is None:
            raise KeyError(name)
        return (self.n,) + arr.shape[1:]

    def dtype(self, name: str) -> np.dtype:
        arr = getattr(self, name)
        if arr is None:
            raise KeyError(name)
        return arr.dtype

    def iter_column(self, name: str) -> Iterator[np.ndarray]:
        """Yield disk chunks followed by the live tail, each in row order."""
        if getattr(self, name) is None:
            raise KeyError(name)
        for index in range(self._chunk_count):
            path = self._work_dir / f"chunk_{index:08d}.npz"
            with np.load(path, allow_pickle=False) as chunk:
                # Loading one fixed-size column is still bounded by ``cap`` and
                # avoids ever materializing all columns or all episode rows.
                yield chunk[name]
        if self._used:
            yield getattr(self, name)[: self._used]

    def seal(self) -> None:
        self._sealed = True

    def __len__(self) -> int:
        return self.n


class Recorder:
    """Collects samples, then writes them. Use `oglo.record()` unless you need this."""

    def __init__(self, glove: Any, path: Path, *, chunk_samples: int = 4096) -> None:
        self.glove = glove
        # Metadata describes the semantics at capture start. Reading glove.info at
        # finalization let a later RAW/CLEAN/threshold/rate change retroactively
        # relabel earlier rows. Info is frozen, but its list/dict members are not.
        self.info = deepcopy(glove.info)
        transport = getattr(glove, "_t", None)
        self.tag_version = getattr(transport, "tag_version", None)
        self.stream_boot_id = deepcopy(getattr(transport, "stream_boot_id", None))
        self.dir = Path(path)
        self._work = self.dir / f".recording-{uuid4().hex}"
        chunks = self._work / "chunks"
        self._t = _Buffer(chunks / "tactile", ashape=(5, 4, 4), cap=chunk_samples)
        self._i = _Buffer(
            chunks / "imu", ashape=(3,), bshape=(3,), rawshape=(6,), cap=chunk_samples
        )
        self._m = _Buffer(
            chunks / "mag", ashape=(3,), rawshape=(3,), cap=chunk_samples
        )
        self._started_wall: Optional[float] = None
        self._started_mono: Optional[float] = None
        self._ended_wall: Optional[float] = None
        self._ended_mono: Optional[float] = None
        self._finalized = False
        self._marker_published = False
        self.status_start: Optional[Dict[str, Any]] = None
        self.status_end: Optional[Dict[str, Any]] = None
        self.dropped_start: Optional[Dict[str, int]] = None
        self.dropped_end: Optional[Dict[str, int]] = None
        self._last_added_mono: Dict[str, Optional[float]] = {
            "tactile": None, "imu": None, "mag": None,
        }
        self._last_progress_mono: Dict[str, Optional[float]] = {
            "tactile": None, "imu": None, "mag": None,
        }

    def add_tactile(self, f: Frame) -> None:
        self._stamp()
        self._last_added_mono["tactile"] = int(f.host_received_ns) / 1_000_000_000.0
        self._t.add(f.seq, f.t_us, f.device_time_us, f.host_t, f.host_t_ns,
                    f.host_received_ns, f.dropped, f.counts)

    def add_imu(self, s: ImuSample) -> None:
        self._stamp()
        self._last_added_mono["imu"] = int(s.host_received_ns) / 1_000_000_000.0
        self._i.add(s.seq, s.t_us, s.device_time_us, s.host_t, s.host_t_ns,
                    s.host_received_ns, s.dropped, s.accel, s.gyro, raw=s.raw)

    def add_mag(self, m: MagSample) -> None:
        self._stamp()
        self._last_added_mono["mag"] = int(m.host_received_ns) / 1_000_000_000.0
        self._m.add(m.seq, m.t_us, m.device_time_us, m.host_t, m.host_t_ns,
                    m.host_received_ns, m.dropped, m.field, raw=m.raw)

    def _stamp(self) -> None:
        self.begin_capture()

    def begin_capture(self) -> None:
        """Publish the fail-closed episode marker before reading capture rows."""
        if self._ended_wall is not None:
            raise RecordError("cannot add samples after the capture end was marked")
        if self._started_wall is None:
            self._started_wall = time.time()
            self._started_mono = time.monotonic()
        if not self._marker_published:
            # Publish before the first row can spill into hidden disk chunks. A
            # process/power failure then leaves an explicitly incomplete episode,
            # never an unexplained directory of private implementation files.
            self.dir.mkdir(parents=True, exist_ok=True)
            marker = self._meta()
            marker.update(
                complete=False,
                stop_reason="recording",
                error="capture did not reach finalization",
            )
            _atomic_text(self.dir / "meta.json", json.dumps(marker, indent=1) + "\n")
            self._marker_published = True

    def finish_capture(self) -> None:
        """Freeze capture-end clocks before status reads and compression work."""
        if self._ended_wall is None:
            self._ended_wall = time.time()
            self._ended_mono = time.monotonic()

    @property
    def has_data(self) -> bool:
        return bool(len(self._t) or len(self._i) or len(self._m))

    def write(self, *, complete: bool = True, error: Optional[str] = None,
              stop_reason: str = "requested") -> Path:
        if not len(self._t) and not len(self._i) and not len(self._m):
            raise RecordError("nothing was captured; refusing to write an empty episode")
        if self._finalized:
            raise RecordError("this recording was already finalized")
        stage = self._work / f"publish-{uuid4().hex}"
        hidden_meta = self.dir / f".meta.json.{uuid4().hex}.stage"
        meta: Optional[Dict[str, Any]] = None
        try:
            self.finish_capture()
            self.dir.mkdir(parents=True, exist_ok=True)
            meta = self._meta()
            meta.update(
                complete=bool(complete),
                stop_reason=stop_reason,
                error=error,
            )

            # Readers may discover a reserved episode while it is being compressed.
            # A fail-closed marker is published first, and complete=true only becomes
            # visible after every NPZ has been staged and atomically replaced.
            marker = dict(meta)
            marker.update(
                complete=False,
                stop_reason=stop_reason if not complete else "finalizing",
                error=error if not complete else "episode finalization is in progress",
            )
            _atomic_text(self.dir / "meta.json", json.dumps(marker, indent=1) + "\n")
            stage.mkdir(parents=True)
            common = {
                "seq": "seq",
                "t_us": "t_us",
                "device_time_us": "device_time_us",
                "host_t": "host_t",
                "host_t_ns": "host_t_ns",
                "host_received_ns": "host_received_ns",
                "dropped": "dropped",
            }
            _write_buffer_npz(stage / "tactile.npz", self._t, {
                **common, "counts": "a",
            })
            _write_buffer_npz(stage / "imu.npz", self._i, {
                **common, "accel": "a", "gyro": "b", "raw": "raw",
                "raw_valid": "raw_valid",
            })
            _write_buffer_npz(stage / "mag.npz", self._m, {
                **common, "field": "a", "raw": "raw", "raw_valid": "raw_valid",
            })
            _atomic_text(stage / "meta.json", json.dumps(meta, indent=1) + "\n")

            # Publish data files first and the authoritative metadata last. If any
            # replace fails, the marker remains complete=false and replay cannot
            # mistake a mixed set for a complete episode.
            for name in ("tactile.npz", "imu.npz", "mag.npz"):
                os.replace(stage / name, self.dir / name)
            os.replace(stage / "meta.json", hidden_meta)
            shutil.rmtree(self._work)
            os.replace(hidden_meta, self.dir / "meta.json")
        except BaseException as exc:
            try:
                hidden_meta.unlink()
            except FileNotFoundError:
                pass
            failure = dict(meta or {
                "schema": SCHEMA,
                "started_wall": self._started_wall,
                "started_monotonic": self._started_mono,
                "ended_wall": self._ended_wall,
                "ended_monotonic": self._ended_mono,
                "counts": {
                    "tactile": len(self._t), "imu": len(self._i), "mag": len(self._m),
                },
            })
            detail = f"{type(exc).__name__}: {exc}"
            failure.update(
                complete=False,
                stop_reason="write_error",
                error=(f"{error}; finalization failed: {detail}" if error
                       else f"episode finalization failed: {detail}"),
            )
            try:
                _atomic_text(
                    self.dir / "meta.json", json.dumps(failure, indent=1) + "\n"
                )
            except Exception:
                pass
            try:
                setattr(exc, "partial_episode", self.dir)
            except Exception:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            wrapped = RecordError(f"could not finalize episode {self.dir}: {detail}")
            wrapped.partial_episode = self.dir
            raise wrapped from exc

        for buffer in (self._t, self._i, self._m):
            buffer.seal()
        self._finalized = True
        return self.dir

    def _meta(self) -> Dict[str, Any]:
        from . import __version__

        info = self.info
        status_start = self.status_start or {}
        status_end = self.status_end or {}

        def delta(name: str) -> Optional[int]:
            a, b = status_start.get(name), status_end.get(name)
            if not isinstance(a, int) or not isinstance(b, int) or b < a:
                return None
            return b - a

        dropped_start = self.dropped_start or {}
        dropped_end = self.dropped_end or {}
        dropped_during_capture = _counter_deltas(dropped_start, dropped_end)

        return {
            "schema": SCHEMA,
            "sdk_version": __version__,
            # Identity. A dataset that cannot say which board and which firmware
            # produced it cannot be compared with another one.
            "serial": info.serial,
            "side": info.side,
            "hw_rev": info.hw_rev,
            "fw_rev": info.fw_rev,
            "channels": list(info.channels),
            "has_mag": info.has_mag,
            "transport": info.transport,
            # Wire/session provenance. A native u64 timestamp is only meaningful if
            # the recording says it came from TAG v2, and boot identity must be the
            # value captured at stream start rather than a later CONFIG observation.
            "tag_version": self.tag_version if info.transport == "usb" else None,
            "tag_ver_max": info.tag_ver_max,
            "boot_id": self.stream_boot_id,
            # The calibration IN FORCE AT CAPTURE TIME. `stream_thr` is mutable on the
            # device, so asking the board later returns today's value, not the one
            # this data was taken under. Without it the counts cannot be interpreted.
            "stream_clean": info.stream_clean,
            "stream_thr": info.stream_thr,
            "zero_valid": info.zero_valid,
            "rate_hz": info.rate_hz,
            "imu_period_ms": info.imu_period_ms,
            # Two clocks. `t_us` counts from the board's own power-on and is unrelated
            # between gloves; `host_t` is monotonic on this machine; `started_wall` is
            # the only one that means a date. All three, because each answers a
            # question the others cannot.
            "started_wall": self._started_wall,
            "started_monotonic": self._started_mono,
            "ended_wall": self._ended_wall,
            "ended_monotonic": self._ended_mono,
            "counts": {
                "tactile": len(self._t), "imu": len(self._i), "mag": len(self._m)
            },
            # Capture-window deltas, not lifetime totals from a glove that happened
            # to be streaming before record() was called.
            "dropped": dropped_during_capture,
            "dropped_start": dropped_start,
            "dropped_end": dropped_end,
            "device_dropped_at_connect": int(getattr(info, "device_dropped", 0)),
            "status_start": self.status_start,
            "status_end": self.status_end,
            "device_counters_during_capture": {
                "tag_dropped": delta("tag_dropped"),
                "tag_short_writes": delta("tag_short_writes"),
                "deadline_misses": delta("deadline_misses"),
            },
        }


def _write_buffer_npz(path: Path, buffer: _Buffer,
                      columns: Mapping[str, str]) -> None:
    """Create an NPZ while holding at most one fixed-size chunk in memory.

    NPZ is a ZIP of ordinary NPY members. Writing the one header for the final
    shape followed by each chunk's raw bytes is byte-for-byte the same array layout
    as ``numpy.savez_compressed`` without first concatenating every chunk.
    """
    with path.open("xb") as raw_file:
        with zipfile.ZipFile(
            raw_file, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for public_name, internal_name in columns.items():
                dtype = buffer.dtype(internal_name)
                header = {
                    "descr": np.lib.format.dtype_to_descr(dtype),
                    "fortran_order": False,
                    "shape": buffer.shape(internal_name),
                }
                with archive.open(
                    f"{public_name}.npy", mode="w", force_zip64=True
                ) as member:
                    np.lib.format.write_array_header_1_0(member, header)
                    for piece in buffer.iter_column(internal_name):
                        contiguous = np.ascontiguousarray(piece, dtype=dtype)
                        member.write(memoryview(contiguous).cast("B"))
        raw_file.flush()
        os.fsync(raw_file.fileno())


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def record(path: Any, seconds: Optional[float] = None, *, glove: Any = None,
           serial: Optional[str] = None) -> Path:
    """Capture an episode. Returns the directory written.

    With no `glove`, one is opened and closed for you. `seconds=None` records until
    interrupted, which is what a person at a keyboard wants; a script should pass a
    number.
    """
    if seconds is not None and (
        isinstance(seconds, bool)
        or not isinstance(seconds, Real)
        or not math.isfinite(float(seconds))
        or float(seconds) <= 0
    ):
        raise ValueError("seconds must be None or a finite real number greater than zero")
    own = glove is None
    if own:
        from . import connect

        glove = connect(serial)
    recording_guard = False
    try:
        begin_recording = getattr(glove, "_begin_recording", None)
        if begin_recording is not None:
            begin_recording()
            recording_guard = True
        try:
            start_status = glove.status()
        except Exception as exc:
            raise RecordError(f"refusing to record without a readable device status: {exc}") from exc
        start_issues = _status_issues(start_status, has_mag=bool(glove.info.has_mag))
        if start_issues:
            raise RecordError(
                "refusing to record while device health is bad: " + ", ".join(start_issues)
            )

        # Establish the acquisition-session boundary before snapshotting host loss
        # counters. A stopped Glove resets its transport counters on start; taking
        # the snapshot first would make the later value look as if it ran backward.
        if hasattr(glove, "start"):
            glove.start()

        target = next_episode_dir(Path(path))
        rec = Recorder(glove, target)
        rec.status_start = asdict(start_status)
        rec.dropped_start = dict(getattr(glove, "dropped", {}) or {})

        def publish(*, complete: bool, error: Optional[str] = None,
                    stop_reason: str) -> Path:
            """Seal files while a real Glove is quiet, then resume a fresh stream.

            Compression and fsync can take long enough on a Raspberry Pi SD card
            for an active transport to overflow.  It also leaves stale post-capture
            samples in the public queues.  Custom glove adapters without the SDK's
            private pause context keep their existing behaviour.
            """
            pause = getattr(glove, "_paused", None)
            if pause is None:
                return rec.write(
                    complete=complete, error=error, stop_reason=stop_reason
                )
            with pause(resume=not own):
                return rec.write(
                    complete=complete, error=error, stop_reason=stop_reason
                )
        try:
            rec.begin_capture()
        except BaseException:
            _discard_empty_reservation(target, rec)
            raise
        add = {"tactile": rec.add_tactile, "imu": rec.add_imu, "mag": rec.add_mag}

        def drain_once(progress_at: float) -> bool:
            if hasattr(glove, "read_batch"):
                ready = glove.read_batch().as_dict()
            else:
                ready = glove._drain_ready()
            for name, items in ready.items():
                if items:
                    # This timestamp is recorder-local progress, separate from
                    # the sample's preserved host receive timestamp. Custom
                    # adapters may use another monotonic epoch in their sample.
                    rec._last_progress_mono[name] = progress_at
                fn = add[name]
                for item in items:
                    fn(item)
            return any(ready.values())

        required_streams = ["tactile", "imu"] + (
            ["mag"] if glove.info.has_mag else []
        )

        def raise_on_silent_stream(now: float) -> None:
            """Fail closed if any fitted modality stops making progress."""
            start = rec._started_mono
            if start is None:
                return
            stale = []
            for name in required_streams:
                last = rec._last_progress_mono[name]
                age = now - (last if last is not None else start)
                if age >= RECORDING_STREAM_SILENCE_S:
                    stale.append(f"{name}:{age:.3f}s")
            if stale:
                raise RecordError(
                    "recording stream stalled for at least "
                    f"{RECORDING_STREAM_SILENCE_S:g}s: " + ", ".join(stale)
                )

        deadline = None if seconds is None else time.monotonic() + seconds
        stop_reason = "duration" if seconds is not None else "requested"
        try:
            loop_now = time.monotonic()
            while deadline is None or loop_now < deadline:
                # Take everything each stream has ready, not one from each in turn.
                # One-each throttles every stream to the slowest: the IMU produces
                # twice what tactile does, so half of it would be lost to queue
                # overflow and the episode would come back with three equal counts.
                if not drain_once(loop_now):
                    time.sleep(0.0005)
                loop_now = time.monotonic()
                raise_on_silent_stream(loop_now)
            # A busy host can be descheduled across the deadline while USB bytes
            # accumulate. Do one final non-blocking transport read before freezing
            # the capture clock; otherwise the deadline check wins without a poll
            # and a healthy buffered tail is misreported as every modality stopping.
            drain_once(loop_now)
        except KeyboardInterrupt:
            stop_reason = "keyboard_interrupt"
        except BaseException as exc:
            rec.finish_capture()
            rec.dropped_end = dict(getattr(glove, "dropped", {}) or {})
            try:
                rec.status_end = asdict(glove.status())
            except Exception as status_exc:
                rec.status_end = {"error": f"{type(status_exc).__name__}: {status_exc}"}
            if rec.has_data:
                try:
                    rec.write(complete=False, error=f"{type(exc).__name__}: {exc}",
                              stop_reason="error")
                    try:
                        setattr(exc, "partial_episode", target)
                    except Exception:
                        pass
                except Exception as save_exc:
                    failure = RecordError(
                        f"capture failed ({exc}) and the partial episode could not be saved: {save_exc}"
                    )
                    failure.partial_episode = target
                    raise failure from exc
            else:
                _discard_empty_reservation(target, rec)
            raise
        rec.finish_capture()
        rec.dropped_end = dict(getattr(glove, "dropped", {}) or {})
        try:
            end_status = glove.status()
            rec.status_end = asdict(end_status)
        except BaseException as exc:
            message = f"capture ended but final device status could not be read: {exc}"
            if rec.has_data:
                rec.status_end = {"error": f"{type(exc).__name__}: {exc}"}
                rec.write(complete=False, error=message, stop_reason="status_error")
                try:
                    setattr(exc, "partial_episode", target)
                except Exception:
                    pass
            else:
                _discard_empty_reservation(target, rec)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            failure = RecordError(message)
            failure.partial_episode = target
            raise failure from exc

        device_drops = max(0, end_status.tag_dropped - start_status.tag_dropped)
        short_writes = max(0, end_status.tag_short_writes - start_status.tag_short_writes)
        deadline_misses = max(0, end_status.deadline_misses - start_status.deadline_misses)
        device_reset = end_status.uptime_ms < start_status.uptime_ms
        end_issues = _status_issues(end_status, has_mag=bool(glove.info.has_mag))
        if device_drops:
            end_issues.append(f"new_tag_dropped={device_drops}")
        if short_writes:
            end_issues.append(f"new_tag_short_writes={short_writes}")
        if deadline_misses:
            end_issues.append(f"new_deadline_misses={deadline_misses}")
        if device_reset:
            end_issues.append("device_reset=true")

        # A nominally successful capture must contain every fitted stream. There is
        # deliberately no ultra-short exception: asking for a duration too short to
        # observe one sample of each sensor is not evidence of a complete episode.
        missing_streams = []
        if not len(rec._t):
            missing_streams.append("tactile")
        if not len(rec._i):
            missing_streams.append("imu")
        if glove.info.has_mag and not len(rec._m):
            missing_streams.append("mag")
        if missing_streams:
            end_issues.append("missing_streams=" + ",".join(missing_streams))
        too_short_to_check = []
        for name, buffer in (("tactile", rec._t), ("imu", rec._i), ("mag", rec._m)):
            if name == "mag" and not rec.info.has_mag:
                continue
            if 0 < len(buffer) < 2:
                too_short_to_check.append(f"{name}:{len(buffer)}")
        if too_short_to_check:
            end_issues.append(
                "insufficient_stream_samples=" + ",".join(too_short_to_check)
            )
        end_issues.extend(_modality_freshness_issues(rec))

        host_deltas = _counter_deltas(rec.dropped_start or {}, rec.dropped_end or {})
        for name, value in host_deltas.items():
            if not _is_strict_loss_counter(name):
                continue
            if value is None:
                end_issues.append(f"host_counter_reset={name}")
            elif value > 0:
                end_issues.append(f"host_loss_{name}={value}")
        if end_issues:
            message = (
                "capture ended with unhealthy device state: " + ", ".join(end_issues)
            )
            if rec.has_data:
                publish(complete=False, error=message, stop_reason="device_health")
                failure = RecordError(message)
                failure.partial_episode = target
                raise failure
            else:
                _discard_empty_reservation(target, rec)
            raise RecordError(message)
        try:
            return publish(complete=True, stop_reason=stop_reason)
        except Exception:
            if not rec.has_data:
                _discard_empty_reservation(target, rec)
            raise
    finally:
        if recording_guard:
            glove._end_recording()
        if own:
            glove.close()


def _status_issues(status: Any, *, has_mag: bool) -> List[str]:
    """Health predicates that depend on the fitted hardware configuration."""
    issues: List[str] = []
    if not status.imu_ok:
        issues.append("imu_ok=false")
    if has_mag and not status.mag_ok:
        issues.append("mag_ok=false")
    if not status.sensor_ok:
        issues.append("sensor_ok=false")
    if status.error_flags:
        issues.append(f"error_flags={status.error_flags}")
    return issues


def _modality_freshness_issues(recorder: Recorder) -> List[str]:
    """Detect a modality that stopped while the other streams kept arriving.

    Supported firmware does not expose sensor read-failure counters. A final STATUS can
    therefore remain healthy after USB IMU packets stop.  This host-side guard is
    deliberately lenient (at least three expected periods and never below 0.1 s) so
    scheduler/USB jitter does not reject good captures, while a sustained silent
    modality cannot be labelled complete.
    """
    start = recorder._started_mono
    end = recorder._ended_mono
    if start is None or end is None:
        return []
    info = recorder.info
    limits = {
        "tactile": max(0.1, 3.0 / max(1, int(info.rate_hz))),
        "imu": max(
            0.1,
            3.0 * ((info.imu_period_ms if info.imu_period_ms is not None else 2) / 1000.0),
        ),
        "mag": 0.1,
    }
    required = ["tactile", "imu"] + (["mag"] if info.has_mag else [])
    issues: List[str] = []
    for name in required:
        limit = limits[name]
        last = recorder._last_added_mono[name]
        # Missing streams have a clearer, separate error. Short captures also
        # cannot prove sustained freshness and are handled by the presence gate.
        if last is None or end - start < limit:
            continue
        age = end - last
        if age > limit:
            issues.append(f"stale_{name}_for={age:.3f}s")
    return issues


def _discard_empty_reservation(target: Path, recorder: Recorder) -> None:
    """Remove only files owned by an empty, never-finalized recorder."""
    if recorder.has_data:
        return
    shutil.rmtree(recorder._work, ignore_errors=True)
    for path in target.iterdir() if target.exists() else ():
        if path.name == "meta.json" or path.name.startswith(".meta.json."):
            try:
                path.unlink()
            except OSError:
                pass
    try:
        target.rmdir()
    except OSError:
        pass


def _counter_deltas(before: Mapping[str, int],
                    after: Mapping[str, int]) -> Dict[str, Optional[int]]:
    """Strict capture-window deltas; a counter moving backward is not zero loss."""
    out: Dict[str, Optional[int]] = {}
    for name in sorted(set(before) | set(after)):
        start = int(before.get(name, 0))
        end = int(after.get(name, 0))
        out[name] = end - start if end >= start else None
    return out


def _is_strict_loss_counter(name: str) -> bool:
    """Counters that prove samples or decodable transport input were discarded."""
    return (
        name.startswith("wire_")
        or name.startswith("overflow_")
        or name.startswith("transport_")
        or name.startswith("duplicate_")
        or name.startswith("backward_")
        or name == "unrouted_packets"
    )
