# Calibration

There is exactly one calibration: the **per-taxel zero**. It lives on the device.

## Why a still hand is not enough

Velostat responds to pressure, and bending a finger applies pressure on its own. A
baseline captured with your hand held still is only correct for a hand held still.
Make a fist afterwards and every taxel in that finger reads high, and the data shows
a grip that never happened.

So the calibration is a **sweep**: open and close your hand for a few seconds while
the board records the maximum each taxel reaches. That envelope becomes the baseline.

```python
g.zero(sweep=5)      # wear the glove, open and close, touch nothing
```

Five seconds is the default and is usually enough. The firmware clamps to 1-30 s.

The still-hand and two-pose variants that older tools offered were removed, not
hidden: on a worn glove they capture an average that live readings exceed about half
the time. This SDK does not offer them back.

## Raw versus clean

The zero can be applied on the device or not at all.

```python
g.clean(threshold=30)    # device subtracts the baseline and applies a deadband
g.raw()                  # device sends unprocessed counts
```

**Clean is the recommended path**, because it is the only way USB, BLE and any
third-party client see byte-identical data. The transform happens once, on the board.

| | `f.counts` | `f.residual` |
| --- | --- | --- |
| clean | already zeroed | same values |
| raw | raw ADC (~550 idle) | **raises**; there is no host-baseline fallback |

`residual` raising on a raw stream is deliberate. Silently handing back raw counts
that look like a residual is how a dataset ends up quietly wrong.

## The deadband is a cutoff, not a subtraction

```
out = (raw - baseline) < thr ? 0 : (raw - baseline)
```

A value one count above the threshold reports `thr + 1`, **not** `1`. A host that
subtracts the threshold again changes the data and is wrong for the supported
contract.

`thr` is one scalar shared by all 80 taxels. The per-taxel `noise` the board stores
alongside the baseline is diagnostic only and must not be used to size it.

## Record the threshold with your data

`stream_thr` is mutable at runtime. Asking the board later returns **today's** value,
not the one your data was taken under, so counts recorded without it cannot be
interpreted afterwards.

`oglo.record()` writes it into `meta.json` automatically. If you build your own
capture path, carry it yourself.

## What is intended to survive a power cycle

The firmware stores the zero and stream mode in device flash, and
`g.info.zero_valid` reports the active state after connection. Firmware 0.9.9 does
not report the flash-write result or perform a power-cycle readback, so the SDK alone
cannot prove persistence. Reboot, reconnect and compare the recipe when that is a
release or factory gate.

## Over BLE

`clean()` and tactile-rate changes can be confirmed by re-reading config. Firmware
does not expose the applied IMU period in BLE config, so `rates(imu=...)` is also
USB-only rather than returning an unverified success.
`zero()` is deliberately USB-only: firmware 0.9.9 sends the start/completion lines
and full `GET ZERO` recipe only over serial. Without those, BLE can send the command
but cannot prove capture or persistence completed, so the SDK fails immediately
instead of waiting and then pretending success.

Over USB, `zero()` requires the start acknowledgement, validates all 80 baseline and
noise values, re-reads them with `GET ZERO`, and finally verifies `zero_valid` in
config before returning.

That proves the active firmware recipe is consistent. Firmware 0.9.9 does not report
the NVS write result or re-read flash before replying, so the SDK cannot prove power-
cycle persistence without an actual reboot/reconnect check. Do that as a release or
factory gate; do not interpret a successful call as an atomic-flash guarantee.
