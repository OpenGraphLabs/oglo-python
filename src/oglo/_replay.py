"""Reading an episode back.

An `Episode` has the same shape as a `Glove`: `.info`, `.tactile()`, `.imu()`,
`.mag()`, yielding the same `Frame` / `ImuSample` / `MagSample` objects. That is the
point. A team can write and test their whole pipeline against recorded data and then
swap `oglo.replay(path)` for `oglo.connect()` without touching anything downstream,
which means the pipeline can be finished before the gloves arrive.

Replay reproduces what was recorded and does not reprocess it. There is no option to
re-threshold or re-zero on the way out: the counts on disk were produced under the
calibration named in `meta.json`, and quietly applying a different one would make the
replayed data disagree with the live data it is supposed to stand in for.
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
        raise ReplayError(f"schema-2 meta.json is missing required field {name!r}")
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
        raise ReplayError(f"complete schema-2 episode requires non-empty {name}")
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
        raise ReplayError(f"complete schema-2 episode has unhealthy {name}")
    if has_mag and not value["mag_ok"]:
        raise ReplayError(f"complete schema-2 episode has mag_ok=false in {name}")
    if value["mag_required"] is not has_mag:
        raise ReplayError(f"meta.json {name}.mag_required disagrees with has_mag")
    return value


def _schema2_integrity(
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
        raise ReplayError("complete schema-2 episode requires finite start and end clocks")
    if error is not None:
        raise ReplayError("complete schema-2 episode must have error=null")
    if counts["tactile"] == 0 or counts["imu"] == 0 or (has_mag and counts["mag"] == 0):
        raise ReplayError("complete schema-2 episode is missing a required fitted stream")

    start = _complete_status("status_start", status_start, has_mag=has_mag)
    end = _complete_status("status_end", status_end, has_mag=has_mag)
    if end["uptime_ms"] < start["uptime_ms"]:
        raise ReplayError("complete schema-2 episode records a device reset")

    for name, values in (
        ("dropped_start", dropped_start),
        ("dropped_end", dropped_end),
        ("dropped", dropped),
    ):
        missing = _HOST_LOSS_NAMES - set(values)
        if missing:
            raise ReplayError(f"complete schema-2 episode {name} lacks counters: {sorted(missing)}")
    if set(dropped_start) != set(dropped_end) or set(dropped_start) != set(dropped):
        raise ReplayError("complete schema-2 episode host-loss counter sets disagree")
    for name in dropped_start:
        before, after, delta = dropped_start[name], dropped_end[name], dropped[name]
        if after < before or delta != after - before:
            raise ReplayError(
                f"complete schema-2 episode has inconsistent host-loss counter {name}"
            )
        if delta != 0:
            raise ReplayError(f"complete schema-2 episode records host loss in {name}")

    if set(device_deltas) != _DEVICE_COUNTER_NAMES:
        raise ReplayError(
            "complete schema-2 episode device_counters_during_capture must contain "
            "exactly tag_dropped, tag_short_writes, and deadline_misses"
        )
    for name in _DEVICE_COUNTER_NAMES:
        before, after, delta = start[name], end[name], device_deltas[name]
        if after < before or delta != after - before:
            raise ReplayError(f"complete schema-2 episode has inconsistent device counter {name}")
        if delta != 0:
            raise ReplayError(f"complete schema-2 episode records device loss in {name}")


def _schema2_info(meta: Dict[str, Any]) -> Info:
    """Validate the writer-owned schema-2 identity/config contract without coercion."""
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
    pair_id = _json_string(meta, "pair_id", allow_empty=True)
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

    _schema2_integrity(meta, complete=complete, has_mag=has_mag, counts=counts)

    return Info(
        serial=serial,
        side=side,
        hw_rev=hw_rev,
        fw_rev=fw_rev,
        rate_hz=rate_hz,
        channels=list(channels),
        has_mag=has_mag,
        transport="replay",
        pair_id=pair_id,
        zero_valid=zero_valid,
        stream_clean=stream_clean,
        stream_thr=stream_thr,
        imu_period_ms=imu_period_ms,
        device_dropped=device_dropped,
        raw=dict(meta),
    )


def _schema1_info(meta: Dict[str, Any]) -> Info:
    """Preserve the pre-schema-2 permissive defaults, but normalize bad casts."""
    if meta.get("side", "right") not in ("left", "right"):
        raise ReplayError("meta.json side must be 'left' or 'right'")
    try:
        return Info(
            serial=meta.get("serial", ""),
            side=meta.get("side", "right"),
            hw_rev=meta.get("hw_rev", ""),
            fw_rev=meta.get("fw_rev", ""),
            rate_hz=int(meta.get("rate_hz", 0) or 0),
            channels=list(meta.get("channels", [])),
            has_mag=bool(meta.get("has_mag", False)),
            transport="replay",
            pair_id=str(meta.get("pair_id", "") or ""),
            zero_valid=bool(meta.get("zero_valid", False)),
            stream_clean=bool(meta.get("stream_clean", False)),
            stream_thr=int(meta.get("stream_thr", 0) or 0),
            imu_period_ms=(
                int(meta["imu_period_ms"])
                if meta.get("imu_period_ms") is not None
                else None
            ),
            device_dropped=int(meta.get("device_dropped_at_connect", 0) or 0),
            raw=dict(meta),
        )
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"invalid schema-1 metadata: {exc}") from exc


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
        schema_value = self.meta.get("schema", 1)
        if isinstance(schema_value, bool) or not isinstance(schema_value, int):
            raise ReplayError("meta.json schema must be an integer")
        self.schema = schema_value
        if self.schema not in (1, 2):
            raise ReplayError(f"episode schema {self.schema} is not supported")
        if self.schema == 2:
            self._info = _schema2_info(self.meta)
        else:
            if "complete" in self.meta and type(self.meta["complete"]) is not bool:
                raise ReplayError("meta.json complete must be boolean")
            self._info = _schema1_info(self.meta)

    @property
    def info(self) -> Info:
        """Identity and the calibration that was in force when this was captured."""
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

    def _load(self, name: str) -> Optional[Dict[str, np.ndarray]]:
        p = self.dir / f"{name}.npz"
        if not p.exists():
            expected = self.meta.get("counts", {})
            expected_n = expected.get(name) if isinstance(expected, dict) else None
            if self.schema >= 2 or (isinstance(expected_n, int) and expected_n > 0):
                raise ReplayError(
                    f"{p.name} is missing from a schema-{self.schema} episode "
                    f"(meta count={expected_n!r})"
                )
            return None
        try:
            with np.load(p, allow_pickle=False) as z:
                out = {k: z[k] for k in z.files}
        except Exception as exc:
            raise ReplayError(f"could not read {p}: {exc}") from exc
        self._validate_arrays(name, out)
        return out

    def _validate_arrays(self, name: str, data: Dict[str, np.ndarray]) -> None:
        payload = {
            "tactile": ("counts",),
            "imu": ("accel", "gyro"),
            "mag": ("field",),
        }[name]
        required = {"seq", "t_us", "host_t", "dropped", *payload}
        if self.schema >= 2:
            required |= {"device_time_us", "host_t_ns", "host_received_ns"}
        missing = required - set(data)
        if missing:
            raise ReplayError(f"{name}.npz is missing columns: {sorted(missing)}")

        if data["seq"].ndim != 1:
            raise ReplayError(f"{name}.npz seq must be a one-dimensional column")
        n = len(data["seq"])
        try:
            bad_lengths = {key: len(value) for key, value in data.items() if len(value) != n}
        except TypeError as exc:
            raise ReplayError(f"{name}.npz contains a scalar where a sample column is required") from exc
        if bad_lengths:
            raise ReplayError(f"{name}.npz columns have inconsistent lengths: {bad_lengths}, seq={n}")
        for column in required:
            if data[column].ndim != 1 and column not in payload:
                raise ReplayError(f"{name}.npz {column} must have shape (N,)")
        counts_meta = self.meta.get("counts", {})
        if not isinstance(counts_meta, dict):
            raise ReplayError("meta.json counts must be an object")
        expected_n = counts_meta.get(name)
        if expected_n is not None and (
            isinstance(expected_n, bool) or not isinstance(expected_n, int) or expected_n < 0
        ):
            raise ReplayError(f"meta count for {name} must be a non-negative integer")
        if expected_n is not None and expected_n != n:
            raise ReplayError(f"meta says {expected_n} {name} samples but file contains {n}")

        self._validate_integer_column(name, data, "seq", 0xFFFFFFFF)
        self._validate_integer_column(name, data, "t_us", 0xFFFFFFFF)
        self._validate_integer_column(name, data, "dropped", 0xFFFFFFFF)
        self._validate_float_column(name, data, "host_t")
        if self.schema >= 2:
            self._validate_integer_column(name, data, "device_time_us", np.iinfo(np.uint64).max)
            self._validate_integer_column(name, data, "host_t_ns", np.iinfo(np.uint64).max)
            self._validate_integer_column(name, data, "host_received_ns", np.iinfo(np.uint64).max)
            self._validate_timeline(name, data)

        if name == "tactile":
            counts = data["counts"]
            if counts.shape != (n, 5, 4, 4):
                raise ReplayError(f"tactile counts shape is {counts.shape}, expected {(n, 5, 4, 4)}")
            if counts.size and (
                not np.issubdtype(counts.dtype, np.integer)
                or int(counts.min()) < 0
                or int(counts.max()) > 4095
            ):
                raise ReplayError("tactile counts are not valid 12-bit integers")
        elif name == "imu":
            if data["accel"].shape != (n, 3) or data["gyro"].shape != (n, 3):
                raise ReplayError("IMU accel and gyro must both have shape (N, 3)")
            self._validate_finite_payload(name, data, "accel")
            self._validate_finite_payload(name, data, "gyro")
        elif data["field"].shape != (n, 3):
            raise ReplayError("mag field must have shape (N, 3)")
        else:
            self._validate_finite_payload(name, data, "field")

        if name in ("imu", "mag"):
            has_raw = "raw" in data
            has_valid = "raw_valid" in data
            if has_raw != has_valid:
                raise ReplayError(f"{name}.npz must contain raw and raw_valid together")
            if has_raw:
                width = 6 if name == "imu" else 3
                raw = data["raw"]
                valid = data["raw_valid"]
                if raw.shape != (n, width):
                    raise ReplayError(f"{name}.npz raw must have shape {(n, width)}")
                if not np.issubdtype(raw.dtype, np.integer):
                    raise ReplayError(f"{name}.npz raw must use an integer dtype")
                if raw.size and (int(raw.min()) < -32768 or int(raw.max()) > 32767):
                    raise ReplayError(f"{name}.npz raw is outside signed int16 range")
                if valid.shape != (n,) or valid.dtype != np.dtype(bool):
                    raise ReplayError(f"{name}.npz raw_valid must be a boolean (N,) column")

    def _validate_timeline(self, name: str, data: Dict[str, np.ndarray]) -> None:
        """Cross-check redundant schema-2 clocks, sequences and loss columns."""
        n = len(data["seq"])
        if not n:
            return
        device = data["device_time_us"]
        host_ns = data["host_t_ns"]
        received_ns = data["host_received_ns"]
        complete = self.schema == 2 and self.meta["complete"] is True
        if complete and np.any(device[1:] < device[:-1]):
            raise ReplayError(f"{name}.npz device_time_us must be nondecreasing")
        if np.any(host_ns[1:] < host_ns[:-1]):
            raise ReplayError(f"{name}.npz host_t_ns must be nondecreasing")
        if np.any(received_ns[1:] < received_ns[:-1]):
            raise ReplayError(f"{name}.npz host_received_ns must be nondecreasing")
        if not np.array_equal(host_ns, received_ns):
            raise ReplayError(
                f"{name}.npz host_t_ns must equal its recorded host_received_ns boundary"
            )
        if not np.array_equal(
            np.bitwise_and(device, np.uint64(0xFFFFFFFF)).astype(np.uint32),
            data["t_us"].astype(np.uint32),
        ):
            raise ReplayError(f"{name}.npz t_us disagrees with device_time_us modulo 2^32")
        if not np.allclose(
            data["host_t"], host_ns.astype(np.float64) / 1_000_000_000.0,
            rtol=0.0, atol=1e-9,
        ):
            raise ReplayError(f"{name}.npz host_t disagrees with host_t_ns")

        if complete and np.any(data["dropped"] != 0):
            raise ReplayError(f"complete schema-2 {name}.npz contains dropped samples")
        seq = data["seq"]
        dropped = data["dropped"]
        last_accepted = int(seq[0])
        for index in range(1, n):
            transition = classify_seq(last_accepted, int(seq[index]))
            if int(dropped[index]) != transition.missing:
                raise ReplayError(
                    f"{name}.npz row {index} sequence transition requires "
                    f"dropped={transition.missing}, got {int(dropped[index])}"
                )
            if transition.kind in ("forward", "wrap"):
                last_accepted = int(seq[index])
            if complete and transition.kind in ("duplicate", "backward"):
                raise ReplayError(
                    f"complete schema-2 {name}.npz contains a {transition.kind} sequence"
                )

    @staticmethod
    def _validate_integer_column(
        stream: str, data: Dict[str, np.ndarray], column: str, maximum: int
    ) -> None:
        values = data[column]
        if not np.issubdtype(values.dtype, np.integer):
            raise ReplayError(f"{stream}.npz {column} must use an integer dtype")
        if values.size and (int(values.min()) < 0 or int(values.max()) > maximum):
            raise ReplayError(f"{stream}.npz {column} is outside 0..{maximum}")

    @staticmethod
    def _validate_float_column(stream: str, data: Dict[str, np.ndarray], column: str) -> None:
        values = data[column]
        if not np.issubdtype(values.dtype, np.floating):
            raise ReplayError(f"{stream}.npz {column} must use a floating-point dtype")
        if values.size and not np.isfinite(values).all():
            raise ReplayError(f"{stream}.npz {column} contains NaN or infinity")

    @staticmethod
    def _validate_finite_payload(stream: str, data: Dict[str, np.ndarray], column: str) -> None:
        values = data[column]
        if not np.issubdtype(values.dtype, np.number):
            raise ReplayError(f"{stream}.npz {column} must be numeric")
        if values.size and not np.isfinite(values).all():
            raise ReplayError(f"{stream}.npz {column} contains NaN or infinity")

    def tactile(self) -> Iterator[Frame]:
        d = self._load("tactile")
        if not d:
            return
        clean = self._info.stream_clean
        for i in range(len(d["seq"])):
            yield Frame(
                seq=int(d["seq"][i]),
                t_us=int(d["t_us"][i]),
                host_t=float(d["host_t"][i]),
                counts=d["counts"][i],
                dropped=int(d["dropped"][i]),
                device_time_us=int(d.get("device_time_us", d["t_us"])[i]),
                host_t_ns=int(d.get("host_t_ns", np.rint(d["host_t"] * 1e9))[i]),
                host_received_ns=int(
                    d.get("host_received_ns", d.get("host_t_ns", np.rint(d["host_t"] * 1e9)))[i]
                ),
                # Carried from the recording, not chosen now. A replayed frame must
                # answer `.residual` exactly as the live one did.
                _stream_clean=clean,
            )

    def imu(self) -> Iterator[ImuSample]:
        d = self._load("imu")
        if not d:
            return
        for i in range(len(d["seq"])):
            yield ImuSample(
                seq=int(d["seq"][i]),
                t_us=int(d["t_us"][i]),
                host_t=float(d["host_t"][i]),
                accel=tuple(float(x) for x in d["accel"][i]),
                gyro=tuple(float(x) for x in d["gyro"][i]),
                dropped=int(d["dropped"][i]),
                device_time_us=int(d.get("device_time_us", d["t_us"])[i]),
                host_t_ns=int(d.get("host_t_ns", np.rint(d["host_t"] * 1e9))[i]),
                host_received_ns=int(
                    d.get("host_received_ns", d.get("host_t_ns", np.rint(d["host_t"] * 1e9)))[i]
                ),
                raw=(
                    tuple(int(x) for x in d["raw"][i])
                    if "raw" in d and bool(d["raw_valid"][i])
                    else None
                ),
            )

    def mag(self) -> Iterator[MagSample]:
        d = self._load("mag")
        if not d:
            return
        for i in range(len(d["seq"])):
            yield MagSample(
                seq=int(d["seq"][i]),
                t_us=int(d["t_us"][i]),
                host_t=float(d["host_t"][i]),
                field=tuple(float(x) for x in d["field"][i]),
                dropped=int(d["dropped"][i]),
                device_time_us=int(d.get("device_time_us", d["t_us"])[i]),
                host_t_ns=int(d.get("host_t_ns", np.rint(d["host_t"] * 1e9))[i]),
                host_received_ns=int(
                    d.get("host_received_ns", d.get("host_t_ns", np.rint(d["host_t"] * 1e9)))[i]
                ),
                raw=(
                    tuple(int(x) for x in d["raw"][i])
                    if "raw" in d and bool(d["raw_valid"][i])
                    else None
                ),
            )

    # -- whole arrays, for anyone who would rather not iterate -------------------

    def arrays(self, stream: str = "tactile") -> Dict[str, np.ndarray]:
        """The raw arrays for one stream. Nothing is copied or converted."""
        d = self._load(stream)
        if d is None:
            raise ReplayError(f"{self.dir} has no {stream}.npz")
        return d

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
            if not d or len(d["seq"]) == 0:
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
