# NTU handoff readiness — 2026-09-20

> Historical validation record for the dated software and devices below.
> See [current candidate status](../08_candidate_status.md) before choosing a release.
> Raw `acceptance-results/` paths identify internally retained evidence, not public downloads.

**Status: not ready for qualified experimental data collection.** A development
preview can be supplied with the firmware limitation stated explicitly. No
message or package has been sent to NTU as part of this assessment.

**Overnight continuous qualification: FAIL — test execution gap.** The assistant
did not continue validation recording between the September 20 23:07 KST suite
completion and the September 21 06:46 KST restart, and did not disclose the gap
promptly. Approximately 7 hours 39 minutes have no validation recording proving
continuity. Individual completed tests and subsequent runs cannot fill this gap
or turn this overnight attempt into a pass. This is a failure to execute and
report the required test; the gap does not establish a device fault. The overnight
qualification requirement was not met by that run. See [the recorded verdict](2026-09-21-repeat-and-reconnect.md).

**September 22 overnight result:** A separate run on L-00003 and R-00028 completed
8 h 59 m 33 s of continuous simultaneous rc7/0.9.17 recording, from September 21
22:51:31 to September 22 07:51:04 KST. All 56,653,162 recorded samples were checked;
both hands maintained measured average 250/500/125 Hz with zero sequence loss and
zero capture-window USB completion errors. The requested 8–9 hour capture scope
passed. Both initial attempts had a pre-recording status timeout; its cause is
unresolved. The original strict nine-hour result remains FAIL. A post-capture
report file-enumeration error is also preserved separately from valid full USB
verification. This does not clear earlier failures or grant release qualification.
The archive and all 95 member hashes were verified on the Mac. See
[the completed overnight evidence](2026-09-22-overnight-results.md).

**Current candidate:** SDK rc7 at `50b59f72d81d769111db4e82451933b7e0b20a9e`
with the canonical Ubuntu-built 0.9.17 firmware. Linux and Mac two-hand 75-minute
acceptance and independent postflight passed at 21:33 and 23:05 KST respectively.
Mac used the same two cables through one shared hub. Physical reconnect and physical web-updater installation verification remain open.
The exact candidate is now production-key signed for explicit supervised canary
use, registered as release `9d2370ad-f157-4ae6-afd7-3ba21839c6a5`, and selectable at
https://oglo-updater.vercel.app. Updater PR 29 is merged at `27bd246c`; its 70 unit
tests and full repository CI passed. Canary activation and the live selection
were verified through the authenticated production UI. This does not qualify
the firmware or SDK for NTU delivery. Earlier rc4,
rc5 and rc6 results below are historical and do not replace this candidate's gates.

**September 21 follow-up:** L-00003 passed six 10-minute direct captures and one
one-hour SDK recording. The next recording ended at a USB disconnect; the owner
subsequently reported likely removal by a colleague. The Mac interruption was
confirmed to coincide with cable/hub manipulation. Neither event establishes a
spontaneous firmware regression. R-00021 has been taken away and replaced by
R-00028, initially on 0.9.16. Its NVS-preserving canonical 0.9.17 write and
post-cycle runtime/settings checks passed. The replacement pair passed the
60-second recording and independent 105,140-sample checks. Its 75-minute Mac
qualification was interrupted when the owner moved R-00028 to the Pi. L-00003's
resumed first one-hour recording completed and passed independent checks; the
single-glove suite then stopped because the second glove changed its topology.
The owner has now connected both historically suspect units, L-00003 and R-00028,
directly to the Pi. The detached queue passed the 75-minute SDK and 30-minute
LINK PING stages, then stopped at 14:07 KST when the whitespace stage failed the
zero TAG-drop/deadline counter check. SSH recovery allowed full archive collection
and all 179 member hashes to be verified at 19:58 KST. Independent decoding found
116 missing samples on L-00003 and 118 on R-00028, exactly matching the new TAG
drops, with no capture-window deadline increments or USB completion errors.
Both host read loops paused together for 379 ms shortly before those holes;
both gloves continued through the full 30 minutes. Host logging/storage or
scheduling delay is the leading explanation, not yet a proven specific cause.
Both gloves still answered queries with the same boot/image/settings at 19:59.
The suite remains stopped and failed; the eight-hour stage did not run and no
physical reconnect pass is claimed. See [the current two-unit plan and evidence](2026-09-21-suspect-pair.md).
See [the preserved results and operator corrections](2026-09-21-repeat-and-reconnect.md).

