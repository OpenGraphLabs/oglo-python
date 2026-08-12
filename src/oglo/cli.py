"""`oglo` on the command line.

The point of a CLI here is that the first thing anyone does with new hardware is
check whether it works at all, and that should not require writing Python.
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


def _cmd_acceptance(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .acceptance import AcceptanceConfig, run_acceptance

    if args.interactive and not sys.stdin.isatty():
        raise ValueError("--interactive needs a real terminal for the finger/motion prompts")
    if args.zero and not args.yes and not sys.stdin.isatty():
        raise ValueError("--zero needs a real terminal confirmation, or explicit --yes")
    config = AcceptanceConfig(
        output_root=Path(args.output),
        stream_seconds=args.seconds,
        record_seconds=0.0 if args.no_record else args.record,
        soak_seconds=args.soak,
        mutations=args.mutations,
        zero=args.zero,
        zero_sweep_seconds=args.zero_sweep,
        interactive=args.interactive,
        interactive_seconds=args.interactive_seconds,
        taxel_delta=args.taxel_delta,
        assume_yes=args.yes,
    )
    report = run_acceptance(config)
    return 2 if report.failed else 0


def _cmd_hil(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .hil import HilConfig, run_hil

    config = HilConfig(
        left_serial=args.left,
        right_serial=args.right,
        output_root=Path(args.output),
        expected_firmware=args.firmware,
        tag_seconds=args.tag_seconds,
        reconnect_cycles=args.reconnect_cycles,
        reconnect_seconds=args.reconnect_seconds,
        stall_seconds=args.stall,
        recovery_seconds=args.recovery,
        short_seconds=args.short,
        soak_seconds=args.soak,
        window_seconds=args.window,
        confirm_soak=args.confirm_soak,
        dry_run=args.dry_run,
        store_soak_raw=not args.no_soak_raw,
        tag2_spec=Path(args.tag2_spec) if args.tag2_spec else None,
    )
    report = run_hil(config)
    print(f"HIL result: {report.result}")
    print(f"Evidence: {report.run_dir}")
    return 0 if report.result in ("pass", "dry-run") else 2


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

    from .acceptance import parse_duration

    a = sub.add_parser(
        "acceptance",
        help="test a physical left/right USB pair and write JSON/Markdown evidence",
    )
    a.add_argument(
        "--output",
        default="acceptance-results",
        help="root directory for a new timestamped report (default: acceptance-results)",
    )
    a.add_argument(
        "--seconds",
        type=parse_duration,
        default=5.0,
        help="two-hand stream measurement duration: seconds, 75m, 1.5h (default: 5s)",
    )
    a.add_argument(
        "--record",
        type=parse_duration,
        default=2.0,
        help="short simultaneous record/replay duration (default: 2s)",
    )
    a.add_argument("--no-record", action="store_true", help="skip the short record/replay")
    a.add_argument(
        "--soak",
        type=parse_duration,
        default=None,
        help="also record/replay both hands for a long duration, e.g. 75m",
    )
    a.add_argument(
        "--interactive",
        action="store_true",
        help="prompt for every finger press and a wrist-motion response",
    )
    a.add_argument(
        "--interactive-seconds",
        type=parse_duration,
        default=1.5,
        help="capture window for each prompted action (default: 1.5s)",
    )
    a.add_argument(
        "--taxel-delta",
        type=float,
        default=25.0,
        help="minimum selected-finger response in ADC counts (default: 25)",
    )
    a.add_argument(
        "--mutations",
        action="store_true",
        help="exercise RAW/CLEAN/threshold/rates, then restore observed settings",
    )
    a.add_argument(
        "--zero",
        action="store_true",
        help="DESTRUCTIVE: replace each glove's stored zero after confirmation",
    )
    a.add_argument(
        "--zero-sweep",
        type=int,
        default=5,
        help="zero sweep duration, 1..30 seconds (default: 5)",
    )
    a.add_argument(
        "--yes",
        action="store_true",
        help="with --zero, bypass the typed confirmation (still requires explicit --zero)",
    )
    a.set_defaults(func=_cmd_acceptance)

    h = sub.add_parser(
        "hil",
        help="run the observation-only 0.9.13 release HIL/soak gate for an exact pair",
    )
    h.add_argument("--left", required=True, help="exact logical serial, e.g. OGLO-L-00028")
    h.add_argument("--right", required=True, help="exact logical serial, e.g. OGLO-R-00028")
    h.add_argument(
        "--firmware", default="0.9.13", help="exact candidate firmware required (default: 0.9.13)"
    )
    h.add_argument(
        "--output", default="hil-results", help="root for a new evidence directory"
    )
    h.add_argument(
        "--tag-seconds", type=parse_duration, default=3.0,
        help="per-version, per-hand TAG capture window (default: 3s)",
    )
    h.add_argument(
        "--reconnect-cycles", type=int, default=20,
        help="close/reopen cycles per hand (default: 20)",
    )
    h.add_argument(
        "--reconnect-seconds", type=parse_duration, default=0.5,
        help="fresh TAG2 capture per reconnect (default: 500ms)",
    )
    h.add_argument(
        "--stall", type=parse_duration, default=30.0,
        help="intentional no-read interval per hand (default: 30s)",
    )
    h.add_argument(
        "--recovery", type=parse_duration, default=3.0,
        help="fresh post-stall capture (default: 3s)",
    )
    h.add_argument(
        "--short", type=parse_duration, default=10.0,
        help="simultaneous two-hand acceptance capture (default: 10s)",
    )
    h.add_argument(
        "--soak", type=parse_duration, default=None,
        help="optional simultaneous soak, e.g. 72h; omitted by default",
    )
    h.add_argument(
        "--window", type=parse_duration, default=30.0,
        help="rolling soak sidecar window (default: 30s)",
    )
    h.add_argument(
        "--confirm-soak", default=None,
        help="for >=1h, must equal LEFT_SERIAL,RIGHT_SERIAL exactly",
    )
    h.add_argument(
        "--no-soak-raw", action="store_true",
        help="retain metrics but do not save the long raw TAG2 byte streams",
    )
    h.add_argument(
        "--dry-run", action="store_true",
        help="validate guardrails and write the plan without opening any serial port",
    )
    h.add_argument(
        "--tag2-spec", default=None,
        help="canonical TAG_V2.json (normally resolved from this source checkout)",
    )
    h.set_defaults(func=_cmd_hil)

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
