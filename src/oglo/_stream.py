"""Demultiplexing one transport into three streams, and measuring what arrives.

Three iterators share one serial port, so polling has to happen somewhere central:
whoever calls `next()` pumps the transport, and packets for the other two modalities
have to go somewhere. They go into bounded queues.

Bounded, because unbounded is a leak: a user who iterates `tactile()` and never
touches `imu()` would otherwise accumulate 500 IMU samples a second forever. When a
queue overflows the oldest sample is dropped and counted, and that count is kept
**separate from wire loss**. Three different things can lose a sample and a user
debugging a gap needs to know which:

    frame.dropped         the wire lost it (sequence gap)
    info.device_dropped   the device threw it away before sending
    stream.overflowed     we threw it away because nobody was reading
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterator, List, Optional

from . import _wire as w
from ._frame import Frame, ImuSample, MagSample, counts_to_grid

#: One second of every modality at the shipping rates, which is generous for a
#: consumer that is keeping up and a hard stop for one that is not.
DEFAULT_MAXLEN = {"tactile": 250, "imu": 500, "mag": 125}


@dataclass
class RateMeter:
    """Delivered rate over a sliding window of arrival times.

    Wall-clock, not device timestamps: this answers "what is this machine actually
    receiving", which is the question `oglo doctor` exists to answer and the one a
    device-clock average cannot.
    """

    window: float = 2.0
    _times: Deque[float] = field(default_factory=deque, repr=False)

    def tick(self, now: Optional[float] = None) -> None:
        t = time.monotonic() if now is None else now
        self._times.append(t)
        cutoff = t - self.window
        while self._times and self._times[0] < cutoff:
            self._times.popleft()

    def reset(self) -> None:
        self._times.clear()

    @property
    def hz(self) -> float:
        if len(self._times) < 2:
            return 0.0
        span = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / span if span > 0 else 0.0


class Stream:
    """A bounded queue of one modality, with its own loss accounting."""

    def __init__(self, name: str, maxlen: int) -> None:
        self.name = name
        self._q: Deque[Any] = deque(maxlen=maxlen)
        self.rate = RateMeter()
        #: Samples we discarded because the consumer was not reading. Not wire loss.
        self.overflowed = 0

    def push(self, item: Any) -> None:
        if len(self._q) == self._q.maxlen:
            self.overflowed += 1  # deque drops the oldest for us
        self._q.append(item)
        self.rate.tick(item.host_t)

    def pop(self) -> Optional[Any]:
        return self._q.popleft() if self._q else None

    def __len__(self) -> int:
        return len(self._q)

    def reset(self) -> None:
        self._q.clear()
        self.rate.reset()
        self.overflowed = 0

    def discard_pending(self) -> None:
        """Drop queued samples at an intentional semantic boundary.

        Keep the overflow counter: unlike :meth:`reset`, this is used while a live
        session is paused for a command, so loss accumulated earlier in that session
        must remain observable.
        """
        self._q.clear()
        self.rate.reset()


class DeviceTimeUnwrapper:
    """Turn a shared device u32 microsecond clock into a continuous integer clock.

    All three modalities share one instance. Older IMU readings embedded in a BLE
    tactile slot are mapped into the current epoch without moving the clock backward.
    """

    _MASK = 0xFFFFFFFF
    _HALF = 1 << 31

    def __init__(self) -> None:
        self._latest_raw: Optional[int] = None
        self._latest: Optional[int] = None

    def unwrap(self, raw_us: int) -> int:
        raw = int(raw_us) & self._MASK
        if self._latest_raw is None or self._latest is None:
            self._latest_raw = raw
            # Start in safety epoch 1. Sensor producers are independently scheduled,
            # so an older pre-rollover IMU packet can arrive in a later USB poll than
            # a post-rollover tactile packet. Once the first sample has been exposed
            # we cannot retroactively rebase it; the spare epoch lets that older
            # companion be represented as a smaller, still non-negative timestamp.
            # Only differences are meaningful; raw ``t_us`` remains available for
            # callers that need the exact wire value.
            self._latest = (self._MASK + 1) + raw
            return self._latest
        delta = (raw - self._latest_raw) & self._MASK
        if delta >= self._HALF:
            delta -= self._MASK + 1
        candidate = self._latest + delta
        if delta > 0:
            self._latest_raw = raw
            self._latest = candidate
        return candidate

    def shift_epoch(self) -> None:
        """Move the current epoch forward once when a signed companion proves wrap."""
        if self._latest is not None:
            self._latest += self._MASK + 1


@dataclass(frozen=True)
class _Prepared:
    kind: str
    packet: Any
    raw_us: int
    device_us: int
    received_ns: int


class Demux:
    """Pumps a transport and routes packets into per-modality streams.

    Also converts decoder records into the public sample types, which is where
    `host_t` is stamped and where the device's raw counts become a (5,4,4) grid.
    """

    def __init__(self, transport: Any, *, stream_clean: bool, maxlen: Optional[Dict[str, int]] = None) -> None:
        self._t = transport
        self._clean = stream_clean
        lens = {**DEFAULT_MAXLEN, **(maxlen or {})}
        self.tactile = Stream("tactile", lens["tactile"])
        self.imu = Stream("imu", lens["imu"])
        self.mag = Stream("mag", lens["mag"])
        self._last: Dict[str, Optional[int]] = {"tactile": None, "imu": None, "mag": None}
        self._clock = DeviceTimeUnwrapper()
        self.sequence_anomalies: Dict[str, Dict[str, int]] = {
            name: {"duplicate": 0, "backward": 0} for name in self._last
        }
        #: Packets of a type no branch handles. Should always be zero; if it is not,
        #: samples are being discarded silently.
        self.unrouted = 0

    def set_clean(self, clean: bool) -> None:
        self._clean = clean

    def reset_sequence(self) -> None:
        """Forget sequence references across an intentional stream-off interval."""
        self._last = {name: None for name in self._last}

    def start_session(self, *, reset_clock: bool = True) -> None:
        """Discard queued samples and counters at an explicit capture boundary."""
        for stream in (self.tactile, self.imu, self.mag):
            stream.reset()
        self.reset_sequence()
        self.sequence_anomalies = {
            name: {"duplicate": 0, "backward": 0} for name in self._last
        }
        self.unrouted = 0
        if reset_clock:
            self._clock = DeviceTimeUnwrapper()

    def discard_pending(self) -> None:
        """Discard pre-command samples without erasing session loss counters.

        A stream-mode or calibration command changes what subsequent counts mean.
        Returning already-queued raw samples after ``clean()`` (or vice versa) would
        put two incompatible data semantics in one public batch.
        """
        for stream in (self.tactile, self.imu, self.mag):
            stream.discard_pending()
        self.reset_sequence()

    def pump(self) -> int:
        """One read. Returns how many samples were routed."""
        packets = self._t.poll()
        fallback_received_ns = time.monotonic_ns()
        prepared: List[_Prepared] = []
        for packet in packets:
            prepared.extend(self._prepare(packet, fallback_received_ns))

        # On the first poll around micros() rollover, packets from independently
        # scheduled sensor producers can arrive in either order. If a post-wrap
        # tactile sample (raw=100) is seen before an older IMU sample
        # (raw=0xfffffa88), the latter initially unwraps to -1400. That is a valid
        # relative time, but not a valid public uint64 timestamp. The negative value
        # proves that the whole first batch belongs to the next epoch.
        if prepared:
            minimum = min(sample.device_us for sample in prepared)
            if minimum < 0:
                epochs = (-minimum + (1 << 32) - 1) // (1 << 32)
                for _ in range(epochs):
                    self._clock.shift_epoch()
                shift = epochs << 32
                prepared = [
                    _Prepared(
                        sample.kind,
                        sample.packet,
                        sample.raw_us,
                        sample.device_us + shift,
                        sample.received_ns,
                    )
                    for sample in prepared
                ]

        for sample in prepared:
            # This is an observed I/O boundary, not a guessed sensor time. Re-anchoring
            # each USB read to device time can make host time run backward when a
            # buffered read carries more device time than host time elapsed.
            self._route(sample, sample.received_ns)
        return len(prepared)

    def _prepare(self, p: Any, fallback_received_ns: int) -> List[_Prepared]:
        received_ns = int(getattr(p, "host_received_ns", None) or fallback_received_ns)
        if isinstance(p, w.TactilePacket):
            raw = p.t_us & 0xFFFFFFFF
            return [_Prepared("tactile", p, raw, self._clock.unwrap(raw), received_ns)]
        if isinstance(p, w.ImuPacket):
            raw = p.t_us & 0xFFFFFFFF
            return [_Prepared("imu", p, raw, self._clock.unwrap(raw), received_ns)]
        if isinstance(p, w.MagPacket):
            raw = p.t_us & 0xFFFFFFFF
            return [_Prepared("mag", p, raw, self._clock.unwrap(raw), received_ns)]
        if isinstance(p, w.BleSample):
            tactile_raw = p.t_us & 0xFFFFFFFF
            tactile_us = self._clock.unwrap(tactile_raw)
            imu_dt = int(p.imu_dt_us or 0)
            imu_raw = (tactile_raw + imu_dt) & 0xFFFFFFFF
            # The signed offset is relative to this tactile sample. On the first
            # notify just after rollover it is the only evidence that tactile belongs
            # to epoch 1, not epoch 0; rebase both before exposing a negative u64.
            imu_us = tactile_us + imu_dt
            if imu_us < 0:
                self._clock.shift_epoch()
                tactile_us += 1 << 32
                imu_us += 1 << 32
            out = [
                _Prepared("tactile", p, tactile_raw, tactile_us, received_ns),
                _Prepared("imu", p, imu_raw, imu_us, received_ns),
            ]
            if p.mag is not None:
                out.append(_Prepared("mag", p, tactile_raw, tactile_us, received_ns))
            return out
        self.unrouted += 1
        return []

    def _route(self, sample: _Prepared, host_t_ns: int) -> None:
        p = sample.packet
        host_t = host_t_ns / 1_000_000_000.0
        common = dict(
            t_us=sample.raw_us,
            host_t=host_t,
            device_time_us=sample.device_us,
            host_t_ns=host_t_ns,
            host_received_ns=sample.received_ns,
        )
        if sample.kind == "tactile":
            self.tactile.push(
                Frame(
                    seq=p.seq,
                    counts=counts_to_grid(p.counts),
                    dropped=self._gap("tactile", p.seq),
                    _stream_clean=self._clean,
                    **common,
                )
            )
        elif sample.kind == "imu":
            raw = p.imu_raw if isinstance(p, w.BleSample) else p.raw
            self.imu.push(
                ImuSample(
                    seq=p.seq,
                    accel=p.accel, gyro=p.gyro,
                    dropped=self._gap("imu", p.seq), raw=raw,
                    **common,
                )
            )
        elif sample.kind == "mag":
            raw = p.mag_raw if isinstance(p, w.BleSample) else p.raw
            self.mag.push(
                MagSample(
                    seq=p.seq,
                    field=p.field if isinstance(p, w.MagPacket) else p.mag,
                    dropped=self._gap("mag", p.seq), raw=raw,
                    **common,
                )
            )

    def _gap(self, name: str, seq: int) -> int:
        transition = w.classify_seq(self._last[name], seq)
        if transition.kind in ("first", "forward", "wrap"):
            self._last[name] = seq
        elif transition.kind in ("duplicate", "backward"):
            self.sequence_anomalies[name][transition.kind] += 1
        return transition.missing

    def drain_ready(self) -> Dict[str, list]:
        """One pump, then everything currently queued, per stream.

        Recording needs this rather than the iterators. Taking one sample from each
        stream per pass silently rate-limits every stream to the slowest of them: the
        IMU produces twice what tactile does, so half of it would be discarded by
        queue overflow and the episode would come back with three equal counts. That
        is resampling, and it is exactly what the format exists to avoid.
        """
        self.pump()
        out: Dict[str, list] = {}
        for name in ("tactile", "imu", "mag"):
            stream: Stream = getattr(self, name)
            items = []
            while True:
                item = stream.pop()
                if item is None:
                    break
                items.append(item)
            out[name] = items
        return out

    def iterate(self, name: str, *, timeout: Optional[float] = None) -> Iterator[Any]:
        """Yield from one stream, pumping the transport when it runs dry.

        `timeout` is seconds of silence before `StopIteration`. `None` means block
        forever, which is what a capture loop wants.
        """
        stream: Stream = getattr(self, name)
        last = time.monotonic()
        while True:
            item = stream.pop()
            if item is not None:
                last = time.monotonic()
                yield item
                continue
            routed = self.pump()
            # A poll may have routed only other modalities. Check the requested
            # queue again before applying its silence deadline; tying timeout to
            # ``pump()==0`` made a dead tactile producer block forever while IMU
            # packets continued to arrive normally.
            item = stream.pop()
            if item is not None:
                last = time.monotonic()
                yield item
                continue
            if timeout is not None and time.monotonic() - last >= timeout:
                return
            if routed == 0:
                time.sleep(0.0005)  # nothing ready; do not spin a core
