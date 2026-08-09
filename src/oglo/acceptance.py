"""User-run acceptance checks for a physical OGLO USB pair.

This is intentionally different from the pytest hardware suite.  Pytest proves
implementation contracts for developers; this runner walks an owner through a real
pair and leaves a JSON/Markdown record of what was measured.  The default path is
read-only with respect to device state.  Calibration and reversible setting changes
require explicit flags.

Only public SDK objects are used: ``connect``, ``connect_pair``, ``record``,
``replay`` and public ``Glove``/sample members.  That makes this a customer-view test
of the installed SDK rather than a privileged probe that can pass around a broken
public API.
"""

from __future__ import annotations

import json
import math
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO

import numpy as np


PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
_VERDICT_ORDER = {PASS: 0, SKIP: 1, WARN: 2, FAIL: 3}
_FINGERS = ("thumb", "index", "middle", "ring", "pinky")
_STRICT_LOSS_PREFIXES = (
    "wire_",
    "overflow_",
    "transport_",
    "duplicate_",
    "backward_",
)


@dataclass(frozen=True)
class AcceptanceConfig:
    output_root: Path = Path("acceptance-results")
    stream_seconds: float = 5.0
    record_seconds: float = 2.0
    soak_seconds: Optional[float] = None
    mutations: bool = False
    zero: bool = False
    zero_sweep_seconds: int = 5
    interactive: bool = False
    interactive_seconds: float = 1.5
    taxel_delta: float = 25.0
    assume_yes: bool = False


@dataclass
class CheckResult:
    name: str
    verdict: str
    detail: str = ""
    measurements: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AcceptanceReport:
    run_dir: Path
    config: AcceptanceConfig
    sdk_version: str
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: Optional[str] = None
    checks: List[CheckResult] = field(default_factory=list)
    devices: List[Dict[str, Any]] = field(default_factory=list)
    sink: Optional[TextIO] = field(default=None, repr=False)

    def add(
        self,
        name: str,
        verdict: str,
        detail: str = "",
        measurements: Optional[Mapping[str, Any]] = None,
    ) -> CheckResult:
        if verdict not in _VERDICT_ORDER:
            raise ValueError(f"unknown acceptance verdict {verdict!r}")
        check = CheckResult(
            name=name,
            verdict=verdict,
            detail=detail,
            measurements=_jsonable(dict(measurements or {})),
        )
        self.checks.append(check)
        if self.sink is not None:
            mark = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}[verdict]
            print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), file=self.sink)
            self.sink.flush()
        return check

    @property
    def worst(self) -> str:
        if not self.checks:
            return FAIL
        if any(c.verdict == FAIL for c in self.checks):
            return FAIL
        if any(c.verdict == WARN for c in self.checks):
            return WARN
        if any(c.verdict == PASS for c in self.checks):
            return PASS
        return SKIP

    @property
    def failed(self) -> bool:
        return any(c.verdict == FAIL for c in self.checks)

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.write()

    def as_dict(self) -> Dict[str, Any]:
        cfg = asdict(self.config)
        cfg["output_root"] = str(self.config.output_root)
        return {
            "schema": 1,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sdk_version": self.sdk_version,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "result": FAIL if self.failed else self.worst,
            "config": _jsonable(cfg),
            "devices": _jsonable(self.devices),
            "checks": [_jsonable(asdict(c)) for c in self.checks],
        }

    def write(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        data = self.as_dict()
        (self.run_dir / "acceptance-report.json").write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.run_dir / "acceptance-report.md").write_text(
            _markdown_report(data), encoding="utf-8"
        )


def parse_duration(value: str) -> float:
    """Parse seconds or a compact duration such as ``75m`` or ``1.5h``."""
    text = str(value).strip().lower()
    multiplier = 1.0
    for suffix, factor in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break
    try:
        result = float(text) * multiplier
    except ValueError as exc:
        raise ValueError(f"invalid duration {value!r}; use seconds, 75m, or 1.5h") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("duration must be finite and greater than zero")
    return result


