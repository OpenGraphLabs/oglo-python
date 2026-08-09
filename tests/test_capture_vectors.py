"""The hardware capture tool's non-I/O logic. No serial port is opened here."""

from __future__ import annotations

import json
import struct
import sys

import pytest

from tools import capture_vectors as capture


def tagged(ptype: int, payload: bytes, *, seq: int = 7, t_us: int = 1234) -> bytes:
    return capture.TAG_MAGIC + bytes([ptype]) + struct.pack("<HII", len(payload), seq, t_us) + payload


def tactile_packet() -> bytes:
    # First pair is 0x123/0xabc. Remaining 78 values are zero. This is deliberately
    # literal rather than produced by either decoder's pack helper.
    return tagged(capture.TAG_TACTILE, bytes.fromhex("12 3a bc") + bytes(117))


def imu_packet() -> bytes:
    return tagged(capture.TAG_IMU, struct.pack("<6h", 4096, 0, -4096, 164, 0, -164))


def mag_packet() -> bytes:
    return tagged(capture.TAG_MAG, struct.pack("<3h", 6842, 0, -3421))


def test_reference_decoder_is_an_independent_plain_data_oracle():
    tactile = capture.decode_reference(tactile_packet())
    assert tactile["seq"] == 7 and tactile["t_us"] == 1234
    assert tactile["counts"][:2] == [0x123, 0xABC]
    assert tactile["counts"][2:] == [0] * 78

    imu = capture.decode_reference(imu_packet())
    assert imu["accel"] == [1.0, 0.0, -1.0]
    assert imu["gyro"] == [10.0, 0.0, -10.0]


def test_capture_config_enforces_the_0910_schema6_boundary_numerically():
    cfg = {
        "fw_rev": "0.9.10",
        "schema_ver": 6,
        "values_per_sample": 80,
        "sample_shape": [5, 4, 4],
        "imu_len": 25,
        "serial": "OGLO-L-TEST01",
        "hw_rev": "RDR02_FLEX5_REV_D_TIA",
        "has_mag": True,
    }
    assert capture.validate_capture_config(cfg) is True
    with pytest.raises(ValueError, match="older than 0.9.10"):
        capture.validate_capture_config({**cfg, "fw_rev": "0.9.9"})


def test_capture_fails_before_writing_when_a_required_modality_is_missing():
    with pytest.raises(RuntimeError, match="mag"):
        capture.collect_vectors(tactile_packet() + imu_packet(), has_mag=True)

    vectors = capture.collect_vectors(tactile_packet() + imu_packet(), has_mag=False)
    assert {name.rsplit("_", 1)[0] for name in vectors} == {"tag_tactile", "tag_imu"}


def test_public_capture_metadata_redacts_the_logical_serial_by_default():
    cfg = {
        "serial": "OGLO-L-00001",
        "side": "left",
        "hw_rev": "RDR02_FLEX5_REV_D_TIA",
        "fw_rev": "0.9.10",
        "rate_hz": 250,
        "imu_len": 25,
        "has_mag": True,
    }
    public = capture.capture_metadata(cfg)
    assert public["serial"] == "OGLO-L-GOLDEN"
    assert public["serial_redacted"] is True
    assert capture.capture_metadata(cfg, include_serial=True)["serial"] == "OGLO-L-00001"


def test_replacing_a_capture_removes_obsolete_length_variants(tmp_path):
    stale_bin = tmp_path / "tag_tactile_999b.bin"
    stale_json = tmp_path / "tag_tactile_999b.expected.json"
    unrelated = tmp_path / "README.md"
    stale_bin.write_bytes(b"old")
    stale_json.write_text("{}")
    unrelated.write_text("keep")

    vectors = capture.collect_vectors(tactile_packet() + imu_packet(), has_mag=False)
    capture.write_vector_set(vectors, {"fw_rev": "0.9.10"}, directory=tmp_path)

    assert not stale_bin.exists() and not stale_json.exists()
    assert unrelated.read_text() == "keep"
    for name in vectors:
        assert (tmp_path / f"{name}.bin").exists()
        assert (tmp_path / f"{name}.expected.json").exists()


def test_config_reader_carries_a_json_line_across_arbitrary_serial_reads(monkeypatch):
    cfg = {
        "device": "oglo",
        "fw_rev": "0.9.10",
        "schema_ver": 6,
    }
    encoded = b"#CONFIG " + json.dumps(cfg).encode() + b"\r\n"

    class RaggedSerial:
        def __init__(self):
            self.parts = [encoded[:4], encoded[4:13], encoded[13:]]

        def write(self, data): return len(data)
        def flush(self): pass
        def reset_input_buffer(self): pass
        def read(self, size): return self.parts.pop(0) if self.parts else b""

    monkeypatch.setattr(capture.time, "sleep", lambda _seconds: None)
    assert capture.read_config(RaggedSerial(), timeout=0.2) == cfg


def test_capture_requires_an_explicit_port_when_more_than_one_glove_is_visible(monkeypatch):
    monkeypatch.setattr(capture, "find_ports", lambda: ["/dev/glove-left", "/dev/glove-right"])
    monkeypatch.setattr(sys, "argv", ["capture_vectors.py"])
    with pytest.raises(SystemExit, match="more than one glove"):
        capture.main()
