# Test your own glove pair

`oglo acceptance` is the owner-facing test for one physical left/right USB pair.
Unlike the developer pytest suite, it uses only the installed SDK's public API,
guides optional physical actions, and writes a durable Markdown and JSON report.

## Safe default

Connect exactly two gloves and run:

```bash
oglo acceptance
```

The default run does **not** change zero, threshold, RAW/CLEAN mode, or stream rates.
It checks:

- one left and one right glove with distinct logical serials
- firmware 0.9.10 or newer, CONFIG schema 6, USB transport, dimensions, and finger order
- sensor health and existing zero state
- public tactile, IMU, magnetometer, `stop()`, `start()`, and `read_batch()` paths
- simultaneous two-hand rate, timestamps, sequence gaps, malformed data, and overflow
- a short simultaneous recording and replay
- logical-serial reconnect after both original connections close

## Reports

Every run creates a new directory instead of overwriting evidence:

```text
acceptance-results/
  run-20260809-170000/
    acceptance-report.md
    acceptance-report.json
    recordings/
      left/ep_0001/
      right/ep_0001/
```

Each check is `PASS`, `WARN`, `FAIL`, or `SKIP` and includes the measured rates and
counters where useful. A failed run exits with status 2. Optional checks that were not
requested are `SKIP` and do not turn a healthy read-only run into a failure.

Use a different result root with `--output PATH`. Skip the short recording with
`--no-record`, or change its duration with `--record 10s`.

## Press every finger and move the IMU

```bash
oglo acceptance --interactive
```

The runner asks for each finger on each hand. It captures a released baseline, then
measures the press and verifies that the requested name is the strongest responding
4x4 region according to that device's `info.channels`. It also compares a still wrist
with a deliberate rotation.

This proves dynamic response and labeling, not force calibration. Press one sensing
area at a time; bending the whole hand can legitimately compress several fingers and
make the selected finger lose the "strongest" check. The default minimum response is
25 ADC counts and can be changed with `--taxel-delta`.

The magnetometer motion result only checks that values change. It does not validate
the magnetometer axes or prove a trustworthy heading.

## Reversible setting checks

```bash
oglo acceptance --mutations
```

This explicitly enables state-changing checks:

- switch to RAW and prove `Frame.residual` refuses raw data
- switch to CLEAN with a temporary threshold and verify the clean result
- change tactile rate and, when the measured starting IMU cadence is the known 500
  packets/s value, change IMU rate
- restore the observed tactile rate, RAW/CLEAN mode, threshold, and known IMU default
- read back the restored state

Restoration is attempted in `finally` even when an intermediate check fails. A
restoration failure is a `FAIL` and is printed prominently; do not continue collecting
customer data until the settings have been inspected manually.

## Replace the stored zero

```bash
oglo acceptance --zero
```

This is deliberately separate because it overwrites the only stored calibration.
For each glove the runner prints the serial and requires typing `ZERO <serial>`. Wear
that glove, touch nothing, and repeatedly open and close the hand during the sweep.

The SDK validates the completion recipe, all 80 baseline/noise entries, `GET ZERO`,
and CONFIG `zero_valid`. Supported firmware cannot prove that flash survived a power
cycle, so the report leaves that gate as `SKIP`. Unplug/replug the glove and run the
safe default again to provide separate read-back evidence.

`--zero --yes` bypasses the typed phrase and is intended only for a deliberately
controlled station. `--yes` by itself does nothing.

## Long two-hand soak

The device `t_us` counter wraps after roughly 71 minutes 35 seconds. Qualify that
boundary and the actual destination disk with:

```bash
oglo acceptance --soak 75m --output /path/on/the/target/disk
```

The short checks run first, then both hands record concurrently for 75 minutes. The
episodes are replayed and must be complete, correctly identified, non-empty in every
fitted modality, and free of recorded sequence gaps.

A successful soak is evidence for the machine, cables, hubs, gloves, duration, and
storage named in that report. It is not a permanent guarantee for every host.

## Deliberate scope

This command is USB-only. BLE remains experimental and must be qualified separately.
It does not claim:

- hardware synchronisation between the two gloves
- Newton/force calibration
- fused orientation or validated magnetometer axes
- payload integrity beyond what supported firmware exposes
- power-cycle zero persistence unless that physical cycle was separately performed

Run the default acceptance check before an important capture and attach its JSON
report to the dataset or deployment record.
