"""Reply-free USB liveness keepalive. No hardware."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo import _usb, _wire as w
from oglo._usb import DisconnectedError, UsbTransport


PING_CFG = {**CFG_V6, "fw_rev": "0.9.16", "link_ping": True}


def wait_for(predicate, *, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    assert predicate(), "condition did not become true before the test deadline"


def pings(serial: FakeSerial) -> int:
    return serial.commands.count("LINK PING")


@pytest.fixture
def fast_ping(monkeypatch):
    monkeypatch.setattr(_usb, "_LINK_PING_INTERVAL_S", 0.02)


def test_real_port_opener_bounds_keepalive_writes(monkeypatch):
    import serial as pyserial

    class Port:
        closed = False
        opened = False

        def open(self) -> None:
            self.opened = True

        def close(self) -> None:
            self.closed = True

    port = Port()
    monkeypatch.setattr(pyserial, "Serial", lambda: port)
    assert _usb.open_serial("/dev/fake-oglo", settle=0) is port
    assert port.opened is True
    assert port.timeout == 0.05
    assert port.write_timeout == 0.5
    assert port.dtr is True and port.rts is False


@pytest.mark.parametrize("cfg", [CFG_V6, PING_CFG])
def test_commands_and_close_never_wait_on_unbounded_serial_flush(cfg):
    """macOS tcdrain can remain blocked after a dead endpoint accepts write()."""
    class DeadDrainSerial(FakeSerial):
        def flush(self):
            raise AssertionError("unbounded USB drain was called")

    serial = DeadDrainSerial(cfg, stream=tagged_burst(4))
    transport = UsbTransport(serial)
    try:
        info, _ = transport.read_config(interval=0.01, drain=0)
        assert info.fw_rev == cfg["fw_rev"]
        assert transport.read_status(timeout=0.2).healthy
        transport.start()
        assert any(transport.poll() for _ in range(10))
        transport.stop()
    finally:
        transport.close()
    assert serial.closed


@pytest.mark.parametrize("failure", ["timeout", "short"])
def test_failed_manual_write_blocks_later_commands_and_close_stop(failure):
    class FailedCommandSerial(FakeSerial):
        attempts = 0

        def write(self, data):
            self.attempts += 1
            if data.strip() == b"GET STATUS":
                if failure == "timeout":
                    raise OSError("write timeout after a partial command")
                return 3
            return super().write(data)

    serial = FailedCommandSerial(CFG_V6)
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    with pytest.raises(DisconnectedError):
        transport.send("GET STATUS")
    attempts = serial.attempts
    with pytest.raises(DisconnectedError):
        transport.send("GET CONFIG")
    transport.close()
    assert serial.attempts == attempts
    assert serial.closed


@pytest.mark.parametrize(
    "cfg",
    [
        CFG_V6,
        # The earlier final2 test candidate has this version but no capability.
        {**CFG_V6, "fw_rev": "0.9.15"},
        {**CFG_V6, "fw_rev": "0.9.15", "link_ping": False},
        # Defense in depth: even a malformed old build advertising it stays off.
        {**CFG_V6, "fw_rev": "0.9.15", "link_ping": True},
    ],
)
def test_legacy_or_unadvertised_firmware_never_receives_link_ping(cfg, fast_ping):
    """Unknown commands become ASCII #ERR inside TAG on legacy firmware."""
    serial = FakeSerial(cfg, stream=tagged_burst(2))
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    time.sleep(0.07)
    assert pings(serial) == 0
    assert transport._keepalive_thread is None
    transport.close()


