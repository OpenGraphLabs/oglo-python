"""`Glove`: the object a user holds.

Seven public members, and every one of them exists because leaving it out would make
the documentation contradict itself. The device commands are spelled exactly as the
firmware accepts them, which is less obvious than it sounds -- see `zero()`.
"""

from __future__ import annotations

import json
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from numbers import Real
from typing import Any, Dict, Iterator, Optional, Tuple

from ._config import Capabilities, Info
from ._frame import Frame, ImuSample, MagSample
from ._stream import Demux
from ._status import DeviceStatus

#: `SWEEP` with no argument is a DIFFERENT COMMAND. The firmware matches
#: `"DIAG SWEEP" || "SWEEP"` first and runs the settle-timing diagnostic; only
#: `SWEEP <n>` reaches the calibration handler. Always send the number.
_SWEEP_NEEDS_ARG = True

#: `SET IMURATE` takes a period in MILLISECONDS, not a rate in Hz.
_IMU_PERIOD_MIN_MS = 1
_IMU_PERIOD_MAX_MS = 100
_MAX_TEXT_BUFFER = 64 * 1024


class DeviceError(RuntimeError):
    pass


class CalibrationLocked(DeviceError):
    """`FACTORY LOCK` is set; the zero cannot be changed until `FACTORY UNLOCK`."""


def _validated_seconds(
    value: Optional[float],
    name: str,
    *,
    allow_none: bool,
    allow_zero: bool,
) -> Optional[float]:
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} cannot be None")
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite number of seconds")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if result < 0 or (result == 0 and not allow_zero):
        relation = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be {relation} seconds")
    return result


@dataclass(frozen=True)
class SampleBatch:
    """Everything currently ready from one glove, without resampling streams."""

    tactile: Tuple[Frame, ...] = ()
    imu: Tuple[ImuSample, ...] = ()
    mag: Tuple[MagSample, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tactile or self.imu or self.mag)

    def as_dict(self) -> Dict[str, list]:
        return {
            "tactile": list(self.tactile),
            "imu": list(self.imu),
            "mag": list(self.mag),
        }


