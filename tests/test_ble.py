"""BLE transport. bleak is stubbed; no radio, no glove."""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from fake_serial import CFG_V6, COUNTS
from oglo import _wire as w


def ble_notify(n=3):
    p = bytearray(bytes([n, w.BLE_FLAG_PACKED6 | w.BLE_FLAG_PACKET_MAG]) + struct.pack("<II", 100, 50_000))
    for k in range(n):
        p += struct.pack("<H", k * 4000)
        p += w.pack12(COUNTS)
        p += struct.pack("<6h", 777, -531, -3982, -5, -8, 1)
        p += struct.pack("<3h", 3142, 678, -1107)
        p += struct.pack("<h", -1500)
    return bytes(p)


class FakeBleakClient:
    """Only what _ble touches."""

    def __init__(self, address, **kw):
        self.address = address
        self.connected = False
        self.notifying = False
        self.written = []
        self._cb = None
        self.config = dict(CFG_V6)
        self.status = {
            "uptime_ms": 1000, "seq": 1, "imu_ok": True,
            "imu": {"ok": True, "mag_ok": True}, "sensor_ok": True,
            "error_flags": 0, "deadline_misses": 0,
            "tag_dropped": 0, "tag_short_writes": 0,
        }
        self.disconnected_callback = kw.get("disconnected_callback")

    async def connect(self): self.connected = True; return True
    async def disconnect(self):
        self.connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)
    async def read_gatt_char(self, uuid):
        from oglo._ble import LOG_UUID
        return json.dumps(self.status if uuid == LOG_UUID else self.config).encode()
    async def write_gatt_char(self, uuid, data, response=True): self.written.append(bytes(data).decode())
    async def start_notify(self, uuid, cb): self.notifying = True; self._cb = cb
    async def stop_notify(self, uuid): self.notifying = False

    def push(self, payload):  # the radio delivering a notify
        self._cb(None, bytearray(payload))

    def vanish(self):
        self.connected = False
        if self.disconnected_callback:
            self.disconnected_callback(self)


@pytest.fixture
def bleak(monkeypatch):
    made = {}

    def factory(address, **kw):
        made["client"] = FakeBleakClient(address, **kw)
        return made["client"]

    mod = SimpleNamespace(BleakClient=factory, BleakScanner=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "bleak", mod)
    return made


def transport(bleak):
    from oglo._ble import BleTransport
    return BleTransport("AA:BB:CC:DD:EE:FF"), bleak["client"]


def test_ble_client_constructor_failure_does_not_leak_the_private_event_loop(monkeypatch):
    class ExplodingClient:
        def __init__(self, *a, **kw):
            raise RuntimeError("constructor boom")

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=ExplodingClient))
    from oglo._ble import BleError, BleTransport

    before = {thread.ident for thread in threading.enumerate() if thread.name == "oglo-ble"}
    with pytest.raises(BleError, match="constructor boom"):
        BleTransport("AA:BB")
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        leaked = [thread for thread in threading.enumerate()
                  if thread.name == "oglo-ble" and thread.ident not in before]
        if not leaked:
            break
        time.sleep(0.01)
    assert not leaked


def test_ble_client_keyboard_interrupt_does_not_leak_the_private_event_loop(monkeypatch):
    class InterruptedClient:
        def __init__(self, *a, **kw):
            raise KeyboardInterrupt

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=InterruptedClient))
    from oglo._ble import BleTransport

    before = {thread.ident for thread in threading.enumerate() if thread.name == "oglo-ble"}
    with pytest.raises(KeyboardInterrupt):
        BleTransport("AA:BB")
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        leaked = [thread for thread in threading.enumerate()
                  if thread.name == "oglo-ble" and thread.ident not in before]
        if not leaked:
            break
        time.sleep(0.01)
    assert not leaked


def test_timed_out_loop_call_cancels_the_coroutine_before_close():
    import asyncio
    from oglo._ble import _Loop

    cancelled = threading.Event()

    async def never():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    loop = _Loop()
    try:
        with pytest.raises(TimeoutError):
            loop.call(never(), timeout=0.01)
        assert cancelled.wait(1.0)
    finally:
        loop.close()


def test_failed_stop_notify_does_not_lie_about_subscription_state(bleak):
    from oglo._ble import BleError

    t, c = transport(bleak)
    try:
        t.read_config()
        t.start()

        async def fail_stop(_uuid):
            raise RuntimeError("radio refused stop")

        c.stop_notify = fail_stop
        with pytest.raises(BleError, match="radio refused stop"):
            t.stop()
        assert t._subscribed is True and c.notifying is True
    finally:
        t.close()
    assert c.connected is False


