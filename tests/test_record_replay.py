"""Round-tripping an episode. The pipeline must not be able to tell live from replay."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo import record, replay
from oglo._record import RecordError, Recorder, next_episode_dir
from oglo._replay import ReplayError
from oglo._device import DeviceError, Glove, SampleBatch
from oglo._frame import CleanStreamError, Frame, ImuSample, MagSample
from oglo._usb import UsbTransport


def glove(cfg=CFG_V6, n=40, *, hz=None) -> Glove:
    s = FakeSerial(cfg, stream=tagged_burst(n), hz=hz)
    t = UsbTransport(s)
    info, caps = t.read_config(interval=0.01, drain=0)
    return Glove(t, info, caps)


def recorded(tmp_path, cfg=CFG_V6, seconds=0.6, n=60):
    # A paced producer models the physical board and keeps the USB reader from
    # becoming a synthetic CPU-saturation test. An unpaced infinite refill can
    # starve either thread on a loaded CI runner and fabricate a stale tail.
    g = glove(cfg, n, hz=250)
    try:
        return record(tmp_path, seconds=seconds, glove=g)
    finally:
        g.close()


# --- writing --------------------------------------------------------------------


@pytest.mark.parametrize("seconds", ["oops", True, float("nan"), -1, 0, float("inf")])
def test_invalid_duration_is_rejected_before_stream_or_episode_side_effects(tmp_path, seconds):
    g = glove(n=0)
    commands_before = list(g._t._s.commands)
    try:
        with pytest.raises(ValueError, match="finite real.*greater than zero"):
            record(tmp_path, seconds=seconds, glove=g)
        assert g._t._s.commands == commands_before
        assert not list(tmp_path.glob("ep_*"))
    finally:
        g.close()


def test_an_episode_gets_its_own_numbered_directory(tmp_path):
    a = recorded(tmp_path)
    b = recorded(tmp_path)
    assert a.name == "ep_0001" and b.name == "ep_0002"
    assert a != b, "a second recording must never overwrite the first"


def test_episode_number_reservation_is_atomic_under_concurrency(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: next_episode_dir(tmp_path), range(20)))
    assert len(set(paths)) == 20
    assert all(path.is_dir() for path in paths)


def test_all_four_files_are_written(tmp_path):
    ep = recorded(tmp_path)
    assert {p.name for p in ep.iterdir()} == {"meta.json", "tactile.npz", "imu.npz", "mag.npz"}


def test_an_empty_capture_is_refused_rather_than_written(tmp_path):
    g = glove(n=0)
    try:
        with pytest.raises(RecordError, match="empty"):
            Recorder(g, tmp_path / "ep_0001").write()
    finally:
        g.close()


def test_recorder_memory_is_bounded_and_large_chunked_capture_round_trips(tmp_path):
    """Ten thousand rows must not turn the live buffer into a ten-thousand-row array."""
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=127)
    counts = np.arange(80, dtype=np.uint16).reshape(5, 4, 4)
    try:
        for seq in range(10_000):
            rec.add_tactile(Frame(
                seq=seq,
                t_us=seq * 4,
                host_t=seq / 250.0,
                counts=counts + (seq % 7),
                device_time_us=(1 << 32) + seq * 4,
                host_t_ns=seq * 4_000_000,
                host_received_ns=seq * 4_000_000,
            ))
        assert rec._t.cap == 127
        assert rec._t.live_samples <= 127
        assert rec._t.chunk_count == 10_000 // 127
        rec.write(complete=False, error="test fixture", stop_reason="test")
    finally:
        g.close()

    arrays = replay(tmp_path / "ep_0001").arrays("tactile")
    assert len(arrays["seq"]) == 10_000
    assert arrays["seq"][[0, 126, 127, -1]].tolist() == [0, 126, 127, 9999]
    assert arrays["device_time_us"][[0, -1]].tolist() == [1 << 32, (1 << 32) + 9999 * 4]
    assert not any(p.name.startswith(".recording-") for p in (tmp_path / "ep_0001").iterdir())


def test_empty_modalities_are_valid_npz_files_with_schema_shapes(tmp_path):
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=2)
    try:
        rec.add_tactile(Frame(
            seq=1, t_us=2, host_t=3.0,
            counts=np.zeros((5, 4, 4), dtype=np.uint16),
        ))
        rec.write(complete=False, error="tactile-only fixture", stop_reason="test")
    finally:
        g.close()

    with np.load(tmp_path / "ep_0001" / "imu.npz", allow_pickle=False) as imu:
        assert imu["accel"].shape == (0, 3)
        assert imu["gyro"].shape == (0, 3)
        assert imu["raw"].shape == (0, 6) and imu["raw"].dtype == np.int16
        assert imu["raw_valid"].shape == (0,) and imu["raw_valid"].dtype == np.bool_
    with np.load(tmp_path / "ep_0001" / "mag.npz", allow_pickle=False) as mag:
        assert mag["field"].shape == (0, 3)
        assert mag["raw"].shape == (0, 3) and mag["raw"].dtype == np.int16


def test_npz_staging_failure_keeps_fail_closed_metadata_and_exposes_path(tmp_path, monkeypatch):
    import oglo._record as record_module

    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=2)
    rec.add_tactile(Frame(
        seq=1, t_us=2, host_t=3.0,
        counts=np.zeros((5, 4, 4), dtype=np.uint16),
    ))
    original = record_module._write_buffer_npz
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated full disk")
        return original(*args, **kwargs)

    monkeypatch.setattr(record_module, "_write_buffer_npz", fail_on_second)
    try:
        with pytest.raises(RecordError, match="could not finalize") as caught:
            rec.write()
    finally:
        g.close()

    assert caught.value.partial_episode == tmp_path / "ep_0001"
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False
    assert meta["stop_reason"] == "write_error"
    assert "simulated full disk" in meta["error"]
    assert not list((tmp_path / "ep_0001").glob("*.npz")), "nothing publishes before all staging succeeds"
    assert any(p.name.startswith(".recording-") for p in (tmp_path / "ep_0001").iterdir())


def test_marker_refresh_failure_still_exposes_a_fail_closed_partial_path(tmp_path, monkeypatch):
    import oglo._record as record_module

    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001")
    rec.add_tactile(Frame(
        seq=1, t_us=2, host_t=3.0,
        counts=np.zeros((5, 4, 4), dtype=np.uint16),
    ))
    original = record_module._atomic_text
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("marker fsync failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(record_module, "_atomic_text", fail_once)
    try:
        with pytest.raises(RecordError, match="could not finalize") as caught:
            rec.write()
    finally:
        g.close()

    assert caught.value.partial_episode == tmp_path / "ep_0001"
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False
    assert meta["stop_reason"] == "write_error"
    assert "marker fsync failed" in meta["error"]


def test_raw_integer_sensor_values_survive_record_and_replay(tmp_path):
    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001", chunk_samples=1)
    imu_raw = (-32768, -123, 0, 456, 32766, 32767)
    mag_raw = (-30000, 17, 29999)
    try:
        rec.add_imu(ImuSample(
            seq=1, t_us=2, host_t=3.0,
            accel=(0.1, 0.2, 0.3), gyro=(1.1, 1.2, 1.3), raw=imu_raw,
        ))
        rec.add_imu(ImuSample(
            seq=2, t_us=3, host_t=4.0,
            accel=(0.4, 0.5, 0.6), gyro=(1.4, 1.5, 1.6), raw=None,
        ))
        rec.add_mag(MagSample(
            seq=1, t_us=2, host_t=3.0, field=(0.01, 0.02, 0.03), raw=mag_raw,
        ))
        rec.write(complete=False, error="raw fixture", stop_reason="test")
    finally:
        g.close()

    imu_arrays = replay(tmp_path / "ep_0001").arrays("imu")
    assert imu_arrays["raw"].dtype == np.int16
    assert tuple(int(x) for x in imu_arrays["raw"][0]) == imu_raw
    assert imu_arrays["raw_valid"].tolist() == [True, False]
    assert [sample.raw for sample in replay(tmp_path / "ep_0001").imu()] == [imu_raw, None]
    assert next(replay(tmp_path / "ep_0001").mag()).raw == mag_raw


def test_the_metadata_pins_the_calibration_that_was_in_force(tmp_path):
    """`stream_thr` is mutable on the device, so asking the board later returns
    today's value. An episode that does not record it cannot be interpreted."""
    ep = recorded(tmp_path, cfg={**CFG_V6, "stream_thr": 42, "stream_clean": True})
    meta = json.loads((ep / "meta.json").read_text())
    assert meta["stream_thr"] == 42
    assert meta["stream_clean"] is True
    assert meta["zero_valid"] is True


