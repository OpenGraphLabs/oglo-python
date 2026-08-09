# Golden wire vectors

Each `.bin` is one whole packet captured from a physical glove. Its paired
`.expected.json` records the capture metadata and the result from the independent
reference decoder in `tools/capture_vectors.py`. The SDK decoder does not generate
its own expected answers.

The currently checked-in packets were captured on 2026-08-09 from a physical left
glove running firmware 0.9.10/schema 6. The public metadata redacts its logical
serial as `OGLO-L-GOLDEN`; raw packet bytes and decoded values are unchanged. The
tactile frame includes non-zero counts, and `tests/test_capture_vectors.py` also
pins a literal non-zero `0x123, 0xabc` pair through the independent reference
decoder so every packed12 nibble order is testable without relying on one pose.

The capture tool requires tactile and IMU packets, plus magnetometer packets when the
board reports `has_mag=true`. It writes nothing on an incomplete capture and removes
obsolete `tag_*_<length>b` variants only after the replacement set is ready.

Only the supported tagged USB contract is kept here. Legacy interleaved BIN captures
and decoders are not part of this SDK.