def test_ping_is_reply_free_and_independent_of_poll(fast_ping):
    class TimedSerial(FakeSerial):
        def __init__(self):
            super().__init__(PING_CFG, stream=tagged_burst(4))
            self.ping_times: list[float] = []

        def write(self, data: bytes) -> int:
            if data.strip() == b"LINK PING":
                self.ping_times.append(time.monotonic())
            return super().write(data)

    serial = TimedSerial()
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    try:
        transport.start()
        # Deliberately do not call poll(). This checks real worker independence,
        # not the shared CI host's sub-80 ms scheduling latency. Exact cadence and
        # missed-deadline behavior are checked with controlled time below.
        wait_for(lambda: len(serial.ping_times) >= 4, timeout=2.0)
        assert not serial._out.startswith(b"#")  # LINK PING added no text reply

        packets = []
        for _ in range(100):
            packets.extend(transport.poll())
            if any(isinstance(packet, w.TactilePacket) for packet in packets):
                break
        assert any(isinstance(packet, w.TactilePacket) for packet in packets)
        assert transport.dropped.malformed_usb == 0
    finally:
        transport.close()


@pytest.mark.parametrize("first_write_pause", [0.0, 3.5])
def test_ping_cadence_is_immediate_and_skips_missed_deadlines(monkeypatch, first_write_pause):
    clock = [0.0]
    ping_times = []
    waits = []
    workers = []

    class ClockedSerial(FakeSerial):
        def write(self, data):
            if data.strip() == b"LINK PING":
                ping_times.append(clock[0])
                if len(ping_times) == 1:
                    clock[0] += first_write_pause
            return super().write(data)

    class ClockedStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, delay):
            waits.append(delay)
            clock[0] += delay
            return self.stopped or len(waits) > 4

    class ControlledThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
            workers.append(self)

        def start(self):
            pass  # Run the actual worker loop synchronously after start() returns.

        def is_alive(self):
            return False

        def join(self, **kwargs):
            pass

    serial = ClockedSerial(PING_CFG)
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    # Replace only this module's references; real threading/time remain intact.
    monkeypatch.setattr(_usb, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=time.sleep,
    ))
    monkeypatch.setattr(_usb, "threading", SimpleNamespace(
        Event=ClockedStop, Thread=ControlledThread, current_thread=threading.current_thread,
    ))
    try:
        transport.start()
        assert len(workers) == 1
        workers[0].target()
        # First ping is immediate. A long write/scheduling pause skips all missed
        # deadlines instead of issuing a catch-up burst.
        assert ping_times == pytest.approx([
            0.0, first_write_pause + 1.0, first_write_pause + 2.0, first_write_pause + 3.0,
        ])
        assert waits == pytest.approx([0.0, 1.0, 1.0, 1.0, 1.0])
    finally:
        transport.close()


def test_stop_keeps_pinging_until_close_ends_the_usb_epoch(fast_ping):
    serial = FakeSerial(PING_CFG, stream=tagged_burst(2))
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: pings(serial) >= 2)
    worker = transport._keepalive_thread

    transport.stop()
    stopped_at = pings(serial)
    wait_for(lambda: pings(serial) > stopped_at)
    assert transport._keepalive_thread is not None

    transport.start()
    assert transport._keepalive_thread is worker
    restarted_at = pings(serial)
    wait_for(lambda: pings(serial) > restarted_at)
    transport.close()
    closed_at = pings(serial)
    time.sleep(0.06)
    assert pings(serial) == closed_at
    assert serial.closed is True
    assert transport._keepalive_thread is None


def test_failed_start_never_creates_an_authorizing_worker(fast_ping):
    class FailStartSerial(FakeSerial):
        fail_start = False

        def write(self, data: bytes) -> int:
            if self.fail_start and data.strip() == b"STREAM TAG ON":
                raise OSError("START failed")
            return super().write(data)

    serial = FailStartSerial(PING_CFG)
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    serial.fail_start = True
    with pytest.raises(DisconnectedError, match="STREAM TAG ON"):
        transport.start()
    time.sleep(0.05)
    assert pings(serial) == 0
    assert transport._keepalive_thread is None
    assert serial._streaming is False
    transport.close()


