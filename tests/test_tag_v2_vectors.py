"""Static vectors for the approved TAG v2 wire contract.

Unlike ``tag_*.bin``, these are not claimed to come from hardware. They pin the host
implementation while firmware is being built; a release gate must replace or
supplement them with captures from the final 0.9.13 image.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

from oglo import _wire as w


VECTOR_PATH = Path(__file__).resolve().parent.parent / "spec" / "TAG_V2.json"


def test_canonical_tag_v2_contract_and_vectors_decode_to_the_locked_values():
    document = json.loads(VECTOR_PATH.read_text())
    assert document["schema_version"] == 1
    assert document["status"] == "implementation-contract-not-hardware-captured"
    assert document["frame"] == {
        "magic_hex": "a55b",
        "header_format": "<2sBHIQ",
        "header_len": 17,
        "payload_length": "payload_bytes_only",
        "crc32": {
            "algorithm": "CRC-32/ISO-HDLC",
            "field_format": "<I",
            "field_len": 4,
            "coverage": "header_and_payload",
            "polynomial_reflected_hex": "edb88320",
            "init_hex": "ffffffff",
            "xorout_hex": "ffffffff",
            "reference": "zlib.crc32",
        },
    }

    decoded = {}
    for vector in document["vectors"]:
        frame = bytes.fromhex(vector["frame_hex"])
        (crc32,) = struct.unpack("<I", frame[-4:])
        assert crc32 == zlib.crc32(frame[:-4]) == int(vector["crc32_hex"], 16)
        packets, remainder = w.iter_tagged_v2(frame)
        assert remainder == b"" and len(packets) == 1
        decoded[vector["name"]] = (packets[0], vector["expected"])

    tactile, expected = decoded["tactile"]
    assert isinstance(tactile, w.TactilePacket)
    assert (tactile.seq, tactile.device_time_us, tactile.t_us, tactile.counts) == (
        expected["seq"],
        expected["timestamp_us"],
        expected["t_us"],
        expected["counts"],
    )

    for name, packet_type in (("imu", w.ImuPacket), ("mag", w.MagPacket)):
        packet, expected = decoded[name]
        assert isinstance(packet, packet_type)
        assert (packet.seq, packet.device_time_us, packet.t_us, list(packet.raw)) == (
            expected["seq"],
            expected["timestamp_us"],
            expected["t_us"],
            expected["raw"],
        )