def test_config_is_read_from_the_characteristic_not_a_command(bleak):
    t, _ = transport(bleak)
    try:
        info, caps = t.read_config()
        assert info.serial == "OGLO-L-TEST01" and info.transport == "ble"
        assert caps.imu_len == 25
    finally:
        t.close()


def test_explicit_ble_address_is_verified_against_logical_serial(bleak):
    from oglo._ble import BleError, connect_ble

    with pytest.raises(BleError, match="OGLO-L-TEST01.*OGLO-R-WRONG"):
        connect_ble(serial="OGLO-R-WRONG", address="AA:BB:CC:DD:EE:FF")
    assert bleak["client"].connected is False


def test_ble_logical_serial_search_closes_a_prior_match_on_interrupt(monkeypatch):
    import oglo._ble as ble_module

    monkeypatch.setattr(
        ble_module,
        "list_candidates",
        lambda timeout: [
            ble_module.BleCandidate("AA:01", "OGLO LEFT"),
            ble_module.BleCandidate("AA:02", "OGLO RIGHT"),
        ],
    )

    class Match:
        info = SimpleNamespace(serial="OGLO-L-TARGET")
        closed = False

        def close(self):
            self.closed = True

    match = Match()
    calls = 0

    def open_candidate(address):
        nonlocal calls
        calls += 1
        if calls == 1:
            return match
        raise KeyboardInterrupt

    monkeypatch.setattr(ble_module, "_connect_ble_address", open_candidate)
    with pytest.raises(KeyboardInterrupt):
        ble_module.connect_ble(serial="OGLO-L-TARGET")
    assert match.closed


def test_status_is_read_from_the_ble_log_characteristic(bleak):
    t, c = transport(bleak)
    try:
        c.status["tag_dropped"] = 4
        status = t.read_status()
        assert status.healthy and status.tag_dropped == 4
    finally:
        t.close()


def test_a_truncated_config_blames_the_ble_stack_not_the_firmware(bleak):
    t, c = transport(bleak)
    try:
        async def truncated(uuid): return json.dumps(CFG_V6).encode()[:200]
        c.read_gatt_char = truncated
        with pytest.raises(Exception, match="truncated read points at the BLE stack"):
            t.read_config()
    finally:
        t.close()


def test_notifies_decode_into_samples(bleak):
    t, c = transport(bleak)
    try:
        t.read_config(); t.start()
        c.push(ble_notify(3))
        out = t.poll()
        assert len(out) == 3
        assert [s.seq for s in out] == [100, 101, 102]
        assert out[0].mag is not None and out[0].imu_dt_us == -1500
    finally:
        t.close()


def test_saturated_imu_age_is_preserved_but_counted_as_stale(bleak):
    t, c = transport(bleak)
    try:
        t.read_config(); t.start()
        payload = bytearray(ble_notify(1))
        struct.pack_into("<h", payload, w.BLE_HDR_LEN + w.BLE_V6_STRIDE - 2, -32768)
        c.push(bytes(payload))
        out = t.poll()
        assert len(out) == 1 and out[0].imu_dt_us == -32768
        assert t.stale_imu == 1
    finally:
        t.close()


def test_a_malformed_notify_is_counted_not_raised_from_a_callback(bleak):
    """The callback runs on the background loop; letting it raise would kill the
    link silently rather than report anything."""
    t, c = transport(bleak)
    try:
        t.read_config(); t.start()
        c.push(b"\x03\x10short")
        assert t.poll() == [] and t.malformed == 1
    finally:
        t.close()


def test_notify_mag_flag_must_match_the_config_capability(bleak):
    t, c = transport(bleak)
    try:
        t.read_config(); t.start()
        c.push(ble_notify(3))
        missing_mag = bytearray(ble_notify(3))
        missing_mag[1] &= ~w.BLE_FLAG_PACKET_MAG
        missing_mag[2:6] = struct.pack("<I", 103)
        c.push(bytes(missing_mag))
        out = t.poll()
        assert len(out) == 3
        assert t.malformed == 1
    finally:
        t.close()


def test_sequence_gaps_are_counted(bleak):
    t, c = transport(bleak)
    try:
        t.read_config(); t.start()
        c.push(ble_notify(1))
        p = bytearray(ble_notify(1)); p[2:6] = struct.pack("<I", 110)  # jump 100 -> 110
        c.push(bytes(p))
        t.poll()
        assert (t.dropped.tactile, t.dropped.imu, t.dropped.mag) == (9, 9, 9)
    finally:
        t.close()