def test_failed_redundant_start_invalidates_session_before_another_ping(fast_ping):
    class FailNextStartSerial(FakeSerial):
        fail_next_start = False

        def write(self, data: bytes) -> int:
            if self.fail_next_start and data.strip() == b"STREAM TAG ON":
                self.fail_next_start = False
                raise OSError("repeated START failed")
            return super().write(data)

    serial = FailNextStartSerial(PING_CFG)
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: pings(serial) >= 2)
    worker = transport._keepalive_thread
    serial.fail_next_start = True
    with pytest.raises(DisconnectedError, match="STREAM TAG ON"):
        transport.start()
    assert transport._keepalive_thread is worker
    assert serial._streaming is True
    before = pings(serial)
    wait_for(lambda: not worker.is_alive())
    assert pings(serial) == before
    with pytest.raises(DisconnectedError, match="STREAM TAG ON"):
        transport.poll()
    transport.close()
    assert serial.closed


def test_reply_free_ping_does_not_break_a_paused_text_round_trip(fast_ping):
    serial = FakeSerial(PING_CFG, stream=tagged_burst(2))
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: pings(serial) >= 2)
    worker = transport._keepalive_thread

    transport.stop()
    transport.drain(settle=0)
    status = transport.read_status(timeout=0.2)
    assert status.healthy
    transport.start()
    assert transport._keepalive_thread is worker
    transport.close()


def test_reconnect_gets_a_fresh_worker_not_the_closed_session(fast_ping):
    old_serial = FakeSerial(PING_CFG)
    old = UsbTransport(old_serial)
    old.read_config(interval=0.01, drain=0)
    old.start()
    wait_for(lambda: pings(old_serial) >= 2)
    old.close()
    old_count = pings(old_serial)

    new_serial = FakeSerial(PING_CFG)
    new = UsbTransport(new_serial)
    new.read_config(interval=0.01, drain=0)
    new.start()
    wait_for(lambda: pings(new_serial) >= 2)
    time.sleep(0.04)
    assert pings(old_serial) == old_count
    assert pings(new_serial) >= 2
    new.close()


def test_two_gloves_own_independent_keepalive_workers(fast_ping):
    left_serial = FakeSerial(PING_CFG)
    right_serial = FakeSerial({**PING_CFG, "serial": "OGLO-R-TEST02", "side": "right",
                               "channels": list(reversed(PING_CFG["channels"]))})
    left = UsbTransport(left_serial)
    right = UsbTransport(right_serial)
    left.read_config(interval=0.01, drain=0)
    right.read_config(interval=0.01, drain=0)
    left.start()
    right.start()
    wait_for(lambda: pings(left_serial) >= 2 and pings(right_serial) >= 2)

    left.stop()
    left_count = pings(left_serial)
    right_count = pings(right_serial)
    wait_for(lambda: pings(left_serial) > left_count and pings(right_serial) > right_count)
    left.close()
    left_closed = pings(left_serial)
    time.sleep(0.05)
    assert pings(left_serial) == left_closed
    right_count = pings(right_serial)
    wait_for(lambda: pings(right_serial) > right_count)
    right.close()


@pytest.mark.parametrize("failure", ["raise", "short"])
def test_background_ping_write_failure_is_raised_by_poll(failure, fast_ping):
    class FailingPingSerial(FakeSerial):
        def write(self, data: bytes) -> int:
            if data.strip() == b"LINK PING":
                if failure == "raise":
                    raise OSError("CDC OUT failed")
                return 0
            return super().write(data)

    serial = FailingPingSerial(PING_CFG, stream=tagged_burst(2))
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: transport._keepalive_error is not None)
    with pytest.raises(DisconnectedError, match="LINK PING"):
        transport.poll()
    stop_count = serial.commands.count("STREAM TAG OFF")
    with pytest.raises(DisconnectedError, match="LINK PING"):
        transport.stop()
    transport.close()
    # A possibly partial LINK PING must not be concatenated with another command.
    assert serial.commands.count("STREAM TAG OFF") == stop_count
    assert serial.closed is True


