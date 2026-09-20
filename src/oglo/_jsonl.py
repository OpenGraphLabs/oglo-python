"""The og-skill sensor row contract, with lossless SDK replay metadata."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np


STREAMS = ("tactile", "imu", "mag")
INERTIAL_CHANNELS = {
    "imu": ("ax", "ay", "az", "gx", "gy", "gz"),
    "mag": ("mx", "my", "mz"),
}
COMMON_DTYPES = {
    "seq": "uint32", "t_us": "uint32", "device_time_us": "uint64",
    "host_t": "float64", "host_t_ns": "uint64", "host_received_ns": "uint64",
    "dropped": "uint32",
}
CLEAN_FORMULA = "v=max(0,raw-baseline); if v<threshold then 0 else v"
MAX_ROW_BYTES = 65536


def validate_clock(clock_domain, uncertainty_ns):
    if not isinstance(clock_domain, str) or not clock_domain.strip():
        raise ValueError("clock_domain must be a non-empty host identifier")
    if type(uncertainty_ns) is not int or not 0 <= uncertainty_ns <= 2**64 - 1:
        raise ValueError("uncertainty_ns must be an unsigned 64-bit integer")


def channel_names(name, info):
    if name == "tactile":
        return [f"{finger}_{row}_{col}" for finger in info.channels
                for row in range(4) for col in range(4)]
    return list(INERTIAL_CHANNELS[name])


def stream_filename(name, info):
    if name not in STREAMS:
        raise ValueError(f"unknown stream {name!r}; choose tactile, imu, or mag")
    if name == "tactile":
        suffix = "" if info.stream_clean else ".raw"
        return f"tactile_{info.side}{suffix}.jsonl"
    return f"wrist_{name}_{info.side}.jsonl"


def _integer(value, name, maximum, minimum=0):
    if type(value) is not int:
        raise ValueError(f"{name} must use an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside {minimum}..{maximum}")
    return value


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} contains NaN or infinity")
    return value


def _vector(value, name, width, *, integer=False, minimum=-32768, maximum=32767):
    if type(value) is not list or len(value) != width:
        raise ValueError(f"{name} must have {width} values")
    for item in value:
        if integer:
            _integer(item, name, maximum, minimum)
        else:
            _number(item, name)
            if abs(item) > np.finfo(np.float32).max:
                raise ValueError(f"{name} is outside float32 range")
    return value


def make_row(name, info, index, common, a, b=None, raw=None, *,
             clock_domain="local_host", uncertainty_ns=500_000):
    """Serialize once at acquisition; every saved stream uses backend channel units."""
    source = {key: value.item() if isinstance(value, np.generic) else value
              for key, value in common.items()}
    names = channel_names(name, info)
    if name == "tactile":
        counts = np.asarray(a)
        if counts.shape != (5, 4, 4):
            raise ValueError("tactile counts must have shape (5, 4, 4)")
        values = _vector(counts.reshape(80).tolist(), "counts", 80,
                         integer=True, minimum=0, maximum=4095)
    else:
        source["raw_valid"] = raw is not None
        source["accel" if name == "imu" else "field"] = np.asarray(a, dtype=np.float32).tolist()
        if name == "imu":
            source["gyro"] = np.asarray(b, dtype=np.float32).tolist()
        values = [] if raw is None else _vector(
            np.asarray(raw).tolist(), "raw sensor sample", len(names), integer=True
        )
    row = {
        "frame_number": index,
        "capture_ns": source["host_received_ns"],
        "clock_source": "host_monotonic",
        "clock_domain": clock_domain,
        "uncertainty_ns": uncertainty_ns,
        "channels": dict(zip(names, values)),
        "device_timestamp_ns": source["t_us"] * 1000,
        "oglo": source,
    }
    # Validate before a JSON number or NumPy cast could silently lose information.
    row_sample(row, name, info, index, clock_domain, uncertainty_ns)
    return row


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"JSON contains NaN or infinity: {value}")


def json_rows(path):
    """Reject blank, torn, oversized, duplicate-key, and nonstandard JSON rows."""
    with Path(path).open("rb") as source:
        index = 0
        while line := source.readline(MAX_ROW_BYTES + 1):
            if len(line) > MAX_ROW_BYTES:
                raise ValueError(f"row {index} exceeds {MAX_ROW_BYTES} bytes")
            if not line.endswith(b"\n"):
                raise ValueError(f"row {index} is truncated (missing newline)")
            value = json.loads(line, object_pairs_hook=_unique_object,
                               parse_constant=_invalid_constant)
            if type(value) is not dict:
                raise ValueError(f"row {index} must be a JSON object")
            yield value
            index += 1


def row_sample(row, name, info, index, clock_domain, uncertainty_ns):
    """Read the canonical channels and validate the SDK's extra replay fields."""
    frame = _integer(row["frame_number"], "frame_number", 2**64 - 1)
    if frame != index:
        raise ValueError(f"frame_number must be consecutive: expected {index}, got {frame}")
    capture = _integer(row["capture_ns"], "capture_ns", 2**64 - 1)
    device = _integer(row["device_timestamp_ns"], "device_timestamp_ns", (2**32 - 1) * 1000)
    if row["clock_source"] != "host_monotonic" or row["clock_domain"] != clock_domain:
        raise ValueError("row clock source/domain disagrees with episode metadata")
    uncertainty = _integer(row["uncertainty_ns"], "uncertainty_ns", 2**64 - 1)
    if uncertainty != uncertainty_ns:
        raise ValueError("row uncertainty_ns disagrees with episode metadata")
    extra = row["oglo"]
    if type(extra) is not dict:
        raise ValueError("oglo replay metadata must be an object")
    sample = {}
    for key, dtype in COMMON_DTYPES.items():
        value = extra[key]
        sample[key] = (_number(value, key) if dtype == "float64" else
                       _integer(value, key, int(np.iinfo(dtype).max)))
    if capture != sample["host_received_ns"]:
        raise ValueError("capture_ns disagrees with host_received_ns")
    if device != sample["t_us"] * 1000:
        raise ValueError("device_timestamp_ns disagrees with t_us")
    channels = row["channels"]
    if type(channels) is not dict:
        raise ValueError("channels must be an object")
    names = channel_names(name, info)
    if name == "tactile":
        if set(channels) != set(names):
            raise ValueError("tactile channels must contain the recorded 80 taxel labels")
        values = _vector([channels[key] for key in names], "counts", 80,
                         integer=True, minimum=0, maximum=4095)
        sample["counts"] = [values[i:i+16] for i in range(0, 80, 16)]
    else:
        valid = extra["raw_valid"]
        if type(valid) is not bool:
            raise ValueError("raw_valid must be a boolean")
        if set(channels) != (set(names) if valid else set()):
            raise ValueError(f"{name} channels disagree with raw_valid")
        sample["raw"] = (_vector([channels[key] for key in names], "raw", len(names), integer=True)
                         if valid else [0] * len(names))
        sample["raw_valid"] = valid
        for key in (("accel", "gyro") if name == "imu" else ("field",)):
            sample[key] = _vector(extra[key], key, 3)
    return sample


