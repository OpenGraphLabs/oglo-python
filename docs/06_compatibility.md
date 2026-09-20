# Compatibility and validation scope

## Supported contract

| Component | Supported contract |
| --- | --- |
| Python | 3.10 or newer |
| Firmware | 0.9.10 or newer |
| CONFIG schema | exactly 6 |
| USB | Tagged schema-6 stream |
| BLE | Experimental schema-6 notifications |
| Current committed golden firmware | 0.9.16 for Rev-D-TIA; team functionality tests passed |

Live connections, replay, and vector capture reject firmware below 0.9.10 and
incompatible schemas or packet dimensions. There is no upper firmware bound.
Protocol compatibility does not establish recording reliability; see
[candidate status](08_candidate_status.md) for the current qualification limits.

## USB behavior

On firmware 0.9.16+ advertising `"link_ping": true`, SDK-owned connections send
`LINK PING` once per second after streaming starts. It runs independently of reads
and continues across stream pauses until close. Legacy firmware and caller-owned
handles do not opt in. Firmware updates require a separate updater session.

Command writes have a bounded timeout. A failed or partial write invalidates the
connection. Recordings detect five-second stream stalls and preserve partial data.
SDK-owned ports lower DTR on close; caller-owned handles keep their DTR state.
These safeguards report and contain failures; they do not repair device firmware.

## Historical physical validation

Firmware 0.9.10/schema 6 was exercised on one left/right USB pair on macOS.
Default delivery was approximately 250 tactile, 500 IMU, and 125 magnetometer
packets/s per hand, with no capture-window sequence gaps, malformed frames, or
host queue overflow in the final checks.

The checks covered discovery and identity, health and calibration readback, all
three streams, reconnects and disconnects, a 60-second two-hand stream,
simultaneous recording/replay, reversible stream/rate changes, and `oglo doctor`.
`zero()` was transport-tested but not physically run: it requires a person wearing
and moving the glove. These results do not qualify firmware 0.9.16.

## Validation limits

- Offline CI tests Python 3.10–3.14 on Linux, macOS, and Windows. It does not test
  physical USB timing, sensors, cables, or target storage.
- BLE throughput has not been qualified. Use USB for timing-sensitive capture.
- The gloves have independent clocks and no shared hardware trigger.
- IMU packet cadence is not proof of the same rate of fresh sensor measurements.
- Supported USB frames have no end-to-end payload CRC.
- Multi-hour capture, slow storage, and device-clock rollover require physical
  qualification on the intended setup.

Run `oglo doctor` before collection and the [acceptance procedure](07_acceptance.md)
when qualifying a deployment.