An additional operator-reported suspect glove, L-00003, reproduced terminal USB
failure on signed 0.9.16 without the SDK: after 23.507 seconds with 10 Hz whitespace
and 217.014 seconds with 1 Hz LINK PING, each with 384 capture-window USB IN -71 completions and
zero observer drops. USB resets recovered the same MCU boot/settings/zero. Four
stock units now have reproduced failures. The subsequent authorized canonical
0.9.17 write preserved NVS, identity, settings and zero. Both matched five-minute
probes and rc7 60-second/15-minute recordings passed without sequence loss, new
device drops/deadlines, capture-window USB errors or observer loss. The original
Pi services were restored. All four stock failures now have candidate comparisons;
this is scoped USB capture evidence, not complete hardware or fleet qualification.

## Verified starting point (historical)

- Public SDK main is `0.1.0rc4`, merged through public PR #9 at
  `425346d3d979db742a767c19fd133f3c49efb493`. This includes the DTR-close fix,
  bounded writes, stream-silence detection and cancellation of the peer recorder
  when either hand fails acceptance. The original investigation branch remains
  local; its old rc3 development artifact is not the proposed handoff package.
- All 17 final PR CI jobs and all 17 merged-main CI jobs passed. Each ran 499
  offline tests with 11 physical hardware tests deselected. Coverage includes
  Python 3.10–3.14 on macOS, Linux and Windows, minimum dependencies and installed
  wheel/sdist verification. The clean installed wheel also passed 499 tests on
  macOS and on the bench Pi's Linux/aarch64 environment.
- Earlier rc4 wheel SHA-256 (the rc5 successor is described below):
  `99a4ec85194dc03a90c0923fa93fc5930c0fd5e831a1156c8e72d46dfec8c395`.
  All 14 installed modules match the artifact/source. The rc4 GitHub release
  remains an unpublished draft with eight uploaded assets verified by downloading
  them again. The public repository/commit can already be shared for evaluation.
- The owner confirmed the target is **macOS and Linux, two hands simultaneously**.
  Exact target host versions, architecture, cable/port and disk configuration
  still need to be recorded during physical qualification.
- One glove's private FIFO-allocation firmware intervention completed two
  300-second SDK-free USB traffic tests without USB errors or complete-frame
  sequence gaps. This is not a production firmware release or an SDK soak pass.
- After the operator's USB cycle on September 20, signed 0.9.16 passed all nine
  independent runtime/digest/identity/configuration/calibration/health checks.
  The planned SDK-free 10 Hz whitespace return comparison failed after 52.323
  seconds of reception, with 384 USB IN protocol errors. A targeted USB reset
  recovered the same MCU boot and preserved settings. This strengthens the
  firmware FIFO-allocation finding; it does not qualify stock 0.9.16 for capture.
- A replacement right glove on the same Pi port independently reports the same
  production application hash. The identical SDK-free comparison failed twice,
  after 47.912 and 15.838 seconds, each with 384 USB IN protocol errors and zero
  observer losses. Thus the failure is reproduced on two physical gloves; this
  is neither an all-glove failure rate nor simultaneous two-hand validation.
  Final recovery preserves firmware, CONFIG, stored zero and MCU boot, and the
  original Pi services are restored with the replacement glove connected.
- A third physical glove, a different left glove with the same production hash,
  failed the identical comparison after 5.614 and 40.044 seconds, again with
  384 USB IN protocol errors in each run and no observer loss. Settings, MCU boot
  and services are restored. The stock failure therefore spans three tested
  gloves. The narrow diagnostic intervention covered one glove; the subsequent
  source-candidate result on L-00021 is described below.
- The [uploaded 0.9.16 field audit](2026-09-20-field-usb-0916.md) covers 554
  non-bench captures and 151.662 hours, including 42 recovered captures reviewed
  separately. It confirms capture-time USB reader failures in 51 recordings;
  49 also have tactile RAW ending over five seconds
  before the camera frame log. A fully scanned example loses the last roughly
  4.5 minutes of both tactile streams despite `completed` report statuses.
  Historical version labels lack runtime hashes and USB FIFO traces, so this
  confirms field symptoms without proving the allocation defect caused each one.
