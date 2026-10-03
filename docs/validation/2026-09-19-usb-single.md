# Single-glove USB validation — 2026-09-19

> Historical validation record for the dated software and devices below.
> See [current candidate status](../08_candidate_status.md) before choosing a release.
> Raw `acceptance-results/` paths identify internally retained evidence, not public downloads.

**Verdict: NO-GO for sustained USB capture on the tested glove/host combinations.**
The final software matrix and short hardware checks pass, but USB communication
faults persist with two gloves, two cables and hub/direct connections. Device
journals independently confirm TX stalls and, in one incident, an autonomous
recovery reboot. An instrumented plain SDK recording also stopped receiving
after about 486 seconds despite continuous host polling and a maximum recording
file flush of 1.53 ms.
After quitting Dia and recovering USB by re-enumeration, a second instrumented
recording also stopped receiving after 647 seconds. Its device journal confirms
another TX stall. A running Dia process is therefore not required for recurrence.

Crucially, a later **SDK-free** pyserial test reproduced a command-write failure
after about 453 seconds while sensor reception continued for the full 900-second
observation. Reopening the port did not restore commands. Sending the same
commands over BLE then produced prompt replies over USB, without rebooting the
MCU or changing its firmware/calibration. This establishes a USB command-input
path failure independent of the SDK parser and recorder; the entire MCU and
shared command handler were still operating. The earlier TX-silence incident and
this command-input failure must not be assumed to have identical causes.
The exact initiating defect within the host USB/CDC driver, device CDC/USB stack
or their interaction remains unisolated. A successful short reader is not a
qualification or proof that the SDK caused the other failures.

## Scope and tested artifacts

- Host: macOS 27.0, build 26A428, arm64. Two left Rev-D-TIA gloves tested individually, firmware
  0.9.16, CONFIG schema 6. This does not qualify two-hand operation.
- Starting state: RAW, threshold 70, tactile 250 Hz, existing zero valid.
- The original glove was powered only by USB; the operator confirmed no battery.
- Initial tests used a USB 2.1 hub. The final test used a direct host-controller
  connection, independently confirmed in the host USB topology. Failure on both
  paths means the hub alone is not a sufficient explanation. The SDK also changed
  between runs, so this is not a controlled comparison of the hub itself.
- No zero sweep, firmware update, identity change or BLE qualification was run.
  BLE was later used only to deliver diagnostic commands whose replies were
  captured over the failing glove's still-working USB transmit path.
- A later trial changed only the cable according to the operator. Glove identity,
  firmware image, SDK module hashes, original calibration/settings and direct USB
  controller path were verified unchanged. Exact cable models/lengths were not
  supplied; this report calls them cable A and cable B.
- A subsequent trial changed only the glove according to the operator. The same
  direct USB location, firmware image, SDK hashes and stream settings were
  confirmed. Each glove retained its own existing calibration.
- The device-reported running image matched the signed committed golden image.
- Detached firmware signature verification and esptool image validation passed.
- Device identifiers, calibration arrays and recordings remain in ignored local
  evidence; none are included in this public report.

Firmware image Validation Hash:

```text
b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897
```

The locally built wheel is a development snapshot using the existing rc3 version;
it is **not** the published rc3 wheel or a newly published release. Every installed
SDK module was compared byte-for-byte with `src/oglo`.
The final tested runtime source is commit `ee9cde2`; the earlier silence guard is
`a388045`, following the initial qualification fixes in `75ae98e`.

| Local artifact | SHA-256 |
| --- | --- |
| Initial corrected wheel, before silence detection | `7701200f40e317903b144f712b7764455aed7ed40b20c9d0ac599e4ff508af5f` |
| Initial corrected sdist | `ddfef4d5af2581a84cd34c8d894cf97193bac9a52451fa4be4156dbea29ed2b5` |
| Wheel including silence detection | `f40c4b95a88ea01e87ea0d798b397f5099467578367475d482ada1520bee68d7` |
| Sdist including silence detection | `80829f38d2366f8489d03ca801caba2fd1c4ba1cd58b944186b36e5d6b5485ce` |
| Final wheel including bounded USB commands | `930f4c05a58062b35243701fb34d85ac4c0d2385306871e54204ca6c270157c9` |
| Final sdist including bounded USB commands | `21f298fa4eee9b028e1a29c748708651a955d056a3e40ab9d9d4209a11daed56` |

## Completed checks

