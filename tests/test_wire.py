"""Decoder tests. No hardware, no I/O -- that is the point of `_wire` being pure.

Frames are built here rather than captured, so these tests pin the decoder against
the *specification*. `spec/vectors/` pins it against a *real board*; both matter, and
the two catch different mistakes. See `tools/capture_vectors.py`.
"""

from __future__ import annotations

import struct
import zlib

import pytest

from oglo import _wire as w


# --- builders (the inverse of what we are testing, kept dumb on purpose) --------


def tag(ptype: int, seq: int, t_us: int, payload: bytes) -> bytes:
    return w.TAG_MAGIC + bytes([ptype]) + struct.pack("<HII", len(payload), seq, t_us) + payload


def tag2(ptype: int, seq: int, timestamp_us: int, payload: bytes) -> bytes:
    body = (
        w.TAG_V2_MAGIC
        + bytes([ptype])
        + struct.pack("<HIQ", len(payload), seq, timestamp_us)
        + payload
    )
    return body + struct.pack("<I", zlib.crc32(body))


def ble_notify(samples, *, mag=True, seq=100, t_us=50_000) -> bytes:
    flags = w.BLE_FLAG_PACKED6 | (w.BLE_FLAG_PACKET_MAG if mag else 0)
    p = bytearray(bytes([len(samples), flags]) + struct.pack("<II", seq, t_us))
    for k, counts in enumerate(samples):
        p += struct.pack("<H", k * 4000)
        p += w.pack12(counts)
        p += struct.pack("<6h", 777, -531, -3982, -5, -8, 1)
        p += struct.pack("<3h", 3142, 678, -1107)
        p += struct.pack("<h", -1500)
    return bytes(p)


COUNTS = [500 + i for i in range(w.TAXELS)]


# --- 12-bit packing ------------------------------------------------------------


def test_pack12_roundtrips_the_full_12_bit_range():
    vals = [(i * 53) % 4096 for i in range(w.TAXELS)]
    assert w.unpack12(w.pack12(vals)) == vals


def test_pack12_is_three_bytes_per_two_values():
    assert len(w.pack12(COUNTS)) == w.TAXEL_PACKED_LEN == 120


def test_unpack12_rejects_a_short_buffer_instead_of_reading_past_it():
    with pytest.raises(w.WireError):
        w.unpack12(w.pack12(COUNTS)[:-1])


# --- tagged stream -------------------------------------------------------------


def test_tagged_tactile_decodes_the_schema6_packed_payload():
    pkts, rest = w.iter_tagged(tag(w.TAG_TACTILE, 7, 1234, w.pack12(COUNTS)))
    assert rest == b"" and len(pkts) == 1
    assert (pkts[0].seq, pkts[0].t_us, pkts[0].counts) == (7, 1234, COUNTS)


def test_tagged_imu_converts_to_g_and_dps():
    pkts, _ = w.iter_tagged(tag(w.TAG_IMU, 3, 99, struct.pack("<6h", 4096, 0, -4096, 164, 0, -164)))
    imu = pkts[0]
    assert imu.accel == pytest.approx((1.0, 0.0, -1.0))
    assert imu.gyro == pytest.approx((10.0, 0.0, -10.0))
    assert imu.raw == (4096, 0, -4096, 164, 0, -164)


def test_tagged_mag_converts_to_gauss():
    pkts, _ = w.iter_tagged(tag(w.TAG_MAG, 1, 5, struct.pack("<3h", 6842, 0, -3421)))
    assert pkts[0].field == pytest.approx((1.0, 0.0, -0.5))


