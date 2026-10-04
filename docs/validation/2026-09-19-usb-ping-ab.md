# USB command-traffic isolation — 2026-09-19–20

> Historical validation record for the dated software and devices below.
> See [current candidate status](../08_candidate_status.md) before choosing a release.
> Raw `acceptance-results/` paths identify internally retained evidence, not public downloads.

**USB IN stops completing while device sampling and USB OUT reception continue;
sustained capture remains NO-GO.** The matched production-firmware comparison
failed both PING-enabled runs and completed both no-PING runs. The private
observation image subsequently reproduced the stall with both PING and whitespace
traffic, so PING command semantics are not required. The experiments remove the
SDK entirely and first change whether a separate thread sends
`LINK PING\n` once per second during TAG streaming. It follows the
[initial Linux investigation](2026-09-19-linux-usb.md). The FIFO intervention and
signed-production return comparison is now complete for this one device. The
confirmed allocation defect has strong causal support for this Pi failure;
a distributable firmware correction and target-host qualification remain open.
Subsequent matched tests reproduce the stock-firmware IN-stall signature on
three physical gloves. The FIFO intervention remains a one-device experiment.

## Controlled conditions

The same Raspberry Pi 5, physical USB port, cable and left glove remain connected
through the comparison. Firmware is the original signed 0.9.16 application,
runtime image digest
`b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897`.
Tactile/IMU/magnetometer rates remain 250/500/125 Hz, with RAW tactile streaming
and the saved calibration/configuration preserved. No firmware flash occurs in
this baseline comparison. The earlier Mac-to-Pi move also changed the cable;
it still cannot isolate an operating-system effect.

Order: PING on / off / off / on, up to 900 seconds each. Each run starts with the
same targeted USB reset, exact-identity/configuration/calibration queries and
journal read. A fresh DTR generation is verified to have no inherited host-recovery
authorization. The pyserial 3.5 reader uses 8,192-byte reads, a 50 ms read timeout
and a 500 ms write timeout, with explicit DTR deassertion on close. RAW bytes and
userspace timing traces go to RAM during acquisition, then are copied and hashed.
The capture agent and ModemManager are stopped during the sequence. Kernel USB
requests/completions are captured with the binary usbmon observer.

This tests **periodic command traffic plus its firmware handling**, not just a
Python function name. PING also authorizes the firmware's existing recovery
policy. A difference between conditions alone cannot distinguish the PING parser,
the authorization path, USB OUT activity, and bidirectional USB timing.

## Baseline results

| Run | Periodic PING | Stream duration | Complete samples: tactile / IMU / mag | USB IN errors | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | On | 165.002 s before silence | 41,250 / 82,502 / 20,625 | 384 × `-EPROTO` | Persistent reception failure |
| 2 | Off | 900.012 s capture | 225,006 / 450,014 / 112,504 | 0 | Duration completed |
| 3 | Off | 900.017 s capture | 225,000 / 450,002 / 112,501 | 0 | Duration completed |
| 4 | On | 106.996 s before silence | 26,749 / 53,499 / 13,375 | 384 × `-EPROTO` | Persistent reception failure |

All complete frames in all four runs have continuous sequence numbers,
valid headers and nondecreasing unwrapped device time. Run 2 ends with 128 bytes
of a partial next frame at the requested time boundary; this is not claimed as
a complete-frame capture. Run 3 crosses the natural 32-bit microsecond timestamp
wrap once per stream without a sequence anomaly. The two completed no-PING runs
have zero device-reported sample drops and deadline misses. Partial USB writes
are counted by firmware and retried; that counter is not a dropped-sample count.

Run 1's last data is followed by 12 seconds of observed silence before the reader
closes. Its maximum gap between userspace reads is 0.719 ms, ruling out the
hundreds-of-milliseconds reader pause seen in an earlier, different run as the
immediate explanation for this incident. All 178 captured PING OUT transfers
complete successfully at the host-controller boundary. The first IN error follows
the preceding PING completion by 794 microseconds. This is temporal association,
not proof that firmware parsed that PING or that the physical bus handshake was
valid. The observer reports zero lost events. A targeted USB reset recovers the
same MCU boot, firmware and calibration/settings.

