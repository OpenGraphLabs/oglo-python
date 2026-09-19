# Candidate status

`0.1.0rc4` is prepared for SDK evaluation. The latest published release is still
`0.1.0rc3`. A candidate wheel, passing offline tests or a draft release must not be
described as a completed physical qualification.

## What changed in the SDK

- Capability-gated USB keepalive runs independently of stream reads.
- USB command writes time out; partial/failed writes invalidate the connection.
- Recordings detect silent streams within five seconds and preserve failed
  partial episodes, including their error and `complete=false` metadata.
- Closing an SDK-owned USB port explicitly lowers DTR to end its firmware
  recovery authorization, including when the host retains DTR on descriptor close.
- Single-glove acceptance, cooperative recording cancellation, RAW/CLEAN checks
  and firmware 0.9.16 retry-counter interpretation are covered by offline tests.
- Tactile orientation helpers preserve the original wire-order counts.

## Current physical limitation

Sustained USB reception failures were reproduced on firmware 0.9.16, including
tests that did not import the SDK. One device USB-driver FIFO-allocation defect
was confirmed. A private firmware intervention completed two five-minute traffic
tests, but that intervention is not a qualified firmware release and does not
establish long-duration SDK reliability. Installing this SDK does not modify or
repair the glove firmware.

The firmware return comparison, distributable firmware fix and target-host
recording qualification remain open. The 0.9.10 firmware floor is a protocol
compatibility check; it is not a reliability guarantee for every newer firmware.
Earlier two-hand measurements on 0.9.10 do not qualify a new 0.9.16 combination.

## Evaluate the prepared package

1. Verify the supplied `SHA256SUMS.txt` and source commit in the handoff manifest.
2. Install the candidate in a clean virtual environment:

   ```bash
   python3 -m venv .venv
   # macOS/Linux: source .venv/bin/activate
   # Windows PowerShell: .venv\Scripts\Activate.ps1
   python -m pip install ./oglo-0.1.0rc4-py3-none-any.whl
   python -c "import oglo; print(oglo.__version__)"
   oglo --help
   ```

3. Review the [quickstart](01_quickstart.md) and [recording format](04_recording.md).
   Reading examples and replaying supplied captures can be evaluated without
   claiming that live recording is qualified.
4. Once the compatible firmware and physical setup are ready, run the
   [acceptance checks](07_acceptance.md), then its 75-minute soak on the intended
   storage. Use `--single` for one glove; qualify both gloves together when the
   deployment needs two hands.

Offline CI covers Python 3.10–3.14 on Linux, macOS and Windows using installed
packages. It cannot establish USB timing, cable quality, actual sensor response or
target-disk reliability. BLE throughput, two-hand hardware synchronisation and
force calibration are outside this USB candidate's qualification.