| Check | Result |
| --- | --- |
| Python 3.10.20 offline suite | 492 passed; 11 hardware tests deselected |
| Python 3.11.15 offline suite | 492 passed; 11 hardware tests deselected |
| Python 3.12.13 installed-wheel offline suite | 492 passed; 11 hardware tests deselected |
| Python 3.13.13 offline suite | 492 passed; 11 hardware tests deselected |
| Python 3.14.0 development-environment offline suite | 492 passed; 11 hardware tests deselected |
| Build wheel from sdist, distribution metadata checks | PASS |
| Installed wheel/source module parity | PASS |
| Hardware pytest, one glove with reversible mutations | 8 passed; 3 two-hand tests skipped |
| Final installed wheel, direct-port hardware pytest | 8 passed; 3 two-hand tests skipped |
| Calibration/mode/threshold/tactile-rate restoration after hardware pytest | PASS |
| Device firmware image versus signed golden bundle | PASS |
| Reconnection after physical USB power cycle, original calibration/settings | PASS |
| Initial corrected wheel: short record/replay | PASS, 10.062 seconds |
| Initial corrected wheel: orderly cancellation and reconnect without reset | PASS |
| Initial corrected wheel: planned 75-minute capture | FAIL, all streams stopped after about 257 seconds |
| Silence-detection wheel, hub: planned 75-minute capture | FAIL, LINK PING write failed after 521.658 seconds |
| Final wheel, direct: short record/replay | PASS, 10.073 seconds, no observed loss |
| Final wheel, direct: planned 75-minute capture | FAIL, all streams stopped after about 494 seconds |
| Final wheel, direct: silence detection and process cleanup | PASS, detected after 5.005 seconds of silence; exited with code 2 |
| Final wheel, direct: reconnect and postflight read-back | FAIL, no CONFIG reply within the bounded attempts |
| Same final wheel/direct port, replacement cable B: short checks | PASS, 30-second stream and 10.077-second record/replay |
| Replacement cable B: planned 75-minute capture | FAIL, all streams stopped after about 1.16 seconds of this capture |
| Replacement cable B: independent post-failure USB read | FAIL, zero received bytes in ten seconds; device still enumerated |
| Replacement glove, same cable B/port/SDK: short checks | PASS, 30-second stream and 10.075-second record/replay |
| Replacement glove: planned 75-minute capture | FAIL, all streams stopped after about 226 seconds |
| Replacement glove: journal and postflight | Autonomous USB-wedge recovery reboot confirmed; original calibration/settings preserved |
| Replacement glove after recovery: independent direct reader | Completed 600.026 seconds, no sequence loss, device drops or reboot; not a 75-minute qualification |
| Replacement glove: instrumented plain SDK recording | FAIL after about 486 seconds of data; continuous host reads and matching device TX-stall record |
| Replacement glove: SDK-free threaded 1 Hz command test | FAIL, command write timed out at about 453 seconds; sensor reception continued through 900.020 seconds |
| Failed USB command path: BLE command / USB reply comparison | Matching STOP, STATUS, IDENT, ZERO, CONFIG and EVENTS replies; same MCU boot, firmware, configuration and calibration |
| Dia closed, USB re-enumerated: instrumented SDK recording | FAIL after 647.013 seconds of data; 5.005-second silence guard and matching device TX-stall record |
| Re-enumeration after the Dia-closed failure | USB queries recovered; same MCU boot, firmware, CONFIG and calibration |

The hardware pytest run covered discovery and identity, zero read-back, all three
streams, stop/start, repeated reconnect without reset, LINK PING cadence and
firmware authorization across a stream pause, doctor, and reversible RAW/CLEAN,
threshold and rate changes. Its 10-second stream checks observed no sequence loss.
The later installed-wheel 30-second stream check measured approximately
250.4 tactile, 500.8 IMU and 125.2 magnetometer packets/s, with zero host/wire loss,
device drops or missed deadlines. These are transport rates, not sensor accuracy.

## Problems found and corrected

1. A manual command queued behind a failed/partial LINK PING could still be sent.
   Timeout and short-write regressions reproduced the issue. Failure publication
   and the waiting writer's final check now share the USB writer lock.
2. Doctor, acceptance, recording and replay treated every USB short write as loss.
   Firmware 0.9.16 retries pending data; short-write counts now remain visible and
   internally consistent without invalidating otherwise lossless data. Firmware
   below 0.9.16 retains its prior rejection, and actual-loss checks remain strict.
