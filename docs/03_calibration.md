# Calibration

Calibration records the pressure caused by wearing and bending the glove. The
device saves one baseline for each of its 80 touch sensors.

**A new calibration replaces the stored baseline.** It requires a person wearing
the glove and a USB connection.

## Calibrate while moving your hand

Wear the glove. Touch nothing. Open and close your hand throughout the sweep:

```python
import oglo

with oglo.connect() as glove:
    glove.zero(sweep=5)
```

Five seconds is the default; firmware limits the duration to 1–30 seconds.
The device records each sensor's highest value during the sweep.

Keep moving: a baseline recorded with a still hand can mistake finger bending
for contact later.

Over USB, the SDK checks that the sweep started and finished, validates all 80
baseline and noise values, reads the recipe back with `GET ZERO`, and checks
`zero_valid` in the device configuration.

## Raw versus clean

| Mode | `frame.counts` | `frame.residual` | Use it for |
| --- | --- | --- | --- |
| RAW | Original ADC values, often around 550 at rest | Raises an error | Keeping the original measurements |
| CLEAN | Baseline removed and threshold applied by firmware | The same values as float32 | Reading contact values directly |

Choose the mode explicitly on a connected glove:

```python
glove.clean(threshold=30)  # Use the stored baseline and a cutoff of 30 counts.
glove.raw()               # Return to original ADC values.
```

These calls change device settings. CLEAN applies the same transformation for
all clients, including USB and BLE. RAW keeps information that cleaning removes.
The [recorder](04_recording.md#raw-and-clean) keeps RAW data and also creates
CLEAN data from the stored recipe.

## The threshold is a cutoff

Cleaning uses this rule:

```text
value = max(0, raw - baseline)
if value < threshold:
    value = 0
```

With a threshold of 30, values of 29, 30, and 31 become **0, 30, and 31**.
The threshold is not subtracted from the result.

One threshold applies to all 80 sensors. The stored per-sensor `noise` values are
diagnostic information; they are not separate thresholds.

## Keep the settings with the data

`oglo.record()` saves the mode and threshold in `meta.json`. Use those recorded
settings when interpreting old data; the glove's current settings may differ.

USB recording saves the full baseline/noise recipe in
`tactile_<side>.calibration.json`. A custom device adapter can pass the matching
`GET ZERO` response with `record(..., calibration=recipe)`.

## Check persistence when it matters

Firmware is intended to save calibration and mode in flash. The SDK cannot prove
that they survive a power cycle. To check, unplug the glove, reconnect it, and
compare the recipe and settings.

## BLE limits

BLE supports verified CLEAN and tactile-rate changes. `zero()` and
`rates(imu=...)` require USB because BLE does not provide the responses needed
to verify those operations.
