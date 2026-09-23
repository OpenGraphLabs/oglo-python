# Recorded test packets

Each `.bin` file is one packet captured from a physical glove. Its matching
`.expected.json` describes the capture and expected decoded values.

The expected values come from a separate reference decoder in
[`tools/capture_vectors.py`](../../tools/capture_vectors.py). This keeps the SDK
from testing itself against its own answers.

## Where the packets came from

The checked-in packets were captured on 2026-08-09 from a left glove running
firmware 0.9.10/schema 6. Its serial is replaced with `OGLO-L-GOLDEN` in public
metadata. Packet bytes and decoded values are unchanged.

The tactile packet contains nonzero values. Tests also decode a known
`0x123, 0xabc` pair to check the packed 12-bit layout independently.

## Replacing the packets

The capture tool requires tactile and IMU packets, plus magnetic packets when
`has_mag=true`. It writes nothing if capture is incomplete. It removes older
`tag_*_<length>b` variants only after the replacement set is ready.

This folder contains supported tagged USB packets only. Legacy interleaved BIN
packets are not part of the SDK.
