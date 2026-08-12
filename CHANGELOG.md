# Changelog

All notable user-facing changes are recorded here. Versions follow
[Semantic Versioning](https://semver.org/) and Python package versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

## [0.1.0rc4] - 2026-08-12

### Added

- negotiated TAG v2 support with a native u64 device timestamp, distinct wire
  magic, CRC-32 frame integrity, and exact boot-scoped start acknowledgement
- recording and replay metadata for negotiated TAG version, firmware capability,
  and boot identity
- contract vectors and boundary tests covering fragmented acknowledgements,
  malformed frames, multiple u32 epochs, reconnects, and reboot boundaries

### Changed

- preserved TAG v1 automatically for firmware 0.9.10 through 0.9.12 and select
  TAG v2 only when the device explicitly advertises it
- documented 0.9.12 as the current signed fleet image while keeping 0.9.13 TAG v2
  physical qualification as a release gate

### Fixed

- fail closed when a TAG v2 start acknowledgement is missing, malformed, or
  belongs to a different boot instead of silently accepting an ambiguous stream
- reject a TAG v2 frame whose header, payload, or CRC trailer is corrupt and
  resynchronize at a later valid frame while leaving TAG v1 bytes unchanged
- stop and seal an incomplete episode when any fitted sensor stream makes no
  progress for three seconds, including an open serial handle returning only
  empty reads

## [0.1.0rc3] - 2026-08-09

### Changed

- raised the single firmware floor to 0.9.10 for live connections, replay, hardware
  vector capture, and acceptance while retaining schema 6 and accepting current
  firmware 0.9.11
- replaced the checked-in physical tagged-stream vectors with a redacted firmware
  0.9.10 capture and removed the unsupported legacy interleaved BIN capture
- synchronized README, quickstart, data, calibration, recording, troubleshooting,
  compatibility, acceptance, contribution, and security documentation with the
  current 0.9.10+ contract and 0.9.11 golden firmware

### Fixed

- made acceptance compare firmware numerically against a minimum so 0.9.11 does
  not fail an exact-0.9.10 check

## [0.1.0rc2] - 2026-08-09

### Added

- `oglo acceptance`, an owner-facing USB pair test that uses public SDK APIs,
  separates read-only, interactive, reversible mutation, zero, and long-soak gates,
  and writes non-overwriting Markdown/JSON evidence bundles

### Changed

- consolidated development, issues, tags, and releases in the public
  `OpenGraphLabs/oglo-python` repository; private and staging repositories are no
  longer active upstreams
- documented firmware 0.9.10/schema 6 as the only supported live-glove baseline;
  retained 0.9.9/schema-6 captures solely as historical decoder provenance
- removed the unused `pair_id` contract and `allow_unpaired` escape; two-glove
  connection now relies on one left side, one right side, and distinct logical
  serials

## [0.1.0rc1] - 2026-08-07

First public release candidate.

### Added

- USB and experimental BLE discovery for firmware 0.9.9+/schema 6
- verified logical-serial, side, and pair selection
- typed tactile, IMU, and magnetometer streams
- sequence, timestamp, malformed-packet, and queue-overflow accounting
- bounded-memory recording and hardware-free replay
- explicit zero, raw/clean, threshold, and stream-rate controls
- `oglo doctor`, captured golden vectors, and opt-in live-hardware tests

### Fixed

- keep USB CDC alive briefly after stopping a stream so firmware can drain its
  response before the host closes the port
- drain the buffered USB tail once at the duration boundary so host scheduling
  jitter cannot fabricate an all-modality freshness failure
- fail closed on truncated, malformed, incompatible, duplicated, or ambiguous data

### Known limitations

- BLE is experimental and its throughput is not release-qualified
- two gloves are not hardware-synchronised
- firmware 0.9.9/0.9.10 does not provide an end-to-end USB payload CRC
- zero persistence requires a power-cycle read-back when it is a release gate
- multi-hour and slow-storage target-host qualification remain deployment tasks

[Unreleased]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc4...HEAD
[0.1.0rc4]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc3...v0.1.0rc4
[0.1.0rc3]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc2...v0.1.0rc3
[0.1.0rc2]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc1...v0.1.0rc2
[0.1.0rc1]: https://github.com/OpenGraphLabs/oglo-python/releases/tag/v0.1.0rc1
