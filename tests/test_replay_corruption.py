"""Fail-closed checks for episodes that are present but internally corrupt."""

from __future__ import annotations

import json

import numpy as np
import pytest

from oglo import replay
from oglo._replay import ReplayError


_LOSS_NAMES = {
    *(f"wire_{name}" for name in ("tactile", "imu", "mag")),
    *(f"overflow_{name}" for name in ("tactile", "imu", "mag")),
    *(f"duplicate_{name}" for name in ("tactile", "imu", "mag")),
    *(f"backward_{name}" for name in ("tactile", "imu", "mag")),
    "transport_overflow_ble",
    "transport_malformed_ble",
    "transport_malformed_usb",
    "transport_stale_imu_ble",
    "unrouted_packets",
}


def _status(uptime):
    return {
        "uptime_ms": uptime,
        "seq": 1,
        "imu_ok": True,
        "mag_ok": True,
        "sensor_ok": True,
        "error_flags": 0,
        "deadline_misses": 0,
        "tag_dropped": 0,
        "tag_short_writes": 0,
        "mag_required": True,
        "raw": {},
    }


def _meta(counts=None):
    loss = {name: 0 for name in _LOSS_NAMES}
    return {
        "schema": 2,
        "complete": True,
        "sdk_version": "0.1.0rc3",
        "serial": "OGLO-L-TEST01",
        "side": "left",
        "hw_rev": "RDR02_FLEX5_REV_D_TIA",
        "fw_rev": "0.9.10",
        "transport": "usb",
        "rate_hz": 250,
        "channels": ["pinky", "ring", "middle", "index", "thumb"],
        "has_mag": True,
        "zero_valid": True,
        "stream_clean": True,
        "stream_thr": 30,
        "imu_period_ms": None,
        "device_dropped_at_connect": 0,
        "started_wall": 1_800_000_000.0,
        "started_monotonic": 100.0,
        "ended_wall": 1_800_000_001.0,
        "ended_monotonic": 101.0,
        "status_start": _status(1000),
        "status_end": _status(2000),
        "dropped_start": dict(loss),
        "dropped_end": dict(loss),
        "dropped": dict(loss),
        "device_counters_during_capture": {
            "tag_dropped": 0,
            "tag_short_writes": 0,
            "deadline_misses": 0,
        },
        "stop_reason": "duration",
        "error": None,
        "counts": counts if counts is not None else {"tactile": 1, "imu": 1, "mag": 1},
    }


def _columns(n=1):
    return {
        "seq": np.arange(n, dtype=np.uint32),
        "t_us": np.arange(n, dtype=np.uint32),
        "device_time_us": np.arange(n, dtype=np.uint64),
        "host_t": np.arange(n, dtype=np.float64) / 1_000_000_000.0,
        "host_t_ns": np.arange(n, dtype=np.uint64),
        "host_received_ns": np.arange(n, dtype=np.uint64),
        "dropped": np.zeros(n, dtype=np.uint32),
    }


def _episode(tmp_path):
    ep = tmp_path / "ep_0001"
    ep.mkdir()
    (ep / "meta.json").write_text(json.dumps(_meta()))
    np.savez(ep / "tactile.npz", counts=np.zeros((1, 5, 4, 4), dtype=np.uint16), **_columns())
    np.savez(
        ep / "imu.npz",
        accel=np.zeros((1, 3), dtype=np.float32),
        gyro=np.zeros((1, 3), dtype=np.float32),
        **_columns(1),
    )
    np.savez(ep / "mag.npz", field=np.zeros((1, 3), dtype=np.float32), **_columns(1))
    return ep


def _two_tactile_rows(ep):
    meta_path = ep / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["counts"]["tactile"] = 2
    meta_path.write_text(json.dumps(meta))
    arrays = _columns(2)
    np.savez(
        ep / "tactile.npz",
        counts=np.zeros((2, 5, 4, 4), dtype=np.uint16),
        **arrays,
    )
    return arrays


def test_schema2_missing_stream_file_is_not_silently_replayed_as_empty(tmp_path):
    ep = _episode(tmp_path)
    (ep / "tactile.npz").unlink()
    with pytest.raises(ReplayError, match="tactile.npz is missing"):
        list(replay(ep).tactile())
    with pytest.raises(ReplayError, match="tactile.npz is missing"):
        replay(ep).summary()