3. The hardware suite assumed CLEAN mode and its RAW cleanup failed to restore
   a temporarily changed threshold. Both starting modes are tested and restored.
4. Acceptance waited for recording threads during interruption without requesting
   them to stop. `record(stop_event=...)` now supports cooperative cancellation;
   acceptance signals its workers before waiting and cannot count a cancelled
   capture as a passed duration/soak check.
5. Explicit single-glove acceptance and hardware-test modes now leave pair-only
   checks unqualified instead of pretending a single glove proves two-hand behavior.
6. A completely silent endpoint left a long recording waiting for its requested
   duration. Recording now fails after a fitted stream delivers no samples for
   over five seconds and preserves the partial episode. Regressions cover all
   streams stopping, one modality stopping, silence from the start, an absent
   optional magnetometer, and a host scheduling pause followed by healthy data.
7. After a later LINK PING failure, reconnect blocked in macOS `tcdrain()` even
   though serial writes had a timeout. The process stack independently confirmed
   the blocking syscall. All SDK USB commands now avoid `flush()`; command replies
   confirm processing. A failed or partial manual write also invalidates the
   shared connection, preventing a subsequent ping/command from extending a
   possibly incomplete firmware command. The original serial error is retained
   in the displayed exception. Four new regressions reproduced these paths.

## Incomplete hardware gates and preserved failures

The first hardware run failed doctor on short-write counts despite zero loss.
After the doctor correction, the hardware suite passed. The first installed-wheel
short recording then failed on the same older rule in the recorder (1,327 short
writes, no corresponding evidence of loss in the preceding stream check).
That failure motivated the final record/replay correction above.

The earlier acceptance runner had already entered its long capture. That owned
test process was terminated with SIGTERM to stop testing the known-failing build.
Afterward, the USB device still enumerated and no other process owned its port,
but SDK reconnect and a bounded 10-second read after the documented STOP/CONFIG
commands received no reply (zero bytes). The incomplete episode and pre-failure
logs were preserved; they are not accepted as a soak result.

After the user physically reconnected USB, identity, firmware image and the exact
existing calibration/settings matched the saved preflight. The installed wheel's
orderly SIGINT cancellation exited in 2.114 seconds, sealed a lossless episode
with `stop_reason="cancelled"`, correctly failed the unfinished acceptance run,
and reconnected without changing the MCU boot identity.

A subsequent 10.062-second episode independently replayed 2,514 tactile, 5,030 IMU
and 1,258 magnetometer samples, at device-clock rates of approximately 250, 500 and
125 Hz. Sequence gaps, reported sample drops and device drop/deadline deltas were
zero. Its 914 retried USB short writes were retained as evidence.

The following planned 75-minute capture stopped receiving all three streams after
approximately 257 seconds. It preserved 64,250 tactile, 128,501 IMU and 32,125
magnetometer samples, with no sequence gaps inside the received prefix. After
193.508 seconds of silence, an orderly interruption preserved that prefix with
`complete=false` and a final STATUS timeout. Logical reconnect also timed out.
This failure preceded interruption, so the earlier SIGTERM is not a sufficient
explanation for the recurring loss of USB responses.

After a second user-performed USB power cycle, the read-only `GET EVENTS` dump
contained a `kTxStallStarted` event at MCU uptime 455,233 ms, matching capture start
near 198 seconds plus the 257-second received prefix. Later records in that same
boot showed 169,768 device drops and a stall-clear event near uptime 664 seconds.
There was no recorded MCU reboot between them. These records establish a real
device TX stall and loss; they do not establish which component caused it. The
original calibration and stream settings again matched after reconnection.

The next installed-wheel attempt on the same hub path ended after 521.658 seconds
with a LINK PING write failure. The recorder preserved 130,404 tactile, 260,807 IMU
and 65,202 magnetometer samples as incomplete; the received prefix again had no
sequence gaps. Failure was detected about 0.051 seconds after the last received
samples. This exercised the transport-failure path, not the five-second silence
timeout. Reconnect then blocked in `tcdrain()` as described above. SIGINT was sent
after the failed episode had been saved; the process eventually exited with 130
when the connection was removed. No recording from this attempt passed the soak.

The user then moved the glove to the Mac itself. A new USB topology snapshot
confirmed attachment directly below the host controller, and firmware identity,
the exact original calibration and stream settings matched. The final wheel's
direct-port hardware suite passed all eight applicable tests. The five-version
offline matrix above tests this final source, including both new failure guards.

