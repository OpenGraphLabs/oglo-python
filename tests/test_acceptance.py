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


@pytest.mark.parametrize("single", [False, True])
def test_connection_failure_still_leaves_a_machine_readable_report(tmp_path, single):
    class BrokenSdk:
        __version__ = "test"

        @staticmethod
        def connect_pair():
            raise RuntimeError("no gloves")

        @staticmethod
        def connect(*, transport):
            assert transport == "usb"
            raise RuntimeError("no glove")

    report = run_acceptance(
        AcceptanceConfig(output_root=tmp_path, stream_seconds=0.01, record_seconds=0,
                         single=single),
        sdk=BrokenSdk,
        sink=StringIO(),
    )
    assert report.failed
    name = "connect single USB glove" if single else "connect left/right USB pair"
    assert any(c.name == name and c.verdict == FAIL for c in report.checks)
    data = json.loads((report.run_dir / "acceptance-report.json").read_text())
    assert data["result"] == FAIL


@pytest.mark.parametrize("fw_rev", ["0.9.10", "0.9.11", "0.9.16"])
@pytest.mark.parametrize("single", [False, True])
def test_pair_contract_accepts_supported_firmware_schema_and_usb(tmp_path, fw_rev, single):
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
        config=AcceptanceConfig(output_root=tmp_path, single=single),
        sdk_version="test",
    )
    gloves = [Glove(left_info)] if single else [Glove(left_info), Glove(right_info)]
    _check_pair(report, gloves)
    assert report.checks
    assert all(check.verdict in (PASS, SKIP) for check in report.checks)
    if single:
        assert any(c.name == "two-hand compatibility" and c.verdict == SKIP
                   for c in report.checks)
        assert not any(c.name == "one left and one right glove" for c in report.checks)


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


def test_single_cli_selects_single_mode_without_enabling_mutations(monkeypatch, tmp_path):
    seen = []

    def run(config):
        seen.append(config)
        return SimpleNamespace(failed=False)

    monkeypatch.setattr("oglo.acceptance.run_acceptance", run)
    assert cli.main(["acceptance", "--single", "--output", str(tmp_path)]) == 0
    assert len(seen) == 1 and seen[0].single
    assert not seen[0].mutations and not seen[0].zero


@pytest.mark.parametrize(
    ("fw_rev", "short_writes", "dropped", "missed", "expected"),
    [("0.9.16", 5, 0, 0, PASS), ("0.9.16", 0, 1, 0, FAIL),
     ("0.9.16", 0, 0, 1, FAIL), ("0.9.16", -1, 0, 0, FAIL),
     ("0.9.10", 5, 0, 0, FAIL)],
)
def test_stream_health_distinguishes_backpressure_from_loss(
    tmp_path, monkeypatch, fw_rev, short_writes, dropped, missed, expected
):
    import oglo.acceptance as acceptance

    statuses = iter([
        SimpleNamespace(healthy=True, uptime_ms=100, tag_dropped=0,
                        deadline_misses=0, tag_short_writes=10),
        SimpleNamespace(healthy=True, uptime_ms=200, tag_dropped=dropped,
                        deadline_misses=missed, tag_short_writes=10 + short_writes),
    ])
    glove = SimpleNamespace(
        info=SimpleNamespace(side="left", serial="TEST", has_mag=True, fw_rev=fw_rev),
        stop=lambda: None, status=lambda: next(statuses),
        rates_seen={"tactile": 250, "imu": 500, "mag": 125},
        dropped={"wire_tactile": 0, "wire_imu": 0, "wire_mag": 0},
    )
    monkeypatch.setattr(acceptance, "_collect", lambda *args: {
        "tactile": [], "imu": [], "mag": [],
    })
    monkeypatch.setattr(acceptance, "_report_sample_contract", lambda *args: None)
    report = AcceptanceReport(tmp_path, AcceptanceConfig(), "test")
    acceptance._check_streams(report, [glove], 0.01, oglo)
    health = next(c for c in report.checks if "device health during stream" in c.name)
    assert health.verdict == expected
    assert health.measurements["tag_short_writes"] == short_writes


