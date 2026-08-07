"""Pure decoders: bytes in, values out. No I/O, no device, no state.

This module exists so the parser layer can be tested without hardware. Everything
here is a function of its arguments, which is what makes the golden vectors in
`spec/vectors/` possible: the same bytes must decode to the same values forever.

The public contract is documented in `docs/02_data_reference.md` and locked by the
captured vectors under `spec/vectors/`. The implementation was also read back from
the firmware source (`oglo_rdr02_tia.ino`, FW 0.9.9) rather than inferred from prose
alone.

There is one supported wire contract: firmware 0.9.9+, schema 6. USB is the tagged
stream with packed12 tactile payloads; BLE is the packed schema-6 notification.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

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

# Tagged USB stream (STREAM TAG ON).
TAG_MAGIC = b"\xa5\x5a"
TAG_HDR_LEN = 13
TAG_TACTILE, TAG_IMU, TAG_MAG = 1, 2, 3

#: 80 taxels x 12 bits, the only tactile payload supported by firmware 0.9.9+.
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
    (``packTaxels12`` in the sketch). Firmware 0.9.9 uses this packing for both
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


@dataclass(frozen=True)
class ImuPacket:
    seq: int
    t_us: int
    accel: Tuple[float, float, float]  # g
    gyro: Tuple[float, float, float]  # deg/s
    raw: Tuple[int, int, int, int, int, int]
    host_received_ns: Optional[int] = None


@dataclass(frozen=True)
class MagPacket:
    seq: int
    t_us: int
    field: Tuple[float, float, float]  # gauss
    raw: Tuple[int, int, int]
    host_received_ns: Optional[int] = None


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
    packets: List[object] = []
    malformed = 0
    i = 0
    n = len(buf)
    while True:
        j = buf.find(TAG_MAGIC, i)
        if j < 0:
            # Keep one byte: the magic may straddle this read and the next.
            return packets, buf[max(i, n - 1):], malformed
        if n - j < TAG_HDR_LEN:
            return packets, buf[j:], malformed
        ptype = buf[j + 2]
        plen, seq, t_us = struct.unpack_from("<HII", buf, j + 3)
        if not _tag_len_ok(ptype, plen):
            malformed += 1
            i = j + 2  # bad header; resync past this magic
            continue
        end = j + TAG_HDR_LEN + plen
        if end > n:
            return packets, buf[j:], malformed
        payload = buf[j + TAG_HDR_LEN:end]
        pkt = _decode_tagged(ptype, seq, t_us, payload)
        if pkt is not None:
            packets.append(pkt)
        i = end


def _tag_len_ok(ptype: int, plen: int) -> bool:
    if ptype == TAG_TACTILE:
        return plen == TAXEL_PACKED_LEN
    if ptype == TAG_IMU:
        return plen == TAG_IMU_LEN
    if ptype == TAG_MAG:
        return plen == TAG_MAG_LEN
    return False


def _decode_tagged(ptype: int, seq: int, t_us: int, payload: bytes):
    if ptype == TAG_TACTILE:
        return TactilePacket(seq=seq, t_us=t_us, counts=unpack12(payload))
    if ptype == TAG_IMU:
        raw = struct.unpack_from("<6h", payload, 0)
        return ImuPacket(
            seq=seq,
            t_us=t_us,
            accel=tuple(v / ACCEL_LSB_PER_G for v in raw[:3]),
            gyro=tuple(v / GYRO_LSB_PER_DPS for v in raw[3:]),
            raw=raw,
        )
    if ptype == TAG_MAG:
        raw = struct.unpack_from("<3h", payload, 0)
        return MagPacket(
            seq=seq,
            t_us=t_us,
            field=tuple(v / MAG_LSB_PER_GAUSS for v in raw),
            raw=raw,
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
