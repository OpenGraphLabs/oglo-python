# Compatibility and validation scope

This page separates protocol support, automated test coverage, and measurements on
physical gloves. They are different claims.

## Supported contract

| Component | Status in 0.1.0rc1 |
| --- | --- |
| Python | 3.10 or newer |
| Live-glove firmware | 0.9.10 |
| CONFIG schema | exactly 6 |
| USB tagged stream | supported and hardware-validated |
| USB legacy interleaved frame | decoder and captured-vector compatibility |
| BLE schema-6 notifications | experimental; parser-tested, not release-qualified |
| Firmware 0.9.9 | historical parser/vector compatibility only; upgrade before live use |
| Firmware 0.9.8 and older | rejected |

The `0.1.0rc1` parser floor remains 0.9.9 so historical schema-6 golden vectors and
recordings stay readable. That tolerance is not a live-device support claim. The
deployed fleet and physical release qualification use 0.9.10/schema 6; unknown
schemas and firmware older than 0.9.9 fail closed rather than selecting a
best-effort decoder.

## Physical validation for this release candidate

The release candidate was exercised on one left and one right glove running
firmware 0.9.10/schema 6 over USB on macOS. The measured default delivery was about
250 tactile packets/s, 500 IMU packets/s, and 125 magnetometer packets/s per hand,
with no capture-window sequence gaps, malformed frames, or host queue overflow in
the final checks.

The following live paths were exercised:

- discovery, logical identity, side, health, status, and calibration read-back
- tactile, IMU, and magnetometer streams independently
- stop, restart, repeated connect/stream/close, and disconnect handling
- two simultaneous gloves and a 60-second two-hand stream
- simultaneous two-hand recording and replay
- reversible raw/clean, threshold, tactile-rate, and IMU-rate changes with read-back
- `oglo doctor`

`zero()` was not run on physical hardware during automated release testing because
it requires a person wearing and moving the glove and overwrites the stored
calibration. Its command, completion, recipe parsing, and read-back transaction are
covered by firmware-accurate transport tests. Existing recipes were read without
mutation through `GET ZERO`.

## Limits of the claim

- BLE throughput was not qualified. Use USB for timing-sensitive or customer data.
- Automated Linux/macOS jobs validate packaging and hardware-free behavior; they do
  not replace a physical glove test on the target host.
- The two gloves do not share a hardware clock or trigger.
- A nominal 500 IMU packets/s is transport cadence, not proof of 500 fresh physical
  sensor measurements per second.
- Firmware 0.9.10 USB frames do not include an end-to-end payload CRC.
- Multi-hour recording, slow-storage stress, and device-clock rollover remain target
  deployment qualification items.

Run `oglo doctor` on every host/glove combination before collecting a dataset.
