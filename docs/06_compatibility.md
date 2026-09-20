# Compatibility and validation scope

This page separates protocol support, automated test coverage, and measurements on
physical gloves. They are different claims.

## Supported contract

| Component | Supported contract |
| --- | --- |
| Python | 3.10 or newer |
| Minimum supported firmware | 0.9.10 |
| Current committed golden firmware | 0.9.16; sustained USB issue remains open |
| CONFIG schema | exactly 6 |
| USB tagged stream | protocol supported; current firmware/host combination needs qualification |
| BLE schema-6 notifications | experimental; parser-tested, not release-qualified |
| Firmware older than 0.9.10 | rejected for connect, replay, and vector capture |

`0.1.0rc5` has one firmware floor: 0.9.10. Live devices, checked-in vectors, and
recorded episodes below that floor fail closed. Firmware 0.9.11 added a bounded
TinyUSB write path; 0.9.12 added signed USB application update, and 0.9.16 adds
host-authorized USB stall recovery while retaining the schema-6 tagged-stream layout. The SDK
enforces the firmware floor, schema and packet dimensions rather than an upper
firmware bound. The current committed golden bundle is 0.9.16 for Rev-D-TIA
hardware. Deployed schema-6 gloves at or above 0.9.10 remain compatible.

## Candidate versus published release

The current candidate is `0.1.0rc5`; the latest published release is `0.1.0rc3`.
The candidate includes tactile orientation helpers and the USB safeguards below.
Source, package preparation and hardware qualification are separate states; see
[candidate status](08_candidate_status.md).

For firmware 0.9.16 or newer advertising `"link_ping": true`, an SDK-owned USB
connection sends the reply-free `LINK PING` command once per second after a stream
starts. It continues across stream pauses until close, independently of reader
cadence. Legacy or unadvertised firmware and caller-owned handles do not opt in.
Firmware updates require the dedicated updater on its own USB session.

USB command writes have a bounded timeout and avoid unbounded OS drains. A failed
or partial command invalidates the connection so later writes cannot append to an
incomplete command. Recordings fail after a fitted stream remains silent for five
seconds and preserve any partial episode with its failure metadata. SDK-owned
ports explicitly lower DTR on close; caller-owned handles keep their DTR state.
These safeguards improve failure reporting and shutdown; they do not repair a
device USB endpoint that stops communicating.

Firmware 0.9.16 USB stalls were reproduced with and without the SDK on Mac and Pi
host/cable combinations. One Pi incident left sensor sampling and USB OUT alive
while USB IN stopped. A driver FIFO-allocation discrepancy was confirmed in the
running device and the delivered firmware binary. A private firmware intervention
completed two five-minute SDK-free tests that previously failed, but a production
firmware fix and long SDK capture remain unqualified. This result must not be
extrapolated to every observed Mac failure or every glove.

The current combination remains **NO-GO for qualified sustained capture**. The
historical results below apply to firmware 0.9.10 and do not qualify the candidate
on 0.9.16. Run the [acceptance procedure](07_acceptance.md) on the actual target
host, storage and glove configuration after the firmware issue is resolved.

## Historical physical validation on firmware 0.9.10

The earlier release candidate was exercised on one left and one right deployed glove running
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
- Automated Linux/macOS/Windows jobs validate packaging and hardware-free behavior; they do
  not replace a physical glove test on the target host.
- The two gloves do not share a hardware clock or trigger.
- A nominal 500 IMU packets/s is transport cadence, not proof of 500 fresh physical
  sensor measurements per second.
- Supported firmware USB frames do not include an end-to-end payload CRC.
- Multi-hour recording, slow-storage stress, and device-clock rollover remain target
  deployment qualification items.

Run `oglo doctor` on every host/glove combination before collecting a dataset.
