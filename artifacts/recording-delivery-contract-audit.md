# Recording-to-delivery contract audit

2026-09-23. Read-only source review supporting the localhost collection plan; no recording, importer, or delivery was executed.

## Evidence and conclusion

The initial plan checked the SDK's own recording format but did not establish end-to-end delivery readiness. Recording must enforce a versioned capture contract before and during collection. Export packages already validated source data; it cannot reconstruct missing measurements.

Evidence inspected:

- [Annotation handoff discussion](https://opengraph-labs.slack.com/archives/C0C30LKMSEL/p1789810726242279): Jerry requests video plus tactile per episode, with both host-receipt and device timestamps on every video frame and tactile sample.
- [SDK data specification](../docs/09_data_specification.md): schema-3 glove rows, calibration, camera timestamps, and example session layout. It explicitly separates backend upload/session registration from SDK recording.
- [Webcam capture](../examples/camera_glove/capture.py): records host read/arrival times but sets native camera timing to null. It therefore cannot fully meet the Slack timing request as implemented.
- [OVISION capture guide](../examples/camera_glove/OVISION.md): preserves native exposure timing, stereo metadata, camera IMU, and calibration; its Linux-specific capture path does not prove camera/glove exposure synchronization.
- Local `og-skill` checkout at `c957de87`, especially `pi-v3/internal/contract/files.go`, `pi-v3/internal/services/recording/artifacts.go`, `server/src/application/recording/recording.service.ts`, and `pipeline/export/{profiles/base.py,gdm/_inputs.py,profiles/registry.py}`. These demonstrate distinct source capture, registration, and processed customer-delivery contracts. This is checkout evidence, not a claim about deployed services.
- Local SDK checkout at `e564b9b`, including `src/oglo/{_record.py,_device.py,_jsonl.py,data.py}`. RAW recording preserves original values and derives CLEAN using the capture's calibration; recording an already-CLEAN stream cannot restore RAW later.

## Source data the recorder must prepare

| Data | Capture/finalization responsibility | Verification |
| --- | --- | --- |
| Ego video | Preserve recorded resolution, codec, frame order, camera identity, and original native stream where supported | Decode video; reconcile frame count against timing rows and writer counts; record actual duration |
| Per-frame timing | Host receipt timestamp and native camera timestamp, with units, clock domain, and meaning; native exposure/sequence metadata when available | One timing row per decoded frame, ordered indices, valid clock semantics; native timing required for the Slack handoff profile |
| RAW tactile, per selected hand | Full-rate 80-channel ADC values, names/order, sequence, device time including wrap information, host arrival time, and loss counters | Replay validation, channel shape/order, sequence/gap checks, exact integer timestamps |
| CLEAN tactile | Derive from the preserved RAW stream using that episode's immutable recipe | Recompute CLEAN and compare; no unexplained filtering, fabricated samples, or in-place RAW changes |
| Glove calibration | Baseline and noise for all 80 taxels, threshold, transform, channel layout, zero validity, calibration time/identity, glove serial/side | Read back before capture; snapshot per episode; verify matching device and recipe; do not read the current glove later to describe an old recording |
| Glove motion | Raw wrist accelerometer/gyro and available magnetometer channels, timing, units/scale metadata | Preserve SDK files, including valid empty optional magnetic streams; never replace missing required raw values with zeros |
| Camera-specific source files | Native stereo metadata, per-eye geometry/order, camera intrinsics/distortion/extrinsics and calibration provenance, and camera IMU if exposed/required by the target | Check real adapter outputs and profile requirements; unknown calibration remains unknown, never an invented identity transform |
| Session and episode identity | Stable IDs, task description, actual setup/hand inventory, device/firmware/software versions, stream names, relative paths, clock domains, start/end boundaries, requested and observed rates | Versioned schema; referenced files exist; no path collisions or reuse across episodes |
| Timing anchor and action log | Persist paired host monotonic/wall times and their semantics before streams start; timestamp start/stop/trigger actions on the host | Crash leaves an identifiable incomplete recording; never compare unrelated device clocks directly or treat trigger receipt as sensor exposure time |
| Quality and inventory | Counts, missing samples, stream gaps, first/last times, common coverage, stop reason, errors, file sizes, checksums, validation version | Finalize atomically after file checks; retain failed captures separately; verify archive contents against the sealed inventory |

No nominal FPS or frame-index calculation substitutes for measured timestamps. Save each sensor at its own rate. Approximate time matching may be generated as a derived artifact with tolerance and unmatched rows; it must not overwrite the source streams or be described as validated synchronization.

## Delivery levels and existing contract gaps

1. **Portable source archive:** a self-contained dataset index plus original camera/glove episode folders, calibration, timing, action/provenance metadata, validation report, and checksums. Valid one-hand and host-only recordings can exist here with limitations explicitly represented.
2. **Requested annotation handoff:** apply the Slack request as the default handoff requirement: video/tactile per episode and both host/device timing. The current ordinary-webcam adapter lacks native camera timestamps. Preflight must show this before recording and block the “ready for annotation handoff” claim; local source recording may still be offered explicitly. Supporting that handoff with such hardware needs a capable camera adapter or an explicitly revised recipient contract, not inferred timestamps.
3. **Production platform ingestion:** use a tested, versioned mapping into the applicable platform contract. The SDK's nested `oglo-camera-example.v2` manifest is not the platform session descriptor. The inspected phone capture-v2 registration path requires its own inventory and explicitly excludes treating Pi stereo captures as phone captures. A new source importer/profile needs a fixture-based integration check before platform compatibility is claimed. No upload is part of this planning task.
4. **Customer-ready output:** comes after downstream synchronization, annotation, derived data, review, and customer-format validation. The inspected export profiles read processed Clip tables and proof/provenance artifacts, not a raw SDK ZIP. The current GDM stereo+tactile profile names two camera views, bilateral tactile streams, stereo timing, camera calibration, and shared processed inputs. An arbitrary mono camera or single glove is not equivalent to that setup. Keep customer-specific rules outside the generic open-source recorder while capturing the source measurements those rules need.

For an OVISION production mapping, the existing Pi contract supplies concrete source names: `cam_ego.mp4`, `cam_ego.timestamps.jsonl`, `cam_ego_stereo.jsonl`, `camera_intrinsics.json`, the camera raw IMU/magnetometer files, the three camera calibration formats, `sync_point.json`, and bilateral tactile/wrist-motion files. Its SDK-to-platform normalization explicitly maps `cam_ego.stereo.jsonl` to `cam_ego_stereo.jsonl` and camera IMU names to `imu_*_raw.jsonl`. Session descriptors and finalization metadata must also be valid for the consuming path; merely passing a mandatory-file presence check does not prove synchronization/ingestion readiness. Preserve original SDK files and record any mapping in a separate delivery copy.

The recorder should preserve RAW tactile and calibration even where an intermediate inventory calls them optional: they cannot be recovered after capture, and downstream re-derivation/verification depends on them. Do not copy operational fleet threshold numbers into a universal SDK default; capture the actual validated recipe.

## Required changes to the implementation plan

**Before Start:** resolve the selected delivery profile into required capabilities, streams, hand count, timing, and calibration. Verify RAW mode and snapshot calibration/device configuration. Refuse to arm a profile whose required measurements cannot be collected. Save episode identity and timing anchor before capture begins. For a generic local recording, clearly state any downstream limitations.

**During capture:** record original samples and timing continuously, track gaps/loss, prevent device/configuration changes, and attach every stream to the same episode identity. A camera reconnect, changed calibration, or device restart must not silently continue the same timing segment. Stop/preserve partial output when required streams fail.

**On Stop:** freeze capture boundaries and distinguish intentional pedal stop from errors; close video and sensor writers; validate schema/replay, video/timestamp correspondence, RAW/CLEAN derivation, clock domains/rollover, per-stream coverage and profile-specific requirements. Store the report and inventory before publishing readiness. Mere nonempty time overlap is insufficient to prove full episode coverage; measure coverage and let the versioned profile define acceptance limits.

**On Keep:** record the operator's decision separately from integrity and delivery readiness. Proposed status dimensions: `complete`, `selection`, and `delivery_validation` per profile. Keep never overrides a failed delivery check.

**On Export:** include every source sidecar and validation artifact, copy from a sealed inventory, and verify hashes after extraction. For annotation delivery, include only kept recordings that pass that profile. Other complete recordings can be exported explicitly as source archives with a limitations report. Missing source information is never synthesized to make a package pass.

**Acceptance before UI completion:** create a small reference camera+glove episode and a matching delivery fixture. Prove record → stop → validate → export → extract → replay/decode; for any claimed backend profile, run its importer/validator locally with the same fixture. Negative fixtures must fail for missing native timing, missing calibration/RAW data, swapped hands, truncated video/JSONL, unmatched timing rows, stale clock domains, incomplete coverage, or omitted required sidecars. Add physical-camera timing checks separately from simulated browser flow tests.

## Remaining qualification

The source requirements and implementation gaps above are grounded in the inspected Slack request and local code. The external partner's actual camera capabilities, a concrete platform importer for the new archive, and an end-to-end accepted delivery fixture have not been demonstrated. These are explicit acceptance work, not assumptions that the existing SDK format already satisfies them.
