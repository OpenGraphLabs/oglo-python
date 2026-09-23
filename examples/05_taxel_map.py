#!/usr/bin/env python3
"""Live 5x(4x4) taxel map in the terminal, to check where a press lands.

Press one corner of one finger pad and read off which `[finger][row][col]` peaks.
Shows the WIRE layout exactly as the device sends it (finger order = info.channels,
col 0 = fingertip per the SDK) and the oriented position (`oriented_counts`) of the
peak, plus the flat wire index 0..79 for comparison with any other viewer.

Nothing is zeroed or switched on the glove. What the cells show, in order of
preference: the firmware CLEAN residual if the glove is already streaming clean; else
raw counts minus the sweep zero stored on the glove (`GET ZERO`, the same baseline
the app and collect.py use); else, with no valid zero, raw counts minus the
per-taxel median of the first second (keep the hand still and unloaded at start).

    scripts/taxel_map.sh                 # the only glove, or the first found
    scripts/taxel_map.sh --side left     # pick by hand
    scripts/taxel_map.sh --serial OGLO-L-00114
    scripts/taxel_map.sh --oriented      # canonical thumb-first layout instead of wire
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

import oglo
from oglo import FINGERS, oriented_counts
from oglo._wire import NUM_COLS, ROWS_PER_FINGER

BASELINE_FRAMES = 250  # 1 s at 250 Hz
CLEAR = "\x1b[H\x1b[2J"


def pick_serial(side: str | None) -> str | None:
    if side is None:
        return None
    for cand in oglo.list_candidates():
        with oglo.connect(port=cand.device) as g:
            if g.info.side == side:
                return g.info.serial
    sys.exit(f"no {side} glove found among {[c.device for c in oglo.list_candidates()]}")


def device_baseline(g: oglo.Glove) -> np.ndarray | None:
    """The sweep zero stored on the glove, (5, 4, 4) wire order, or None."""
    if not g.info.zero_valid:
        return None
    reply = g.send("GET ZERO", expect="#TZERO ", timeout=4.0)
    recipe = json.loads(reply.removeprefix("#TZERO "))
    return np.asarray(recipe["baseline"], dtype=np.int32).reshape(5, ROWS_PER_FINGER, NUM_COLS)


def screen_grid(block: np.ndarray, reversed_along: bool) -> np.ndarray:
    """One finger's (row, col) block as OGLO Studio draws it: screen[y][x].

    Wire index inside a finger is row*4 + col. Studio (viewer-core/usb.html,
    drawGlove) puts col on the vertical axis, col 0 = fingertip at the TOP, and row
    on the horizontal axis with row 0 at the RIGHT, for both hands. The left thumb
    is the one pad whose col runs the other way, so Studio flips it vertically.
    """
    g = np.asarray(block).T[:, ::-1]
    return g[::-1, :] if reversed_along else g


def render(g: oglo.Glove, resid: np.ndarray, hz: float, dropped: int, source: str,
           oriented: bool) -> str:
    info = g.info
    if oriented:
        names = list(FINGERS)
        resid = oriented_counts(resid, info.channels, info.side)
        layout = "layout: oriented_counts (thumb first, tip already at col 0 on every finger)"
    else:
        names = list(info.channels)
        layout = f"layout: WIRE slots left to right, like Studio; info.channels = {info.channels}"
    screens = [screen_grid(resid[f], reversed_along=(not oriented and info.side == "left"
                                                     and names[f] == "thumb"))
               for f in range(len(names))]
    lines = [
        f"{info.serial}  side={info.side}  fw {info.fw_rev}  {hz:5.1f} Hz  dropped={dropped}",
        f"cells = {source};  fingertip at TOP;  SDK row 0 on the RIGHT (Studio layout)  [peak]",
        layout,
        "",
    ]
    lines.append("        " + "".join(f"  {i}:{name:<14}   " for i, name in enumerate(names)))
    lines.append("        " + "".join("  r3  r2  r1  r0   " for _ in names))
    fi, ri, ci = np.unravel_index(int(resid.argmax()), resid.shape)
    peak = float(resid[fi, ri, ci])
    ylabel = ["tip ", "    ", "    ", "base"]
    for y in range(ROWS_PER_FINGER):
        line = f"{ylabel[y]}    "
        for f in range(len(names)):
            for x in range(NUM_COLS):
                v = int(screens[f][y, x])
                cell = f"{v:>3d}"
                if peak >= 25 and screens[f][y, x] == peak and f == fi:
                    cell = f"[{v}]"
                line += f"{cell:>4}"
            line += "   "
        lines.append(line)
    lines.append("")
    if peak < 25:
        lines.append("peak < 25 counts: press one corner of one finger pad")
    else:
        idx = fi * ROWS_PER_FINGER * NUM_COLS + ri * NUM_COLS + ci
        where = "oriented" if oriented else "wire"
        lines.append(f"PEAK {peak:.0f}  {where}: finger slot {fi} ({names[fi]}) row {ri} col {ci}"
                     + ("" if oriented else f"  flat index {idx} (Studio CSV taxel_{idx})"))
        if not oriented:
            of, orr, oc = np.unravel_index(int(oriented_counts(resid, info.channels, info.side).argmax()), (5, 4, 4))
            lines.append(f"         oriented_counts: {FINGERS[of]} row {orr} col {oc}")
    lines.append("")
    lines.append("Ctrl-C to quit")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="logical CONFIG serial, e.g. OGLO-L-00114")
    ap.add_argument("--side", choices=("left", "right"), help="pick the glove by hand")
    ap.add_argument("--refresh", type=float, default=0.1, help="screen refresh period in s")
    ap.add_argument("--oriented", action="store_true",
                    help="show oriented_counts (thumb first, col 0 = tip) instead of wire order")
    args = ap.parse_args()

    serial = args.serial or pick_serial(args.side)
    with oglo.connect(serial) as g:
        clean = g.info.stream_clean
        base_frames: list[np.ndarray] = []
        baseline: np.ndarray | None = None
        if clean:
            source = f"firmware CLEAN residual (thr={g.info.stream_thr})"
        else:
            baseline = device_baseline(g)
            source = ("raw - sweep zero stored on the glove" if baseline is not None
                      else "raw - host median of first 1 s (NO valid zero on the glove)")
        last_draw = 0.0
        n = 0
        t0 = time.monotonic()
        dropped = 0
        latest = None
        print(CLEAR + f"{g.info.serial}: {source}" +
              ("" if clean or baseline is not None else "; hold still for 1 s ..."), flush=True)
        try:
            # Every frame is consumed as it arrives: the host must never stop reading.
            for f in g.tactile():
                n += 1
                dropped += f.dropped
                counts = f.counts.astype(np.int32)
                if clean:
                    latest = counts
                elif baseline is None:
                    base_frames.append(counts)
                    if len(base_frames) >= BASELINE_FRAMES:
                        baseline = np.median(np.stack(base_frames), axis=0).astype(np.int32)
                    continue
                else:
                    latest = counts - baseline  # signed: a taxel under its zero shows negative
                now = time.monotonic()
                if now - last_draw >= args.refresh:
                    last_draw = now
                    hz = n / (now - t0)
                    print(CLEAR + render(g, latest, hz, dropped, source, args.oriented), flush=True)
        except KeyboardInterrupt:
            pass
    print("\nstream stopped")


if __name__ == "__main__":
    main()
