"""USB transport: discovery, handshake, read loop. No hardware."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from fake_serial import BOOT_A, BOOT_B, CFG_V6, FakeSerial, tagged_burst, tagged_v2_burst
from oglo import _usb, _wire as w
from oglo._usb import (
    DisconnectedError,
    PortCandidate,
    SessionChangedError,
    UsbError,
    UsbTransport,
    find_port,
    list_candidates,
    open_serial,
)


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


def test_open_serial_asserts_dtr_and_keeps_rts_low(monkeypatch):
    class Port:
        def __init__(self):
            self.opened = False
            self.closed = False

        def open(self):
            self.opened = True

        def close(self):
            self.closed = True

    port_object = Port()
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda: port_object))
    opened = open_serial("/dev/fake-oglo", settle=0)
    assert opened is port_object and port_object.opened
    assert port_object.dtr is True and port_object.rts is False


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
    assert s.commands[3] == "STREAM TAG2 OFF"


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


def test_missing_tag_capability_is_an_explicit_v1_fallback():
    s = FakeSerial(CFG_V6, stream=tagged_burst(1))
    t = UsbTransport(s)
    _, caps = t.read_config(interval=0.01, drain=0)
    assert caps.tag_ver_max == 1
    assert t.start() == "tagged"
    assert t.tag_version == 1
    assert s.commands[-1] == "STREAM TAG ON"


def test_tag_v2_is_selected_only_when_config_advertises_it():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=tagged_v2_burst(2, start_time_us=(2 << 32) - 4000))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    assert t.start() == "tagged_v2"
    assert t.tag_version == 2
    assert t.stream_boot_id == BOOT_A
    assert s.commands[-1] == "STREAM TAG2 ON"
    packets = []
    for _ in range(1000):
        packets += t.poll()
        if sum(isinstance(packet, w.TactilePacket) for packet in packets) >= 2:
            break
    tactile = [packet for packet in packets if isinstance(packet, w.TactilePacket)]
    assert [packet.device_time_us for packet in tactile[:2]] == [
        (2 << 32) - 4000,
        2 << 32,
    ]


def test_split_tag_v2_ack_preserves_the_first_binary_frame():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    stream = tagged_v2_burst(1, start_time_us=(4 << 32) + 123)
    s = FakeSerial(config, stream=stream, chunk=1)
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start(ack_timeout=0.2)
    packets = []
    for _ in range(len(stream) + 100):
        packets += t.poll()
        if any(isinstance(packet, w.TactilePacket) for packet in packets):
            break
    tactile = next(packet for packet in packets if isinstance(packet, w.TactilePacket))
    assert tactile.seq == 0
    assert tactile.device_time_us == (4 << 32) + 123


def test_buffered_text_is_drained_before_tag_v2_command_and_binary_is_preserved():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    stream = tagged_v2_burst(1, start_time_us=(5 << 32) + 321)
    s = FakeSerial(config, stream=stream, chunk=7)
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    s._out += b"#HB stale-before-command\r\n"

    assert t.start(ack_timeout=0.2) == "tagged_v2"
    packets = []
    for _ in range(len(stream) + 100):
        packets += t.poll()
        if any(isinstance(packet, w.TactilePacket) for packet in packets):
            break
    tactile = next(packet for packet in packets if isinstance(packet, w.TactilePacket))
    assert tactile.device_time_us == (5 << 32) + 321


@pytest.mark.parametrize("prelude", [
    b"#HB t_us=123 scan_us=2800\r\n",
    b"#ERR busy\r\n",
    b"#STREAM TAG2 on boot_id=short\r\n",
    b"\xa5\x5b\x01\x00\n",
])
def test_tag_v2_ack_rejects_any_post_command_line_before_the_exact_ack(prelude):
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    s.tag2_prelude = prelude
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)

    with pytest.raises(UsbError, match="malformed TAG2 start ACK"):
        t.start(ack_timeout=0.2)


@pytest.mark.parametrize("boot_id", [BOOT_A.upper(), "0" * 31, "g" * 32])
def test_tag_v2_ack_rejects_noncanonical_boot_identity(boot_id):
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    s.tag2_boot_id = boot_id
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    with pytest.raises(UsbError, match="malformed TAG2 start ACK"):
        t.start()


def test_tag_v2_start_rejects_config_ack_boot_identity_mismatch_and_stops_stream():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    s.tag2_boot_id = BOOT_B
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    with pytest.raises(SessionChangedError, match="boot identity changed"):
        t.start()
    assert s.commands[-1] == "STREAM TAG2 OFF"
    assert t.stream_boot_id is None and s._streaming is False


def test_tag_v2_resume_rejects_changed_ack_even_without_config_boot_id():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    s.tag2_boot_id = BOOT_A
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    t.stop()
    s.tag2_boot_id = BOOT_B
    with pytest.raises(SessionChangedError, match="boot identity changed"):
        t.start(reset_counters=False)


def test_tag_v2_start_requires_the_ack_before_accepting_binary():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=b"")
    s.emit_tag2_ack = False
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    with pytest.raises(UsbError, match="no TAG2 start ACK"):
        t.start(ack_timeout=0.02)


def test_tag_v2_start_rejects_a_malformed_ack_boot_id():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    s.tag2_boot_id = "a" * 31
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    with pytest.raises(UsbError, match="malformed TAG2 start ACK"):
        t.start()


def test_tag_v2_start_rolls_back_even_when_ack_read_is_interrupted():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    original_read = s.read

    def interrupt(size=1):
        if s._streaming:
            raise KeyboardInterrupt
        return original_read(size)

    s.read = interrupt
    with pytest.raises(KeyboardInterrupt):
        t.start()
    assert s.commands[-1] == "STREAM TAG2 OFF"
    assert not s._streaming and t.stream_boot_id is None


def test_session_changed_error_is_publicly_catchable():
    import oglo

    assert oglo.SessionChangedError is SessionChangedError


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_tag_v2_ack_timeout_cannot_disable_the_start_deadline(timeout):
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=b"")
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    with pytest.raises(ValueError, match="finite positive"):
        t.start(ack_timeout=timeout)
    assert s._streaming is False


def test_a_future_tag_capability_is_capped_at_the_latest_known_contract():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 7}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    assert t.start() == "tagged_v2"
    assert t.tag_version == 2


def test_boot_identity_is_reobserved_and_never_reused_across_config_reads():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    assert t.stream_boot_id == BOOT_A
    s.config = {**config, "boot_id": BOOT_B}
    s.tag2_boot_id = BOOT_B
    t.read_config(interval=0.01, drain=0)
    assert t.stream_boot_id is None
    t.start()
    assert t.stream_boot_id == BOOT_B


def test_failed_reconnect_config_invalidates_the_previous_boot_identity():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    s.config = None
    with pytest.raises(UsbError, match="no #CONFIG"):
        t.read_config(timeout=0.05, interval=0.01, drain=0)
    assert t.stream_boot_id is None
    with pytest.raises(UsbError, match="read_config"):
        t.start()


def test_stopping_tag_v2_uses_the_matching_command():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    t.stop()
    assert s.commands[-1] == "STREAM TAG2 OFF"


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


def test_tag_v2_crc_corruption_is_counted_and_the_next_frame_is_delivered():
    first = bytearray(tagged_v2_burst(1, start_time_us=(3 << 32) + 100))
    first[17] ^= 0x01  # first tactile payload byte; leave its CRC unchanged
    stream = bytes(first) + tagged_v2_burst(
        1, start_seq=1, start_time_us=(3 << 32) + 4100
    )
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=stream, chunk=7)
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()

    got = []
    for _ in range(4000):
        got += t.poll()
        if any(isinstance(packet, w.TactilePacket) and packet.seq == 1 for packet in got):
            break

    tactile = [packet for packet in got if isinstance(packet, w.TactilePacket)]
    assert not any(packet.seq == 0 for packet in tactile)
    assert any(packet.seq == 1 for packet in tactile)
    assert t.dropped.malformed_usb >= 1


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


@pytest.mark.parametrize("written", [None, 0, 1, True])
def test_control_commands_reject_none_zero_boolean_or_partial_serial_writes(written):
    s = FakeSerial(CFG_V6)
    t = UsbTransport(s)
    flushed = False

    def short_write(_payload):
        return written

    def flush():
        nonlocal flushed
        flushed = True

    s.write = short_write
    s.flush = flush
    with pytest.raises(DisconnectedError, match="no longer reachable") as caught:
        t.send("GET CONFIG")
    assert "short serial write" in str(caught.value.__cause__)
    assert flushed is False


def test_short_tag_v2_on_write_rolls_back_with_the_matching_off_command():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    writes = []
    original_write = s.write

    def short_first_tag2_on(payload):
        writes.append(payload)
        if payload == b"STREAM TAG2 ON\n":
            return len(payload) - 1
        return original_write(payload)

    s.write = short_first_tag2_on
    with pytest.raises(DisconnectedError, match="no longer reachable"):
        t.start()
    assert writes == [b"STREAM TAG2 ON\n", b"STREAM TAG2 OFF\n"]
    assert t.stream_boot_id is None and not t._streaming


def test_short_tag_v2_off_write_is_not_reported_as_a_successful_stop():
    config = {**CFG_V6, "fw_rev": "0.9.13", "tag_ver_max": 2, "boot_id": BOOT_A}
    s = FakeSerial(config, stream=tagged_v2_burst(1))
    t = UsbTransport(s)
    t.read_config(interval=0.01, drain=0)
    t.start()
    original_write = s.write

    def short_tag2_off(payload):
        if payload == b"STREAM TAG2 OFF\n":
            return 0
        return original_write(payload)

    s.write = short_tag2_off
    with pytest.raises(DisconnectedError, match="no longer reachable"):
        t.stop()
    assert t._streaming is True
