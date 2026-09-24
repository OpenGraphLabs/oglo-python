#!/usr/bin/env python3
"""Preview camera-to-glove joins on host arrival time, without changing source data."""

import argparse
from bisect import bisect_left
import json
import math
from pathlib import Path

import oglo


def nearest_sample(times, sequences, camera_time, max_delta_ns):
    """Return a source row reference, or None outside coverage/tolerance.

    Shared USB receive timestamps are ambiguous: choose the first row at the
    nearest timestamp, with the earlier timestamp winning equal-distance ties.
    """
    if not times or camera_time < times[0] or camera_time > times[-1]:
        return None
    insertion = bisect_left(times, camera_time)
    candidates = [insertion]
    if insertion:
        candidates.append(insertion - 1)
    nearest = min(candidates, key=lambda i: (abs(times[i] - camera_time), times[i]))
    row = bisect_left(times, times[nearest])
    delta = times[row] - camera_time  # Python ints avoid uint64 subtraction wrapping.
    if abs(delta) > max_delta_ns:
        return None
    return {"row_index": row, "seq": int(sequences[row]),
            "host_received_ns": times[row], "delta_ns": delta}


def align(session, output, max_delta_ms):
    if not math.isfinite(max_delta_ms) or max_delta_ms <= 0:
        raise ValueError("max_delta_ms must be finite and greater than zero")
    manifest: oglo.OGLData = json.loads(
        (session / "manifest.json").read_text(encoding="utf-8")
    )
    if manifest["schema"] != "oglo-camera-example.v2" or not manifest["complete"]:
        raise ValueError("Expected a complete oglo-camera-example.v2 session")
    if manifest["host_clock"] != "time.monotonic_ns" or manifest["same_host"] is not True:
        raise ValueError("This preview requires the same host monotonic clock for all streams")
    camera = manifest["camera"]
    rows = [json.loads(line) for line in
            (session / camera["timestamps"]).read_text(encoding="utf-8").splitlines()]
    if len(rows) != camera["frames_decoded"]:
        raise ValueError("Camera sidecar length does not match the checked video frame count")
    last = -1
    for index, row in enumerate(rows):
        stamp = row["host_received_ns"]
        if (row["frame_index"] != index or type(stamp) is not int or stamp < last):
            raise ValueError("Camera sidecar must have consecutive frame indices and ordered ns times")
        last = stamp

    sources = []
    for entry in manifest["gloves"]:
        episode = oglo.replay(session / entry["episode"])
        if not episode.meta["complete"]:
            raise ValueError(f"Incomplete episode: {entry['episode']}")
        streams = {}
        for name in ("tactile", "imu", "mag"):
            data = episode.arrays(name)
            times = [int(t) for t in data["host_received_ns"]]
            if any(a > b for a, b in zip(times, times[1:])):
                raise ValueError(f"Unordered {name} host timestamps")
            streams[name] = (times, data["seq"])
        sources.append((entry, streams))

    # The file appears only once every row is written: a crash or an error part way
    # through leaves nothing behind, so its presence (with one row per frame) is the
    # signal that the episode is aligned.
    if output.exists():
        raise FileExistsError(f"{output} already exists")
    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as destination:
            for row in rows:
                joined = {
                    "frame_index": row["frame_index"],
                    "camera_host_received_ns": row["host_received_ns"],
                    "method": "nearest_host_received_ns", "max_delta_ms": max_delta_ms,
                    "alignment_validated": False, "gloves": [],
                }
                for entry, streams in sources:
                    joined["gloves"].append({
                        "serial": entry["serial"], "side": entry["side"], "episode": entry["episode"],
                        **{name: nearest_sample(times, sequences, row["host_received_ns"],
                                                int(max_delta_ms * 1_000_000))
                           for name, (times, sequences) in streams.items()},
                    })
                destination.write(json.dumps(joined) + "\n")
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path, help="new JSONL file; defaults to session/alignment.preview.jsonl")
    parser.add_argument("--max-delta-ms", type=float, default=50,
                        help="illustrative arrival-time tolerance, not a sync guarantee (default: 50)")
    args = parser.parse_args()
    output = args.output or args.session / "alignment.preview.jsonl"
    count = align(args.session, output, args.max_delta_ms)
    print(f"Wrote {count} frame references to {output}; alignment still requires validation.")


if __name__ == "__main__":
    main()
