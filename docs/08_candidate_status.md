# Candidate status

This checkout is `0.1.0rc4`, a build for evaluation. Find published packages on
[GitHub Releases](https://github.com/OpenGraphLabs/oglo-python/releases).
See the [changelog](../CHANGELOG.md) for changes and
[compatibility](06_compatibility.md) for supported versions.

## Firmware status

The team reports successful functionality tests on firmware 0.9.16. Previously
reported problems were resolved on those tested setups; the earlier USB incident
is historical context.

Test each new deployment on its own host, gloves, cables, and disk. Keep the
firmware version and [acceptance report](07_acceptance.md) with the results.

## Evaluate the package

### 1. Download a build

Open a successful [CI run](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml)
for the commit you want to test. Under **Artifacts**, download
`oglo-<full-commit-sha>` and unzip it.

You need to sign in to GitHub. Artifacts expire after 90 days, so keep a local
copy. Older runs may not include the package-upload step.

```text
oglo-0.1.0rc4-py3-none-any.whl   SDK to install
oglo-0.1.0rc4.tar.gz            matching source, docs, and examples
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
python -m pip install ./oglo-0.1.0rc4-py3-none-any.whl
python -c "import oglo; print(oglo.__version__)"
oglo --help
```

### 4. Open the matching examples

```bash
python -m tarfile -e oglo-0.1.0rc4.tar.gz .
cd oglo-0.1.0rc4
```

Follow the included README. Run examples from this extracted folder so they match
the installed wheel. [Replay](04_recording.md#reading-one-back) needs no hardware.

### 5. Test your setup

Run the [acceptance checks](07_acceptance.md). Use `--single` for one glove, and
test both together for a two-hand setup. Use the 75-minute test to check long
recording and device-clock rollover on the intended disk.

Passing software tests does not prove physical capture quality. BLE throughput,
hardware synchronization, and force calibration need separate validation.