class Glove:
    def __init__(self, transport: Any, info: Info, caps: Capabilities) -> None:
        self._t = transport
        self._info = info
        self._caps = caps
        self._demux = Demux(transport, stream_clean=info.stream_clean)
        self._started = False
        self._closed = False
        self._recording_lock = threading.Lock()
        self._recording_owner: Optional[int] = None
        #: Text replies carry across commands; see _await_line.
        self._textbuf = ""

    # -- identity ---------------------------------------------------------------

    @property
    def info(self) -> Info:
        return self._info

    def __repr__(self) -> str:
        return (
            f"<Glove {self._info.serial} {self._info.side} "
            f"fw={self._info.fw_rev} {self._info.rate_hz}Hz via {self._info.transport}>"
        )

    # -- streams ----------------------------------------------------------------

    def _ensure_started(self) -> None:
        self._ensure_recording_owner("read samples")
        if self._closed:
            raise DeviceError("this glove is closed; call oglo.connect() to open it again")
        if not self._started:
            self._demux.start_session(reset_clock=True)
            self._t.start()
            self._started = True

    def tactile(self, *, timeout: Optional[float] = None) -> Iterator[Frame]:
        timeout = _validated_seconds(timeout, "timeout", allow_none=True, allow_zero=True)
        self._ensure_started()
        return self._demux.iterate("tactile", timeout=timeout)

    def imu(self, *, timeout: Optional[float] = None) -> Iterator[ImuSample]:
        timeout = _validated_seconds(timeout, "timeout", allow_none=True, allow_zero=True)
        self._ensure_started()
        return self._demux.iterate("imu", timeout=timeout)

    def mag(self, *, timeout: Optional[float] = None) -> Iterator[MagSample]:
        timeout = _validated_seconds(timeout, "timeout", allow_none=True, allow_zero=True)
        if not self._info.has_mag:
            raise DeviceError(
                f"{self._info.serial} reports no magnetometer (has_mag=false). "
                "The slot exists on the wire and is zero-filled; there is nothing to read."
            )
        self._ensure_started()
        return self._demux.iterate("mag", timeout=timeout)

    def _drain_ready(self) -> Dict[str, list]:
        """Compatibility wrapper for old callers. Prefer :meth:`read_batch`."""
        return self.read_batch().as_dict()

    def read_batch(self, *, timeout: Optional[float] = 0.0) -> SampleBatch:
        """Return all currently ready tactile/IMU/mag samples in one call.

        No stream is resampled to another. ``timeout=0`` polls once, ``None`` waits
        indefinitely, and a positive timeout waits that many seconds for any sample.
        """
        timeout = _validated_seconds(timeout, "timeout", allow_none=True, allow_zero=True)
        self._ensure_started()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            ready = self._demux.drain_ready()
            batch = SampleBatch(
                tactile=tuple(ready["tactile"]),
                imu=tuple(ready["imu"]),
                mag=tuple(ready["mag"]),
            )
            if batch or timeout == 0:
                return batch
            if deadline is not None and time.monotonic() >= deadline:
                return batch
            time.sleep(0.0005)

    def start(self) -> None:
        """Start acquisition explicitly. Iterators and ``read_batch`` also do this."""
        self._ensure_started()

    def stop(self) -> None:
        """Stop acquisition while keeping the device connection open."""
        self._ensure_not_recording("stop streaming")
        if self._started:
            self._t.stop()
            self._started = False
            self._t.drain()
            self._demux.start_session(reset_clock=True)

    @property
    def rates_seen(self) -> Dict[str, float]:
        """What this machine is actually receiving, per stream, over ~2 s."""
        return {
            "tactile": self._demux.tactile.rate.hz,
            "imu": self._demux.imu.rate.hz,
            "mag": self._demux.mag.rate.hz,
        }

    @property
    def dropped(self) -> Dict[str, int]:
        """Loss, kept apart by cause. Merging these hides which side failed."""
        wire = getattr(self._t, "dropped", None)
        out = {
            "wire_tactile": getattr(wire, "tactile", 0),
            "wire_imu": getattr(wire, "imu", 0),
            "wire_mag": getattr(wire, "mag", 0),
            "overflow_tactile": self._demux.tactile.overflowed,
            "overflow_imu": self._demux.imu.overflowed,
            "overflow_mag": self._demux.mag.overflowed,
        }
        for kind in ("duplicate", "backward"):
            for name in ("tactile", "imu", "mag"):
                out[f"{kind}_{name}"] = getattr(wire, f"{kind}_{name}", 0)
        out["transport_overflow_ble"] = int(getattr(self._t, "notification_overflow", 0))
        out["transport_malformed_ble"] = int(getattr(self._t, "malformed", 0))
        out["transport_stale_imu_ble"] = int(getattr(self._t, "stale_imu", 0))
        out["transport_malformed_usb"] = int(getattr(wire, "malformed_usb", 0))
        out["unrouted_packets"] = int(self._demux.unrouted)
        return out

    def status(self) -> DeviceStatus:
        """Read live sensor health and device-side loss counters.

        This is a snapshot, not a capture-window delta. ``record()`` stores both
        start and end snapshots and computes the delta when both are available.
        """
        self._ensure_recording_owner("read status")
        with self._paused():
            return replace(self._t.read_status(), mag_required=self._info.has_mag)

    def _begin_recording(self) -> None:
        """Give one thread exclusive ownership of sample draining for an episode."""
        with self._recording_lock:
            if self._closed:
                raise DeviceError("cannot record from a closed glove")
            if self._recording_owner is not None:
                raise DeviceError("this glove is already being recorded")
            self._recording_owner = threading.get_ident()

    def _end_recording(self) -> None:
        with self._recording_lock:
            if self._recording_owner == threading.get_ident():
                self._recording_owner = None

    def _ensure_recording_owner(self, action: str) -> None:
        owner = self._recording_owner
        if owner is not None and owner != threading.get_ident():
            raise DeviceError(
                f"cannot {action} from another thread while record() owns this glove"
            )

    def _ensure_not_recording(self, action: str) -> None:
        if self._recording_owner is not None:
            raise DeviceError(
                f"cannot {action} while record() is capturing; finish or cancel the episode first"
            )

    # -- pausing around text commands -------------------------------------------

    @contextmanager
    def _paused(self, *, resume: bool = True):
        """Stop streaming for the duration of a text command, then resume.

        Every command that expects a reply needs this. The board answers in ASCII on
        the same wire it is pushing ~48 kB/s of binary down, so the reply lands
        *behind* whatever was already in flight and a reader looking for a line has to
        chew through binary to reach it. It also keeps ASCII out of the user's data
        stream, which a frame parser would otherwise have to resynchronise past.
        ``record()`` also uses ``resume=False`` while it publishes the final files;
        otherwise a slow compression/fsync phase builds a queue of stale samples.
        """
        if self._closed:
            raise DeviceError("this glove is closed; call oglo.connect() to open it again")
        was = self._started
        if was:
            self._t.stop()
            self._started = False
        self._t.drain(settle=0.2 if was else 0.0)
        # Anything queued before this point was produced under the old stream or
        # calibration state. Never return it after the command with new semantics.
        self._demux.discard_pending()
        self._textbuf = ""
        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            if was and resume:
                try:
                    self._t.start(reset_counters=False)
                    self._demux.reset_sequence()
                    self._started = True
                except Exception:
                    self._started = False
                    # Do not replace a more useful command/readback exception with a
                    # secondary resume failure. A successful command, however, must
                    # not return success if streaming could not be restored.
                    if not body_failed:
                        raise

    # -- calibration ------------------------------------------------------------

    def zero(self, sweep: int = 5, *, timeout: float = 40.0) -> Dict[str, Any]:
        """Capture the per-taxel zero while the hand opens and closes.

        **The sweep is the calibration, not an option on it.** Bending a finger
        compresses the sensor by itself, so a baseline taken with a still hand is only
        valid for a still hand: make a fist afterwards and every taxel reads high,
        putting a grip in the data that never happened. The sweep records the envelope
        over the whole range of motion instead.

        Streaming is stopped for the duration. The board prints the resulting recipe
        as `#TZERO {...}`, roughly a kilobyte of ASCII, and injecting that into a
        binary stream would corrupt frames a parser then has to resynchronise past.
        """
        self._ensure_not_recording("calibrate zero")
        if isinstance(sweep, bool) or not isinstance(sweep, int):
            raise TypeError("sweep must be an integer number of seconds")
        if not 1 <= sweep <= 30:
            raise ValueError(f"sweep must be 1..30 seconds (the firmware clamps there), got {sweep}")
        timeout = _validated_seconds(timeout, "timeout", allow_none=False, allow_zero=False)
        if not getattr(self._t, "replies_in_text", True):
            raise DeviceError(
                "sweep zero requires USB in SDK 1.0: firmware 0.9.9 sends its "
                "completion recipe only on the USB text channel, so BLE cannot prove "
                "that capture and persistence finished"
            )

        with self._paused():
            with self._refresh_after_mutation():
                # `SWEEP` alone runs the settle diagnostic. The number is not optional.
                self._command(f"SWEEP {sweep}", expect="#SWEEP started", timeout=3.0)
                completed_line = self._await_line("#TZERO ", timeout=max(timeout, sweep + 10))
                completed = _parse_zero_recipe(completed_line)

                # The completion line may have been generated from RAM. Ask again so a
                # dropped/truncated line or stale buffered response cannot count as success.
                self._textbuf = ""
                readback_line = self._command("GET ZERO", expect="#TZERO ", timeout=4.0)
                readback = _parse_zero_recipe(readback_line)
                keys = ("valid", "count", "frames", "thr", "clean", "locked", "baseline", "noise")
                if any(completed[k] != readback[k] for k in keys):
                    raise DeviceError("zero completed, but GET ZERO did not reproduce the same recipe")
            if not self._info.zero_valid:
                raise DeviceError("GET ZERO was valid, but GET CONFIG still reports zero_valid=false")
        readback["raw"] = readback_line
        return readback

    def clean(self, threshold: int = 0) -> None:
        """Apply the zero and a deadband on the device, so every client agrees.

        The deadband is a **cutoff, not a subtraction**: a value at `thr+1` is
        reported as `thr+1`, not as `1`. `0.9.7` briefly subtracted it and was
        reverted the same day, so a host that compensates for a subtraction is wrong
        on every build that shipped.
        """
        self._ensure_not_recording("change clean-stream settings")
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise TypeError("threshold must be an integer ADC count")
        if not 0 <= threshold <= 4095:
            raise ValueError("threshold must be 0..4095 ADC counts")
        if not self._info.zero_valid:
            raise DeviceError(
                "this board has no zero yet (zero_valid=false), so a clean stream "
                "would subtract nothing. Call glove.zero(sweep=5) first."
            )
        with self._paused():
            with self._refresh_after_mutation():
                self._command(f"SET THR {threshold}", expect="#THR",
                              confirm=lambda i: i.stream_thr == threshold, timeout=3.0)
                self._command("SET STREAM CLEAN", expect="#STREAM clean",
                              confirm=lambda i: i.stream_clean, timeout=3.0)
            if self._info.stream_thr != threshold or not self._info.stream_clean:
                raise DeviceError(
                    "device readback does not match requested clean state: "
                    f"threshold={self._info.stream_thr}, clean={self._info.stream_clean}"
                )
        self._demux.set_clean(True)

    def raw(self) -> None:
        """Stop applying the device-side zero. The counts become raw ADC again."""
        self._ensure_not_recording("change to the raw stream")
        with self._paused():
            with self._refresh_after_mutation():
                self._command("SET STREAM RAW", expect="#STREAM",
                              confirm=lambda i: not i.stream_clean, timeout=3.0)
            if self._info.stream_clean:
                raise DeviceError("device readback still reports stream_clean=true after SET STREAM RAW")
        self._demux.set_clean(False)

    # -- rates ------------------------------------------------------------------

    def rates(self, *, tactile: Optional[int] = None, imu: Optional[int] = None,
              mag: Optional[int] = None) -> Dict[str, Any]:
        """Change what the device produces.

        `mag` is **not settable**. The firmware targets its own roughly 125 Hz cadence
        and expresses that schedule in IMU loop cycles, so it is neither an independent
        command nor reliably one quarter of a custom IMU rate.
        Accepting the argument and ignoring it would be worse than refusing.
        """
        self._ensure_not_recording("change sensor rates")
        if mag is not None:
            raise DeviceError(
                "the magnetometer rate is not independently settable: the firmware "
                "schedules it around a fixed ~125 Hz target. Changing imu= does not "
                "define a requested magnetometer rate."
            )
        if tactile is not None:
            if isinstance(tactile, bool) or not isinstance(tactile, int):
                raise TypeError("tactile rate must be an integer Hz value")
            if not 1 <= tactile <= 1000:
                raise ValueError("tactile rate must be 1..1000 Hz")
        period_ms = self._imu_period_ms(imu) if imu is not None else None
        if period_ms is not None and not getattr(self._t, "replies_in_text", True):
            raise DeviceError(
                "the applied IMU period is not exposed in BLE config, so SDK 1.0 "
                "cannot verify rates(imu=...) over BLE; use USB"
            )

        out: Dict[str, Any] = {}
        try:
            with self._paused():
                with self._refresh_after_mutation():
                    if tactile is not None:
                        out["tactile"] = self._command(f"SET RATE {tactile}", expect="#SCAN",
                                                       confirm=lambda i: i.rate_hz == tactile, timeout=3.0)
                    if period_ms is not None:
                        out["imu"] = self._command(
                            f"SET IMURATE {period_ms}", expect="#IMURATE", timeout=2.0
                        )
                        if out["imu"] != f"#IMURATE period_ms={period_ms}":
                            raise DeviceError(
                                "firmware applied a different IMU period than requested: "
                                f"requested {period_ms} ms, reply was {out['imu']!r}"
                            )
                        out["imu_actual_hz"] = 1000.0 / period_ms
                if tactile is not None and self._info.rate_hz != tactile:
                    raise DeviceError(
                        "device readback does not match requested tactile rate: "
                        f"requested {tactile}, got {self._info.rate_hz}"
                    )
                if period_ms is not None:
                    self._info = replace(self._info, imu_period_ms=period_ms)
        except Exception:
            if period_ms is not None:
                # The command may have partially applied, but 0.9.9 has no read-only
                # IMU-period field. Unknown is safer than stale metadata.
                self._info = replace(self._info, imu_period_ms=None)
            raise
        return out

    @staticmethod
    def _imu_period_ms(hz: int) -> int:
        """Hz -> whole milliseconds, refusing what the device cannot represent.

        The period is an integer millisecond count, so the reachable rates are
        1000/n: 1000, 500, 333.3, 250... Rounding 400 Hz silently to 333 would put a
        number in a dataset that the user never chose.
        """
        if isinstance(hz, bool) or not isinstance(hz, int):
            raise TypeError("imu rate must be an integer Hz value")
        if hz <= 0:
            raise ValueError("imu rate must be positive")
        if hz < 10 or hz > 1000:
            raise ValueError(f"imu rate {hz} Hz is outside firmware's 10..1000 Hz range")
        period = min(
            range(_IMU_PERIOD_MIN_MS, _IMU_PERIOD_MAX_MS + 1),
            key=lambda p: abs(1000.0 / p - hz),
        )
        actual = 1000.0 / period
        if abs(actual - hz) > 0.5:
            raise ValueError(
                f"the device sets the IMU by whole-millisecond period, so {hz} Hz is "
                f"not reachable; the nearest is {actual:.1f} Hz (period {period} ms). "
                f"Ask for that explicitly if you want it."
            )
        return period

    # -- escape hatch -----------------------------------------------------------

    def send(self, command: str, *, expect: Optional[str] = None, timeout: float = 2.0) -> str:
        """Send a raw firmware command. Everything not covered above lives here.

        `SET SIDE`, `SET SERIAL`, `FACTORY LOCK`, the `DIAG` family: deliberately not
        wrapped, because each would be an API surface that has to be kept correct
        forever for a thing a handful of people run once.
        """
        self._ensure_not_recording("send a firmware command")
        timeout = _validated_seconds(timeout, "timeout", allow_none=False, allow_zero=False)
        if command.strip().upper().startswith("SET IMURATE "):
            raise DeviceError(
                "use rates(imu=...) so the exact firmware ACK and recording metadata are verified"
            )
        if not getattr(self._t, "replies_in_text", True):
            raise DeviceError(
                "raw send() requires USB in SDK 1.0 because firmware has no command "
                "reply channel over BLE; use a typed high-level method whose state can be verified"
            )
        with self._paused():
            with self._refresh_after_mutation():
                # Every USB command emits a # line. Waiting for at least one by
                # default is what makes an unknown command surface its #ERR instead
                # of being erased when streaming resumes.
                reply = self._command(command, expect=expect or "#", timeout=timeout)
            return reply

    @contextmanager
    def _refresh_after_mutation(self):
        """Keep public state truthful even when a multi-command operation fails."""
        try:
            yield
        except Exception as operation_error:
            try:
                self._refresh_info(strict=True)
                self._demux.set_clean(self._info.stream_clean)
            except Exception as refresh_error:
                raise DeviceError(
                    f"device operation failed ({operation_error}); its partially applied "
                    f"state could not be read back ({refresh_error})"
                ) from operation_error
            raise
        except BaseException:
            # Preserve KeyboardInterrupt/SystemExit, but still make a best effort to
            # avoid stale stream semantics before unwinding.
            try:
                self._refresh_info(strict=True)
                self._demux.set_clean(self._info.stream_clean)
            except Exception:
                pass
            raise
        else:
            self._refresh_info(strict=True)
            self._demux.set_clean(self._info.stream_clean)

    # -- plumbing ---------------------------------------------------------------

    def _command(self, command: str, *, expect: Optional[str] = None,
                 confirm: Optional[Any] = None, timeout: float = 2.0) -> str:
        """Send, then confirm however this transport allows.

        USB echoes replies as text and we wait for the line. BLE has **no reply
        channel at all** -- the firmware writes command output to Serial only -- so
        there `confirm` re-reads the config and waits for the state to actually
        change. A command that cannot be confirmed either way returns immediately,
        and the docstring of whatever called it should say so.
        """
        self._t.send(command)
        if getattr(self._t, "replies_in_text", True):
            if expect is None:
                return ""
            return self._await_line(expect, timeout=timeout, command=command)
        if confirm is None:
            return ""
        return self._confirm_via_config(confirm, timeout=timeout, command=command)

    def _confirm_via_config(self, predicate: Any, *, timeout: float, command: str) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._refresh_info()
            if predicate(self._info):
                return "confirmed via config"
            time.sleep(0.15)
        raise DeviceError(
            f"{command!r} was sent but the device never reported the change. "
            "Over BLE there is no reply channel, so this is confirmed by re-reading "
            "the config; the command may still have been applied."
        )

    def _await_line(self, prefix: str, *, timeout: float, command: str = "") -> str:
        """Wait for a reply line, consuming the text buffer one line at a time.

        The buffer persists across calls. A serial read returns whatever was ready,
        so the reply to one command and the start of the next can arrive together;
        an implementation that scans a fresh buffer per call throws the second one
        away and then times out waiting for what it already had. That is the same
        carry-the-remainder rule the frame decoders follow.
        """
        deadline = time.monotonic() + timeout
        while True:
            while "\n" in self._textbuf:
                line, self._textbuf = self._textbuf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#ERR"):
                    if "locked" in line.lower():
                        raise CalibrationLocked(
                            "this board is factory locked; run FACTORY UNLOCK first"
                        )
                    raise DeviceError(f"{command or prefix}: board replied {line}")
                if line.startswith(prefix):
                    return line
            if time.monotonic() >= deadline:
                break
            text = self._t.read_text()
            if text:
                self._textbuf += text
                if len(self._textbuf) > _MAX_TEXT_BUFFER:
                    self._textbuf = ""
                    raise DeviceError(
                        f"text reply exceeded {_MAX_TEXT_BUFFER} bytes without a complete expected line"
                    )
            else:
                time.sleep(0.005)
        raise DeviceError(
            f"no {prefix!r} from the board within {timeout:.0f}s"
            + (f" after {command!r}" if command else "")
        )

    def _refresh_info(self, *, strict: bool = False) -> bool:
        try:
            known_imu_period = self._info.imu_period_ms
            info, self._caps = self._t.read_config(timeout=3.0, interval=0.3, drain=0.1)
            if known_imu_period is not None and info.imu_period_ms is None:
                info = replace(info, imu_period_ms=known_imu_period)
            self._info = info
            return True
        except Exception as exc:
            if strict:
                raise DeviceError(f"could not verify the device state after the command: {exc}") from exc
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._t.close()
        finally:
            self._started = False

    def __enter__(self) -> "Glove":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _parse_zero_recipe(line: str) -> Dict[str, Any]:
    """Parse and validate the complete 80-taxel recipe printed by firmware 0.9.9."""
    prefix = "#TZERO "
    if not line.startswith(prefix):
        raise DeviceError(f"malformed zero reply: {line[:80]!r}")
    try:
        raw = json.loads(line[len(prefix):])
    except (TypeError, ValueError) as exc:
        raise DeviceError("#TZERO did not contain valid JSON") from exc
    if not isinstance(raw, dict):
        raise DeviceError("#TZERO JSON must be an object")

    valid_value = raw.get("valid")
    if valid_value is not True:
        raise DeviceError("#TZERO reports valid=false")
    count = raw.get("count")
    if type(count) is not int or count != 80:
        raise DeviceError(f"#TZERO count must be 80, got {count!r}")

    def values(name: str) -> list:
        item = raw.get(name)
        if not isinstance(item, list) or len(item) != 80:
            got = len(item) if isinstance(item, list) else type(item).__name__
            raise DeviceError(f"#TZERO {name} must contain 80 values, got {got}")
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 4095 for v in item):
            raise DeviceError(f"#TZERO {name} values must be integer ADC counts 0..4095")
        return list(item)

    def integer(name: str) -> int:
        if name not in raw:
            raise DeviceError(f"#TZERO is missing {name}")
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise DeviceError(f"#TZERO {name} must be an integer")
        return value

    def boolean(name: str) -> bool:
        if name not in raw:
            raise DeviceError(f"#TZERO is missing {name}")
        value = raw[name]
        if value is True or value is False:
            return value
        raise DeviceError(f"#TZERO {name} must be boolean")

    normalized = dict(raw)
    normalized.update(
        valid=True,
        count=80,
        frames=integer("frames"),
        thr=integer("thr"),
        clean=boolean("clean"),
        locked=boolean("locked"),
        baseline=values("baseline"),
        noise=values("noise"),
    )
    if not 0 <= normalized["thr"] <= 4095:
        raise DeviceError("#TZERO thr must be 0..4095")
    if not 0 <= normalized["frames"] <= 512:
        raise DeviceError("#TZERO frames must be 0..512")
    return normalized
