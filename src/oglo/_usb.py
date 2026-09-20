"""USB transport: discovery, handshake, and the read loop.

Discovery reads USB descriptors without opening ports. The transport accepts an
open serial-like object so the read path can also run against a test double.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Protocol, Tuple

from . import _wire as w
from ._config import Capabilities, Info, _fw_at_least, parse_config
from ._status import DeviceStatus, parse_status

#: Supported firmware 0.9.10+ uses TinyUSB on the Seeed XIAO module with
#: ``OGLO`` / ``OpenGraphLabs`` descriptors. Discovery keys on the stable VID and
#: proves the actual device and firmware contract with strict GET CONFIG later.
SEEED_VID = 0x2886
GLOVE_VIDS = frozenset({SEEED_VID})

#: Ports that are definitely not a glove, so `list_candidates()` can say so instead of
#: leaving a label printer in the list for a user to pick.
_NOT_A_GLOVE_VIDS = {0x3513}  # NIIMBOT

# A previous process may have died in any mode (the browser viewer still uses BIN).
# Stop all producers before asking for text; this is deliberately idempotent.
_HANDSHAKE_STOP = "STREAM BIN OFF\nSTREAM TAXEL OFF\nSTREAM TAG OFF"
_CONFIG_PREFIX = "#CONFIG "
_STATUS_PREFIX = "#STATUS "

# LINK PING is intentionally capability- and version-gated. Sending it to older
# firmware injects an ``#ERR unknown command`` line into a live binary TAG stream.
_LINK_PING_MIN_FIRMWARE = (0, 9, 16)
# One second leaves a full second of scheduling margin under the <=2 s host contract.
_LINK_PING_INTERVAL_S = 1.0
_LINK_PING_JOIN_TIMEOUT_S = 0.75


class SerialLike(Protocol):
    """The slice of pyserial this module uses. A fake only has to provide this."""

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> Optional[int]: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


class UsbError(RuntimeError):
    pass


class NoGloveFound(UsbError):
    """No glove was discovered; only this USB error allows automatic BLE fallback."""


class DisconnectedError(UsbError):
    """The glove disconnected during a session."""


class PortBusyError(UsbError):
    """Another process owns the USB port."""


@dataclass(frozen=True)
class PortCandidate:
    device: str
    serial_number: Optional[str]
    vid: Optional[int]
    pid: Optional[int]
    product: Optional[str]
    manufacturer: Optional[str]

    @property
    def looks_like_glove(self) -> bool:
        if self.vid in _NOT_A_GLOVE_VIDS:
            return False
        return self.vid in GLOVE_VIDS


def list_candidates(*, strict: bool = True) -> List[PortCandidate]:
    """Serial ports that could be a glove. **Opens nothing.**

    With `strict` (the default) only the supported OGLO Seeed VID is returned. With
    `strict=False` anything not on the known-not-a-glove list is returned, which is
    the escape hatch for a board that enumerates under a VID we have not seen.
    """
    from serial.tools import list_ports  # imported here so `import oglo` stays cheap

    out: List[PortCandidate] = []
    for p in list_ports.comports():
        cand = PortCandidate(
            device=p.device,
            serial_number=p.serial_number,
            vid=p.vid,
            pid=p.pid,
            product=p.product,
            manufacturer=p.manufacturer,
        )
        if cand.vid in _NOT_A_GLOVE_VIDS:
            continue
        if strict and not cand.looks_like_glove:
            continue
        if not strict and cand.vid is None:
            continue  # a virtual port (Bluetooth-Incoming, debug-console) is not a device
        out.append(cand)
    return sorted(out, key=lambda c: c.device)


def list_all_ports() -> List[PortCandidate]:
    """Every serial port with a USB descriptor, filtered by nothing.

    `list_candidates()` hides what is definitely not a glove, which is right for
    connecting and wrong for diagnosing: when someone cannot find their glove, "I saw
    a label printer at that path and skipped it" is the useful sentence.
    """
    from serial.tools import list_ports

    return sorted(
        (
            PortCandidate(
                device=p.device, serial_number=p.serial_number, vid=p.vid, pid=p.pid,
                product=p.product, manufacturer=p.manufacturer,
            )
            for p in list_ports.comports()
            if p.vid is not None
        ),
        key=lambda c: c.device,
    )


def find_port(serial_number: Optional[str] = None, *, strict: bool = True) -> PortCandidate:
    """Pick a port by USB serial number, never by path.

    A user should never type `/dev/cu.usbmodem1101`: it is not stable across reboots
    and it says nothing about which hand it is.
    """
    cands = list_candidates(strict=strict)
    if serial_number:
        for c in cands:
            if c.serial_number and serial_number.lower() in c.serial_number.lower():
                return c
        seen = [c.serial_number for c in cands]
        raise NoGloveFound(f"no glove with serial {serial_number!r}; visible: {seen}")
    if not cands:
        hint = "" if strict else " (even with strict=False)"
        raise NoGloveFound(
            f"no glove found{hint}. Is it plugged in and running OGLO firmware? "
            f"Pass a port explicitly to bypass discovery."
        )
    if len(cands) > 1:
        raise UsbError(
            "more than one glove is attached; pass serial= to choose: "
            + ", ".join(f"{c.serial_number}@{c.device}" for c in cands)
        )
    return cands[0]


def _owner_pid(device: str) -> Optional[int]:
    """Best-effort: which process holds this port. Used only to improve an error."""
    try:
        out = subprocess.run(
            ["lsof", "-t", device], capture_output=True, text=True, timeout=3
        ).stdout.split()
        return int(out[0]) if out else None
    except Exception:
        return None


def open_serial(device: str, baud: int = 115200, *, settle: float = 0.8) -> SerialLike:
    """Open with DTR asserted and RTS low.

    **DTR must be high or supported firmware says nothing at all.** TinyUSB gates CDC
    transmit on the host asserting DTR. Measured on OGLO-R-TEST04: dtr=False returns
    0 bytes to `GET CONFIG`, dtr=True returns 561.

    Asserting it is safe. The auto-reset circuit that rule was written for belongs to
    the UART bridge, not to native USB; verified by reading `uptime_ms` across a
    reopen (127174 -> 131043 ms, still counting). **RTS stays low**, because the two
    together are what a bridge decodes as a reset request.
    """
    import serial as pyserial

    s = pyserial.Serial()
    s.port = device
    s.baudrate = baud
    s.timeout = 0.05
    # A keepalive runs independently of the reader. Bound its write so a dead OUT
    # endpoint cannot strand that worker forever or make stop()/close() hang on it.
    s.write_timeout = 0.5
    s.dtr = True
    s.rts = False
    try:
        s.open()
    except BaseException as exc:
        try:
            s.close()
        except BaseException:
            pass
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        pid = _owner_pid(device)
        if pid is not None:
            raise PortBusyError(
                f"{device} is already held by PID {pid}. A USB glove has exactly one "
                f"owner: close the other program (a viewer, a notebook, a stale "
                f"session) and retry."
            ) from exc
        raise UsbError(f"could not open {device}: {exc}") from exc
    time.sleep(settle)
    return s


# --- the transport -------------------------------------------------------------


@dataclass
class StreamCounters:
    """Host-observed loss, per stream. Never merged with what the device dropped."""

    tactile: int = 0
    imu: int = 0
    mag: int = 0
    duplicate_tactile: int = 0
    duplicate_imu: int = 0
    duplicate_mag: int = 0
    backward_tactile: int = 0
    backward_imu: int = 0
    backward_mag: int = 0
    malformed_usb: int = 0


class UsbTransport:
    """Handshake, stream selection and the read loop, over any serial-like object."""

    def __init__(self, serial_like: SerialLike, *, owns_port: bool = True) -> None:
        self._s = serial_like
        self._owns = owns_port
        self._buf = b""
        self._caps: Optional[Capabilities] = None
        self._info: Optional[Info] = None
        self._streaming = False
        # Command writes and the background LINK PING share one CDC OUT endpoint.
        # One lock keeps complete newline-delimited commands from interleaving.
        self._write_lock = threading.Lock()
        self._keepalive_lock = threading.Lock()
        self._keepalive_stop: Optional[threading.Event] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_error: Optional[DisconnectedError] = None
        self._last_seq: Dict[int, Optional[int]] = {
            w.TAG_TACTILE: None, w.TAG_IMU: None, w.TAG_MAG: None
        }
        self.dropped = StreamCounters()

    # -- commands ---------------------------------------------------------------

    def _write_command(
        self, command: str, *,
        keepalive_stop: Optional[threading.Event] = None,
    ) -> None:
        payload = (command.rstrip("\n") + "\n").encode()
        with self._write_lock:
            # A command may have queued behind a ping while that write failed.
            # Check and publish failure under this same lock, before another
            # writer can append to a possibly partial firmware command line.
            self._raise_link_keepalive_error()
            try:
                written = self._s.write(payload)
                if written is not None and written != len(payload):
                    raise OSError(f"short USB write: {written}/{len(payload)} bytes")
                # Serial.write() already queues the complete command in order.
                # flush()/tcdrain is not bounded by write_timeout on macOS and
                # can hang even after write() succeeded on a failed endpoint.
                # Command replies provide acknowledgement; never wait for an
                # unbounded OS drain during handshake, commands, or close.
            except Exception as exc:
                error = DisconnectedError(
                    f"could not send {command!r}: the glove is no longer reachable. "
                    f"{type(exc).__name__}: {exc}"
                )
                with self._keepalive_lock:
                    if keepalive_stop is None or self._keepalive_stop is keepalive_stop:
                        self._keepalive_error = error
                raise error from exc

    def send(self, command: str) -> None:
        # A timed-out/short ping may have left a partial command in the firmware's
        # line buffer. Appending another command could turn both into one valid-looking
        # but wrong line; end this USB epoch and reconnect instead.
        self._raise_link_keepalive_error()
        if command.strip().upper().startswith("FW ") and self._supports_link_ping():
            raise UsbError(
                "firmware update commands require the dedicated updater on its own USB "
                "session; this transport's LINK PING worker must not enter the raw "
                "firmware-image byte stream"
            )
        self._write_command(command)

    def _supports_link_ping(self) -> bool:
        """True only for the explicit, version-pinned reply-free command contract."""
        return bool(
            self._info is not None
            and self._caps is not None
            and self._caps.link_ping
            # If another object owns the file descriptor, close() cannot lower DTR
            # and end the firmware's sticky USB-epoch authorization. Never opt that
            # externally-owned session into a promise this transport cannot keep.
            and self._owns
            and _fw_at_least(self._info.fw_rev, _LINK_PING_MIN_FIRMWARE)
        )

    def _start_link_keepalive(self) -> None:
        if not self._supports_link_ping():
            return
        stop = threading.Event()

        def run() -> None:
            # Opt in immediately, then refresh at a fixed cadence independent of
            # poll(). A slow reader is the exact case this worker exists to cover.
            deadline = time.monotonic()
            while not stop.is_set():
                delay = max(0.0, deadline - time.monotonic())
                if stop.wait(delay):
                    return
                try:
                    self._write_command("LINK PING", keepalive_stop=stop)
                except DisconnectedError:
                    return
                deadline += _LINK_PING_INTERVAL_S
                # Do not send a burst after a process-wide scheduling pause. One
                # fresh ping is useful; replaying every missed deadline is not.
                now = time.monotonic()
                if deadline <= now:
                    deadline = now + _LINK_PING_INTERVAL_S

        with self._keepalive_lock:
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return
            self._keepalive_error = None
            self._keepalive_stop = stop
            thread = threading.Thread(
                target=run,
                name=f"oglo-link-ping-{self._info.serial}",
                daemon=True,
            )
            self._keepalive_thread = thread
        thread.start()

    def _stop_link_keepalive(self) -> bool:
        with self._keepalive_lock:
            stop = self._keepalive_stop
            thread = self._keepalive_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_LINK_PING_JOIN_TIMEOUT_S)
        with self._keepalive_lock:
            if self._keepalive_stop is stop:
                self._keepalive_stop = None
                self._keepalive_thread = None
        return thread is None or not thread.is_alive()

    def _raise_link_keepalive_error(self) -> None:
        with self._keepalive_lock:
            error = self._keepalive_error
        if error is not None:
            raise error

    #: This transport echoes command replies as text lines, so `Glove` can wait for
    #: one. BLE cannot: the firmware writes replies to Serial only, so a BLE transport
    #: sets this False and confirmation goes through the config instead.
    replies_in_text = True

    def read_text(self, size: int = 8192) -> str:
        """Whatever text is waiting. Used only while no binary stream is running."""
        chunk = self._read(size)
        return chunk.decode("utf8", "replace") if chunk else ""

    def _read(self, size: int) -> bytes:
        """One read, with a disconnect turned into something actionable."""
        try:
            return self._s.read(size)
        except Exception as exc:
            raise DisconnectedError(
                "the glove stopped responding mid-session. Usually the USB cable "
                "came out or a hub reset. Reconnect and call oglo.connect() again; "
                "anything already recorded is unaffected."
            ) from exc

    def read_config(
        self, timeout: float = 6.0, interval: float = 0.5, *, drain: float = 0.4
    ) -> Tuple[Info, Capabilities]:
        """Stop whatever is streaming, then ask until the board answers.

        The retry is not defensive padding: a board that was mid-stream needs its
        output drained before a text reply is findable, and one that just enumerated
        may not have run `setup()` yet.
        """
        self.send(_HANDSHAKE_STOP)
        self._streaming = False
        if drain:
            time.sleep(drain)  # let a stopped stream finish draining before we read text
        self._s.reset_input_buffer()
        self._buf = b""

        deadline = time.monotonic() + timeout
        text = b""
        while time.monotonic() < deadline:
            self.send("GET CONFIG")
            end = time.monotonic() + interval
            while time.monotonic() < end:
                chunk = self._s.read(8192)
                if chunk:
                    text += chunk
                cfg = _find_config(text)
                if cfg is not None:
                    self._info, self._caps = parse_config(cfg, transport="usb")
                    return self._info, self._caps
            text = text[-16384:]  # bound the buffer if the board is spewing
        raise UsbError(
            "no #CONFIG from the board within "
            f"{timeout:.0f}s. Wrong port, or firmware that does not answer GET CONFIG."
        )

    def read_status(self, timeout: float = 3.0) -> DeviceStatus:
        """Read one runtime health snapshot while binary streaming is paused."""
        self.send("GET STATUS")
        deadline = time.monotonic() + timeout
        text = b""
        while time.monotonic() < deadline:
            chunk = self._read(8192)
            if chunk:
                text += chunk
            raw = _find_prefixed_json(text, _STATUS_PREFIX)
            if raw is not None:
                return parse_status(raw)
            text = text[-16384:]
        raise UsbError(f"no #STATUS from the board within {timeout:.0f}s")

    # -- streaming --------------------------------------------------------------

    @property
    def info(self) -> Info:
        if self._info is None:
            raise UsbError("read_config() first")
        return self._info

    @property
    def caps(self) -> Capabilities:
        if self._caps is None:
            raise UsbError("read_config() first")
        return self._caps

    def start(self, *, reset_counters: bool = True) -> str:
        """Begin the supported firmware-0.9.10+ tagged stream."""
        self._buf = b""
        self._last_seq = {k: None for k in self._last_seq}
        if reset_counters:
            self.dropped = StreamCounters()
        self._s.reset_input_buffer()
        self.send("STREAM TAG ON")
        self._streaming = True
        self._start_link_keepalive()
        return "tagged"

    def stop(self) -> None:
        self.send("STREAM TAG OFF")
        self._streaming = False

    def drain(self, settle: float = 0.2) -> None:
        """Throw away whatever is still in flight after stopping a stream.

        At the shipping rates a glove produces ~48 kB/s, so the moment `stop()` is
        sent there is already a backlog on the wire. A text command issued straight
        afterwards has its reply buried under that backlog, and a reader looking for
        an ASCII line has to chew through binary to reach it. Drop it instead.
        """
        if settle:
            time.sleep(settle)
        try:
            self._s.reset_input_buffer()
        except Exception:
            pass
        self._buf = b""

    def poll(self, size: int = 8192) -> List[Any]:
        """Read what is available and decode it.

        The undecoded tail is carried to the next call. A caller that drops it will
        desync, which is why the buffer lives here and not in the caller.
        """
        self._raise_link_keepalive_error()
        chunk = self._read(size)
        self._raise_link_keepalive_error()
        received_ns = time.monotonic_ns() if chunk else None
        if chunk:
            self._buf += chunk
        if not self._buf:
            return []
        packets, self._buf, malformed = w.iter_tagged_diagnostic(self._buf)
        self.dropped.malformed_usb += malformed
        if received_ns is not None:
            packets = [replace(p, host_received_ns=received_ns) for p in packets]
        for p in packets:
            self._account(p)
        return packets

    def _account(self, packet: Any) -> None:
        kind = {
            w.TactilePacket: (w.TAG_TACTILE, "tactile"),
            w.ImuPacket: (w.TAG_IMU, "imu"),
            w.MagPacket: (w.TAG_MAG, "mag"),
        }.get(type(packet))
        if kind is None:
            return
        key, attr = kind
        transition = w.classify_seq(self._last_seq[key], packet.seq)
        if transition.kind in ("first", "forward", "wrap"):
            self._last_seq[key] = packet.seq
        if transition.missing:
            setattr(self.dropped, attr, getattr(self.dropped, attr) + transition.missing)
        elif transition.kind == "duplicate":
            counter = f"duplicate_{attr}"
            setattr(self.dropped, counter, getattr(self.dropped, counter) + 1)
        elif transition.kind == "backward":
            counter = f"backward_{attr}"
            setattr(self.dropped, counter, getattr(self.dropped, counter) + 1)

    def close(self) -> None:
        was_streaming = self._streaming
        stopped = False
        worker_stopped = self._stop_link_keepalive()
        with self._keepalive_lock:
            worker_error = self._keepalive_error
        if worker_stopped and worker_error is None:
            try:
                self.send("STREAM TAG OFF")
                self._streaming = False
                stopped = True
            except BaseException:
                # Close is the one best-effort boundary: even when the device vanished
                # or refused STOP, release the host file descriptor.
                pass
        # If the bounded join failed, do not race a second command write against the
        # stuck worker. Release the owned handle and explicitly lower DTR to clear
        # firmware authorization for this USB epoch, even if the OS retains DTR
        # when closing a tty (for example, Linux with HUPCL disabled).
        if stopped and was_streaming:
            # Keep DTR/the CDC endpoint alive long enough for firmware to process
            # STREAM TAG OFF and finish its in-flight frame before the descriptor
            # disappears. Closing immediately after write() can strand the firmware
            # TX task while it owns the serial mutex; the command task then blocks on
            # that mutex until the task watchdog resets the board. This is the same
            # measured drain boundary used by Glove.stop(), now also enforced for a
            # context-manager/close path that never called stop() explicitly.
            self.drain(settle=0.2)
        if self._owns:
            try:
                if hasattr(self._s, "dtr"):
                    setattr(self._s, "dtr", False)
            except BaseException:
                # Control requests can also fail after a USB disconnect. Always
                # attempt descriptor cleanup even if the line cannot be lowered.
                pass
            try:
                self._s.close()
            except BaseException:
                pass
        with self._keepalive_lock:
            self._keepalive_error = None

    def __enter__(self) -> "UsbTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _find_config(text: bytes) -> Optional[Dict[str, Any]]:
    """Last complete `#CONFIG` line in a buffer, or None.

    Takes the LAST one: an earlier retry may have been answered while a stale stream
    was still draining, so the freshest reply is the trustworthy one.
    """
    return _find_prefixed_json(text, _CONFIG_PREFIX)


def _find_prefixed_json(text: bytes, prefix: str) -> Optional[Dict[str, Any]]:
    import json

    found = None
    for line in text.split(b"\n"):
        s = line.decode("utf8", "replace").strip()
        if s.startswith(prefix):
            try:
                value = json.loads(s[len(prefix):])
            except ValueError:
                continue
            if isinstance(value, dict):
                found = value
    return found
