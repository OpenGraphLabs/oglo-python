# Changelog

All notable user-facing changes are recorded here. Versions follow
[Semantic Versioning](https://semver.org/) and Python package versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

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

[Unreleased]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc1...HEAD
[0.1.0rc1]: https://github.com/OpenGraphLabs/oglo-python/releases/tag/v0.1.0rc1