def test_recorder_metadata_is_an_immutable_capture_start_snapshot(tmp_path):
    g = glove({**CFG_V6, "stream_clean": False}, n=0)
    rec = Recorder(g, tmp_path / "ep_0001")
    try:
        rec.add_tactile(Frame(
            seq=1, t_us=2, host_t=3.0,
            counts=np.full((5, 4, 4), 550, dtype=np.uint16),
            _stream_clean=False,
        ))
        g._info = replace(g.info, stream_clean=True, stream_thr=99)
        rec.write(complete=False, error="direct recorder fixture", stop_reason="test")
    finally:
        g.close()

    episode = replay(tmp_path / "ep_0001")
    assert episode.info.stream_clean is False and episode.info.stream_thr == 80
    with pytest.raises(CleanStreamError):
        _ = next(episode.tactile()).residual


def test_recording_ownership_blocks_other_readers_and_state_mutations():
    g = glove(n=0)
    g._begin_recording()
    try:
        with pytest.raises(DeviceError, match=r"record\(\).*capturing"):
            g.raw()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(g.read_batch)
            with pytest.raises(DeviceError, match=r"another thread.*record\(\) owns"):
                future.result()
    finally:
        g._end_recording()
        g.close()


def test_the_metadata_identifies_the_board_and_the_firmware(tmp_path):
    meta = json.loads((recorded(tmp_path) / "meta.json").read_text())
    for key in ("serial", "side", "hw_rev", "fw_rev", "channels", "sdk_version", "schema"):
        assert meta.get(key) not in (None, "", []), f"{key} missing from meta.json"


