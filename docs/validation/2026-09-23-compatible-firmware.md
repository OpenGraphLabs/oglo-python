# Generic compatibility-based updater — 2026-09-23

SDK candidate: `0.1.0rc8.dev2`. This replaces mandatory per-device enrollment with
live USB identity discovery and the existing hardware/image migration checks.
No research-lab identifiers or per-unit allowlist are included in the common package.
The exact production-signed 0.9.17 image is bundled without modification.

The new installer installs the pinned wheel and explicitly enables preparation in
the invoking Python environment. Enabling never opens a device. Existing
`connect()` / `connect_pair()` calls discover attached gloves, check all selected
units before any write and retain physical locks through reboot and handoff.
Legacy policies remain usable for existing pending recovery; they are not required
for new installations.

## Observed checks

- Full local suite after the compatibility change: **658 passed, 1 skipped,
  11 hardware tests deselected** (Python 3.14, macOS). Additional final guards and
  installer regressions are checked separately and by the final GitHub matrix.
- Production signature and both application/runtime hashes verified from the
  bundled files. Wheel and sdist passed `twine check`; a clean wheel installation
  matched the Python modules and bundled resources. No bench policy, calibration
  or private inventory is shipped.
- Simulations cover arbitrary unregistered physical IDs, duplicate identities,
  missing/wrong-hand pairs, incompatible second-hand hardware/hash/protocol before
  any write, logical selection without updating other gloves, no-OUT recovery
  before discovery, retained USB leases, partial-worker-result rejection, saved
  history semantics and installer integrity/failure ordering.

## Mac installed-wheel check

A new Python environment installed the common wheel, enabled updates without a
policy file, then called ordinary `oglo.connect()` with **no serial argument**.
The attached **OGLO-L-00006 / USB 68EE8F4968F4** was discovered and verified. It was
already on the approved 0.9.17 image, so no firmware write occurred. Connection
preparation took **11.272 seconds**.

A subsequent **60.017-second** recording completed and replayed successfully:

| Stream | Samples | Delivered packets/s | Counted loss |
| --- | ---: | ---: | ---: |
| Tactile | 15,016 | 250.2 | 0 |
| IMU | 30,033 | 500.4 | 0 |
| Magnetometer | 7,508 | 125.1 | 0 |

Full CONFIG/ZERO preservation fields and the running-image identity were unchanged.
The archive reports complete with no error. These are packet rates, not fresh
sensor measurements or zero-latency claims. Raw evidence remains local in
`acceptance-results/generic-firmware-20260923/` and the per-device journal.

This recording preceded the final extra parent-side all-device-result guard and
settings-schema validation. Those do not alter the wire protocol. The final wheel
was reinstalled, matched byte-for-byte to the source/resources, and passed an
ordinary managed connection again with no policy file or serial selection. The
final focused connection/firmware/documentation suite passed **106 tests**.

The generated installer also passed in a fresh Mac Python environment using the
local wheel as its download source: wheel checksum, actual pip installation, exact
SDK replacement, bundled production-signature verification and persistent enablement.
The same end-to-end installer check is included in the Linux CI packaging job.
This local-source check is distinct from verification of the eventual public URL.

## Remaining physical scope

This generic discovery path has not yet performed a new **0.9.16 -> 0.9.17** flash
on real hardware. The earlier Mac test exercised the same transfer/reboot protocol
with an explicit policy; it is separate evidence, not a generic-path flash result.
Research Linux/Zed USB, real interruption/recovery, two-hand long-duration use and
fleet-wide qualification remain open. A successful software matrix or publishing
this development prerelease does not close those gates.