The no-PING runs' maximum userspace gaps are 0.780 and 0.786 ms. Both USB observers
report zero lost events; successful IN completion counts are 937,396 and 942,619.
Immediate stop/query checks and post-reset checks preserve the MCU boot and the
saved firmware/configuration/calibration.

Run 4 reproduces the same IN-failure signature. Its maximum userspace read gap is
0.709 ms; all 120 PING OUT transfers complete successfully. The first IN error is
493 microseconds after the preceding PING completion. The observer reports zero
lost events. USB reset again recovers the same MCU boot and preserved settings.
The full capture, including the final 12-second silence observation, lasts
119.029 seconds. Thus the ordered result is fail / complete / complete / fail.

The earlier PING-enabled SDK-free run completed 15 minutes with a separate
backpressure/sample-loss interval. Thus even a repeated on/off difference here
must not be described as a deterministic failure on every PING-enabled run.

## Private diagnostic image

The hardware repository's isolated branch
`debug/usb-endpoint-observability-20260919`, commit `0396c43d`, contains a local
0.9.99 observation builder. The signed production source and golden bundle remain
unchanged. The build records separate DCD IN/OUT submissions/completions, CDC
callbacks, TinyUSB busy/FIFO state and read-only USB endpoint registers. A bounded
RAM history can be read through an added BLE characteristic after USB fails.
It changes execution timing and is not itself a proposed fix.

Built image file SHA-256:
`b8b56cbc175e7f3b1b916afb8735f3dbd87134131b87d11d9a2949c23edfe2d6`.
Runtime image digest:
`8d6ffb0a0f8ca4c68c7e777561e513f28085e7af7cc21ae75895fa73d6e1a60e`.
The actual ELF cross-reference table binds TinyUSB to both wrappers; the event
wrapper is in IRAM. Host checks pass: 269 existing firmware tests plus 102
subtests, one skipped; three observer tests; all six required hardware-repository
verification groups. The private image and signed-production rollback each pass
the canonical NVS-preserving writer's dry-run with external digest pins.

The diagnostic write and required power cycle completed. The canonical writer
verified its write, and an independent runtime probe passed all nine checks:
exact identity, firmware version, both running-image digests, configuration and
zero preservation, sensor/IMU health, and zero device error flags. The 20 KiB NVS
region was read and hashed before the write. The installed core's `versions.txt`
pins upstream TinyUSB to `a0e5626bc50d484a23f33000c48082179f0cc2dd`; its headers
report 0.16.0. This is insufficient to identify the DCD implementation: the
Arduino library builder supplies its own `components/arduino_tinyusb/src/`
override. The override source and actual ELF disassembly are retained separately.

A 60.035-second SDK-free diagnostic health run contains 15,007 tactile, 30,016
IMU and 7,504 magnetometer samples, with no sequence, framing, time-order or
device-drop anomalies. BLE reads at idle and during a second 30-second stream
confirmed that the IN/OUT and CDC counters advance. The first BLE probe used an
environment without `bleak` and failed before connecting; subsequent probes use
the verified BLE-capable environment.

The second health run exposed a separate firmware output issue: BLE connect and
disconnect callbacks insert 16 and 42 ASCII bytes into the USB TAG stream
(`#BLE connected` and `#BLE disconnected; advertising restarted`). The saved
bytes match unconditional `Serial.println` calls in the unchanged production
sketch. All complete samples in this run retain continuous sequences, but its
58 unframed bytes and 64-byte partial final frame prevent a clean framing claim.
This is separate from the earlier USB protocol failures, which occurred without
these BLE connections. The long diagnostic run starts with BLE disconnected and
only attempts BLE collection after more than one second of USB silence.

## Instrumented results

