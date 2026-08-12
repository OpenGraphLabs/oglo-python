# 0.9.13 release HIL and 72-hour soak

`oglo hil` is the observation-only release gate for one specifically named left/right
pair. It does **not** contain a flash command, does not replace calibration and does
not select whichever two USB ports happen to enumerate first.

The updater/factory station must install the candidate first. The HIL gate then
requires both CONFIG identities and the exact candidate version before it toggles
modem lines or starts a stream. A 0.9.10 or 0.9.12 unit therefore fails preflight with
an instruction to flash it separately; the runner does not silently change it.

## 1. Prove the command without opening USB

```bash
oglo hil \
  --left OGLO-L-00028 \
  --right OGLO-R-00028 \
  --firmware 0.9.13 \
  --output hil-results \
  --dry-run
```

The dry run validates the logical serial formats and binds the SDK parser to the
canonical `spec/TAG_V2.json` vectors. It writes the exact planned steps, JSON report,
Markdown report and SHA-256 manifest while opening no serial port.

## 2. Run the bounded bench gate

After installing the same 0.9.13 candidate on both named units:

```bash
oglo hil \
  --left OGLO-L-00028 \
  --right OGLO-R-00028 \
  --firmware 0.9.13 \
  --output hil-results
```

The gate records immutable before/after CONFIG, STATUS, GET ZERO and GET FWINFO
snapshots. It then checks:

1. the exact USB/logical identities, hands, firmware and running-image SHA-256;
2. `00 -> 10 -> 00 -> 11 -> 00 -> 01 -> 00` DTR/RTS behavior, followed by a safe
   `10` postcheck that proves continuous uptime and boot identity;
3. real TAG1 and negotiated TAG2 frames from tactile, IMU and magnetometer streams;
4. every TAG2 CRC, sequence, u64 device timestamp and maximum device-time gap;
5. 20 close/reopen cycles per hand;
6. a 30-second unread-host interval followed by a fresh post-backlog capture;
7. a simultaneous short two-hand capture;
8. unchanged identity, calibration fingerprint, running image and device counters.

The modem-line implementation opens the port exclusively with both lines already
low, passes through an explicit both-lines-low boundary on each transition and only
runs on the supported native-USB VID. There is no bootloader, reboot, factory-reset,
ZERO, SET or firmware-update command in this path.

## 3. Start the real 72-hour gate

The long run needs an additional confirmation containing both serials in left/right
order. This prevents a copied command from starting against a replacement unit.

```bash
oglo hil \
  --left OGLO-L-00028 \
  --right OGLO-R-00028 \
  --firmware 0.9.13 \
  --output hil-results \
  --soak 72h \
  --window 30s \
  --confirm-soak OGLO-L-00028,OGLO-R-00028
```

The runner refuses to start if the estimated artifacts would cross a 100 GiB free
disk reserve. It writes and fsyncs a rolling `soak-windows.jsonl` sidecar containing
per-hand rates, counts, missing/duplicate/backward sequences, CRC/structure failures,
u64 timestamp regressions, device-time maximum gaps and host-read maximum gaps. Raw
TAG2 bytes are retained by default; use `--no-soak-raw` only when the sidecar and
before/after evidence are sufficient for the release decision.

If either hand fails, the peer capture is cancelled instead of continuing for the
remaining 72 hours. A passing unit test or short HIL run is not a substitute for the
completed 72-hour artifact.

## Evidence

Each run gets a new timestamped directory with:

- `hil-report.json` and `hil-report.md`;
- an exact read-only copy of `TAG_V2.json` and its SHA-256/parser binding;
- read-only `before/left.json`, `before/right.json`, `after/left.json` and
  `after/right.json` snapshots;
- real TAG capture files and per-capture summaries, reparsed from disk against the
  same canonical TAG2 contract;
- reconnect and soak sidecars;
- best-effort bounded kernel USB logs where the host OS permits them;
- `manifest.sha256`, which covers every other evidence file and intentionally does
  not hash itself.

Preserve the complete directory as one release artifact. Never copy a firmware
binary, signing key or device credential into this evidence directory.