def test_tagged_stream_survives_arbitrary_chunk_boundaries():
    """A transport hands us whatever the OS had ready. Every split must decode the
    same, and the remainder must be carried forward."""
    stream = b"".join(
        [
            tag(w.TAG_TACTILE, 1, 1000, w.pack12(COUNTS)),
            tag(w.TAG_IMU, 1, 1001, struct.pack("<6h", 1, 2, 3, 4, 5, 6)),
            tag(w.TAG_MAG, 1, 1002, struct.pack("<3h", 7, 8, 9)),
            tag(w.TAG_TACTILE, 2, 5000, w.pack12(COUNTS)),
        ]
    )
    for chunk in (1, 7, 13, 37, 133, len(stream)):
        got, buf = [], b""
        for o in range(0, len(stream), chunk):
            buf += stream[o:o + chunk]
            pkts, buf = w.iter_tagged(buf)
            got += pkts
        assert buf == b"", f"chunk={chunk} left {len(buf)} B stranded"
        assert [type(p).__name__ for p in got] == [
            "TactilePacket", "ImuPacket", "MagPacket", "TactilePacket"
        ], f"chunk={chunk}"


def test_tagged_skips_leading_junk_so_a_mid_stream_attach_recovers():
    pkts, rest = w.iter_tagged(b"\x00\xff\xa5garbage" + tag(w.TAG_IMU, 9, 9, b"\x00" * 12))
    assert len(pkts) == 1 and pkts[0].seq == 9 and rest == b""


def test_a_bad_declared_length_is_skipped_not_decoded():
    """A truncated frame that reached a parser used to desync it. A length that
    cannot belong to its type is a bad frame, not data."""
    bad = w.TAG_MAGIC + bytes([w.TAG_TACTILE]) + struct.pack("<HII", 999, 1, 1)
    good = tag(w.TAG_IMU, 2, 2, b"\x00" * 12)
    pkts, rest = w.iter_tagged(bad + good)
    assert [p.seq for p in pkts] == [2] and rest == b""


def test_bad_tag_headers_are_counted_but_plain_preamble_junk_is_not():
    bad_length = w.TAG_MAGIC + bytes([w.TAG_TACTILE]) + struct.pack("<HII", 999, 1, 1)
    bad_type = w.TAG_MAGIC + bytes([99]) + struct.pack("<HII", 12, 2, 2)
    good = tag(w.TAG_IMU, 3, 3, b"\x00" * 12)
    packets, rest, malformed = w.iter_tagged_diagnostic(
        b"#STREAM TAG on\r\nordinary ascii preamble" + bad_length + bad_type + good
    )
    assert [packet.seq for packet in packets] == [3]
    assert rest == b"" and malformed == 2


def test_bad_tag_header_split_across_reads_is_counted_exactly_once():
    bad = w.TAG_MAGIC + bytes([w.TAG_MAG]) + struct.pack("<HII", 7, 1, 1)
    packets, remainder, malformed = w.iter_tagged_diagnostic(bad[:8])
    assert packets == [] and remainder == bad[:8] and malformed == 0
    packets, remainder, malformed = w.iter_tagged_diagnostic(remainder + bad[8:])
    assert packets == [] and not remainder.startswith(w.TAG_MAGIC) and malformed == 1
    packets, remainder, malformed = w.iter_tagged_diagnostic(remainder + b"more junk")
    assert packets == [] and malformed == 0


def test_an_unsupported_wide_tactile_payload_is_skipped():
    wide = tag(w.TAG_TACTILE, 1, 1, struct.pack(f"<{w.TAXELS}H", *COUNTS))
    good = tag(w.TAG_TACTILE, 2, 2, w.pack12(COUNTS))
    pkts, rest = w.iter_tagged(wide + good)
    assert [p.seq for p in pkts] == [2] and rest == b""


def test_an_incomplete_trailing_packet_is_returned_not_dropped():
    whole = tag(w.TAG_IMU, 1, 1, b"\x00" * 12)
    pkts, rest = w.iter_tagged(whole + tag(w.TAG_MAG, 2, 2, b"\x00" * 6)[:10])
    assert len(pkts) == 1
    assert rest.startswith(w.TAG_MAGIC) and len(rest) == 10