The original observation image completed 900.036 seconds with PING at 1 Hz:
225,009 tactile, 450,019 IMU and 112,505 magnetometer samples; zero framing,
sequence, device-drop, deadline-miss or USB completion errors; zero observer
loss. This single completed run does not invalidate the production failures.

Two subsequent stress comparisons keep the same image, glove, cable, port,
sensor setup and reader, while sending a 10-byte OUT payload every 100 ms:

| Payload | Host data reception before silence | USB IN errors | Last preceding OUT completion to first IN error |
| --- | --- | --- | --- |
| `LINK PING\n` | 4.211 s | 384 × `-EPROTO` | 667 µs |
| Nine spaces plus newline | 30.223 s | 384 × `-EPROTO` | 748 µs |

The whitespace command trims to an empty line and returns before PING handling.
No recovery authorization is inherited at the beginning of either stream. Thus
the 10 Hz reproduction does not require PING parsing or authorization. It does
not establish the failure rate for a 1 Hz host. Both captures preserve continuous
complete frames with zero unframed/trailing bytes before reception stops. The
first whitespace-run error reports 64 transferred bytes; later failures and the
first PING-run error report zero. Host maximum read gaps are 0.925 and 0.980 ms;
neither has lost usbmon events. Each reader observes another 12 seconds of
silence before closing; postflight STOP writes time out.

Both BLE reads recover a coherent, frozen 25-sample history covering 480 ms,
including the transition into failure. The histories agree on the failed state:

- One IN transfer remains submitted without a completion. TinyUSB IN busy stays
  true, its software TX buffer has no available space, and the producer queue
  fills to 128 frames before device-reported drops increase.
- IN completion and CDC TX callback counts both stop. OUT completion/CDC RX
  counts and received-byte counts continue, as does the sensor sample counter.
- `DIEPCTL4=0x80488040`: endpoint enabled, programmed STALL bit clear.
  `DIEPINT4=0x2090`: no transfer-complete bit. `DIEPTSIZ4=0x00080000` and
  `DTXFSTS4=240` remain unchanged throughout the stalled history.
- USB reset count is constant within each incident. The later targeted USB
  reset restores query responses with preserved firmware/configuration/zero.

The recorded state is stronger evidence than a host timeout alone: progress stops
at the device IN transaction/completion path, before either the SDK parser or
recorder. It is not proof of a particular electrical defect or an interrupt race.
In particular, pending hardware packet state must not be described as a proven
lost completion interrupt. The two directional clocks also show why incoming
traffic can keep the release's shared RX/TX liveness policy satisfied while
outgoing data is stuck; recovery behavior and the initiating fault are distinct.

An additional observation build at hardware commit `4702570a` adds an explicitly
requested, read-only FIFO-allocation characteristic without changing the timed
ring or USB policy. File SHA-256:
`8dc93d93f369e60998da9e5dbba3c1129d8de42e93953c3111c9f2f658bc865e`;
runtime digest:
`690e13905805f88f114d5827f895b33af2e73413d3ad2d9f925f5601d5874dfb`.
Its four observer host tests, seven ELF/image checks and five routed repository
verification groups pass. After the operator's power cycle, the runtime probe
passed all nine identity/digest/configuration/calibration/health checks. The
read-only BLE characteristic returned the following live configuration:

| Register / field | Observed value | Interpretation |
| --- | --- | --- |
| `DIEPCTL4.TXFNUM` | 1 | CDC IN endpoint 4 selects FIFO 1 |
| `DIEPTXF1` | `0x01000200` | 256 words starting at word 512 |
| `DIEPTXF4` | `0x002f0044` | 47 words starting at word 68 |
| `GRXFSIZ` | 52 | Receive FIFO allocation in words |
| `GNPTXFSIZ` | `0x00100034` | 16 words starting at word 52 |
| `DTXFSTS4` at idle | 256 | Available words for the selected transmit FIFO |

