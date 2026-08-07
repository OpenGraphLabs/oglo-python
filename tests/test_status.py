"""Strict parsing of the runtime health contract."""

from __future__ import annotations

from copy import deepcopy

import pytest

from oglo._status import StatusError, parse_status


GOOD = {
    "uptime_ms": 1234,
    "seq": 99,
    "imu_ok": True,
    "imu": {"ok": True, "mag_ok": True, "future_sensor_detail": 7},
    "sensor_ok": True,
    "error_flags": 0,
    "deadline_misses": 0,
    "tag_dropped": 0,
    "tag_short_writes": 0,
    "future_status_detail": "kept",
}


def test_status_preserves_health_and_unknown_fields():
    status = parse_status(deepcopy(GOOD))
    assert status.healthy
    assert status.uptime_ms == 1234 and status.seq == 99
    assert status.raw["future_status_detail"] == "kept"
    assert status.raw["imu"]["future_sensor_detail"] == 7


def test_optional_magnetometer_is_only_required_when_the_glove_says_it_is_fitted():
    raw = deepcopy(GOOD)
    raw["imu"]["mag_ok"] = False
    status = parse_status(raw)
    assert status.healthy  # parser cannot know whether the optional part is fitted
    assert status.mag_required is False
    assert status.__class__(**{**status.__dict__, "mag_required": True}).healthy is False


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda d: d.clear(), "status is empty"),
        (lambda d: d.pop("imu"), "missing imu"),
        (lambda d: d.__setitem__("imu", []), "imu status must be an object"),
        (lambda d: d.pop("imu_ok"), "missing imu_ok"),
        (lambda d: d["imu"].pop("ok"), "missing ok"),
        (lambda d: d["imu"].pop("mag_ok"), "missing mag_ok"),
        (lambda d: d.pop("sensor_ok"), "missing sensor_ok"),
        (lambda d: d.pop("uptime_ms"), "missing uptime_ms"),
        (lambda d: d.__setitem__("seq", True), "seq must be an integer"),
        (lambda d: d.__setitem__("deadline_misses", -1), "cannot be negative"),
        (lambda d: d.__setitem__("tag_dropped", 0x1_0000_0000), "exceeds"),
        (lambda d: d.__setitem__("imu_ok", 1), "imu_ok must be boolean"),
        (lambda d: d["imu"].__setitem__("ok", False), "disagrees"),
        (lambda d: d["imu"].__setitem__("mag_ok", "yes"), "mag_ok must be boolean"),
        (lambda d: d.__setitem__("sensor_ok", None), "sensor_ok must be boolean"),
    ],
)
def test_malformed_or_contradictory_status_fails_closed(mutate, message):
    raw = deepcopy(GOOD)
    mutate(raw)
    with pytest.raises(StatusError, match=message):
        parse_status(raw)
