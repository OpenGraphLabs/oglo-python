"""Validate the single supported OGLO contract and expose its runtime state.

This SDK intentionally starts at firmware 0.9.9. Older schemas are rejected at
connect time instead of entering a compatibility mode whose semantics differ.

The config is read from `GET CONFIG` over serial or from the config characteristic
over BLE; either way it is the same JSON and this module does not care which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ._wire import NUM_COLS, NUM_FINGERS, ROWS_PER_FINGER, TAXELS

MIN_FIRMWARE = (0, 9, 9)
REQUIRED_SCHEMA = 6
REQUIRED_IMU_LEN = 25


class ConfigError(ValueError):
    """The config is missing something the SDK cannot proceed without."""


@dataclass(frozen=True)
class Info:
    """What a board says about itself. Public: this is `glove.info`."""

    serial: str
    side: str  # "left" | "right"
    hw_rev: str
    fw_rev: str
    rate_hz: int
    channels: List[str]  # finger order ON THE WIRE, left hand is reversed
    #: Firmware initialised the magnetometer at boot. It is not immutable proof that
    #: a part is fitted, nor a runtime freshness/health counter.
    has_mag: bool
    transport: str  # "usb" | "ble"
    pair_id: str

    # Calibration state, as the device reports it.
    zero_valid: bool
    stream_clean: bool
    stream_thr: int

    #: Applied IMU period when this SDK session set and exactly acknowledged it.
    #: Firmware 0.9.9 does not expose a read-only value in CONFIG, so otherwise None.
    imu_period_ms: Optional[int] = None

    #: Frames the device itself discarded. Distinct from host-side loss and never
    #: merged with it: a user chasing missing samples needs to know which side failed.
    device_dropped: int = 0

    #: Everything else the board sent, unparsed, so a new field is readable before
    #: the SDK knows about it.
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_left(self) -> bool:
        return self.side == "left"


@dataclass(frozen=True)
class Capabilities:
    """The dimensions that parameterise arrays and validation."""

    values_per_sample: int
    imu_len: int
    has_mag: bool


def parse_config(cfg: Dict[str, Any], *, transport: str = "usb") -> Tuple[Info, Capabilities]:
    """Turn a config dict into `(Info, Capabilities)`.

    Firmware older than 0.9.9 and anything other than schema 6 are unsupported.
    Required fields do not get fallbacks: a truncated or incompatible config fails
    before streaming begins.
    """
    if not isinstance(cfg, dict) or not cfg:
        raise ConfigError("config is empty; the board did not answer GET CONFIG")
    if cfg.get("device") != "oglo":
        raise ConfigError(f"device={cfg.get('device')!r}, not an OGLO reader")

    schema = _config_int(cfg, "schema_ver")
    if schema != REQUIRED_SCHEMA:
        raise ConfigError(f"schema_ver={schema}; this SDK requires schema {REQUIRED_SCHEMA}")

    fw_value = cfg.get("fw_rev")
    fw = fw_value if isinstance(fw_value, str) else ""
    if not _fw_at_least(fw, MIN_FIRMWARE):
        want = ".".join(str(x) for x in MIN_FIRMWARE)
        raise ConfigError(f"fw_rev={fw or '<missing>'}; this SDK requires firmware {want} or newer")

    vps = _config_int(cfg, "values_per_sample")
    if vps != TAXELS:
        raise ConfigError(
            f"values_per_sample={vps}; this SDK assumes {NUM_FINGERS}x{ROWS_PER_FINGER}x{NUM_COLS}"
        )
    if cfg.get("sample_shape") != [NUM_FINGERS, ROWS_PER_FINGER, NUM_COLS]:
        raise ConfigError(
            f"sample_shape={cfg.get('sample_shape')!r}; expected "
            f"[{NUM_FINGERS}, {ROWS_PER_FINGER}, {NUM_COLS}]"
        )

    imu_len = _config_int(cfg, "imu_len")
    if imu_len != REQUIRED_IMU_LEN:
        raise ConfigError(f"imu_len={imu_len}; schema 6 requires {REQUIRED_IMU_LEN}")

    channels = cfg.get("channels")
    if not isinstance(channels, list) or len(channels) != NUM_FINGERS:
        raise ConfigError(f"channels must list exactly {NUM_FINGERS} fingers")

    side = str(cfg.get("side", "") or "").lower()
    if side not in ("left", "right"):
        raise ConfigError(f"side={side or '<missing>'!r}; expected 'left' or 'right'")
    if any(not isinstance(c, str) or not c for c in channels):
        raise ConfigError("channels entries must be non-empty strings")
    channel_names = list(channels)
    if len(set(channel_names)) != NUM_FINGERS:
        raise ConfigError("channels contains duplicate finger names")
    expected_channels = {"thumb", "index", "middle", "ring", "pinky"}
    if set(channel_names) != expected_channels:
        raise ConfigError(f"channels must contain each finger exactly once, got {channel_names}")

    serial_value = cfg.get("serial")
    serial = serial_value if isinstance(serial_value, str) else ""
    if not serial:
        raise ConfigError("config is missing device serial")
    hw_value = cfg.get("hw_rev")
    hw_rev = hw_value if isinstance(hw_value, str) else ""
    if not hw_rev:
        raise ConfigError("config is missing hw_rev")
    rate_hz = _config_int(cfg, "rate_hz")
    if not 1 <= rate_hz <= 1000:
        raise ConfigError(f"rate_hz={rate_hz}; expected 1..1000")
    samples_per_packet = _config_int(cfg, "samples_per_packet")
    if not 1 <= samples_per_packet <= 3:
        raise ConfigError(f"samples_per_packet={samples_per_packet}; expected 1..3")
    stream_thr = _config_int(cfg, "stream_thr")
    if not 0 <= stream_thr <= 4095:
        raise ConfigError(f"stream_thr={stream_thr}; expected 0..4095")
    zero_valid = _config_bool(cfg, "zero_valid")
    stream_clean = _config_bool(cfg, "stream_clean")
    if stream_clean and not zero_valid:
        raise ConfigError("stream_clean=true is impossible when zero_valid=false")

    if "tag_dropped" in cfg:
        device_dropped = _config_int(cfg, "tag_dropped")
    elif "device_dropped" in cfg:
        device_dropped = _config_int(cfg, "device_dropped")
    else:
        device_dropped = 0
    if device_dropped < 0:
        raise ConfigError("device drop counter cannot be negative")

    info = Info(
        serial=serial,
        side=side,
        hw_rev=hw_rev,
        fw_rev=fw,
        rate_hz=rate_hz,
        channels=channel_names,
        has_mag=_config_bool(cfg, "has_mag"),
        transport=transport,
        pair_id=_config_string(cfg, "pair_id", allow_empty=True),
        zero_valid=zero_valid,
        stream_clean=stream_clean,
        stream_thr=stream_thr,
        imu_period_ms=None,
        device_dropped=device_dropped,
        raw=dict(cfg),
    )
    caps = Capabilities(
        values_per_sample=vps,
        imu_len=imu_len,
        has_mag=info.has_mag,
    )
    return info, caps


def _config_int(cfg: Dict[str, Any], name: str) -> int:
    if name not in cfg:
        raise ConfigError(f"config is missing {name}")
    value = cfg[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be a JSON integer, got {value!r}")
    return value


def _config_bool(cfg: Dict[str, Any], name: str) -> bool:
    if name not in cfg:
        raise ConfigError(f"config is missing {name}")
    value = cfg[name]
    if value is not True and value is not False:
        raise ConfigError(f"{name} must be a JSON boolean, got {value!r}")
    return value


def _config_string(cfg: Dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    if name not in cfg:
        raise ConfigError(f"config is missing {name}")
    value = cfg[name]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ConfigError(f"{name} must be a JSON string")
    return value


def _fw_at_least(fw_rev: str, floor: Tuple[int, int, int]) -> bool:
    """Compare a dotted version numerically.

    String comparison is wrong here and the failure is quiet: `"0.9.10" < "0.9.9"`
    lexicographically, so a future build would be judged older than the floor.
    Non-numeric suffixes (`0.7.3-tzerobtn`) are tolerated by taking the leading digits.
    """
    parts: List[int] = []
    for chunk in (fw_rev or "").split(".")[:3]:
        digits = ""
        for c in chunk:
            if not c.isdigit():
                break
            digits += c
        if not digits:
            return False
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts) >= floor


def taxel_index(finger: int, row: int, col: int) -> int:
    """Wire index for a taxel. Order is `finger, row, col`, a v6 constant.

    It used to be in the config as `sample_order` and was dropped in 0.8.2 to fit the
    BLE size budget, so it lives in the packet-format document and here.
    """
    if not (0 <= finger < NUM_FINGERS and 0 <= row < ROWS_PER_FINGER and 0 <= col < NUM_COLS):
        raise IndexError(f"taxel ({finger}, {row}, {col}) is outside 5x4x4")
    return finger * (ROWS_PER_FINGER * NUM_COLS) + row * NUM_COLS + col


def finger_index(info: Info, name: str) -> int:
    """Wire position of a named finger on THIS board.

    Resolving through `info.channels` rather than a constant is what makes the same
    code correct on both hands.
    """
    try:
        return info.channels.index(name)
    except ValueError:
        raise KeyError(
            f"{name!r} is not one of this board's channels: {info.channels}"
        ) from None
