"""Public sample types, and the two things they refuse to do."""

from __future__ import annotations

import numpy as np
import pytest

from oglo._frame import (FINGERS, SHAPE, CleanStreamError, Frame, ImuSample, MagSample,
                         counts_to_grid, oriented_counts)


def grid(fill=550):
    return np.full(SHAPE, fill, dtype=np.uint16)


def test_counts_are_a_5x4x4_grid_in_wire_order():
    g = counts_to_grid(list(range(80)))
    assert g.shape == SHAPE and g.dtype == np.uint16
    assert g[0, 0, 0] == 0 and g[0, 0, 3] == 3 and g[0, 1, 0] == 4 and g[4, 3, 3] == 79


def test_a_wrong_sized_count_list_is_rejected():
    with pytest.raises(ValueError):
        counts_to_grid(list(range(79)))


def test_a_frame_rejects_a_grid_of_the_wrong_shape():
    with pytest.raises(ValueError, match=r"\(5, 4, 4\)"):
        Frame(seq=1, t_us=1, host_t=1.0, counts=np.zeros((5, 4), dtype=np.uint16))


def test_a_frame_rejects_counts_outside_the_12_bit_adc_range():
    bad = grid()
    bad.flat[0] = 4096
    with pytest.raises(ValueError, match="12-bit"):
        Frame(seq=1, t_us=1, host_t=1.0, counts=bad)


# --- the double-subtraction refusal --------------------------------------------


def test_a_clean_stream_returns_counts_as_the_residual():
    """The device already subtracted its baseline and applied the deadband. Doing it
    again here produces plausible, quietly wrong numbers."""
    f = Frame(seq=1, t_us=1, host_t=1.0, counts=grid(30), _stream_clean=True)
    assert np.all(f.residual == 30.0)


def test_a_raw_stream_with_no_baseline_raises_rather_than_passing_off_raw_counts():
    f = Frame(seq=1, t_us=1, host_t=1.0, counts=grid(600))
    with pytest.raises(CleanStreamError, match="zero"):
        _ = f.residual
    # .counts is still there for anyone who genuinely wants raw ADC
    assert np.all(f.counts == 600)


def test_the_residual_error_names_the_fix():
    f = Frame(seq=1, t_us=1, host_t=1.0, counts=grid())
    with pytest.raises(CleanStreamError) as e:
        _ = f.residual
    assert "zero(sweep=5)" in str(e.value) and "clean" in str(e.value)


# --- the orientation refusal ----------------------------------------------------


def test_orientation_raises_and_says_why():
    f = Frame(seq=1, t_us=1, host_t=1.0, counts=grid())
    with pytest.raises(NotImplementedError) as e:
        _ = f.orientation
    msg = str(e.value)
    assert "axis rotation is measured" in msg and "magnetometer" in msg


def test_imu_orientation_raises_too():
    s = ImuSample(seq=1, t_us=1, host_t=1.0, accel=(0, 0, -1), gyro=(0, 0, 0))
    with pytest.raises(NotImplementedError):
        _ = s.orientation


# --- units ----------------------------------------------------------------------


def test_mag_magnitude_matches_earths_field_for_a_real_reading():
    """Anonymized bench reading from OGLO-RDR02A-TEST04: raw (2280, -1230, -2150)
    at 6842 LSB/gauss. Earth is roughly 0.5 G, which is what makes this a check on
    the scale factor rather than on arithmetic."""
    s = MagSample(
        seq=1, t_us=1, host_t=1.0,
        field=(2280 / 6842.0, -1230 / 6842.0, -2150 / 6842.0),
    )
    assert s.magnitude == pytest.approx(0.487, abs=0.01)


def test_a_frame_exposes_one_finger_at_a_time():
    g = counts_to_grid(list(range(80)))
    f = Frame(seq=1, t_us=1, host_t=1.0, counts=g)
    assert f.finger(0).shape == (4, 4)
    assert f.finger(4)[3, 3] == 79


def test_dropped_defaults_to_zero_and_is_carried_per_sample():
    assert Frame(seq=1, t_us=1, host_t=1.0, counts=grid()).dropped == 0
    assert Frame(seq=9, t_us=1, host_t=1.0, counts=grid(), dropped=3).dropped == 3


# --- the measured rotation --------------------------------------------------------


