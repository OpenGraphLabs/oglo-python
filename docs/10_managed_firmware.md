# Automatic firmware updates

The SDK uses **hardware and firmware compatibility**, not a lab-specific package
or a list of glove numbers. The common wheel includes the signed 0.9.17 application
and verifies its production signature and hashes before opening a device. There
is no firmware download, browser, login, serial registration or JSON editing during
normal use. Serial numbers identify the physical device and its saved evidence;
they do not decide which research lab is allowed to update it.

This is a development prerelease. The prior [Mac bench validation](validation/2026-09-23-managed-firmware.md)
covered one glove. Software CI is not physical Linux/pair/long-duration qualification.
See the [generic updater validation](validation/2026-09-23-compatible-firmware.md)
for the current measured scope.

## Install once

Use **the Python environment that runs your collection program**. The public release
installer verifies the exact wheel checksum, installs the common SDK and firmware
extra, then enables automatic updates in that environment:

```sh
curl -fsSL https://github.com/OpenGraphLabs/oglo-python/releases/download/v0.1.0rc8.dev2/install.py | python - --auto-firmware
```

The command requires Python 3.10+, pip, HTTPS access to GitHub and the package index,
and macOS or Linux. Activate your existing project environment first. Installing in
an unrelated Python environment does not upgrade your collection program. Linux USB
permissions must already allow that user to open the glove; the installer does not
change system permissions or install as root. No glove needs to be connected during
installation and installation itself never flashes a glove.

For an offline wheel or an existing SDK install:

```sh
python -m pip install './oglo-0.1.0rc8.dev2-py3-none-any.whl[firmware]'
python -m oglo firmware enable
```

`enable` validates the bundled firmware and saves a setting for the current user
and Python environment. It does not require a device inventory or environment
variable. A plain SDK install leaves automatic firmware writes off.

```sh
python -m oglo firmware status
python -m oglo firmware disable
```

## Use your existing collection code

```python
import oglo
left, right = oglo.connect_pair()
```

Before returning the gloves, the SDK discovers their identities, locks their USB
connections, checks both selected devices, updates them sequentially if needed,
waits for automatic reboot and verifies the running image, preserved settings and
calibration, and basic packet delivery. Progress appears on stderr. If either hand
fails, no usable pair is returned. This preparation takes time even when the update
is skipped; it is not an instantaneous connection.

A new compatible glove is handled when it is first used. All 40 devices do not need
to be attached at installation. Already updated gloves are checked without rewriting
the image. Multiple attached pairs require explicit selection:

```python
left, right = oglo.connect_pair(serials=("OGLO-L-00001", "OGLO-R-00001"))
```

These serials select a pair for that call; they are not an update allowlist.
For one call, `firmware_policy=True` enables the same bundled compatibility rules;
`firmware_policy=False` disables preparation for that call, including recursive
connections. `oglo info` and `oglo doctor` never initiate firmware updates. An
unfinished firmware journal still blocks ordinary capture until it is resolved.
Windows retains normal SDK support, but automatic firmware updates are not supported.

## Compatibility and failure handling

The bundled migration is limited to **RDR02_FLEX5_REV_D_TIA**, schema 6, application
update protocol 1, the production signing key and the following exact images:

| Image | SHA-256 |
| --- | --- |
| Allowed 0.9.16 running image | `b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897` |
| Target 0.9.17 application file | `bcfdb9944e27bc38289d44edb6b1a6d6c3838244fa805723c8dd2ee3a8c4a3ad` |
| Target 0.9.17 running image | `eddf0ca99dcd929e202464d2a9c311923e895bee95fd7aa0c5dd7ec013a01615` |

Unknown hardware, source hashes, update contracts or same-version alternative
images are rejected before firmware writes. There is no `latest` lookup, downgrade,
BLE update or assumption that a version string identifies the executable image.

- All selected devices pass preflight before the first update starts. Logical/USB
  identity mismatches and ambiguous pairs fail. A selected logical serial may require
  briefly reading CONFIG from other attached OGLO candidates to identify the target;
  no firmware is written to those other candidates.
- CONFIG and the full GET ZERO response are backed up before BEGIN and compared
  after reboot. No NVS erase or recalibration occurs. Preservation covers exposed
  CONFIG/ZERO fields, not every byte of flash or unexposed settings.
- Interrupted updates keep a durable original backup. Recovery waits 65 seconds
  without opening the port before discovery or other commands, because 0.9.16 can
  interpret text commands as binary update data. There are no blind chunk retries,
  repeated COMMIT or text ABORT commands after ambiguous writes. This wait does not
  repair a wedged USB endpoint; physical reconnection may still be necessary.
- USB I/O runs in a subprocess with absolute deadlines. Physical device leases
  remain held across reboot and handoff; unrelated sessions cannot borrow them.
  OS tty exclusion is best effort against noncooperating drivers or other users.
- Readiness checks three seconds of **packet delivery**, gaps and counters. It does
  not measure fresh sensor-value rates, force accuracy or long-term reliability.
  Retained-frame `tag_short_writes` retries are diagnostic, while actual dropped
  frames, sequence loss, new deadline misses and unhealthy status fail readiness.
- A `needs_attention` preservation failure is never automatically cleared. Keep
  the original journal for investigation; do not delete it to bypass a failure.

## Explicit preparation and saved observations

```sh
python -m oglo firmware prepare
python -m oglo firmware prepare --watch
python -m oglo firmware inventory
```

Explicit preparation works without enabling future automatic updates. Watch mode
handles newly attached USB devices individually and tries each attachment once.
Replug a failed glove before retrying. Use `--serial` with one-shot preparation;
compatibility watch discovers all attached gloves and does not accept `--serial`.

Inventory contains **observed devices only**, not a complete fleet manifest or live
health result. An empty history is not success, and two successes do not imply that
40 devices completed. Exports from compatible rules can be reconciled using
`firmware inventory --merge other-host.json`; the newest observation wins, including
failures. Imported history never changes trusted recovery state or bypasses the next
live check.

State lives in `$OGLO_STATE_DIR`, otherwise `$XDG_STATE_HOME/oglo` or
`~/.local/state/oglo` on Mac/Linux. Enabling is scoped to `sys.prefix`; device locks
and journals remain shared across environments of the same user. Do not give
concurrent applications separate state roots. Evidence includes phase logs, original
snapshots and final results. Recordings retain actual running hash, physical identity,
compatibility-rule hash, SDK version and verification timestamp.

Legacy `--policy` / `OGLO_FIRMWARE_POLICY` device lists remain readable for existing
controlled deployments and recovery. They are optional and are not created by the
installer. Unset `OGLO_FIRMWARE_POLICY` before enabling the generic path; an old
pending update may need its original policy to finish recovery first.