The final direct-port wheel independently replayed a 10.073-second episode with
2,517 tactile, 5,035 IMU and 1,259 magnetometer samples. Device drop/deadline deltas,
sequence gaps and reported sample drops were zero; 794 retried short writes
remained visible in metadata.

Its planned 75-minute capture then stopped receiving after approximately 494.126
seconds. At 499.166 seconds elapsed, the new guard raised `RecordError` for all
three streams: independently measured silence was 5.005 seconds. The recorder
preserved 123,531 tactile, 247,064 IMU and 61,766 magnetometer samples with
`complete=false`. The received prefix contained no sequence gaps, but the capture
did not meet its duration or rollover gate. Final STATUS and bounded CONFIG
reconnect both timed out. The CLI completed with exit code 2 without any external
signal or a stuck `tcdrain()` call. A separate final read-back attempt also failed,
so device health and calibration cannot be freshly confirmed after this failure.
The latest successful read-back, before the failed capture, matched the exact
original calibration and settings; no calibration-writing command was issued.

Software failure handling is therefore verified, while the underlying USB fault
is not repaired. Before sustained deployment, isolate the remaining path with a
known-good replacement cable, a second host and an independent USB reader, then
repeat the 75-minute loss-free/clock-rollover gate after addressing the cause.
Repeated power cycles of the same setup are not evidence that the fault is fixed.

## Cable-only repeat with the final SDK

At 18:24 KST the operator had replaced the cable while retaining the same glove
and Mac port. USB topology again placed the glove directly below the same host
controller (`location 2-1`), with no intervening hub. Firmware validation hash,
the installed wheel and every SDK module hash were unchanged from the direct-port
failure above. No SDK code was changed for this trial. The preflight read-back
matched the exact original zero, RAW mode, threshold 70 and tactile 250 Hz.

The same acceptance command passed the 30-second stream, reversible setting
changes/restoration and short record/replay. Independent replay of the
10.077-second short episode checked 2,518 tactile, 5,036 IMU and 1,259 magnetometer
samples, with zero sequence errors, sample drops or device drop/deadline deltas.

The following planned 75-minute capture received only 289 tactile, 580 IMU and
145 magnetometer samples, spanning approximately 1.16 seconds, before all streams
went silent. The five-second guard ended acquisition at 6.222 seconds elapsed;
the saved prefix's last receive timestamp preceded that by 5.046 seconds. Final
STATUS and logical reconnect timed out, and acceptance exited with code 2.
The 1.16-second figure is measured from the start of this long capture, not from
cable insertion or the beginning of the entire acceptance sequence.

After the SDK process had exited and no process owned the serial port, a separate
script using only pyserial opened it with DTR high, RTS low and bounded I/O. It
queued the documented `STREAM TAG OFF` and `GET CONFIG` commands and saved every
received byte for ten seconds: **zero bytes arrived**, while the USB device still
enumerated. Successful host writes establish that the OS accepted the commands,
not that the firmware consumed them. This probe confirms that the silent state
persisted outside the SDK's parser/recorder. It does **not** show whether an
independent reader started on a freshly powered glove would trigger the fault.

Replacing this cable did not resolve the observed fault. One repeat cannot
declare either cable universally good/bad or assign the initiating cause to
firmware versus SDK/host behavior. The next discriminating test is an independent
reader from a fresh power-on, followed by a second-host comparison if needed.

Two-hand operation, BLE throughput, physical finger/IMU actions, force calibration,
new `zero()` recipe persistence, slow-storage stress and other operating systems are
outside the completed evidence. Do not reinterpret their absence as a pass.

## Glove-only repeat and autonomous recovery

At 18:33 KST a second left glove ran the same acceptance command with the same
cable B, direct controller location and installed final wheel. The two gloves
reported the same hardware revision, exact firmware image, RAW mode, threshold
70, tactile 250 Hz and fitted magnetometer. The replacement's own zero was saved;
no calibration was copied between units or overwritten.

The 30-second stream and reversible setting checks passed. Independent replay of
the 10.075-second short recording found 2,518 tactile, 5,037 IMU and 1,260
magnetometer samples, with no sequence gaps, sample drops or device drop/deadline
deltas. The 1,111 retried short writes remained visible.

The planned 75-minute capture then stopped receiving after approximately 226.154
seconds. At 231.207 seconds elapsed, the recorder failed after 5.017 seconds of
silence, preserving 56,539 tactile, 113,078 IMU and 28,269 magnetometer samples.
The received prefix had no sequence gaps or timestamp reversals, but is incomplete.
Final STATUS timed out; the immediate reconnect found no USB glove and the CLI
exited with code 2. A later descriptor snapshot showed the same device again at
the same USB location, with a different registry entry.