@pytest.mark.parametrize("version", [1, 2])
def test_every_partial_tactile_prefix_is_buffered_and_never_decoded(version):
    """A fail-closed device may stop after any emitted byte of a TAG frame.

    Until that same frame is completed, the host must retain the exact prefix and
    must not publish even one sensor sample.  Reconnect discards this session-local
    remainder before a new stream begins.
    """
    payload = w.pack12(COUNTS)
    frame = (
        tag(w.TAG_TACTILE, 41, 9000, payload)
        if version == 1
        else tag2(w.TAG_TACTILE, 41, (2 << 32) + 9000, payload)
    )
    parser = w.iter_tagged if version == 1 else w.iter_tagged_v2
    for cut in range(1, len(frame)):
        packets, remainder = parser(frame[:cut])
        assert packets == [], cut
        assert remainder == frame[:cut], cut


# --- TAG v2 USB stream ---------------------------------------------------------


def test_tag_v2_decodes_the_native_u64_timestamp_and_preserves_raw_t_us():
    timestamp_us = (3 << 32) + 0x1234
    packets, rest = w.iter_tagged_v2(
        tag2(w.TAG_IMU, 0xAABBCCDD, timestamp_us, struct.pack("<6h", 4096, 0, -4096, 164, 0, -164))
    )
    assert rest == b"" and len(packets) == 1
    packet = packets[0]
    assert packet.seq == 0xAABBCCDD
    assert packet.t_us == 0x1234
    assert packet.device_time_us == timestamp_us
    assert packet.accel == pytest.approx((1.0, 0.0, -1.0))


def test_tag_v2_header_is_packed_little_endian_and_has_a_distinct_magic():
    frame = tag2(w.TAG_MAG, 0x01020304, 0x0102030405060708, b"\x00" * 6)
    assert w.TAG_V2_MAGIC == b"\xa5\x5b" != w.TAG_MAGIC
    assert w.TAG_V2_HDR_LEN == 17
    assert w.TAG_V2_CRC_LEN == 4
    assert frame[:17].hex() == "a55b030600040302010807060504030201"
    assert struct.unpack("<I", frame[-4:])[0] == zlib.crc32(frame[:-4])


def test_v1_and_v2_decoders_fail_closed_on_the_other_magic():
    v1 = tag(w.TAG_MAG, 1, 2, b"\x00" * 6)
    v2 = tag2(w.TAG_MAG, 1, 2, b"\x00" * 6)
    assert w.iter_tagged(v2)[0] == []
    assert w.iter_tagged_v2(v1)[0] == []


@pytest.mark.parametrize("chunk", [1, 2, 5, 16, 17, 18, 29, 256])
def test_tag_v2_stream_survives_arbitrary_chunk_boundaries(chunk):
    stream = b"junk" + tag2(w.TAG_IMU, 7, (2 << 32) + 99, b"\x00" * 12)
    pending = b""
    packets = []
    for offset in range(0, len(stream), chunk):
        got, pending = w.iter_tagged_v2(pending + stream[offset:offset + chunk])
        packets.extend(got)
    assert pending == b""
    assert len(packets) == 1 and packets[0].device_time_us == (2 << 32) + 99


@pytest.mark.parametrize("corrupt_at", [2, 5, 9, 17, -1])
def test_tag_v2_crc_rejects_header_payload_or_trailer_corruption_and_recovers(corrupt_at):
    damaged = bytearray(tag2(w.TAG_IMU, 7, (2 << 32) + 99, bytes(range(12))))
    damaged[corrupt_at] ^= 0x01
    good = tag2(w.TAG_MAG, 8, (2 << 32) + 100, b"\x00" * 6)
    packets, rest, malformed = w.iter_tagged_v2_diagnostic(bytes(damaged) + good)
    assert [packet.seq for packet in packets] == [8]
    assert rest == b"" and malformed >= 1