def run_acceptance(
    config: AcceptanceConfig,
    *,
    sdk: Optional[Any] = None,
    input_fn: Callable[[str], str] = input,
    sink: Optional[TextIO] = None,
) -> AcceptanceReport:
    """Run the requested acceptance checks and always leave a report.

    ``sdk`` and ``input_fn`` are injectable so the orchestration and refusal paths
    can be tested without opening hardware or waiting for a person.
    """
    _validate_config(config)
    if sdk is None:
        import oglo as sdk  # public package, deliberately not private transports

    if sink is None:
        sink = sys.stdout
    run_dir = _new_run_dir(Path(config.output_root))
    report = AcceptanceReport(
        run_dir=run_dir,
        config=config,
        sdk_version=str(getattr(sdk, "__version__", "unknown")),
        sink=sink,
    )
    print(f"Acceptance artifacts: {run_dir}", file=sink)
    print("Default checks do not change calibration or stream settings.", file=sink)
    if config.mutations:
        print("Reversible RAW/CLEAN/rate changes are ENABLED.", file=sink)
    if config.zero:
        print("ZERO REPLACEMENT is ENABLED and cannot be automatically restored.", file=sink)

    gloves: List[Any] = []
    serials: List[str] = []
    stream_stats: Dict[str, Dict[str, float]] = {}
    try:
        try:
            left, right = sdk.connect_pair()
            gloves = [left, right]
        except Exception as exc:
            report.add(
                "connect left/right USB pair",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )
            return report

        gloves.sort(key=lambda g: g.info.side)
        serials = [str(g.info.serial) for g in gloves]
        report.devices = [_device_dict(g.info) for g in gloves]
        _check_pair(report, gloves)
        _check_public_iterators(report, gloves, sdk)
        stream_stats = _check_streams(report, gloves, config.stream_seconds, sdk)

        if config.interactive:
            _interactive_checks(report, gloves, config, input_fn)
        else:
            report.add(
                "human tactile/IMU interaction",
                SKIP,
                "pass --interactive to test finger labels and motion response",
            )

        if config.mutations:
            _mutation_checks(report, gloves, stream_stats, sdk)
        else:
            report.add(
                "reversible RAW/CLEAN/rate mutations",
                SKIP,
                "pass --mutations to enable state-changing checks",
            )

        if config.zero:
            _zero_checks(report, gloves, config, input_fn)
        else:
            report.add(
                "zero sweep replacement",
                SKIP,
                "pass --zero only while wearing and moving the glove; this overwrites calibration",
            )

        if config.record_seconds > 0:
            _record_replay_pair(
                report,
                gloves,
                config.record_seconds,
                report.run_dir / "recordings",
                sdk,
                label="short record/replay",
            )

        if config.soak_seconds is not None:
            _record_replay_pair(
                report,
                gloves,
                config.soak_seconds,
                report.run_dir / "soak",
                sdk,
                label="long two-hand soak",
            )
        else:
            report.add(
                "72+ minute clock-rollover soak",
                SKIP,
                "pass --soak 75m to qualify rollover and target storage",
            )
    except KeyboardInterrupt:
        report.add("acceptance run completed", FAIL, "cancelled by user")
        raise
    except Exception as exc:
        report.add(
            "acceptance runner",
            FAIL,
            f"unexpected {type(exc).__name__}: {exc}",
        )
    finally:
        for glove in gloves:
            try:
                glove.close()
            except Exception as exc:
                report.add(
                    f"close {getattr(getattr(glove, 'info', None), 'serial', '?')}",
                    FAIL,
                    f"{type(exc).__name__}: {exc}",
                )

        if serials:
            _check_logical_reconnect(report, serials, sdk)
        report.finish()
        print(
            f"Result: {'FAIL' if report.failed else report.worst.upper()} — "
            f"{report.run_dir / 'acceptance-report.md'}",
            file=sink,
        )
    return report