def test_interrupted_pair_recording_cancels_workers_before_waiting_for_shutdown(tmp_path, monkeypatch):
    import threading
    from oglo.acceptance import _record_replay_pair

    started = threading.Event()
    finished = []

    def record(path, seconds, *, glove, stop_event):
        started.set()
        assert stop_event.wait(2.0), "acceptance did not cancel its recording workers"
        finished.append(glove.info.side)
        return path

    def interrupted_wait(futures):
        assert started.wait(1.0)
        raise KeyboardInterrupt

    monkeypatch.setattr("oglo.acceptance.as_completed", interrupted_wait)
    gloves = [SimpleNamespace(info=SimpleNamespace(side=side), stop=lambda: None)
              for side in ("left", "right")]
    report = AcceptanceReport(tmp_path, AcceptanceConfig(), "test")
    with pytest.raises(KeyboardInterrupt):
        _record_replay_pair(report, gloves, 4500, tmp_path / "recordings",
                            SimpleNamespace(record=record), label="soak")
    assert sorted(finished) == ["left", "right"]
    assert (tmp_path / "acceptance-report.json").exists()


@pytest.mark.parametrize("failed_side", ["left", "right"])
def test_either_failed_hand_cancels_the_other_recording(tmp_path, failed_side):
    import threading
    from oglo.acceptance import _record_replay_pair

    peer_started = threading.Event()
    peer_cancelled = []

    def record(path, seconds, *, glove, stop_event):
        if glove.info.side == failed_side:
            assert peer_started.wait(2.0)
            raise RuntimeError(f"{failed_side} stream stalled")
        peer_started.set()
        # A bounded wait makes the regression fail rather than hanging a test for
        # the requested 75 minutes when the other hand's failure is ignored.
        peer_cancelled.append(stop_event.wait(2.0))
        return path

    gloves = [SimpleNamespace(info=SimpleNamespace(side=side), stop=lambda: None)
              for side in ("left", "right")]
    report = AcceptanceReport(tmp_path, AcceptanceConfig(), "test")
    _record_replay_pair(report, gloves, 4500, tmp_path / "recordings",
                        SimpleNamespace(record=record), label="soak")
    assert peer_cancelled == [True]
    assert report.failed
    assert report.checks[-1].verdict == FAIL
    assert f"{failed_side} stream stalled" in report.checks[-1].detail


@pytest.mark.parametrize("wire_loss", [0, 3])
def test_stream_analysis_stops_both_hands_and_keeps_pre_stop_evidence(
    tmp_path, monkeypatch, wire_loss
):
    import threading
    import oglo.acceptance as acceptance

    barrier = threading.Barrier(2)
    gloves = []
    for side in ("left", "right"):
        glove = SimpleNamespace(
            info=SimpleNamespace(side=side, serial=side, has_mag=True, fw_rev="0.9.17"),
            running=False,
            rates_seen={}, dropped={},
            status=lambda: SimpleNamespace(healthy=True, uptime_ms=100,
                tag_dropped=0, deadline_misses=0, tag_short_writes=0),
        )
        def stop(g=glove):
            g.running = False
            g.rates_seen.clear()
            g.dropped.clear()
        glove.stop = stop
        gloves.append(glove)

    def collect(glove, seconds):
        glove.running = True
        glove.rates_seen.update(tactile=250, imu=500, mag=125)
        glove.dropped.update(wire_tactile=wire_loss, wire_imu=0, wire_mag=0)
        barrier.wait(timeout=3)
        return {"tactile": [], "imu": [], "mag": []}

    def analyze(*args):
        assert all(not glove.running for glove in gloves), "analysis starves active readers"

    monkeypatch.setattr(acceptance, "_collect", collect)
    monkeypatch.setattr(acceptance, "_report_sample_contract", analyze)
    report = AcceptanceReport(tmp_path, AcceptanceConfig(), "test")
    acceptance._check_streams(report, gloves, 0.01, oglo)
    rates = [c for c in report.checks if "public rates_seen" in c.name]
    losses = [c for c in report.checks if "host/wire loss counters" in c.name]
    assert len(rates) == len(losses) == 2
    assert all(c.verdict == PASS and c.measurements["tactile"] == 250 for c in rates)
    assert all(c.verdict == (FAIL if wire_loss else PASS) for c in losses)
    assert all(c.measurements["wire_tactile"] == wire_loss for c in losses)


def test_failed_stream_collection_stops_the_started_glove(tmp_path, monkeypatch):
    import oglo.acceptance as acceptance

    glove = SimpleNamespace(info=SimpleNamespace(side="left"), running=False)
    glove.stop = lambda: setattr(glove, "running", False)
    glove.status = lambda: None

    def fail(glove, seconds):
        glove.running = True
        raise RuntimeError("reader failed")

    monkeypatch.setattr(acceptance, "_collect", fail)
    report = AcceptanceReport(tmp_path, AcceptanceConfig(), "test")
    with pytest.raises(RuntimeError, match="reader failed"):
        acceptance._check_streams(report, [glove], 0.01, oglo)
    assert not glove.running
