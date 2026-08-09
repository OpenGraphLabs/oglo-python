# Recording and replay

```python
ep = oglo.record("out/", seconds=60)     # -> out/ep_0001
```

```bash
oglo record out/ --seconds 60
oglo replay out/ep_0001
```

Episodes are numbered and never overwritten.

## Reading one back

```python
e = oglo.replay("out/ep_0001")

print(e.summary())
for f in e:                  # tactile frames, the same objects a live glove yields
    f.residual.max()
```

An `Episode` has the same shape as a `Glove`: `.info`, `.tactile()`, `.imu()`,
`.mag()`. That is the point. Swap `oglo.replay(path)` for `oglo.connect()` and nothing
downstream changes, **so you can write and finish your pipeline before the gloves
arrive.**

A test enforces this: it breaks the serial layer so any attempt to touch hardware
raises, then replays an episode.

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

Nothing is interpolated onto a common clock. Forcing one rate either invents samples
for the slow stream or throws them away from the fast one, and a dataset carries that
choice forever. Each stream keeps its own sequence and its own timestamps, and you
align them yourself with the numbers in front of you.

Nominal USB packet counts are about `IMU:tactile:mag = 4:2:1` at default
settings. Treat these as packet cadences, not proof of fresh physical sensor samples.

## Both hands

Record each hand independently, one thread each. Reading one sample from each in turn
locks them together and throttles both to the slower one.

```python
left, right = oglo.connect_pair()
threads = [threading.Thread(target=lambda g=g: oglo.record(f"out/{g.info.side}", 60, glove=g))
           for g in (left, right)]
```

Relate their transport-arrival timelines afterwards on `host_t`; samples from one
read can share a timestamp. This is coarse host alignment, not hardware trigger sync
or exact sensor-capture alignment. See `examples/04_two_hands.py`.

## How long you can record

`record()` keeps only a fixed-size block for each stream in RAM. Full blocks are
spooled below the episode's hidden working directory, and final NPZ files are built
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

This bounds SDK memory, but it is not proof of unlimited recording. A chunk flush is
a synchronous write and `fsync` on the same thread that drains USB; a slow Raspberry
Pi SD-card stall can still cause receive loss. The SDK refuses to mark the episode
complete when a sequence gap, overflow, malformed frame or
sustained freshness gap is observable. Firmware 0.9.10 has no end-to-end CRC or
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
presented as proof of a 0.9.10 multi-hour hardware capture. Release qualification must
include two hands for more than 72 minutes to cross the device-clock rollover, plus a
slow-storage stress run.

## Metadata worth knowing about

`meta.json` carries the logical board serial, side, hardware and firmware revision,
the finger order, **`stream_thr` and `stream_clean` as they were at capture time**,
both clocks, and the host loss counters. It also carries start/end `GET STATUS`,
device counter deltas, and `complete`/`error`. Without the threshold the counts cannot
be interpreted later, since the device's current value is not the one the data was
taken under.
