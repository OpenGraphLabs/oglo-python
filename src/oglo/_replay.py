"""Replay recorded samples through the same stream interface as a live Glove.

Counts retain their recorded mode and threshold; replay never re-zeros or
re-thresholds. The recorded zero recipe is saved beside the sensor JSONL when available.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import numpy as np

from ._config import MIN_FIRMWARE, Info, _fw_at_least
from ._frame import Frame, ImuSample, MagSample
from ._wire import classify_seq
from ._jsonl import (stream_filename, read_arrays, json_rows, row_sample,
                     baseline_for, calibration_document, COMMON_DTYPES)


class ReplayError(RuntimeError):
    pass


_STREAM_NAMES = ("tactile", "imu", "mag")
_CHANNEL_NAMES = {"thumb", "index", "middle", "ring", "pinky"}
_HOST_LOSS_NAMES = {
    *(f"wire_{name}" for name in _STREAM_NAMES),
    *(f"overflow_{name}" for name in _STREAM_NAMES),
    *(f"duplicate_{name}" for name in _STREAM_NAMES),
    *(f"backward_{name}" for name in _STREAM_NAMES),
    "transport_overflow_ble",
    "transport_malformed_ble",
    "transport_malformed_usb",
    "transport_stale_imu_ble",
    "unrouted_packets",
}
_DEVICE_COUNTER_NAMES = {"tag_dropped", "tag_short_writes", "deadline_misses"}
_STATUS_INT_NAMES = {
    "uptime_ms",
    "seq",
    "error_flags",
    "deadline_misses",
    "tag_dropped",
    "tag_short_writes",
}
_STATUS_BOOL_NAMES = {"imu_ok", "mag_ok", "sensor_ok", "mag_required"}


def _required(meta: Dict[str, Any], name: str) -> Any:
    if name not in meta:
        raise ReplayError(f"schema-3 meta.json is missing required field {name!r}")
    return meta[name]


def _json_string(meta: Dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = _required(meta, name)
    if type(value) is not str or (not allow_empty and not value):
        requirement = "a JSON string" if allow_empty else "a non-empty JSON string"
        raise ReplayError(f"meta.json {name} must be {requirement}")
    return value


def _json_bool(meta: Dict[str, Any], name: str) -> bool:
    value = _required(meta, name)
    if type(value) is not bool:
        raise ReplayError(f"meta.json {name} must be a JSON boolean")
    return value


def _json_int(meta: Dict[str, Any], name: str, minimum: int, maximum: Optional[int] = None) -> int:
    value = _required(meta, name)
    if type(value) is not int:
        raise ReplayError(f"meta.json {name} must be a JSON integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise ReplayError(f"meta.json {name} must be {bound}, got {value}")
    return value


def _finite_number_or_none(meta: Dict[str, Any], name: str) -> Optional[float]:
    value = _required(meta, name)
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ReplayError(f"meta.json {name} must be null or a finite JSON number")
    try:
        normalized = float(value)
    except OverflowError as exc:
        raise ReplayError(f"meta.json {name} must be null or a finite JSON number") from exc
    if not math.isfinite(normalized):
        raise ReplayError(f"meta.json {name} must be null or a finite JSON number")
    return normalized


def _object(
    meta: Dict[str, Any], name: str, *, allow_none: bool = False
) -> Optional[Dict[str, Any]]:
    value = _required(meta, name)
    if value is None and allow_none:
        return None
    if type(value) is not dict:
        suffix = " or null" if allow_none else ""
        raise ReplayError(f"meta.json {name} must be an object{suffix}")
    return value


def _counter_object(
    meta: Dict[str, Any], name: str, *, allow_none_values: bool = False
) -> Dict[str, Any]:
    values = _object(meta, name)
    assert values is not None
    for counter, value in values.items():
        if type(counter) is not str or not counter:
            raise ReplayError(f"meta.json {name} counter names must be non-empty strings")
        if value is None and allow_none_values:
            continue
        if type(value) is not int or value < 0:
            requirement = (
                "non-negative integers or null"
                if allow_none_values
                else "non-negative integers"
            )
            raise ReplayError(f"meta.json {name} counter values must be {requirement}")
    return values


def _complete_status(
    name: str, value: Optional[Dict[str, Any]], *, has_mag: bool
) -> Dict[str, Any]:
    if not value:
        raise ReplayError(f"complete schema-3 episode requires non-empty {name}")
    missing = (_STATUS_INT_NAMES | _STATUS_BOOL_NAMES | {"raw"}) - set(value)
    if missing:
        raise ReplayError(f"meta.json {name} is missing status fields: {sorted(missing)}")
    for field in _STATUS_INT_NAMES:
        item = value[field]
        if type(item) is not int or not 0 <= item <= 0xFFFFFFFF:
            raise ReplayError(f"meta.json {name}.{field} must be an unsigned 32-bit JSON integer")
    for field in _STATUS_BOOL_NAMES:
        if type(value[field]) is not bool:
            raise ReplayError(f"meta.json {name}.{field} must be a JSON boolean")
    if type(value["raw"]) is not dict:
        raise ReplayError(f"meta.json {name}.raw must be an object")
    if not value["imu_ok"] or not value["sensor_ok"] or value["error_flags"] != 0:
        raise ReplayError(f"complete schema-3 episode has unhealthy {name}")
    if has_mag and not value["mag_ok"]:
        raise ReplayError(f"complete schema-3 episode has mag_ok=false in {name}")
    if value["mag_required"] is not has_mag:
        raise ReplayError(f"meta.json {name}.mag_required disagrees with has_mag")
    return value


def _schema3_integrity(
    meta: Dict[str, Any], *, complete: bool, has_mag: bool, counts: Dict[str, int]
) -> None:
    started_wall = _finite_number_or_none(meta, "started_wall")
    started_mono = _finite_number_or_none(meta, "started_monotonic")
    ended_wall = _finite_number_or_none(meta, "ended_wall")
    ended_mono = _finite_number_or_none(meta, "ended_monotonic")
    for label, started, ended in (
        ("wall", started_wall, ended_wall),
        ("monotonic", started_mono, ended_mono),
    ):
        if started is not None and ended is not None and ended < started:
            raise ReplayError(f"meta.json {label} end time precedes its start time")

    status_start = _object(meta, "status_start", allow_none=True)
    status_end = _object(meta, "status_end", allow_none=True)
    dropped = _counter_object(meta, "dropped", allow_none_values=True)
    dropped_start = _counter_object(meta, "dropped_start")
    dropped_end = _counter_object(meta, "dropped_end")
    device_deltas = _counter_object(
        meta, "device_counters_during_capture", allow_none_values=True
    )

    stop_reason = _required(meta, "stop_reason")
    if type(stop_reason) is not str or not stop_reason:
        raise ReplayError("meta.json stop_reason must be a non-empty JSON string")
    error = _required(meta, "error")
    if error is not None and type(error) is not str:
        raise ReplayError("meta.json error must be null or a JSON string")

    # An in-progress or failed marker is intentionally readable even before the
    # recorder has end clocks/status. It must still be structurally typed above.
    if not complete:
        return

    if None in (started_wall, started_mono, ended_wall, ended_mono):
        raise ReplayError("complete schema-3 episode requires finite start and end clocks")
    if error is not None:
        raise ReplayError("complete schema-3 episode must have error=null")
    if counts["tactile"] == 0 or counts["imu"] == 0 or (has_mag and counts["mag"] == 0):
        raise ReplayError("complete schema-3 episode is missing a required fitted stream")

    start = _complete_status("status_start", status_start, has_mag=has_mag)
    end = _complete_status("status_end", status_end, has_mag=has_mag)
    if end["uptime_ms"] < start["uptime_ms"]:
        raise ReplayError("complete schema-3 episode records a device reset")

    for name, values in (
        ("dropped_start", dropped_start),
        ("dropped_end", dropped_end),
        ("dropped", dropped),
    ):
        missing = _HOST_LOSS_NAMES - set(values)
        if missing:
            raise ReplayError(f"complete schema-3 episode {name} lacks counters: {sorted(missing)}")
    if set(dropped_start) != set(dropped_end) or set(dropped_start) != set(dropped):
        raise ReplayError("complete schema-3 episode host-loss counter sets disagree")
    for name in dropped_start:
        before, after, delta = dropped_start[name], dropped_end[name], dropped[name]
        if after < before or delta != after - before:
            raise ReplayError(
                f"complete schema-3 episode has inconsistent host-loss counter {name}"
            )
        if delta != 0:
            raise ReplayError(f"complete schema-3 episode records host loss in {name}")

    if set(device_deltas) != _DEVICE_COUNTER_NAMES:
        raise ReplayError(
            "complete schema-3 episode device_counters_during_capture must contain "
            "exactly tag_dropped, tag_short_writes, and deadline_misses"
        )
    for name in _DEVICE_COUNTER_NAMES:
        before, after, delta = start[name], end[name], device_deltas[name]
        if after < before or delta != after - before:
            raise ReplayError(f"complete schema-3 episode has inconsistent device counter {name}")
        retry_counter = name == "tag_short_writes" and _fw_at_least(meta["fw_rev"], (0, 9, 16))
        if delta != 0 and not retry_counter:
            raise ReplayError(f"complete schema-3 episode records device loss in {name}")


def _schema3_info(meta: Dict[str, Any]) -> Info:
    """Validate the writer-owned schema-3 identity/config contract without coercion."""
    complete = _json_bool(meta, "complete")
    _json_string(meta, "sdk_version")
    serial = _json_string(meta, "serial")
    side = _json_string(meta, "side")
    if side not in ("left", "right"):
        raise ReplayError("meta.json side must be 'left' or 'right'")
    hw_rev = _json_string(meta, "hw_rev")
    fw_rev = _json_string(meta, "fw_rev")
    if not _fw_at_least(fw_rev, MIN_FIRMWARE):
        floor = ".".join(str(part) for part in MIN_FIRMWARE)
        raise ReplayError(f"meta.json fw_rev must be firmware {floor} or newer")
    transport = _json_string(meta, "transport")
    if transport not in ("usb", "ble"):
        raise ReplayError("meta.json transport must be 'usb' or 'ble'")

    channels = _required(meta, "channels")
    if (
        type(channels) is not list
        or len(channels) != 5
        or any(type(channel) is not str for channel in channels)
        or set(channels) != _CHANNEL_NAMES
    ):
        raise ReplayError(
            "meta.json channels must be a JSON list containing each of "
            "thumb, index, middle, ring, and pinky exactly once"
        )

    has_mag = _json_bool(meta, "has_mag")
    zero_valid = _json_bool(meta, "zero_valid")
    stream_clean = _json_bool(meta, "stream_clean")
    if stream_clean and not zero_valid:
        raise ReplayError("meta.json stream_clean=true is impossible when zero_valid=false")
    stream_thr = _json_int(meta, "stream_thr", 0, 4095)
    rate_hz = _json_int(meta, "rate_hz", 1, 1000)
    device_dropped = _json_int(meta, "device_dropped_at_connect", 0)

    imu_period_value = _required(meta, "imu_period_ms")
    if imu_period_value is None:
        imu_period_ms = None
    elif type(imu_period_value) is not int or not 1 <= imu_period_value <= 100:
        raise ReplayError("meta.json imu_period_ms must be null or a JSON integer in 1..100")
    else:
        imu_period_ms = imu_period_value

    counts = _required(meta, "counts")
    if type(counts) is not dict:
        raise ReplayError("meta.json counts must be an object")
    if set(counts) != set(_STREAM_NAMES):
        raise ReplayError("meta.json counts must contain exactly tactile, imu, and mag")
    for name in _STREAM_NAMES:
        value = counts[name]
        if type(value) is not int or value < 0:
            raise ReplayError(f"meta count for {name} must be a non-negative integer JSON value")

    _schema3_integrity(meta, complete=complete, has_mag=has_mag, counts=counts)

    return Info(
        serial=serial,
        side=side,
        hw_rev=hw_rev,
        fw_rev=fw_rev,
        rate_hz=rate_hz,
        channels=list(channels),
        has_mag=has_mag,
        transport="replay",
        zero_valid=zero_valid,
        stream_clean=stream_clean,
        stream_thr=stream_thr,
        imu_period_ms=imu_period_ms,
        device_dropped=device_dropped,
        raw=dict(meta),
        firmware_verification=_firmware_metadata(meta),
    )


class Episode:
    """A recorded episode, iterated like a live glove."""

    def __init__(self, path: Any) -> None:
        self.dir = Path(path)
        meta_path = self.dir / "meta.json"
        if not meta_path.exists():
            raise ReplayError(
                f"{self.dir} is not an episode (no meta.json). "
                "Point at the ep_NNNN directory, not the folder holding them."
            )
        try:
            parsed = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            raise ReplayError(f"could not read {meta_path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ReplayError("meta.json must contain one JSON object")
        self.meta: Dict[str, Any] = parsed
        schema_value = _required(self.meta, "schema")
        if isinstance(schema_value, bool) or not isinstance(schema_value, int):
            raise ReplayError("meta.json schema must be an integer")
        self.schema = schema_value
        if self.schema != 3:
            raise ReplayError(f"episode schema {self.schema} is not supported; expected JSONL schema 3")
        self._info = _schema3_info(self.meta)
        self._clock_domain = _json_string(self.meta, "clock_domain")
        self._uncertainty_ns = _json_int(self.meta, "uncertainty_ns", 0, 2**64 - 1)
        self._baseline = None
        calibration = _required(self.meta, "calibration")
        if calibration is not None:
            expected = f"tactile_{self.info.side}.calibration.json"
            if calibration != expected:
                raise ReplayError(f"calibration must be {expected!r} or null")
            path = self.dir / calibration
            if path.exists() or self.meta["complete"]:
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    self._baseline = baseline_for(self.info, document["zero"])
                    expected_document = calibration_document(self.info, document["zero"], self._baseline)
                    for key in ("schema", "stream_id", "side", "sample", "transform"):
                        if document[key] != expected_document[key]:
                            raise ValueError(f"calibration {key} disagrees with episode metadata")
                    for key in ("serial", "hw_rev", "fw_rev"):
                        if document["device"][key] != getattr(self.info, key):
                            raise ValueError(f"calibration {key} disagrees with episode metadata")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise ReplayError(f"could not read calibration: {exc}") from exc
        clean_file = _required(self.meta, "clean_file")
        if clean_file is not None and clean_file != f"tactile_{self.info.side}.jsonl":
            raise ReplayError("clean_file must name this hand's CLEAN tactile file or be null")
        if self.meta["complete"] and (clean_file is None or
                (not self.info.stream_clean and self._baseline is None)):
            raise ReplayError("complete episode requires CLEAN tactile and its RAW calibration recipe")

    @property
    def info(self) -> Info:
        """Identity, calibration state, and stream settings at capture time."""
        return self._info

    def __repr__(self) -> str:
        c = self.meta.get("counts", {})
        return (
            f"<Episode {self.dir.name}{' partial' if not self.meta.get('complete', True) else ''} "
            f"{self._info.serial} {self._info.side} "
            f"fw={self._info.fw_rev} tactile={c.get('tactile', 0)} "
            f"imu={c.get('imu', 0)} mag={c.get('mag', 0)}>"
        )

    def __iter__(self) -> Iterator[Frame]:
        """Iterating an episode gives its tactile frames, the common case."""
        return self.tactile()

    def __len__(self) -> int:
        try:
            return int(self.meta.get("counts", {}).get("tactile", 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReplayError(f"invalid meta.json tactile count: {exc}") from exc

    def _load(self, name: str) -> Dict[str, np.ndarray]:
        try:
            path = self.dir / stream_filename(name, self.info)
        except ValueError as exc:
            raise ReplayError(str(exc)) from exc
        if not path.is_file():
            raise ReplayError(f"{path.name} is missing (meta count={self.meta['counts'].get(name)!r})")
        try:
            data = read_arrays(path, name, self.info, clock_domain=self._clock_domain,
                               uncertainty_ns=self._uncertainty_ns)
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ReplayError(f"could not read {path}: {exc}") from exc
        self._validate_arrays(name, data)
        if name == "tactile" and not self.info.stream_clean and self.meta["clean_file"] is not None:
            self._validate_clean(data)
        return data

    def _validate_clean(self, original: Dict[str, np.ndarray]) -> None:
        if self._baseline is None:
            raise ReplayError("derived CLEAN tactile requires a valid calibration recipe")
        path = self.dir / self.meta["clean_file"]
        count = 0
        try:
            baseline = np.asarray(list(self._baseline.values()), dtype=np.int64).reshape(5, 4, 4)
            for index, row in enumerate(json_rows(path)):
                if index >= len(original["seq"]):
                    raise ValueError("CLEAN tactile has more rows than RAW")
                sample = row_sample(row, "tactile", self.info, index,
                                    self._clock_domain, self._uncertainty_ns)
                if any(sample[key] != original[key][index] for key in COMMON_DTYPES):
                    raise ValueError("CLEAN tactile timing/sequence metadata differs from RAW")
                expected = np.maximum(0, original["counts"][index].astype(np.int64) - baseline)
                expected[expected < self.info.stream_thr] = 0
                if not np.array_equal(np.asarray(sample["counts"]).reshape(5, 4, 4), expected):
                    raise ValueError("CLEAN tactile values disagree with RAW and calibration")
                count += 1
            if count != len(original["seq"]):
                raise ValueError("CLEAN and RAW tactile row counts disagree")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ReplayError(f"could not validate {path.name}: {exc}") from exc

    def _validate_arrays(self, name: str, data: Dict[str, np.ndarray]) -> None:
        # JSON rows were type/range/shape checked before building these arrays.
        expected = self.meta["counts"][name]
        count = len(data["seq"])
        if count != expected:
            raise ReplayError(f"meta says {expected} {name} samples but file contains {count}")
        self._validate_timeline(name, data)
        if name in ("imu", "mag") and self.meta["complete"] and not data["raw_valid"].all():
            raise ReplayError(f"complete {name} stream requires raw integer samples")

    def _validate_timeline(self, name: str, data: Dict[str, np.ndarray]) -> None:
        """Cross-check recorded clocks, sequence transitions, and loss."""
        label = stream_filename(name, self.info)
        n = len(data["seq"])
        if not n:
            return
        device = data["device_time_us"]
        host_ns = data["host_t_ns"]
        received_ns = data["host_received_ns"]
        complete = self.meta["complete"]
        if complete and np.any(device[1:] < device[:-1]):
            raise ReplayError(f"{label} device_time_us must be nondecreasing")
        if np.any(host_ns[1:] < host_ns[:-1]):
            raise ReplayError(f"{label} host_t_ns must be nondecreasing")
        if np.any(received_ns[1:] < received_ns[:-1]):
            raise ReplayError(f"{label} host_received_ns must be nondecreasing")
        if not np.array_equal(host_ns, received_ns):
            raise ReplayError(
                f"{label} host_t_ns must equal its recorded host_received_ns boundary"
            )
        if not np.array_equal(
            np.bitwise_and(device, np.uint64(0xFFFFFFFF)).astype(np.uint32),
            data["t_us"].astype(np.uint32),
        ):
            raise ReplayError(f"{label} t_us disagrees with device_time_us modulo 2^32")
        if not np.allclose(
            data["host_t"], host_ns.astype(np.float64) / 1_000_000_000.0,
            rtol=0.0, atol=1e-9,
        ):
            raise ReplayError(f"{label} host_t disagrees with host_t_ns")

        if complete and np.any(data["dropped"] != 0):
            raise ReplayError(f"complete schema-3 {label} contains dropped samples")
        seq = data["seq"]
        dropped = data["dropped"]
        last_accepted = int(seq[0])
        for index in range(1, n):
            transition = classify_seq(last_accepted, int(seq[index]))
            if int(dropped[index]) != transition.missing:
                raise ReplayError(
                    f"{label} row {index} sequence transition requires "
                    f"dropped={transition.missing}, got {int(dropped[index])}"
                )
            if transition.kind in ("forward", "wrap"):
                last_accepted = int(seq[index])
            if complete and transition.kind in ("duplicate", "backward"):
                raise ReplayError(
                    f"complete schema-3 {label} contains a {transition.kind} sequence"
                )

    def tactile(self) -> Iterator[Frame]:
        d = self._load("tactile")
        clean = self._info.stream_clean
        for i in range(len(d["seq"])):
            yield Frame(
                seq=int(d["seq"][i]),
                t_us=int(d["t_us"][i]),
                host_t=float(d["host_t"][i]),
                counts=d["counts"][i],
                dropped=int(d["dropped"][i]),
                device_time_us=int(d["device_time_us"][i]),
                host_t_ns=int(d["host_t_ns"][i]),
                host_received_ns=int(d["host_received_ns"][i]),
                # Carried from the recording, not chosen now. A replayed frame must
                # answer `.residual` exactly as the live one did.
                _stream_clean=clean,
            )

    def imu(self) -> Iterator[ImuSample]:
        d = self._load("imu")
        for i in range(len(d["seq"])):
            yield ImuSample(
                seq=int(d["seq"][i]),
                t_us=int(d["t_us"][i]),
                host_t=float(d["host_t"][i]),
                accel=tuple(float(x) for x in d["accel"][i]),
                gyro=tuple(float(x) for x in d["gyro"][i]),
                dropped=int(d["dropped"][i]),
                device_time_us=int(d["device_time_us"][i]),
                host_t_ns=int(d["host_t_ns"][i]),
                host_received_ns=int(d["host_received_ns"][i]),
                raw=(
                    tuple(int(x) for x in d["raw"][i])
                    if bool(d["raw_valid"][i])
                    else None
                ),
            )

    def mag(self) -> Iterator[MagSample]:
        d = self._load("mag")
        for i in range(len(d["seq"])):
            yield MagSample(
                seq=int(d["seq"][i]),
                t_us=int(d["t_us"][i]),
                host_t=float(d["host_t"][i]),
                field=tuple(float(x) for x in d["field"][i]),
                dropped=int(d["dropped"][i]),
                device_time_us=int(d["device_time_us"][i]),
                host_t_ns=int(d["host_t_ns"][i]),
                host_received_ns=int(d["host_received_ns"][i]),
                raw=(
                    tuple(int(x) for x in d["raw"][i])
                    if bool(d["raw_valid"][i])
                    else None
                ),
            )

    # -- whole arrays, for anyone who would rather not iterate -------------------

    def arrays(self, stream: str = "tactile") -> Dict[str, np.ndarray]:
        """Read and validate a JSONL stream as NumPy arrays."""
        return self._load(stream)

    def summary(self) -> Dict[str, Any]:
        """Counts, duration and delivered rate per stream, computed from the data.

        Rates come from `host_t` rather than from `rate_hz` in the metadata, because
        the question a dataset has to answer is what arrived, not what was requested.
        """
        out: Dict[str, Any] = {
            "serial": self._info.serial,
            "side": self._info.side,
            "fw_rev": self._info.fw_rev,
            "stream_clean": self._info.stream_clean,
            "stream_thr": self._info.stream_thr,
            "complete": bool(self.meta.get("complete", True)),
            "error": self.meta.get("error"),
        }
        for name in ("tactile", "imu", "mag"):
            d = self._load(name)
            if len(d["seq"]) == 0:
                out[name] = {"n": 0}
                continue
            host = d["host_t"]
            span = float(host[-1] - host[0])
            out[name] = {
                "n": int(len(d["seq"])),
                "seconds": round(span, 3),
                "hz": round((len(d["seq"]) - 1) / span, 1) if span > 0 else 0.0,
                "dropped": int(d["dropped"].sum()),
            }
        return out


def replay(path: Any) -> Episode:
    """Open a recorded episode. Iterate it exactly as you would a live glove."""
    return Episode(path)


def _firmware_metadata(meta):
    value = meta.get("firmware_verification")
    if value is None:
        return None
    required = {"running_image_sha256", "usb_serial", "policy_id", "policy_sha256",
                "sdk_version", "verified_at", "attempt"}
    if not isinstance(value, dict) or set(value) != required or any(not isinstance(v, str) or not v for v in value.values()):
        raise ReplayError("invalid firmware verification metadata")
    import re
    for name in ("running_image_sha256", "policy_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", value[name]):
            raise ReplayError("invalid firmware verification hash")
    if not re.fullmatch(r"[0-9a-fA-F]{12}", value["usb_serial"]):
        raise ReplayError("invalid firmware USB identity")
    return dict(value)
