"""Primary JSONL recording: backend fields, exact replay, and damaged files."""

import json
import tracemalloc
from dataclasses import replace

import numpy as np
import pytest

from fake_serial import CFG_V6
from test_record_replay import glove, recorded
from oglo import replay
from oglo._frame import Frame
from oglo._device import Glove
from oglo._record import RecordError, Recorder
from oglo._replay import ReplayError


@pytest.mark.parametrize("side", ["left", "right"])
def test_recording_writes_backend_files_directly_with_raw_and_clean(side, tmp_path):
    channels = CFG_V6["channels"] if side == "left" else list(reversed(CFG_V6["channels"]))
    path = recorded(tmp_path, cfg={**CFG_V6, "side": side, "channels": channels, "stream_clean": False})
    assert {p.name for p in path.iterdir()} == {
        "meta.json", f"tactile_{side}.raw.jsonl", f"tactile_{side}.jsonl",
        f"wrist_imu_{side}.jsonl", f"wrist_mag_{side}.jsonl", f"tactile_{side}.calibration.json",
    }
    episode = replay(path)
    assert episode.meta["schema"] == 3 and episode.meta["complete"] is True
    arrays = episode.arrays("imu")
    rows = [json.loads(line) for line in (path / f"wrist_imu_{side}.jsonl").read_text().splitlines()]
    assert len(rows) == len(arrays["seq"])
    for index, row in enumerate(rows):
        assert set(row) == {"frame_number", "capture_ns", "clock_source", "clock_domain",
                            "uncertainty_ns", "device_timestamp_ns", "channels", "oglo"}
        assert row["frame_number"] == index
        assert row["capture_ns"] == int(arrays["host_received_ns"][index])
        assert row["device_timestamp_ns"] == int(arrays["t_us"][index]) * 1000
        assert row["clock_source"] == "host_monotonic" and row["clock_domain"] == "local_host"
        assert row["uncertainty_ns"] == 500_000
        assert [row["channels"][k] for k in ("ax", "ay", "az", "gx", "gy", "gz")] == arrays["raw"][index].tolist()
        assert all(type(value) is int for value in row["channels"].values())
    assert (path / f"tactile_{side}.raw.jsonl").read_bytes() != (path / f"tactile_{side}.jsonl").read_bytes()


def test_clean_threshold_boundary_and_recorded_finger_order(tmp_path):
    g = glove({**CFG_V6, "side": "right", "channels": list(reversed(CFG_V6["channels"])),
               "stream_clean": False}, n=0)
    recipe = dict(g._t._s.zero_recipe, baseline=list(range(100, 180)))
    recorder = Recorder(g, tmp_path / "episode", calibration=recipe, chunk_samples=1)
    counts = np.asarray(recipe["baseline"], dtype=np.int64)
    counts[:4] += [-1, 79, 80, 81]
    try:
        recorder.add_tactile(Frame(seq=42, t_us=7, host_t=3.0,
                                   counts=counts.astype(np.uint16).reshape(5, 4, 4)))
        recorder.write(complete=False, error="fixture", stop_reason="test")
    finally:
        g.close()
    raw = json.loads((recorder.dir / "tactile_right.raw.jsonl").read_text())
    clean = json.loads((recorder.dir / "tactile_right.jsonl").read_text())
    assert raw["channels"]["thumb_0_0"] == 99
    assert [clean["channels"][f"thumb_0_{i}"] for i in range(4)] == [0, 0, 80, 81]
    assert raw["frame_number"] == 0 and raw["oglo"]["seq"] == 42
    assert {k: v for k, v in raw.items() if k != "channels"} == {
        k: v for k, v in clean.items() if k != "channels"
    }
    assert replay(recorder.dir).arrays("tactile")["counts"][0].reshape(80).tolist() == counts.tolist()


def test_exact_integer_timestamps_and_device_rollover_survive_chunk_boundaries(tmp_path):
    g = glove(n=0)
    recorder = Recorder(g, tmp_path / "episode", chunk_samples=1)
    base = 2**53 + 123
    try:
        for i in range(2):
            recorder.add_tactile(Frame(
                seq=(2**32 - 1 + i) % 2**32, t_us=(2**32 - 1 + i) % 2**32,
                device_time_us=2**33 - 1 + i, host_t=(base + i * 1000) / 1e9,
                host_t_ns=base + i * 1000, host_received_ns=base + i * 1000,
                counts=np.arange(80, dtype=np.uint16).reshape(5, 4, 4),
            ))
        # Chunks are also standard sensor JSONL, with continuous row indices.
        recorder._writer._queue.join()  # Publication is asynchronous.
        chunks = list(recorder.dir.rglob("chunk_*.jsonl"))
        assert len(chunks) == 1
        assert json.loads(chunks[0].read_text())["capture_ns"] == base
        recorder.write(complete=False, error="fixture", stop_reason="test")
    finally:
        g.close()
    data = replay(recorder.dir).arrays("tactile")
    assert data["host_received_ns"].tolist() == [base, base + 1000]
    assert data["device_time_us"].tolist() == [2**33 - 1, 2**33]
    assert data["seq"].tolist() == [2**32 - 1, 0]
    assert data["counts"].dtype == np.uint16
    assert not list(recorder.dir.rglob("chunk_*"))


