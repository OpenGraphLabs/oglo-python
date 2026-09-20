#!/usr/bin/env python3
"""Collect one USB glove episode and keep the SDK format for OGLO post-processing.

Run oglo doctor first. This example preserves the glove's current calibration,
stream mode, and rates. Send the entire episode directory, without editing its
JSON or NPZ files. See README.md for the file layout and handoff instructions.
"""

import argparse
import math
from pathlib import Path
import sys

import oglo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out"),
                        help="parent directory for numbered episodes (default: out)")
    parser.add_argument("--seconds", type=float, default=60,
                        help="capture duration in seconds (default: 60)")
    parser.add_argument("--serial", help="logical CONFIG serial of the USB glove")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and greater than zero")

    print(f"Collecting {args.seconds:g} seconds; wait for the saved episode path.")
    try:
        # The SDK records all fitted streams at their own rates, including the
        # original ordering, timestamps, loss counters, and capture metadata.
        episode_path = oglo.record(args.output, seconds=args.seconds, serial=args.serial)
    except Exception as exc:
        print(f"Capture failed: {exc}", file=sys.stderr)
        partial = getattr(exc, "partial_episode", None)
        if partial is not None:
            print(f"Keep the incomplete capture for diagnosis: {partial}", file=sys.stderr)
        return 1

    # Replay reads the saved files without changing them or requiring hardware.
    episode = oglo.replay(episode_path)
    print(episode.summary())
    print(f"Saved episode: {episode_path.resolve()}")
    if not episode.meta["complete"] or episode.meta["stop_reason"] != "duration":
        print("Capture was incomplete or stopped early. Keep it separate and retry.",
              file=sys.stderr)
        return 1

    print("Send this entire episode directory to the OGLO team unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