def test_failed_stop_does_not_append_ping_to_uncertain_command(fast_ping):
    class FailOneStopSerial(FakeSerial):
        fail_stop = False

        def write(self, data: bytes) -> int:
            if self.fail_stop and data.strip() == b"STREAM TAG OFF":
                self.fail_stop = False
                raise OSError("one transient STOP failure")
            return super().write(data)

    serial = FailOneStopSerial(PING_CFG, stream=tagged_burst(2))
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: pings(serial) >= 2)
    serial.fail_stop = True
    with pytest.raises(DisconnectedError, match="STREAM TAG OFF"):
        transport.stop()
    assert serial._streaming is True
    before = pings(serial)
    wait_for(lambda: not transport._keepalive_thread.is_alive())
    assert pings(serial) == before
    with pytest.raises(DisconnectedError, match="STREAM TAG OFF"):
        transport.poll()
    transport.close()
    assert serial.closed


def test_non_owning_transport_never_opts_in_because_close_cannot_lower_dtr(fast_ping):
    serial = FakeSerial(PING_CFG)
    transport = UsbTransport(serial, owns_port=False)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    time.sleep(0.06)
    assert pings(serial) == 0
    transport.close()
    assert serial.closed is False


def test_owned_close_revokes_recovery_when_os_close_retains_dtr(fast_ping):
    """Linux without HUPCL can retain the firmware's recovery authorization."""
    class RetainedDtrSerial(FakeSerial):
        _dtr = True
        rts = False
        recovery_authorized = False
        dtr_at_close = None

        @property
        def dtr(self):
            return self._dtr

        @dtr.setter
        def dtr(self, value):
            self._dtr = value
            if not value:
                self.recovery_authorized = False

        def write(self, data):
            if data.strip() == b"LINK PING":
                self.recovery_authorized = True
            return super().write(data)

        def close(self):
            self.dtr_at_close = self.dtr
            super().close()  # Models an OS close that does not lower DTR.

    serial = RetainedDtrSerial(PING_CFG)
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: serial.recovery_authorized)
    transport.close()
    assert serial.closed
    assert serial.dtr_at_close is False
    assert serial.recovery_authorized is False
    assert serial.rts is False


def test_non_owned_close_preserves_callers_dtr():
    serial = FakeSerial(PING_CFG)
    serial.dtr = True
    transport = UsbTransport(serial, owns_port=False)
    transport.close()
    assert serial.dtr is True
    assert serial.closed is False


def test_close_releases_handle_even_if_lowering_dtr_fails():
    class FailedControlSerial(FakeSerial):
        dtr_attempts = 0

        @property
        def dtr(self):
            return True

        @dtr.setter
        def dtr(self, value):
            self.dtr_attempts += 1
            raise OSError("USB control request failed")

    serial = FailedControlSerial(PING_CFG)
    transport = UsbTransport(serial)
    transport.close()
    assert serial.dtr_attempts == 1
    assert serial.closed


def test_firmware_update_requires_a_dedicated_session_without_ping_bytes(fast_ping):
    serial = FakeSerial(PING_CFG)
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    wait_for(lambda: pings(serial) >= 2)
    before = list(serial.commands)

    with pytest.raises(_usb.UsbError, match="dedicated updater"):
        transport.send("FW BEGIN 1 part hw 0.9.16 123 sha key signature")
    assert not any(command.startswith("FW BEGIN") for command in serial.commands)
    assert serial.commands[:len(before)] == before

    # A read-only updater capability query is not a raw-image transition.
    transport.send("GET FWINFO")
    assert "GET FWINFO" in serial.commands
    transport.close()


def test_ping_and_manual_commands_are_serialized_on_one_writer_lock(fast_ping):
    class BlockingPingSerial(FakeSerial):
        def __init__(self):
            super().__init__(PING_CFG)
            self.ping_entered = threading.Event()
            self.release_ping = threading.Event()
            self.status_entered = threading.Event()

        def write(self, data: bytes) -> int:
            if data.strip() == b"LINK PING" and not self.ping_entered.is_set():
                self.ping_entered.set()
                assert self.release_ping.wait(0.5)
            if data.strip() == b"GET STATUS":
                self.status_entered.set()
            return super().write(data)

    serial = BlockingPingSerial()
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    transport.start()
    assert serial.ping_entered.wait(0.2)

    sender = threading.Thread(target=transport.send, args=("GET STATUS",))
    sender.start()
    time.sleep(0.03)
    assert not serial.status_entered.is_set(), "manual command raced the in-flight ping"
    serial.release_ping.set()
    sender.join(timeout=0.5)
    assert not sender.is_alive()
    assert serial.status_entered.is_set()
    transport.close()