@pytest.mark.parametrize(
    "column,value,message",
    [
        ("seq", np.array([1.9]), "seq must use an integer"),
        ("t_us", np.array([2.9]), "t_us must use an integer"),
        ("device_time_us", np.array([-5], dtype=np.int64), "outside"),
        ("host_t", np.array([np.nan]), "NaN or infinity"),
        ("host_t_ns", np.array([-7], dtype=np.int64), "outside"),
        ("host_received_ns", np.array([-8], dtype=np.int64), "outside"),
        ("dropped", np.array([-1], dtype=np.int64), "outside"),
    ],
)
def test_invalid_header_columns_are_rejected_before_lossy_python_casts(
    tmp_path, column, value, message
):
    ep = _episode(tmp_path)
    with np.load(ep / "tactile.npz", allow_pickle=False) as stored:
        arrays = {name: stored[name] for name in stored.files}
    arrays[column] = value
    np.savez(ep / "tactile.npz", **arrays)
    with pytest.raises(ReplayError, match=message):
        replay(ep).arrays("tactile")


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda a: a["dropped"].__setitem__(1, 1), "contains dropped samples"),
        (
            lambda a: a["device_time_us"].__setitem__(slice(None), [1, 0]),
            "device_time_us must be nondecreasing",
        ),
        (lambda a: a["t_us"].__setitem__(1, 99), "t_us disagrees"),
        (lambda a: a["host_t_ns"].__setitem__(1, 0), "host_t_ns must equal"),
        (lambda a: a["host_t"].__setitem__(1, 9.0), "host_t disagrees"),
        (lambda a: a["seq"].__setitem__(1, 2), "requires dropped=1"),
    ],
)
def test_schema2_rows_cross_validate_clocks_sequences_and_loss(tmp_path, mutate, message):
    ep = _episode(tmp_path)
    arrays = _two_tactile_rows(ep)
    mutate(arrays)
    np.savez(
        ep / "tactile.npz",
        counts=np.zeros((2, 5, 4, 4), dtype=np.uint16),
        **arrays,
    )
    with pytest.raises(ReplayError, match=message):
        replay(ep).arrays("tactile")


def test_partial_sequence_validation_keeps_the_last_accepted_reference(tmp_path):
    ep = _episode(tmp_path)
    meta = json.loads((ep / "meta.json").read_text())
    meta.update(
        complete=False,
        error="sequence anomaly fixture",
        stop_reason="error",
    )
    meta["counts"]["tactile"] = 3
    (ep / "meta.json").write_text(json.dumps(meta))
    arrays = _columns(3)
    arrays["seq"][:] = [5, 4, 6]
    arrays["t_us"][:] = [5, 4, 6]
    arrays["device_time_us"][:] = [5, 4, 6]
    np.savez(
        ep / "tactile.npz",
        counts=np.zeros((3, 5, 4, 4), dtype=np.uint16),
        **arrays,
    )

    replayed = replay(ep).arrays("tactile")
    assert replayed["seq"].tolist() == [5, 4, 6]
    assert replayed["dropped"].tolist() == [0, 0, 0]


def test_malformed_meta_json_and_noninteger_counts_are_replay_errors(tmp_path):
    ep = _episode(tmp_path)
    (ep / "meta.json").write_text("[")
    with pytest.raises(ReplayError, match="could not read"):
        replay(ep)

    (ep / "meta.json").write_text(json.dumps(_meta({"tactile": "1", "imu": 0, "mag": 0})))
    with pytest.raises(ReplayError, match="non-negative integer"):
        replay(ep)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("complete", None, "complete.*boolean"),
        ("serial", "", "serial.*non-empty JSON string"),
        ("hw_rev", 4, "hw_rev.*non-empty JSON string"),
        ("fw_rev", "", "fw_rev.*non-empty JSON string"),
        ("fw_rev", "0.9.9", "firmware 0.9.10 or newer"),
        ("transport", "replay", "transport must be 'usb' or 'ble'"),
        ("rate_hz", "250", "rate_hz.*JSON integer"),
        ("rate_hz", 0, "rate_hz.*1..1000"),
        ("channels", "pinky,ring,middle,index,thumb", "channels.*JSON list"),
        ("channels", ["pinky"] * 5, "channels.*exactly once"),
        ("has_mag", "false", "has_mag.*JSON boolean"),
        ("zero_valid", 1, "zero_valid.*JSON boolean"),
        ("stream_clean", "false", "stream_clean.*JSON boolean"),
        ("stream_thr", "30", "stream_thr.*JSON integer"),
        ("stream_thr", 4096, "stream_thr.*0..4095"),
        ("imu_period_ms", "2", "imu_period_ms.*JSON integer"),
        ("device_dropped_at_connect", False, "device_dropped_at_connect.*JSON integer"),
        ("device_dropped_at_connect", -1, "device_dropped_at_connect.*>=0"),
    ],
)
def test_schema2_metadata_rejects_coercion_empty_identity_and_invalid_config(
    tmp_path, field, value, message
):
    ep = _episode(tmp_path)
    meta = _meta()
    meta[field] = value
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match=message):
        replay(ep)