def test_the_measured_rotation_is_a_proper_rotation():
    from oglo import R_FRAME_FROM_IMU as R
    assert np.allclose(R @ R.T, np.eye(3))
    assert np.linalg.det(R) == pytest.approx(1.0)


def test_it_matches_what_both_boards_measured():
    """Six gravity poses on OGLO-R-TEST04 and OGLO-L-TEST01, 2026-08-07, identical."""
    from oglo import R_FRAME_FROM_IMU as R
    assert np.array_equal(R, np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]], dtype=np.float32))


def test_gravity_lands_where_the_poses_said_it_would():
    """Module side up: frame +Z points at the ceiling, so frame accel must read +1 g
    on Z. The raw sensor read -0.99 on its own z, which is the whole point of having
    a rotation."""
    s = ImuSample(seq=1, t_us=1, host_t=1.0, accel=(0.0, 0.0, -0.99), gyro=(0, 0, 0))
    assert s.accel_frame == pytest.approx((0.0, 0.0, 0.99), abs=1e-6)


def test_the_in_plane_swap_is_applied():
    s = ImuSample(seq=1, t_us=1, host_t=1.0, accel=(1.0, 2.0, 3.0), gyro=(4.0, 5.0, 6.0))
    assert s.accel_frame == pytest.approx((2.0, 1.0, -3.0))
    assert s.gyro_frame == pytest.approx((5.0, 4.0, -6.0))


def test_orientation_still_raises_because_the_magnetometer_is_unmeasured():
    s = ImuSample(seq=1, t_us=1, host_t=1.0, accel=(0, 0, -1), gyro=(0, 0, 0))
    with pytest.raises(NotImplementedError):
        _ = s.orientation


RIGHT_WIRE = ["thumb", "index", "middle", "ring", "pinky"]
LEFT_WIRE = ["pinky", "ring", "middle", "index", "thumb"]


def _wire_grid():
    # Every taxel carries its own wire index, so any re-indexing is visible.
    return counts_to_grid(list(range(80)))


def test_a_right_glove_is_already_in_physical_order():
    g = _wire_grid()
    assert np.array_equal(oriented_counts(g, RIGHT_WIRE, "right"), g)


def test_a_left_glove_gets_its_fingers_put_back_thumb_first():
    g = _wire_grid()
    phys = oriented_counts(g, LEFT_WIRE, "left")
    # wire slot 4 is the left thumb; index/middle/ring/pinky come straight across
    assert np.array_equal(phys[1], g[3]) and np.array_equal(phys[2], g[2])
    assert np.array_equal(phys[3], g[1]) and np.array_equal(phys[4], g[0])


def test_only_the_left_thumb_has_its_col_axis_reversed():
    g = _wire_grid()
    phys = oriented_counts(g, LEFT_WIRE, "left")
    thumb_wire = g[4]
    assert np.array_equal(phys[0], thumb_wire[:, ::-1])
    assert phys[0][0, 0] == thumb_wire[0, 3] and phys[0][3, 3] == thumb_wire[3, 0]
    # rows are untouched, and no other finger is flipped
    assert np.array_equal(phys[0][:, 0], thumb_wire[:, 3])
    assert np.array_equal(phys[4], g[0])


def test_a_right_thumb_is_not_flipped():
    g = _wire_grid()
    assert np.array_equal(oriented_counts(g, RIGHT_WIRE, "right")[0], g[0])


def test_oriented_counts_never_writes_back_into_the_wire_grid():
    g = _wire_grid()
    before = g.copy()
    out = oriented_counts(g, LEFT_WIRE, "left")
    out[:] = 0
    assert np.array_equal(g, before)


def test_oriented_counts_refuses_an_unknown_side_or_finger_list():
    g = _wire_grid()
    with pytest.raises(ValueError, match="side"):
        oriented_counts(g, RIGHT_WIRE, "both")
    with pytest.raises(ValueError, match="channels"):
        oriented_counts(g, ["thumb", "index", "middle", "ring", "ring"], "right")


def test_a_frame_orients_itself_from_the_glove_info():
    class Info:
        channels = LEFT_WIRE
        side = "left"

    f = Frame(seq=1, t_us=10, host_t=0.0, counts=_wire_grid())
    assert np.array_equal(f.oriented(Info()), oriented_counts(f.counts, LEFT_WIRE, "left"))
    assert tuple(FINGERS) == ("thumb", "index", "middle", "ring", "pinky")