def test_metadata_contains_start_end_device_status_and_capture_counter_deltas(tmp_path):
    meta = json.loads((recorded(tmp_path) / "meta.json").read_text())
    assert meta["complete"] is True
    assert meta["status_start"]["sensor_ok"] is True
    assert meta["status_end"]["sensor_ok"] is True
    assert meta["device_counters_during_capture"] == {
        "tag_dropped": 0, "tag_short_writes": 0, "deadline_misses": 0,
    }


def test_metadata_drop_counts_are_capture_deltas_not_glove_lifetime_totals(tmp_path):
    g = glove(n=40)
    try:
        g.start()
        g._t.dropped.tactile = 10
        g._demux.imu.overflowed = 3
        ep = record(tmp_path, seconds=0.2, glove=g)
    finally:
        g.close()
    meta = json.loads((ep / "meta.json").read_text())
    assert meta["dropped_start"]["wire_tactile"] == 10
    assert meta["dropped_start"]["overflow_imu"] == 3
    assert meta["dropped"]["wire_tactile"] == 0
    assert meta["dropped"]["overflow_imu"] == 0


def test_record_refuses_a_fitted_but_unhealthy_magnetometer_before_capture(tmp_path):
    g = glove(n=10)
    g._t._s.status["imu"]["mag_ok"] = False
    try:
        with pytest.raises(RecordError, match="mag_ok=false"):
            record(tmp_path, seconds=0.1, glove=g)
    finally:
        g.close()
    assert not list(tmp_path.glob("ep_*"))