def test_close_is_bounded_when_a_keepalive_write_never_returns(fast_ping, monkeypatch):
    monkeypatch.setattr(_usb, "_LINK_PING_JOIN_TIMEOUT_S", 0.03)

    class StuckPingSerial(FakeSerial):
        def __init__(self):
            super().__init__(PING_CFG)
            self.ping_entered = threading.Event()
            self.release_ping = threading.Event()

        def write(self, data: bytes) -> int:
            if data.strip() == b"LINK PING":
                self.ping_entered.set()
                self.release_ping.wait()
            return super().write(data)

    serial = StuckPingSerial()
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    closed = threading.Event()
    close_errors = []
    join_timeouts = []
    worker = None
    closer = None

    def close_transport():
        try:
            transport.close()
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            closed.set()

    try:
        transport.start()
        worker = transport._keepalive_thread
        assert serial.ping_entered.wait(2.0)
        stop_count = serial.commands.count("STREAM TAG OFF")
        real_join = worker.join

        def observed_join(timeout=None):
            join_timeouts.append(timeout)
            return real_join(timeout=timeout)

        monkeypatch.setattr(worker, "join", observed_join)
        closer = threading.Thread(target=close_transport)
        closer.start()
        # The write stays blocked until cleanup. Verify the exact join bound and
        # that close returns independently, without timing CI scheduler latency.
        assert closed.wait(2.0), "close waited for the stuck command write"
        assert not close_errors
        assert join_timeouts == [0.03]
        assert worker.is_alive()
        assert serial.closed is True
        assert serial.commands.count("STREAM TAG OFF") == stop_count
    finally:
        serial.release_ping.set()
        if closer is not None:
            closer.join(timeout=2.0)
        if worker is not None:
            threading.Thread.join(worker, timeout=2.0)
        transport.close()
    assert not worker.is_alive()


def test_shipping_interval_has_margin_below_the_two_second_contract():
    assert 0 < _usb._LINK_PING_INTERVAL_S < 2.0


@pytest.mark.parametrize("failure", ["raise", "short"])
def test_command_waiting_behind_failed_ping_cannot_append_to_partial_line(failure, monkeypatch):
    class FailingSerial(FakeSerial):
        def __init__(self):
            super().__init__(PING_CFG)
            self.ping_entered = threading.Event()
            self.release_ping = threading.Event()

        def write(self, data: bytes) -> int:
            if data.strip() == b"LINK PING":
                self.ping_entered.set()
                assert self.release_ping.wait(2.0)
                if failure == "raise":
                    raise OSError("partial ping followed by timeout")
                return 4
            return super().write(data)

    serial = FailingSerial()
    transport = UsbTransport(serial)
    transport.read_config(interval=0.01, drain=0)
    sender_waiting = threading.Event()
    write_command = transport._write_command

    def observed_write(command, **kwargs):
        if command == "GET STATUS":
            sender_waiting.set()
        return write_command(command, **kwargs)

    monkeypatch.setattr(transport, "_write_command", observed_write)
    errors = []

    def send_status():
        try:
            transport.send("GET STATUS")
        except Exception as exc:
            errors.append(exc)

    sender = threading.Thread(target=send_status)
    try:
        transport.start()
        assert serial.ping_entered.wait(1.0)
        sender.start()
        assert sender_waiting.wait(1.0)
        serial.release_ping.set()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert "GET STATUS" not in serial.commands
        assert len(errors) == 1
        assert isinstance(errors[0], DisconnectedError)
        assert "LINK PING" in str(errors[0])
    finally:
        serial.release_ping.set()
        if sender.ident is not None:
            sender.join(timeout=1.0)
        transport.close()
