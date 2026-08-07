"""USB transport: discovery, handshake, read loop. No hardware."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo import _usb, _wire as w
from oglo._usb import PortCandidate, UsbError, UsbTransport, find_port, list_candidates


# --- discovery: opens nothing ---------------------------------------------------


def _ports(monkeypatch, entries):
    fake = SimpleNamespace(comports=lambda: entries)
    import serial.tools

    monkeypatch.setattr(serial.tools, "list_ports", fake, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "serial.tools.list_ports", fake)


def port(device, vid=_usb.SEEED_VID, pid=0x56, serial_number="68EE8F000001",
         product="OGLO", manufacturer="Seeed"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial_number,
                           product=product, manufacturer=manufacturer)


NIIMBOT = port("/dev/cu.usbmodemB1_TEST000011", vid=0x3513, pid=0x2,
               serial_number="B1-TEST00001", product="B1 LABEL PRINTER",
               manufacturer="NIIMBOT")
VIRTUAL = port("/dev/cu.Bluetooth-Incoming-Port", vid=None, pid=None,
               serial_number=None, product=None, manufacturer=None)


def test_a_label_printer_on_the_same_bus_is_not_offered_as_a_glove(monkeypatch):
    """This is not hypothetical: probing it blocked for two minutes."""
    _ports(monkeypatch, [NIIMBOT, VIRTUAL, port("/dev/cu.usbmodem68EE8F0000011")])
    got = list_candidates()
    assert [c.device for c in got] == ["/dev/cu.usbmodem68EE8F0000011"]


def test_virtual_ports_are_not_devices(monkeypatch):
    _ports(monkeypatch, [VIRTUAL, NIIMBOT])
    assert list_candidates(strict=False) == []


def test_a_glove_is_found_by_usb_serial_not_by_path(monkeypatch):
    _ports(monkeypatch, [
        port("/dev/cu.usbmodemA1", serial_number="68EE8F000001"),
        port("/dev/cu.usbmodemB1", serial_number="68EE8F000002"),
    ])
    assert find_port("000002").device == "/dev/cu.usbmodemB1"
    assert find_port("68EE8F000001").device == "/dev/cu.usbmodemA1"


def test_two_gloves_with_no_serial_given_is_an_error_that_lists_them(monkeypatch):
    _ports(monkeypatch, [
        port("/dev/cu.usbmodemA1", serial_number="68EE8F000001"),
        port("/dev/cu.usbmodemB1", serial_number="68EE8F000002"),
    ])
    with pytest.raises(UsbError, match="more than one"):
        find_port()


def test_an_unknown_serial_says_what_is_actually_visible(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA1", serial_number="68EE8F000001")])
    with pytest.raises(UsbError, match="68EE8F000001"):
        find_port("DEADBEEF")


def test_no_glove_at_all_is_an_error_not_an_empty_result(monkeypatch):
    _ports(monkeypatch, [NIIMBOT])
    with pytest.raises(UsbError, match="no glove found"):
        find_port()


def test_firmware_099_default_usb_strings_are_accepted_by_vid_then_config(monkeypatch):
    _ports(monkeypatch, [
        port("/dev/cu.usbmodemOLD", vid=0x303A, serial_number="68EE8F000001"),
        port("/dev/cu.usbmodemCURRENT", vid=_usb.SEEED_VID, serial_number="68EE8F000003",
             product="XIAO_ESP32S3", manufacturer="Espressif Systems"),
    ])
    assert [c.device for c in list_candidates()] == ["/dev/cu.usbmodemCURRENT"]
    assert find_port("000003").product == "XIAO_ESP32S3"


# --- handshake ------------------------------------------------------------------


def test_the_handshake_stops_a_stream_a_crashed_session_left_running():
    s = FakeSerial(CFG_V6)
    UsbTransport(s).read_config(drain=0)
    assert s.commands[:3] == ["STREAM BIN OFF", "STREAM TAXEL OFF", "STREAM TAG OFF"]


def test_config_is_retried_until_the_board_answers():
    """A board mid-stream needs draining before a text reply is findable."""
    s = FakeSerial(CFG_V6, config_after=3)
    info, caps = UsbTransport(s).read_config(timeout=6.0, interval=0.01, drain=0)
    assert info.serial == "OGLO-L-TEST01"
    assert s.commands.count("GET CONFIG") >= 3


def test_runtime_status_exposes_sensor_health_and_device_drop_counters():
    s = FakeSerial(CFG_V6)
    s.status["tag_dropped"] = 7
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    status = t.read_status(timeout=0.2)
    assert status.healthy and status.tag_dropped == 7 and status.mag_ok


def test_runtime_status_rejects_truthy_strings_and_wrapped_integer_fields():
    from oglo._status import StatusError, parse_status

    raw = dict(FakeSerial(CFG_V6).status)
    raw["imu_ok"] = "false"
    with pytest.raises(StatusError, match="imu_ok must be boolean"):
        parse_status(raw)

    raw = dict(FakeSerial(CFG_V6).status)
    raw["seq"] = 1 << 32
    with pytest.raises(StatusError, match="exceeds"):
        parse_status(raw)

    raw = dict(FakeSerial(CFG_V6).status)
    raw["imu"] = {**raw["imu"], "ok": False}
    with pytest.raises(StatusError, match="disagrees"):
        parse_status(raw)


def test_truncated_status_cannot_look_like_healthy_zero_counters():
    from oglo._status import StatusError, parse_status

    with pytest.raises(StatusError, match="missing"):
        parse_status({
            "imu_ok": True,
            "imu": {"mag_ok": True},
            "sensor_ok": True,
        })


def test_a_board_that_never_answers_fails_with_a_useful_message():
    s = FakeSerial(config=None)
    with pytest.raises(UsbError, match="no #CONFIG"):
        UsbTransport(s).read_config(timeout=0.2, interval=0.01, drain=0)


def test_the_freshest_config_wins_when_several_are_in_the_buffer():
    """An earlier retry may be answered while a stale stream drains."""
    stale = dict(CFG_V6, serial="OLD", stream_thr=99)
    s = FakeSerial(CFG_V6)
    s._out += b"#CONFIG " + __import__("json").dumps(stale).encode() + b"\r\n"
    info, _ = UsbTransport(s).read_config(interval=0.01, drain=0)
    assert info.serial == "OGLO-L-TEST01"


def test_the_sdk_does_not_touch_scan_timing():
    """The viewer once wrote SET SCAN on connect and cut a board from 250 to 154 Hz.
    An SDK has even less business changing a device setting nobody asked it to."""
    s = FakeSerial(CFG_V6)
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    assert not any(c.upper().startswith("SET SCAN") for c in s.commands)


# --- stream ---------------------------------------------------------------------


def test_a_current_board_gets_the_tagged_stream():
    s = FakeSerial(CFG_V6, stream=tagged_burst(2))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    assert t.start() == "tagged"
    assert "STREAM TAG ON" in s.commands


# --- read loop ------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [1, 7, 64, 133, 4096])
def test_the_read_loop_reassembles_across_any_chunk_size(chunk):
    """A serial read returns whatever was ready. A decoder that only works on tidy
    boundaries works right up until it does not."""
    s = FakeSerial(CFG_V6, stream=tagged_burst(4), chunk=chunk)
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    got = []
    for _ in range(4000):
        got += t.poll()
        if sum(isinstance(p, w.TactilePacket) for p in got) >= 4:
            break
    tac = [p for p in got if isinstance(p, w.TactilePacket)]
    assert len(tac) >= 4, f"chunk={chunk}"
    assert all(p.counts[0] == 550 for p in tac)


def test_all_three_modalities_arrive_at_their_own_rates():
    s = FakeSerial(CFG_V6, stream=tagged_burst(8))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    got = []
    for _ in range(2000):
        got += t.poll()
        if sum(isinstance(p, w.TactilePacket) for p in got) >= 8:
            break
    n_tac = sum(isinstance(p, w.TactilePacket) for p in got)
    n_imu = sum(isinstance(p, w.ImuPacket) for p in got)
    n_mag = sum(isinstance(p, w.MagPacket) for p in got)
    assert n_imu == pytest.approx(n_tac * 2, abs=2)   # IMU is 2x tactile
    assert n_mag == pytest.approx(n_tac / 2, abs=2)   # mag is a quarter of IMU


def test_host_side_loss_is_counted_per_stream():
    s = FakeSerial(CFG_V6, stream=b"")
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    s._out += tagged_burst(1, start_seq=0)
    s._out += tagged_burst(1, start_seq=5)  # tactile 1..4 never arrive
    for _ in range(200):
        t.poll()
    assert t.dropped.tactile == 4
    assert t.dropped.imu > 0


def test_malformed_tag_header_is_counted_while_ascii_stream_preamble_is_ignored():
    import struct

    bad = w.TAG_MAGIC + bytes([w.TAG_TACTILE]) + struct.pack("<HII", 999, 1, 1)
    stream = b"#STREAM TAG on\r\nordinary junk" + bad + tagged_burst(2)
    s = FakeSerial(CFG_V6, stream=stream)
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    got = []
    for _ in range(1000):
        got += t.poll()
        if sum(isinstance(packet, w.TactilePacket) for packet in got) >= 2:
            break
    assert sum(isinstance(packet, w.TactilePacket) for packet in got) >= 2
    assert t.dropped.malformed_usb == 1


def test_plain_usb_preamble_does_not_increment_malformed_counter():
    s = FakeSerial(CFG_V6, stream=b"#STREAM TAG on\r\n" + tagged_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    for _ in range(200):
        t.poll()
    assert t.dropped.malformed_usb == 0


def test_starting_a_new_capture_resets_the_loss_counters():
    """An intentional OFF interval is not data loss."""
    s = FakeSerial(CFG_V6, stream=b"")
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    s._out += tagged_burst(1, start_seq=0) + tagged_burst(1, start_seq=9)
    for _ in range(200):
        t.poll()
    assert t.dropped.tactile > 0
    t.start()
    assert t.dropped.tactile == 0
    assert t.dropped.malformed_usb == 0


def test_close_stops_the_stream_and_releases_the_port():
    s = FakeSerial(CFG_V6, stream=tagged_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    resets_before_close = 0
    original_reset = s.reset_input_buffer

    def reset_after_stop():
        nonlocal resets_before_close
        resets_before_close += 1
        original_reset()

    s.reset_input_buffer = reset_after_stop
    t.close()
    assert "STREAM TAG OFF" in s.commands and s.closed is True
    assert resets_before_close == 1  # OFF was allowed to drain before CDC close


def test_the_transport_is_a_context_manager():
    s = FakeSerial(CFG_V6, stream=tagged_burst(1))
    with UsbTransport(s) as t:
        t.read_config(interval=0.01, drain=0)
        t.start()
    assert s.closed is True


def test_info_before_read_config_is_an_error_not_a_none():
    with pytest.raises(UsbError, match="read_config"):
        _ = UsbTransport(FakeSerial(CFG_V6)).info


def test_a_disconnect_mid_stream_says_what_happened_and_what_to_do():
    """pyserial's own words for this are "Attempting to use a port that is not open",
    which tells a researcher nothing. Confirmed on hardware by closing the handle
    under a live stream."""
    from oglo._usb import DisconnectedError

    s = FakeSerial(CFG_V6, stream=tagged_burst(4))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    t.poll()

    def dead(*a, **k):
        raise OSError("device not configured")

    s.read = dead
    with pytest.raises(DisconnectedError, match="cable"):
        t.poll()


def test_a_command_to_a_vanished_glove_also_reports_the_disconnect():
    from oglo._usb import DisconnectedError

    s = FakeSerial(CFG_V6)
    t = UsbTransport(s)

    def dead(*a, **k):
        raise OSError("device not configured")

    s.write = dead
    with pytest.raises(DisconnectedError, match="no longer reachable"):
        t.send("GET CONFIG")