def _check_pair(report: AcceptanceReport, gloves: Sequence[Any]) -> None:
    infos = [g.info for g in gloves]
    sides = {i.side for i in infos}
    report.add(
        "one left and one right glove",
        PASS if sides == {"left", "right"} else FAIL,
        ", ".join(f"{i.serial}={i.side}" for i in infos),
    )
    unique = len({str(i.serial).casefold() for i in infos}) == 2
    report.add(
        "distinct logical serials",
        PASS if unique else FAIL,
        ", ".join(i.serial for i in infos),
    )

    for info in infos:
        raw = dict(getattr(info, "raw", {}) or {})
        fw = _version_tuple(str(info.fw_rev))
        report.add(
            f"{info.serial}: firmware 0.9.10",
            PASS if fw == (0, 9, 10) else FAIL,
            f"reported {info.fw_rev}",
        )
        report.add(
            f"{info.serial}: CONFIG schema 6",
            PASS if raw.get("schema_ver") == 6 else FAIL,
            f"reported {raw.get('schema_ver')!r}",
        )
        contract_ok = (
            info.transport == "usb"
            and raw.get("values_per_sample") == 80
            and raw.get("sample_shape") == [5, 4, 4]
            and raw.get("imu_len") == 25
            and set(info.channels) == set(_FINGERS)
        )
        report.add(
            f"{info.serial}: USB data contract",
            PASS if contract_ok else FAIL,
            f"transport={info.transport}, shape={raw.get('sample_shape')}, "
            f"imu_len={raw.get('imu_len')}, channels={info.channels}",
        )
        expected = (
            ["pinky", "ring", "middle", "index", "thumb"]
            if info.side == "left"
            else ["thumb", "index", "middle", "ring", "pinky"]
        )
        report.add(
            f"{info.serial}: finger order",
            PASS if list(info.channels) == expected else FAIL,
            ", ".join(info.channels),
        )
        report.add(
            f"{info.serial}: zero available",
            PASS if info.zero_valid else WARN,
            (
                "captured"
                if info.zero_valid
                else "missing; raw ADC still works but clean data is unavailable"
            ),
        )
        try:
            glove = next(g for g in gloves if g.info is info)
            status = glove.status()
            healthy = bool(status.healthy)
            report.add(
                f"{info.serial}: sensor health",
                PASS if healthy else FAIL,
                f"imu_ok={status.imu_ok}, mag_ok={status.mag_ok}, "
                f"sensor_ok={status.sensor_ok}, error_flags={status.error_flags}",
                _status_dict(status),
            )
        except Exception as exc:
            report.add(
                f"{info.serial}: sensor health",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )
        if info.zero_valid:
            try:
                line = glove.send("GET ZERO", expect="#TZERO ", timeout=5.0)
                recipe = json.loads(line.removeprefix("#TZERO "))
                recipe_ok = (
                    recipe.get("valid") is True
                    and recipe.get("count") == 80
                    and len(recipe.get("baseline", [])) == 80
                    and len(recipe.get("noise", [])) == 80
                    and recipe.get("thr") == info.stream_thr
                    and recipe.get("clean") is info.stream_clean
                )
                report.add(
                    f"{info.serial}: public send(GET ZERO)",
                    PASS if recipe_ok else FAIL,
                    "80-taxel recipe agrees with CONFIG" if recipe_ok else "recipe mismatch",
                    {
                        "count": recipe.get("count"),
                        "threshold": recipe.get("thr"),
                        "clean": recipe.get("clean"),
                        "locked": recipe.get("locked"),
                    },
                )
            except Exception as exc:
                report.add(
                    f"{info.serial}: public send(GET ZERO)",
                    FAIL,
                    f"{type(exc).__name__}: {exc}",
                )
        else:
            report.add(f"{info.serial}: public send(GET ZERO)", SKIP, "zero_valid=false")


def _check_public_iterators(report: AcceptanceReport, gloves: Sequence[Any], sdk: Any) -> None:
    for glove in gloves:
        serial = glove.info.serial
        for name in ("tactile", "imu", "mag"):
            if name == "mag" and not glove.info.has_mag:
                report.add(f"{serial}: {name} iterator", SKIP, "has_mag=false")
                continue
            try:
                glove.stop()
                sample = next(getattr(glove, name)(timeout=2.0))
                type_name = {"tactile": "Frame", "imu": "ImuSample", "mag": "MagSample"}[name]
                expected = getattr(sdk, type_name)
                report.add(
                    f"{serial}: {name} iterator",
                    PASS if isinstance(sample, expected) else FAIL,
                    type(sample).__name__,
                )
            except Exception as exc:
                report.add(
                    f"{serial}: {name} iterator",
                    FAIL,
                    f"{type(exc).__name__}: {exc}",
                )
        try:
            glove.stop()
            glove.start()
            batch = glove.read_batch(timeout=2.0)
            report.add(
                f"{serial}: stop/start/read_batch",
                PASS if bool(batch) else FAIL,
                "stream resumed" if batch else "no sample after restart",
            )
            glove.stop()
        except Exception as exc:
            report.add(
                f"{serial}: stop/start/read_batch",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )


def _check_streams(
    report: AcceptanceReport,
    gloves: Sequence[Any],
    seconds: float,
    sdk: Any,
) -> Dict[str, Dict[str, float]]:
    before: Dict[str, Any] = {}
    for glove in gloves:
        glove.stop()
        try:
            before[glove.info.side] = glove.status()
        except Exception as exc:
            report.add(
                f"{glove.info.serial}: pre-stream status",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {g.info.side: pool.submit(_collect, g, seconds) for g in gloves}
        collected = {side: future.result() for side, future in futures.items()}

    stats: Dict[str, Dict[str, float]] = {}
    for glove in gloves:
        side = glove.info.side
        samples = collected[side]
        stats[side] = _sample_stats(samples)
        _report_sample_contract(report, glove, samples, stats[side], sdk)
        public_rates = dict(glove.rates_seen)
        rates_seen_ok = all(
            float(public_rates.get(name, 0.0)) > 0
            for name in ("tactile", "imu") + (("mag",) if glove.info.has_mag else ())
        )
        report.add(
            f"{glove.info.serial}: public rates_seen",
            PASS if rates_seen_ok else FAIL,
            ", ".join(f"{name}={hz:.1f}" for name, hz in public_rates.items()),
            public_rates,
        )
        _report_loss(report, glove)
        try:
            after = glove.status()
            start = before.get(side)
            if start is None:
                continue
            deltas = {
                "tag_dropped": after.tag_dropped - start.tag_dropped,
                "tag_short_writes": after.tag_short_writes - start.tag_short_writes,
                "deadline_misses": after.deadline_misses - start.deadline_misses,
            }
            healthy = (
                after.healthy
                and after.uptime_ms >= start.uptime_ms
                and all(v == 0 for v in deltas.values())
            )
            report.add(
                f"{glove.info.serial}: device health during stream",
                PASS if healthy else FAIL,
                f"healthy={after.healthy}, uptime "
                f"{start.uptime_ms}->{after.uptime_ms}, deltas={deltas}",
                deltas,
            )
        except Exception as exc:
            report.add(
                f"{glove.info.serial}: post-stream status",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )
    return stats


def _report_sample_contract(
    report: AcceptanceReport,
    glove: Any,
    samples: Mapping[str, Sequence[Any]],
    stats: Mapping[str, float],
    sdk: Any,
) -> None:
    serial = glove.info.serial
    for name in ("tactile", "imu", "mag"):
        values = list(samples[name])
        if name == "mag" and not glove.info.has_mag:
            report.add(f"{serial}: {name} samples", SKIP, "has_mag=false")
            continue
        monotonic = bool(values) and all(
            b.device_time_us >= a.device_time_us
            and b.host_t_ns >= a.host_t_ns
            and b.host_received_ns == b.host_t_ns
            and b.dropped == 0
            for a, b in zip(values, values[1:])
        ) and all(v.host_received_ns == v.host_t_ns and v.dropped == 0 for v in values)
        report.add(
            f"{serial}: {name} timestamps/sequences",
            PASS if monotonic else FAIL,
            f"{len(values)} samples",
        )

    tactile = list(samples["tactile"])
    tactile_ok = bool(tactile) and all(
        isinstance(f, sdk.Frame)
        and f.counts.shape == (5, 4, 4)
        and f.counts.dtype == np.uint16
        and 0 <= int(f.counts.min()) <= int(f.counts.max()) <= 4095
        and f.finger(0).shape == (4, 4)
        and f.device_time_ns == f.device_time_us * 1000
        for f in tactile
    )
    if tactile:
        try:
            if glove.info.stream_clean:
                residual_ok = np.array_equal(
                    tactile[0].residual, tactile[0].counts.astype(np.float32)
                )
            else:
                try:
                    _ = tactile[0].residual
                    residual_ok = False
                except sdk.CleanStreamError:
                    residual_ok = True
        except Exception:
            residual_ok = False
    else:
        residual_ok = False
    orientation_refused = False
    if tactile:
        try:
            _ = tactile[0].orientation
        except NotImplementedError:
            orientation_refused = True
    report.add(
        f"{serial}: tactile values and RAW/CLEAN semantics",
        PASS if tactile_ok and residual_ok and orientation_refused else FAIL,
        f"shape/dtype/range={tactile_ok}, residual={residual_ok}, "
        f"orientation_refused={orientation_refused}",
    )

    imu = list(samples["imu"])
    imu_ok = bool(imu) and all(
        isinstance(s, sdk.ImuSample)
        and len(s.accel) == len(s.gyro) == 3
        and s.device_time_ns == s.device_time_us * 1000
        and all(math.isfinite(v) for v in (*s.accel, *s.gyro, *s.accel_frame, *s.gyro_frame))
        for s in imu
    )
    imu_orientation_refused = False
    if imu:
        try:
            _ = imu[0].orientation
        except NotImplementedError:
            imu_orientation_refused = True
    report.add(
        f"{serial}: IMU values",
        PASS if imu_ok and imu_orientation_refused else FAIL,
        f"{len(imu)} finite samples, orientation_refused={imu_orientation_refused}",
    )

    mag = list(samples["mag"])
    if glove.info.has_mag:
        mag_ok = bool(mag) and all(
            isinstance(s, sdk.MagSample)
            and len(s.field) == 3
            and all(math.isfinite(v) for v in s.field)
            and math.isfinite(s.magnitude)
            for s in mag
        )
        report.add(
            f"{serial}: magnetometer values",
            PASS if mag_ok else FAIL,
            f"{len(mag)} finite samples",
        )

    expected = {
        "tactile": (0.90 * glove.info.rate_hz, 1.10 * glove.info.rate_hz),
        "imu": (450.0, 550.0),
        "mag": (105.0, 145.0),
    }
    for name, (low, high) in expected.items():
        if name == "mag" and not glove.info.has_mag:
            continue
        hz = float(stats.get(f"{name}_hz", 0.0))
        report.add(
            f"{serial}: {name} rate",
            PASS if low <= hz <= high else FAIL,
            f"{hz:.1f} packets/s; expected {low:.1f}..{high:.1f}",
            {"delivered_hz": hz, "expected_min_hz": low, "expected_max_hz": high},
        )


def _report_loss(report: AcceptanceReport, glove: Any) -> None:
    counters = dict(glove.dropped)
    strict = {
        name: int(value)
        for name, value in counters.items()
        if name == "unrouted_packets" or name.startswith(_STRICT_LOSS_PREFIXES)
    }
    ok = bool(strict) and all(value == 0 for value in strict.values())
    report.add(
        f"{glove.info.serial}: host/wire loss counters",
        PASS if ok else FAIL,
        "all zero" if ok else str({k: v for k, v in strict.items() if v}),
        strict,
    )


def _mutation_checks(
    report: AcceptanceReport,
    gloves: Sequence[Any],
    stream_stats: Mapping[str, Mapping[str, float]],
    sdk: Any,
) -> None:
    for glove in gloves:
        glove.stop()
    for glove in gloves:
        for other in gloves:
            other.stop()
        serial = glove.info.serial
        original_rate = int(glove.info.rate_hz)
        original_clean = bool(glove.info.stream_clean)
        original_threshold = int(glove.info.stream_thr)
        baseline_imu = float(stream_stats.get(glove.info.side, {}).get("imu_hz", 0.0))
        restore_imu = 500 if 450.0 <= baseline_imu <= 550.0 else None
        changed_ok = True
        detail: List[str] = []
        try:
            glove.stop()
            glove.raw()
            frame = next(glove.tactile(timeout=2.0))
            try:
                _ = frame.residual
                changed_ok = False
                detail.append("raw residual did not raise")
            except sdk.CleanStreamError:
                pass

            if not glove.info.zero_valid:
                changed_ok = False
                detail.append("zero_valid=false prevents CLEAN test")
            else:
                alternate_threshold = original_threshold + 1 if original_threshold < 4095 else 4094
                glove.clean(threshold=alternate_threshold)
                clean = next(glove.tactile(timeout=2.0))
                if not np.array_equal(clean.residual, clean.counts.astype(np.float32)):
                    changed_ok = False
                    detail.append("clean residual mismatch")

            alternate_rate = 200 if original_rate != 200 else 250
            kwargs: Dict[str, int] = {"tactile": alternate_rate}
            if restore_imu is not None:
                kwargs["imu"] = 250
            glove.rates(**kwargs)
            changed = _collect(glove, 1.5)
            changed_stats = _sample_stats(changed)
            if not 0.88 * alternate_rate <= changed_stats["tactile_hz"] <= 1.12 * alternate_rate:
                changed_ok = False
                detail.append(f"tactile rate {changed_stats['tactile_hz']:.1f}")
            if restore_imu is not None and not 220.0 <= changed_stats["imu_hz"] <= 280.0:
                changed_ok = False
                detail.append(f"IMU rate {changed_stats['imu_hz']:.1f}")
        except Exception as exc:
            changed_ok = False
            detail.append(f"{type(exc).__name__}: {exc}")
        finally:
            restored = True
            try:
                restore: Dict[str, int] = {"tactile": original_rate}
                if restore_imu is not None:
                    restore["imu"] = restore_imu
                glove.rates(**restore)
                if original_clean:
                    glove.clean(threshold=original_threshold)
                else:
                    if glove.info.zero_valid and glove.info.stream_thr != original_threshold:
                        glove.clean(threshold=original_threshold)
                    glove.raw()
                restored = (
                    glove.info.rate_hz == original_rate
                    and glove.info.stream_clean is original_clean
                    and glove.info.stream_thr == original_threshold
                )
            except Exception as exc:
                restored = False
                detail.append(f"RESTORE {type(exc).__name__}: {exc}")
            report.add(
                f"{serial}: reversible RAW/CLEAN/rate mutations",
                PASS if changed_ok and restored else FAIL,
                (
                    "; ".join(detail)
                    if detail
                    else "changed, read back, and restored original settings"
                ),
                {
                    "original_tactile_hz": original_rate,
                    "original_imu_measured_hz": baseline_imu,
                    "original_clean": original_clean,
                    "original_threshold": original_threshold,
                    "restored": restored,
                },
            )


def _interactive_checks(
    report: AcceptanceReport,
    gloves: Sequence[Any],
    config: AcceptanceConfig,
    input_fn: Callable[[str], str],
) -> None:
    for glove in gloves:
        for other in gloves:
            other.stop()
        serial = glove.info.serial
        channels = list(glove.info.channels)
        for finger in _FINGERS:
            reply = input_fn(
                f"\n{serial} ({glove.info.side}) {finger}: release the hand, then press Enter "
                "(or type s to skip): "
            ).strip().lower()
            if reply == "s":
                report.add(f"{serial}: physical {finger} mapping", SKIP, "skipped by user")
                continue
            glove.stop()
            baseline = _collect(glove, 0.6)["tactile"]
            glove.stop()
            input_fn(f"Get ready to press only the {finger} sensing area, then press Enter: ")
            print(
                f"Press {finger} now and hold for {config.interactive_seconds:.1f}s.",
                file=report.sink,
            )
            time.sleep(0.5)
            active = _collect(glove, config.interactive_seconds)["tactile"]
            glove.stop()
            scores = _finger_scores(baseline, active, channels)
            strongest = max(scores, key=scores.get) if scores else "none"
            selected = float(scores.get(finger, 0.0))
            ok = selected >= config.taxel_delta and strongest == finger
            report.add(
                f"{serial}: physical {finger} mapping",
                PASS if ok else FAIL,
                f"strongest={strongest}, selected delta={selected:.1f} counts",
                {"finger_delta_counts": scores, "required_delta": config.taxel_delta},
            )

        input_fn(f"\n{serial}: hold the glove still and press Enter: ")
        glove.stop()
        still = _collect(glove, 0.8)
        glove.stop()
        input_fn(f"Get ready to rotate {serial}, then press Enter: ")
        print(f"Rotate now for {config.interactive_seconds:.1f}s.", file=report.sink)
        time.sleep(0.5)
        moving = _collect(glove, config.interactive_seconds)
        glove.stop()
        still_gyro = _max_gyro(still["imu"])
        moving_gyro = _max_gyro(moving["imu"])
        motion_ok = moving_gyro >= max(20.0, still_gyro * 1.5)
        report.add(
            f"{serial}: physical IMU motion response",
            PASS if motion_ok else FAIL,
            f"still max={still_gyro:.1f}, moving max={moving_gyro:.1f} deg/s",
            {"still_max_dps": still_gyro, "moving_max_dps": moving_gyro},
        )
        if glove.info.has_mag:
            mag_span = _mag_span(moving["mag"])
            report.add(
                f"{serial}: magnetometer changes while moved",
                PASS if mag_span >= 0.02 else WARN,
                f"max vector span={mag_span:.3f} G; this checks response, not axis calibration",
                {"max_vector_span_gauss": mag_span},
            )


def _zero_checks(
    report: AcceptanceReport,
    gloves: Sequence[Any],
    config: AcceptanceConfig,
    input_fn: Callable[[str], str],
) -> None:
    for glove in gloves:
        serial = glove.info.serial
        for other in gloves:
            other.stop()
        if not config.assume_yes:
            phrase = f"ZERO {serial}"
            answer = input_fn(
                f"\nThis replaces calibration on {serial}. Wear it, touch nothing, and open/close "
                f"the hand during the sweep. Type exactly '{phrase}' to continue: "
            )
            if answer.strip() != phrase:
                report.add(f"{serial}: zero sweep", SKIP, "confirmation phrase did not match")
                continue
        print(
            f"Starting {config.zero_sweep_seconds}s zero on {serial}; open and close the hand now.",
            file=report.sink,
        )
        try:
            recipe = glove.zero(sweep=config.zero_sweep_seconds)
            ok = (
                recipe.get("valid") is True
                and recipe.get("count") == 80
                and len(recipe.get("baseline", [])) == 80
                and len(recipe.get("noise", [])) == 80
                and glove.info.zero_valid
            )
            report.add(
                f"{serial}: zero sweep",
                PASS if ok else FAIL,
                "recipe and CONFIG read-back agree" if ok else "invalid recipe/read-back",
                {
                    "frames": recipe.get("frames"),
                    "threshold": recipe.get("thr"),
                    "locked": recipe.get("locked"),
                },
            )
            report.add(
                f"{serial}: zero survives power cycle",
                SKIP,
                "unplug/replug and rerun read-only acceptance; firmware cannot prove "
                "flash persistence itself",
            )
        except Exception as exc:
            report.add(f"{serial}: zero sweep", FAIL, f"{type(exc).__name__}: {exc}")


def _record_replay_pair(
    report: AcceptanceReport,
    gloves: Sequence[Any],
    seconds: float,
    root: Path,
    sdk: Any,
    *,
    label: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for glove in gloves:
        glove.stop()
    paths: Dict[str, Path] = {}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                g.info.side: pool.submit(sdk.record, root / g.info.side, seconds, glove=g)
                for g in gloves
            }
            for side, future in futures.items():
                paths[side] = Path(future.result())
    except Exception as exc:
        partial = getattr(exc, "partial_episode", None)
        report.add(
            label,
            FAIL,
            f"{type(exc).__name__}: {exc}" + (f"; partial={partial}" if partial else ""),
        )
        return

    all_ok = True
    measurements: Dict[str, Any] = {"seconds_requested": seconds, "episodes": {}}
    details: List[str] = []
    for glove in gloves:
        side = glove.info.side
        try:
            episode = sdk.replay(paths[side])
            summary = episode.summary()
            required = ("tactile", "imu") + (("mag",) if glove.info.has_mag else ())
            ok = (
                summary.get("complete") is True
                and summary.get("serial") == glove.info.serial
                and summary.get("side") == side
                and all(summary.get(name, {}).get("n", 0) >= 2 for name in required)
                and all(summary.get(name, {}).get("dropped", 0) == 0 for name in required)
                and len(episode) == summary["tactile"]["n"]
                and episode.info.serial == glove.info.serial
            )
            first = next(iter(episode), None)
            ok = ok and isinstance(first, sdk.Frame)
            iterator_types = {
                "tactile": sdk.Frame,
                "imu": sdk.ImuSample,
                "mag": sdk.MagSample,
            }
            for name in required:
                arrays = episode.arrays(name)
                ok = ok and len(arrays["seq"]) == summary[name]["n"]
                sample = next(getattr(episode, name)(), None)
                ok = ok and isinstance(sample, iterator_types[name])
            all_ok = all_ok and ok
            measurements["episodes"][side] = {"path": str(paths[side]), "summary": summary}
            details.append(f"{side}={paths[side].name}:{'ok' if ok else 'bad'}")
        except Exception as exc:
            all_ok = False
            details.append(f"{side}={type(exc).__name__}:{exc}")
    report.add(label, PASS if all_ok else FAIL, ", ".join(details), measurements)


def _check_logical_reconnect(report: AcceptanceReport, serials: Sequence[str], sdk: Any) -> None:
    for serial in serials:
        glove = None
        try:
            glove = sdk.connect(serial=serial, transport="usb", timeout=10.0)
            ok = glove.info.serial == serial
            report.add(
                f"logical reconnect {serial}",
                PASS if ok else FAIL,
                f"returned {glove.info.serial}",
            )
        except Exception as exc:
            report.add(
                f"logical reconnect {serial}",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if glove is not None:
                try:
                    glove.close()
                except Exception:
                    pass


def _collect(glove: Any, seconds: float) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {"tactile": [], "imu": [], "mag": []}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        batch = glove.read_batch(timeout=min(0.2, remaining))
        for name, values in batch.as_dict().items():
            out[name].extend(values)
    return out


def _sample_stats(samples: Mapping[str, Sequence[Any]]) -> Dict[str, float]:
    return {f"{name}_hz": _rate(values) for name, values in samples.items()}


def _rate(samples: Sequence[Any]) -> float:
    if len(samples) < 2:
        return 0.0
    span = float(samples[-1].host_t) - float(samples[0].host_t)
    return (len(samples) - 1) / span if span > 0 else 0.0


def _finger_scores(
    baseline: Sequence[Any], active: Sequence[Any], channels: Sequence[str]
) -> Dict[str, float]:
    if not baseline or not active:
        return {}
    base = np.median(np.stack([f.counts for f in baseline]).astype(np.float32), axis=0)
    observed = np.stack([f.counts for f in active]).astype(np.float32)
    delta = np.max(np.abs(observed - base), axis=0)
    return {str(name): float(delta[index].max()) for index, name in enumerate(channels)}


def _max_gyro(samples: Sequence[Any]) -> float:
    return max(
        (math.sqrt(sum(float(v) ** 2 for v in sample.gyro)) for sample in samples),
        default=0.0,
    )


def _mag_span(samples: Sequence[Any]) -> float:
    if len(samples) < 2:
        return 0.0
    values = np.asarray([sample.field for sample in samples], dtype=np.float64)
    return float(np.linalg.norm(values.max(axis=0) - values.min(axis=0)))


def _status_dict(status: Any) -> Dict[str, Any]:
    return {
        "uptime_ms": status.uptime_ms,
        "imu_ok": status.imu_ok,
        "mag_ok": status.mag_ok,
        "sensor_ok": status.sensor_ok,
        "error_flags": status.error_flags,
        "deadline_misses": status.deadline_misses,
        "tag_dropped": status.tag_dropped,
        "tag_short_writes": status.tag_short_writes,
    }


def _device_dict(info: Any) -> Dict[str, Any]:
    return {
        "serial": info.serial,
        "side": info.side,
        "hardware": info.hw_rev,
        "firmware": info.fw_rev,
        "transport": info.transport,
        "channels": list(info.channels),
        "zero_valid": info.zero_valid,
        "stream_clean": info.stream_clean,
        "stream_threshold": info.stream_thr,
        "config_schema": dict(getattr(info, "raw", {}) or {}).get("schema_ver"),
    }


def _version_tuple(value: str) -> Optional[tuple[int, int, int]]:
    parts: List[int] = []
    for chunk in value.split(".")[:3]:
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        parts.append(int(digits))
    if len(parts) != 3:
        return None
    return tuple(parts)  # type: ignore[return-value]


def _validate_config(config: AcceptanceConfig) -> None:
    for name in ("stream_seconds", "interactive_seconds"):
        value = float(getattr(config, name))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and greater than zero")
    if not math.isfinite(float(config.record_seconds)) or config.record_seconds < 0:
        raise ValueError("record_seconds must be finite and non-negative")
    if config.soak_seconds is not None and (
        not math.isfinite(float(config.soak_seconds)) or config.soak_seconds <= 0
    ):
        raise ValueError("soak_seconds must be finite and greater than zero")
    if not 1 <= int(config.zero_sweep_seconds) <= 30:
        raise ValueError("zero_sweep_seconds must be 1..30")
    if not math.isfinite(float(config.taxel_delta)) or config.taxel_delta <= 0:
        raise ValueError("taxel_delta must be finite and greater than zero")


def _new_run_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        candidate = root / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _markdown_report(data: Mapping[str, Any]) -> str:
    lines = [
        "# OGLO acceptance report",
        "",
        f"- Result: **{str(data['result']).upper()}**",
        f"- SDK: `{data['sdk_version']}`",
        f"- Started: `{data['started_at']}`",
        f"- Finished: `{data.get('finished_at')}`",
        f"- Python: `{data['python']}`",
        f"- Platform: `{data['platform']}`",
        "",
        "## Devices",
        "",
    ]
    for device in data.get("devices", []):
        lines.append(
            f"- `{device['serial']}` — {device['side']}, firmware {device['firmware']}, "
            f"schema {device['config_schema']}"
        )
    lines.extend(["", "## Checks", "", "| Result | Check | Detail |", "| --- | --- | --- |"])
    for check in data.get("checks", []):
        detail = str(check.get("detail", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| **{str(check['verdict']).upper()}** | {check['name']} | {detail} |"
        )
    lines.extend(
        [
            "",
            "## Scope warning",
            "",
            "A PASS proves only the checks and duration listed above. It is not proof of "
            "hardware synchronisation, Newton calibration, BLE qualification, or payload "
            "integrity beyond what firmware 0.9.10 exposes.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "AcceptanceConfig",
    "AcceptanceReport",
    "CheckResult",
    "FAIL",
    "PASS",
    "SKIP",
    "WARN",
    "parse_duration",
    "run_acceptance",
]