@pytest.mark.parametrize("mutate,match", [
    (lambda row: row.update(frame_number=1), "frame_number"),
    (lambda row: row.update(capture_ns=1.5), "capture_ns.*integer"),
    (lambda row: row.update(clock_domain="another-host"), "clock source/domain"),
    (lambda row: row.update(uncertainty_ns=True), "uncertainty_ns.*integer"),
    (lambda row: row.update(device_timestamp_ns=row["device_timestamp_ns"] + 1), "device_timestamp_ns disagrees"),
    (lambda row: row["oglo"].update(seq=True), "seq.*integer"),
    (lambda row: row["channels"].pop("pinky_0_0"), "80 taxel labels"),
])
def test_corrupt_backend_fields_are_rejected(tmp_path, mutate, match):
    path = recorded(tmp_path)
    sensor = path / "tactile_left.jsonl"
    lines = sensor.read_text().splitlines()
    row = json.loads(lines[0])
    mutate(row)
    lines[0] = json.dumps(row)
    sensor.write_text("\n".join(lines) + "\n")
    with pytest.raises(ReplayError, match=match):
        replay(path).arrays("tactile")


@pytest.mark.parametrize("fault", ["truncated", "blank", "duplicate_key", "oversized"])
def test_invalid_jsonl_is_never_silently_skipped(tmp_path, fault):
    path = recorded(tmp_path)
    sensor = path / "wrist_imu_left.jsonl"
    body = sensor.read_text()
    if fault == "truncated":
        body = body.rstrip("\n")
    elif fault == "blank":
        body = "\n" + body
    elif fault == "duplicate_key":
        body = body.replace('"frame_number":0', '"frame_number":0,"frame_number":0', 1)
    else:
        body = '{"padding":"' + 'x' * 65536 + '"}\n' + body
    sensor.write_text(body)
    with pytest.raises(ReplayError):
        replay(path).arrays("imu")


def test_replay_checks_derived_clean_values_and_calibration(tmp_path):
    path = recorded(tmp_path, cfg={**CFG_V6, "stream_clean": False})
    clean = path / "tactile_left.jsonl"
    lines = clean.read_text().splitlines()
    row = json.loads(lines[0])
    row["channels"]["pinky_0_0"] += 1
    lines[0] = json.dumps(row)
    clean.write_text("\n".join(lines) + "\n")
    with pytest.raises(ReplayError, match="CLEAN tactile values disagree"):
        replay(path).arrays("tactile")


def test_missing_raw_measurements_preserve_partial_data_but_cannot_pass(tmp_path, monkeypatch):
    original = Recorder.add_imu
    monkeypatch.setattr(Recorder, "add_imu", lambda self, sample: original(self, replace(sample, raw=None)))
    with pytest.raises(RecordError, match="missing_raw_imu") as caught:
        recorded(tmp_path)
    episode = replay(caught.value.partial_episode)
    assert episode.meta["complete"] is False
    assert not episode.arrays("imu")["raw_valid"].any()
    assert next(episode.imu()).raw is None


def test_raw_capture_without_a_valid_recipe_cannot_claim_backend_ready_data(tmp_path, monkeypatch):
    monkeypatch.setattr(Glove, "_read_recording_calibration", lambda self: {"valid": False})
    with pytest.raises(RecordError, match="missing_valid_calibration") as caught:
        recorded(tmp_path, cfg={**CFG_V6, "stream_clean": False, "zero_valid": False})
    episode = replay(caught.value.partial_episode)
    assert episode.meta["complete"] is False
    assert episode.meta["clean_file"] is None
    assert len(episode.arrays("tactile")["seq"]) > 0
    assert (episode.dir / "tactile_left.raw.jsonl").exists()
    assert not (episode.dir / "tactile_left.jsonl").exists()


def test_mismatched_calibration_validity_is_rejected_before_creating_an_episode(tmp_path, monkeypatch):
    monkeypatch.setattr(Glove, "_read_recording_calibration", lambda self: {"valid": False})
    with pytest.raises(ValueError, match="validity must match"):
        recorded(tmp_path)
    assert not list(tmp_path.iterdir())


def test_replay_does_not_accumulate_python_taxel_objects_for_the_whole_stream(tmp_path):
    g = glove(n=0)
    recorder = Recorder(g, tmp_path / "episode", chunk_samples=127)
    counts = np.full((5, 4, 4), 600, dtype=np.uint16)  # Outside Python's small-int cache.
    try:
        for index in range(5000):
            if index and index % (127 * 4) == 0:
                # This is a replay-memory fixture, not a faster-than-disk capture.
                recorder._writer._queue.join()
            recorder.add_tactile(Frame(seq=index, t_us=index * 4000,
                                       host_t=1.0 + index * 0.004, counts=counts))
        recorder.write(complete=False, error="memory fixture", stop_reason="test")
    finally:
        g.close()

    tracemalloc.start()
    try:
        arrays = replay(recorder.dir).arrays("tactile")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert arrays["counts"].shape == (5000, 5, 4, 4)
    assert np.all(arrays["counts"] == 600)
    assert peak < 8 * sum(array.nbytes for array in arrays.values())


def test_short_replay_does_not_retain_unused_batch_storage(tmp_path):
    g = glove(n=0)
    recorder = Recorder(g, tmp_path / "episode")
    try:
        recorder.add_tactile(Frame(seq=1, t_us=2, host_t=3.0,
                                   counts=np.full((5, 4, 4), 600, dtype=np.uint16)))
        recorder.write(complete=False, error="short fixture", stop_reason="test")
    finally:
        g.close()
    arrays = replay(recorder.dir).arrays("tactile")
    assert arrays["counts"].shape == (1, 5, 4, 4)
    for array in arrays.values():
        backing = array
        while isinstance(backing.base, np.ndarray):
            backing = backing.base
        assert backing.nbytes == array.nbytes