Unlike the previous glove's persistent silent state, the replacement had rebooted
and answered the subsequent postflight. Boot identity changed, reset reason was
`software`, and `wedge_recoveries` increased from 0 to 1. It reported a last stall
duration of 9,255 ms. Firmware image, its exact original zero and stream settings
all matched preflight. The operator explicitly confirmed no cable or reset-button
action during this incident; no host reset command was issued.

A complete read-only journal dump contained 8,161 unique records, matching its
terminal count, with zero journal queue drops. Joining records by exact boot ID
showed TX stall at uptime 530,025 ms, a USB-wedge restart event at 539,173 ms with
7,967 device drops, then the next software-reset boot. Capture-start STATUS was
at uptime 303,709 ms, consistent with the approximately 226-second received
prefix. These independent device records confirm actual transfer failure and
recovery restart; the missing interval cannot be classified as lossless capture.

Replacing the glove did not produce a completed soak. This establishes that the
observed streaming interruption is not confined to the original unit; it does
not identify the initiating cause or establish that every glove is affected.
Both units share firmware, host and SDK. Automatic recovery restores access but
does not repair the failure or qualify uninterrupted recording.

## Independent reader after replacement-glove recovery

From 18:41 KST, the replacement glove's first streaming capture after its recovery
reboot used a separate single-thread pyserial program. The SDK postflight and
read-only journal dump above had already run on this boot; this was not a pristine
power-on test. The program imported no `oglo` module and used no SDK parser,
recorder or worker. It retained all raw stream bytes, host receive timestamps and
outgoing commands. The same port, cable, firmware, RAW mode, threshold, rate,
pyserial version, DTR/RTS settings, 8,192-byte reads, 50 ms read timeout and 500 ms
write timeout were retained. Documented reply-free `LINK PING` continued roughly
once per second, with a maximum observed interval of 1.056 seconds.

The reader completed 600.026 seconds and saved 28,876,556 bytes. Independent
offline parsing with Python `struct`, without SDK imports, found 150,008 tactile,
300,016 IMU and 75,004 magnetometer frames. Sequence anomalies, backwards device
timestamps, malformed headers, unexpected inter-frame bytes and trailing partial
bytes were all zero. Device-clock rates were approximately 250/500/125 Hz. The
maximum interval between nonempty host reads was 57.3 ms.

End-of-capture queries succeeded. Boot identity, the exact zero and CONFIG were
unchanged; device drop and deadline-miss deltas were zero. The device reported
70,509 retried short writes during the capture, retained as diagnostic evidence.
The direct reader exited with code 0 and released the port.

This is a successful ten-minute direct-read observation, not a substitute for the
failed SDK soak or its 75-minute/clock-rollover gate. It makes SDK acquisition,
recording load, concurrent command timing and earlier command sequence useful
next comparisons. One sequential trial on a different boot cannot distinguish
those effects from intermittent firmware/USB behavior or prove the SDK caused
the earlier stalls. No production-readiness claim follows from this result.

## Instrumented SDK recording and independent fault evidence

At 19:42 KST, a plain `oglo.record(..., seconds=900)` run used the same installed
wheel, glove, direct port, cable and firmware. It did not run acceptance's mode,
threshold or rate mutations. A transparent serial wrapper retained every received
byte and measured each read, write and recorder chunk flush. The original harness
is preserved beside the trace; the installed SDK's I/O and recording policies
were unchanged. Instrumentation adds work and therefore does not establish the
exact timing of an uninstrumented run.

All three streams stopped after approximately 486.049 seconds. The five-second
guard ended the incomplete capture at 491.091 seconds. Independent `struct`-only
parsing of the 23,389,018 capture bytes matched all saved episode counts:
121,501 tactile, 243,004 IMU and 60,751 magnetometer samples. Sequence anomalies,
backwards timestamps, malformed headers, inter-frame garbage and trailing bytes
were zero. Each stream crossed one 32-bit microsecond rollover successfully;
data continued about 15.52 seconds beyond the rollover. This is not a passed
75-minute qualification.

