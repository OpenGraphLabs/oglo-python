# Managed firmware preparation (development candidate)

`0.1.0rc8.dev1` adds opt-in, offline application updates for **macOS and Linux**.
It is not yet approved for a 40-glove rollout. Physical application-update and
recovery qualification is recorded separately; unit-test success is not hardware
qualification. Windows retains normal SDK support but does not support this updater.

Install the reviewed wheel and its firmware extra once on the research host:

```sh
python -m pip install './oglo-0.1.0rc8.dev1-py3-none-any.whl[firmware]'
export OGLO_FIRMWARE_POLICY=/absolute/path/lab-firmware.json
```

With an explicitly installed lab policy, the existing `oglo.connect()` and
`oglo.connect_pair()` prepare their selected gloves before returning them. There is
no browser, login, confirmation prompt or firmware selection in the collection
flow. Without a policy, firmware writes remain disabled. `oglo info` and
`oglo doctor` bypass policy-driven updates. An unfinished update still blocks an
ordinary connection until preparation has verified it.

## What the lab administrator supplies

An offline directory containing `application.bin`, `manifest.txt`, and
`signature.der`, and one policy file. Build the directory from already signed
release artifacts using `tools/prepare_firmware_bundle.py --help`. This does not
sign or fetch firmware and needs no cloud/admin credential on the research host.
The SDK verifies ECDSA-P256/SHA-256 against its production public key, canonical
manifest, application file hash and embedded ESP image digest.

The first implementation accepts only the reviewed 0.9.16 -> 0.9.17 migration:

| Identity | SHA-256 |
| --- | --- |
| Allowed starting runtime | `b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897` |
| Target application file | `bcfdb9944e27bc38289d44edb6b1a6d6c3838244fa805723c8dd2ee3a8c4a3ad` |
| Target running image | `eddf0ca99dcd929e202464d2a9c311923e895bee95fd7aa0c5dd7ec013a01615` |

The file hash and running-image hash are different by design. There is no `latest`
lookup, downgrade, reinstall of the same version with a different hash, or firmware
update over BLE. Unknown images stop preparation.

Example policy below uses placeholder identities. Replace them with the verified
full logical and USB serials of the actual inventory before enabling deployment.
A logical serial alone is insufficient: binding the physical chip identity prevents
searching for one glove from changing another.

```json
{
  "schema": 1,
  "id": "lab-0917-v1",
  "enabled": true,
  "bundle": "./firmware-0917",
  "devices": [
    {"serial": "OGLO-L-EXAMPLE", "usb_serial": "AAAAAAAAAAAA", "side": "left"},
    {"serial": "OGLO-R-EXAMPLE", "usb_serial": "BBBBBBBBBBBB", "side": "right"}
  ]
}
```

For several pairs on one host, select the pair explicitly:

```python
left, right = oglo.connect_pair(
    serials=("OGLO-L-EXAMPLE", "OGLO-R-EXAMPLE"),
    firmware_policy="/absolute/path/lab-firmware.json",
)
```

Only selected policy devices are opened. Both hands must pass preflight before
writing either; transfer is sequential. If one fails, no usable pair is returned.
Already verified target images are not rewritten, including images installed on
another host. The current device is always queried again.

## Failure and preservation contract

- Identity, configuration and the complete `GET ZERO` response are saved before
  `FW BEGIN`. The original snapshot survives an interrupted attempt. The updater
  never clears NVS or recalibrates. Differences block capture and retain evidence.
- Preservation covers the fields exposed by CONFIG/ZERO, including identity,
  sample rate, stream settings, calibration lock and arrays. It does **not** claim
  to read or preserve an unexposed IMU-period setting or all flash contents.
- A durable `pending` marker is written before the first possible update write.
  Following failure or process/host restart, preparation acquires the same device
  lock and waits 65 seconds without opening/sending to pending devices. This is
  necessary because 0.9.16 consumes text ABORT as binary and incoming bytes extend
  its receive timeout. The wait is restarted from zero; wall-clock changes cannot
  shorten it. It does not repair a wedged USB endpoint.
- No automatic chunk retry or text ABORT follows ambiguous binary transmission.
  A final RECEIVED can establish completion when only its ACK was lost. A lost
  COMMIT response triggers quiet recovery and actual-image inspection, not another
  COMMIT or a blind success report.
- A subprocess owns USB I/O. Absolute phase deadlines cover open, transfer,
  response, reboot, verification and close. Timeout/cancellation terminates the
  process; inherited device locks remain held if an OS call cannot terminate.
  Progress does not extend a write-phase deadline. Callbacks must return promptly.
- Cooperative device locks use stable USB identity and remain held through reboot
  and handoff to capture. The SDK checks existing tty owners and requests OS tty
  exclusion. This does not establish universal protection against noncooperating
  drivers/apps or another OS user. Darwin PTYs did not enforce TIOCEXCL in testing;
  physical USB and Linux driver behavior need separate qualification.
- Post-update readiness checks the running hash, calibration/configuration, status
  and three seconds of tactile/IMU/magnetometer **packet delivery**, sequence gaps,
  head/tail gaps and counter changes. On these pinned firmware versions,
  `tag_short_writes` counts retained-frame retries and is recorded as diagnostic
  evidence; increases alone do not mean sample loss. Actual drops, host sequence
  loss, reset, unhealthy status and new deadline misses still fail readiness. It is not a fresh-sensor-value rate test,
  force calibration or long-duration stability qualification. Its provisional
  delivery floor is 80% of configured tactile / 500 IMU / 125 magnetometer packets
  per second, with no gap above 0.5 s or counted loss. These are readiness limits,
  not a claim of exact 250/500/125 Hz performance.

A `needs_attention` journal is never reset automatically. Preserve it and its
original snapshot for investigation; do not delete it just to get past the check.
After a completed update, intentional setting changes become the baseline at the
next normal preparation rather than being mistaken for update corruption.

Normal streaming keeps its existing prohibition on `FW` commands. Firmware
preparation uses its own serial session; streaming keepalives cannot enter its
binary payload.

## Batch use and evidence

```sh
oglo firmware prepare --policy lab-firmware.json
oglo firmware prepare --policy lab-firmware.json --watch
oglo firmware inventory --policy lab-firmware.json > inventory.json
```

One-shot mode prepares connected approved gloves. Watch mode tries each physical
attachment once; after a failure unplug/replug that glove before retrying. It does
not repeatedly write to an unresponsive device. Ctrl+C stops it. Single-glove
batches are allowed; normal `connect_pair()` still requires both selected hands.

Inventory includes every policy device, including units never connected. It is
explicitly saved history, not a live-health claim. Two successes never imply that
40 devices completed. Cross-host inventory merge is not implemented in this
candidate; retain each host's export and reconcile by both identities.

Default state directory is `$XDG_STATE_HOME/oglo` or `~/.local/state/oglo` on
macOS/Linux; `OGLO_STATE_DIR` overrides it. All cooperating SDK processes for the
same user must use the same state directory. Do not use isolated state directories
for concurrent connections to the same gloves.

The directory holds device lock files, recovery journals, and per-attempt request,
worker stderr and progress/result JSONL. Do not remove lock files while processes
are active. Recordings made through managed preparation preserve the observed
runtime image hash, USB identity, policy hash/ID, verification time, attempt ID and
SDK version; replay retains that metadata.

Remaining release gates: real original-0.9.16 application update/automatic reboot,
real interruption/recovery and port conflict, Mac and research Ubuntu/Zed-host
installation, target-pair continuous recording, then the complete 40-device ledger.
The existing web updater and already deployed firmware are not modified by this
SDK change.
