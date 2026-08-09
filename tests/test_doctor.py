"""doctor: verdicts, not numbers. No hardware."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo import _usb, cli
from oglo._device import Glove
from oglo._doctor import FAIL, OK, WARN, Report, doctor
from oglo._usb import UsbTransport


def _ports(monkeypatch, entries):
    fake = SimpleNamespace(comports=lambda: entries)
    import serial.tools
    monkeypatch.setattr(serial.tools, "list_ports", fake, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "serial.tools.list_ports", fake)


def port(device, vid=_usb.SEEED_VID, serial_number="68EE8F000000", product="XIAO_ESP32S3"):
    return SimpleNamespace(device=device, vid=vid, pid=0x56, serial_number=serial_number,
                           product=product, manufacturer="Espressif Systems")


NIIMBOT = port("/dev/cu.usbmodemB1", vid=0x3513, serial_number="B1-TEST00001",
               product="B1 LABEL PRINTER")


def fake_connect(cfg=CFG_V6, n=4, hz=250.0):
    def _c(*a, **kw):
        s = FakeSerial(cfg, stream=tagged_burst(n), hz=hz)
        t = UsbTransport(s)
        info, caps = t.read_config(interval=0.01, drain=0)
        return Glove(t, info, caps)
    return _c


def find(rep: Report, needle: str):
    return [c for c in rep.checks if needle in c.name]


def test_no_glove_is_a_failure_that_names_the_cable(monkeypatch):
    _ports(monkeypatch, [NIIMBOT])
    rep = doctor(seconds=0.05)
    assert rep.worst == FAIL
    assert "cable" in find(rep, "no glove found")[0].detail


def test_other_serial_devices_are_listed_as_ignored_not_hidden(monkeypatch):
    """One of these once blocked a probe for two minutes. Saying it is present and
    deliberately skipped is more useful than silence."""
    _ports(monkeypatch, [NIIMBOT, port("/dev/cu.usbmodemA")])
    rep = doctor(seconds=0.05, connect=fake_connect())
    other = find(rep, "other serial device")
    assert other and "B1 LABEL PRINTER" in other[0].detail
    assert other[0].verdict == OK


def test_a_healthy_glove_passes_every_glove_check(monkeypatch):
    """Scoped to the glove, not the environment: this machine's system python is 3.9,
    which doctor correctly fails against the declared 3.10 floor."""
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])
    rep = doctor(seconds=1.0, connect=fake_connect())
    glove_checks = [c for c in rep.checks if "OGLO-" in c.name]
    assert glove_checks
    assert not [c for c in glove_checks if c.verdict == FAIL], str(rep)


def test_the_measured_rate_is_reported_against_what_the_board_says(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])
    rep = doctor(seconds=1.5, connect=fake_connect(hz=250.0))
    tac = [c for c in rep.checks if c.name.endswith("tactile rate")][0]
    assert tac.verdict == OK, tac.detail
    assert "250 Hz expected" in tac.detail


def test_fake_board_buffers_samples_while_host_is_descheduled(monkeypatch):
    """A busy test runner delays reads; it does not stop the simulated board clock."""
    import fake_serial
    from oglo import _wire as w

    now = [10.0]
    monkeypatch.setattr(fake_serial, "time", SimpleNamespace(monotonic=lambda: now[0]))
    serial = FakeSerial(CFG_V6, stream=tagged_burst(4), chunk=1_000_000, hz=250.0)
    serial.write(b"STREAM TAG ON\n")

    # Three more four-sample bursts should accumulate during a 49 ms host pause.
    now[0] += 0.049
    packets, remainder = w.iter_tagged(serial.read(1_000_000))
    tactile = [packet for packet in packets if isinstance(packet, w.TactilePacket)]
    assert not remainder
    assert len(tactile) == 16


def test_a_slow_board_is_failed_with_the_percentage(monkeypatch):
    """A number needs a reader who knows the expected value. A verdict does not."""
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])
    rep = doctor(seconds=1.5, connect=fake_connect(hz=100.0))  # board says 250, gives 100
    tac = [c for c in rep.checks if c.name.endswith("tactile rate")][0]
    assert tac.verdict == FAIL
    assert "%" in tac.detail and "another program reading the port" in tac.detail


def test_a_board_with_no_zero_is_warned_about_in_plain_words(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])
    rep = doctor(seconds=0.3, connect=fake_connect({
        **CFG_V6, "zero_valid": False, "stream_clean": False,
    }))
    c = find(rep, "no zero captured")[0]
    assert c.verdict == WARN and "550" in c.detail and "sweep" in c.detail


def test_bad_sensor_status_is_a_failure_not_healthy_looking_zero_data(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])

    def connect(*a, **kw):
        s = FakeSerial(CFG_V6, stream=tagged_burst(4), hz=250)
        s.status.update(imu_ok=False, sensor_ok=False, error_flags=4)
        s.status["imu"] = {**s.status["imu"], "ok": False}
        t = UsbTransport(s)
        info, caps = t.read_config(interval=0.01, drain=0)
        return Glove(t, info, caps)

    rep = doctor(seconds=0.1, connect=connect)
    health = find(rep, "sensor health")[0]
    assert health.verdict == FAIL and "error_flags=4" in health.detail


def test_malformed_usb_tag_header_is_a_doctor_failure(monkeypatch):
    import struct

    from oglo import _wire as w

    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])

    def connect(*a, **kw):
        bad = w.TAG_MAGIC + bytes([w.TAG_IMU]) + struct.pack("<HII", 99, 1, 1)
        s = FakeSerial(CFG_V6, stream=bad + tagged_burst(4), hz=250)
        t = UsbTransport(s)
        info, caps = t.read_config(interval=0.01, drain=0)
        return Glove(t, info, caps)

    rep = doctor(seconds=0.1, connect=connect)
    malformed = find(rep, "undecodable/unrouted packets")[0]
    assert malformed.verdict == FAIL
    assert "malformed USB TAG headers 1" in malformed.detail


def test_a_glove_that_will_not_open_is_reported_not_swallowed(monkeypatch):
    _ports(monkeypatch, [port("/dev/cu.usbmodemA")])

    def boom(*a, **kw):
        raise RuntimeError("port already held by PID 1234")

    rep = doctor(seconds=0.05, connect=boom)
    c = find(rep, "could not be opened")[0]
    assert c.verdict == FAIL and "PID 1234" in c.detail


def test_the_report_rolls_up_to_the_worst_verdict():
    r = Report()
    r.add("a", OK); assert r.worst == OK
    r.add("b", WARN); assert r.worst == WARN
    r.add("c", FAIL); assert r.worst == FAIL


@pytest.mark.parametrize("seconds", [True, 0, -1, float("nan"), float("inf"), "1"])
def test_doctor_rejects_invalid_durations_before_discovery(seconds):
    from oglo._doctor import doctor

    with pytest.raises(ValueError, match="finite real.*greater than zero"):
        doctor(seconds)


# --- CLI -------------------------------------------------------------------------


def test_doctor_exits_nonzero_when_something_failed(monkeypatch, capsys):
    _ports(monkeypatch, [NIIMBOT])
    assert cli.main(["doctor", "--seconds", "0.05"]) == 2
    assert "no glove found" in capsys.readouterr().out


def test_replay_prints_a_summary(tmp_path, capsys):
    from oglo import record, replay
    g = fake_connect()()
    try:
        ep = record(tmp_path, seconds=0.5, glove=g)
    finally:
        g.close()
    assert cli.main(["replay", str(ep)]) == 0
    out = capsys.readouterr().out
    assert "tactile" in out and "OGLO-L-TEST01" in out


def test_replay_json_is_machine_readable(tmp_path, capsys):
    import json
    from oglo import record
    g = fake_connect()()
    try:
        ep = record(tmp_path, seconds=0.5, glove=g)
    finally:
        g.close()
    cli.main(["replay", str(ep), "--json"])
    assert json.loads(capsys.readouterr().out)["tactile"]["n"] > 0


def test_replay_cli_makes_an_incomplete_episode_visible_and_nonzero(tmp_path, capsys):
    import json
    from oglo import record

    g = fake_connect()()
    try:
        ep = record(tmp_path, seconds=0.5, glove=g)
    finally:
        g.close()
    meta_path = ep / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta.update(complete=False, error="simulated disconnect")
    meta_path.write_text(json.dumps(meta))

    assert cli.main(["replay", str(ep)]) == 2
    out = capsys.readouterr().out
    assert "INCOMPLETE EPISODE" in out and "simulated disconnect" in out
    assert cli.main(["replay", str(ep), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_a_bad_path_fails_cleanly_instead_of_a_traceback(tmp_path, capsys):
    assert cli.main(["replay", str(tmp_path / "nope")]) == 1
    assert "ReplayError" in capsys.readouterr().err


def test_info_json_is_one_parseable_document_with_two_gloves(monkeypatch, capsys):
    import json
    import oglo

    candidates = [
        SimpleNamespace(device="/dev/a"),
        SimpleNamespace(device="/dev/b"),
    ]
    monkeypatch.setattr("oglo._usb.list_candidates", lambda: candidates)

    class G:
        def __init__(self, port):
            side = "left" if port == "/dev/a" else "right"
            self.info = SimpleNamespace(
                raw={"serial": f"OGLO-{side}"}, serial=f"OGLO-{side}", side=side,
                transport="usb", hw_rev="D", fw_rev="0.9.10", rate_hz=250,
                channels=[], has_mag=True, zero_valid=True, stream_clean=True,
                stream_thr=30,
            )

        def close(self):
            pass

    monkeypatch.setattr(oglo, "connect", lambda *, port: G(port))
    assert cli.main(["info", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list) and len(parsed) == 2
