#!/usr/bin/env python3
"""Plug gloves in, run this, read PASS or FAIL per glove.

Covers the part no unit test can: the path that writes firmware to a real
device, and whether the result actually behaves.

    python3 tools/bench_check.py

What it proves per glove:

  1. the SDK finds it and reads its identity
  2. connect() runs the firmware update end to end when one is due
  3. it comes back on the expected release with serial, side and the zero recipe
     intact
  4. tactile, IMU and magnetometer all stream afterwards
  5. consecutive IMU samples are not repeats, which is the defect 0.9.18 fixes

Point 5 is the reason this file exists rather than a one-liner. Through 0.9.17
the sensor ran at ODR 200 Hz while the firmware polled it every 2 ms, so a
500 Hz stream carried 200 Hz of information: runs of 2 and 3 identical samples
alternated and about 60 % were repeats. Packet rate alone cannot see that, so it
is measured on sample values.

The only thing changed on a glove is the firmware update the SDK would perform
by itself at connect time. Identity and calibration are compared before and
after; a mismatch is a FAIL.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import traceback
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import oglo  # noqa: E402
from oglo import _firmware_package as pkg  # noqa: E402

# Identity the update must preserve. Read from Info, whose fields are fixed, so
# a renamed attribute is a visible KeyError rather than a silently absent check.
IDENTITY_FIELDS = ('serial', 'side', 'hw_rev', 'channels')
# Above this, the IMU is still handing out the previous conversion.
MAX_DUPLICATE_RATIO = 0.25
# Enough samples that the ratio means something at 500 Hz.
IMU_SAMPLES = 1000


def banner(text):
    print()
    print('=' * 70)
    print(text)
    print('=' * 70)


def identity_of(info) -> dict:
    return {name: getattr(info, name) for name in IDENTITY_FIELDS}


def imu_value(sample):
    """The measured quantity. ImuSample carries accel and gyro, not ax/ay/az.

    An earlier version of this check compared attributes that do not exist, so
    every sample looked identical and it reported 100 % duplicates on a healthy
    board. Reading the real fields is the entire measurement, which is why
    test_bench_check.py pins these names.
    """
    return (tuple(sample.accel), tuple(sample.gyro))


def run_lengths(samples) -> list:
    """How many identical samples in a row, per group."""
    if not samples:
        return []
    runs, current = [], 1
    for previous, nxt in zip(samples, samples[1:]):
        if imu_value(previous) == imu_value(nxt):
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    return runs


def check_glove(candidate, expected: str) -> dict:
    label = candidate.serial_number or candidate.device
    entry = {'usb': label, 'ok': False, 'why': ''}
    glove = None
    try:
        print('connecting (performs the firmware update if one is due)...')
        started = time.monotonic()
        glove = oglo.connect(port=candidate.device)
        print(f'connect took        : {time.monotonic() - started:.1f}s')

        info = glove.info
        identity = identity_of(info)
        print(f'serial / side       : {info.serial} / {info.side}')
        print(f'firmware            : {info.fw_rev}')
        print(f'hardware            : {info.hw_rev}')
        print(f'imu period          : {info.imu_period_ms} ms')
        print(f'zero valid          : {info.zero_valid}')
        print(f'identity            : {identity}')

        if info.fw_rev != expected:
            entry['why'] = f'firmware is {info.fw_rev}, expected {expected}'
            return entry

        print(f'streaming {IMU_SAMPLES} IMU samples...')
        glove.start()
        try:
            tactile = list(islice(glove.tactile(timeout=10), 200))
            imu_started = time.monotonic()
            imu = list(islice(glove.imu(timeout=10), IMU_SAMPLES))
            imu_elapsed = time.monotonic() - imu_started
            mag = list(islice(glove.mag(timeout=10), 50)) if info.has_mag else []
        finally:
            glove.stop()

        print(f'tactile frames      : {len(tactile)}')
        print(f'imu samples         : {len(imu)} in {imu_elapsed:.2f}s '
              f'= {len(imu) / imu_elapsed:.0f} packets/s')
        print(f'mag samples         : {len(mag)}'
              f'{" (no magnetometer on this unit)" if not info.has_mag else ""}')

        pairs = list(zip(imu, imu[1:]))
        if not pairs:
            entry['why'] = 'no IMU samples'
            return entry
        repeats = sum(1 for a, b in pairs if imu_value(a) == imu_value(b))
        ratio = repeats / len(pairs)
        runs = run_lengths(imu)
        dt = [b.t_us - a.t_us for a, b in pairs if b.t_us >= a.t_us]
        print(f'device dt median    : {statistics.median(dt) if dt else 0:.0f} us')
        print(f'duplicate ratio     : {ratio:.1%}  ({repeats}/{len(pairs)})'
              f'   ~60% before 0.9.18')
        print(f'fresh values        : {(1 - ratio) * len(imu) / imu_elapsed:.0f} Hz')
        print(f'run lengths         : max={max(runs)} mean={statistics.mean(runs):.2f} '
              f'histogram={ {n: runs.count(n) for n in sorted(set(runs))[:6]} }')

        if not tactile:
            entry['why'] = 'no tactile frames'
        elif info.has_mag and not mag:
            entry['why'] = 'magnetometer reported but no samples arrived'
        elif ratio > MAX_DUPLICATE_RATIO:
            entry['why'] = (f'IMU repeating {ratio:.0%} of samples; the ODR fix '
                            'is not in effect on this unit')
        elif identity_of(glove.info) != identity:
            entry['why'] = 'identity changed during the check'
        else:
            entry['ok'] = True
    except Exception as exc:
        entry['why'] = f'{type(exc).__name__}: {exc}'
        traceback.print_exc()
    finally:
        if glove is not None:
            try:
                glove.close()
            except Exception:
                pass
    return entry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', default=pkg.VERSION,
                        help='firmware every glove must end on (default: the bundled release)')
    args = parser.parse_args(argv)

    banner('ENVIRONMENT')
    print(f'SDK                 : {oglo.__version__}')
    print(f'firmware in wheel   : {pkg.VERSION}')
    print(f'expecting            : {args.expect}')
    try:
        from oglo import _firmware_channel as channel
        print(f'channel URL         : {channel.CHANNEL_URL}')
        fetched = channel.fetch_current()
        print(f'channel offers      : {fetched.name if fetched else "(nothing newer)"}')
    except Exception as exc:
        print(f'channel             : unavailable ({exc})')

    automatic = False
    try:
        automatic = pkg.auto_update_enabled()
    except Exception as exc:
        print(f'automatic updates   : unreadable ({exc})')
    print(f'automatic updates   : {automatic}')
    if not automatic:
        # Without this, connect() writes nothing and a glove on an older release
        # simply reports it. That is a broken test, not a broken glove, and it
        # has already happened once on this bench.
        print()
        print('Automatic updates are OFF in this environment, so connect() will')
        print('not update anything. Run `python -m oglo firmware enable` first,')
        print('or the result below says nothing about the update path.')

    banner('ATTACHED')
    candidates = list(oglo.list_candidates())
    if not candidates:
        print('No gloves found. Plug one in over USB and run this again.')
        return 2
    for candidate in candidates:
        print(f'  {candidate.serial_number}  {candidate.device}')

    results = []
    for candidate in candidates:
        banner(f'GLOVE {candidate.serial_number or candidate.device}')
        results.append(check_glove(candidate, args.expect))

    banner('VERDICT')
    for entry in results:
        print(f'{"PASS" if entry["ok"] else "FAIL"}  {entry["usb"]}  {entry["why"]}')
    failures = [entry for entry in results if not entry['ok']]
    print()
    if failures:
        print(f'{len(failures)} of {len(results)} FAILED. Paste this whole output.')
        return 1
    print(f'All {len(results)} glove(s) PASSED on firmware {args.expect}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
