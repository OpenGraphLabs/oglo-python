# Managed firmware candidate validation — 2026-09-23

SDK code checkpoint: `5ec6cd7f12c763ad8b7325c481083bb17f038e0b`.
Upstream integrated: `e564b9b449418e98033e1957c1a470a4cd0bb11c` (backend JSONL).
Candidate: `0.1.0rc8.dev1`. Host: macOS 27.0, arm64.

## Software checks

- Python 3.14.0, final source with camera dependencies: **635 passed, 1 skipped,
  11 hardware tests deselected**. The skip is Linux PTY kernel-exclusion coverage:
  Darwin PTYs did not enforce TIOCEXCL. Native USB was tested separately below.
- Python 3.10.20, installed package with numpy 1.24.0, pyserial 3.5, bleak 0.21.0
  and cryptography 43.0.0: **614 passed, 2 skipped, 11 deselected** at the checkpoint
  before the final two read-only-policy propagation regressions were added. Skips
  were the camera module (OpenCV absent) and Linux PTY coverage.
- The final connection/policy/diagnostic subset: **105 passed**. This includes the
  explicit no-update override through automatic transport and pair connections.
- Camera examples with SyncField 0.8.14: **19 passed** (included in the 635 above).
- Wheel and sdist built; `twine check` passed. A separate wheel installation matched
  all **22 Python modules** byte-for-byte. Private bench files were absent from
  both distributions.

Fault tests cover signature/image/policy corruption, wrong physical identity,
missing pair member, blocked open/write/close, inherited-lock lifetime, lost READY,
intermediate/final ACK and COMMIT, partial writes, interrupted-state quiet recovery,
calibration mismatch, first-hand success/second-hand failure, repeated-image skip,
and history merge. These are simulations; they do not replace physical interruption
or host reboot testing.

## Mac application-update test

Device: **OGLO-L-00006**, USB chip **68EE8F4968F4**. One glove was attached.
The source running-image digest matched the approved stock **0.9.16** digest, and
`rollback_supported=true` was read from the device before writing.

The signed **0.9.17 application image** was sent using the new SDK worker. The
board **rebooted automatically without a manual USB disconnect**. The target running
hash and all CONFIG/ZERO preservation fields matched their expected values.
No ROM flash, factory reset or recalibration was used.

The first attempt's readiness stage rejected a `tag_short_writes` increase from
0 to 309. This was an incorrect new host predicate: the reviewed firmware retains
partial frames while incrementing this retry counter, and the existing recorder
already distinguishes that from actual loss on 0.9.16+. The correction retains
this diagnostic counter while rejecting real drops, sequence loss, reset, unhealthy
status and new deadline misses. Regression tests cover both cases.

A subsequent invocation retained the original backup, waited **65 seconds without
USB OUT**, rechecked the current image, **skipped rewriting**, and passed readiness:
761 tactile, 1,524 IMU and 381 magnetometer packets in approximately three seconds;
all host loss/duplicate/backward/malformed counters were zero. This exercises
recovery from a pending post-verification state, not an actual mid-flash power loss.

## Actual JSONL recording and final device state

The integrated SDK then connected through the same managed policy, skipped writing,
and recorded approximately **60 seconds**. JSONL publication and replay completed:

| Stream | Samples | Delivered packet rate | Sequence loss |
| --- | ---: | ---: | ---: |
| Tactile | 15,025 | 250.2 Hz | 0 |
| IMU | 30,050 | 500.4 Hz | 0 |
| Magnetometer | 7,512 | 125.1 Hz | 0 |

Device drop delta: **0**. Deadline-miss delta: **0**. Host wire/queue/parser,
duplicate and backward counters: **0**. Retry-counter delta: **3,457**, retained in
metadata. The recording also retained the actual runtime image hash, USB identity,
policy identity/hash, verification timestamp and attempt ID.

These are **delivered packets**, not proof of independently fresh IMU measurements
at 500 Hz, sensor accuracy, zero transport latency or overnight stability.

The opt-in hardware suite ran with `--hardware-single --hardware-mutations`:
**8 passed, 3 two-hand checks skipped**. It exercised discovery/identity/health,
serial selection, streams, stop/restart, link pings, five streaming open/close
cycles, diagnostics, and reversible RAW/CLEAN/threshold/sample-rate changes.
Afterwards the complete preservation snapshot was compared again with the original
0.9.16 CONFIG/ZERO: **unchanged**. The device remains on verified 0.9.17.

On this native Mac USB device, an overlapping SDK process was rejected with
`PortBusyError`, and a separate raw `os.open` was rejected with `EBUSY`. This does
not prove exclusion against all drivers, privileged clients or direct IOKit access.

## Remaining deployment gates

- Original 0.9.16 application update and interruption/recovery on the research
  Ubuntu/Zed host; physical host-reboot and partial-flash recovery.
- Two hands together, actual continuous long recording, and the research pair.
- The complete 40-device policy inventory and per-unit results.
- Sensor fresh-value rates, physical tactile quality, BLE and force calibration
  remain separate qualifications.

The web updater and firmware source are unchanged by this SDK work. Neither the
software tests nor this one-device Mac result approves an unattended fleet rollout.
Raw local evidence is retained under the ignored `acceptance-results/` directories
and the SDK state directory; calibration backups are not published in this repo.
