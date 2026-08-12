"""Release HIL and long-soak evidence for a named physical OGLO pair.

This module deliberately contains no flashing primitive.  Installing a candidate is
an independent, explicit factory/updater action; this runner only observes the two
logical serials named by the operator.  Keeping those boundaries separate prevents a
test command from turning into a fleet mutation because a different board happened
to enumerate first.

The public CLI writes content-addressed JSON, Markdown, raw TAG captures and rolling
JSONL sidecars.  Its low-level helpers accept serial factories so modem-line, CRC and
soak behaviour can be exercised with fake serial ports in the normal unit suite.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from . import _wire as wire
from ._tag_contract import (
    BOOT_ID_BYTES,
    BOOT_ID_HEX_CHARS,
    BOOT_ID_SCOPE,
    TAG2_ACK_PREFIX,
    TAG_V2,
    parse_tag2_ack,
)


PASS, WARN, FAIL = "pass", "warn", "fail"
_VERDICT_ORDER = {PASS: 0, WARN: 1, FAIL: 2}
_LOGICAL_SERIAL = re.compile(r"^OGLO-([LR])-([0-9]{5})$")
_STOP_ALL = b"STREAM BIN OFF\nSTREAM TAXEL OFF\nSTREAM TAG OFF\nSTREAM TAG2 OFF\n"
_STATUS_COUNTERS = ("deadline_misses", "tag_dropped", "tag_short_writes")
RELEASE_SOAK_SECONDS = 72 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class HilConfig:
    left_serial: str
    right_serial: str
    output_root: Path = Path("hil-results")
    expected_firmware: str = "0.9.13"
    tag_seconds: float = 3.0
    reconnect_cycles: int = 20
    reconnect_seconds: float = 0.5
    stall_seconds: float = 30.0
    recovery_seconds: float = 3.0
    short_seconds: float = 10.0
    soak_seconds: Optional[float] = None
    window_seconds: float = 30.0
    confirm_soak: Optional[str] = None
    dry_run: bool = False
    store_soak_raw: bool = True
    min_free_gib: float = 100.0
    tag2_spec: Optional[Path] = None

    @property
    def expected_by_side(self) -> Dict[str, str]:
        return {"left": self.left_serial, "right": self.right_serial}

    @property
    def soak_confirmation(self) -> str:
        return f"{self.left_serial},{self.right_serial}"


@dataclass(frozen=True)
class Target:
    side: str
    logical_serial: str
    port: str
    usb_serial: Optional[str]
    vid: Optional[int]
    pid: Optional[int]
    product: Optional[str]
    manufacturer: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Check:
    name: str
    verdict: str
    detail: str = ""
    measurements: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HilReport:
    run_dir: Path
    config: HilConfig
    started_at: str = field(default_factory=utc_now)
    finished_at: Optional[str] = None
    checks: List[Check] = field(default_factory=list)
    targets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    artifacts: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def add(
        self,
        name: str,
        verdict: str,
        detail: str = "",
        measurements: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if verdict not in _VERDICT_ORDER:
            raise ValueError(f"unknown HIL verdict {verdict!r}")
        self.checks.append(
            Check(name, verdict, detail, _jsonable(dict(measurements or {})))
        )

    @property
    def result(self) -> str:
        if any(item.verdict == FAIL for item in self.checks):
            return FAIL
        if self.config.dry_run:
            return "dry-run"
        if not self.checks:
            return FAIL
        return max((item.verdict for item in self.checks), key=_VERDICT_ORDER.__getitem__)

    def as_dict(self) -> Dict[str, Any]:
        config = asdict(self.config)
        config["output_root"] = str(self.config.output_root)
        return {
            "schema": 1,
            "kind": "oglo-release-hil",
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "flash_performed": False,
            "config": _jsonable(config),
            "targets": _jsonable(self.targets),
            "snapshots": _jsonable(self.snapshots),
            "checks": [_jsonable(asdict(item)) for item in self.checks],
            "artifacts": dict(self.artifacts),
            "error": self.error,
        }


class SerialLike(Protocol):
    timeout: float
    dtr: bool
    rts: bool

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


SerialFactory = Callable[[Target, bool, bool], SerialLike]
CandidateProvider = Callable[[], Sequence[Any]]


def validate_config(config: HilConfig) -> None:
    expected = ((config.left_serial, "L"), (config.right_serial, "R"))
    for value, side in expected:
        match = _LOGICAL_SERIAL.fullmatch(value)
        if match is None or match.group(1) != side:
            label = "left" if side == "L" else "right"
            raise ValueError(
                f"{label} serial must be exact OGLO-{side}-NNNNN form, got {value!r}"
            )
    if config.left_serial == config.right_serial:
        raise ValueError("left and right expected serials must be distinct")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", config.expected_firmware):
        raise ValueError("expected firmware must be an exact numeric x.y.z version")
    for name in (
        "tag_seconds",
        "reconnect_seconds",
        "stall_seconds",
        "recovery_seconds",
        "short_seconds",
        "window_seconds",
    ):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{name} must be greater than zero")
    if isinstance(config.reconnect_cycles, bool) or config.reconnect_cycles < 1:
        raise ValueError("reconnect_cycles must be at least one")
    if config.min_free_gib < 100.0:
        raise ValueError("the long-soak disk reserve cannot be lowered below 100 GiB")
    if config.soak_seconds is not None:
        if (
            isinstance(config.soak_seconds, bool)
            or not isinstance(config.soak_seconds, (int, float))
            or not math.isfinite(float(config.soak_seconds))
            or config.soak_seconds <= 0
        ):
            raise ValueError("soak_seconds must be a finite number greater than zero")
        if config.soak_seconds >= 3600 and config.confirm_soak != config.soak_confirmation:
            raise ValueError(
                "a soak of one hour or longer needs --confirm-soak "
                f"{config.soak_confirmation!r}"
            )


def _release_soak_gate(
    config: HilConfig, soak: Optional[Mapping[str, Any]]
) -> Tuple[str, str, Dict[str, Any]]:
    """Classify only the explicit 72-hour release gate, not a diagnostic soak."""
    release_duration_requested = (
        config.soak_seconds is not None
        and float(config.soak_seconds) >= RELEASE_SOAK_SECONDS
    )
    release_confirmation_present = config.confirm_soak == config.soak_confirmation
    measurements = {
        "requested_seconds": config.soak_seconds,
        "minimum_release_seconds": RELEASE_SOAK_SECONDS,
        "exact_pair_confirmation": release_confirmation_present,
    }
    if (
        release_duration_requested
        and release_confirmation_present
        and isinstance(soak, Mapping)
        and soak.get("ok") is True
    ):
        return (
            PASS,
            "at least 259200 seconds requested with the exact pair confirmation and passed",
            measurements,
        )
    if release_duration_requested:
        return (
            FAIL,
            "the full release-duration soak was requested but did not produce passing evidence",
            measurements,
        )
    if config.soak_seconds is None:
        detail = "not requested; pass --soak 72h with exact confirmation"
    else:
        detail = (
            f"diagnostic soak was {float(config.soak_seconds):g}s; "
            f"release pass requires at least {RELEASE_SOAK_SECONDS}s with exact confirmation"
        )
    return WARN, detail, measurements


def validate_tag2_spec(path: Path) -> Dict[str, Any]:
    """Bind source specification, SDK parser and all canonical vectors exactly."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read canonical TAG2 spec {path}: {exc}") from exc
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"canonical TAG2 spec is not JSON: {exc}") from exc
    frame = spec.get("frame") if isinstance(spec, dict) else None
    negotiation = spec.get("negotiation") if isinstance(spec, dict) else None
    boot_id = spec.get("boot_id") if isinstance(spec, dict) else None
    expected_contract = {
        "version": TAG_V2.version,
        "magic_hex": TAG_V2.magic.hex(),
        "header_format": TAG_V2.header.format,
        "header_len": TAG_V2.header.size,
        "crc_field_format": TAG_V2.crc.format if TAG_V2.crc is not None else None,
        "start_command": TAG_V2.start_command,
        "start_ack_prefix": TAG2_ACK_PREFIX.decode("ascii"),
        "stop_command": TAG_V2.stop_command,
        "boot_id_hex_chars": BOOT_ID_HEX_CHARS,
        "boot_id_bytes": BOOT_ID_BYTES,
        "boot_id_scope": BOOT_ID_SCOPE,
    }
    observed_contract = {
        "version": spec.get("version"),
        "magic_hex": frame.get("magic_hex") if isinstance(frame, dict) else None,
        "header_format": frame.get("header_format") if isinstance(frame, dict) else None,
        "header_len": frame.get("header_len") if isinstance(frame, dict) else None,
        "crc_field_format": (
            frame.get("crc32", {}).get("field_format") if isinstance(frame, dict) else None
        ),
        "start_command": (
            negotiation.get("start_command") if isinstance(negotiation, dict) else None
        ),
        "start_ack_prefix": (
            negotiation.get("start_ack_prefix") if isinstance(negotiation, dict) else None
        ),
        "stop_command": (
            negotiation.get("stop_command") if isinstance(negotiation, dict) else None
        ),
        "boot_id_hex_chars": (
            len("0" * BOOT_ID_HEX_CHARS)
            if isinstance(boot_id, dict)
            and boot_id.get("encoding")
            == f"{BOOT_ID_HEX_CHARS} lowercase hexadecimal characters"
            else None
        ),
        "boot_id_bytes": boot_id.get("bytes") if isinstance(boot_id, dict) else None,
        "boot_id_scope": boot_id.get("scope") if isinstance(boot_id, dict) else None,
    }
    if observed_contract != expected_contract:
        raise ValueError(
            f"TAG2 spec/parser mismatch: expected {expected_contract}, got {observed_contract}"
        )
    crc = frame.get("crc32", {})
    expected_crc = {
        "algorithm": "CRC-32/ISO-HDLC",
        "field_len": 4,
        "coverage": "header_and_payload",
        "polynomial_reflected_hex": "edb88320",
        "init_hex": "ffffffff",
        "xorout_hex": "ffffffff",
        "reference": "zlib.crc32",
    }
    for key, value in expected_crc.items():
        if crc.get(key) != value:
            raise ValueError(f"TAG2 spec CRC {key}={crc.get(key)!r}, expected {value!r}")
    if not isinstance(boot_id, dict) or set(boot_id) != {"encoding", "bytes", "scope"}:
        raise ValueError("TAG2 spec boot_id contract must bind encoding, bytes, and scope exactly")

    vectors = spec.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != 3:
        raise ValueError("TAG2 spec must contain tactile, IMU and magnetometer vectors")
    decoded = []
    for vector in vectors:
        if not isinstance(vector, dict):
            raise ValueError("TAG2 vector must be an object")
        try:
            frame_bytes = bytes.fromhex(vector["frame_hex"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("TAG2 vector frame_hex is invalid") from exc
        packets, remainder, malformed = wire.iter_tagged_v2_diagnostic(frame_bytes)
        if malformed or remainder or len(packets) != 1:
            raise ValueError(
                f"TAG2 vector {vector.get('name')!r} does not decode exactly once"
            )
        packet = packets[0]
        expected = vector.get("expected", {})
        kind = {
            wire.TactilePacket: "tactile",
            wire.ImuPacket: "imu",
            wire.MagPacket: "mag",
        }.get(type(packet))
        if (
            kind != vector.get("name")
            or kind != expected.get("type")
            or packet.seq != expected.get("seq")
            or packet.device_time_us != expected.get("timestamp_us")
            or packet.t_us != expected.get("t_us")
        ):
            raise ValueError(f"TAG2 vector {vector.get('name')!r} decoded values drifted")
        if kind == "tactile" and packet.counts != expected.get("counts"):
            raise ValueError("TAG2 tactile vector counts drifted")
        if kind in ("imu", "mag") and list(packet.raw) != expected.get("raw"):
            raise ValueError(f"TAG2 {kind} vector raw values drifted")
        decoded.append(kind)
    if set(decoded) != {"tactile", "imu", "mag"}:
        raise ValueError(f"TAG2 canonical vectors incomplete: {decoded}")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "vectors": decoded,
        "parser_contract": expected_contract,
    }


def resolve_tag2_spec(config: HilConfig) -> Path:
    candidates = (
        [Path(config.tag2_spec)]
        if config.tag2_spec is not None
        else [
            Path.cwd() / "spec" / "TAG_V2.json",
            Path(__file__).resolve().parents[2] / "spec" / "TAG_V2.json",
            Path(__file__).resolve().parent / "spec" / "TAG_V2.json",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(
        "canonical spec/TAG_V2.json was not found; run from the source checkout or pass --tag2-spec"
    )


def bind_raw_tag2_capture(
    result: Dict[str, Any], contract_evidence: Mapping[str, Any]
) -> Dict[str, Any]:
    """Reparse saved board bytes and bind them to the exact canonical spec hash."""
    if result.get("tag_version") != 2:
        raise ValueError("only TAG2 captures can be bound to the TAG2 spec")
    if result.get("crc_checked") is not True:
        raise ValueError("TAG2 capture was not CRC checked")
    binding = {
        "spec_sha256": contract_evidence["sha256"],
        "parser_contract": contract_evidence["parser_contract"],
    }
    result["tag2_contract_spec_sha256"] = contract_evidence["sha256"]
    result["tag2_contract_binding_sha256"] = sha256_bytes(canonical_json(binding))
    raw_value = result.get("raw_path")
    if raw_value:
        raw_path = Path(str(raw_value))
        data = raw_path.read_bytes()
        if result.get("raw_sha256") != sha256_bytes(data):
            raise ValueError("TAG2 raw SHA-256 changed before contract binding")
        monitor = StreamMonitor(2, started_ns=0)
        monitor.feed(data, observed_ns=1)
        replayed = monitor.cumulative(now_ns=max(1, int(float(result["elapsed_s"]) * 1e9)))
        for field in (
            "counts",
            "missing",
            "duplicates",
            "backwards",
            "timestamp_regressions",
            "malformed_crc_or_structure",
            "trailing_bytes",
        ):
            if replayed[field] != result[field]:
                raise ValueError(
                    f"saved TAG2 bytes do not reproduce live {field}: "
                    f"{replayed[field]!r} != {result[field]!r}"
                )
        result["saved_raw_reparsed_against_contract"] = True
    else:
        result["saved_raw_reparsed_against_contract"] = False
    return result


def _new_run_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for suffix in range(1000):
        path = root / (stem if suffix == 0 else f"{stem}-{suffix}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a unique HIL result directory")


def _write_json(path: Path, value: Any, *, seal: bool = False) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_jsonable(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(payload, encoding="utf-8")
    if seal:
        path.chmod(0o444)
    return sha256_bytes(payload.encode())


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _write_all(serial: SerialLike, payload: bytes) -> None:
    written = serial.write(payload)
    if type(written) is not int or written != len(payload):
        raise OSError(f"short serial write: accepted {written!r} of {len(payload)} bytes")
    serial.flush()


def _set_lines(serial: SerialLike, dtr: bool, rts: bool) -> List[str]:
    """Move through an explicit all-low boundary; never emit a reset recipe helper."""
    applied = []
    serial.dtr = False
    applied.append("dtr=0")
    serial.rts = False
    applied.append("rts=0")
    if dtr:
        serial.dtr = True
        applied.append("dtr=1")
    if rts:
        serial.rts = True
        applied.append("rts=1")
    return applied


def _read_for(serial: SerialLike, seconds: float, *, sleep: Callable[[float], None] = time.sleep) -> bytes:
    deadline = time.monotonic() + seconds
    out = bytearray()
    while time.monotonic() < deadline:
        chunk = serial.read(8192)
        if chunk:
            out += chunk
        else:
            sleep(0.002)
    return bytes(out)


def _find_prefixed_json(data: bytes, prefix: bytes) -> Optional[Dict[str, Any]]:
    found = None
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix):].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            found = value
    return found


def run_line_matrix(
    target: Target,
    *,
    serial_factory: SerialFactory,
    candidate_provider: CandidateProvider,
    settle_seconds: float = 0.12,
    response_seconds: float = 0.20,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Prove DTR gating and RTS independence without any flash/reset command.

    The exact observed sequence is 00 -> 10 -> 00 -> 11 -> 00 -> 01 -> 00.  Each
    transition first drives both lines low, and the runner is allowed only for the
    supported native-USB VID.  Uptime and USB descriptor continuity turn an accidental
    reset/re-enumeration into a failed result instead of a hidden side effect.
    """
    if target.vid != 0x2886:
        raise RuntimeError(
            f"refusing modem-line matrix on non-native/unknown VID {target.vid!r}"
        )
    sequence = [(False, False), (True, False), (False, False), (True, True),
                (False, False), (False, True), (False, False)]
    observations: List[Dict[str, Any]] = []
    serial = serial_factory(target, False, False)
    uptimes: List[int] = []
    postcheck: Optional[Dict[str, Any]] = None
    try:
        for dtr, rts in sequence:
            applied = _set_lines(serial, dtr, rts)
            sleep(settle_seconds)
            serial.reset_input_buffer()
            _write_all(serial, b"GET STATUS\n")
            if dtr:
                _write_all(serial, b"GET CONFIG\n")
            response = _read_for(serial, response_seconds, sleep=sleep)
            status = _find_prefixed_json(response, b"#STATUS ")
            config = _find_prefixed_json(response, b"#CONFIG ")
            visible = list(candidate_provider())
            descriptor_present = any(
                getattr(item, "serial_number", None) == target.usb_serial
                if target.usb_serial
                else getattr(item, "device", None) == target.port
                for item in visible
            )
            if status is not None and isinstance(status.get("uptime_ms"), int):
                uptimes.append(status["uptime_ms"])
            observations.append(
                {
                    "state": f"{int(dtr)}{int(rts)}",
                    "dtr": dtr,
                    "rts": rts,
                    "applied": applied,
                    "response_bytes": len(response),
                    "status": status,
                    "config": config,
                    "descriptor_present": descriptor_present,
                }
            )

        # Required sequence ends at 00. Reassert the normal safe host state once to
        # prove that a reset/re-enumeration caused specifically by the final 01->00
        # edge was not hidden by a fast descriptor return.
        applied = _set_lines(serial, True, False)
        sleep(settle_seconds)
        serial.reset_input_buffer()
        _write_all(serial, b"GET STATUS\nGET CONFIG\n")
        response = _read_for(serial, response_seconds, sleep=sleep)
        status = _find_prefixed_json(response, b"#STATUS ")
        config = _find_prefixed_json(response, b"#CONFIG ")
        visible = list(candidate_provider())
        descriptor_present = any(
            getattr(item, "serial_number", None) == target.usb_serial
            if target.usb_serial
            else getattr(item, "device", None) == target.port
            for item in visible
        )
        postcheck = {
            "state": "10",
            "label": "postcheck_after_final_01_to_00",
            "dtr": True,
            "rts": False,
            "applied": applied,
            "response_bytes": len(response),
            "status": status,
            "config": config,
            "descriptor_present": descriptor_present,
        }
        if status is not None and isinstance(status.get("uptime_ms"), int):
            uptimes.append(status["uptime_ms"])
    finally:
        try:
            _set_lines(serial, False, False)
        finally:
            serial.close()

    failures: List[str] = []
    for item in observations:
        should_reply = item["dtr"]
        if should_reply and item["status"] is None:
            failures.append(f"state {item['state']} did not return STATUS with DTR high")
        if not should_reply and item["response_bytes"]:
            failures.append(f"state {item['state']} emitted {item['response_bytes']} B with DTR low")
        if not item["descriptor_present"]:
            failures.append(f"state {item['state']} lost the USB descriptor")
    if any(after < before for before, after in zip(uptimes, uptimes[1:])):
        failures.append(f"uptime moved backward: {uptimes}")
    high_configs = [item["config"] for item in observations if item["dtr"]]
    if not high_configs or any(item is None for item in high_configs):
        failures.append("DTR-high matrix observations did not return CONFIG")
    if postcheck is None or postcheck["status"] is None or postcheck["config"] is None:
        failures.append("final safe 10 postcheck did not return STATUS and CONFIG")
    elif not postcheck["descriptor_present"]:
        failures.append("final safe 10 postcheck lost the USB descriptor")
    elif high_configs:
        first_boot_id = high_configs[0].get("boot_id") if high_configs[0] else None
        final_boot_id = postcheck["config"].get("boot_id")
        if not first_boot_id or final_boot_id != first_boot_id:
            failures.append(f"boot_id changed across matrix: {first_boot_id}->{final_boot_id}")
    if len(uptimes) >= 2 and uptimes[-1] < uptimes[0]:
        failures.append(f"final uptime {uptimes[-1]} precedes initial {uptimes[0]}")
    return {
        "ok": not failures,
        "sequence": [item["state"] for item in observations],
        "observations": observations,
        "postcheck_10": postcheck,
        "actual_transition_states": [item["state"] for item in observations] + ["10", "00"],
        "uptimes_ms": uptimes,
        "failures": failures,
    }


class StreamMonitor:
    """Incremental TAG parser with cumulative and resettable rolling statistics."""

    _NAMES = {
        wire.TactilePacket: "tactile",
        wire.ImuPacket: "imu",
        wire.MagPacket: "mag",
    }

    def __init__(self, version: int, *, started_ns: Optional[int] = None) -> None:
        if version not in (1, 2):
            raise ValueError("TAG monitor version must be 1 or 2")
        self.version = version
        self.buffer = b""
        self.started_ns = time.monotonic_ns() if started_ns is None else started_ns
        self.window_started_ns = self.started_ns
        self.counts = {name: 0 for name in self._NAMES.values()}
        self.window_counts = dict(self.counts)
        self.malformed = 0
        self.window_malformed = 0
        self.missing = {name: 0 for name in self.counts}
        self.window_missing = dict(self.missing)
        self.duplicates = {name: 0 for name in self.counts}
        self.window_duplicates = dict(self.duplicates)
        self.backwards = {name: 0 for name in self.counts}
        self.window_backwards = dict(self.backwards)
        self.max_gap_us = {name: 0 for name in self.counts}
        self.window_max_gap_us = dict(self.max_gap_us)
        self.first_seq: Dict[str, Optional[int]] = {name: None for name in self.counts}
        self.first_device_us: Dict[str, Optional[int]] = {name: None for name in self.counts}
        self.last_seq: Dict[str, Optional[int]] = {name: None for name in self.counts}
        self.last_device_us: Dict[str, Optional[int]] = {name: None for name in self.counts}
        self.first_observed_ns: Optional[int] = None
        self.timestamp_regressions = {name: 0 for name in self.counts}
        self.window_timestamp_regressions = dict(self.timestamp_regressions)
        self.last_observed_ns: Optional[int] = None
        self.max_host_read_gap_ms = 0.0
        self.window_max_host_read_gap_ms = 0.0
        self.bytes_seen = 0

    def feed(self, chunk: bytes, *, observed_ns: Optional[int] = None) -> int:
        if not chunk:
            return 0
        self.bytes_seen += len(chunk)
        self.buffer += chunk
        packets, self.buffer, malformed = wire.iter_tagged_version_diagnostic(
            self.buffer, self.version
        )
        if packets:
            now_ns = time.monotonic_ns() if observed_ns is None else observed_ns
            if self.first_observed_ns is None:
                self.first_observed_ns = now_ns
            if self.last_observed_ns is not None:
                gap_ms = max(0.0, (now_ns - self.last_observed_ns) / 1e6)
                self.max_host_read_gap_ms = max(self.max_host_read_gap_ms, gap_ms)
                self.window_max_host_read_gap_ms = max(
                    self.window_max_host_read_gap_ms, gap_ms
                )
            self.last_observed_ns = now_ns
        self.malformed += malformed
        self.window_malformed += malformed
        for packet in packets:
            name = self._NAMES.get(type(packet))
            if name is None:
                continue
            self.counts[name] += 1
            self.window_counts[name] += 1
            current_us = packet.device_time_us if packet.device_time_us is not None else packet.t_us
            if self.first_seq[name] is None:
                self.first_seq[name] = packet.seq
                self.first_device_us[name] = int(current_us)
            transition = wire.classify_seq(self.last_seq[name], packet.seq)
            if transition.kind in ("first", "forward", "wrap"):
                self.last_seq[name] = packet.seq
            if transition.missing:
                self.missing[name] += transition.missing
                self.window_missing[name] += transition.missing
            elif transition.kind == "duplicate":
                self.duplicates[name] += 1
                self.window_duplicates[name] += 1
            elif transition.kind == "backward":
                self.backwards[name] += 1
                self.window_backwards[name] += 1

            previous_us = self.last_device_us[name]
            if previous_us is not None:
                if self.version == 1:
                    delta = (int(current_us) - int(previous_us)) & 0xFFFFFFFF
                    if delta >= 0x80000000:
                        self.timestamp_regressions[name] += 1
                        self.window_timestamp_regressions[name] += 1
                        delta = 0
                else:
                    delta = int(current_us) - int(previous_us)
                    if delta < 0:
                        self.timestamp_regressions[name] += 1
                        self.window_timestamp_regressions[name] += 1
                        delta = 0
                self.max_gap_us[name] = max(self.max_gap_us[name], delta)
                self.window_max_gap_us[name] = max(self.window_max_gap_us[name], delta)
            self.last_device_us[name] = int(current_us)
        return len(packets)

    def _stats(self, *, elapsed_s: float, window: bool) -> Dict[str, Any]:
        counts = self.window_counts if window else self.counts
        return {
            "elapsed_s": elapsed_s,
            "counts": dict(counts),
            "rates_hz": {
                name: (count / elapsed_s if elapsed_s > 0 else 0.0)
                for name, count in counts.items()
            },
            "max_gap_us": dict(self.window_max_gap_us if window else self.max_gap_us),
            "missing": dict(self.window_missing if window else self.missing),
            "duplicates": dict(self.window_duplicates if window else self.duplicates),
            "backwards": dict(self.window_backwards if window else self.backwards),
            "timestamp_regressions": dict(
                self.window_timestamp_regressions if window else self.timestamp_regressions
            ),
            "malformed_crc_or_structure": self.window_malformed if window else self.malformed,
            "max_host_read_gap_ms": (
                self.window_max_host_read_gap_ms if window else self.max_host_read_gap_ms
            ),
            "bytes_seen": self.bytes_seen,
            "trailing_bytes": len(self.buffer),
            "crc_checked": self.version == 2,
        }

    def cumulative(self, *, now_ns: Optional[int] = None) -> Dict[str, Any]:
        now = time.monotonic_ns() if now_ns is None else now_ns
        return self._stats(elapsed_s=max(0.0, (now - self.started_ns) / 1e9), window=False)

    def roll_window(self, *, now_ns: Optional[int] = None) -> Dict[str, Any]:
        now = time.monotonic_ns() if now_ns is None else now_ns
        elapsed = max(0.0, (now - self.window_started_ns) / 1e9)
        out = self._stats(elapsed_s=elapsed, window=True)
        self.window_started_ns = now
        self.window_counts = {name: 0 for name in self.window_counts}
        self.window_missing = {name: 0 for name in self.window_missing}
        self.window_duplicates = {name: 0 for name in self.window_duplicates}
        self.window_backwards = {name: 0 for name in self.window_backwards}
        self.window_timestamp_regressions = {
            name: 0 for name in self.window_timestamp_regressions
        }
        self.window_max_gap_us = {name: 0 for name in self.window_max_gap_us}
        self.window_malformed = 0
        self.window_max_host_read_gap_ms = 0.0
        return out


def _stream_summary_ok(
    summary: Mapping[str, Any],
    *,
    has_mag: bool,
    expected_tactile_hz: Optional[float] = None,
) -> Tuple[bool, List[str]]:
    failures = []
    required = ("tactile", "imu", "mag") if has_mag else ("tactile", "imu")
    for name in required:
        if int(summary["counts"].get(name, 0)) <= 0:
            failures.append(f"no {name} frames")
    for field in ("missing", "duplicates", "backwards", "timestamp_regressions"):
        bad = {key: value for key, value in summary[field].items() if value}
        if bad:
            failures.append(f"{field}={bad}")
    if summary["malformed_crc_or_structure"]:
        failures.append(
            f"malformed_crc_or_structure={summary['malformed_crc_or_structure']}"
        )
    elapsed = float(summary.get("elapsed_s", 0.0))
    if elapsed >= 1.0 and expected_tactile_hz:
        expected_rates = {
            "tactile": float(expected_tactile_hz),
            "imu": 500.0,
            "mag": 125.0,
        }
        for name in required:
            observed = float(summary["rates_hz"].get(name, 0.0))
            expected = expected_rates[name]
            if not 0.80 * expected <= observed <= 1.20 * expected:
                failures.append(
                    f"{name} rate={observed:.1f} Hz outside {0.8 * expected:.1f}..{1.2 * expected:.1f}"
                )
            max_gap = int(summary["max_gap_us"].get(name, 0))
            if max_gap > int(5_000_000 / expected):
                failures.append(
                    f"{name} device-time max gap={max_gap} us exceeds five periods"
                )
    return not failures, failures


def _read_ack(serial: SerialLike, timeout: float) -> Tuple[str, bytes]:
    deadline = time.monotonic() + timeout
    pending = bytearray()
    while time.monotonic() < deadline:
        chunk = serial.read(8192)
        if chunk:
            pending += chunk
            newline = pending.find(b"\n")
            if newline >= 0:
                line = bytes(pending[:newline]).removesuffix(b"\r")
                return parse_tag2_ack(line), bytes(pending[newline + 1:])
            if len(pending) > 96:
                raise RuntimeError(f"TAG2 ACK exceeded the exact line boundary: {pending[:96]!r}")
        else:
            time.sleep(0.002)
    raise TimeoutError(f"no exact TAG2 ACK within {timeout:g}s")


def _quiet(serial: SerialLike, *, settle: float = 0.20) -> None:
    _write_all(serial, _STOP_ALL)
    time.sleep(settle)
    serial.reset_input_buffer()


def _query_json(
    serial: SerialLike,
    command: str,
    prefix: bytes,
    *,
    timeout: float = 3.0,
) -> Dict[str, Any]:
    serial.reset_input_buffer()
    _write_all(serial, command.rstrip().encode() + b"\n")
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        chunk = serial.read(8192)
        if chunk:
            data += chunk
            value = _find_prefixed_json(bytes(data), prefix)
            if value is not None:
                return value
            data[:] = data[-65536:]
        else:
            time.sleep(0.002)
    raise TimeoutError(f"no {prefix.decode(errors='replace').strip()} reply to {command}")


def capture_tag_stream(
    target: Target,
    *,
    version: int,
    seconds: float,
    serial_factory: SerialFactory,
    raw_path: Optional[Path] = None,
    window_seconds: Optional[float] = None,
    window_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    stall_before_read: float = 0.0,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Capture one negotiated wire version from one exact target.

    ``stall_before_read`` intentionally leaves the CDC host unread, then discards the
    host/kernel backlog before measuring fresh post-stall packets.  This distinguishes
    recovery from merely draining old bytes.
    """
    if version not in (1, 2):
        raise ValueError("version must be 1 or 2")
    if stall_before_read and version != 2:
        raise ValueError("stalled-reader recovery requires TAG2 u64 device timestamps")
    serial = serial_factory(target, True, False)
    raw_handle = None
    started_ns = time.monotonic_ns()
    try:
        _quiet(serial)
        config = _query_json(serial, "GET CONFIG", b"#CONFIG ")
        if config.get("serial") != target.logical_serial or config.get("side") != target.side:
            raise RuntimeError(
                f"{target.port} identity changed: {config.get('serial')}/{config.get('side')}"
            )
        if version == 2 and int(config.get("tag_ver_max", 1)) < 2:
            raise RuntimeError(f"{target.logical_serial} does not advertise TAG2")
        pre_stall_status = (
            _query_json(serial, "GET STATUS", b"#STATUS ") if stall_before_read else None
        )
        serial.reset_input_buffer()
        command = b"STREAM TAG2 ON\n" if version == 2 else b"STREAM TAG ON\n"
        _write_all(serial, command)
        initial = b""
        ack_boot_id = None
        if version == 2:
            ack_boot_id, initial = _read_ack(serial, timeout=2.0)
            if ack_boot_id != config.get("boot_id"):
                raise RuntimeError(
                    f"TAG2 ACK boot_id={ack_boot_id} does not match CONFIG {config.get('boot_id')}"
                )
        stall_evidence: Optional[Dict[str, Any]] = None
        post_reset_ns: Optional[int] = None
        if stall_before_read:
            pre_monitor = StreamMonitor(2)
            if initial:
                pre_monitor.feed(initial)
            boundary_deadline = time.monotonic() + 2.0
            while pre_monitor.last_device_us["tactile"] is None:
                if time.monotonic() >= boundary_deadline:
                    raise TimeoutError("no valid tactile TAG2 frame before the host-read stall")
                chunk = serial.read(8192)
                if chunk:
                    pre_monitor.feed(chunk)
                else:
                    time.sleep(0.001)
            if pre_monitor.malformed:
                raise RuntimeError(
                    "malformed TAG2 bytes before the stalled-reader boundary: "
                    f"{pre_monitor.malformed}"
                )
            stall_started_ns = time.monotonic_ns()
            time.sleep(stall_before_read)
            serial.reset_input_buffer()
            post_reset_ns = time.monotonic_ns()
            actual_stall_s = max(0.0, (post_reset_ns - stall_started_ns) / 1e9)
            stall_evidence = {
                "pre_stall_boundary": {
                    "seq": dict(pre_monitor.last_seq),
                    "device_time_us": dict(pre_monitor.last_device_us),
                    "boot_id": ack_boot_id,
                    "status": pre_stall_status,
                    "observed_monotonic_ns": pre_monitor.last_observed_ns,
                },
                "requested_unread_stall_s": stall_before_read,
                "actual_unread_stall_s": actual_stall_s,
                "host_input_reset_after_stall": True,
            }
            initial = b""

        freshness_probe_buffer = b""
        first_fresh_tactile: Optional[Dict[str, int]] = None
        first_fresh_observed_ns: Optional[int] = None
        stale_tactile_frames = 0
        freshness_tolerance_s: Optional[float] = None
        min_advance_us: Optional[int] = None
        if stall_evidence is not None:
            rate_hz = float(config.get("rate_hz", 0) or 0)
            # Five tactile periods cover an in-flight USB transaction; the 0.5%
            # term covers ordinary independent host/device clock drift on long
            # diagnostic stalls.
            freshness_tolerance_s = max(
                0.050,
                (5.0 / rate_hz) if rate_hz > 0 else 0.0,
                0.005 * float(stall_evidence["actual_unread_stall_s"]),
            )
            min_advance_us = max(
                0,
                int(
                    (
                        float(stall_evidence["actual_unread_stall_s"])
                        - freshness_tolerance_s
                    )
                    * 1_000_000
                ),
            )

        capture_started_ns = time.monotonic_ns()
        monitor = StreamMonitor(version, started_ns=capture_started_ns)
        if raw_path is not None:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_handle = raw_path.open("wb")
        if initial:
            if raw_handle is not None:
                raw_handle.write(initial)
            monitor.feed(initial)

        deadline = time.monotonic() + seconds
        next_window = (
            time.monotonic() + window_seconds if window_seconds is not None else None
        )
        while time.monotonic() < deadline and not (
            cancel_event is not None and cancel_event.is_set()
        ):
            chunk = serial.read(8192)
            if chunk:
                if raw_handle is not None:
                    raw_handle.write(chunk)
                observed_ns = time.monotonic_ns()
                monitor.feed(chunk, observed_ns=observed_ns)
                if stall_evidence is not None and first_fresh_tactile is None:
                    freshness_probe_buffer += chunk
                    packets, freshness_probe_buffer, _ = wire.iter_tagged_v2_diagnostic(
                        freshness_probe_buffer
                    )
                    pre_device_us = stall_evidence["pre_stall_boundary"][
                        "device_time_us"
                    ]["tactile"]
                    assert min_advance_us is not None and pre_device_us is not None
                    for packet in packets:
                        if not isinstance(packet, wire.TactilePacket):
                            continue
                        device_us = packet.device_time_us
                        if device_us is None:
                            continue
                        advance_us = int(device_us) - int(pre_device_us)
                        if advance_us < min_advance_us:
                            stale_tactile_frames += 1
                            continue
                        first_fresh_tactile = {
                            "seq": int(packet.seq),
                            "device_time_us": int(device_us),
                        }
                        first_fresh_observed_ns = observed_ns
                        break
                if cancel_event is not None and (
                    monitor.malformed
                    or any(monitor.missing.values())
                    or any(monitor.duplicates.values())
                    or any(monitor.backwards.values())
                    or any(monitor.timestamp_regressions.values())
                ):
                    cancel_event.set()
            else:
                time.sleep(0.001)
            now = time.monotonic()
            if next_window is not None and now >= next_window:
                window = monitor.roll_window()
                if raw_handle is not None:
                    raw_handle.flush()
                    os.fsync(raw_handle.fileno())
                if window_callback is not None:
                    window_callback(window)
                if cancel_event is not None:
                    window_ok, _ = _stream_summary_ok(
                        window,
                        has_mag=bool(config.get("has_mag")),
                        expected_tactile_hz=float(config.get("rate_hz", 0) or 0),
                    )
                    if not window_ok:
                        cancel_event.set()
                next_window += float(window_seconds)
        if window_seconds is not None:
            window = monitor.roll_window()
            if any(window["counts"].values()) and window_callback is not None:
                window_callback(window)

        _write_all(serial, b"STREAM TAG2 OFF\n" if version == 2 else b"STREAM TAG OFF\n")
        if raw_handle is not None:
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
            raw_handle.close()
            raw_handle = None
        summary = monitor.cumulative()
        ok, failures = _stream_summary_ok(
            summary,
            has_mag=bool(config.get("has_mag")),
            expected_tactile_hz=float(config.get("rate_hz", 0) or 0),
        )
        if stall_evidence is not None:
            assert post_reset_ns is not None
            post_config = _query_json(serial, "GET CONFIG", b"#CONFIG ")
            post_status = _query_json(serial, "GET STATUS", b"#STATUS ")
            first_latency_s = (
                max(0.0, (first_fresh_observed_ns - post_reset_ns) / 1e9)
                if first_fresh_observed_ns is not None
                else None
            )
            pre_seq = stall_evidence["pre_stall_boundary"]["seq"]["tactile"]
            post_seq = first_fresh_tactile["seq"] if first_fresh_tactile else None
            pre_device_us = stall_evidence["pre_stall_boundary"]["device_time_us"][
                "tactile"
            ]
            post_device_us = (
                first_fresh_tactile["device_time_us"] if first_fresh_tactile else None
            )
            device_advance_us = (
                int(post_device_us) - int(pre_device_us)
                if post_device_us is not None and pre_device_us is not None
                else None
            )
            assert freshness_tolerance_s is not None and min_advance_us is not None
            max_advance_us = int(
                (
                    float(stall_evidence["actual_unread_stall_s"])
                    + (first_latency_s or 0.0)
                    + freshness_tolerance_s
                )
                * 1_000_000
            )
            seq_transition = (
                wire.classify_seq(int(pre_seq), int(post_seq)).kind
                if pre_seq is not None and post_seq is not None
                else "missing"
            )
            stale_backlog = stale_tactile_frames > 0
            excessive_advance = (
                device_advance_us is not None and device_advance_us > max_advance_us
            )
            post_boot_id = post_config.get("boot_id")
            same_boot = post_boot_id == ack_boot_id == config.get("boot_id")
            status_uptime_ok = (
                isinstance(pre_stall_status, dict)
                and isinstance(pre_stall_status.get("uptime_ms"), int)
                and isinstance(post_status.get("uptime_ms"), int)
                and post_status["uptime_ms"] >= pre_stall_status["uptime_ms"]
            )
            stall_evidence.update(
                {
                    "post_stall_first_valid": {
                        "seq": dict(monitor.first_seq),
                        "device_time_us": dict(monitor.first_device_us),
                    },
                    "post_stall_first_fresh_tactile": first_fresh_tactile,
                    "first_fresh_frame_latency_after_input_reset_s": first_latency_s,
                    "post_stall_config_boot_id": post_boot_id,
                    "post_stall_status": post_status,
                    "boot_identity_unchanged": same_boot,
                    "status_uptime_non_decreasing": status_uptime_ok,
                    "tactile_seq_transition": seq_transition,
                    "tactile_device_time_advance_us": device_advance_us,
                    "expected_device_time_advance_min_us": min_advance_us,
                    "expected_device_time_advance_max_us": max_advance_us,
                    "freshness_tolerance_s": freshness_tolerance_s,
                    "stale_device_backlog_detected": stale_backlog,
                    "stale_tactile_frames_before_fresh": stale_tactile_frames,
                    "excessive_device_time_advance_detected": excessive_advance,
                    "freshness_basis": (
                        "host input was reset after the measured unread interval; the first "
                        "valid post-reset tactile TAG2 u64 timestamp is compared with the "
                        "last valid pre-stall tactile timestamp"
                    ),
                }
            )
            if first_latency_s is None:
                failures.append("no fresh valid TAG2 frame arrived after the host input reset")
            if seq_transition not in ("forward", "wrap"):
                failures.append(f"post-stall tactile sequence transition is {seq_transition}")
            if not same_boot:
                failures.append(
                    "boot_id changed across stalled-reader recovery: "
                    f"{config.get('boot_id')}->{post_boot_id}"
                )
            if not status_uptime_ok:
                failures.append("device STATUS uptime reset or was unavailable across the stall")
            if excessive_advance:
                failures.append(
                    "post-stall tactile u64 advancement exceeds the measured stall window: "
                    f"advance={device_advance_us}, maximum={max_advance_us} us"
                )
            ok = not failures
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        if cancelled:
            ok = False
            failures.append("peer capture failed or the soak was cancelled")
        return {
            "ok": ok,
            "failures": failures,
            "serial": target.logical_serial,
            "side": target.side,
            "port": target.port,
            "tag_version": version,
            "config_boot_id": config.get("boot_id"),
            "ack_boot_id": ack_boot_id,
            "stall_before_read_s": stall_before_read,
            "stalled_reader_recovery": stall_evidence,
            "cancelled": cancelled,
            "capture_started_at": datetime.fromtimestamp(
                time.time() - ((time.monotonic_ns() - capture_started_ns) / 1e9),
                timezone.utc,
            ).isoformat(),
            **summary,
            "raw_path": str(raw_path) if raw_path is not None else None,
            "raw_sha256": sha256_file(raw_path) if raw_path is not None else None,
            "total_operation_s": (time.monotonic_ns() - started_ns) / 1e9,
        }
    finally:
        if raw_handle is not None:
            raw_handle.close()
        try:
            _write_all(serial, b"STREAM TAG OFF\nSTREAM TAG2 OFF\n")
        except BaseException:
            pass
        serial.close()


class ActualBackend:
    """Physical implementation.  All methods are observation-only."""

    def __init__(
        self,
        *,
        serial_factory: Optional[SerialFactory] = None,
        candidate_provider: Optional[CandidateProvider] = None,
    ) -> None:
        from ._usb import list_candidates

        self.serial_factory = serial_factory or self._open_serial
        self.candidate_provider = candidate_provider or list_candidates

    @staticmethod
    def _open_serial(target: Target, dtr: bool, rts: bool) -> SerialLike:
        import serial as pyserial

        handle = pyserial.Serial()
        handle.port = target.port
        handle.baudrate = 115200
        handle.timeout = 0.05
        if hasattr(handle, "exclusive"):
            handle.exclusive = True
        # Set both line states while the pyserial object is still closed. POSIX open
        # applies these stored values atomically; setting them only after open would
        # transiently expose pyserial defaults and invalidate the tested transition.
        handle.dtr = dtr
        handle.rts = rts
        handle.open()
        time.sleep(0.8)
        return handle

    def discover(self, config: HilConfig) -> Tuple[Dict[str, Target], List[Dict[str, Any]]]:
        from . import connect

        expected = config.expected_by_side
        found: Dict[str, Target] = {}
        seen: List[Dict[str, Any]] = []
        candidates = list(self.candidate_provider())
        for candidate in candidates:
            try:
                with connect(port=candidate.device, timeout=6.0) as glove:
                    info = glove.info
                    row = {
                        "port": candidate.device,
                        "usb_serial": candidate.serial_number,
                        "serial": info.serial,
                        "side": info.side,
                        "firmware": info.fw_rev,
                    }
                    seen.append(row)
                    if info.side not in expected or info.serial != expected[info.side]:
                        continue
                    if info.side in found:
                        raise RuntimeError(
                            f"duplicate {info.side} target {info.serial} on two USB ports"
                        )
                    found[info.side] = Target(
                        side=info.side,
                        logical_serial=info.serial,
                        port=candidate.device,
                        usb_serial=candidate.serial_number,
                        vid=candidate.vid,
                        pid=candidate.pid,
                        product=candidate.product,
                        manufacturer=candidate.manufacturer,
                    )
            except Exception as exc:
                seen.append(
                    {
                        "port": getattr(candidate, "device", None),
                        "usb_serial": getattr(candidate, "serial_number", None),
                        "probe_error": f"{type(exc).__name__}: {exc}",
                    }
                )
        missing = [side for side in ("left", "right") if side not in found]
        if missing:
            raise RuntimeError(
                f"exact expected pair not found; missing {missing}, observed {seen}"
            )
        return found, seen

    def snapshot(self, target: Target) -> Dict[str, Any]:
        from . import connect

        with connect(port=target.port, timeout=8.0) as glove:
            if glove.info.serial != target.logical_serial or glove.info.side != target.side:
                raise RuntimeError("logical identity changed before snapshot")
            status = glove.status()
            zero_line = glove.send("GET ZERO", expect="#TZERO ", timeout=5.0)
            zero = json.loads(zero_line.removeprefix("#TZERO "))
            fw_line = glove.send("GET FWINFO", expect="#", timeout=5.0)
            fwinfo = None
            fwinfo_error = None
            if fw_line.startswith("#FWINFO "):
                fwinfo = json.loads(fw_line.removeprefix("#FWINFO "))
            else:
                fwinfo_error = fw_line
            config = dict(glove.info.raw)
        return {
            "observed_at": utc_now(),
            "target": target.as_dict(),
            "config": config,
            "status": asdict(status),
            "zero": zero,
            "calibration_sha256": sha256_bytes(canonical_json(zero)),
            "fwinfo": fwinfo,
            "fwinfo_error": fwinfo_error,
            "running_image_sha256": (
                fwinfo.get("running_image_sha256") if isinstance(fwinfo, dict) else None
            ),
        }

    def line_matrix(self, target: Target) -> Dict[str, Any]:
        return run_line_matrix(
            target,
            serial_factory=self.serial_factory,
            candidate_provider=self.candidate_provider,
        )

    def capture(
        self,
        target: Target,
        *,
        version: int,
        seconds: float,
        raw_path: Optional[Path],
        stall_before_read: float = 0.0,
    ) -> Dict[str, Any]:
        return capture_tag_stream(
            target,
            version=version,
            seconds=seconds,
            serial_factory=self.serial_factory,
            raw_path=raw_path,
            stall_before_read=stall_before_read,
        )

    def dual_capture(
        self,
        targets: Mapping[str, Target],
        *,
        seconds: float,
        output_dir: Path,
        label: str,
    ) -> Dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(
                    self.capture,
                    target,
                    version=2,
                    seconds=seconds,
                    raw_path=output_dir / f"{label}-{side}-tag2.bin",
                ): side
                for side, target in targets.items()
            }
            for future in as_completed(futures):
                side = futures[future]
                results[side] = future.result()
        return {
            "ok": all(value["ok"] for value in results.values()),
            "devices": results,
        }

    def reconnect(
        self,
        target: Target,
        *,
        cycles: int,
        seconds: float,
    ) -> Dict[str, Any]:
        observations = []
        previous_uptime = None
        previous_boot_id = None
        failures = []
        for index in range(cycles):
            capture = self.capture(
                target, version=2, seconds=seconds, raw_path=None
            )
            snapshot = self.snapshot(target)
            uptime = snapshot["status"]["uptime_ms"]
            boot_id = snapshot["config"].get("boot_id")
            if previous_uptime is not None and uptime < previous_uptime:
                failures.append(f"cycle {index + 1}: uptime reset {previous_uptime}->{uptime}")
            if previous_boot_id is not None and boot_id != previous_boot_id:
                failures.append(f"cycle {index + 1}: boot_id changed")
            if not capture["ok"]:
                failures.append(f"cycle {index + 1}: {capture['failures']}")
            observations.append(
                {
                    "cycle": index + 1,
                    "uptime_ms": uptime,
                    "boot_id": boot_id,
                    "stream": capture,
                }
            )
            previous_uptime = uptime
            previous_boot_id = boot_id
        return {"ok": not failures, "failures": failures, "cycles": observations}

    def soak(
        self,
        targets: Mapping[str, Target],
        *,
        seconds: float,
        window_seconds: float,
        output_dir: Path,
        store_raw: bool,
        min_free_gib: float = 100.0,
    ) -> Dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(output_dir)
        raw_estimate = int(seconds * 2 * 128 * 1024) if store_raw else 0
        sidecar_estimate = int(max(1.0, seconds / window_seconds) * 2 * 4096)
        estimated_bytes = raw_estimate + sidecar_estimate
        reserve_bytes = int(min_free_gib * 1024**3)
        if disk.free < reserve_bytes + estimated_bytes:
            raise RuntimeError(
                f"refusing long soak: free disk would cross the {min_free_gib:g} GiB reserve; "
                f"free={disk.free}, estimated={estimated_bytes}, reserve={reserve_bytes}"
            )
        sidecar = output_dir / "soak-windows.jsonl"
        sidecar.touch(exist_ok=False)
        lock = threading.Lock()
        cancel_event = threading.Event()

        def append_window(side: str, target: Target, window: Dict[str, Any]) -> None:
            row = {
                "observed_at": utc_now(),
                "side": side,
                "serial": target.logical_serial,
                **window,
            }
            payload = json.dumps(row, sort_keys=True) + "\n"
            with lock:
                with sidecar.open("a", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())

        def worker(side: str, target: Target) -> Dict[str, Any]:
            try:
                return capture_tag_stream(
                    target,
                    version=2,
                    seconds=seconds,
                    serial_factory=self.serial_factory,
                    raw_path=(output_dir / f"soak-{side}-tag2.bin") if store_raw else None,
                    window_seconds=window_seconds,
                    window_callback=lambda value: append_window(side, target, value),
                    cancel_event=cancel_event,
                )
            except BaseException:
                cancel_event.set()
                raise

        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(worker, side, target): side for side, target in targets.items()
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return {
            "ok": all(value["ok"] for value in results.values()),
            "devices": results,
            "window_sidecar": str(sidecar),
            "window_sidecar_sha256": sha256_file(sidecar),
            "disk_free_before_bytes": disk.free,
            "estimated_artifact_bytes": estimated_bytes,
            "required_reserve_bytes": reserve_bytes,
        }


def _snapshot_ok(snapshot: Mapping[str, Any], config: HilConfig, side: str) -> Tuple[bool, List[str]]:
    failures = []
    raw = snapshot["config"]
    status = snapshot["status"]
    fwinfo = snapshot.get("fwinfo")
    if raw.get("serial") != config.expected_by_side[side]:
        failures.append(f"serial={raw.get('serial')!r}")
    if raw.get("side") != side:
        failures.append(f"side={raw.get('side')!r}")
    if raw.get("fw_rev") != config.expected_firmware:
        failures.append(
            f"firmware={raw.get('fw_rev')!r}, expected {config.expected_firmware!r}; flash separately"
        )
    if int(raw.get("tag_ver_max", 1)) < 2:
        failures.append("TAG2 capability is absent")
    if not raw.get("boot_id"):
        failures.append("boot_id is absent")
    if not raw.get("zero_valid"):
        failures.append("calibration is not valid")
    if not status.get("imu_ok") or not status.get("sensor_ok"):
        failures.append("IMU/sensor status is not healthy")
    if raw.get("has_mag") and not status.get("mag_ok"):
        failures.append("magnetometer status is not healthy")
    if status.get("error_flags") != 0:
        failures.append(f"error_flags={status.get('error_flags')}")
    if not isinstance(fwinfo, dict):
        failures.append(f"GET FWINFO unavailable: {snapshot.get('fwinfo_error')!r}")
    elif fwinfo.get("running_image_sha256") is None:
        failures.append("running image SHA-256 is absent")
    return not failures, failures


def _compare_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> Tuple[bool, List[str]]:
    failures = []
    for field in ("serial", "side", "device_id"):
        if before["config"].get(field) != after["config"].get(field):
            failures.append(f"CONFIG {field} changed")
    if before.get("calibration_sha256") != after.get("calibration_sha256"):
        failures.append("calibration fingerprint changed")
    if before.get("running_image_sha256") != after.get("running_image_sha256"):
        failures.append("running firmware SHA-256 changed during observation-only HIL")
    if after["status"].get("uptime_ms", 0) < before["status"].get("uptime_ms", 0):
        failures.append("uptime reset")
    if after["config"].get("boot_id") != before["config"].get("boot_id"):
        failures.append("boot_id changed")
    if after["status"].get("error_flags") != 0:
        failures.append(f"final error_flags={after['status'].get('error_flags')}")
    for counter in _STATUS_COUNTERS:
        delta = int(after["status"].get(counter, 0)) - int(before["status"].get(counter, 0))
        if delta != 0:
            failures.append(f"{counter} delta={delta}")
    return not failures, failures


def capture_kernel_usb_logs(started_at: str, finished_at: str, output: Path) -> Dict[str, Any]:
    """Best-effort bounded host USB log evidence; never needs administrator rights."""
    command: Optional[List[str]] = None
    if sys.platform == "darwin":
        command = [
            "/usr/bin/log", "show", "--style", "compact", "--start", started_at,
            "--end", finished_at, "--predicate",
            'process == "kernel" AND (eventMessage CONTAINS[c] "USB" OR eventMessage CONTAINS[c] "CDC")',
        ]
    elif sys.platform.startswith("linux"):
        command = ["journalctl", "-k", "--since", started_at, "--until", finished_at,
                   "--no-pager", "--output=short-iso"]
    if command is None:
        output.write_text("kernel USB log capture unsupported on this platform\n", encoding="utf-8")
        return {"available": False, "reason": "unsupported platform", "path": str(output)}
    try:
        completed = subprocess.run(command, capture_output=True, timeout=45)
        data = completed.stdout + completed.stderr
        truncated = len(data) > 4 * 1024 * 1024
        if truncated:
            data = data[-4 * 1024 * 1024:]
        output.write_bytes(data)
        return {
            "available": completed.returncode == 0,
            "returncode": completed.returncode,
            "truncated_to_last_4mib": truncated,
            "bytes": len(data),
            "path": str(output),
            "sha256": sha256_bytes(data),
        }
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}\n"
        output.write_text(text, encoding="utf-8")
        return {"available": False, "reason": text.strip(), "path": str(output)}


def _attempt(report: HilReport, name: str, fn: Callable[[], Any]) -> Any:
    try:
        value = fn()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        report.add(name, FAIL, f"{type(exc).__name__}: {exc}")
        return None
    ok = bool(value.get("ok", True)) if isinstance(value, Mapping) else True
    detail = "" if ok else "; ".join(str(item) for item in value.get("failures", []))
    report.add(name, PASS if ok else FAIL, detail, value if isinstance(value, Mapping) else {})
    return value


def _finalize(report: HilReport) -> HilReport:
    report.finished_at = utc_now()
    report_path = report.run_dir / "hil-report.json"
    markdown_path = report.run_dir / "hil-report.md"
    manifest_path = report.run_dir / "manifest.sha256"
    report.artifacts.update(
        {
            "report_json": str(report_path),
            "report_markdown": str(markdown_path),
            "sha256_manifest": str(manifest_path),
        }
    )
    _write_json(report_path, report.as_dict())
    markdown_path.write_text(_markdown(report.as_dict()), encoding="utf-8")
    rows = []
    for path in sorted(item for item in report.run_dir.rglob("*") if item.is_file()):
        if path == manifest_path:
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(report.run_dir)}")
    manifest_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    for path in (report_path, markdown_path, manifest_path):
        path.chmod(0o444)
    return report