def test_this_transport_declares_it_has_no_reply_channel(bleak):
    """The firmware writes command output to Serial only. Glove branches on this to
    confirm via the config instead of waiting for a line that never comes."""
    t, _ = transport(bleak)
    try:
        assert t.replies_in_text is False and t.read_text() == ""
    finally:
        t.close()


def test_commands_reach_the_command_characteristic(bleak):
    t, c = transport(bleak)
    try:
        t.send("SET THR 30")
        assert c.written == ["SET THR 30"]
    finally:
        t.close()


def test_a_glove_over_ble_confirms_a_command_by_rereading_the_config(bleak):
    from oglo._device import Glove
    t, c = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        c.config = dict(CFG_V6, stream_thr=30)  # the board applies it
        g.clean(threshold=30)
        assert "SET THR 30" in c.written and "SET STREAM CLEAN" in c.written
        assert g.info.stream_thr == 30
    finally:
        t.close()


def test_an_unconfirmed_command_says_it_may_still_have_applied(bleak):
    from oglo._device import DeviceError, Glove
    t, c = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)  # config never changes; the board ignores us
        with pytest.raises(DeviceError, match="may still have been applied"):
            g.clean(threshold=99)
    finally:
        t.close()


def test_ble_refuses_imu_rate_change_that_config_cannot_confirm(bleak):
    from oglo._device import DeviceError, Glove
    t, _ = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        with pytest.raises(DeviceError, match="cannot verify"):
            g.rates(imu=500)
    finally:
        t.close()


def test_ble_send_with_expect_refuses_an_impossible_reply_contract(bleak):
    from oglo._device import DeviceError, Glove
    t, _ = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        with pytest.raises(DeviceError, match="no command reply channel"):
            g.send("DIAG I2C", expect="#I2C")
    finally:
        t.close()


def test_ble_raw_send_is_refused_even_without_expect_because_success_is_unverifiable(bleak):
    from oglo._device import DeviceError, Glove
    t, _ = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        with pytest.raises(DeviceError, match="requires USB"):
            g.send("NONSENSE")
    finally:
        t.close()


def test_start_reports_the_ble_v6_layout(bleak):
    t, _ = transport(bleak)
    try:
        t.read_config()
        assert t.start() == "ble_v6"
    finally:
        t.close()


def test_a_glove_over_ble_actually_yields_samples(bleak):
    """The transport decoded fine and the demux dropped everything, because no branch
    handled BleSample. Zero samples on a healthy link, silently."""
    from oglo._device import Glove
    t, c = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        g._ensure_started()
        c.push(ble_notify(3))
        got = g._drain_ready()
        assert len(got["tactile"]) == 3, "BLE samples never reached the streams"
        assert len(got["imu"]) == 3
        assert len(got["mag"]) == 3
        assert got["imu"][0].raw == (777, -531, -3982, -5, -8, 1)
        assert got["mag"][0].raw == (3142, 678, -1107)
        assert g._demux.unrouted == 0
    finally:
        t.close()


def test_nothing_is_ever_dropped_without_being_counted(bleak):
    """A packet type no branch handles must not vanish quietly."""
    from oglo._device import Glove
    t, _ = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        g._demux._prepare(object(), 1)
        assert g._demux.unrouted == 1
    finally:
        t.close()


def test_ble_zero_fails_immediately_instead_of_waiting_for_a_text_reply_that_cannot_arrive(bleak):
    from oglo._device import DeviceError, Glove
    t, _ = transport(bleak)
    try:
        info, caps = t.read_config()
        g = Glove(t, info, caps)
        with pytest.raises(DeviceError, match="requires USB"):
            g.zero(sweep=1, timeout=0.01)
    finally:
        t.close()


def test_ble_callback_queue_is_bounded_and_reports_overflow(bleak):
    from oglo._ble import BleTransport
    t = BleTransport("AA:BB:CC:DD:EE:FF", queue_size=2)
    c = bleak["client"]
    try:
        t.read_config(); t.start()
        c.push(ble_notify(3))
        out = t.poll()
        assert len(out) == 2 and t.notification_overflow == 1
    finally:
        t.close()


def test_ble_disconnect_is_reported_instead_of_becoming_permanent_silence(bleak):
    from oglo._usb import DisconnectedError
    t, c = transport(bleak)
    try:
        t.read_config(); t.start()
        c.vanish()
        with pytest.raises(DisconnectedError, match="disconnected"):
            t.poll()
    finally:
        t.close()
