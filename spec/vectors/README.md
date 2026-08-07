# Golden wire vectors

Each `.bin` is one whole packet captured from a physical glove. Its paired
`.expected.json` records the capture metadata and the result from the independent
reference decoder in `tools/capture_vectors.py`. The SDK decoder does not generate
its own expected answers.

The currently checked-in packets were captured from physical glove
`OGLO-R-TEST04` running firmware 0.9.9. The tactile capture is an all-zero clean
frame, so it proves the real packet length/framing but does not independently excite
every packed12 nibble. `tests/test_capture_vectors.py` therefore also pins a literal
non-zero `0x123, 0xabc` pair through the independent reference decoder.

The capture tool requires tactile and IMU packets, plus magnetometer packets when the
board reports `has_mag=true`. It writes nothing on an incomplete capture and removes
obsolete `tag_*_<length>b` variants only after the replacement set is ready.

`usb_frame_v6_191b.*` is a physical 0.9.9 capture of the legacy interleaved BIN
stream retained as provenance for the browser viewer and hardware repository. The
SDK runtime intentionally does not decode BIN, and `tests/test_vectors.py` therefore
does not treat that file as a supported decoder contract.
