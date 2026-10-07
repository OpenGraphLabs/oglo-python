"""Guard the bench check against the bug class that made it lie.

The first version compared `sample.ax/ay/az`, which `ImuSample` does not have,
so `getattr(..., None)` made every sample equal and it reported 100 % duplicate
IMU samples on a healthy board that had just been updated correctly. A checker
that fails a good unit is worse than no checker, and nothing in the suite would
have caught it, because the tool was a script nobody imported.

These tests pin the field names the measurement depends on and the arithmetic it
reports, so a rename breaks the suite instead of a bench session.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

import bench_check  # noqa: E402
from oglo import Info  # noqa: E402
from oglo._frame import ImuSample  # noqa: E402


def sample(accel, gyro, *, seq=0, t_us=0):
    return ImuSample(seq=seq, t_us=t_us, host_t=0.0, accel=accel, gyro=gyro,
                     dropped=0, raw=None)


def test_the_measured_fields_exist_on_the_real_sample_type():
    """The exact failure that made it report 100 % duplicates on a good board."""
    names = {field.name for field in dataclasses.fields(ImuSample)}
    assert {'accel', 'gyro'} <= names
    # And the names it used to read, which silently produced None for everything.
    assert not ({'ax', 'ay', 'az', 'gx', 'gy', 'gz'} & names)
    value = bench_check.imu_value(sample((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))
    assert value == ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0))


def test_identity_fields_exist_on_the_real_info_type():
    """A preservation check that reads absent attributes checks nothing."""
    names = {field.name for field in dataclasses.fields(Info)}
    for field in bench_check.IDENTITY_FIELDS:
        assert field in names, field


def test_distinct_samples_are_not_counted_as_repeats():
    samples = [sample((float(i), 0.0, 0.0), (0.0, 0.0, 0.0)) for i in range(10)]
    assert bench_check.run_lengths(samples) == [1] * 10


def test_the_pre_fix_pattern_is_recognised():
    """200 Hz read every 2 ms gives runs of 2 and 3, about 60 % duplicates."""
    samples = []
    value = 0
    for length in (2, 3) * 20:
        samples += [sample((float(value), 0.0, 0.0), (0.0, 0.0, 0.0))] * length
        value += 1
    runs = bench_check.run_lengths(samples)
    assert set(runs) == {2, 3}
    pairs = list(zip(samples, samples[1:]))
    repeats = sum(1 for a, b in pairs
                  if bench_check.imu_value(a) == bench_check.imu_value(b))
    ratio = repeats / len(pairs)
    assert 0.55 < ratio < 0.65
    assert ratio > bench_check.MAX_DUPLICATE_RATIO


def test_a_fresh_stream_passes_the_threshold():
    samples = [sample((float(i), 0.0, 0.0), (0.0, 0.0, 0.0)) for i in range(1000)]
    pairs = list(zip(samples, samples[1:]))
    repeats = sum(1 for a, b in pairs
                  if bench_check.imu_value(a) == bench_check.imu_value(b))
    assert repeats / len(pairs) < bench_check.MAX_DUPLICATE_RATIO


def test_empty_input_does_not_raise():
    assert bench_check.run_lengths([]) == []


def test_identity_snapshot_reads_every_declared_field():
    info = Info(serial='OGLO-R-00009', side='right', hw_rev='RDR02_FLEX5_REV_D_TIA',
                fw_rev='0.9.18', rate_hz=250, channels=('thumb',), has_mag=True,
                transport='usb', zero_valid=True, stream_clean=False, stream_thr=80,
                imu_period_ms=2, device_dropped=0, raw={}, firmware_verification=None)
    snapshot = bench_check.identity_of(info)
    assert snapshot == {'serial': 'OGLO-R-00009', 'side': 'right',
                        'hw_rev': 'RDR02_FLEX5_REV_D_TIA', 'channels': ('thumb',)}
    assert None not in snapshot.values()
