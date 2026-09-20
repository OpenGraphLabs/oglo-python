# Recording and replay

```python
ep = oglo.record("out/", seconds=60)     # -> out/ep_0001
```

```bash
oglo record out/ --seconds 60
oglo replay out/ep_0001
```

Episodes are numbered and never overwritten.

The candidate accepts `stop_event=threading.Event()` on `record()`.
Another thread can call that event's `set()` to stop capture and seal the files
without killing the process. Metadata records `stop_reason="cancelled"`; a healthy
shortened capture can be complete, but it does not prove the requested duration.
The acceptance runner rejects cancelled captures as duration/soak evidence.

The development recorder also detects a fitted stream that delivers no samples for
over five seconds, including a completely silent USB endpoint. It raises
`RecordError` and preserves received data as an incomplete episode. Detection runs
after polling available bytes, so a host scheduling pause alone does not trigger
it. Final status collection and file publication can take additional time; this
guard detects a stalled capture but does not repair the underlying USB fault.

## Reading one back

```python
import oglo

e = oglo.replay("out/ep_0001")

print(e.summary())
for f in e:                  # tactile frames, the same objects a live glove yields
    values = f.residual if e.info.stream_clean else f.counts
    print(values.max())
```

RAW recordings expose ADC counts through `counts`; `residual` is only available
for recordings captured in CLEAN mode. Replay preserves the recorded mode.

An `Episode` exposes `.info`, `.tactile()`, `.imu()`, and `.mag()` with the same
sample types as a live `Glove`. Replay requires no hardware and preserves the
recorded calibration and stream settings.

## What is on disk

```
ep_0001/
  meta.json     identity, calibration, status/loss snapshots, complete/error state
  tactile.npz   seq, raw/unwrapped device time, host times, counts, dropped
  imu.npz       seq, times, accel, gyro, raw/raw_valid, dropped
  mag.npz       seq, times, field, raw/raw_valid, dropped
```

Plain `.npz`, so anything can open them:

```python
import numpy as np
d = np.load("out/ep_0001/tactile.npz")
d["counts"].shape        # (N, 5, 4, 4)
```

`Episode.summary()` computes delivered counts and rates from the recorded host
receive boundaries. Samples from one USB read or BLE notification share a host
timestamp, so very short captures can have coarse rate estimates. The raw device
clock remains available for within-glove sample spacing.

## Three streams, three files, no resampling

Each stream keeps its own rate, sequence numbers, and timestamps. Align them in
postprocessing; the SDK does not interpolate or resample.

Nominal USB packet counts are about `IMU:tactile:mag = 4:2:1` at default
settings. Treat these as packet cadences, not proof of fresh physical sensor samples.

## Both hands

Record each hand independently, one thread each. Reading one sample from each in turn
locks them together and throttles both to the slower one.

Relate their transport-arrival timelines afterwards on `host_t`; samples from one
read can share a timestamp. This is coarse host alignment, not hardware trigger sync
or exact sensor-capture alignment. See the complete
[two-hand example](../examples/04_two_hands.py) for recording, cleanup, and replay.

## How long you can record

`record()` keeps a fixed-size live block for each stream in RAM. A separate storage
worker spools immutable copies of full blocks below the episode's hidden working
directory. Its queue holds at most eight blocks in addition to the block being
written. Disk writes and `fsync` do not run on the thread that drains the glove.
Final NPZ files wait for all queued blocks and are built
from those blocks without joining the whole capture in memory. Episode directory
numbers are atomically reserved, so simultaneous recorders cannot overwrite one
another.

An incomplete `meta.json` marker is published before capture begins. Final files are
staged first, then the three NPZ files and finally the authoritative metadata are
replaced. A disconnect or detected loss therefore leaves `complete=false`; it cannot
silently look like a healthy finished episode.

`complete=true` also requires at least two rows from every required modality and a
fresh row near the capture boundary. If tactile keeps arriving after IMU or
magnetometer packets stop, the episode is sealed as incomplete instead of treating
the earlier rows as proof that the sensor remained alive. At a requested duration
boundary the recorder performs one final non-blocking read, so bytes already queued
while the host was descheduled are included before that freshness check.

On an exception, the original exception is re-raised with `partial_episode` pointing
to that directory; the CLI prints the path.

This bounds SDK memory, but it is not proof of unlimited recording. If storage
cannot keep up and the queue fills, capture stops with an explicit storage-backlog
error and saves the accepted rows as an incomplete episode. A write failure also
prevents `complete=true`; recoverable chunks and failure metadata remain available.
The SDK refuses to mark the episode
complete when a sequence gap, overflow, malformed frame or
sustained freshness gap is observable. Supported firmware has no end-to-end CRC or
read-failure counters, so that is not proof that every short tail loss is detectable;
release qualification must measure it on the target storage. A hard process/power
loss can also lose the not-yet-flushed RAM tail; there is not yet a recovery command
that publishes the already-spooled hidden chunks.

For operationally bounded files, segmentation is still useful:

```python
for i in range(12):
    oglo.record("out/", seconds=300, glove=g)   # 5 minutes each
```

The repository does not contain a raw current long-soak report, so unit tests are not
presented as proof of a multi-hour hardware capture. Release qualification must
include two hands for more than 72 minutes to cross the device-clock rollover, plus a
slow-storage stress run.

## Metadata worth knowing about

`meta.json` carries the logical board serial, side, hardware and firmware revision,
the finger order, **`stream_thr` and `stream_clean` as they were at capture time**,
both clocks, and the host loss counters. It also carries start/end `GET STATUS`,
device counter deltas, and `complete`/`error`. Without the threshold the counts cannot
be interpreted later, since the device's current value is not the one the data was
taken under.

For firmware 0.9.16 or newer, `tag_short_writes` records USB backpressure attempts
that firmware retries. The counter remains in metadata and its start/end/delta
consistency is checked during replay, but a positive value alone does not make a
capture incomplete. Device drops, missed deadlines, sequence gaps, malformed data
and host overflow still invalidate a capture. Older firmware retains the stricter
short-write rejection.
