"""`oglo doctor`: measure this machine, do not ask the user to read a table.

Published rates are what a board did on our bench. What matters to someone with a
problem is what their machine is receiving right now, so this connects, streams for a
few seconds and reports the delivered rate per stream against what the board says it
is producing.

Every check returns a verdict rather than a number alone. A number needs a reader who
knows the expected value; a verdict does not.
"""

from __future__ import annotations

import math
import platform
import sys
import time
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Callable, Dict, List, Optional

OK, WARN, FAIL = "ok", "warn", "fail"

#: Delivered rate below this fraction of the configured rate is a failure. Generous,
#: because a laptop under load loses a few percent and that is not a broken glove.
RATE_FAIL_BELOW = 0.85
RATE_WARN_BELOW = 0.95

@dataclass
class Check:
    name: str
    verdict: str
    detail: str = ""

    def __str__(self) -> str:
        mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[self.verdict]
        return f"[{mark}] {self.name}" + (f"\n           {self.detail}" if self.detail else "")


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, verdict: str, detail: str = "") -> None:
        self.checks.append(Check(name, verdict, detail))

    @property
    def worst(self) -> str:
        for level in (FAIL, WARN):
            if any(c.verdict == level for c in self.checks):
                return level
        return OK

    def __str__(self) -> str:
        return "\n".join(str(c) for c in self.checks)


def _environment(rep: Report) -> None:
    v = sys.version_info
    rep.add(
        f"python {v.major}.{v.minor}.{v.micro}",
        OK if (v.major, v.minor) >= (3, 10) else FAIL,
        "" if (v.major, v.minor) >= (3, 10) else "the SDK needs 3.10 or newer",
    )
    for mod, why in (("serial", "USB transport"), ("numpy", "sample arrays")):
        try:
            __import__(mod)
            rep.add(f"{mod} installed", OK)
        except ImportError:
            rep.add(
                f"{mod} missing",
                FAIL,
                f"required for the {why}; reinstall this checkout with "
                "python -m pip install -e .",
            )
    try:
        __import__("bleak")
        rep.add("bleak installed", OK, "BLE transport available")
    except ImportError:
        rep.add("bleak missing", WARN, "USB works; BLE will not")
    rep.add(f"platform {platform.system()} {platform.release()}", OK)


def _ports(rep: Report) -> List[Any]:
    from ._usb import list_all_ports, list_candidates

    gloves = list_candidates()
    others = [c for c in list_all_ports() if c not in gloves]

    if not gloves:
        rep.add(
            "no glove found",
            FAIL,
            "nothing enumerated under a known vendor id. Check the cable (a "
            "charge-only USB-C cable enumerates nothing at all) and that the board "
            "is powered.",
        )
    else:
        rep.add(
            f"{len(gloves)} glove(s) on USB",
            OK,
            "; ".join(f"{c.serial_number} at {c.device}" for c in gloves),
        )
    if others:
        # Naming these matters: one of them once blocked a probe for two minutes.
        rep.add(
            f"{len(others)} other serial device(s) present",
            OK,
            "ignored, not gloves: "
            + "; ".join(f"{c.product or '?'} ({c.serial_number})" for c in others),
        )
    return gloves


