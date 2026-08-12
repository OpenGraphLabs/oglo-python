"""Pure decoders: bytes in, values out. No I/O, no device, no state.

This module exists so the parser layer can be tested without hardware. Everything
here is a function of its arguments, which is what makes the golden vectors in
`spec/vectors/` possible: the same bytes must decode to the same values forever.

The public contract is documented in `docs/02_data_reference.md` and locked by
vectors under `spec/vectors/`. TAG v1 was read back from firmware rather than
inferred from prose. TAG v2 has a distinct magic, a 64-bit timestamp, and a CRC;
its canonical synthetic vectors live in `spec/TAG_V2.json` until physical release
evidence is captured separately.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

from ._tag_contract import TAG_V1, TAG_V2, TagContract, tag_contract

# --- constants, all confirmed against the firmware source ---------------------

NUM_FINGERS = 5
ROWS_PER_FINGER = 4
NUM_COLS = 4
TAXELS = NUM_FINGERS * ROWS_PER_FINGER * NUM_COLS  # 80
ADC_MAX = 4095

#: Sensitivities. Datasheet-confirmed against the full scale the firmware programs:
#: ICM-42688-P DS-000347 Rev 1.2 (ACCEL_FS_SEL=1 = +/-8 g, GYRO_FS_SEL=0 = +/-2000 dps)
#: and LIS3MDL DocID024204 Rev 4 Table 3 (CTRL2=0x00 = +/-4 gauss).
ACCEL_LSB_PER_G = 4096.0
GYRO_LSB_PER_DPS = 16.4
MAG_LSB_PER_GAUSS = 6842.0

# Tagged USB stream. Preserve the original aliases for downstream code that imports
# the v1 constants while exposing an unambiguous v2 contract alongside them.
TAG_MAGIC = TAG_V1.magic
TAG_HDR_LEN = TAG_V1.header.size
TAG_V2_MAGIC = TAG_V2.magic
TAG_V2_HDR_LEN = TAG_V2.header.size
TAG_V2_CRC_LEN = TAG_V2.crc.size if TAG_V2.crc is not None else 0
TAG_TACTILE, TAG_IMU, TAG_MAG = 1, 2, 3

#: 80 taxels x 12 bits, the only tactile payload supported by firmware 0.9.10+.
TAXEL_PACKED_LEN = (TAXELS * 12 + 7) // 8  # 120

TAG_IMU_LEN = 12  # ax, ay, az, gx, gy, gz
TAG_MAG_LEN = 6  # mx, my, mz

# BLE notify.
BLE_HDR_LEN = 10
#: The fixed mag slot exists in schema 6 even when the part is absent. This bit says
#: the part is fitted and the values are meaningful; without it the slot is zero-fill.
BLE_FLAG_PACKET_MAG = 0x08
BLE_FLAG_PACKED6 = 0x10
BLE_V6_STRIDE = 2 + TAXEL_PACKED_LEN + TAG_IMU_LEN + TAG_MAG_LEN + 2  # 142


class WireError(ValueError):
    """A buffer could not be decoded. Never raised for a merely incomplete buffer."""


# --- primitives ---------------------------------------------------------------


def unpack12(buf: bytes, offset: int = 0, count: int = TAXELS) -> List[int]:
    """Unpack `count` 12-bit values from three-bytes-per-two-values packing.

    The firmware writes pairs as ``a>>4 | ((a&0xF)<<4)|(b>>8) | b&0xFF``
    (``packTaxels12`` in the sketch). Supported firmware uses this packing for both
    tagged USB tactile packets and BLE notifications.
    """
    if count % 2:
        raise WireError(f"unpack12 needs an even count, got {count}")
    need = count // 2 * 3
    if len(buf) - offset < need:
        raise WireError(f"unpack12 needs {need} B at offset {offset}, have {len(buf) - offset}")
    out: List[int] = []
    o = offset
    for _ in range(count // 2):
        b0, b1, b2 = buf[o], buf[o + 1], buf[o + 2]
        o += 3
        out.append((b0 << 4) | (b1 >> 4))
        out.append(((b1 & 0x0F) << 8) | b2)
    return out


def pack12(values: Sequence[int]) -> bytes:
    """Inverse of :func:`unpack12`. Used to build test vectors, not on the hot path."""
    if len(values) % 2:
        raise WireError(f"pack12 needs an even count, got {len(values)}")
    if any(not 0 <= int(v) <= ADC_MAX for v in values):
        raise WireError(f"pack12 values must be 0..{ADC_MAX}")
    out = bytearray()
    for i in range(0, len(values), 2):
        a, b = int(values[i]), int(values[i + 1])
        out += bytes((a >> 4, ((a & 0x0F) << 4) | (b >> 8), b & 0xFF))
    return bytes(out)


# --- decoded records ----------------------------------------------------------


@dataclass(frozen=True)
class TactilePacket:
    seq: int
    t_us: int
    counts: List[int]  # length 80, order finger,row,col
    #: Host monotonic time at the transport receive boundary, not decoder time.
    host_received_ns: Optional[int] = None
    #: Present only for TAG v2. ``t_us`` remains its low u32 for API compatibility.
    device_time_us: Optional[int] = None


@dataclass(frozen=True)
class ImuPacket:
    seq: int
    t_us: int
    accel: Tuple[float, float, float]  # g
    gyro: Tuple[float, float, float]  # deg/s
    raw: Tuple[int, int, int, int, int, int]
    host_received_ns: Optional[int] = None
    device_time_us: Optional[int] = None


@dataclass(frozen=True)
class MagPacket:
    seq: int
    t_us: int
    field: Tuple[float, float, float]  # gauss
    raw: Tuple[int, int, int]
    host_received_ns: Optional[int] = None
    device_time_us: Optional[int] = None


@dataclass(frozen=True)
class BleSample:
    seq: int
    t_us: int
    counts: List[int]
    accel: Tuple[float, float, float]
    gyro: Tuple[float, float, float]
    imu_raw: Optional[Tuple[int, int, int, int, int, int]] = None
    mag: Optional[Tuple[float, float, float]] = None
    mag_raw: Optional[Tuple[int, int, int]] = None
    imu_dt_us: Optional[int] = None
    host_received_ns: Optional[int] = None


# --- tagged USB stream --------------------------------------------------------


def iter_tagged(buf: bytes) -> Tuple[List[object], bytes]:
    """Decode every whole tagged packet in `buf`.

    Returns ``(packets, remainder)``. The remainder is the trailing bytes that do not
    yet form a whole packet and must be prepended to the next read; a caller that
    discards it will desync. Junk before a magic is skipped silently, because that is
    what a mid-stream attach looks like.

    A packet whose declared length is impossible for its type is skipped rather than
    decoded, and resynchronisation restarts after its magic. Truncated frames used to
    reach parsers here and desync them, so a length that does not fit its type is
    treated as a bad frame, not as data.
    """
    packets, remainder, _malformed = iter_tagged_diagnostic(buf)
    return packets, remainder


def iter_tagged_diagnostic(buf: bytes) -> Tuple[List[object], bytes, int]:
    """Decode tagged packets and count structurally invalid TAG headers.

    Arbitrary bytes before a magic marker are normal when attaching mid-stream and
    are not counted. Once a complete header follows ``TAG_MAGIC``, however, an
    unknown packet type or a payload length impossible for that type is a malformed
    frame. The returned count lets transports make that silent resynchronisation
    visible without changing the long-standing two-value :func:`iter_tagged` API.
    """
    return _iter_tagged_diagnostic(buf, TAG_V1)


def iter_tagged_v2(buf: bytes) -> Tuple[List[object], bytes]:
    """Decode every whole TAG v2 packet in ``buf``.

    TAG v2 is deliberately a separate entry point: callers must negotiate the
    version from CONFIG and cannot make a byte stream look valid by guessing.
    """
    packets, remainder, _malformed = iter_tagged_v2_diagnostic(buf)
    return packets, remainder


def iter_tagged_v2_diagnostic(buf: bytes) -> Tuple[List[object], bytes, int]:
    """Decode TAG v2 packets and count structurally invalid v2 headers."""
    return _iter_tagged_diagnostic(buf, TAG_V2)


def iter_tagged_version_diagnostic(
    buf: bytes, version: int
) -> Tuple[List[object], bytes, int]:
    """Decode an already-negotiated TAG version; unsupported versions fail closed."""
    return _iter_tagged_diagnostic(buf, tag_contract(version))


def _iter_tagged_diagnostic(
    buf: bytes, contract: TagContract
) -> Tuple[List[object], bytes, int]:
    packets: List[object] = []
    malformed = 0
    i = 0
    n = len(buf)
    while True:
        j = buf.find(contract.magic, i)
        if j < 0:
            # Keep one byte: the magic may straddle this read and the next.
            return packets, buf[max(i, n - 1):], malformed
        if n - j < contract.header.size:
            return packets, buf[j:], malformed
        _magic, ptype, plen, seq, timestamp_us = contract.header.unpack_from(buf, j)
        if not _tag_len_ok(ptype, plen):
            malformed += 1
            i = j + 2  # bad header; resync past this magic
            continue
        payload_end = j + contract.header.size + plen
        frame_end = payload_end + (contract.crc.size if contract.crc is not None else 0)
        if frame_end > n:
            return packets, buf[j:], malformed
        payload = buf[j + contract.header.size:payload_end]
        if contract.crc is not None:
            (expected_crc,) = contract.crc.unpack_from(buf, payload_end)
            observed_crc = zlib.crc32(buf[j:payload_end])
            if observed_crc != expected_crc:
                malformed += 1
                i = j + 2
                continue
        pkt = _decode_tagged(
            ptype,
            seq,
            timestamp_us & 0xFFFFFFFF,
            payload,
            device_time_us=timestamp_us if contract.version == 2 else None,
        )
        if pkt is not None:
            packets.append(pkt)
        i = frame_end


def _tag_len_ok(ptype: int, plen: int) -> bool:
    if ptype == TAG_TACTILE:
        return plen == TAXEL_PACKED_LEN
    if ptype == TAG_IMU:
        return plen == TAG_IMU_LEN
    if ptype == TAG_MAG:
        return plen == TAG_MAG_LEN
    return False


def _decode_tagged(
    ptype: int,
    seq: int,
    t_us: int,
    payload: bytes,
    *,
    device_time_us: Optional[int] = None,
):
    if ptype == TAG_TACTILE:
        return TactilePacket(
            seq=seq, t_us=t_us, counts=unpack12(payload), device_time_us=device_time_us
        )
    if ptype == TAG_IMU:
        raw = struct.unpack_from("<6h", payload, 0)
        return ImuPacket(
            seq=seq,
            t_us=t_us,
            accel=tuple(v / ACCEL_LSB_PER_G for v in raw[:3]),
            gyro=tuple(v / GYRO_LSB_PER_DPS for v in raw[3:]),
            raw=raw,
            device_time_us=device_time_us,
        )
    if ptype == TAG_MAG:
        raw = struct.unpack_from("<3h", payload, 0)
        return MagPacket(
            seq=seq,
            t_us=t_us,
            field=tuple(v / MAG_LSB_PER_GAUSS for v in raw),
            raw=raw,
            device_time_us=device_time_us,
        )
    return None


# --- BLE notify ---------------------------------------------------------------


def decode_ble_notify(payload: bytes) -> List[BleSample]:
    """Decode one BLE notify into its samples.

    Branches on the notify's own `flags` byte, which is what the format document
    requires of a host, rather than on any version field.
    """
    if len(payload) < BLE_HDR_LEN:
        raise WireError(f"notify is {len(payload)} B, shorter than its {BLE_HDR_LEN} B header")
    count = payload[0]
    flags = payload[1]
    seq_base, t_base = struct.unpack_from("<II", payload, 2)

    if not flags & BLE_FLAG_PACKED6:
        raise WireError(f"notify flags 0x{flags:02x} are not schema-6 packed data (0x10)")
    stride = BLE_V6_STRIDE

    if not 1 <= count <= 3:
        raise WireError(f"notify sample count is {count}; schema 6 allows 1..3")
    need = BLE_HDR_LEN + count * stride
    if len(payload) != need:
        raise WireError(
            f"notify declares {count} samples ({need} B at stride {stride}) but is {len(payload)} B"
        )

    out: List[BleSample] = []
    off = BLE_HDR_LEN
    for k in range(count):
        dt = struct.unpack_from("<H", payload, off)[0]
        counts = unpack12(payload, off + 2)
        imu = struct.unpack_from("<6h", payload, off + 2 + TAXEL_PACKED_LEN)
        mx, my, mz = struct.unpack_from("<3h", payload, off + 2 + TAXEL_PACKED_LEN + TAG_IMU_LEN)
        mag = None
        mag_raw = None
        if flags & BLE_FLAG_PACKET_MAG:
            mag = (mx / MAG_LSB_PER_GAUSS, my / MAG_LSB_PER_GAUSS, mz / MAG_LSB_PER_GAUSS)
            mag_raw = (mx, my, mz)
        imu_dt = struct.unpack_from(
            "<h", payload, off + 2 + TAXEL_PACKED_LEN + TAG_IMU_LEN + TAG_MAG_LEN
        )[0]
        out.append(
            BleSample(
                # Both fields are u32 on the wire. A three-sample notify can cross
                # either rollover; exposing 2**32 here used to crash Recorder.
                seq=(seq_base + k) & 0xFFFFFFFF,
                t_us=(t_base + dt) & 0xFFFFFFFF,
                counts=counts,
                accel=tuple(v / ACCEL_LSB_PER_G for v in imu[:3]),
                gyro=tuple(v / GYRO_LSB_PER_DPS for v in imu[3:]),
                imu_raw=imu,
                mag=mag,
                mag_raw=mag_raw,
                imu_dt_us=imu_dt,
            )
        )
        off += stride
    return out


# --- loss accounting ----------------------------------------------------------


@dataclass(frozen=True)
class SeqTransition:
    """Classification of one sequence transition.

    Duplicate and backward/reset samples are anomalies, not billions of missing
    packets. Callers only advance their reference on ``first``, ``forward`` or
    ``wrap``.
    """

    kind: str  # first | forward | wrap | duplicate | backward
    missing: int = 0


def classify_seq(prev: Optional[int], cur: int, *, width: int = 32) -> SeqTransition:
    if width < 2:
        raise ValueError("sequence width must be at least 2 bits")
    mask = (1 << width) - 1
    cur &= mask
    if prev is None:
        return SeqTransition("first")
    prev &= mask
    delta = (cur - prev) & mask
    if delta == 0:
        return SeqTransition("duplicate")
    if delta < (1 << (width - 1)):
        return SeqTransition("wrap" if cur < prev else "forward", delta - 1)
    return SeqTransition("backward")


def seq_gap(prev: Optional[int], cur: int, *, width: int = 32) -> int:
    """Samples missing between two sequence numbers, wrap-safe.

    Returns 0 for the first sample of a stream. Host-side loss only; what the device
    threw away itself is a separate counter and the two must never be merged.
    """
    return classify_seq(prev, cur, width=width).missing