| Measured capture behavior | Observation |
| --- | --- |
| Serial reads | 9,640 calls; no read exceptions |
| Host gap between consecutive reads | Maximum 9.932 ms; p99 1.186 ms |
| Recorder chunk flush | 102 calls; maximum 1.530 ms |
| Serial read duration, configured 50 ms | Maximum 55.379 ms |
| `LINK PING` interval | Maximum 1.004974 seconds |
| Command write duration | Maximum 0.667 ms; no write errors |
| After the last received data | 93 consecutive empty reads before the guard |

The host kept polling while the endpoint returned no bytes. A parser that merely
discarded valid incoming data, a seconds-long disk flush, and the acceptance
setting-mutation sequence are not necessary explanations for this reproduced
failure. Host/USB transaction timing can still matter despite fast Python calls.

After the operator reconnected the same physical connection, a complete journal
dump contained 8,175 unique records with zero journal queue drops. The failed
boot recorded a TX-stall-start event at uptime 4,310,596 ms. The last received
IMU timestamp unwraps to 4,310,487.402 ms, 108.598 ms earlier, consistent with
the firmware's 100 ms sustained-stall threshold. No USB stop, suspend or reboot
record precedes that stall in this capture. Later records show 5,577 device
drops. A DTR-clear record about ten seconds after the stall is consistent with
host cleanup; its accompanying stall-clear record alone does not prove endpoint
recovery, because the detector also clears when the host detaches. Post-failure
CONFIG queries still timed out before the operator's reconnect.

The post-stall record also changes the stream-active flag from true to false.
Source inspection shows that DTR changes do not clear this flag: the relevant
non-update paths are explicit stream commands. This host trace sent `STREAM TAG
OFF` after the fault and no firmware-update or alternative-stream command.
Thus the journal and code together support continued command reception/execution
after sensor data stopped reaching the host. Later DTR deassertion and assertion
are also recorded in the same boot. A missing text reply alone is **not evidence
that the command was never received**. This incident supports a stalled device-to-
host data path with at least some surviving command/control activity, rather than
a continuously frozen entire MCU. It does not prove every later ping arrived or
that all earlier incidents had the same directional failure.

A `LINK PING` write occurred during the final nonempty read: 7.20 ms after that
read began and 47.83 ms before it returned. Subsequent writes were accepted by
the host, which does not prove firmware consumption. This temporal association
motivates testing command timing independently; it does not prove that this
particular command caused the fault or identify a firmware source line.

## SDK-free command-path failure and live cross-transport diagnosis

After the operator reconnected the same glove/cable/port, an independent pyserial
program ran with no `oglo` imports, no TAG parser and no SDK recording code. A
separate thread sent the same reply-free `LINK PING` command with a one-second
deadline. Serial settings remained 115200, 50 ms read timeout, 500 ms write
timeout, DTR asserted, RTS low and 8,192-byte reads. The original harness is
archived with its hash; it differs from the earlier successful direct reader by
using a separate command thread instead of sending pings inline between reads.

The 454th ping attempt started at elapsed 453.007 seconds and raised
`SerialTimeoutException` about 510 ms later. The 453 earlier writes had returned
success; this does not prove when the device last consumed a ping. The command
thread stopped on that error. The independent reader continued receiving until
900.020 seconds, saving 43,314,056 bytes. The final `STREAM TAG OFF` write also
timed out. Exit code was 2. This was **not a passed duplex test**: progress output
initially displayed receive health without the command-thread error, and the
final joined trace corrected that provisional interpretation. The diagnostic
harness was subsequently amended to display and fail on command-thread errors
explicitly; the original executed copy and evidence were retained.

Independent offline decoding found 225,008 tactile, 450,016 IMU and 112,504
magnetometer frames, with zero sequence anomalies, backwards timestamps,
malformed headers, inter-frame garbage or trailing bytes. No read exceptions or
empty reads followed the final data. The maximum host gap between reads was
2.636 ms. A device journal read later confirms zero device drops at the first
DTR-clear event after this observation. Later drops accumulated while the
still-streaming device had no reader during diagnosis and must not be attributed
to the 900-second received prefix.

The next probes preserved this failure without a power cycle:

