"""BLE transport.

Same shape as `UsbTransport`, so `Glove` does not know which one it holds. Two things
are genuinely different and neither can be papered over:

**There is no command reply channel.** The firmware writes command output to `Serial`
only; the BLE command characteristic is write-only and the log characteristic carries
a periodic status JSON, not replies. So a command is confirmed by re-reading the
config characteristic until the state actually changes. `Glove` handles that via
`replies_in_text = False`; the consequence is that a command with nothing observable
in the config cannot be confirmed at all.

**BLE does not deliver what USB delivers.** The notify packet still uses the
interleaved v6 slot: one IMU and one magnetometer reading per tactile sample. So the
IMU loses half of what the board produces (~194 Hz of a 500 Hz stream) and the
magnetometer arrives duplicated. Tactile is unaffected. Measured ceiling is 312 Hz
tactile / 45.4 kB/s. **Use USB when IMU rate or timing matters.**

bleak is async and this SDK is not, so the client runs on its own event loop in a
background thread. That is also what avoids a trap: driving bleak from a loop that a
synchronous read blocks makes notifications stop silently, reporting zero packets a
second while the link is perfectly healthy.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import _wire as w
from ._config import Capabilities, Info, parse_config
from ._status import DeviceStatus, parse_status
from ._usb import DisconnectedError, StreamCounters, UsbError

SERVICE_UUID = "4652535f-424c-4500-0000-000000000001"
NOTIFY_UUID = "4652535f-424c-4500-0001-000000000001"
CONFIG_UUID = "4652535f-424c-4500-0002-000000000001"
COMMAND_UUID = "4652535f-424c-4500-0003-000000000001"
LOG_UUID = "4652535f-424c-4500-0004-000000000001"

#: Boards advertise as "OGLO LEFT" / "OGLO RIGHT".
NAME_PREFIX = "OGLO"


class BleError(UsbError):
    """Kept in the UsbError family so `except oglo.UsbError` catches either transport."""


@dataclass(frozen=True)
class BleCandidate:
    address: str
    name: str

    @property
    def serial_number(self) -> str:
        return self.name

    @property
    def device(self) -> str:
        return self.address


class _Loop:
    """An asyncio loop on a private thread, so blocking the caller cannot starve it."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="oglo-ble")
        self._thread.start()
        self._closed = False

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 30.0):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except BaseException as exc:
            # A timed-out GATT/connect operation must not survive invisibly on the
            # private loop. It can otherwise keep references/FDS alive after close.
            future.cancel()
            try:
                future.result(1.0)
            except BaseException:
                pass
            if isinstance(exc, concurrent.futures.TimeoutError):
                raise TimeoutError(f"BLE operation timed out after {timeout:.3g}s") from exc
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.loop.is_running():
            async def _cancel_pending() -> None:
                current = asyncio.current_task()
                pending = [task for task in asyncio.all_tasks() if task is not current]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            shutdown = asyncio.run_coroutine_threadsafe(_cancel_pending(), self.loop)
            try:
                shutdown.result(2.0)
            except BaseException:
                shutdown.cancel()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3)
        if not self.loop.is_running():
            self.loop.close()


def list_candidates(timeout: float = 8.0, name: str = NAME_PREFIX) -> List[BleCandidate]:
    """Scan for advertising gloves. Matches the service UUID or the name."""
    try:
        from bleak import BleakScanner
    except ImportError as exc:
        raise BleError("BLE needs bleak: pip install bleak") from exc

    async def _scan():
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for dev, adv in found.values():
            nm = dev.name or adv.local_name or ""
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            if name.lower() in nm.lower() or SERVICE_UUID in uuids:
                out.append(BleCandidate(address=dev.address, name=nm or dev.address))
        return sorted(out, key=lambda c: (c.name, c.address))

    lp = _Loop()
    try:
        return lp.call(_scan(), timeout=timeout + 10)
    finally:
        lp.close()


