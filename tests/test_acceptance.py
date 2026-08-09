"""The owner-facing acceptance runner must be safe and reportable without hardware."""

from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace

import numpy as np
import pytest

import oglo
from oglo import cli
from oglo.acceptance import (
    FAIL,
    PASS,
    SKIP,
    AcceptanceConfig,
    AcceptanceReport,
    _finger_scores,
    _new_run_dir,
    _sample_stats,
    _version_tuple,
    parse_duration,
    run_acceptance,
)

from fake_serial import CFG_V6
from oglo._config import parse_config


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("2", 2.0), ("500ms", 0.5), ("5s", 5.0), ("75m", 4500.0), ("1.5h", 5400.0)],
)
def test_human_durations_are_unambiguous(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "nope", "0", "-1s", "nanh", "inf"])
def test_bad_durations_fail_before_hardware_is_touched(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_report_treats_optional_skips_as_a_pass_and_writes_both_formats(tmp_path):
    run_dir = _new_run_dir(tmp_path)
    report = AcceptanceReport(
        run_dir=run_dir,
        config=AcceptanceConfig(output_root=tmp_path),
        sdk_version="0.1.test",
    )
    report.add("read-only", PASS, "worked", {"rate": np.float32(250.0)})
    report.add("zero", SKIP, "not requested")
    report.finish()

    assert report.worst == PASS and not report.failed
    raw = json.loads((run_dir / "acceptance-report.json").read_text())
    assert raw["result"] == PASS
    assert raw["checks"][0]["measurements"]["rate"] == 250.0
    markdown = (run_dir / "acceptance-report.md").read_text()
    assert "**PASS**" in markdown and "zero" in markdown


def test_new_runs_never_overwrite_an_existing_report_directory(tmp_path):
    first = _new_run_dir(tmp_path)
    second = _new_run_dir(tmp_path)
    assert first != second and first.exists() and second.exists()


def _frames(values, *, channels):
    return [
        oglo.Frame(
            seq=index,
            t_us=index * 4_000,
            host_t=index / 250.0,
            counts=array,
            _stream_clean=True,
        )
        for index, array in enumerate(values)
    ], channels


def test_physical_finger_scoring_uses_the_device_channel_order():
    channels = ["pinky", "ring", "middle", "index", "thumb"]
    base = np.zeros((5, 4, 4), dtype=np.uint16)
    pressed = base.copy()
    pressed[3, 2, 1] = 180  # index is wire slot 3 on the left hand
    baseline, _ = _frames([base, base], channels=channels)
    active, _ = _frames([base, pressed], channels=channels)

    scores = _finger_scores(baseline, active, channels)
    assert scores["index"] == 180.0
    assert max(scores, key=scores.get) == "index"


def test_sample_stats_use_observed_host_timestamps_not_requested_rates():
    counts = np.zeros((5, 4, 4), dtype=np.uint16)
    tactile = [
        oglo.Frame(seq=i, t_us=i * 4_000, host_t=i * 0.004, counts=counts)
        for i in range(11)
    ]
    stats = _sample_stats({"tactile": tactile, "imu": [], "mag": []})
    assert stats["tactile_hz"] == pytest.approx(250.0)
    assert stats["imu_hz"] == 0.0


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0.9.10", (0, 9, 10)),
        ("0.9.11", (0, 9, 11)),
        ("0.9.10-release", (0, 9, 10)),
        ("0.9", None),
        ("x", None),
    ],
)
def test_live_firmware_comparison_is_numeric_and_exact(text, value):
    assert _version_tuple(text) == value


def test_connection_failure_still_leaves_a_machine_readable_report(tmp_path):
    class BrokenSdk:
        __version__ = "test"

        @staticmethod
        def connect_pair():
            raise RuntimeError("no gloves")

    report = run_acceptance(
        AcceptanceConfig(output_root=tmp_path, stream_seconds=0.01, record_seconds=0),
        sdk=BrokenSdk,
        sink=StringIO(),
    )
    assert report.failed
    assert any(c.name == "connect left/right USB pair" and c.verdict == FAIL for c in report.checks)
    data = json.loads((report.run_dir / "acceptance-report.json").read_text())
    assert data["result"] == FAIL


