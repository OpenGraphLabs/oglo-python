"""Opt-in end-to-end checks against one attached left/right USB pair.

Run safe checks with::

    python -m pytest -m hardware --hardware-seconds 5

Add the reversible RAW/CLEAN and rate exercise with::

    python -m pytest -m hardware --hardware-mutations --hardware-seconds 5

The zero sweep is deliberately absent: it requires a worn glove moving through its
full range and overwrites the only calibration recipe on the device. ``GET ZERO`` is
verified here; the state-changing ``zero()`` transaction is exhaustively exercised
against the firmware-accurate fake in ``test_device.py``.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np
import pytest

import oglo
from oglo._config import _fw_at_least
from oglo._doctor import OK, doctor
from oglo._frame import CleanStreamError, Frame, ImuSample, MagSample
from oglo._usb import PortCandidate, list_candidates


pytestmark = pytest.mark.hardware


@dataclass(frozen=True)
class AttachedGlove:
    port: PortCandidate
    serial: str
    side: str


@pytest.fixture(scope="module")
def attached_pair() -> Sequence[AttachedGlove]:
    candidates = list_candidates()
    assert len(candidates) == 2, (
        "the pair suite requires exactly two known OGLO USB devices; saw "
        f"{[(c.device, c.serial_number) for c in candidates]}"
    )
    found: List[AttachedGlove] = []
    for candidate in candidates:
        with oglo.connect(port=candidate.device) as glove:
            found.append(
                AttachedGlove(
                    port=candidate,
                    serial=glove.info.serial,
                    side=glove.info.side,
                )
            )
    assert {item.side for item in found} == {"left", "right"}
    assert len({item.serial.casefold() for item in found}) == 2
    return tuple(sorted(found, key=lambda item: item.side))


def _all_zero_loss(counters: Dict[str, int]) -> None:
    strict = {
        name: value
        for name, value in counters.items()
        if name.startswith(("wire_", "overflow_", "transport_", "duplicate_", "backward_"))
        or name == "unrouted_packets"
    }
    assert strict and all(value == 0 for value in strict.values()), strict


def _rate(samples: Sequence[object]) -> float:
    if len(samples) < 2:
        return 0.0
    span = float(samples[-1].host_t) - float(samples[0].host_t)
    return (len(samples) - 1) / span if span > 0 else 0.0


def _collect(glove: oglo.Glove, seconds: float) -> Dict[str, list]:
    out: Dict[str, list] = {"tactile": [], "imu": [], "mag": []}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        batch = glove.read_batch(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
        for name, samples in batch.as_dict().items():
            out[name].extend(samples)
    return out


def _assert_monotonic(samples: Sequence[object]) -> None:
    assert samples
    assert all(b.device_time_us >= a.device_time_us for a, b in zip(samples, samples[1:]))
    assert all(b.host_t_ns >= a.host_t_ns for a, b in zip(samples, samples[1:]))
    assert all(sample.host_received_ns == sample.host_t_ns for sample in samples)
    assert all(sample.dropped == 0 for sample in samples)


def _assert_sample_contract(samples: Dict[str, list], *, tactile_hz: float) -> None:
    tactile: List[Frame] = samples["tactile"]
    imu: List[ImuSample] = samples["imu"]
    mag: List[MagSample] = samples["mag"]
    for stream in (tactile, imu, mag):
        _assert_monotonic(stream)

    assert 0.90 * tactile_hz <= _rate(tactile) <= 1.10 * tactile_hz
    assert 450.0 <= _rate(imu) <= 550.0
    assert 105.0 <= _rate(mag) <= 145.0

    for frame in tactile:
        assert frame.counts.shape == (5, 4, 4)
        assert frame.counts.dtype == np.uint16
        assert int(frame.counts.min()) >= 0 and int(frame.counts.max()) <= 4095
        assert frame.finger(0).shape == (4, 4)
        assert frame.residual.dtype == np.float32
        assert np.array_equal(frame.residual, frame.counts.astype(np.float32))

    for sample in imu:
        assert len(sample.accel) == len(sample.gyro) == 3
        assert sample.raw is not None and len(sample.raw) == 6
        assert all(math.isfinite(value) for value in (*sample.accel, *sample.gyro))
        assert all(math.isfinite(value) for value in (*sample.accel_frame, *sample.gyro_frame))

    for sample in mag:
        assert len(sample.field) == 3
        assert sample.raw is not None and len(sample.raw) == 3
        assert all(math.isfinite(value) for value in sample.field)
        assert math.isfinite(sample.magnitude) and sample.magnitude >= 0


def _assert_status_healthy(glove: oglo.Glove):
    status = glove.status()
    assert status.healthy
    assert status.imu_ok and status.mag_ok and status.sensor_ok
    assert status.error_flags == 0
    return status


def test_usb_discovery_identity_health_and_zero_readback(attached_pair):
    assert len({item.port.serial_number for item in attached_pair}) == 2
    for item in attached_pair:
        with oglo.connect(port=item.port.device) as glove:
            info = glove.info
            assert (info.serial, info.side, info.transport) == (item.serial, item.side, "usb")
            assert _fw_at_least(info.fw_rev, (0, 9, 10))
            assert info.hw_rev and info.zero_valid and info.stream_clean
            assert info.rate_hz == 250 and info.has_mag
            assert info.channels == (
                ["pinky", "ring", "middle", "index", "thumb"]
                if info.side == "left"
                else ["thumb", "index", "middle", "ring", "pinky"]
            )
            _assert_status_healthy(glove)

            line = glove.send("GET ZERO", expect="#TZERO ", timeout=5.0)
            recipe = json.loads(line.removeprefix("#TZERO "))
            assert recipe["valid"] is True and recipe["count"] == 80
            assert len(recipe["baseline"]) == len(recipe["noise"]) == 80
            assert recipe["thr"] == info.stream_thr
            assert recipe["clean"] is info.stream_clean


def test_logical_serial_selection_never_returns_the_other_hand(attached_pair):
    for item in attached_pair:
        with oglo.connect(serial=item.serial, timeout=10.0) as glove:
            assert glove.info.serial == item.serial
            assert glove.info.side == item.side


def test_pair_connects_one_left_and_one_right(attached_pair):
    left, right = oglo.connect_pair()
    try:
        assert (left.info.side, right.info.side) == ("left", "right")
        assert left.info.serial != right.info.serial
    finally:
        left.close()
        right.close()


def test_each_hand_streams_all_modalities_without_loss(attached_pair, hardware_seconds):
    for item in attached_pair:
        with oglo.connect(port=item.port.device) as glove:
            before = _assert_status_healthy(glove)
            samples = _collect(glove, hardware_seconds)
            _assert_sample_contract(samples, tactile_hz=float(glove.info.rate_hz))
            _all_zero_loss(glove.dropped)
            after = _assert_status_healthy(glove)
            assert after.uptime_ms >= before.uptime_ms
            assert after.tag_dropped == before.tag_dropped
            assert after.tag_short_writes == before.tag_short_writes
            assert after.deadline_misses == before.deadline_misses


def test_stop_restart_and_public_iterators(attached_pair):
    for item in attached_pair:
        with oglo.connect(port=item.port.device) as glove:
            tactile = next(glove.tactile(timeout=2.0))
            imu = next(glove.imu(timeout=2.0))
            mag = next(glove.mag(timeout=2.0))
            assert isinstance(tactile, Frame)
            assert isinstance(imu, ImuSample)
            assert isinstance(mag, MagSample)
            glove.stop()
            glove.start()
            restarted = glove.read_batch(timeout=2.0)
            assert restarted
            _all_zero_loss(glove.dropped)


def test_repeated_streaming_context_close_does_not_reboot_a_glove(attached_pair):
    """Regression for closing CDC before firmware processes STREAM TAG OFF.

    The failure is physical: the port disappears, then returns with a lower uptime
    and ``reset_reason=task_wdt``. Five streamed open/close cycles per hand catch the
    race while keeping the suite short enough for routine bench use.
    """
    previous_uptime: Dict[str, int] = {}
    for _ in range(5):
        for item in attached_pair:
            with oglo.connect(port=item.port.device) as glove:
                status = _assert_status_healthy(glove)
                if item.serial in previous_uptime:
                    assert status.uptime_ms >= previous_uptime[item.serial]
                previous_uptime[item.serial] = status.uptime_ms
                assert next(glove.tactile(timeout=2.0)).dropped == 0
        assert len(list_candidates()) == 2


def test_two_hands_stream_concurrently_without_cross_throttling(attached_pair, hardware_seconds):
    left, right = oglo.connect_pair()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                glove.info.side: pool.submit(_collect, glove, hardware_seconds)
                for glove in (left, right)
            }
            samples = {side: future.result() for side, future in futures.items()}
        for glove in (left, right):
            _assert_sample_contract(samples[glove.info.side], tactile_hz=float(glove.info.rate_hz))
            _all_zero_loss(glove.dropped)
            _assert_status_healthy(glove)
    finally:
        left.close()
        right.close()


def _assert_episode(path: Path, *, serial: str, side: str) -> None:
    episode = oglo.replay(path)
    summary = episode.summary()
    assert summary["complete"] is True
    assert (summary["serial"], summary["side"]) == (serial, side)
    assert summary["tactile"]["n"] > 0
    assert summary["imu"]["n"] > summary["tactile"]["n"]
    assert summary["mag"]["n"] > 0
    assert 220.0 <= summary["tactile"]["hz"] <= 280.0
    assert 450.0 <= summary["imu"]["hz"] <= 550.0
    assert 105.0 <= summary["mag"]["hz"] <= 145.0
    for name in ("tactile", "imu", "mag"):
        assert summary[name]["dropped"] == 0
        arrays = episode.arrays(name)
        assert len(arrays["seq"]) == summary[name]["n"]
        assert np.array_equal(arrays["host_t_ns"], arrays["host_received_ns"])
    assert len(list(episode.tactile())) == summary["tactile"]["n"]
    assert len(list(episode.imu())) == summary["imu"]["n"]
    assert len(list(episode.mag())) == summary["mag"]["n"]


def test_two_hand_record_replay_round_trip(attached_pair, tmp_path):
    left, right = oglo.connect_pair()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                glove.info.side: pool.submit(
                    oglo.record, tmp_path / glove.info.side, 2.0, glove=glove
                )
                for glove in (left, right)
            }
            paths = {side: future.result() for side, future in futures.items()}
        for glove in (left, right):
            _assert_episode(paths[glove.info.side], serial=glove.info.serial, side=glove.info.side)
    finally:
        left.close()
        right.close()


def test_doctor_passes_both_attached_usb_gloves(attached_pair, hardware_seconds):
    report = doctor(seconds=hardware_seconds)
    failures = [check for check in report.checks if check.verdict != OK]
    assert not failures, str(report)
    for item in attached_pair:
        assert any(item.serial in check.name for check in report.checks)


@pytest.mark.hardware_mutation
def test_reversible_raw_clean_threshold_and_rate_changes(
    attached_pair, hardware_mutations_enabled
):
    """Exercise mutations while restoring every observed initial setting.

    IMU period is not readable from CONFIG in firmware 0.9.10, so this test first
    proves the attached device is at the 500 Hz shipping value before changing it.
    That makes restoring ``imu=500`` evidence-based rather than an assumption.
    """
    for item in attached_pair:
        with oglo.connect(port=item.port.device) as glove:
            original_rate = glove.info.rate_hz
            original_clean = glove.info.stream_clean
            original_threshold = glove.info.stream_thr
            initial = _collect(glove, 1.5)
            assert 450.0 <= _rate(initial["imu"]) <= 550.0
            try:
                glove.raw()
                assert glove.info.stream_clean is False
                raw_frame = next(glove.tactile(timeout=2.0))
                with pytest.raises(CleanStreamError):
                    _ = raw_frame.residual

                alternate_threshold = original_threshold + 1 if original_threshold < 4095 else 4094
                glove.clean(threshold=alternate_threshold)
                assert glove.info.stream_clean and glove.info.stream_thr == alternate_threshold
                clean_frame = next(glove.tactile(timeout=2.0))
                assert np.array_equal(clean_frame.residual, clean_frame.counts.astype(np.float32))

                alternate_rate = 200 if original_rate != 200 else 250
                glove.rates(tactile=alternate_rate, imu=250)
                assert glove.info.rate_hz == alternate_rate
                changed = _collect(glove, 1.5)
                assert 0.88 * alternate_rate <= _rate(changed["tactile"]) <= 1.12 * alternate_rate
                assert 220.0 <= _rate(changed["imu"]) <= 280.0
                _all_zero_loss(glove.dropped)
            finally:
                # Restore even when an assertion above exposes a regression.
                glove.rates(tactile=original_rate, imu=500)
                if original_clean:
                    glove.clean(threshold=original_threshold)
                else:
                    glove.raw()

            assert glove.info.rate_hz == original_rate
            assert glove.info.stream_clean is original_clean
            assert glove.info.stream_thr == original_threshold
            restored = _collect(glove, 1.5)
            assert 0.90 * original_rate <= _rate(restored["tactile"]) <= 1.10 * original_rate
            assert 450.0 <= _rate(restored["imu"]) <= 550.0
            _all_zero_loss(glove.dropped)
            _assert_status_healthy(glove)
