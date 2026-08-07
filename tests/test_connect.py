"""connect(), connect_pair() and the transport selection. Never covered before."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
import oglo
from oglo import _usb
from oglo._usb import PortBusyError, UsbError


def _ports(monkeypatch, entries):
    fake = SimpleNamespace(comports=lambda: entries)
    import serial.tools
    monkeypatch.setattr(serial.tools, "list_ports", fake, raising=False)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake)


def port(device, serial_number="68EE8F000000"):
    return SimpleNamespace(device=device, vid=_usb.SEEED_VID, pid=0x56,
                           serial_number=serial_number, product="XIAO_ESP32S3",
                           manufacturer="Espressif Systems")


def fake_open(cfg=CFG_V6, side=None):
    def _open(device, *a, **kw):
        c = dict(cfg)
        if side:
            c["side"] = side
            c["serial"] = f"OGLO-{side[0].upper()}-{device[-1]}"
        return FakeSerial(c, stream=tagged_burst(4), hz=250.0)
    return _open


# --- transport selection ---------------------------------------------------------


def test_an_unknown_transport_is_rejected_by_name():
    with pytest.raises(ValueError, match="'usb', 'ble' or 'auto'"):
        oglo.connect(transport="carrier-pigeon")


def test_auto_uses_usb_when_a_glove_is_attached(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])
    monkeypatch.setattr(_usb, "open_serial", fake_open())
    monkeypatch.setattr(oglo, "open_serial", fake_open())
    g = oglo.connect(transport="auto")
    try:
        assert g.info.transport == "usb"
    finally:
        g.close()


def test_auto_falls_back_to_ble_when_no_glove_is_on_usb(monkeypatch):
    _ports(monkeypatch, [])
    called = {}

    def fake_connect_ble(serial=None, **kw):
        called["ble"] = True
        raise RuntimeError("reached BLE")

    import oglo._ble as ble
    monkeypatch.setattr(ble, "connect_ble", fake_connect_ble)
    with pytest.raises(RuntimeError, match="reached BLE"):
        oglo.connect(transport="auto")
    assert called.get("ble")


def test_auto_does_not_fall_back_when_the_port_is_merely_busy(monkeypatch):
    """A held port means the glove IS there. Silently going wireless would hide the
    real problem and hand back a connection with different characteristics."""
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])

    def busy(device, *a, **kw):
        raise PortBusyError(f"{device} is already held by PID 1234")

    monkeypatch.setattr(_usb, "open_serial", busy)
    monkeypatch.setattr(oglo, "open_serial", busy)

    import oglo._ble as ble
    monkeypatch.setattr(ble, "connect_ble",
                        lambda *a, **k: pytest.fail("fell back to BLE on a busy port"))

    with pytest.raises(PortBusyError, match="PID 1234"):
        oglo.connect(transport="auto")


def test_usb_serial_selector_matches_logical_config_identity_not_chip_descriptor(monkeypatch):
    _ports(monkeypatch, [
        port("/dev/cu.usbmodem1", "68EE8F111111"),
        port("/dev/cu.usbmodem2", "68EE8F222222"),
    ])
    opened = []

    def opener(device, *a, **kw):
        logical = "OGLO-R-TEST02" if device.endswith("1") else "OGLO-L-TEST01"
        serial = FakeSerial(dict(CFG_V6, serial=logical), stream=tagged_burst(2))
        opened.append(serial)
        return serial

    monkeypatch.setattr(oglo, "open_serial", opener)
    glove = oglo.connect(serial="OGLO-L-TEST01")
    try:
        assert glove.info.serial == "OGLO-L-TEST01"
        assert opened[0].closed, "the non-matching glove was left open"
    finally:
        glove.close()


def test_explicit_port_is_still_verified_against_requested_logical_serial(monkeypatch):
    opened = []

    def opener(device, *a, **kw):
        serial = FakeSerial(dict(CFG_V6, serial="OGLO-L-ACTUAL"))
        opened.append(serial)
        return serial

    monkeypatch.setattr(oglo, "open_serial", opener)
    with pytest.raises(UsbError, match="ACTUAL.*REQUESTED"):
        oglo.connect(serial="OGLO-L-REQUESTED", port="/dev/cu.usbmodem1")
    assert opened[0].closed


def test_usb_logical_serial_search_closes_a_prior_match_on_interrupt(monkeypatch):
    candidates = [SimpleNamespace(device="/dev/one"), SimpleNamespace(device="/dev/two")]
    monkeypatch.setattr(oglo, "list_candidates", lambda: candidates)

    class Match:
        info = SimpleNamespace(serial="OGLO-L-TARGET")
        closed = False

        def close(self):
            self.closed = True

    match = Match()
    calls = 0

    def open_candidate(device, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return match
        raise KeyboardInterrupt

    monkeypatch.setattr(oglo, "_connect_usb_port", open_candidate)
    with pytest.raises(KeyboardInterrupt):
        oglo.connect(serial="OGLO-L-TARGET")
    assert match.closed


# --- connect_pair ----------------------------------------------------------------


def test_pair_needs_two_gloves_and_says_what_it_found(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])
    with pytest.raises(UsbError, match="needs two gloves; found 1"):
        oglo.connect_pair()


def test_pair_refuses_three(monkeypatch):
    _ports(monkeypatch, [port(f"/dev/cu.usbmodem{c}", f"SN{c}") for c in "ABC"])
    with pytest.raises(UsbError, match="more than two"):
        oglo.connect_pair()


def test_pair_returns_left_then_right_regardless_of_port_order(monkeypatch):
    """Which hand is which comes from the device, so swapping cables cannot
    mislabel a hand."""
    _ports(monkeypatch, [port("/dev/cu.usbmodem1", "SN1"), port("/dev/cu.usbmodem2", "SN2")])

    def opener(device, *a, **kw):
        # port 1 is the RIGHT hand, port 2 the LEFT: deliberately not port order
        side = "right" if device.endswith("1") else "left"
        return FakeSerial(dict(CFG_V6, side=side, serial=f"OGLO-{side}", pair_id="PAIR-1"),
                          stream=tagged_burst(4), hz=250.0)

    monkeypatch.setattr(_usb, "open_serial", opener)
    monkeypatch.setattr(oglo, "open_serial", opener)
    left, right = oglo.connect_pair()
    try:
        assert left.info.side == "left" and right.info.side == "right"
    finally:
        left.close(); right.close()


def test_pair_of_the_same_side_is_refused_with_the_fix(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodem1", "SN1"), port("/dev/cu.usbmodem2", "SN2")])
    monkeypatch.setattr(_usb, "open_serial", fake_open(side="right"))
    monkeypatch.setattr(oglo, "open_serial", fake_open(side="right"))
    with pytest.raises(UsbError, match="SET SIDE"):
        oglo.connect_pair()


def test_pair_refuses_different_pair_ids(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodem1", "SN1"), port("/dev/cu.usbmodem2", "SN2")])

    def opener(device, *a, **kw):
        side = "left" if device.endswith("1") else "right"
        return FakeSerial(dict(
            CFG_V6,
            side=side,
            serial=f"OGLO-{side}",
            pair_id=f"PAIR-{device[-1]}",
        ))

    monkeypatch.setattr(oglo, "open_serial", opener)
    with pytest.raises(UsbError, match="different pair_id"):
        oglo.connect_pair()


def test_pair_requires_explicit_opt_in_when_both_pair_ids_are_blank(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodem1", "SN1"), port("/dev/cu.usbmodem2", "SN2")])

    def opener(device, *a, **kw):
        side = "left" if device.endswith("1") else "right"
        return FakeSerial(dict(CFG_V6, side=side, serial=f"OGLO-{side}", pair_id=""))

    monkeypatch.setattr(oglo, "open_serial", opener)
    with pytest.raises(UsbError, match="empty pair_id"):
        oglo.connect_pair()

    left, right = oglo.connect_pair(allow_unpaired=True)
    left.close(); right.close()


def test_pair_closes_the_first_glove_when_the_second_fails(monkeypatch):
    """Otherwise a failed pair leaks a held port, and the next attempt hits
    PortBusyError against your own process."""
    _ports(monkeypatch, [port("/dev/cu.usbmodem1", "SN1"), port("/dev/cu.usbmodem2", "SN2")])
    opened = []

    def opener(device, *a, **kw):
        if device.endswith("2"):
            raise UsbError("second glove is unhappy")
        s = FakeSerial(CFG_V6, stream=tagged_burst(4), hz=250.0)
        opened.append(s)
        return s

    monkeypatch.setattr(_usb, "open_serial", opener)
    monkeypatch.setattr(oglo, "open_serial", opener)
    with pytest.raises(UsbError):
        oglo.connect_pair()
    assert opened and all(s.closed for s in opened), "a failed pair leaked an open port"