@pytest.mark.parametrize("fw_rev", ["0.9.10", "0.9.11"])
def test_pair_contract_accepts_supported_firmware_schema_and_usb(tmp_path, fw_rev):
    from oglo.acceptance import _check_pair

    cfg = {**CFG_V6, "fw_rev": fw_rev}
    left_info, _ = parse_config(cfg)
    right_info = replace(
        left_info,
        serial="OGLO-R-TEST02",
        side="right",
        channels=["thumb", "index", "middle", "ring", "pinky"],
        raw={
            **left_info.raw,
            "serial": "OGLO-R-TEST02",
            "side": "right",
            "channels": ["thumb", "index", "middle", "ring", "pinky"],
        },
    )
    status = SimpleNamespace(
        healthy=True,
        uptime_ms=1,
        imu_ok=True,
        mag_ok=True,
        sensor_ok=True,
        error_flags=0,
        deadline_misses=0,
        tag_dropped=0,
        tag_short_writes=0,
    )

    class Glove:
        def __init__(self, info):
            self.info = info

        def status(self):
            return status

        def send(self, command, *, expect=None, timeout=2.0):
            assert command == "GET ZERO"
            return "#TZERO " + json.dumps(
                {
                    "valid": True,
                    "count": 80,
                    "baseline": [550] * 80,
                    "noise": [2] * 80,
                    "thr": self.info.stream_thr,
                    "clean": self.info.stream_clean,
                    "locked": False,
                }
            )

    report = AcceptanceReport(
        run_dir=tmp_path / "report",
        config=AcceptanceConfig(output_root=tmp_path),
        sdk_version="test",
    )
    _check_pair(report, [Glove(left_info), Glove(right_info)])
    assert report.checks
    assert all(check.verdict == PASS for check in report.checks)


def test_mutation_check_restores_threshold_even_when_original_mode_was_raw(tmp_path, monkeypatch):
    import oglo.acceptance as acceptance

    class Glove:
        def __init__(self):
            self.info = SimpleNamespace(
                serial="OGLO-L-TEST01",
                side="left",
                rate_hz=250,
                stream_clean=False,
                stream_thr=80,
                zero_valid=True,
            )
            self.imu_hz = 500

        def stop(self):
            pass

        def raw(self):
            self.info.stream_clean = False

        def clean(self, threshold=0):
            self.info.stream_clean = True
            self.info.stream_thr = threshold

        def rates(self, *, tactile=None, imu=None, mag=None):
            if tactile is not None:
                self.info.rate_hz = tactile
            if imu is not None:
                self.imu_hz = imu

        def tactile(self, timeout=None):
            yield oglo.Frame(
                seq=1,
                t_us=1,
                host_t=1.0,
                counts=np.zeros((5, 4, 4), dtype=np.uint16),
                _stream_clean=self.info.stream_clean,
            )

    def samples(rate):
        return [SimpleNamespace(host_t=i / rate) for i in range(5)]

    glove = Glove()
    monkeypatch.setattr(
        acceptance,
        "_collect",
        lambda g, seconds: {
            "tactile": samples(g.info.rate_hz),
            "imu": samples(g.imu_hz),
            "mag": [],
        },
    )
    report = AcceptanceReport(
        run_dir=tmp_path / "report",
        config=AcceptanceConfig(output_root=tmp_path),
        sdk_version="test",
    )
    acceptance._mutation_checks(report, [glove], {"left": {"imu_hz": 500.0}}, oglo)

    assert report.checks[-1].verdict == PASS
    assert glove.info.rate_hz == 250
    assert glove.imu_hz == 500
    assert glove.info.stream_clean is False
    assert glove.info.stream_thr == 80


def test_interactive_cli_refuses_to_hang_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["acceptance", "--interactive"]) == 1
    assert "needs a real terminal" in capsys.readouterr().err
