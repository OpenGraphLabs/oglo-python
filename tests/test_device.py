"""Glove: streams, calibration, rates. Still no hardware."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from fake_serial import CFG_V6, FakeSerial, tagged_burst
from oglo._config import parse_config
from oglo._device import CalibrationLocked, DeviceError, Glove
from oglo._frame import CleanStreamError, Frame, ImuSample, MagSample
from oglo._usb import UsbTransport


def make(cfg=CFG_V6, stream=b"", **kw) -> tuple[Glove, FakeSerial]:
    s = FakeSerial(cfg, stream=stream, **kw)
    t = UsbTransport(s)
    info, caps = t.read_config(interval=0.01, drain=0)
    return Glove(t, info, caps), s


# --- streams --------------------------------------------------------------------


def test_tactile_yields_frames_with_a_5x4x4_grid():
    g, _ = make(stream=tagged_burst(6))
    got = [f for _, f in zip(range(4), g.tactile(timeout=2.0))]
    assert len(got) == 4 and all(isinstance(f, Frame) for f in got)
    assert got[0].counts.shape == (5, 4, 4)
    assert [f.seq for f in got] == [0, 1, 2, 3]


def test_the_three_streams_are_independent_and_keep_their_own_rates():
    g, _ = make(stream=tagged_burst(8))
    tac = [f for _, f in zip(range(4), g.tactile(timeout=2.0))]
    imu = [s for _, s in zip(range(8), g.imu(timeout=2.0))]
    mag = [m for _, m in zip(range(2), g.mag(timeout=2.0))]
    assert len(tac) == 4 and len(imu) == 8 and len(mag) == 2
    assert isinstance(imu[0], ImuSample) and isinstance(mag[0], MagSample)
    # IMU is twice tactile, mag a quarter of IMU: distinct sequences, not repeats.
    assert len({s.seq for s in imu}) == 8


def test_a_board_with_no_magnetometer_refuses_the_stream_instead_of_yielding_zeros():
    g, _ = make(cfg={**CFG_V6, "has_mag": False}, stream=tagged_burst(2))
    with pytest.raises(DeviceError, match="no magnetometer"):
        next(g.mag(timeout=1.0))


def test_an_unread_stream_is_bounded_and_counts_what_it_discarded():
    """Iterating tactile while ignoring imu must not accumulate 500 samples a second
    forever. Overflow is counted apart from wire loss: different cause, different fix."""
    g, _ = make(stream=tagged_burst(400))
    for _, _f in zip(range(300), g.tactile(timeout=2.0)):
        pass
    d = g.dropped
    assert d["overflow_imu"] > 0
    assert d["wire_imu"] == 0  # nothing was lost on the wire; we chose not to read it


def test_loss_is_reported_by_cause_not_as_one_number():
    g, _ = make(stream=tagged_burst(2))
    assert {
        "wire_tactile", "wire_imu", "wire_mag",
        "overflow_tactile", "overflow_imu", "overflow_mag",
    } < set(g.dropped)
    assert g.dropped["duplicate_tactile"] == 0
    assert g.dropped["backward_tactile"] == 0


def test_malformed_usb_tag_header_is_exposed_in_public_loss_counters():
    import struct

    from oglo import _wire as w

    bad = w.TAG_MAGIC + bytes([w.TAG_TACTILE]) + struct.pack("<HII", 999, 1, 1)
    g, _ = make(stream=bad + tagged_burst(1))
    assert next(g.tactile(timeout=1.0)).seq == 0
    assert g.dropped["transport_malformed_usb"] == 1


def test_rates_seen_measures_what_actually_arrived():
    g, _ = make(stream=tagged_burst(40))
    for _, _f in zip(range(20), g.tactile(timeout=2.0)):
        pass
    assert g.rates_seen["tactile"] > 0


# --- zero -----------------------------------------------------------------------


def test_zero_sends_sweep_with_its_argument_because_bare_sweep_is_another_command():
    """`SWEEP` alone matches the firmware's `"DIAG SWEEP" || "SWEEP"` branch and runs
    the settle-timing diagnostic. Only `SWEEP <n>` reaches the calibration handler."""
    g, s = make()
    g.zero(sweep=5)
    assert "SWEEP 5" in s.commands
    assert "SWEEP" not in [c.upper() for c in s.commands]


def test_zero_stops_the_stream_first_so_a_kilobyte_of_ascii_is_not_injected():
    g, s = make(stream=tagged_burst(2))
    next(g.tactile(timeout=1.0))
    g.zero(sweep=2)
    i_stop = s.commands.index("STREAM TAG OFF", 3)  # after the handshake's three
    i_sweep = s.commands.index("SWEEP 2")
    assert i_stop < i_sweep, "the stream must be stopped before the sweep"


def test_zero_waits_for_the_recipe_not_just_the_acknowledgement():
    g, s = make(sweep_completes=False)  # board acks, then never finishes
    with pytest.raises(DeviceError, match="#TZERO"):
        g.zero(sweep=1, timeout=0.4)


def test_zero_rejects_a_truncated_recipe_instead_of_accepting_the_prefix():
    g, s = make()
    s.zero_recipe["baseline"] = [550] * 79
    with pytest.raises(DeviceError, match="baseline must contain 80"):
        g.zero(sweep=1)


@pytest.mark.parametrize("missing", ["frames", "thr", "clean", "locked"])
def test_zero_recipe_never_invents_missing_firmware_fields(missing):
    g, s = make()
    del s.zero_recipe[missing]
    with pytest.raises(DeviceError, match=f"missing {missing}"):
        g.zero(sweep=1)


@pytest.mark.parametrize("field", ["valid", "clean", "locked"])
def test_zero_recipe_requires_json_booleans_not_truthy_integers(field):
    g, s = make()
    s.zero_recipe[field] = 1
    with pytest.raises(DeviceError, match=field):
        g.zero(sweep=1)


def test_zero_updates_public_stream_semantics_when_requested_clean_becomes_effective():
    g, s = make(
        cfg={**CFG_V6, "zero_valid": False, "stream_clean": False},
        stream=tagged_burst(2),
    )
    # Firmware retains the requested clean mode in NVS even while no zero exists;
    # after a successful sweep it becomes the effective wire mode.
    s.zero_recipe["clean"] = True
    g.zero(sweep=1)
    frame = next(g.tactile(timeout=1.0))
    assert g.info.stream_clean is True
    assert np.array_equal(frame.residual, frame.counts.astype(np.float32))


def test_a_locked_board_says_so_instead_of_appearing_to_succeed():
    g, s = make(locked=True)
    with pytest.raises(CalibrationLocked, match="(?i)locked"):
        g.zero(sweep=5)


@pytest.mark.parametrize("bad", [0, 31, -1])
def test_zero_rejects_a_sweep_length_the_firmware_would_silently_clamp(bad):
    g, _ = make()
    with pytest.raises(ValueError, match="1..30"):
        g.zero(sweep=bad)


@pytest.mark.parametrize("bad", [True, 1.9])
def test_zero_rejects_noninteger_sweep_values_before_sending_anything(bad):
    g, s = make()
    with pytest.raises(TypeError, match="integer"):
        g.zero(sweep=bad)
    assert not any(command.startswith("SWEEP ") for command in s.commands)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_public_waits_reject_nonfinite_or_invalid_timeouts(bad):
    g, _ = make()
    with pytest.raises((TypeError, ValueError)):
        g.read_batch(timeout=bad)
    with pytest.raises((TypeError, ValueError)):
        g.zero(sweep=1, timeout=bad)


# --- clean ----------------------------------------------------------------------


def test_clean_refuses_when_the_board_has_no_zero_to_apply():
    g, _ = make(cfg={**CFG_V6, "zero_valid": False, "stream_clean": False})
    with pytest.raises(DeviceError, match="no zero yet"):
        g.clean(threshold=30)


def test_clean_sets_the_threshold_then_the_mode():
    g, s = make()
    g.clean(threshold=30)
    assert s.commands.index("SET THR 30") < s.commands.index("SET STREAM CLEAN")


@pytest.mark.parametrize("bad", [-1, 4096, True, 1.5])
def test_clean_rejects_a_threshold_the_firmware_would_clamp_or_misread(bad):
    g, s = make()
    with pytest.raises((TypeError, ValueError)):
        g.clean(bad)
    assert not any(command.startswith("SET THR") for command in s.commands)


def test_a_clean_stream_does_not_subtract_a_second_baseline():
    g, _ = make(cfg={**CFG_V6, "stream_clean": True}, stream=tagged_burst(2))
    f = next(g.tactile(timeout=2.0))
    assert np.allclose(f.residual, f.counts.astype(np.float32))


def test_a_raw_stream_has_no_residual():
    g, _ = make(cfg={**CFG_V6, "stream_clean": False}, stream=tagged_burst(2))
    f = next(g.tactile(timeout=2.0))
    with pytest.raises(CleanStreamError):
        _ = f.residual


# --- rates ----------------------------------------------------------------------


def test_tactile_rate_is_set_in_hz():
    g, s = make()
    g.rates(tactile=300)
    assert "SET RATE 300" in s.commands


def test_imu_rate_is_converted_to_the_whole_millisecond_period_the_device_wants():
    g, s = make()
    out = g.rates(imu=1000)
    assert "SET IMURATE 1" in s.commands and out["imu_actual_hz"] == 1000.0


def test_an_imu_rate_the_device_cannot_represent_is_refused_not_rounded():
    """The period is whole milliseconds, so 400 Hz does not exist. Silently giving
    333 Hz would put a number in a dataset the user never chose."""
    g, _ = make()
    with pytest.raises(ValueError, match=r"nearest is 333\.3 Hz"):
        g.rates(imu=400)


def test_imu_rate_below_the_firmware_minimum_is_refused_before_it_can_be_clamped():
    g, s = make()
    with pytest.raises(ValueError, match="10..1000"):
        g.rates(imu=4)
    assert not any(command.startswith("SET IMURATE") for command in s.commands)


def test_the_magnetometer_rate_is_refused_because_it_is_derived_not_settable():
    """The firmware computes the mag period from the IMU period. Accepting the
    argument and ignoring it would be worse than refusing."""
    g, _ = make()
    with pytest.raises(DeviceError, match="not independently settable"):
        g.rates(mag=155)


# --- escape hatch and lifecycle --------------------------------------------------


def test_send_passes_an_arbitrary_command_through():
    g, s = make()
    g.send("DIAG I2C")
    assert "DIAG I2C" in s.commands


def test_send_refreshes_config_and_demux_after_a_state_changing_escape_command():
    g, s = make(stream=tagged_burst(2), chunk=8192)
    assert g.info.stream_clean is True
    g.send("SET STREAM RAW", expect="#STREAM")
    assert s.config["stream_clean"] is False
    assert g.info.stream_clean is False
    frame = next(g.tactile(timeout=1.0))
    with pytest.raises(CleanStreamError):
        _ = frame.residual


def test_partial_multi_command_failure_refreshes_the_state_that_did_apply():
    g, s = make()
    original = s._handle

    def wrong_imu_ack(command):
        if command.upper().startswith("SET IMURATE "):
            s._out += b"#IMURATE period_ms=99\r\n"
        else:
            original(command)

    s._handle = wrong_imu_ack
    with pytest.raises(DeviceError, match="different IMU period"):
        g.rates(tactile=300, imu=1000)
    assert s.config["rate_hz"] == 300
    assert g.info.rate_hz == 300, "SDK metadata stayed stale after partial application"


def test_failed_zero_readback_still_refreshes_effective_stream_semantics():
    g, s = make(
        cfg={**CFG_V6, "zero_valid": False, "stream_clean": False},
        stream=tagged_burst(2),
        chunk=8192,
    )
    s.zero_recipe["clean"] = True
    original = s._handle

    def corrupt_readback(command):
        if command.upper() == "GET ZERO":
            recipe = dict(s.zero_recipe)
            recipe["noise"] = list(recipe["noise"])
            recipe["noise"][0] += 1
            s._out += b"#TZERO " + json.dumps(recipe).encode() + b"\r\n"
        else:
            original(command)

    s._handle = corrupt_readback
    with pytest.raises(DeviceError, match="did not reproduce"):
        g.zero(sweep=1)
    assert g.info.zero_valid is True and g.info.stream_clean is True
    assert next(g.tactile(timeout=1.0))._stream_clean is True


def test_send_surfaces_a_board_error_rather_than_returning_it_as_text():
    g, s = make()
    with pytest.raises(DeviceError, match="#ERR"):
        g.send("NONSENSE", expect="#OK")


def test_send_without_an_expected_prefix_still_waits_for_and_raises_board_errors():
    g, _ = make()
    with pytest.raises(DeviceError, match="#ERR unknown command"):
        g.send("NONSENSE")


def test_the_glove_is_a_context_manager_and_stops_the_stream_on_exit():
    s = FakeSerial(CFG_V6, stream=tagged_burst(2))
    t = UsbTransport(s)
    info, caps = t.read_config(interval=0.01, drain=0)
    with Glove(t, info, caps) as g:
        next(g.tactile(timeout=1.0))
    assert s.closed and "STREAM TAG OFF" in s.commands


@pytest.mark.parametrize(
    "operation",
    [
        lambda g: g.status(),
        lambda g: g.send("DIAG I2C"),
        lambda g: g.raw(),
        lambda g: g.rates(tactile=250),
        lambda g: g.zero(sweep=1),
    ],
)
def test_every_command_path_fails_deterministically_after_close(operation):
    g, _ = make()
    g.close()
    with pytest.raises(DeviceError, match="closed"):
        operation(g)


def test_repr_says_which_glove_this_is():
    g, _ = make()
    r = repr(g)
    assert "OGLO-L-TEST01" in r and "left" in r and "0.9.9" in r


def test_read_batch_is_the_public_non_resampling_fanout_api():
    g, _ = make(stream=tagged_burst(4))
    got = {"tactile": [], "imu": [], "mag": []}
    for _ in range(20):
        batch = g.read_batch(timeout=1.0)
        for name, items in batch.as_dict().items():
            got[name].extend(items)
        if len(got["tactile"]) >= 4:
            break
    assert all(got.values())
    assert len(got["imu"]) > len(got["tactile"]) > len(got["mag"])


def test_usb_device_time_unwraps_the_32_bit_rollover_without_inventing_host_spacing():
    import struct
    from fake_serial import COUNTS, tag
    from oglo import _wire as w

    stream = (
        tag(w.TAG_TACTILE, 0xFFFFFFFF, 0xFFFFFFF0, w.pack12(COUNTS))
        + tag(w.TAG_TACTILE, 0, 0x20, w.pack12(COUNTS))
    )
    g, _ = make(stream=stream, chunk=len(stream))
    batch = g.read_batch(timeout=1.0)
    first, second = batch.tactile
    assert [first.t_us, second.t_us] == [0xFFFFFFF0, 0x20]
    assert second.device_time_us - first.device_time_us == 48
    assert second.host_t_ns == first.host_t_ns
    assert first.host_received_ns == second.host_received_ns


def test_ble_imu_uses_the_signed_imu_capture_offset_not_the_tactile_time():
    from oglo import _wire as w

    class OnePoll:
        def __init__(self):
            self.done = False

        def poll(self):
            if self.done:
                return []
            self.done = True
            return [w.BleSample(
                seq=1, t_us=50_000, counts=[550] * 80,
                accel=(0.0, 0.0, 1.0), gyro=(0.0, 0.0, 0.0),
                mag=None, imu_dt_us=-1500, host_received_ns=1_000_000_000,
            )]

    from oglo._stream import Demux
    d = Demux(OnePoll(), stream_clean=False)
    ready = d.drain_ready()
    tactile, imu = ready["tactile"][0], ready["imu"][0]
    assert tactile.device_time_us - imu.device_time_us == 1500
    assert tactile.host_t_ns == imu.host_t_ns


def test_stream_timeout_is_target_specific_while_other_modalities_keep_arriving():
    from oglo import _wire as w
    from oglo._stream import Demux

    class ImuOnly:
        seq = 0

        def poll(self):
            self.seq += 1
            return [w.ImuPacket(
                seq=self.seq,
                t_us=self.seq * 100,
                accel=(0.0, 0.0, 1.0),
                gyro=(0.0, 0.0, 0.0),
                raw=(0, 0, 4096, 0, 0, 0),
            )]

    demux = Demux(ImuOnly(), stream_clean=False)
    started = time.monotonic()
    assert list(demux.iterate("tactile", timeout=0.01)) == []
    assert time.monotonic() - started < 0.1


def test_first_ble_notify_just_after_wrap_never_produces_negative_device_time():
    from oglo import _wire as w
    from oglo._stream import Demux

    class OnePoll:
        done = False

        def poll(self):
            if self.done:
                return []
            self.done = True
            return [w.BleSample(
                seq=0, t_us=100, counts=[550] * 80,
                accel=(0.0, 0.0, 1.0), gyro=(0.0, 0.0, 0.0),
                imu_dt_us=-1500, host_received_ns=1_000_000_000,
            )]

    ready = Demux(OnePoll(), stream_clean=False).drain_ready()
    tactile, imu = ready["tactile"][0], ready["imu"][0]
    assert tactile.device_time_us == (1 << 32) + 100
    assert imu.device_time_us == (1 << 32) - 1400


def test_first_usb_batch_around_wrap_never_produces_negative_device_time():
    from fake_serial import COUNTS
    from oglo import _wire as w
    from oglo._stream import Demux

    packets = [
        w.TactilePacket(seq=0, t_us=100, counts=COUNTS, host_received_ns=1),
        w.ImuPacket(
            seq=0,
            t_us=0xFFFFFA88,
            accel=(0.0, 0.0, 1.0),
            gyro=(0.0, 0.0, 0.0),
            raw=(0, 0, 4096, 0, 0, 0),
            host_received_ns=1,
        ),
    ]

    class OnePoll:
        def poll(self):
            nonlocal packets
            out, packets = packets, []
            return out

    ready = Demux(OnePoll(), stream_clean=False).drain_ready()
    tactile, imu = ready["tactile"][0], ready["imu"][0]
    assert tactile.device_time_us == (1 << 32) + 100
    assert imu.device_time_us == (1 << 32) - 1400


def test_cross_poll_sensor_reordering_at_wrap_does_not_invent_a_71_minute_jump():
    from fake_serial import COUNTS
    from oglo import _wire as w
    from oglo._stream import Demux

    polls = [
        [w.TactilePacket(seq=0, t_us=100, counts=COUNTS, host_received_ns=1)],
        [w.ImuPacket(
            seq=0, t_us=0xFFFFFA88,
            accel=(0.0, 0.0, 1.0), gyro=(0.0, 0.0, 0.0),
            raw=(0, 0, 4096, 0, 0, 0), host_received_ns=2,
        )],
        [w.TactilePacket(seq=1, t_us=200, counts=COUNTS, host_received_ns=3)],
    ]

    class Polls:
        def poll(self):
            return polls.pop(0) if polls else []

    demux = Demux(Polls(), stream_clean=False)
    first = demux.drain_ready()["tactile"][0]
    older_imu = demux.drain_ready()["imu"][0]
    third = demux.drain_ready()["tactile"][0]
    assert older_imu.device_time_us == (1 << 32) - 1400
    assert first.device_time_us == (1 << 32) + 100
    assert third.device_time_us == (1 << 32) + 200
    assert third.device_time_us - older_imu.device_time_us == 1600


def test_clean_discards_samples_queued_under_the_previous_raw_semantics():
    g, _ = make(
        cfg={**CFG_V6, "stream_clean": False},
        stream=tagged_burst(8),
        chunk=8192,
    )
    first = next(g.tactile(timeout=1.0))
    with pytest.raises(CleanStreamError):
        _ = first.residual

    g.clean(30)
    batch = g.read_batch(timeout=1.0)
    assert batch.tactile
    assert all(frame._stream_clean for frame in batch.tactile)
    assert all(np.array_equal(frame.residual, frame.counts.astype(np.float32))
               for frame in batch.tactile)


def test_stop_start_discards_old_queue_and_resets_sequence_accounting():
    g, _ = make(stream=tagged_burst(4))
    first = g.read_batch(timeout=1.0)
    assert first.tactile
    g.stop()
    restarted = g.read_batch(timeout=1.0)
    assert restarted.tactile
    assert all(frame.dropped == 0 for frame in restarted.tactile)


def test_failed_usb_stop_is_not_reported_as_success_and_close_still_releases_port():
    g, s = make(stream=tagged_burst(2))
    next(g.tactile(timeout=1.0))
    original_write = s.write
    fail_once = True

    def write(data):
        nonlocal fail_once
        if fail_once and data.strip() == b"STREAM TAG OFF":
            fail_once = False
            raise OSError("USB write failed")
        return original_write(data)

    s.write = write
    with pytest.raises(Exception, match="no longer reachable"):
        g.stop()
    assert g._started is True and s._streaming is True
    g.close()
    assert s.closed is True
