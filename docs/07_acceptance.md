# Test your gloves

For a left/right USB pair:

```bash
oglo acceptance
```

For exactly one connected glove:

```bash
oglo acceptance --single
```

The default checks identity, health, streams, short recording/replay, and
reconnection. It preserves calibration, mode, threshold, and rates.

## Additional checks

| Option | Action |
| --- | --- |
| `--interactive` | Follow prompts to press each finger and rotate the wrist |
| `--mutations` | Test setting changes, then attempt to restore them |
| `--zero` | Perform a new calibration; replaces the stored baseline |
| `--record 10s` | Set the short recording duration |
| `--no-record` | Skip the short recording |
| `--output PATH` | Choose the results folder |

For finger checks, press one region at a time. The default minimum response is
25 counts; change it with `--taxel-delta`.

If settings cannot be restored after `--mutations`, inspect the glove before
collecting more data.

`--zero` asks you to type `ZERO <serial>`. Wear the glove, touch nothing, and open
and close your hand during the sweep. `--zero --yes` skips confirmation and is
intended for controlled stations. See [calibration](03_calibration.md).

## Long two-hand soak

Test long recording on the actual destination disk:

```bash
oglo acceptance --soak 75m --output /path/on/the/target/disk
```

The duration crosses the device clock's roughly 72-minute rollover. Short checks
must pass first. Add `--single` for one glove.

Ctrl-C stops and saves the recordings. A cancelled run is not a passed long test.
If either hand fails, the other recorder is asked to stop too.

## Reports

Results go into a new `acceptance-results/run-<date-time>/` folder. Open
`acceptance-report.md`; keep `acceptance-report.json` with your data.

Checks are `PASS`, `WARN`, `FAIL`, or `SKIP`. Failed runs exit with code 2.
Optional checks are skipped unless requested.

A pass applies to the tested setup. It does not prove force accuracy or hardware
synchronization. Calibration persistence is reported as `SKIP`; check it by
comparing the recipe before and after unplugging and reconnecting the glove.