def run_hil(config: HilConfig, *, backend: Optional[Any] = None) -> HilReport:
    """Run the observation-only release gate and seal evidence before interrupts escape."""
    validate_config(config)
    run_dir = _new_run_dir(Path(config.output_root))
    report = HilReport(run_dir=run_dir, config=config)
    try:
        return _run_hil_steps(config, report=report, backend=backend)
    except (KeyboardInterrupt, SystemExit) as exc:
        detail = f"{type(exc).__name__}: HIL execution did not complete"
        if isinstance(exc, SystemExit):
            detail += f" (code={exc.code!r})"
        report.add("HIL execution interrupted", FAIL, detail)
        report.error = detail
        finalized = False
        finalize_error: Optional[BaseException] = None
        try:
            _finalize(report)
            finalized = True
        except BaseException as seal_exc:
            # Preserve the operator's original interrupt. The attached error makes
            # the rare failure-to-seal state observable to direct API callers.
            finalize_error = seal_exc
        for name, value in (
            ("hil_report_dir", report.run_dir),
            ("hil_report_path", report.run_dir / "hil-report.json"),
            ("hil_report_finalized", finalized),
            ("hil_report_finalize_error", finalize_error),
        ):
            try:
                setattr(exc, name, value)
            except Exception:
                pass
        raise


