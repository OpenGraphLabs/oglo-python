"""The axis tool must refuse a wrong answer, which is its whole job."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location(
    "measure_axes", Path(__file__).resolve().parent.parent / "tools" / "measure_axes.py")
ma = importlib.util.module_from_spec(spec)
sys.modules["measure_axes"] = ma
spec.loader.exec_module(ma)


def pose(axis, sign, mag=(0.0, 0.0, 0.0)):
    a = [0.0, 0.0, 0.0]
    a["xyz".index(axis)] = float(sign)
    return {"accel": a, "mag": list(mag), "axis": axis, "sign": sign, "norm": 1.0}


def good():
    """The arrangement section 2.3 of the frames doc predicts: IMU on the back face,
    so frame Z maps to -sensor z. Right-handed, so one in-plane axis inverts too."""
    return {
        "+X": pose("x", +1), "-X": pose("x", -1),
        "+Y": pose("y", -1), "-Y": pose("y", +1),
        "+Z": pose("z", -1), "-Z": pose("z", +1),
    }


def test_a_clean_set_of_poses_produces_a_permutation():
    R, problems = ma.build_matrix(good())
    assert problems == []
    assert ma.validate(R) == []
    assert np.linalg.det(R) == pytest.approx(1.0)
    assert "frame Z  =  -sensor z" in ma.render(R)


def test_the_sign_failing_to_invert_is_caught():
    """Doing the same pose twice instead of flipping the board."""
    p = good(); p["-Z"] = pose("z", -1)
    _, problems = ma.build_matrix(p)
    assert any("did not invert" in x for x in problems)


def test_a_different_axis_after_flipping_is_caught():
    p = good(); p["-Z"] = pose("x", +1)
    _, problems = ma.build_matrix(p)
    assert any("changed which sensor axis" in x for x in problems)


def test_a_missing_pose_is_caught():
    p = good(); del p["+Y"]
    _, problems = ma.build_matrix(p)
    assert any("both directions" in x for x in problems)


def test_reusing_one_sensor_axis_twice_is_caught():
    """Two different poses reported the same sensor axis, so it is not a permutation
    and one physical orientation was repeated."""
    p = good()
    p["+Y"], p["-Y"] = pose("z", +1), pose("z", -1)
    R, problems = ma.build_matrix(p)
    assert problems == []
    assert any("not a permutation" in x for x in ma.validate(R))


def test_a_left_handed_result_is_rejected_as_physically_impossible():
    """Mirroring one in-plane pose gives determinant -1: no rotation does that."""
    p = good()
    p["+Y"], p["-Y"] = pose("y", +1), pose("y", -1)
    R, problems = ma.build_matrix(p)
    assert problems == []
    out = ma.validate(R)
    assert any("left-handed" in x for x in out), out


@pytest.mark.parametrize("v,want", [
    ([0.02, -0.01, -0.99], (2, -1)),      # square on -z
    ([0.98, 0.03, 0.01], (0, 1)),         # square on +x
    ([0.55, 0.55, 0.55], None),           # held at a corner
    ([0.6, 0.5, 0.02], None),             # between two axes
    ([0.224, -0.976, 0.039], None),       # real reading: 13 deg off, propped at an angle
])
def test_only_a_square_pose_counts(v, want):
    assert ma.dominant_axis(np.array(v)) == want


def test_the_tilt_is_reported_in_degrees_so_it_can_be_acted_on():
    """A real board leaning against something measured [0.224, -0.976, 0.039]. The
    useful thing to tell someone is "13 degrees off", not a ratio."""
    i, off = ma.tilt_deg(np.array([0.224, -0.976, 0.039]))
    assert ma.AXES[i] == "y" and 12 < off < 14


def test_pose_table_is_self_consistent():
    """Every instruction must actually point the axis it claims at the ceiling.

    The first version had the two Y poses backwards. The tool caught the result as
    left-handed, correctly, but only after someone had done all six poses by hand.
    This checks the instructions against the frame definition instead.
    """
    for want, instruction, module_dir, usbc_dir in ma.POSES:
        got = ma.pose_up_axis(module_dir, usbc_dir)
        assert got == want, f"{instruction!r} points {got} at the ceiling, not {want}"


def test_the_six_poses_are_six_different_orientations():
    seen = {ma.pose_up_axis(m, u) for _, _, m, u in ma.POSES}
    assert seen == {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}


def test_the_reading_that_was_rejected_is_valid_once_the_labels_are_right():
    """The real measurement from OGLO-R-TEST04. Nothing was wrong with the board or
    the poses performed -- only with which label two of them carried."""
    as_performed = {
        "+Z": pose("z", -1), "-Z": pose("z", +1),
        "+X": pose("y", +1), "-X": pose("y", -1),
        "+Y": pose("x", -1), "-Y": pose("x", +1),   # instructions were swapped
    }
    R, _ = ma.build_matrix(as_performed)
    assert any("left-handed" in p for p in ma.validate(R))

    corrected = dict(as_performed, **{"+Y": pose("x", +1), "-Y": pose("x", -1)})
    R2, problems = ma.build_matrix(corrected)
    assert problems == [] and ma.validate(R2) == []
    assert np.linalg.det(R2) == pytest.approx(1.0)
    assert "frame X  =  +sensor y" in ma.render(R2)
    assert "frame Y  =  +sensor x" in ma.render(R2)
    assert "frame Z  =  -sensor z" in ma.render(R2)


def test_a_contaminated_field_is_called_out_rather_than_interpreted():
    """Real readings from OGLO-R-TEST04: |B| swung 0.70 to 1.15 G. Earth's field is
    ~0.5 G and rotating a sensor cannot change its strength, so these support no
    conclusion at all."""
    poses = {k: {"mag": v} for k, v in {
        "+Z": [0.029, -0.618, 0.571], "-Z": [0.314, -0.588, 0.942],
        "+X": [0.290, -0.602, 0.577], "-X": [0.240, -0.187, 1.035],
        "+Y": [0.259, -0.254, 0.594], "-Y": [-0.098, -0.545, 0.631],
    }.items()}
    out = ma.mag_verdict(poses)
    assert "too much" in out and "hard-iron" in out
    assert "out-of-plane axis" not in out, "it must not name an axis from bad data"


def test_a_uniform_field_still_does_not_resolve_the_in_plane_axes():
    """Even clean readings only fix up/down. Gravity has one direction."""
    poses = {k: {"mag": v} for k, v in {
        "+Z": [0.10, 0.20, 0.45], "-Z": [0.10, 0.20, -0.45],
        "+X": [0.45, 0.20, 0.10], "-X": [-0.45, 0.20, 0.10],
        "+Y": [0.20, 0.45, 0.10], "-Y": [0.20, -0.45, 0.10],
    }.items()}
    out = ma.mag_verdict(poses)
    assert "not resolved" in out and "compass heading" in out
    assert "too much" not in out
