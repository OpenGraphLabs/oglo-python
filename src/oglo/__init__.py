"""OGLO Python SDK.

A five-finger tactile glove: 80 taxels per hand at 250 Hz, plus a 9-axis IMU.

    import oglo

    with oglo.connect() as g:
        for f in g.tactile():          # nominal 250 packets/s over USB
            f.counts                   # (5, 4, 4) uint16, raw ADC, NOT force
            f.seq, f.t_us, f.host_t, f.dropped

Calibration is an explicit state-changing operation. See `docs/03_calibration.md`
before calling `g.zero(...)`, `g.clean(...)`, or `g.raw()`.

Two hands: `left, right = oglo.connect_pair()`. Use the host receive boundary only to
relate them approximately; never align two gloves on `t_us` or `device_time_us`.
Their device-clock origins and drift are unrelated, and supported firmware provides no
hardware time-synchronisation contract.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from ._config import Info
from ._device import CalibrationLocked, DeviceError, Glove, SampleBatch
from ._frame import (R_FRAME_FROM_IMU, CleanStreamError, Frame, ImuSample,
                     MagSample)
from ._status import DeviceStatus, StatusError
from ._record import RecordError, record
from ._replay import Episode, ReplayError, replay
from ._usb import (DisconnectedError, NoGloveFound, PortBusyError, UsbError,
                   find_port, list_candidates, open_serial)
from ._usb import UsbTransport as _UsbTransport

__version__ = "0.1.0rc3"

__all__ = [
    "connect",
    "connect_pair",
    "record",
    "replay",
    "Glove",
    "Frame",
    "ImuSample",
    "MagSample",
    "Info",
    "R_FRAME_FROM_IMU",
    "Episode",
    "DeviceError",
    "CalibrationLocked",
    "SampleBatch",
    "DeviceStatus",
    "StatusError",
    "CleanStreamError",
    "UsbError",
    "PortBusyError",
    "NoGloveFound",
    "DisconnectedError",
    "RecordError",
    "ReplayError",
]


def connect(serial: Optional[str] = None, *, transport: str = "usb",
            port: Optional[str] = None, timeout: float = 6.0) -> Glove:
    """Open one glove by its logical CONFIG serial, not its USB chip identity.

    A port path is not stable across reboots and says nothing about which hand it is,
    so `port=` exists only as an escape hatch for a board that does not enumerate the
    way discovery expects.

    `transport="ble"` connects wirelessly. It delivers the same tactile rate but
    **not the same IMU rate**: BLE carries one IMU slot per tactile sample, so ~194 Hz
    of a 500 Hz stream arrives and the magnetometer repeats. Use USB when IMU rate or
    timing matters. `transport="auto"` tries USB and falls back to BLE.
    """
    if transport == "ble":
        from ._ble import connect_ble

        return connect_ble(serial, address=port, scan_timeout=timeout)
    if transport == "auto":
        try:
            return connect(serial, transport="usb", port=port, timeout=timeout)
        except NoGloveFound:
            # Only this one. A busy port, a board that will not answer, a truncated
            # config: all mean the glove is there and something specific is wrong,
            # and silently going wireless would both hide that and return a link
            # with a different IMU rate.
            from ._ble import connect_ble

            return connect_ble(serial, scan_timeout=timeout)
    if transport != "usb":
        raise ValueError(f"transport must be 'usb', 'ble' or 'auto', not {transport!r}")
    if port is not None:
        glove = _connect_usb_port(port, timeout=timeout)
        if serial is not None and glove.info.serial.casefold() != serial.casefold():
            actual = glove.info.serial
            glove.close()
            raise UsbError(
                f"{port} reports logical serial {actual!r}, not requested {serial!r}"
            )
        return glove

    if serial is None:
        return _connect_usb_port(find_port().device, timeout=timeout)

    # USB descriptor serials are chip identities (for example 68EE8F...), while the
    # public serial is the logical OGLO-L/R-... stored in CONFIG. Discovery cannot
    # know the latter without a quiet handshake, so inspect candidates and verify the
    # identity after opening. Returning a different glove is never an acceptable
    # fallback for a selector.
    candidates = list_candidates()
    if not candidates:
        raise NoGloveFound(f"no USB glove is attached while looking for {serial!r}")
    deadline = time.monotonic() + timeout
    matches: List[Glove] = []
    seen: List[str] = []
    failures: List[str] = []
    try:
        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures.append("search timeout")
                break
            try:
                glove = _connect_usb_port(candidate.device, timeout=remaining)
            except Exception as exc:
                failures.append(f"{candidate.device}: {type(exc).__name__}: {exc}")
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
        raise UsbError(f"multiple attached gloves report the duplicate logical serial {serial!r}")
    if failures:
        raise UsbError(
            f"could not safely resolve logical serial {serial!r}; "
            f"healthy devices reported {seen}, failures: {failures}"
        )
    raise NoGloveFound(f"no attached glove reports logical serial {serial!r}; saw {seen}")


def _connect_usb_port(device: str, *, timeout: float) -> Glove:
    t = _UsbTransport(open_serial(device))
    try:
        info, caps = t.read_config(timeout=timeout)
    except BaseException:
        t.close()
        raise
    return Glove(t, info, caps)


def connect_pair(*, timeout: float = 10.0) -> Tuple[Glove, Glove]:
    """Open both hands and return them as `(left, right)`.

    Which glove is which comes from the side stored on the device, never from the
    order the ports enumerated, so swapping cables cannot mislabel a hand. The two
    devices must report opposite sides and distinct logical serials.
    """
    cands = list_candidates()
    if len(cands) < 2:
        raise UsbError(
            f"connect_pair needs two gloves; found {len(cands)}: "
            + ", ".join(f"{c.serial_number}@{c.device}" for c in cands)
        )
    if len(cands) > 2:
        raise UsbError(
            "more than two gloves are attached; open them individually with connect(serial=...)"
        )

    gloves = []
    deadline = time.monotonic() + timeout
    try:
        for c in cands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsbError(f"connect_pair timed out after {timeout:.1f}s")
            gloves.append(connect(port=c.device, timeout=remaining))
        by_side = {g.info.side: g for g in gloves}
        if set(by_side) != {"left", "right"}:
            sides = [g.info.serial + "=" + g.info.side for g in gloves]
            raise UsbError(
                "both gloves report the same side, so a pair cannot be formed: "
                f"{sides}. Fix it on the device with SET SIDE."
            )
        if len({g.info.serial.casefold() for g in gloves}) != 2:
            raise UsbError("the two devices report the same logical serial; fix SET SERIAL first")
        return by_side["left"], by_side["right"]
    except BaseException:
        for g in gloves:
            g.close()
        raise