| Probe | Result and meaning |
| --- | --- |
| Close/reopen serial, delimit any partial command with a newline, then STOP and STATUS | Writes returned success, but no reply and sensor streaming continued |
| Native nonblocking `os.write`, bypassing pyserial's write implementation | Same commands accepted by the OS, still no effect; USB receive continued |
| Termios and tty queue | XON/XOFF/IXANY and RTS/CTS disabled; tty output queue reported zero; this does not expose every internal USB-driver queue |
| BLE command with USB reply, exact glove identity verified first | STOP replied in 100 ms and STATUS in 90 ms; IDENT, ZERO, CONFIG and complete EVENTS dump also succeeded |
| Firmware/boot/calibration read through that alternate command path | Same MCU boot, same application hash, exact original CONFIG and zero; healthy sensors and no I2C-stuck indication |
| Standard USB endpoint GET_STATUS through native IOKit control requests | OUT `0x03`, IN `0x84` and notification `0x85` each replied with HALT clear |
| USB STATUS/CONFIG after BLE stopped sensor transmission | Still no command replies; periodic device heartbeat continued |

BLE delivered the same firmware commands to the same main-loop command handler;
their replies arrived over the same physical USB connection that ignored USB
commands. This isolates the failure to the **USB command-input delivery path**
and rules out a continuously frozen MCU, a universally broken command parser,
or the SDK recorder/parser being necessary for this fault. It does not by itself
separate the Mac's CDC OUT path from the device's CDC receive path.

An IOKit registry snapshot also showed a Dia device user client even when `lsof`
reported no second tty owner. The operator completely quit Dia. A fresh registry
snapshot showed no USB user clients, but USB STATUS, CONFIG and IDENT still
received no replies. A retained device handle alone does not prove traffic or
causation, and quitting a browser cannot exclude an earlier contribution to an
already latched fault.

Two targeted recovery operations were then compared with streaming stopped and
all diagnostic serial/BLE sessions closed. Native `ResetDevice` returned success,
but USB commands still failed; another BLE-to-USB diagnostic dump confirmed the
same MCU boot. Native `USBDeviceReEnumerate(..., 0)` then returned success and
USB STATUS, CONFIG, IDENT and ZERO immediately worked. Boot identity, firmware
hash, CONFIG and the exact original zero were unchanged. There was no firmware
flash or physical power cycle. This shows recovery by rebuilding the USB device/
interface session while the MCU continued running. Re-enumeration affects both
host driver attachment and device USB configuration; it does **not** alone prove
which side contains the initiating defect. Recovery is not lossless capture or
evidence that the fault cannot recur.

Firmware 0.9.16 has an additional recovery limitation: its stall decision uses a
shared link-progress clock renewed by completed device transmissions **or**
received host bytes. Consequently, even in an opted-in session, continued traffic
in one direction can prevent the automatic-restart threshold from being reached
when the other direction is stuck. Closing/reopening DTR also clears that
session's recovery authorization. This is source-verified policy behavior, not
proof of which low-level USB defect first stopped a direction. Neither the SDK
silence guard nor a device recovery reboot eliminates that underlying defect.

## Recurrence with Dia closed

After the operator quit Dia and USB commands recovered through re-enumeration,
the same installed wheel and transparent timing harness attempted another
900-second plain recording. Sensor reception stopped after 647.013 seconds.
The SDK ended the incomplete recording after 652.018 seconds, including 5.005
seconds of silence, and the diagnostic process exited with code 2. This fresh
recurrence means an actively running Dia process is not necessary. Because the
MCU was not power-cycled between these two trials, it does not exclude every
possible retained effect from earlier activity.

The saved prefix contains 161,753 tactile, 323,505 IMU and 80,876 magnetometer
samples. Independent decoding found zero sequence anomalies, backwards
timestamps, malformed headers, unframed bytes or trailing bytes. There was no
timestamp rollover in this capture. All 652 in-capture ping writes returned
success, with a maximum interval of 1.004993 seconds. The maximum serial read
duration was 57.024 ms, host gap between reads 15.363 ms, recording flush 3.259 ms
and command write 0.104 ms. After the last data, 93 empty reads preceded the guard;
there were no serial read or write exceptions within the capture.

A bounded post-failure STATUS query received zero bytes. Re-enumerating USB
again restored STATUS, CONFIG, IDENT and ZERO while retaining the same MCU boot,
application hash, configuration and exact original calibration. The subsequent
journal dump contained 8,205 unique records with zero journal queue drops. Its
TX-stall record occurs 108.257 ms after the last received IMU device timestamp,
consistent with the firmware's 100 ms detector. Device drops increased by 5,813
by the first DTR-close record after the failure; these are post-stall drops, not
sequence gaps within the saved prefix. No USB stop, suspend or reboot record
precedes this stall during the capture.

