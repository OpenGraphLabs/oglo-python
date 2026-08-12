"""Versioned USB TAG framing constants.

Keep the bytes and commands in one small module: changing a magic value in a parser
or a fake without changing firmware produces a stream that looks valid in tests and
can never exist on a board.  TAG v2 is additive; v1 remains the explicit fallback
for firmware whose CONFIG omits ``tag_ver_max`` or reports 1.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class TagContract:
    version: int
    magic: bytes
    header: struct.Struct
    start_command: str
    stop_command: str
    mode_name: str
    crc: struct.Struct | None = None


# Firmware 0.9.10-0.9.12: magic, type u8, payload_len u16, seq u32,
# timestamp_us u32.  Little endian, packed, no implicit alignment.
TAG_V1 = TagContract(
    version=1,
    magic=b"\xa5\x5a",
    header=struct.Struct("<2sBHII"),
    start_command="STREAM TAG ON",
    stop_command="STREAM TAG OFF",
    mode_name="tagged",
)

# Firmware contract approved for 0.9.13 implementation: the distinct magic makes
# an accidental v1/v2 decoder mismatch fail closed. The header widens timestamp
# width and every frame ends with a little-endian CRC over header plus payload.
TAG_V2 = TagContract(
    version=2,
    magic=b"\xa5\x5b",
    header=struct.Struct("<2sBHIQ"),
    start_command="STREAM TAG2 ON",
    stop_command="STREAM TAG2 OFF",
    mode_name="tagged_v2",
    crc=struct.Struct("<I"),
)

SDK_TAG_VERSION_MAX = TAG_V2.version
TAG2_ACK_PREFIX = b"#STREAM TAG2 on boot_id="
BOOT_ID_HEX_CHARS = 32
BOOT_ID_BYTES = 16
BOOT_ID_SCOPE = "one firmware boot"


def canonical_boot_id(value: object) -> str:
    """Validate the one wire representation used by CONFIG and the TAG2 ACK.

    This intentionally does not normalize integers or uppercase text. JSON numbers
    cannot carry a uint128 portably through JavaScript, and accepting a second text
    form would make CONFIG and the exact start acknowledgement different contracts.
    """
    if (
        isinstance(value, str)
        and len(value) == BOOT_ID_HEX_CHARS
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    raise ValueError("boot_id must be exactly 32 lowercase hexadecimal characters")


def parse_tag2_ack(line: bytes) -> str:
    """Parse the exact firmware ACK and return a canonical boot identity."""
    if not line.startswith(TAG2_ACK_PREFIX):
        raise ValueError("TAG2 start reply has the wrong prefix")
    value = line[len(TAG2_ACK_PREFIX):]
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("TAG2 boot_id is not ASCII") from exc
    return canonical_boot_id(text)


def tag_contract(version: int) -> TagContract:
    """Return the exact supported contract; never guess a future layout."""
    if version == TAG_V1.version:
        return TAG_V1
    if version == TAG_V2.version:
        return TAG_V2
    raise ValueError(f"unsupported TAG version {version}")