- The exact rc4 wheel subsequently passed a 60-second single-glove Pi recording.
  Every saved sample matches independent USB decoding; USB errors, sequence gaps,
  new device drops and missed deadlines are zero. A separate readback after a
  20-second closed interval preserves MCU boot, firmware, configuration and zero.
  Pi services are restored. This does not qualify a fixed firmware release,
  long-duration recording, macOS hardware or two simultaneous gloves.

See [the investigation evidence](2026-09-19-usb-ping-ab.md) for exact firmware
identities, comparisons, limitations and private evidence hashes.

## FIFO candidate progress

The earlier **rc6** candidate used source `32b59db741c6dda3723860614733ae9d7cf662d3`
in [draft PR #11](https://github.com/OpenGraphLabs/oglo-python/pull/11); public main
remains rc4. rc5 fixed stream-collection analysis but left record/replay analysis
running while caller-owned gloves resumed transmission. The rc5 pair's short
capture passed, then the long capture failed preflight with error_flags=16.
Its failed evidence is preserved. rc6 stops each recording's glove in the worker
before waiting for the peer or replaying files, including on exceptions.
Actual losses and unhealthy status remain strict failures. The runtime wheel
SHA-256 is `59974f43e8c0e9852733c1f61c826f701a81a8bb3a2a87e8d86a90072ffccfb7`.
Source and fresh installed-wheel tests passed 504 cases on Mac and Pi, with 11
hardware tests deselected. A later CI timing failure in the episode-numbering
test is retained; that test now uses a shared fake clock without changing product
code or freshness thresholds. All 17 final source-head CI jobs passed 504 tests
([run 35502027175](https://github.com/OpenGraphLabs/oglo-python/actions/runs/35502027175)).

The [0.9.17 source candidate and capture-status repair](2026-09-20-fifo-release-candidate.md)
are committed in hardware PR #44 and og-skill PR #1134, with passing GitHub CI.
The current L-00021 passed the matched 300-second whitespace test with the Mac
build candidate: zero USB errors, sequence anomalies or observer loss; NVS and
configuration were preserved. Ubuntu CI builds repeat byte-for-byte but differ
from the Mac application. The Ubuntu image is the release qualification target
and has been written and hash-verified with all 20 KiB of NVS preserved against
the original stock backup. After physical USB cycling, its runtime/settings checks,
both matched 300-second probes and independent rc4 USB/recording parity pass.
Installed rc5 general single-glove acceptance then passed 27 checks with 4 explicit
skips, including a 60-second record/replay, reversible settings and reconnect.
Final independent readback preserves identity, image, MCU boot, CONFIG and zero;
the Pi services and usbmon state have been restored.
An updater-mode EPROTO/re-enumeration issue is recorded separately in the report.
These results do not close the two-hand gate.

The same canonical Ubuntu image subsequently passed the right R-00021's two
matched 300-second probes, with zero USB errors, complete-frame anomalies,
observer loss, new device drops or missed deadlines. This glove had failed twice
on stock firmware after 47.912 and 15.838 seconds. NVS, configuration and zero
were preserved. rc5 recording parity covers all 52,580 saved samples; a 64-byte
terminal fragment at timed stop is explicitly excluded from complete-sample
parity. The original boundary-unaware verifier failure is retained, and the
replacement rejects corrupted data in negative controls. General rc5 acceptance
passed 27 checks with the same 4 explicit skips, followed by independent state
readback and Pi service restoration. Thus two individual gloves have passed the
canonical short suite; simultaneous two-hand operation is still unqualified.
L-00028 subsequently passed both matched 300-second probes and independent
60-second recording parity with all 52,593 saved samples. Its timed-stop terminal
fragment is 128/133 bytes and passes the same boundary verifier and negative
controls. Its general single acceptance refused to start because the right glove
was already attached; this is not a single acceptance pass. A descriptor -71 on
right-glove addition followed by successful retry is a separate unresolved
enumeration observation.

The rc6 pair repeat uses the same L-00028/R-00021, ports, cables, MCU boots and
canonical firmware as the rc5 failure. Pair 30-second streaming, 60-second
recording and settings restoration pass. Unlike rc5, the transition into the soak
adds no drops or missed deadlines on either hand. The Linux 75-minute capture
finished but **failed**: left lost 558 samples and right lost 542; both recordings
are explicitly incomplete. Both gloves still respond, with unchanged MCU boots,
firmware, configuration and zero. Capture-window bulk completions have zero
errors and both USB observers lost zero events. No new device deadline misses
occurred. Left has four loss intervals and right six; all four left intervals
overlap right-hand loss. Receive pauses of up to 554/518 ms coincide with chunk
boundaries. A timing-only 25-minute rc6 repeat then measured natural fsync stalls
of 394/391 ms, receive pauses of 519/512 ms and exactly matching device/sequence
losses of 200/193 samples. Both hands' losses follow those same-thread storage
calls; USB completion errors, observer drops and new deadline misses are zero.
This establishes a separate SDK storage/read coupling failure; it does not
diagnose a defective SD card.
All original evidence is retained and archive/file hashes were verified. Mac
movement is held; the operator is available after the completion instruction.
No Mac pair soak has run, and this combination is not qualified for NTU capture.

rc7 separates chunk storage into a bounded worker and retains strict incomplete
recording behavior. Under matched, explicitly injected 500 ms storage delays,
rc6 lost 286/287 samples while rc7 lost zero on both hands. Firmware, MCU boots,
settings and zero were unchanged. This is a controlled mechanism test, not a
long-soak pass. The rc7 source candidate is
`50b59f72d81d769111db4e82451933b7e0b20a9e`, including current main's collection
examples and data specification. Its Mac installed suite passes 526 tests; Pi
passes 509 SDK tests (one optional camera-module skip); 18 CI jobs pass, including
the separate 17-test camera job. All 15 installed modules and the wheel match.
The uninstrumented Linux pair 75-minute repeat passed 54 checks, with two explicit
skips for human tactile/IMU interaction and calibration replacement. Both captures
lasted over 4,500 seconds and overlapped for 4,500.094 seconds. Each hand saved
1,125,068 tactile, 2,250,138 IMU and 562,534 magnetometer samples. Independent array
checks found no missing, duplicate or backward sequences, no backward host/device
time, and continuous unwrapped clocks across one raw-clock rollover per stream.
New device drops and deadline misses, capture-window USB bulk completion errors
and observer drops were all zero. Final readbacks preserved firmware, MCU boot,
configuration and zero. The operator has been instructed to move the same pair
to Mac; that physical test has not started. Original failed captures are retained.

## Remaining delivery gates

1. **Causal return comparison completed.** The stock firmware return reproduced
   the USB failure, its original trace was retained, and runtime/identity/settings
   checks passed after recovery. The same stock failure subsequently reproduced
   on two additional physical gloves. The narrow diagnostic intervention was a
   one-device test; the source candidate has separate L-00021 and R-00021 results in the
   candidate progress section above.
2. **Source candidate prepared; firmware release qualification remains open.** Carry the verified allocation
   correction into a reproducible candidate without diagnostic instrumentation.
   Preserve the supported TAG/configuration contracts and NVS. Assign explicit
   firmware identity, verify the built artifact, and retain a tested rollback.
   Do not distribute the private 0.9.99 diagnostic as a qualified customer release.
3. **Qualify the actual SDK/firmware combination.** Use the confirmed Mac/Linux
   two-hand scope. Test clean
   installation, streaming, recording/replay, stop/start and reconnect with the
   exact proposed artifacts. Run the existing 75-minute acceptance soak on the
   intended storage to cross the roughly 71-minute device clock rollover. Require
   completed recordings, continuous recorded sequences and successful settings
   restoration. Include USB-error and device-drop observations in the report.
   A single-glove result cannot qualify simultaneous two-hand operation; BLE
   remains outside this USB delivery gate.
4. **SDK package prepared; firmware package remains open.** The corrected SDK
   has a distinct rc7 candidate, exact source/artifact identity, installed-artifact
   checks, checksums, evaluation guide and draft handoff message. Bundle the
   subsequently qualified firmware/update and rollback instructions,
   installation steps, working example, acceptance report and known limitations.
   If delivering through the public repository, ensure the pinned tag/commit
   contains the corrections. Do not publish the draft or send it as a qualified
   capture release until the remaining physical gates pass.

The [acceptance guide](../07_acceptance.md) defines the public-API tests and
75-minute soak. Passing evidence applies to the tested configuration and duration;
it is not an unlimited reliability guarantee. Mac/Linux two-hand claims need
corresponding runtime evidence rather than extrapolation from a single-glove Pi test.