def _glove(rep: Report, g: Any, seconds: float) -> None:
    i = g.info
    rep.add(f"{i.serial} ({i.side})", OK, f"hw {i.hw_rev}, fw {i.fw_rev}, configured {i.rate_hz} Hz")

    status_start = None
    try:
        status_start = g.status()
        start_healthy = status_start.healthy and (not i.has_mag or status_start.mag_ok)
        rep.add(
            f"{i.serial}: sensor health",
            OK if start_healthy else FAIL,
            f"imu_ok={status_start.imu_ok}, mag_ok={status_start.mag_ok}, "
            f"sensor_ok={status_start.sensor_ok}, error_flags={status_start.error_flags}",
        )
    except Exception as exc:
        rep.add(f"{i.serial}: status unreadable", FAIL, f"{type(exc).__name__}: {exc}")

    # Calibration state, which is the most common cause of "the numbers look wrong".
    if not i.zero_valid:
        rep.add(
            f"{i.serial}: no zero captured",
            WARN,
            "every taxel will read its idle offset (~550), not 0. Run "
            "glove.zero(sweep=5) while opening and closing the hand.",
        )
    elif i.stream_clean:
        rep.add(f"{i.serial}: clean stream", OK, f"device applies its zero, deadband {i.stream_thr}")
    else:
        rep.add(
            f"{i.serial}: raw stream",
            OK,
            "counts are unprocessed ADC; .residual will raise unless you zero first",
        )

    # The measurement.
    counts = {"tactile": 0, "imu": 0, "mag": 0}
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        for name, items in g.read_batch().as_dict().items():
            counts[name] += len(items)
    el = time.monotonic() - t0
    status_end = None
    try:
        status_end = g.status()
        end_healthy = status_end.healthy and (not i.has_mag or status_end.mag_ok)
        if not end_healthy:
            rep.add(
                f"{i.serial}: sensor health changed during check",
                FAIL,
                f"imu_ok={status_end.imu_ok}, mag_ok={status_end.mag_ok}, "
                f"sensor_ok={status_end.sensor_ok}, error_flags={status_end.error_flags}",
            )
    except Exception as exc:
        rep.add(f"{i.serial}: final status unreadable", FAIL, f"{type(exc).__name__}: {exc}")

    expected = {"tactile": float(i.rate_hz or 0)}
    imu_period = i.imu_period_ms
    if imu_period:
        expected["imu"] = 1000.0 / float(imu_period)
    for name in ("tactile", "imu", "mag"):
        if name == "mag" and not i.has_mag:
            rep.add(
                f"{i.serial}: magnetometer unavailable",
                WARN,
                "has_mag=false means firmware did not initialise the part at boot; "
                "it cannot distinguish an intentionally absent part from a failed one",
            )
            continue
        hz = counts[name] / el if el > 0 else 0.0
        want = expected.get(name)
        if want is None:
            verdict = OK if hz > 0 else FAIL
            rep.add(
                f"{i.serial}: {name} rate",
                verdict,
                f"{hz:.1f} Hz delivered; firmware config does not expose the applied {name} rate",
            )
            continue
        if want <= 0:
            continue
        ratio = hz / want
        verdict = OK if ratio >= RATE_WARN_BELOW else (WARN if ratio >= RATE_FAIL_BELOW else FAIL)
        detail = f"{hz:.1f} Hz delivered, {want:.0f} Hz expected ({ratio*100:.0f}%)"
        if verdict != OK:
            detail += ". A busy machine, a USB hub, or another program reading the port."
        rep.add(f"{i.serial}: {name} rate", verdict, detail)

    d = g.dropped
    wire = sum(d[k] for k in ("wire_tactile", "wire_imu", "wire_mag"))
    over = sum(d[k] for k in ("overflow_tactile", "overflow_imu", "overflow_mag"))
    rep.add(
        f"{i.serial}: wire loss {wire}",
        OK if wire == 0 else WARN,
        "" if wire == 0 else (
            "end-to-end sequence gaps; device queue drops and transport loss can both contribute"
        ),
    )
    if over:
        # Not a fault here: doctor drains everything, so this would be surprising.
        rep.add(
            f"{i.serial}: {over} samples discarded unread",
            WARN,
            "a queue overflowed. Expected only if a consumer is slower than the stream.",
        )
    anomalies = sum(d.get(f"{kind}_{name}", 0)
                    for kind in ("duplicate", "backward")
                    for name in ("tactile", "imu", "mag"))
    if anomalies:
        rep.add(
            f"{i.serial}: {anomalies} sequence anomalies",
            WARN,
            "duplicate or backward/reset sequence numbers were observed; they were not counted as packet loss",
        )
    transport_overflow = int(d.get("transport_overflow_ble", 0))
    if transport_overflow:
        rep.add(
            f"{i.serial}: {transport_overflow} BLE callback samples discarded",
            FAIL,
            "the bounded BLE receive queue overflowed before the SDK consumer drained it",
        )
    malformed_ble = int(d.get("transport_malformed_ble", 0))
    malformed_usb = int(d.get("transport_malformed_usb", 0))
    unrouted = int(d.get("unrouted_packets", 0))
    if malformed_ble or malformed_usb or unrouted:
        rep.add(
            f"{i.serial}: undecodable/unrouted packets "
            f"{malformed_ble + malformed_usb + unrouted}",
            FAIL,
            f"malformed BLE notifications {malformed_ble}, malformed USB TAG headers "
            f"{malformed_usb}, decoded packets with no route {unrouted}",
        )
    stale_imu = int(d.get("transport_stale_imu_ble", 0))
    if stale_imu:
        rep.add(
            f"{i.serial}: {stale_imu} stale BLE IMU samples",
            FAIL,
            "firmware saturated imu_dt_us, so tactile continued while the embedded IMU age was no longer valid",
        )
    if status_start is not None and status_end is not None:
        reset = status_end.uptime_ms < status_start.uptime_ms
        if reset:
            rep.add(f"{i.serial}: device reset during check", FAIL, "uptime moved backward")
        else:
            device_drop = max(0, status_end.tag_dropped - status_start.tag_dropped)
            short = max(0, status_end.tag_short_writes - status_start.tag_short_writes)
            deadlines = max(0, status_end.deadline_misses - status_start.deadline_misses)
            rep.add(
                f"{i.serial}: device drops {device_drop}",
                FAIL if short else (WARN if device_drop or deadlines else OK),
                f"short writes {short}, deadline misses {deadlines} during this check",
            )


def doctor(seconds: float = 3.0, *, connect: Optional[Callable] = None) -> Report:
    """Run every check and return the report. Printing is the caller's business."""
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, Real)
        or not math.isfinite(float(seconds))
        or float(seconds) <= 0
    ):
        raise ValueError("doctor seconds must be a finite real number greater than zero")
    rep = Report()
    _environment(rep)
    gloves = _ports(rep)
    if not gloves:
        return rep

    if connect is None:
        from . import connect as _c

        connect = _c

    for cand in gloves:
        g = None
        try:
            g = connect(port=cand.device)
            _glove(rep, g, seconds)
        except Exception as exc:
            rep.add(
                f"{cand.serial_number}: could not be opened",
                FAIL,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if g is not None:
                try:
                    g.close()
                except Exception:
                    pass
    return rep
