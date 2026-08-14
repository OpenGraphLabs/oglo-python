# OGLO diagnostics v1 contract

Status: **implementation-contract-not-hardware-captured**.

This is the Phase 0 schema/vector freeze only. It makes no runtime parser, USB,
firmware, or SDK behavior change. `contract.json` freezes TAG1, TAG2, typed control
frames, admission/state rules, CONTEXT canonicalization, and the canonical manifest
preimage. `manifest.json` has an explicit `contract_inputs` hash view and a separate
complete generated-vector digest inventory, avoiding START_ACK hash self-reference.

Existing `spec/vectors/tag_*.bin` files have `physical_capture` provenance. All
files in `vectors/` are deterministic synthetic conformance evidence, not board
captures. At default rates, TAG2 estimates 49,937.5 B/s (about 49,938): tactile
250 Hz plus IMU 500 Hz batched by 4 plus MAG 125 Hz batched by 4, excluding boundary
flush partial batches/control frames. This is an unqualified estimate, not a
throughput PASS: real hardware jitter and throughput capture remain required.

Regenerate with `python tools/generate_diagnostics_contract.py`; verify without
writing with `python tools/generate_diagnostics_contract.py --check`.

This directory is the only TAG2 protocol source of truth. Any SDK-facing
projection such as `spec/TAG_V2.json` must pin this manifest's
`contract_sha256` and `vector_set_sha256` and reproduce its header, sequence,
and typed-binary negotiation fields. An independent or ASCII-ACK TAG2 contract
is a conformance failure.
