#!/usr/bin/env python3
"""Measure which sensor axis points which way. Six poses, about two minutes.

Why this exists: accelerometer/gyroscope axes must be measured, not guessed from a
board drawing. A 3x3 signed permutation has 48 possible values, and a wrong mapping
still produces plausible motion. The SDK includes the mapping measured on two boards;
this tool makes that result reproducible. It still does not create a fused orientation
because magnetometer axes and calibration remain unresolved.

    python3 tools/measure_axes.py

WHAT FRAME THIS PRODUCES. Not the KiCad board frame -- you cannot see KiCad's axes
while holding a board. A frame defined by two things you can point at:

    +Z   out of the face the XIAO module is mounted on ("module side")
    +X   toward the USB-C connector
    +Y   +Z cross +X, which makes it right-handed

Composing that with the CAD frame is a separate, documented step; this measures the
part nobody can do from a desk.

HOW IT WORKS. Gravity is a known vector: an accelerometer at rest reads +1 g along
whichever axis points **up**. Put the board in six orientations, read which sensor
axis sees the gravity, and the mapping falls out. The script refuses any pose where
the board moved, where no axis clearly dominates, or where the six poses do not
compose into a valid permutation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

import oglo  # noqa: E402

AXES = ("x", "y", "z")

#: Each pose names the frame axis that ends up pointing at the ceiling, alongside the
#: orientation as (module-side direction, USB-C direction) in a world frame where
#: East=(1,0,0), North=(0,1,0), Up=(0,0,1). The vectors are not decoration: they are
#: what `test_pose_table_is_self_consistent` checks the labels against.
#:
#: The two Y poses were written backwards in the first version and a real measurement
#: came back left-handed because of it. `+Y = +Z x +X`, so with the module side facing
#: you (+Z south) and USB-C to your left (+X west), +Y is (0,0,-1) -- pointing at the
#: floor, not the ceiling. The tool rejected the run and guessed the cause correctly,
#: but a person had already spent two minutes on it.
POSES: List[Tuple[str, str, Tuple, Tuple]] = [
    ("+Z", "Lay the board FLAT with the MODULE SIDE FACING UP.",
     (0, 0, 1), (0, 1, 0)),
    ("-Z", "Flip it over: FLAT, MODULE SIDE FACING DOWN.",
     (0, 0, -1), (0, 1, 0)),
    ("+X", "Stand it on an edge with the USB-C CONNECTOR POINTING UP at the ceiling.",
     (0, -1, 0), (0, 0, 1)),
    ("-X", "Stand it on an edge with the USB-C CONNECTOR POINTING DOWN at the desk.",
     (0, -1, 0), (0, 0, -1)),
    ("+Y", "Module side toward you, USB-C pointing RIGHT. Stand it on the edge.",
     (0, -1, 0), (1, 0, 0)),
    ("-Y", "Module side toward you, USB-C pointing LEFT. Stand it on the edge.",
     (0, -1, 0), (-1, 0, 0)),
]


def pose_up_axis(module_dir, usbc_dir) -> str:
    """Which frame axis a described orientation actually points at the ceiling.

    Derived from the frame definition rather than from anyone's spatial reasoning,
    which is what got the Y poses wrong the first time.
    """
    Z = np.asarray(module_dir, dtype=float)
    X = np.asarray(usbc_dir, dtype=float)
    for name, v in (("X", X), ("Y", np.cross(Z, X)), ("Z", Z)):
        d = float(v @ np.array([0.0, 0.0, 1.0]))
        if abs(d) > 0.5:
            return f"{'+' if d > 0 else '-'}{name}"
    raise AssertionError("no frame axis points up in this orientation")

#: How far the board may lean off an axis and still count, in degrees. Expressed as
#: an angle because that is a thing a person can act on ("it is 13 degrees off"); the
#: first version compared against a sum of absolute values, which told nobody anything.
MAX_TILT_DEG = 12.0
#: Gravity should be near 1 g. Outside this the board was not still, or is being held.
G_LOW, G_HIGH = 0.85, 1.15
#: Above this the board is still moving; deg/s.
STILL_GYRO = 8.0


def read_still(g, seconds: float = 2.0) -> Tuple[np.ndarray, np.ndarray, float]:
    """Average accel and mag while checking the board is actually still."""
    acc, mag, gyro = [], [], []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        # Drain every ready stream independently. Alternating next(imu), next(mag)
        # throttles the faster IMU to the magnetometer and overflows its queue.
        batch = g.read_batch(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
        for sample in batch.imu:
            acc.append(sample.accel)
            gyro.append(sample.gyro)
        for sample in batch.mag:
            mag.append(sample.field)
    if not acc:
        raise RuntimeError("no IMU samples arrived during the pose")
    if g.info.has_mag and not mag:
        raise RuntimeError("board reports has_mag=true but no magnetometer samples arrived")
    a = np.array(acc).mean(axis=0)
    m = np.array(mag).mean(axis=0) if mag else np.zeros(3)
    worst_rotation = float(np.abs(np.array(gyro)).max())
    return a, m, worst_rotation


def tilt_deg(v: np.ndarray) -> Tuple[int, float]:
    """(index of the nearest axis, angle away from it in degrees)."""
    n = float(np.linalg.norm(v))
    if n == 0:
        return 0, 90.0
    i = int(np.abs(v).argmax())
    return i, float(np.degrees(np.arccos(min(1.0, abs(v[i]) / n))))


def dominant_axis(v: np.ndarray) -> Optional[Tuple[int, int]]:
    """(index, sign) of the axis carrying the vector, or None if it leans too far."""
    i, off = tilt_deg(v)
    if off > MAX_TILT_DEG:
        return None
    return i, (1 if v[i] > 0 else -1)


def measure(g, seconds: float, assume_yes: bool) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for want, instruction, _m, _u in POSES:
        while True:
            print(f"\n  {want}:  {instruction}")
            if not assume_yes:
                input("       Let go, keep it still, then press Enter: ")
            a, m, rot = read_still(g, seconds)
            norm = float(np.linalg.norm(a))
            hit = dominant_axis(a)

            if rot > STILL_GYRO:
                print(f"       moving ({rot:.0f} deg/s). Let it settle and try again.")
                continue
            if not (G_LOW <= norm <= G_HIGH):
                print(f"       |a| = {norm:.2f} g, expected ~1.00. Is it resting on something?")
                continue
            if hit is None:
                i, off = tilt_deg(a)
                print(f"       leaning {off:.0f} degrees off {AXES[i]} (limit {MAX_TILT_DEG:.0f}). "
                      "Sit it squarely against something flat, do not prop it at an angle.")
                continue

            i, sign = hit
            _, off = tilt_deg(a)
            print(f"       ok: sensor {AXES[i]} reads {a[i]:+.2f} g   "
                  f"({off:.0f} deg off axis, |a| {norm:.2f}, still)")
            out[want] = {"accel": a.tolist(), "mag": m.tolist(),
                         "axis": AXES[i], "sign": sign, "norm": norm}
            break
    return out


def build_matrix(poses: Dict[str, dict]) -> Tuple[np.ndarray, List[str]]:
    """Signed permutation mapping sensor axes -> the landmark frame, plus complaints.

    Pose "+Z" points frame +Z at the ceiling, and the accelerometer reads +1 g along
    whatever sensor axis points up, so that pose identifies the sensor axis carrying
    frame Z and its sign directly.
    """
    R = np.zeros((3, 3))
    problems: List[str] = []
    for k, frame_axis in enumerate("XYZ"):
        plus, minus = poses.get(f"+{frame_axis}"), poses.get(f"-{frame_axis}")
        if not plus or not minus:
            problems.append(f"frame {frame_axis} was not measured in both directions")
            continue
        if plus["axis"] != minus["axis"]:
            problems.append(
                f"frame {frame_axis}: flipping the board changed which sensor axis "
                f"saw gravity ({plus['axis']} then {minus['axis']}). One of the two "
                "poses was wrong."
            )
            continue
        if plus["sign"] == minus["sign"]:
            problems.append(
                f"frame {frame_axis}: the sign did not invert when flipped "
                f"({plus['sign']:+d} both times). One of the two poses was wrong."
            )
            continue
        R[k, AXES.index(plus["axis"])] = plus["sign"]
    return R, problems


def validate(R: np.ndarray) -> List[str]:
    """A real answer is a signed permutation with determinant +1."""
    problems = []
    if not np.all(np.isin(R, (-1.0, 0.0, 1.0))):
        problems.append("matrix has entries other than 0 and +/-1")
    if not (np.abs(R).sum(axis=1) == 1).all() or not (np.abs(R).sum(axis=0) == 1).all():
        problems.append(
            "not a permutation: some sensor axis is used twice or not at all. "
            "Two poses were probably the same orientation."
        )
        return problems
    det = float(np.linalg.det(R))
    if abs(abs(det) - 1) > 1e-6:
        problems.append(f"determinant {det:.3f}, expected +/-1")
    elif det < 0:
        problems.append(
            f"determinant {det:+.0f}: this maps a right-handed frame to a left-handed "
            "one, which is physically impossible. A pose was mirrored -- most likely "
            "+Y and -Y were swapped."
        )
    return problems


#: Earth's field is about 0.5 G and its MAGNITUDE does not change when you rotate a
#: sensor. If it does, something local is adding to it, or the part has a large
#: hard-iron offset, and no axis conclusion drawn from those readings means anything.
MAG_SPREAD_LIMIT = 0.25   # fraction of the mean


def mag_verdict(poses: Dict[str, dict]) -> str:
    """What, if anything, the magnetometer readings support.

    The first version of this picked the axis that changed most when the board was
    flipped and called it the out-of-plane one. On two boards measured minutes apart
    that gave two different answers, and the margins were 1.30x and 1.03x -- the
    second is a coin toss. Flipping a board changes its heading as well as its
    up-vector, so the in-plane axes move too and the comparison never meant anything.

    A confident answer from noise is the exact failure this tool exists to prevent,
    so it is gone. What is left is the check that would have caught it.
    """
    norms = np.array([np.linalg.norm(p["mag"]) for p in poses.values()])
    mean = float(norms.mean())
    spread = float(norms.max() - norms.min()) / mean if mean else 1.0
    lines = [f"  Magnetometer: |B| ranged {norms.min():.2f} to {norms.max():.2f} G "
             f"(mean {mean:.2f}, spread {spread*100:.0f}%)."]
    if spread > MAG_SPREAD_LIMIT:
        lines.append("    That is too much. Rotating a sensor cannot change the strength of")
        lines.append("    Earth's field, so something magnetic is nearby or the part has a")
        lines.append("    large hard-iron offset. No axis conclusion is drawn from this.")
    else:
        lines.append("    Field looks uniform, but the axes still are not resolved: gravity")
        lines.append("    fixes up/down only. The in-plane pair needs a known compass heading.")
    return "\n".join(lines)


def render(R: np.ndarray) -> str:
    rows = []
    for k, frame_axis in enumerate("XYZ"):
        j = int(np.abs(R[k]).argmax())
        rows.append(f"    frame {frame_axis}  =  {'+' if R[k, j] > 0 else '-'}sensor {AXES[j]}")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default=None, help="which glove, if more than one is attached")
    ap.add_argument("--seconds", type=float, default=2.0, help="averaging time per pose")
    ap.add_argument("--out", default=None, help="where to write the result (default: alongside this tool)")
    ap.add_argument("--yes", action="store_true", help="do not wait for Enter (for a dry run)")
    args = ap.parse_args()

    g = oglo.connect(args.serial)
    try:
        print(f"\n  {g.info.serial}  ({g.info.side})  fw {g.info.fw_rev}")
        print(__doc__.split("HOW IT WORKS.")[0].split("WHAT FRAME THIS PRODUCES.")[1].strip())
        print("\n  Six poses. Rest the board on something flat each time and let go of it;\n"
              "  holding it adds your own hand tremor and the pose will be rejected.")

        poses = measure(g, args.seconds, args.yes)
        R, problems = build_matrix(poses)
        problems += validate(R) if not problems else []

        print("\n" + "=" * 62)
        if problems:
            print("  MEASUREMENT REJECTED\n")
            for p in problems:
                print(f"    - {p}")
            print("\n  Nothing was written. Re-run and check the pose descriptions.")
            return 2

        print("  R_frame_from_imu\n")
        print(render(R))
        print(f"\n{np.array2string(R.astype(int), prefix='    ')}")

        if g.info.has_mag:
            print("\n" + mag_verdict(poses))

        out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "spec" / "axes.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "serial": g.info.serial, "side": g.info.side, "fw_rev": g.info.fw_rev,
            "frame": {"+Z": "out of the module side", "+X": "toward the USB-C connector",
                      "+Y": "+Z cross +X (right-handed)"},
            "R_frame_from_imu": R.astype(int).tolist(),
            "poses": poses,
        }, indent=1) + "\n")
        print(f"\n  written to {out}")
        print("  This is one board. Repeat on a second before trusting it as the design.")
        return 0
    finally:
        g.close()


if __name__ == "__main__":
    raise SystemExit(main())
