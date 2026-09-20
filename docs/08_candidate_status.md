# Candidate status

`0.1.0rc4` is prepared for SDK evaluation; the latest published release is
`0.1.0rc3`. See the [changelog](../CHANGELOG.md) for SDK changes and
[compatibility](06_compatibility.md) for the supported contract.

## Firmware status

The team reports successful functionality testing on firmware 0.9.16 and confirms
that the previously reported problems are resolved on its tested setups. The
earlier USB incident is historical context, not a current release blocker.

For a new deployment, retain the firmware version and acceptance report for its
host, gloves, and storage. The [acceptance guide](07_acceptance.md) includes a
75-minute soak for checking device-clock rollover and long recordings.

## Evaluate the package

1. Open the [CI runs](https://github.com/OpenGraphLabs/oglo-python/actions/workflows/ci.yml)
   and select a successful run for the commit you intend to evaluate. In its
   **Artifacts** section, download `oglo-<full-commit-sha>` and unzip it. This
   handoff is produced by runs containing the candidate-upload workflow, after
   the SDK, minimum-dependency, and camera tests pass. Older runs will not have it.
   GitHub sign-in is required to download CI artifacts. They are retained for
   90 days; keep the downloaded bundle with your project. For a public release,
   maintainers can attach the same bundle to
   [GitHub Releases](https://github.com/OpenGraphLabs/oglo-python/releases).

   The bundle contains:

   ```text
   oglo-0.1.0rc4-py3-none-any.whl   installable SDK
   oglo-0.1.0rc4.tar.gz            matching source, docs, tests, and examples
   handoff.json                  SDK version, exact source commit, CI run URL
   SHA256SUMS.txt                 checksums for the three files above
   ```

2. From the unpacked directory, verify the checksums before installation:

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

   Check that `handoff.json` names the expected source commit and CI run. The SDK
   version alone does not distinguish two candidate builds from different commits.

3. Install the candidate wheel in a clean environment:

   ```bash
   python3 -m venv .venv
   # macOS/Linux: source .venv/bin/activate
   # Windows PowerShell: .venv\Scripts\Activate.ps1
   python -m pip install ./oglo-0.1.0rc4-py3-none-any.whl
   python -c "import oglo; print(oglo.__version__)"
   oglo --help
   ```

4. Extract the matching source archive to use its documentation and examples:

   ```bash
   python -m tarfile -e oglo-0.1.0rc4.tar.gz .
   cd oglo-0.1.0rc4
   ```

   Follow its README and [recording guide](04_recording.md). Run camera examples
   from this extracted directory so their version matches the installed wheel.
   Replay can be evaluated without hardware.
5. For a new hardware deployment, run the [acceptance checks](07_acceptance.md)
   on the intended setup. Use `--single` for one glove; test both together for
   two-hand deployments.

A candidate package, passing offline tests, or a draft release does not establish
physical qualification. BLE throughput, hardware synchronisation, and force
calibration are outside this USB candidate's scope.
