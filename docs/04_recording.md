# Recording and replay

Recordings are JSONL: one sensor sample per line. Files use the `og-skill` backend's
names, timestamps, and sensor channels. No conversion step is needed.

## Record one glove

```python
import oglo

path = oglo.record("out/", seconds=60)
print(path)  # For example: out/ep_0001
```

Or run `oglo record out/ --seconds 60`.
Each recording gets a new folder. Existing recordings are never overwritten.
The recorder reads the stored calibration over USB; it does not recalibrate.

## What is on disk

For a left glove:

| File | Contents |
| --- | --- |
| `meta.json` | Device, settings, counts, and completion status |
| `tactile_left.jsonl` | CLEAN touch values |
| `tactile_left.raw.jsonl` | Original touch values, when recorded in RAW mode |
| `wrist_imu_left.jsonl` | Raw integer acceleration and gyroscope channels |
| `wrist_mag_left.jsonl` | Raw integer magnetic channels; may be empty |
| `tactile_left.calibration.json` | Saved zero recipe and how it was applied |

Right-hand files use `_right`. Keep the entire folder.

## RAW and CLEAN

A RAW capture preserves the original values and derives a CLEAN file from the
stored baseline and threshold. A CLEAN capture saves what firmware sent; it cannot
recover the RAW values removed by firmware.

A RAW capture without a valid zero recipe is kept as incomplete. The same applies
when raw IMU or magnetometer values are missing. These recordings must not be used
as successful backend input. See [calibration](03_calibration.md).

## Reading one back

```python
import oglo

episode = oglo.replay("out/ep_0001")
print(episode.summary())
for frame in episode.tactile():
    print(frame.counts)
```

Use `episode.imu()` or `episode.mag()` for the other sensors. Replay needs no
hardware and validates the files, including any CLEAN file derived from RAW.

For array calculations, use `episode.arrays("tactile")`. This loads that stream
into memory. Replay preserves the capture's RAW/CLEAN mode. `frame.residual` is
available only for CLEAN captures.

The current episode format is schema 3. Earlier recording schemas are not supported.
Exact JSONL fields are in the [data specification](09_data_specification.md#sensor-rows).

## Both hands

Use [04_two_hands.py](../examples/04_two_hands.py). It records each hand independently.
Compare `capture_ns` on the same computer for approximate alignment; device clocks
are independent. Pass `clock_domain="your-host"` to `record()` to name the host.

## Stop or handle a failed recording

For a live UI, pass `on_tactile=callback` to `oglo.record()`. The callback sees
the latest tactile frame in each drained batch after recording has accepted the
batch. It is a lossy preview feed; the saved JSONL contains every accepted row.
The callback must return quickly and must never read the glove separately.
Callback errors do not stop the recording.

Pass a `threading.Event()` as `stop_event` and call its `set()` method from another
thread to stop cleanly. An early stop can be complete but have
`stop_reason: "cancelled"`; check both fields when duration matters.

Errors preserve partial data with `complete: false`. The exception's
`partial_episode` gives its path. Keep failed recordings for diagnosis.
The final `meta.json` is written only after the stream files are ready.

## How long you can record

Live recording uses fixed-size batches of JSONL rows, so memory does not grow
with recording duration. Slow disks can still cause loss. A crash can lose
unwritten samples; unfinished hidden chunks have no recovery command.

Before long collection, run the [75-minute hardware test](07_acceptance.md#long-two-hand-soak)
on the intended host and disk.