The [Espressif ESP32-S3 TRM, revision 1.8, section 32.3.3, page 1231](https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf)
specifies a total 256-word / 1,024-byte FIFO RAM. Thus the selected FIFO 1's
recorded allocation is outside that range. The
[Arduino library-builder DCD override](https://github.com/espressif/esp32-arduino-lib-builder/blob/3eb9cb06afb534d37f47e98bca9f45e99126f81a/components/arduino_tinyusb/src/dcd_esp32sx.c)
selects an incrementing FIFO number in the endpoint control register, while
writing the allocation register indexed by endpoint number. Disassembly of the
actual observation ELF independently confirms this indexing. The discrepancy
is confirmed; physical address aliasing, corruption and its causal relationship
to the protocol failures are not yet established.

A second 10 Hz whitespace run on this read-only image again stopped receiving:
432,983 bytes, 2,249 / 4,499 / 1,125 complete samples, 9,167 successful IN
completions followed by 384 `-EPROTO` completions, and 115 successful OUT
completions. The capture including the final silence observation lasted
21.052 seconds. Its automatic post-fault BLE read failed with GATT
`UNLIKELY_ERROR`; it must not be described as a recovered frozen history.

Hardware commit `b11a44f5` adds a separate, explicit FIFO-allocation intervention.
After the original endpoint-open routine, it copies the observed 47-word
allocation into the selected FIFO 1 register only if the endpoint, selected FIFO,
old allocation and proposed allocation all match the observed configuration.
The compiled wrapper contains one guarded register store before transfers are
scheduled. Five host tests, eight ELF/image checks and five routed repository
verification groups pass, and the committed generated source/headers/build flags
match those used to compile the image. File SHA-256:
`c981f789b385a0c5c96ba5bb41958518371150bc91653dc2e461d9014d7ee571`;
runtime digest:
`8f8451a9515f6797617d01165749d1fe52aade206207c90fe14bc9babdac50c9`.
The first installation attempt was deferred because Pi SSH/mDNS/IP reachability
was lost before any write. The operator subsequently confirmed that the Pi had
been accidentally powered off and restored power. This interruption must not be
counted as a spontaneous glove or Pi crash. The failed BLE retry has no saved
result; volatile history was lost on the power cycle. Earlier completed files
survived and were archived.

After reconnecting, the Pi was idle (`DEVICE_WAIT`, recording elapsed zero). Its
automatically restarted capture agent and ModemManager were stopped again. The
read-only diagnostic passed all nine runtime/settings/health checks. The FIFO
intervention then passed write/hash verification with a fresh NVS backup. After
the operator power cycle, the writer rebound the exact USB target and returned
zero; the independent runtime probe passed all nine checks. BLE readback changed
only `DIEPTXF1` (to `0x002f0044`), `DTXFSTS4` (to 47 available words) and the
observation timestamp. Endpoint 4 still selects FIFO 1; all other reported
configuration registers are equal to the prior read-only image.

The separately pinned intervention completed both predefined 300-second tests:

| 10 Hz OUT payload | Duration | Complete tactile / IMU / mag samples | Successful IN / OUT completions | USB errors / observer losses |
| --- | --- | --- | --- | --- |
| Nine spaces plus newline | 300.000843 s | 74,998 / 149,998 / 37,500 | 283,870 / 3,000 | 0 / 0 |
| `LINK PING\n` | 300.040735 s | 75,010 / 150,021 / 37,505 | 282,015 / 3,001 | 0 / 0 |

Both runs have continuous complete-frame sequences, no backwards timestamps,
malformed headers or unframed bytes, and zero capture-window increments in device
sample drops and deadline misses. Maximum userspace read gaps are 0.762 and
0.745 ms. Immediate postflight checks preserve MCU boot, firmware, configuration
and calibration. The whitespace capture ends with 45 bytes of a valid 133-byte
next tactile frame (sequence 74,998), cut at the requested capture boundary;
its saved tail is independently checked. The PING capture has no partial tail.
These are bounded single-glove results, not a fleet or unlimited-duration claim.

After both tests, the additional BLE read timed out before returning an identity
or registers. There is therefore no claimed final BLE snapshot. A subsequent
USB runtime probe still passed all nine digest/identity/settings/health checks.
The BLE timeout remains separate and unresolved.

Independent examination of the signed production application also confirms the
same allocation indexing. Its 54-byte allocation calculation/store block matches
uniquely in the production image; disassembly locates the function at
`0x420848e4`. The endpoint descriptor is decoded into register `a8`, FIFO selection
uses `a10`, and the allocation store remains indexed by `a8`. The USB-register
base literal is `0x60080000`. The private `check_production_dcd_bytes.py` reruns the
pinned binary checks; its analysis-only ELF wraps unmodified IROM bytes and is
never used for flashing. Thus this indexing is present in delivered 0.9.16,
not merely introduced by diagnostic instrumentation.

## Signed-production return and rc4 verification

The operator completed the USB power cycle on September 20. Independent readback
passed all nine checks of signed 0.9.16 runtime hashes, identity, complete CONFIG,
stored zero and device health. The same physical USB location and USB/logical
identity were verified before the predefined 300-second return comparison.

The original 0.9.16 image **failed again with the same 10 Hz whitespace payload**.
Data stopped after 52.322514 seconds; capture ended at 64.352801 seconds after the
12-second silence observation. The host received 2,516,786 bytes, exactly matching
the successful kernel bulk-IN byte count. Independent decoding found 13,074
tactile, 26,149 IMU and 6,537 magnetometer samples, without malformed/unframed or
trailing bytes, sequence anomalies or backwards timestamps in the received
prefix. The maximum userspace read gap was 0.729 ms. This validates the prefix,
not the requested duration.

usbmon recorded 384 IN `-EPROTO` completions, 56,550 successful IN completions and
548 successful OUT completions in the capture window, with zero observer losses.
OUT traffic continued after IN stopped. The final query sequence nevertheless
failed with a serial write timeout; successful kernel completions do not prove
that firmware consumed the command. A targeted USB bus reset restored the same
MCU boot; all nine independent runtime/settings/zero checks passed again. Device
counters read after recovery include 10,456 dropped samples accumulated during
the failed interval. No firmware reflash or replacement zero was used to recover.

| Matched 10 Hz whitespace condition | Last reception / duration | USB IN errors |
| --- | --- | --- |
| Read-only FIFO observer, original allocation | 9.021569 s before silence | 384 |
| Guarded FIFO-allocation intervention | 300.000843 s completed | 0 |
| Signed production 0.9.16 restored | 52.322514 s before silence | 384 |

All three use the identical SDK-free reader script, SHA-256
`718797a608ef27fc469396dfd1010198ffdde57a29a6d4cbf71cf4ee47cccfd0`, the same
300-second request, payload, period and device/USB location. The intervention
also completed the separate 300-second PING test above. Combined with register
readback and the production-binary check, this strongly supports the device USB
FIFO-allocation defect as causal for this Pi IN-stall signature. It does not
prove a mechanism for every earlier Mac symptom, rule out every electrical issue
or establish long-duration/fleet reliability. The stock and observer images are
distinct pinned builds; this is not a claim that they differ by only one binary
instruction. No signal-integrity or rail-transient measurement was performed.

The exact proposed SDK rc4 wheel was then installed in a fresh Pi environment:
SHA-256 `99a4ec85194dc03a90c0923fa93fc5930c0fd5e831a1156c8e72d46dfec8c395`,
source `425346d3d979db742a767c19fd133f3c49efb493`. All 14 installed modules match
the candidate source, dependencies pass `pip check`, and all 499 offline tests
pass on Linux/aarch64, with 11 physical tests deselected. NumPy 2.5.3, pyserial
3.5 and Bleak 3.0.2 match the preceding Pi SDK environment.

A requested 60-second SDK recording completed in 60.109580 seconds and saved
15,026 tactile, 30,053 IMU and 7,513 magnetometer samples. Independent decoding
of retained USB bytes matches every saved sequence, timestamp and payload.
The episode is complete with stop reason `duration`; all recorded sequences are
continuous. The capture has zero USB errors and zero observer losses, 60/60
successful PING completions, and no new device drops or deadline misses. The
short-write/retry counter increased by 21,677; this is retained as backpressure
evidence, not mislabeled as either zero retries or lost samples on 0.9.16.

The tty retained its original HUPCL setting; no explicit HUPCL change was requested.
After SDK close and a 20.000643-second closed observation window, independent
queries confirm the same MCU boot, firmware, configuration and calibration.
This bounded pass does not erase the reproduced stock-firmware failure or qualify
the Mac/Linux two-hand target. The capture agent and ModemManager are restored
to active, agent health responds `ok`, and the unused usbmon module is unloaded.

## Replacement-glove comparison — September 20

The operator replaced the left glove with a different right glove on the same
Pi USB location `2-1:1.0`. Independent IDENT and FWINFO queries report the same
signed production 0.9.16 application digest
`b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897`
and hardware revision `RDR02_FLEX5_REV_D_TIA`. CONFIG differs only in channels,
device ID, logical serial and hand side. No firmware, configuration or zero
write was performed. Cable identity was not independently verified.

Both replacement-glove runs use the exact SDK-free acquisition script and
300-second request above, with nine spaces plus newline every 0.1 seconds.
Each starts after a targeted USB bus reset. The MCU boot identity remains
unchanged across the baseline, both captures and final recovery.

| Production firmware / physical glove | Last reception | Received bytes | USB IN protocol errors | Observer drops |
| --- | --- | --- | --- | --- |
| Original left, return comparison | 52.322514 s | 2,516,786 | 384 | 0 |
| Replacement right, run 1 | 47.912148 s | 2,304,923 | 384 | 0 |
| Replacement right, run 2 | 15.837773 s | 760,214 | 384 | 0 |

Both new runs end through the 12-second silence observation and their final
serial query times out. Successful kernel IN bytes exactly match userspace
bytes. Complete received frames have no sequence anomalies, backwards
timestamps, malformed headers or unframed bytes; run 1 ends with 45 trailing
bytes and run 2 with none. These are failed captures, not loss-free recordings.
Maximum userspace read gaps are 0.718 and 0.688 ms respectively.

This establishes the same Pi failure signature on **two physical gloves**
running identical firmware, rather than only one unit. The defect-containing
application is shared; the time to failure varies even on the same unit.
It does not establish an all-glove failure rate, deterministic failure on every
session, identical live FIFO registers on the replacement, or other firmware
versions' behavior. The FIFO intervention was tested only on the original glove;
no diagnostic firmware was installed on this replacement.

Final targeted USB reset and readback preserve the replacement's firmware,
complete CONFIG, stored zero and MCU boot. Both Pi services are restored,
agent health is `ok`, the right glove is connected in `DEVICE_WAIT` with zero
recording elapsed time, and usbmon is unloaded.

## Third-glove comparison — September 20

A second replacement, this time a left glove, independently reports the same
signed production application digest and hardware revision as both prior units.
It uses the same Pi USB location and byte-identical SDK-free acquisition script,
300-second request and 10 Hz whitespace traffic. Cable identity is not separately
verified. No firmware, configuration or zero write is performed. Both runs start
after a targeted USB reset; MCU boot identity remains unchanged.

| Third physical glove | Last reception | Received bytes | USB IN protocol errors | Observer drops |
| --- | --- | --- | --- | --- |
| Run 1 | 5.613579 s | 269,383 | 384 | 0 |
| Run 2 | 40.043704 s | 1,924,858 | 384 | 0 |

Both runs stop after 12 seconds of observed silence; the final serial query
times out. Successful kernel IN bytes equal userspace bytes. The received
prefixes have no complete-frame sequence anomalies, backwards timestamps,
malformed headers, unframed or trailing bytes. Maximum userspace read gaps are
0.392 and 0.781 ms. These continuous prefixes do not make either capture a pass.

The stock-firmware failure is now reproduced on **three physical gloves**.
It remains variable in time and does not prove that every glove or every session
will fail. Neither replacement received the diagnostic intervention; their live
FIFO registers were not measured. Final reset/readback preserves firmware,
complete CONFIG, stored zero and MCU boot. Both services are active, agent health
is `ok`, the left glove is connected in `DEVICE_WAIT` with zero recording elapsed,
and usbmon is unloaded.

The separate [uploaded-recording audit](2026-09-20-field-usb-0916.md) confirms
historical USB reader failures in captures labeled 0.9.16. Historical firmware
hashes and FIFO traces are absent, so it does not assign all field failures to
this allocation defect.

## Evidence

Private evidence is retained under
`acceptance-results/qualification-20260919-toz3249b/glove-b/ping-ab-20260919/`
and `/home/ogpi/oglo-debug-20260919/ping-ab/` on the Pi. It includes source snapshots,
RAW input, host read/write timestamps, binary and derived usbmon records, drop
counts, complete device journals, recovery snapshots, build/ELF checks and both
flash dry-runs. Identifiers and calibration arrays are kept out of this public
report. The baseline archive has 139 independently hash-verified files,
867,623,104 uncompressed bytes, archive SHA-256
`d37608f016afea0e4e6fac8de3df2b738f13e901a14b23deca3ebda2a2194c77`.
The first diagnostic archive contains 118 locally extracted and individually
hash-verified files, 396,462,876 uncompressed bytes, archive SHA-256
`50238ed2a04fc70a1f81e555033c60444af5185b64d6d9fbcca0068af54cd43d`.
The initial services were restored and verified after the baseline comparison,
then stopped again for diagnostics. The additional-register archive contains 38 locally extracted and individually
hash-verified files, 13,016,032 uncompressed bytes, archive SHA-256
`24bdca0f2232d0d2a034e7549a96d024ea4cd8ec2e8c4b12671a45f81f5cb88d`.
The intervention archive contains 60 locally extracted and individually
hash-verified files, 249,624,913 uncompressed bytes, archive SHA-256
`cb55643fa99c23af7d2cf883dddd52aecc43b3f4200708461bd4f0053551f917`.
It includes the final failed BLE attempt; the later successful USB probe is
retained separately. The production-return archive has 143 individually
hash-verified files, 59,923,645 uncompressed bytes, SHA-256
`16b89f12b76593bab7f88bbe82e9994ffbda699b8d3a829a68bc5ab8748737af`.
It includes the failed return comparison, runtime and journal readback, final rc4
package/tests/recording/independent sample validation and restored-service checks.
The local `check_return_evidence.py` verifies archive member hashes, matched
conditions, the fail/complete/fail result, SDK recording checks and restoration.

Replacement-glove evidence is retained separately under
`acceptance-results/qualification-20260919-toz3249b/glove-c-20260920/`.
Its archive contains 56 individually hash-verified files, 31,197,409 uncompressed
bytes, SHA-256
`adaa1c2233a99d1328490536fa8624a9a3d24dd033a6b7eb858b5980c1343d78`.
The local `check_evidence.py` checks archive hashes, distinct physical identities,
equal firmware, exact acquisition conditions, kernel/userspace byte parity,
both reproduced failures and final settings/service restoration.

Third-glove evidence is retained under
`acceptance-results/qualification-20260919-toz3249b/glove-d-20260920/`.
Its archive contains 56 individually hash-verified files, 24,528,478 uncompressed
bytes, SHA-256
`035ef8250b8435d63d8e1e4797b35f57bb10a9ec0068b604bcd45e62bce2faac`.
Its `check_evidence.py` independently checks identity, equal firmware, acquisition
conditions, both failed captures, byte parity and settings/service restoration.
