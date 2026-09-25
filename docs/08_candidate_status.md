# Candidate status

This is a development candidate, not a fleet deployment approval. It integrates
upstream JSONL recording with a bounded storage worker and opt-in signed firmware
preparation. See [managed updates](10_managed_firmware.md).

This checkout is `0.1.0rc8.dev2`, a build for evaluation. Find published packages on
[GitHub Releases](https://github.com/OpenGraphLabs/oglo-python/releases).
See the [changelog](../CHANGELOG.md) for changes and
[compatibility](06_compatibility.md) for supported versions.

The generic installer and compatibility-based setup are described in
[automatic firmware updates](10_managed_firmware.md); it requires no device list.
[Current validation](validation/2026-09-23-compatible-firmware.md) distinguishes
installed-device checks from simulated failure tests.

## Firmware status

The included firmware is the same signed 0.9.17 application as
`oglo-hardware` tag `fw/rdr02-tia/v0.9.17`. The
[cross-repository status](https://github.com/OpenGraphLabs/oglo-hardware/blob/main/docs/firmware-0.9.17-status.md)
separates the SDK, web test channel and factory rollout.

Earlier **rc7 NPZ** recordings with this firmware completed paired Linux/Mac
75-minute captures and a Pi recording with **8 h 59 min 33 s of continuous
overlap and zero observed sample loss**. The overnight first connection attempt
failed GET STATUS before recording; its strict nine-hour verdict remains FAIL.
See the [overnight results](validation/2026-09-22-overnight-results.md) and
[earlier experiment overview](validation/2026-09-22-experiment-overview.md).
These results do not qualify this checkout's newer JSONL recording/update path.

On 2026-09-25, a separate stock 0.9.16 R-00023 reproduced three USB receive
stalls without the SDK. The owner retained 0.9.16; no update or same-unit
0.9.17 comparison was performed. See [the controlled comparison](validation/2026-09-25-stock-0916-r00023.md).

The Mac bench unit L-00006 was updated through the USB application protocol from
its verified stock 0.9.16 image to the signed 0.9.17 image, with automatic reboot
and unchanged CONFIG/ZERO preservation fields. A repeat preparation skipped
writing. The initial readiness check incorrectly rejected transient TX retries;
that host-side interpretation was corrected to match the firmware and existing
recorder. Sequence loss and actual device drops remain failures.

A newly attached R-00025 also completed the migration using the installed common
package with no device list, policy or serial argument. Its 120.012-second
recording delivered tactile/IMU/magnetometer packets at 250.1/500.2/125.0 Hz with
zero counted loss, preserved CONFIG/ZERO and passed three software reopen cycles
without rewriting firmware. See the current validation report above for exact
image hashes and evidence scope.

These are short Mac tests. Linux/Zed application-update recovery, two-hand long
recording and the research fleet are not qualified by these results.
The earlier rc6 75-minute Linux sample-loss report remains a failed run; integrating
a storage worker into JSONL recording does not retroactively make it pass.

Test each new deployment on its own host, gloves, cables, and disk. Keep the
firmware version and [acceptance report](07_acceptance.md) with the results.

## Evaluate the package

For the published common package with automatic firmware preparation, run this
once in the Python environment used by your collection program (macOS/Linux):

```bash
curl -fsSL https://github.com/OpenGraphLabs/oglo-python/releases/download/v0.1.0rc8.dev2/install.py | python - --auto-firmware
```

The download/check/install steps below are an alternative for manually evaluating
CI artifacts; they do not enable automatic firmware preparation by themselves.

### 1. Download a build

Open a successful [CI run](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml)
for the commit you want to test. Under **Artifacts**, download
`oglo-<full-commit-sha>` and unzip it.

You need to sign in to GitHub. Artifacts expire after 90 days, so keep a local
copy. Older runs may not include the package-upload step.

```text
oglo-0.1.0rc8.dev2-py3-none-any.whl   SDK to install
oglo-0.1.0rc8.dev2.tar.gz            matching source, docs, and examples
handoff.json                  source commit and CI run
SHA256SUMS.txt                 file checksums
```

### 2. Check the files

In the unzipped directory, run the command for your system:

```bash
# Linux
sha256sum -c SHA256SUMS.txt

# macOS
shasum -a 256 -c SHA256SUMS.txt
```

On Windows PowerShell:

```powershell
Get-Content SHA256SUMS.txt | ForEach-Object {
    $expected, $file = $_ -split '  ', 2
    if ((Get-FileHash $file -Algorithm SHA256).Hash -ne $expected) {
        throw "Checksum mismatch: $file"
    }
}
```

All checks must pass. Check the commit in `handoff.json` too: different candidate
builds can have the same SDK version.

### 3. Install

```bash
python3 -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS/Linux, or
`.venv\Scripts\Activate.ps1` in Windows PowerShell. Then:

```bash
python -m pip install ./oglo-0.1.0rc8.dev2-py3-none-any.whl
python -c "import oglo; print(oglo.__version__)"
oglo --help
```

### 4. Open the matching examples

```bash
python -m tarfile -e oglo-0.1.0rc8.dev2.tar.gz .
cd oglo-0.1.0rc8.dev2
```

Follow the included README. Run examples from this extracted folder so they match
the installed wheel. [Replay](04_recording.md#reading-one-back) needs no hardware.

### 5. Test your setup

Run the [acceptance checks](07_acceptance.md). Use `--single` for one glove, and
test both together for a two-hand setup. Use the 75-minute test to check long
recording and device-clock rollover on the intended disk.

Passing software tests does not prove physical capture quality. BLE throughput,
hardware synchronization, and force calibration need separate validation.
