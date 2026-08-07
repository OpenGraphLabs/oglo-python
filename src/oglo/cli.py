"""`oglo` on the command line.

Four verbs, matching the four functions. The point of a CLI here is that the first
thing anyone does with new hardware is check whether it works at all, and that should
not require writing Python.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional


def _cmd_doctor(args: argparse.Namespace) -> int:
    from ._doctor import FAIL, WARN, doctor

    rep = doctor(seconds=args.seconds)
    print(rep)
    print()
    if rep.worst == FAIL:
        print("Something is wrong. The FAIL lines above say what.")
        return 2
    if rep.worst == WARN:
        print("Usable, with the caveats above.")
        return 0
    print("All good.")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    from . import connect
    from ._usb import list_candidates

    cands = list_candidates()
    if not cands:
        print("no glove found", file=sys.stderr)
        return 2
    json_rows = []
    for c in cands:
        g = connect(port=c.device)
        try:
            i = g.info
            if args.json:
                json_rows.append({**i.raw, "port": c.device, "transport": i.transport})
            else:
                print(f"{i.serial}  {i.side}")
                print(f"  port        {c.device}")
                print(f"  hardware    {i.hw_rev}")
                print(f"  firmware    {i.fw_rev}")
                print(f"  rate        {i.rate_hz} Hz tactile")
                print(f"  fingers     {', '.join(i.channels)}")
                print(f"  magnetometer{'  yes' if i.has_mag else '  no'}")
                print(f"  zero        {'captured' if i.zero_valid else 'NOT captured'}")
                print(f"  stream      {'clean, deadband ' + str(i.stream_thr) if i.stream_clean else 'raw'}")
        finally:
            g.close()
    if args.json:
        # Always one JSON document. Printing adjacent objects made valid-looking
        # output that every JSON parser rejected as soon as two gloves were attached.
        print(json.dumps(json_rows, indent=1))
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    from . import record

    path = record(args.path, seconds=args.seconds, serial=args.serial)
    print(path)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from . import replay

    e = replay(args.path)
    s = e.summary()
    if args.json:
        print(json.dumps(s, indent=1))
        return 0 if s["complete"] else 2
    if not s["complete"]:
        print("  INCOMPLETE EPISODE")
        if s.get("error"):
            print(f"  error       {s['error']}")
    print(f"{s['serial']}  {s['side']}  fw {s['fw_rev']}")
    print(f"  stream      {'clean, deadband ' + str(s['stream_thr']) if s['stream_clean'] else 'raw'}")
    for name in ("tactile", "imu", "mag"):
        d = s[name]
        if not d.get("n"):
            print(f"  {name:10}  empty")
            continue
        print(f"  {name:10}  {d['n']:>7} samples  {d['seconds']:>7.2f} s  "
              f"{d['hz']:>7.1f} Hz  dropped {d['dropped']}")
    return 0 if s["complete"] else 2


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="oglo", description="OGLO tactile glove")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check this machine and every attached glove")
    d.add_argument("--seconds", type=float, default=3.0, help="how long to measure each glove")
    d.set_defaults(func=_cmd_doctor)

    i = sub.add_parser("info", help="what each attached glove says about itself")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=_cmd_info)

    r = sub.add_parser("record", help="capture an episode")
    r.add_argument("path")
    r.add_argument("--seconds", type=float, default=None, help="omit to record until Ctrl-C")
    r.add_argument("--serial", default=None, help="which glove, if more than one is attached")
    r.set_defaults(func=_cmd_record)

    q = sub.add_parser("replay", help="summarise a recorded episode")
    q.add_argument("path")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_replay)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        partial = getattr(exc, "partial_episode", None)
        if partial is not None:
            print(f"partial episode saved at: {partial}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