def test_tag_v2_crc_trailer_is_buffered_until_all_four_bytes_arrive():
    frame = tag2(w.TAG_MAG, 1, (3 << 32) + 2, b"\x00" * 6)
    packets, remainder = w.iter_tagged_v2(frame[:-1])
    assert packets == [] and remainder == frame[:-1]
    packets, remainder = w.iter_tagged_v2(remainder + frame[-1:])
    assert len(packets) == 1 and remainder == b""


def test_bad_tag_v2_header_is_counted_and_resynchronises_to_the_next_v2_frame():
    bad = w.TAG_V2_MAGIC + bytes([w.TAG_TACTILE]) + struct.pack("<HIQ", 999, 1, 1)
    good = tag2(w.TAG_MAG, 2, 2, b"\x00" * 6)
    packets, rest, malformed = w.iter_tagged_v2_diagnostic(bad + good)
    assert [packet.seq for packet in packets] == [2]
    assert rest == b"" and malformed == 1


def test_version_dispatch_rejects_future_tag_layouts_instead_of_guessing():
    with pytest.raises(ValueError, match="unsupported TAG version"):
        w.iter_tagged_version_diagnostic(b"", 3)


# --- BLE -----------------------------------------------------------------------


def test_ble_v6_carries_mag_and_imu_dt_in_every_slot():
    out = w.decode_ble_notify(ble_notify([COUNTS] * 3))
    assert len(out) == 3
    assert [s.seq for s in out] == [100, 101, 102]
    assert [s.t_us for s in out] == [50_000, 54_000, 58_000]
    assert all(s.counts == COUNTS for s in out)
    assert all(s.mag is not None and s.imu_dt_us == -1500 for s in out)
    assert out[0].imu_raw == (777, -531, -3982, -5, -8, 1)
    assert out[0].mag_raw == (3142, 678, -1107)


def test_ble_zero_filled_mag_slot_is_not_promoted_without_the_fitted_flag():
    out = w.decode_ble_notify(ble_notify([COUNTS], mag=False))
    assert out[0].mag is None
    assert out[0].mag_raw is None


def test_ble_batch_wraps_u32_seq_and_time_instead_of_emitting_impossible_values():
    out = w.decode_ble_notify(ble_notify([COUNTS] * 2, seq=0xFFFFFFFF, t_us=0xFFFFFFF0))
    assert [s.seq for s in out] == [0xFFFFFFFF, 0]
    assert [s.t_us for s in out] == [0xFFFFFFF0, (0xFFFFFFF0 + 4000) & 0xFFFFFFFF]


def test_ble_v6_packet_is_436_bytes_at_three_samples():
    """Inside the ~509 B notify ceiling, per the format document."""
    assert len(ble_notify([COUNTS] * 3)) == 436 == w.BLE_HDR_LEN + 3 * w.BLE_V6_STRIDE


def test_a_truncated_notify_is_rejected_rather_than_partly_decoded():
    with pytest.raises(w.WireError):
        w.decode_ble_notify(ble_notify([COUNTS] * 3)[:-1])


def test_unknown_notify_flags_raise_instead_of_guessing_a_layout():
    p = bytearray(ble_notify([COUNTS]))
    p[1] = 0x00
    with pytest.raises(w.WireError):
        w.decode_ble_notify(bytes(p))


# --- loss accounting -----------------------------------------------------------


@pytest.mark.parametrize(
    "prev,cur,gap", [(None, 5, 0), (5, 6, 0), (5, 9, 3), (0xFFFFFFFF, 0, 0), (0xFFFFFFFE, 1, 2)]
)
def test_seq_gap_counts_missing_samples_and_wraps(prev, cur, gap):
    assert w.seq_gap(prev, cur) == gap


@pytest.mark.parametrize("prev,cur,kind", [(5, 5, "duplicate"), (5, 4, "backward")])
def test_duplicate_and_backward_sequence_are_anomalies_not_billions_of_loss(prev, cur, kind):
    transition = w.classify_seq(prev, cur)
    assert transition.kind == kind and transition.missing == 0
