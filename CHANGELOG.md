# Changelog

All notable user-facing changes are recorded here. Versions follow
[Semantic Versioning](https://semver.org/) and Python package versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

## [0.1.0rc7] - Candidate preparation

- Move recording chunk writes and fsync off the glove reader into a bounded
  storage worker. Immutable copied blocks preserve rows when live arrays are
  reused. Backlog exhaustion fails promptly instead of blocking acquisition.
- Wait for pending writes before publishing complete metadata; storage errors
  leave an incomplete episode and stop the writer. Integrity limits are unchanged.
- Preserve the rc6 Linux pair 75-minute failure: 558/542 missing samples despite
  responsive gloves and zero capture-window USB completion errors. The rc7
  storage correction still requires physical qualification on Linux and Mac.

## [0.1.0rc6] - Candidate preparation

### Fixed

- Stop each acceptance recording's glove in its worker before waiting for the
  other hand or replaying the files. The public recorder resumes caller-owned
  gloves; leaving them active during analysis could overflow device queues and
  make the next recording fail its health check. Stop both on recording errors
  as well, retaining peer cancellation and all recording integrity checks.

### Qualification status

- This candidate fixes the acceptance runner's transition between recordings.
  Mac/Linux two-hand long-duration qualification remains pending. rc5 is retained
  as a separate, unpublished candidate with its failed pair report.

## [0.1.0rc5] - Candidate preparation

### Fixed

- Stop each acceptance stream in its collecting worker before analyzing samples.
  On the Pi, post-capture analysis could leave USB unread for about 266 ms while
  streaming continued, producing device queue drops after an otherwise continuous
  capture. Snapshot rates and loss counters before stopping, so actual losses
  still fail the report even when stop clears live session counters.
- Stop a stream when its acceptance collector raises, before leaving that worker.

### Qualification status

- This fixes the acceptance runner; it does not modify glove firmware or qualify
  Mac/Linux two-hand capture. rc4 remains a separate, unpublished candidate.

## [0.1.0rc4] - Candidate preparation

### Qualification status

- SDK/package preparation only: sustained USB capture on firmware 0.9.16 has a
  reproduced device-path failure; the candidate is not a qualified firmware fix
- expanded offline CI to Python 3.10–3.14 on Linux, macOS and Windows, testing
  installed distributions rather than importing the source tree

### Added

- `oglo acceptance --single` and `pytest --hardware-single` for explicit
  one-glove qualification, with two-hand checks reported as untested
- cooperative `record(stop_event=...)` cancellation so acceptance can stop its
  recording workers before closing connections; cancelled captures cannot pass a soak

- `oriented_counts()` and `Frame.oriented(info)`: the tactile grid in canonical
  thumb-first finger order with `col 0` at every fingertip, for either hand. The
  left thumb's flex runs its COL electrodes the other way along the finger, so its
  col axis is reversed there; raw `counts` stay in wire order

### Changed

- aligned current firmware guidance with the committed 0.9.16/schema-6 golden
  bundle, and distinguished checkout-only features from the published rc3 package

### Fixed

- stop the peer recording when either hand fails during acceptance, including a
  right-hand failure while the left-hand recorder is still running
- fail silent recordings after five seconds and preserve their incomplete data
  and error metadata; avoid unbounded USB drains after an endpoint fails
- explicitly lower DTR before closing SDK-owned USB ports, so Linux tty settings
  that retain DTR on close cannot leave firmware recovery authorized after exit
- reject commands queued behind a failed USB keepalive before they can append to
  a partial command line; regression tests cover timeout and short-write races
- qualify RAW as well as CLEAN starting states, restore the original RAW threshold
  after mutation tests, and distinguish recovered USB short writes from actual loss
- apply firmware 0.9.16 USB retry semantics consistently in doctor, recording,
  replay and acceptance while retaining counters and all actual-loss checks

- kept capability-advertising firmware's USB wedge recovery authorized with a
  bounded, reply-free `LINK PING` worker independent of stream-read cadence, while
  never sending the command to legacy or unadvertised firmware; firmware image
  writes remain isolated in the dedicated updater USB session

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

[Unreleased]: https://github.com/OpenGraphLabs/oglo-python/commits/main
[0.1.0rc6]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc5...v0.1.0rc6
[0.1.0rc5]: https://github.com/OpenGraphLabs/oglo-python/compare/425346d3d979db742a767c19fd133f3c49efb493...main
[0.1.0rc4]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc3...425346d3d979db742a767c19fd133f3c49efb493
[0.1.0rc3]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc2...v0.1.0rc3
[0.1.0rc2]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc1...v0.1.0rc2
[0.1.0rc1]: https://github.com/OpenGraphLabs/oglo-python/releases/tag/v0.1.0rc1

[0.1.0rc7]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc6...v0.1.0rc7