def test_device_drops_deadline_misses_and_sensor_failure_seal_an_incomplete_episode(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class ChangingHealthGlove:
        def __init__(self):
            self.info = info
            self.status_calls = 0
            self.batch_calls = 0
            self.dropped = {"wire_tactile": 20, "overflow_imu": 4}

        def status(self):
            self.status_calls += 1
            end = self.status_calls > 1
            return DeviceStatus(
                uptime_ms=2000 if end else 1000,
                seq=2 if end else 1,
                imu_ok=True,
                mag_ok=not end,
                sensor_ok=True,
                error_flags=0,
                deadline_misses=7 if end else 0,
                tag_dropped=5 if end else 0,
                tag_short_writes=0,
            )

        def read_batch(self):
            self.batch_calls += 1
            if self.batch_calls == 1:
                return SampleBatch(tactile=(Frame(
                    seq=1,
                    t_us=1,
                    host_t=1.0,
                    counts=np.zeros((5, 4, 4), dtype=np.uint16),
                ),))
            return SampleBatch()

    with pytest.raises(
        RecordError, match="mag_ok=false.*new_tag_dropped=5.*new_deadline_misses=7"
    ) as caught:
        record(tmp_path, seconds=0.01, glove=ChangingHealthGlove())
    ep = tmp_path / "ep_0001"
    assert caught.value.partial_episode == ep
    meta = json.loads((ep / "meta.json").read_text())
    assert meta["complete"] is False
    assert meta["stop_reason"] == "device_health"
    assert meta["device_counters_during_capture"]["tag_dropped"] == 5
    assert meta["device_counters_during_capture"]["deadline_misses"] == 7


def test_sustained_silent_modality_is_not_considered_fresh(tmp_path):
    from oglo._record import _modality_freshness_issues

    g = glove(n=0)
    try:
        rec = Recorder(g, tmp_path / "ep_0001")
        rec.add_imu(ImuSample(
            seq=1,
            t_us=1,
            host_t=10.5,
            host_t_ns=10_500_000_000,
            host_received_ns=10_500_000_000,
            accel=(0, 0, 1),
            gyro=(0, 0, 0),
        ))
        rec._started_mono = 10.0
        rec._ended_mono = 12.0
        rec._last_added_mono.update(tactile=11.9, mag=11.9)
        assert _modality_freshness_issues(rec) == ["stale_imu_for=1.500s"]
    finally:
        g.close()


def test_both_clocks_and_a_wall_time_are_recorded(tmp_path):
    """Device micros, host monotonic and a real date each answer a question the
    others cannot."""
    meta = json.loads((recorded(tmp_path) / "meta.json").read_text())
    assert meta["started_wall"] > 1_700_000_000  # a plausible unix time
    assert meta["started_monotonic"] is not None
    assert meta["ended_wall"] >= meta["started_wall"]
    assert meta["ended_monotonic"] >= meta["started_monotonic"]


def test_duration_boundary_drains_bytes_queued_during_host_deschedule(tmp_path, monkeypatch):
    """The deadline can pass while this process is not scheduled. The next poll is
    then the capture tail, not post-capture data, and must precede freshness checks."""
    import oglo._record as record_module
    from oglo._config import parse_config
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)
    received_ns = 10_200_000_000

    class TailOnlyGlove:
        dropped = {}

        def __init__(self):
            self.info = info
            self.read_calls = 0

        def start(self):
            pass

        def status(self):
            return DeviceStatus(
                uptime_ms=1000, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            self.read_calls += 1
            if self.read_calls != 1:
                return SampleBatch()
            tactile = tuple(Frame(
                seq=i, t_us=i, host_t=10.2, host_received_ns=received_ns,
                counts=np.zeros((5, 4, 4), dtype=np.uint16),
            ) for i in (1, 2))
            imu = tuple(ImuSample(
                seq=i, t_us=i, host_t=10.2, host_received_ns=received_ns,
                accel=(0, 0, 1), gyro=(0, 0, 0),
            ) for i in (1, 2))
            mag = tuple(MagSample(
                seq=i, t_us=i, host_t=10.2, host_received_ns=received_ns,
                field=(0.1, 0.2, 0.3),
            ) for i in (1, 2))
            return SampleBatch(tactile=tactile, imu=imu, mag=mag)

    # Capture begins at 10.0 s; the very next deadline check happens at 10.2 s.
    # No loop body runs, so only the explicit boundary drain can see the queued tail.
    clock = iter((10.0, 10.0, 10.2, 10.2))
    monkeypatch.setattr(record_module.time, "monotonic", lambda: next(clock))
    glove = TailOnlyGlove()
    episode = record(tmp_path, seconds=0.1, glove=glove)
    assert glove.read_calls == 1
    meta = json.loads((episode / "meta.json").read_text())
    assert meta["complete"] is True
    assert meta["counts"] == {"tactile": 2, "imu": 2, "mag": 2}


def test_capture_end_timestamp_is_frozen_before_slow_finalization(tmp_path, monkeypatch):
    import oglo._record as record_module

    g = glove(n=0)
    rec = Recorder(g, tmp_path / "ep_0001")
    rec.add_tactile(Frame(
        seq=1, t_us=2, host_t=3.0,
        counts=np.zeros((5, 4, 4), dtype=np.uint16),
    ))
    rec.finish_capture()
    ended_wall = rec._ended_wall
    ended_mono = rec._ended_mono
    original = record_module._write_buffer_npz

    def finalization_happens_later(*args, **kwargs):
        monkeypatch.setattr(record_module.time, "time", lambda: 9_999_999_999.0)
        monkeypatch.setattr(record_module.time, "monotonic", lambda: 8_888_888_888.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(record_module, "_write_buffer_npz", finalization_happens_later)
    try:
        rec.write(complete=False, error="timestamp fixture", stop_reason="test")
    finally:
        g.close()
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["ended_wall"] == ended_wall
    assert meta["ended_monotonic"] == ended_mono


def test_record_quiets_transport_while_publishing_then_resumes_fresh(tmp_path, monkeypatch):
    g = glove(n=40)
    original = Recorder.write
    states = []

    def checked_write(recorder, *args, **kwargs):
        states.append((g._started, g._t._s._streaming))
        return original(recorder, *args, **kwargs)

    monkeypatch.setattr(Recorder, "write", checked_write)
    try:
        episode = record(tmp_path, seconds=0.1, glove=g)
        assert episode.exists()
        assert states == [(False, False)]
        assert g._started and g._t._s._streaming
        assert len(g._demux.tactile) == 0
        assert len(g._demux.imu) == 0
        assert len(g._demux.mag) == 0
    finally:
        g.close()


def test_nominal_capture_has_no_ultrashort_missing_stream_grace(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class TactileOnlyGlove:
        def __init__(self):
            self.info = info
            self.calls = 0
            self.dropped = {}

        def status(self):
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            self.calls += 1
            if self.calls == 1:
                return SampleBatch(tactile=(Frame(
                    seq=1, t_us=1, host_t=1.0,
                    counts=np.zeros((5, 4, 4), dtype=np.uint16),
                ),))
            return SampleBatch()

    with pytest.raises(RecordError, match="missing_streams=imu,mag"):
        record(tmp_path, seconds=0.001, glove=TactileOnlyGlove())
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False and meta["stop_reason"] == "device_health"


def test_one_row_per_modality_cannot_prove_a_complete_stream(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class OneBatchGlove:
        dropped = {}

        def __init__(self):
            self.info = info
            self.sent = False

        def status(self):
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            if self.sent:
                return SampleBatch()
            self.sent = True
            return SampleBatch(
                tactile=(Frame(
                    seq=1, t_us=1, host_t=1.0,
                    counts=np.zeros((5, 4, 4), dtype=np.uint16),
                ),),
                imu=(ImuSample(
                    seq=1, t_us=1, host_t=1.0,
                    accel=(0, 0, 1), gyro=(0, 0, 0),
                ),),
                mag=(MagSample(
                    seq=1, t_us=1, host_t=1.0, field=(0, 0, 1),
                ),),
            )

    with pytest.raises(
        RecordError,
        match="insufficient_stream_samples=tactile:1,imu:1,mag:1",
    ):
        record(tmp_path, seconds=0.001, glove=OneBatchGlove())
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False


def test_no_packet_capture_keeps_missing_stream_error_and_removes_reservation(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class SilentGlove:
        dropped = {}

        def __init__(self):
            self.info = info

        def status(self):
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            return SampleBatch()

    with pytest.raises(RecordError, match="missing_streams=tactile,imu,mag"):
        record(tmp_path, seconds=0.001, glove=SilentGlove())
    assert not list(tmp_path.glob("ep_*"))


def test_no_data_final_status_failure_removes_reservation_without_hiding_error(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class SilentThenBrokenGlove:
        dropped = {}

        def __init__(self):
            self.info = info
            self.status_calls = 0

        def status(self):
            self.status_calls += 1
            if self.status_calls > 1:
                raise OSError("status link failed")
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            return SampleBatch()

    with pytest.raises(RecordError, match="final device status.*status link failed"):
        record(tmp_path, seconds=0.001, glove=SilentThenBrokenGlove())
    assert not list(tmp_path.glob("ep_*"))


def test_keyboard_interrupt_during_final_status_seals_partial_before_reraising(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class InterruptedFinalStatusGlove:
        dropped = {}

        def __init__(self):
            self.info = info
            self.status_calls = 0
            self.batch_calls = 0

        def status(self):
            self.status_calls += 1
            if self.status_calls > 1:
                raise KeyboardInterrupt
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            self.batch_calls += 1
            if self.batch_calls > 1:
                return SampleBatch()
            return SampleBatch(
                tactile=(Frame(
                    seq=1, t_us=1, host_t=1.0,
                    counts=np.zeros((5, 4, 4), dtype=np.uint16),
                ),),
                imu=(ImuSample(
                    seq=1, t_us=1, host_t=1.0,
                    accel=(0, 0, 1), gyro=(0, 0, 0),
                ),),
                mag=(MagSample(
                    seq=1, t_us=1, host_t=1.0, field=(0, 0, 1),
                ),),
            )

    with pytest.raises(KeyboardInterrupt) as caught:
        record(tmp_path, seconds=0.001, glove=InterruptedFinalStatusGlove())
    episode = caught.value.partial_episode
    assert episode == tmp_path / "ep_0001"
    meta = json.loads((episode / "meta.json").read_text())
    assert meta["complete"] is False and meta["stop_reason"] == "status_error"
    assert {path.name for path in episode.glob("*.npz")} == {
        "tactile.npz", "imu.npz", "mag.npz",
    }


@pytest.mark.parametrize(
    "counter",
    [
        "transport_malformed_ble",
        "transport_malformed_usb",
        "transport_stale_imu_ble",
        "duplicate_tactile",
        "backward_imu",
    ],
)
def test_any_host_transport_loss_or_sequence_anomaly_seals_incomplete_episode(
    tmp_path, counter
):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class LossyGlove:
        def __init__(self):
            self.info = info
            self.calls = 0

        @property
        def dropped(self):
            return {counter: int(self.calls > 1)}

        def status(self):
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            self.calls += 1
            if self.calls == 1:
                return SampleBatch(
                    tactile=(Frame(
                        seq=1, t_us=1, host_t=1.0,
                        counts=np.zeros((5, 4, 4), dtype=np.uint16),
                    ),),
                    imu=(ImuSample(
                        seq=1, t_us=1, host_t=1.0,
                        accel=(0, 0, 1), gyro=(0, 0, 0),
                    ),),
                    mag=(MagSample(
                        seq=1, t_us=1, host_t=1.0, field=(0, 0, 1),
                    ),),
                )
            return SampleBatch()

    with pytest.raises(RecordError, match=f"host_loss_{counter}=1"):
        record(tmp_path, seconds=0.001, glove=LossyGlove())
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False
    assert meta["dropped"][counter] == 1


def test_actual_malformed_usb_tag_cannot_be_finalized_as_a_complete_episode(tmp_path):
    import struct

    from oglo import _wire as w

    bad = w.TAG_MAGIC + bytes([w.TAG_TACTILE]) + struct.pack("<HII", 999, 1, 1)
    serial = FakeSerial(CFG_V6, stream=bad + tagged_burst(4), hz=250)
    transport = UsbTransport(serial)
    info, caps = transport.read_config(interval=0.01, drain=0)
    g = Glove(transport, info, caps)
    try:
        with pytest.raises(RecordError, match="host_loss_transport_malformed_usb=1"):
            record(tmp_path, seconds=0.05, glove=g)
    finally:
        g.close()
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False
    assert meta["dropped"]["transport_malformed_usb"] == 1


def test_a_disconnect_seals_recoverable_partial_data_before_reraising(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._usb import DisconnectedError

    info, _ = parse_config(CFG_V6)

    class DisconnectingGlove:
        def __init__(self):
            self.info = info
            self.calls = 0
            self.dropped = {}

        def status(self):
            from oglo._status import DeviceStatus
            if self.calls > 1:
                raise DisconnectedError("cable removed")
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0,
                tag_short_writes=0,
            )

        def read_batch(self):
            self.calls += 1
            if self.calls == 1:
                return SampleBatch(tactile=(Frame(
                    seq=1, t_us=0xFFFFFFF0, host_t=1.0,
                    counts=np.zeros((5, 4, 4), dtype=np.uint16),
                    device_time_us=(1 << 32) - 16,
                    host_t_ns=1_000_000_000,
                    host_received_ns=1_000_000_000,
                ),))
            raise DisconnectedError("cable removed")

    with pytest.raises(DisconnectedError) as caught:
        record(tmp_path, seconds=10, glove=DisconnectingGlove())
    ep_dir = tmp_path / "ep_0001"
    meta = json.loads((ep_dir / "meta.json").read_text())
    assert meta["complete"] is False and "DisconnectedError" in meta["error"]
    assert caught.value.partial_episode == ep_dir
    arrays = replay(ep_dir).arrays("tactile")
    assert arrays["device_time_us"].dtype == np.uint64
    assert int(arrays["device_time_us"][0]) == (1 << 32) - 16


def test_final_status_failure_with_data_exposes_the_sealed_partial_path(tmp_path):
    from oglo._config import parse_config
    from oglo._device import SampleBatch
    from oglo._status import DeviceStatus

    info, _ = parse_config(CFG_V6)

    class StatusFailsAfterData:
        dropped = {}

        def __init__(self):
            self.info = info
            self.status_calls = 0
            self.batch_calls = 0

        def status(self):
            self.status_calls += 1
            if self.status_calls > 1:
                raise OSError("status cable failed")
            return DeviceStatus(
                uptime_ms=1, seq=1, imu_ok=True, mag_ok=True, sensor_ok=True,
                error_flags=0, deadline_misses=0, tag_dropped=0, tag_short_writes=0,
            )

        def read_batch(self):
            self.batch_calls += 1
            if self.batch_calls == 1:
                return SampleBatch(tactile=(Frame(
                    seq=1, t_us=1, host_t=1.0,
                    counts=np.zeros((5, 4, 4), dtype=np.uint16),
                ),))
            return SampleBatch()

    with pytest.raises(RecordError, match="final device status") as caught:
        record(tmp_path, seconds=0.001, glove=StatusFailsAfterData())
    assert caught.value.partial_episode == tmp_path / "ep_0001"
    meta = json.loads((tmp_path / "ep_0001" / "meta.json").read_text())
    assert meta["complete"] is False and meta["stop_reason"] == "status_error"


# --- reading back ---------------------------------------------------------------


def test_replay_yields_the_same_types_as_a_live_glove(tmp_path):
    ep = replay(recorded(tmp_path))
    assert isinstance(next(iter(ep.tactile())), Frame)
    assert isinstance(next(iter(ep.imu())), ImuSample)
    assert isinstance(next(iter(ep.mag())), MagSample)


def test_iterating_an_episode_gives_tactile_frames(tmp_path):
    ep = replay(recorded(tmp_path))
    first = next(iter(ep))
    assert isinstance(first, Frame) and first.counts.shape == (5, 4, 4)


def test_every_field_survives_the_round_trip(tmp_path):
    g = glove(n=40)
    try:
        live = [f for _, f in zip(range(10), g.tactile(timeout=2.0))]
        rec = Recorder(g, tmp_path / "ep_0001")
        for f in live:
            rec.add_tactile(f)
        rec.write(complete=False, error="tactile-only round-trip fixture", stop_reason="test")
    finally:
        g.close()

    back = list(replay(tmp_path / "ep_0001").tactile())
    assert len(back) == len(live)
    for a, b in zip(live, back):
        assert a.seq == b.seq and a.t_us == b.t_us and a.dropped == b.dropped
        assert a.host_t == pytest.approx(b.host_t)
        assert np.array_equal(a.counts, b.counts)


def test_replay_carries_the_recorded_clean_flag_so_residual_behaves_the_same(tmp_path):
    """A replayed frame must answer `.residual` exactly as the live one did. If the
    flag were re-chosen at read time, replayed data would disagree with the live data
    it stands in for."""
    clean = replay(recorded(tmp_path, cfg={**CFG_V6, "stream_clean": True}))
    f = next(iter(clean))
    assert np.allclose(f.residual, f.counts.astype(np.float32))

    raw = replay(recorded(tmp_path, cfg={**CFG_V6, "stream_clean": False}))
    f = next(iter(raw))
    with pytest.raises(CleanStreamError):
        _ = f.residual


def test_the_three_streams_keep_their_own_rates_and_are_not_resampled(tmp_path):
    """Forcing one clock either fabricates samples for the slow stream or discards
    them from the fast one. Both are lies a dataset carries forever."""
    ep = replay(recorded(tmp_path, seconds=1.0, n=120))
    n_t = len(list(ep.tactile()))
    n_i = len(list(ep.imu()))
    n_m = len(list(ep.mag()))
    assert n_t and n_i and n_m
    assert len({n_t, n_i, n_m}) > 1, "three rates collapsed to one -- something resampled"


def test_replay_needs_no_hardware(tmp_path, monkeypatch):
    """The point of the whole feature: a pipeline can be finished before the gloves
    arrive. Break discovery entirely and replay must not care."""
    ep_dir = recorded(tmp_path)

    def explode(*a, **k):
        raise AssertionError("replay touched the serial layer")

    import oglo._usb as u
    monkeypatch.setattr(u, "open_serial", explode)
    monkeypatch.setattr(u, "list_candidates", explode)

    ep = replay(ep_dir)
    assert len(list(ep.tactile())) > 0


# --- inspection ------------------------------------------------------------------


def test_summary_reports_delivered_rate_from_the_data_not_from_the_request(tmp_path):
    s = replay(recorded(tmp_path, seconds=1.0, n=120)).summary()
    assert s["serial"] == "OGLO-L-TEST01"
    assert s["tactile"]["n"] > 0 and s["tactile"]["hz"] > 0
    assert "dropped" in s["tactile"]


def test_repr_says_what_is_in_the_episode(tmp_path):
    r = repr(replay(recorded(tmp_path)))
    assert "OGLO-L-TEST01" in r and "tactile=" in r


def test_arrays_gives_the_raw_numpy_for_anyone_who_would_rather_not_iterate(tmp_path):
    a = replay(recorded(tmp_path)).arrays("tactile")
    assert a["counts"].dtype == np.uint16 and a["counts"].ndim == 4
    assert set(a) == {
        "seq", "t_us", "device_time_us", "host_t", "host_t_ns",
        "host_received_ns", "dropped", "counts",
    }


def test_pointing_at_the_wrong_directory_says_so(tmp_path):
    recorded(tmp_path)
    with pytest.raises(ReplayError, match="not an episode"):
        replay(tmp_path)  # the folder holding episodes, not an episode


def test_unknown_episode_schema_is_refused_instead_of_guessed(tmp_path):
    ep_dir = recorded(tmp_path)
    meta = json.loads((ep_dir / "meta.json").read_text())
    meta["schema"] = 999
    (ep_dir / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="schema 999"):
        replay(ep_dir)


def test_metadata_count_disagreement_is_detected_before_replay(tmp_path):
    ep_dir = recorded(tmp_path)
    meta = json.loads((ep_dir / "meta.json").read_text())
    meta["counts"]["tactile"] += 1
    (ep_dir / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ReplayError, match="meta says"):
        replay(ep_dir).arrays("tactile")


def test_asking_for_a_stream_that_was_not_saved_is_an_error(tmp_path):
    ep_dir = recorded(tmp_path)
    (ep_dir / "mag.npz").unlink()
    with pytest.raises(ReplayError, match="mag.npz.*missing"):
        replay(ep_dir).arrays("mag")