The subsequent [Linux/Pi comparison](2026-09-19-linux-usb.md) preserved the measured
glove identity, firmware, configuration and calibration baseline, but the operator
also changed the cable. The SDK-free reader reproduced the 50 ms read / 500 ms
write timeouts, 8,192-byte reads and threaded 1 Hz pings. That follow-up distinguishes
temporary backpressure/loss and a controlled shutdown defect from the persistent
mid-stream failures documented here. It also reproduced persistent reception loss
on Linux after approximately 442 seconds, with bulk-IN `EPROTO` errors, continued
BLE sampling and recovery by USB bus reset without an MCU reboot. A Mac-only
defect cannot explain all observations. The exact initiating defect and the
75-minute qualification gate remain open.

## Reproduction and local evidence

```sh
python -m pytest
python -m pytest -m hardware --hardware-single --hardware-mutations --hardware-seconds 10
oglo acceptance --single --seconds 30s --record 10s --mutations --soak 75m
```

Local evidence root (ignored by Git):
`acceptance-results/qualification-20260919-toz3249b/`.
Key files: `preflight.json`, `hardware.log`, `hardware-fixed.log`,
`hardware-fixed.xml`, `hardware-direct-bounded-usb.xml`, `offline-final-3.*.xml`,
`offline-stall-guard-3.*.xml`, `offline-bounded-usb-3.*.xml`,
`acceptance-wheel.log`, `acceptance-wheel-final.log`,
`restored-after-hardware.json`, `firmware-image-info.txt`,
`tested-artifact-final.json`, `tested-artifact-stall-guard.json`, `tested-artifact-bounded-usb.json`,
`after-stop-usb-read.bin`, `reconnect-check.json`, `reconnected-preflight.json`,
`cancellation-validation.json`, `short-recording-validation.json`,
`stalled-soak-validation.json`, `stalled-soak-stack.txt`,
`second-reconnect-preflight.json`, `device-events-after-second-reconnect.txt`,
`stalled-boot-events.json`, `stalled-soak-stall-guard-validation.json`,
`stalled-soak-stall-guard-stack.txt`, `usb-topology-direct.txt`, `direct-preflight.json`,
`short-recording-direct-validation.json`, `stalled-soak-direct-validation.json`,
`postflight-direct.json`, and `acceptance-wheel-direct.log`.
The cable-only repeat is under `cable-b/`: `comparison-plan.json`, `preflight.json`,
`usb-topology.txt`, `acceptance.log`, `short-recording-validation.json`,
`failed-soak-validation.json`, and `independent-usb-read.json`/`.bin`.
The glove-only repeat is under `glove-b/`: `comparison-plan.json`, `preflight.json`,
`usb-topology.txt`, `usb-topology-after.txt`, `acceptance.log`,
`short-recording-validation.json`, `failed-soak-validation.json`, `postflight.json`,
`device-events.txt`, `device-events-read.json`, and `incident-events.json`.
The independent reader is `glove-b/direct_reader.py`, with raw bytes, receive and
command logs, result metadata and independent validation under
`glove-b/direct-reader/`. `glove-b/analyze_direct_reader.py` parses that saved wire
capture without SDK imports.
The instrumented plain recording is under `glove-b/sdk-record-trace-1/`, including
`io.jsonl`, `usb-read.bin`, `trace-analysis.json`, `raw-validation.json`, the
incomplete episode and `incident-events.json` joined against the later journal.
The SDK-free duplex failure is under `glove-b/raw-threaded-1/`, including the
executed `harness.py`, `stream.bin`, `io.jsonl`, `result.json` and
`raw-validation.json`. Its `reopen/`, `syscall-probe/`, `ble-command-usb-reply/`,
`usb-after-ble-stop/`, `usb-after-dia-quit/`, `usb-after-bus-reset/`,
`ble-after-bus-reset/` and `usb-after-reenumeration/` retain each subsequent probe.
`endpoint-status.jsonl`, native IOKit probe sources, registry snapshots and
`recovery-comparison.json` preserve the endpoint/recovery observations.
The Dia-closed recurrence is under `glove-b/sdk-record-no-dia-1/`, including its
partial episode, raw and timing analyses, complete device journal,
`incident-events.json` and `evidence-manifest.json`. The bounded failure/recovery
queries are under `glove-b/raw-threaded-1/usb-after-no-dia-record/` and
`usb-after-no-dia-reenumeration/`. The private Linux/Pi kit is
`glove-b/linux-comparison-kit/`; follow-up host results are documented separately.
The local evidence includes real device identifiers and must be reviewed/redacted
before sharing outside the test station.