def _run_hil_steps(
    config: HilConfig, *, report: HilReport, backend: Optional[Any] = None
) -> HilReport:
    """Execute HIL steps using the wrapper-owned report and evidence directory."""
    run_dir = report.run_dir
    try:
        spec_path = resolve_tag2_spec(config)
        contract_evidence = validate_tag2_spec(spec_path)
        frozen_spec = run_dir / "contracts" / "TAG_V2.json"
        frozen_spec.parent.mkdir(parents=True, exist_ok=True)
        frozen_spec.write_bytes(spec_path.read_bytes())
        frozen_spec.chmod(0o444)
        report.artifacts["tag2_contract"] = (
            f"{frozen_spec} sha256={contract_evidence['sha256']}"
        )
        report.add(
            "canonical TAG2 spec/parser/vectors",
            PASS,
            measurements=contract_evidence,
        )
    except Exception as exc:
        report.add(
            "canonical TAG2 spec/parser/vectors",
            FAIL,
            f"{type(exc).__name__}: {exc}",
        )
        report.error = "cannot test hardware against an unbound wire contract"
        return _finalize(report)
    if config.dry_run:
        report.add(
            "dry-run plan",
            PASS,
            "no serial port opened and no firmware/calibration write exists in this runner",
            {
                "expected_pair": config.expected_by_side,
                "steps": [
                    "exact logical serial discovery",
                    "sealed before CONFIG/STATUS/ZERO/FWINFO",
                    "DTR/RTS 00-10-00-11-00-01-00",
                    "real TAG1 and TAG2/CRC captures",
                    f"{config.reconnect_cycles} close/reopen cycles per hand",
                    f"{config.stall_seconds:g}s host-read stall per hand",
                    "simultaneous short two-hand capture",
                    "optional diagnostic soak plus explicit >=72h release gate",
                    "sealed after snapshot and SHA-256 manifest",
                ],
                "flash_performed": False,
            },
        )
        return _finalize(report)

    backend = backend or ActualBackend()
    targets: Optional[Dict[str, Target]] = None
    observed = None
    try:
        targets, observed = backend.discover(config)
        report.targets = {side: target.as_dict() for side, target in targets.items()}
        report.add(
            "discover exact named pair", PASS, measurements={"observed": observed}
        )
    except Exception as exc:
        report.add("discover exact named pair", FAIL, f"{type(exc).__name__}: {exc}")
        report.error = "cannot safely continue without the exact named pair"
        return _finalize(report)

    before: Dict[str, Any] = {}
    preflight_ready = True
    for side, target in targets.items():
        snapshot = _attempt(report, f"{side}: immutable before snapshot", lambda t=target: backend.snapshot(t))
        if snapshot is None:
            preflight_ready = False
            continue
        before[side] = snapshot
        ok, failures = _snapshot_ok(snapshot, config, side)
        report.add(
            f"{side}: candidate identity/health",
            PASS if ok else FAIL,
            "; ".join(failures),
            {
                "firmware": snapshot["config"].get("fw_rev"),
                "running_image_sha256": snapshot.get("running_image_sha256"),
                "calibration_sha256": snapshot.get("calibration_sha256"),
            },
        )
        preflight_ready = preflight_ready and ok
        path = run_dir / "before" / f"{side}.json"
        digest = _write_json(path, snapshot, seal=True)
        report.artifacts[f"before_{side}"] = f"{path} sha256={digest}"
    report.snapshots["before"] = before

    if not preflight_ready or set(before) != {"left", "right"}:
        report.error = (
            "candidate preflight failed; flash/repair the named units separately, then rerun HIL"
        )
        return _finalize(report)

    for side, target in targets.items():
        _attempt(report, f"{side}: DTR/RTS matrix", lambda t=target: backend.line_matrix(t))

    captures_dir = run_dir / "captures"
    for side, target in targets.items():
        for version in (1, 2):
            result = _attempt(
                report,
                f"{side}: real TAG{version} modality/CRC capture",
                lambda t=target, v=version, s=side: backend.capture(
                    t,
                    version=v,
                    seconds=config.tag_seconds,
                    raw_path=captures_dir / f"{s}-tag{v}.bin",
                ),
            )
            if isinstance(result, Mapping):
                if version == 2:
                    try:
                        bind_raw_tag2_capture(result, contract_evidence)
                    except Exception as exc:
                        report.add(
                            f"{side}: bind real TAG2 bytes to canonical spec",
                            FAIL,
                            f"{type(exc).__name__}: {exc}",
                        )
                    else:
                        report.add(
                            f"{side}: bind real TAG2 bytes to canonical spec",
                            PASS,
                            measurements={
                                "raw_sha256": result.get("raw_sha256"),
                                "spec_sha256": contract_evidence["sha256"],
                                "binding_sha256": result.get(
                                    "tag2_contract_binding_sha256"
                                ),
                            },
                        )
                _write_json(captures_dir / f"{side}-tag{version}.json", result)

    for side, target in targets.items():
        value = _attempt(
            report,
            f"{side}: repeated disconnect/reconnect",
            lambda t=target: backend.reconnect(
                t, cycles=config.reconnect_cycles, seconds=config.reconnect_seconds
            ),
        )
        if value is not None:
            _write_json(run_dir / "reconnect" / f"{side}.json", value)

    for side, target in targets.items():
        value = _attempt(
            report,
            f"{side}: host-read stall and fresh recovery",
            lambda t=target, s=side: backend.capture(
                t,
                version=2,
                seconds=config.recovery_seconds,
                raw_path=captures_dir / f"{s}-post-stall-tag2.bin",
                stall_before_read=config.stall_seconds,
            ),
        )
        if value is not None:
            try:
                bind_raw_tag2_capture(value, contract_evidence)
            except Exception as exc:
                report.add(
                    f"{side}: bind post-stall TAG2 bytes to canonical spec",
                    FAIL,
                    f"{type(exc).__name__}: {exc}",
                )
            _write_json(captures_dir / f"{side}-post-stall-tag2.json", value)

    short = _attempt(
        report,
        "simultaneous short two-hand acceptance",
        lambda: {
            **backend.dual_capture(
                targets,
                seconds=config.short_seconds,
                output_dir=captures_dir,
                label="short",
            ),
        },
    )
    if isinstance(short, Mapping):
        short_devices = short.get("devices", {})
        for value in short_devices.values():
            if isinstance(value, dict):
                try:
                    bind_raw_tag2_capture(value, contract_evidence)
                except Exception as exc:
                    report.add(
                        "bind simultaneous TAG2 bytes to canonical spec",
                        FAIL,
                        f"{type(exc).__name__}: {exc}",
                    )
        _write_json(captures_dir / "short-pair.json", short)

    soak: Optional[Mapping[str, Any]] = None
    if config.soak_seconds is not None:
        soak = _attempt(
            report,
            "requested dual-hand diagnostic soak",
            lambda: backend.soak(
                targets,
                seconds=config.soak_seconds,
                window_seconds=config.window_seconds,
                output_dir=run_dir / "soak",
                store_raw=config.store_soak_raw,
                min_free_gib=config.min_free_gib,
            ),
        )
        if isinstance(soak, Mapping):
            for value in soak.get("devices", {}).values():
                if isinstance(value, dict):
                    try:
                        bind_raw_tag2_capture(value, contract_evidence)
                    except Exception as exc:
                        report.add(
                            "bind soak TAG2 bytes to canonical spec",
                            FAIL,
                            f"{type(exc).__name__}: {exc}",
                        )
            _write_json(run_dir / "soak" / "soak-summary.json", soak)

    soak_verdict, soak_detail, soak_measurements = _release_soak_gate(config, soak)
    report.add("72h dual-hand soak", soak_verdict, soak_detail, soak_measurements)

    after: Dict[str, Any] = {}
    for side, target in targets.items():
        snapshot = _attempt(report, f"{side}: immutable after snapshot", lambda t=target: backend.snapshot(t))
        if snapshot is None:
            continue
        after[side] = snapshot
        path = run_dir / "after" / f"{side}.json"
        digest = _write_json(path, snapshot, seal=True)
        report.artifacts[f"after_{side}"] = f"{path} sha256={digest}"
        if side in before:
            ok, failures = _compare_snapshots(before[side], snapshot)
            report.add(
                f"{side}: before/after identity calibration health counters",
                PASS if ok else FAIL,
                "; ".join(failures),
            )
    report.snapshots["after"] = after

    finished_for_logs = utc_now()
    log_result = capture_kernel_usb_logs(
        report.started_at, finished_for_logs, run_dir / "kernel-usb.log"
    )
    report.add(
        "host kernel USB logs",
        PASS if log_result.get("available") else WARN,
        "captured" if log_result.get("available") else str(log_result.get("reason", "unavailable")),
        log_result,
    )
    return _finalize(report)


def _markdown(data: Mapping[str, Any]) -> str:
    lines = [
        "# OGLO release HIL evidence",
        "",
        f"- Result: **{str(data['result']).upper()}**",
        f"- Started: `{data['started_at']}`",
        f"- Finished: `{data.get('finished_at')}`",
        f"- Expected firmware: `{data['config']['expected_firmware']}`",
        f"- Flash performed by this runner: `{data['flash_performed']}`",
        "",
        "## Checks",
        "",
        "| Result | Check | Detail |",
        "|---|---|---|",
    ]
    for check in data["checks"]:
        detail = str(check.get("detail", "")).replace("|", "\\|").replace("\n", " ")
        name = str(check["name"]).replace("|", "\\|")
        lines.append(f"| {str(check['verdict']).upper()} | {name} | {detail} |")
    lines += [
        "",
        "## Evidence boundary",
        "",
        "This runner never flashes firmware and never changes calibration. Before/after JSON",
        "files are read-only and every artifact is covered by `manifest.sha256`.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "ActualBackend",
    "HilConfig",
    "HilReport",
    "StreamMonitor",
    "Target",
    "capture_tag_stream",
    "run_hil",
    "run_line_matrix",
    "validate_config",
]
