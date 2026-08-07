"""Runtime health returned by firmware ``GET STATUS`` / BLE log characteristic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


class StatusError(ValueError):
    pass


@dataclass(frozen=True)
class DeviceStatus:
    uptime_ms: int
    seq: int
    imu_ok: bool
    mag_ok: bool
    sensor_ok: bool
    error_flags: int
    deadline_misses: int
    tag_dropped: int
    tag_short_writes: int
    #: Filled by ``Glove.status()`` from CONFIG. The raw status packet cannot know
    #: whether a false ``mag_ok`` means an absent optional part or a failed fitted one.
    mag_required: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return (
            self.imu_ok
            and (not self.mag_required or self.mag_ok)
            and self.sensor_ok
            and self.error_flags == 0
        )


def parse_status(raw: Dict[str, Any]) -> DeviceStatus:
    if not isinstance(raw, dict) or not raw:
        raise StatusError("status is empty")
    if "imu" not in raw:
        raise StatusError("status is missing imu")
    imu_value = raw["imu"]
    if not isinstance(imu_value, dict):
        raise StatusError("imu status must be an object")
    imu = imu_value

    def nonnegative(name: str, *, maximum: int = 0xFFFFFFFF) -> int:
        if name not in raw:
            raise StatusError(f"status is missing {name}")
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise StatusError(f"{name} must be an integer")
        if value < 0:
            raise StatusError(f"{name} cannot be negative")
        if value > maximum:
            raise StatusError(f"{name} exceeds its {maximum}-maximum wire counter")
        return value

    def boolean(name: str, value: Any) -> bool:
        if value is not True and value is not False:
            raise StatusError(f"{name} must be boolean")
        return value

    if "imu_ok" not in raw:
        raise StatusError("status is missing imu_ok")
    if "ok" not in imu:
        raise StatusError("status imu object is missing ok")
    if "mag_ok" not in imu:
        raise StatusError("status imu object is missing mag_ok")
    if "sensor_ok" not in raw:
        raise StatusError("status is missing sensor_ok")
    imu_ok_value = raw["imu_ok"]
    imu_sample_ok = boolean("imu.ok", imu["ok"])
    imu_ok = boolean("imu_ok", imu_ok_value)
    if imu_ok != imu_sample_ok:
        raise StatusError(
            f"status imu_ok={imu_ok} disagrees with imu.ok={imu_sample_ok}"
        )
    mag_ok_value = imu["mag_ok"]

    return DeviceStatus(
        uptime_ms=nonnegative("uptime_ms"),
        seq=nonnegative("seq"),
        imu_ok=imu_ok,
        mag_ok=boolean("mag_ok", mag_ok_value),
        sensor_ok=boolean("sensor_ok", raw["sensor_ok"]),
        error_flags=nonnegative("error_flags"),
        deadline_misses=nonnegative("deadline_misses"),
        tag_dropped=nonnegative("tag_dropped"),
        tag_short_writes=nonnegative("tag_short_writes"),
        raw=dict(raw),
    )
