"""Replay golden vectors captured from a real board.

`test_wire.py` pins the decoder against the specification using frames it builds
itself. This pins it against bytes a board actually emitted, which catches the case
where our reading of the spec and the firmware's behaviour disagree -- the failure
that broke a downstream consumer on 2026-07-28.

Skips cleanly when no vectors are present, so a fresh clone passes before anyone has
touched hardware. Generate them with `python3 tools/capture_vectors.py`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pytest

from oglo import _wire as w
from oglo._config import MIN_FIRMWARE, _fw_at_least

VECTORS = Path(__file__).resolve().parent.parent / "spec" / "vectors"
CASES = (
    sorted([*VECTORS.glob("tag_*.bin"), *VECTORS.glob("ble_*.bin")])
    if VECTORS.exists()
    else []
)


def jsonable(obj):
    if is_dataclass(obj):
        return {
            k: jsonable(v) for k, v in asdict(obj).items()
            if k != "host_received_ns"  # transport metadata, not part of a wire vector
            and not (k == "device_time_us" and v is None)  # absent from TAG v1
        }
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 9)
    return obj


@pytest.mark.skipif(not CASES, reason="no golden vectors yet; run tools/capture_vectors.py")
@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_vector_decodes_to_its_recorded_values(path: Path):
    expected_path = path.with_suffix(".expected.json")
    assert expected_path.exists(), f"{path.name} has no matching expected JSON"
    expected = json.loads(expected_path.read_text())
    raw = path.read_bytes()

    if path.stem.startswith("tag_"):
        decoded, rest = w.iter_tagged(raw)
        assert rest == b"", "a vector must be a whole number of packets"
        assert len(decoded) == 1
        got = decoded[0]
    elif path.stem.startswith("ble_"):
        got = w.decode_ble_notify(raw)[0]
    else:
        pytest.fail(f"unrecognised vector name: {path.stem}")

    assert jsonable(got) == expected["decoded"]


@pytest.mark.skipif(not CASES, reason="no golden vectors yet")
def test_all_checked_in_vectors_are_from_a_supported_firmware_contract():
    capture_identities = set()
    for path in CASES:
        meta = json.loads(path.with_suffix(".expected.json").read_text())["meta"]
        assert _fw_at_least(meta.get("fw_rev", ""), MIN_FIRMWARE), path.name
        assert meta.get("imu_len") == 25, path.name
        capture_identities.add(
            (meta.get("serial"), meta.get("hw_rev"), meta.get("fw_rev"), meta.get("has_mag"))
        )
    assert len(capture_identities) == 1, "golden set mixes packets from different captures"


@pytest.mark.skipif(not CASES, reason="no golden vectors yet")
def test_vector_filename_records_its_actual_packet_length():
    for path in CASES:
        encoded_length = int(path.stem.rsplit("_", 1)[1].removesuffix("b"))
        assert encoded_length == len(path.read_bytes()), path.name


@pytest.mark.skipif(not CASES, reason="no golden vectors yet")
def test_checked_in_capture_has_every_required_modality():
    prefixes = {path.stem.rsplit("_", 1)[0] for path in CASES}
    assert {"tag_tactile", "tag_imu"} <= prefixes
    metadata = [
        json.loads(path.with_suffix(".expected.json").read_text())["meta"] for path in CASES
    ]
    if any(meta.get("has_mag") for meta in metadata):
        assert "tag_mag" in prefixes


@pytest.mark.skipif(not CASES, reason="no golden vectors yet")
def test_every_checked_in_tactile_vector_is_packed12():
    """The supported 0.9.10+ contract has a 120-byte packed12 tactile payload."""
    tac = [p for p in CASES if p.stem.startswith("tag_tactile")]
    assert tac, "capture a tactile vector with tools/capture_vectors.py"
    for path in tac:
        assert len(path.read_bytes()) == w.TAG_HDR_LEN + w.TAXEL_PACKED_LEN, path.name


def test_packed12_nibble_order_is_pinned_without_using_the_pack_helper():
    """A pack/unpack round trip can agree with itself while both directions are wrong."""
    assert w.unpack12(bytes.fromhex("12 3a bc"), count=2) == [0x123, 0xABC]


def test_no_orphan_expected_json_is_silently_ignored():
    expected = {path.name.removesuffix(".expected.json") for path in VECTORS.glob("*.expected.json")}
    binaries = {path.stem for path in VECTORS.glob("*.bin")}
    assert expected == binaries
