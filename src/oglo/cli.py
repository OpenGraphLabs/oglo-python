"""Command-line diagnostics, recording, replay, and acceptance checks."""

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
        g = connect(port=c.device, firmware_policy=False)
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


def _cmd_studio(args: argparse.Namespace) -> int:
    try:
        import uvicorn
        from .studio import create_app
    except ImportError as exc:
        raise RuntimeError("Install the studio dependencies with: python -m pip install -e '.[studio]'") from exc

    # Never publish device controls to the local network.
    app = create_app(args.output)
    print(f"OGLO Studio: http://127.0.0.1:{args.port}/")
    print(f"Captures: {args.output}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    return 0


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
        single=args.single,
    )
    report = run_acceptance(config)
    return 2 if report.failed else 0


def _cmd_firmware(args):
    from .firmware import prepare, inventory, select_devices, merge_inventory
    from ._firmware_package import (resolve_policy, FirmwareError, configure_auto_update,
                                    auto_update_enabled, VERSION)
    import time
    if args.action in ('enable', 'disable', 'status'):
        if args.policy or args.watch or args.serial or args.merge:
            raise ValueError('enable/disable/status do not accept device or policy options')
        if args.action != 'status':
            configure_auto_update(args.action == 'enable')
        print(json.dumps({'automatic_updates': auto_update_enabled(), 'target_firmware': VERSION,
                          'environment': sys.prefix, 'device_registration_required': False,
                          'legacy_policy_override': bool(__import__('os').environ.get('OGLO_FIRMWARE_POLICY'))}, indent=2))
        return 0
    # Explicit prepare is itself the opt-in; no device list or local JSON needed.
    policy = resolve_policy(args.policy if args.policy else True)
    if args.action == "inventory":
        if args.watch or args.serial:
            raise ValueError("inventory does not accept --watch or --serial")
        report = inventory(policy)
        if args.merge:
            from pathlib import Path
            from ._firmware_package import read_json
            report = merge_inventory(policy, [report, *(read_json(Path(p)) for p in args.merge)])
        print(json.dumps(report, indent=2))
        return 0
    if args.merge:
        raise ValueError("--merge is only for inventory exports")
    if args.watch and args.serial and policy.compatible:
        raise ValueError('compatibility-based --watch discovers all attachments; use --serial without --watch')
    def progress(event):
        if "phase" in event or "error" in event:
            print(json.dumps(event), file=sys.stderr, flush=True)
    if not args.watch:
        prepare(policy, serials=args.serial, on_event=progress)
        print(json.dumps(inventory(policy), indent=2))
        return 0
    # Each physical attachment is tried once. A failed glove must be replugged
    # before another attempt; watch never loops on an unresponsive endpoint.
    attempted = set()
    while True:
        try:
            selected = select_devices(policy, args.serial)
        except FirmwareError:
            selected = []
        visible = {d["usb_serial"] for d in selected}
        attempted.intersection_update(visible)
        for chip in sorted(visible - attempted):
            attempted.add(chip)
            try:
                serials = None if policy.compatible else [d['serial'] for d in selected if d['usb_serial'] == chip]
                prepare(policy, serials=serials, on_event=progress, _usb_serials=[chip])
            except FirmwareError as exc:
                print(str(exc), file=sys.stderr, flush=True)
            print(json.dumps(inventory(policy)), flush=True)
        time.sleep(2)


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

    w = sub.add_parser("studio", help="run the local camera-and-glove collection page")
    w.add_argument("--output", default="captures/studio", help="local episode folder")
    w.add_argument("--port", type=int, default=8765, help="loopback web port")
    w.set_defaults(func=_cmd_studio)

    q = sub.add_parser("replay", help="summarise a recorded episode")
    q.add_argument("path")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_replay)

    from .acceptance import parse_duration

    a = sub.add_parser(
        "acceptance",
        help="test a USB glove pair (or --single) and write JSON/Markdown evidence",
    )
    a.add_argument(
        "--single", action="store_true",
        help="test exactly one attached USB glove; leave two-hand checks unqualified",
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

    f = sub.add_parser("firmware", help="automatic compatible firmware updates and saved observations")
    f.add_argument("action", choices=("enable", "disable", "status", "prepare", "inventory"))
    f.add_argument("--policy", help="optional legacy device policy; normally unnecessary")
    f.add_argument("--serial", action="append", help="select full logical serials; repeat for a pair")
    f.add_argument("--merge", action="append", help="merge another host inventory export; inventory action only")
    f.add_argument("--watch", action="store_true", help="prepare attached compatible devices until interrupted")
    f.set_defaults(func=_cmd_firmware)

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
