# Compatibility and validation scope

This page separates protocol support, automated test coverage, and measurements on
physical gloves. They are different claims.

## Supported contract

| Component | Status in 0.1.0rc3 |
| --- | --- |
| Python | 3.10 or newer |
| Minimum supported firmware | 0.9.10 |
| Current golden firmware for new flashes | 0.9.12 |
| CONFIG schema | exactly 6 |
| USB tagged stream | supported and hardware-validated |
| BLE schema-6 notifications | experimental; parser-tested, not release-qualified |
| Firmware older than 0.9.10 | rejected for connect, replay, and vector capture |

`0.1.0rc3` has one firmware floor: 0.9.10. Live devices, checked-in vectors, and
recorded episodes below that floor fail closed. Firmware 0.9.11 added a bounded
TinyUSB write path and 0.9.12 preserves it while adding signed USB application
update; both keep schema 6 and the identical SDK wire contract, so the SDK has no
upper firmware bound. 0.9.12 is the current image for new flashes. Deployed
0.9.10 and 0.9.11 gloves remain compatible.

## Physical validation for this release candidate

The release candidate was exercised on one left and one right deployed glove running
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
- Supported firmware USB frames do not include an end-to-end payload CRC.
- Multi-hour recording, slow-storage stress, and device-clock rollover remain target
  deployment qualification items.

Run `oglo doctor` on every host/glove combination before collecting a dataset.