class BleTransport:
    """Mirrors `UsbTransport`. `Glove` cannot tell the two apart."""

    #: No reply channel; see the module docstring.
    replies_in_text = False

    def __init__(self, address: str, *, name: str = "", queue_size: int = 4096) -> None:
        try:
            from bleak import BleakClient
        except ImportError as exc:
            raise BleError("BLE needs bleak: pip install bleak") from exc

        if queue_size <= 0:
            raise ValueError("BLE queue_size must be positive")
        self.address = address
        self.name = name
        self._disconnected = False
        self._closing = False
        self._closed = False
        self._samples: Deque[Any] = deque(maxlen=queue_size)
        self._lock = threading.Lock()
        self._config: Optional[Dict[str, Any]] = None
        self._info: Optional[Info] = None
        self._caps: Optional[Capabilities] = None
        self._subscribed = False
        self._last_seq: Optional[int] = None
        self.dropped = StreamCounters()
        self.malformed = 0
        self.stale_imu = 0
        self.notification_overflow = 0
        self._lp = _Loop()
        try:
            self._client = BleakClient(address, disconnected_callback=self._on_disconnect)
            self._lp.call(self._client.connect(), timeout=30)
        except BaseException as exc:
            self._lp.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise BleError(f"could not construct/connect BLE client for {address}: {exc}") from exc

    def _on_disconnect(self, _client: Any) -> None:
        if not self._closing:
            self._disconnected = True

    # -- config -----------------------------------------------------------------

    def read_config(self, timeout: float = 6.0, interval: float = 0.5,
                    *, drain: float = 0.0) -> Tuple[Info, Capabilities]:
        raw = self._lp.call(self._client.read_gatt_char(CONFIG_UUID), timeout=timeout + 5)
        try:
            cfg = json.loads(bytes(raw).decode("utf-8"))
        except ValueError as exc:
            # 545-592 B configs used to exceed the 512 B spec limit; a central that
            # truncates rather than refusing lands exactly here.
            raise BleError(
                f"config characteristic did not parse as JSON ({len(raw)} B read). "
                "A truncated read points at the BLE stack, not the firmware."
            ) from exc
        self._config = cfg
        self._info, self._caps = parse_config(cfg, transport="ble")
        return self._info, self._caps

    def read_status(self, timeout: float = 3.0) -> DeviceStatus:
        raw = self._lp.call(self._client.read_gatt_char(LOG_UUID), timeout=timeout + 5)
        try:
            value = json.loads(bytes(raw).decode("utf-8"))
        except ValueError as exc:
            raise BleError("BLE log characteristic did not contain status JSON") from exc
        return parse_status(value)

    @property
    def info(self) -> Info:
        if self._info is None:
            raise BleError("read_config() first")
        return self._info

    @property
    def caps(self) -> Capabilities:
        if self._caps is None:
            raise BleError("read_config() first")
        return self._caps

    # -- commands ---------------------------------------------------------------

    def send(self, command: str) -> None:
        """Write to the command characteristic. Nothing comes back; see the module docstring."""
        payload = command.rstrip("\n").encode()
        self._lp.call(self._client.write_gatt_char(COMMAND_UUID, payload, response=True), timeout=10)

    def read_text(self, size: int = 8192) -> str:
        return ""  # there is no text channel over BLE

    # -- streaming --------------------------------------------------------------

    def _on_notify(self, _sender: Any, payload: bytearray) -> None:
        received_ns = time.monotonic_ns()
        try:
            samples = w.decode_ble_notify(bytes(payload))
        except w.WireError:
            with self._lock:
                self.malformed += 1
            return
        # In firmware 0.9.9 the CONFIG has_mag value and every notify's packet-mag
        # flag come from the same boot-time state. A mismatch is not an optional
        # per-sample omission: it is a corrupt/incompatible notify. Accepting it
        # silently produced tactile+IMU while losing the entire mag batch without a
        # sequence gap or any public loss counter.
        if self._info is not None and any(
            (sample.mag is not None) != self._info.has_mag for sample in samples
        ):
            with self._lock:
                self.malformed += 1
            return
        with self._lock:
            for s in samples:
                # Firmware 0.9.9 saturates the signed IMU age at the int16 limits.
                # At that point tactile is still fresh but the embedded IMU is not;
                # keeping the row is useful for a partial episode, while this
                # counter prevents Recorder from calling the capture complete.
                if s.imu_dt_us in (-32768, 32767):
                    self.stale_imu += 1
                transition = w.classify_seq(self._last_seq, s.seq)
                if transition.kind in ("first", "forward", "wrap"):
                    self._last_seq = s.seq
                affected = ["tactile", "imu"]
                if self._info is not None and self._info.has_mag:
                    affected.append("mag")
                for name in affected:
                    setattr(self.dropped, name, getattr(self.dropped, name) + transition.missing)
                if transition.kind == "duplicate":
                    for name in affected:
                        counter = f"duplicate_{name}"
                        setattr(self.dropped, counter, getattr(self.dropped, counter) + 1)
                elif transition.kind == "backward":
                    for name in affected:
                        counter = f"backward_{name}"
                        setattr(self.dropped, counter, getattr(self.dropped, counter) + 1)
                if len(self._samples) == self._samples.maxlen:
                    self.notification_overflow += 1
                self._samples.append(replace(s, host_received_ns=received_ns))

    def start(self, *, reset_counters: bool = True) -> str:
        """Subscribe to the firmware-0.9.9 schema-6 notification."""
        if self._disconnected:
            raise DisconnectedError("the BLE glove disconnected; reconnect before starting again")
        # Reset callback-owned state before enabling notifications. Doing part of
        # this outside the lock let an immediate callback race with the reset and
        # either lose its sample or account it against the previous session.
        with self._lock:
            self._samples.clear()
            self._last_seq = None
            if reset_counters:
                self.dropped = StreamCounters()
                self.notification_overflow = 0
                self.malformed = 0
                self.stale_imu = 0
        if not self._subscribed:
            try:
                self._lp.call(self._client.start_notify(NOTIFY_UUID, self._on_notify), timeout=15)
            except Exception as exc:
                raise BleError(f"could not start BLE notifications: {exc}") from exc
            self._subscribed = True
        return "ble_v6"

    def stop(self) -> None:
        if self._subscribed:
            try:
                self._lp.call(self._client.stop_notify(NOTIFY_UUID), timeout=10)
            except Exception as exc:
                if self._closing or self._disconnected:
                    self._subscribed = False
                    return
                # Keep the state truthful. Pretending the callback stopped makes a
                # later start_notify collide with the still-live subscription.
                raise BleError(f"could not stop BLE notifications: {exc}") from exc
            else:
                self._subscribed = False

    def drain(self, settle: float = 0.1) -> None:
        if settle:
            time.sleep(settle)
        with self._lock:
            self._samples.clear()

    def poll(self, size: int = 8192) -> List[Any]:
        """Hand over what the notification callback has collected.

        Nothing is read here: bleak's callback runs on the background loop and fills
        the list. That separation is the point -- a caller that blocks cannot stall
        the link.
        """
        with self._lock:
            out = list(self._samples)
            self._samples.clear()
        if not out and self._disconnected:
            raise DisconnectedError("the BLE glove disconnected during capture")
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True
        try:
            self.stop()
        except BaseException:
            pass
        try:
            self._lp.call(self._client.disconnect(), timeout=10)
        except BaseException:
            pass
        finally:
            self._lp.close()

    def __enter__(self) -> "BleTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect_ble(serial: Optional[str] = None, *, address: Optional[str] = None,
                scan_timeout: float = 8.0):
    """Open one glove over BLE, verifying its logical CONFIG serial."""
    from ._device import Glove

    if address is not None:
        glove = _connect_ble_address(address)
        if serial is not None and glove.info.serial.casefold() != serial.casefold():
            actual = glove.info.serial
            glove.close()
            raise BleError(
                f"BLE address {address} reports logical serial {actual!r}, not {serial!r}"
            )
        return glove

    candidates = list_candidates(timeout=scan_timeout)
    if not candidates:
        raise BleError(
            "no glove is advertising. A board streaming over USB still advertises, "
            "but check it is powered and in range."
        )
    if serial is None:
        if len(candidates) > 1:
            raise BleError(
                "more than one glove is advertising; pass the logical CONFIG serial= to choose"
            )
        return _connect_ble_address(candidates[0].address)

    matches = []
    seen = []
    failures = []
    # Advertisements only say OGLO LEFT/RIGHT in firmware 0.9.9. Treating that name
    # as a serial selector silently picked the wrong device, so inspect CONFIG and
    # verify identity before returning anything.
    try:
        for candidate in candidates:
            try:
                glove = _connect_ble_address(candidate.address)
            except Exception as exc:
                failures.append(f"{candidate.address}: {type(exc).__name__}: {exc}")
                continue
            seen.append(glove.info.serial)
            if glove.info.serial.casefold() == serial.casefold():
                matches.append(glove)
            else:
                glove.close()
    except BaseException:
        for glove in matches:
            glove.close()
        raise
    if len(matches) == 1:
        return matches[0]
    for glove in matches:
        glove.close()
    if len(matches) > 1:
        raise BleError(f"multiple advertising gloves report duplicate logical serial {serial!r}")
    raise BleError(
        f"no advertising glove reports logical serial {serial!r}; "
        f"saw {seen}" + (f", failures: {failures}" if failures else "")
    )


def _connect_ble_address(address: str):
    from ._device import Glove

    transport = BleTransport(address)
    try:
        info, caps = transport.read_config()
    except BaseException:
        transport.close()
        raise
    return Glove(transport, info, caps)