def test_schema2_requires_complete_and_all_three_exact_counts(tmp_path):
    ep = _episode(tmp_path)
    meta = _meta()
    del meta["complete"]
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="missing required field 'complete'"):
        replay(ep)

    for counts in (
        {},
        {"tactile": 1, "imu": 0},
        {"tactile": 1, "imu": 0, "mag": 0, "future": 0},
        {"tactile": 1, "imu": 0, "mag": True},
        {"tactile": 1, "imu": 0, "mag": -1},
    ):
        (ep / "meta.json").write_text(json.dumps(_meta(counts)))
        with pytest.raises(ReplayError, match="counts must contain exactly|non-negative"):
            replay(ep)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("started_wall", None, "finite start and end clocks"),
        ("started_monotonic", float("nan"), "finite JSON number"),
        ("ended_wall", 1_700_000_000.0, "end time precedes"),
        ("status_start", {}, "non-empty status_start"),
        ("status_end", None, "non-empty status_end"),
        ("dropped_start", None, "dropped_start must be an object"),
        ("stop_reason", 3, "stop_reason.*JSON string"),
        ("error", "claimed success with error", "must have error=null"),
    ],
)
def test_complete_schema2_requires_ordered_clocks_and_health_loss_evidence(
    tmp_path, field, value, message
):
    ep = _episode(tmp_path)
    meta = _meta()
    meta[field] = value
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match=message):
        replay(ep)


@pytest.mark.parametrize(
    "field",
    [
        "started_wall",
        "started_monotonic",
        "ended_wall",
        "ended_monotonic",
        "status_start",
        "status_end",
        "dropped",
        "dropped_start",
        "dropped_end",
        "device_counters_during_capture",
        "stop_reason",
        "error",
    ],
)
def test_schema2_requires_each_integrity_field_even_for_fail_closed_parsing(tmp_path, field):
    ep = _episode(tmp_path)
    meta = _meta({"tactile": 0, "imu": 0, "mag": 0})
    meta["complete"] = False
    del meta[field]
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match=f"missing required field '{field}'"):
        replay(ep)


@pytest.mark.parametrize(
    "counts",
    [
        {"tactile": 0, "imu": 1, "mag": 1},
        {"tactile": 1, "imu": 0, "mag": 1},
        {"tactile": 1, "imu": 1, "mag": 0},
    ],
)
def test_complete_schema2_requires_every_fitted_stream(tmp_path, counts):
    ep = _episode(tmp_path)
    (ep / "meta.json").write_text(json.dumps(_meta(counts)))
    with pytest.raises(ReplayError, match="missing a required fitted stream"):
        replay(ep)


def test_complete_schema2_rejects_claimed_loss_free_evidence_that_disagrees(tmp_path):
    ep = _episode(tmp_path)
    meta = _meta()
    meta["dropped_end"]["wire_tactile"] = 1
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="inconsistent host-loss counter"):
        replay(ep)

    meta = _meta()
    meta["status_end"]["tag_dropped"] = 1
    meta["device_counters_during_capture"]["tag_dropped"] = 1
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="records device loss"):
        replay(ep)

    meta = _meta()
    meta["status_end"]["sensor_ok"] = False
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="unhealthy status_end"):
        replay(ep)

    meta = _meta()
    del meta["dropped"]["unrouted_packets"]
    (ep / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="dropped lacks counters"):
        replay(ep)


def test_complete_schema2_allows_no_mag_samples_when_magnetometer_is_not_fitted(tmp_path):
    ep = _episode(tmp_path)
    meta = _meta({"tactile": 1, "imu": 1, "mag": 0})
    meta["has_mag"] = False
    meta["status_start"]["mag_required"] = False
    meta["status_end"]["mag_required"] = False
    (ep / "meta.json").write_text(json.dumps(meta))
    np.savez(ep / "mag.npz", field=np.empty((0, 3), dtype=np.float32), **_columns(0))
    episode = replay(ep)
    assert episode.info.has_mag is False
    assert list(episode.mag()) == []


def test_incomplete_schema2_allows_unfinished_clocks_status_and_empty_loss_maps(tmp_path):
    ep = _episode(tmp_path)
    meta = _meta({"tactile": 0, "imu": 0, "mag": 0})
    meta.update(
        complete=False,
        ended_wall=None,
        ended_monotonic=None,
        status_start=None,
        status_end=None,
        dropped={},
        dropped_start={},
        dropped_end={},
        device_counters_during_capture={
            "tag_dropped": None,
            "tag_short_writes": None,
            "deadline_misses": None,
        },
        stop_reason="recording",
        error="capture did not reach finalization",
    )
    (ep / "meta.json").write_text(json.dumps(meta))
    assert replay(ep).meta["complete"] is False


def test_schema1_keeps_legacy_defaults_and_normalizes_cast_errors(tmp_path):
    ep = tmp_path / "legacy"
    ep.mkdir()
    (ep / "meta.json").write_text(json.dumps({"serial": "old", "rate_hz": "250"}))
    legacy = replay(ep)
    assert legacy.schema == 1
    assert legacy.info.serial == "old"
    assert legacy.info.side == "right"
    assert legacy.info.rate_hz == 250

    (ep / "meta.json").write_text(json.dumps({"schema": 1, "rate_hz": "not-a-number"}))
    with pytest.raises(ReplayError, match="invalid schema-1 metadata"):
        replay(ep)
