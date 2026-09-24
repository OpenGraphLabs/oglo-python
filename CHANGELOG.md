# Changelog

User-facing changes by version. Version numbers follow
[Semantic Versioning](https://semver.org/) and [PEP 440](https://peps.python.org/pep-0440/).

## 0.1.0rc8.dev2 - Generic firmware preparation

- Discover attached devices and decide updates by hardware, update contract and
  exact running image; no lab-specific package or device registration is required.
- Include the production-signed 0.9.17 application in the common wheel and sdist.
- Add an exact-wheel-checksum release installer and environment-scoped
  `firmware enable`, `disable`, and `status` commands. Normal SDK installation
  remains read-only with respect to firmware until explicitly enabled.
- Preserve isolated I/O deadlines, no-OUT recovery, full-pair preflight, device
  ownership and calibration checks through generic discovery and capture handoff.
- Inventory describes observed devices only and never implies fleet completeness.

## 0.1.0rc8.dev1 - Development candidate

- Opt-in offline firmware policy, signed application update, stable device locks,
  durable recovery, bounded USB worker, identity/hash/calibration verification,
  batch preparation and saved fleet inventory on Mac/Linux.
- Preserve verified firmware/policy identity in JSONL recordings and replay.
- Expire delivered-rate estimates when samples stop and promptly cancel a peer
  recording when the two-hand example fails.
- Integrate current backend JSONL format with bounded asynchronous chunk writes;
  blocked storage fails explicitly and cannot publish a complete episode.
- Mac L-00006 application update/reboot/readiness exercised; fleet and two-hand
  long-duration qualification remain separate gates. Web updater is unchanged.


## [Unreleased]

### Added

- OGLO Studio for paired-glove and camera collection, calibration, review, and
  checked source ZIP export.
- `oglo.collection.Collection` for the same workflow from Python without the
  web server, with separate `collection` and `studio` install extras.
- Experimental native Linux OVISION stereo, camera IMU, exposure timing, and
  calibration capture for the OG Center sensor-source profile; physical Linux
  hardware validation remains pending.
- `oglo.studio_realsense.RealSenseCameraWorker`: an SDK worker for the Intel
  RealSense D455 on Ubuntu, following the OVISION worker's pattern. Records
  color video plus the camera's own accelerometer and gyroscope on the
  camera's clock, and writes the unit's factory calibration.
- OGLO Studio lists a RealSense D455 camera choice (color + camera IMU) and
  offers it for **Portable source archive** or **Annotation handoff**; it is
  not an OG Center sensor source.
- `examples/camera_glove/realsense.py` (one session, `--check`) and
  `collect.py --camera-backend realsense` (many episodes, one worker kept open
  for the session) share the same worker, matching the OVISION examples.
- `examples/camera_glove/REALSENSE.md`: a partner-facing setup and recording
  guide, plus documentation updates across `docs/09`, `docs/10`, `docs/11`,
  and `docs/13`. Not yet tested on hardware.
- JSONL-only recording and replay using the `og-skill` sensor format and filenames.
- Automatic calibration read-back, RAW preservation, and CLEAN derivation.
- Exact sequence, loss, and timing metadata in each row's `oglo` object.
- Strict checks for malformed rows, truncated files, and RAW/CLEAN disagreement.
- `OGLData` types and a reference for collection files, fields, units, and shapes.
- `examples/camera_glove/collect.py`: multi-episode collection with keyboard / foot
  switch control, per-task numbered sessions, discard and failure folders, and
  `dataset.py index` / `upload` for the episode index, dataset card and Hugging Face
  upload. `scripts/collect.sh`, `scripts/doctor.sh`, `scripts/hf_upload.sh` run them
  with the interpreter named in `scripts/workstation.env`.
- `examples/05_taxel_map.py` / `scripts/taxel_map.sh`: live terminal taxel map in the
  OGLO Studio layout, for checking where a press lands.
- `collect.py`: `g` is refused until every glove holds a valid sweep zero, since a
  RAW episode without one has no CLEAN file; the grids show `max(0, raw - zero)` on a
  RAW stream, the same clamp the CLEAN file gets, since the sweep zero is an envelope
  and a resting hand sits below it; `c` toggles the on-screen grids between that and
  raw ADC without changing what is recorded.
- `collect.py --camera` accepts part of the camera's V4L2 name (`SC233`) and resolves
  it to the capture node, since `/dev/video` numbers change across reboots;
  `scripts/collect.sh` passes the name from `scripts/workstation.env`.
- `collect.py --camera-backend auto|opencv|ovision`: on an OVISION-EGO-V1 (SC233HGS
  with H.264/YCTC firmware) with SyncField 0.8.14 installed, episodes are recorded
  through the native backend of `ovision.py`, one live stream for the whole session:
  original 3840x1080 H.264, camera IMU and magnetometer, exposure timing and per-unit
  calibration in every episode, carried through the manifest and the upload. `auto`
  says why when it falls back to OpenCV. `episodes.jsonl` gains `camera_kind` and
  `camera_imu`, and the dataset card describes the camera actually used.
- One publication gate for the collection tree: `dataset.publishable` (complete
  manifest, files present, `alignment.preview.jsonl` with one row per decoded frame)
  decides what `collect.py` reports as saved, what `episodes.jsonl` lists and what
  `dataset.py upload` may send; `align.py` writes its file atomically; the upload
  refuses folders that are not publishable episodes and files outside the indexed
  ones. The card has no timestamp and states the frame rate actually recorded.
- `collect.py` task folders: tasks in another script get a hash suffix instead of
  merging into `session/`, and a folder that already holds another wording of a task
  is refused; a capture failure after `x` is recorded as failed, not discarded; the
  alignment worker is shut down explicitly when collection ends.
- Workstation choices (interpreter, camera name, GPU codec, Hub repo) moved from the
  scripts and defaults into `scripts/workstation.env` (`workstation.env.example`);
  `collect.py --codec` defaults to `mp4v`, `dataset.py upload` takes the repo from
  `--repo` or `OGLO_HF_REPO`. The camera CI job now runs `test_collect.py` and
  `test_dataset.py` with its no-skip check (and installs `ffmpeg` for the `--codec`
  test); the timed capture test stops on a frame.
- `ovision.py` records through the SDK's own `oglo.studio_ovision.NativeOvisionCameraWorker`
  (what OGLO Studio uses) instead of driving the SyncField stream itself: the worker
  keeps the stream connected between episodes, ends a recording whose capture dies or
  delivers no frame for five seconds, and releases the camera when it fails to start.
  `collect.py` reopens the camera after such a failure, or when it stops while idle,
  waiting for a replug and resolving `--camera` by name again; a stop before the first
  keyframe is a discard, not a failure. `--camera-serial` is now a manifest label only.
- `collect.py` runs `align.py` in a separate low-priority process (in-process alignment
  starved the glove serial ports and the camera during the next episode); Ctrl-C during
  the final wait leaves the remaining episodes complete and unaligned, and the next run
  aligns them first. Every shutdown step (devices, alignment, window, index) runs
  whatever the previous one did. Episode numbers are never reused (`<task>/.next_session`),
  long or symbol-only task names get a hash, a folder another collector created is
  not filed as a failure, and the OpenCV camera's FPS request is made once, at open.
- `capture.py` stops a glove whose recording failed instead of leaving it streaming
  unread, opens the encoder before the timed loop, drains the frames the shared camera
  buffered while the gloves were commanded, and keeps the window alive while the saved
  video is decoded back. `align.py` publishes its file exclusively (a unique temporary,
  fsync, hard link) so two processes cannot overwrite each other.
- `dataset.py`: the publication gate checks every camera file the manifest names (the
  OVISION IMU, calibration, clock anchor and report included) and `camera_imu` follows
  the file; the upload sends only the indexed episodes and `gloves/` (`--include` per
  episode), then the index in a second commit, requires `hf` 1.0+ and refuses an
  existing public repo; hidden folders are not tasks, symbolic links and unreadable
  manifests are held with their reason, a hand-written `README.md` is not overwritten,
  and the root files are replaced atomically. The card states the measured frame rate
  per camera kind, the OpenCV episodes' actual size and codec, the alignment tolerance
  used, and reads gloves by side in its example. `z` leaves the glove in RAW (as OGLO
  Studio does) and the help and README say so.

### Fixed

- The finger grids drawn by `collect.py` were transposed and mirrored relative to
  OGLO Studio. They now follow Studio's `drawGlove`: fingertip at the top, wire row 0
  on the right, left hand mirrored.

### Changed

- macOS OVISION recording now saves both eyes while previewing one selected eye.
- Added user guides for Studio setup, collection scripts, delivery profiles,
  and local troubleshooting.
- `capture.py` stops each glove's stream as soon as its recording returns instead
  of leaving it running unread while the peer finishes and the video is checked.
- Recording format is now schema 3; camera sessions use `oglo-camera-example.v2`.
  Earlier recording formats and the separate export step are no longer supported.
- Simplified the quickstart, camera guides, reference pages, and contributor docs.
- Removed unused internal state and old timestamp-rebasing code; shared timestamp
  defaults without changing public sample constructors.
- Clarified BLE rates, RAW/CLEAN values, and saved calibration information.
- Corrected axis-measurement help and linked historical measurements.
- Candidate CI packages now include the matching wheel, source/examples, commit
  details, and checksums. Camera CI requires its tests to run without skips.
- Updated firmware guidance to reflect the team's successful 0.9.16 tests.


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

### Validation status at preparation

A sustained USB failure on firmware 0.9.16 was reproduced during candidate work.
This entry records that earlier state; see [candidate status](docs/08_candidate_status.md)
for the subsequent team report. SDK test results alone do not qualify a firmware fix.

Offline CI was expanded to Python 3.10–3.14 on Linux, macOS, and Windows, using
installed packages rather than source imports.

### Added

- `oglo acceptance --single` and `pytest --hardware-single` for one-glove tests.
- `record(stop_event=...)` for stopping and saving recordings cleanly. Cancelled
  captures do not count as a passed duration test.
- `oriented_counts()` and `Frame.oriented(info)` for thumb-first ordering with
  column 0 at every fingertip. Original counts remain unchanged.

### Changed

- Aligned firmware guidance with the 0.9.16/schema-6 reference build.
- Distinguished checkout features from the published rc3 package.

### Fixed

- Stop the other hand's recorder when one hand fails during acceptance.
- Fail five-second stream stalls and preserve incomplete recordings.
- Lower DTR when closing SDK-owned ports so recovery permission cannot remain active.
- Reject commands after a failed USB keepalive, before they can append to a
  partial command. Added timeout and short-write race tests.
- Test RAW and CLEAN starting modes and restore the original RAW threshold.
- Treat firmware 0.9.16 short-write retries separately from actual data loss.
- Apply those retry rules consistently in doctor, recording, replay, and acceptance.
- Send `LINK PING` independently of reads only when firmware advertises support.
  Keep firmware updates on a separate updater connection.

## [0.1.0rc3] - 2026-08-09

### Changed

- Raised the minimum firmware version to 0.9.10 for live use, replay, test-packet
  capture, and acceptance. Kept schema 6 support, including firmware 0.9.11.
- Replaced physical test packets with a redacted 0.9.10 capture and removed the
  unsupported legacy interleaved BIN packet.
- Updated the guides for the 0.9.10+ requirement and the then-current 0.9.11 build.

### Fixed

- Compare firmware versions numerically so 0.9.11 passes the 0.9.10 minimum check.

## [0.1.0rc2] - 2026-08-09

### Added

- `oglo acceptance`: public-API USB tests, optional physical/setting/calibration
  checks, long recordings, and new Markdown/JSON reports for each run.

### Changed

- Made `OpenGraphLabs/oglo-python` the single source, issue, and release repository.
- Required firmware 0.9.10/schema 6 for live gloves. Kept 0.9.9 captures only as
  historical decoder evidence at this version.
- Removed `pair_id` and `allow_unpaired`. A pair now requires opposite sides and
  distinct configured serials.

## [0.1.0rc1] - 2026-08-07

First public release candidate.

### Added

- USB and experimental BLE discovery for firmware 0.9.9+/schema 6.
- Verified serial, side, and pair selection.
- Typed tactile, IMU, and magnetometer streams.
- Counters for sequence gaps, timestamps, malformed packets, and queue overflow.
- Recording with fixed-size memory buffers and replay without hardware.
- Explicit zero, RAW/CLEAN, threshold, and rate controls.
- `oglo doctor`, captured test packets, and opt-in hardware tests.

### Fixed

- Keep USB CDC open briefly after stopping so firmware can finish sending replies.
- Read the queued USB tail at the duration boundary before checking stream freshness.
- Reject truncated, malformed, incompatible, duplicated, or ambiguous data.

### Known limitations at release

- BLE throughput was not qualified.
- Gloves were not hardware-synchronized.
- Firmware 0.9.9/0.9.10 lacked an end-to-end USB payload checksum.
- Calibration persistence needed a physical power-cycle check.
- Long recordings and slow storage needed testing on the target host.

[Unreleased]: https://github.com/OpenGraphLabs/oglo-python/commits/main
[0.1.0rc6]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc5...v0.1.0rc6
[0.1.0rc5]: https://github.com/OpenGraphLabs/oglo-python/compare/425346d3d979db742a767c19fd133f3c49efb493...main
[0.1.0rc4]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc3...425346d3d979db742a767c19fd133f3c49efb493
[0.1.0rc3]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc2...v0.1.0rc3
[0.1.0rc2]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc1...v0.1.0rc2
[0.1.0rc1]: https://github.com/OpenGraphLabs/oglo-python/releases/tag/v0.1.0rc1

[0.1.0rc7]: https://github.com/OpenGraphLabs/oglo-python/compare/v0.1.0rc6...v0.1.0rc7