def read_arrays(path, name, info, *, clock_domain, uncertainty_ns):
    columns = {key: [] for key in COMMON_DTYPES}
    if name == "tactile":
        payload = {"counts": ("uint16", (5, 4, 4))}
    else:
        payload = {
            "raw": ("int16", (len(INERTIAL_CHANNELS[name]),)),
            "raw_valid": ("bool", ()),
        }
        if name == "imu":
            payload.update(accel=("float32", (3,)), gyro=("float32", (3,)))
        else:
            payload["field"] = ("float32", (3,))
    columns.update({key: [] for key in payload})
    for index, row in enumerate(json_rows(path)):
        sample = row_sample(row, name, info, index, clock_domain, uncertainty_ns)
        for key in columns:
            columns[key].append(sample[key])
    count = len(columns["seq"])
    return {
        **{key: np.asarray(columns[key], dtype=dtype) for key, dtype in COMMON_DTYPES.items()},
        **{key: np.asarray(columns[key], dtype=dtype).reshape((count, *shape))
           for key, (dtype, shape) in payload.items()},
    }


def baseline_for(info, calibration):
    if calibration is None:
        return None
    if type(calibration) is not dict or type(calibration.get("valid")) is not bool:
        raise ValueError("calibration must be a GET ZERO object with a boolean valid field")
    if calibration["valid"] != info.zero_valid:
        raise ValueError("GET ZERO validity must match the recorded episode")
    if not calibration["valid"]:
        return None
    values = _vector(calibration.get("baseline"), "calibration baseline", 80,
                     integer=True, minimum=0, maximum=4095)
    _vector(calibration.get("noise"), "calibration noise", 80,
            integer=True, minimum=0, maximum=4095)
    if not info.zero_valid or type(calibration.get("count")) is not int or calibration["count"] != 80:
        raise ValueError("calibration requires a valid recorded zero and 80 baselines")
    if (type(calibration.get("thr")) is not int or calibration["thr"] != info.stream_thr
            or type(calibration.get("clean")) is not bool or calibration["clean"] != info.stream_clean):
        raise ValueError("GET ZERO mode/threshold must match the recorded episode")
    return dict(zip(channel_names("tactile", info), values))


def calibration_document(info, calibration, baseline):
    clean_available = info.stream_clean or baseline is not None
    if info.stream_clean:
        mode = "firmware_clean"
    elif clean_available:
        mode = "host_clean_from_raw"
    else:
        mode = "raw"
    transform = {
        "mode": mode,
        "wire": "clean" if info.stream_clean else "raw", "effective": clean_available,
        "threshold_counts": info.stream_thr, "formula": CLEAN_FORMULA,
    }
    if not info.stream_clean:
        transform["raw_file"] = f"tactile_{info.side}.raw.jsonl"
    return {
        "schema": "syncfield.oglo_tactile_calibration.v1",
        "stream_id": f"tactile_{info.side}", "side": info.side,
        "device": {"serial": info.serial, "hw_rev": info.hw_rev, "fw_rev": info.fw_rev,
                   "schema_ver": 6, "device_id": info.raw.get("device_id")},
        "capture": {"command": "GET ZERO", "input": "existing_recipe_readback"},
        "sample": {"shape": [5, 4, 4], "order": "finger,row,col",
                   "finger_channels": info.channels, "taxel_count": 80,
                   "unit": "adc_count", "rate_hz": info.rate_hz},
        "transform": transform, "zero": calibration,
    }


def derive_clean(source, destination, baseline, threshold):
    with Path(destination).open("x", encoding="utf-8") as output:
        for row in json_rows(source):
            channels = {}
            for key, value in row["channels"].items():
                cleaned = max(0, value - baseline[key])
                channels[key] = cleaned if cleaned >= threshold else 0
            output.write(json.dumps({**row, "channels": channels}, allow_nan=False,
                                    separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())
