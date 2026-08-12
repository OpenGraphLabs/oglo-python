"""The release HIL runner is safe and useful before it ever touches a board."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_serial import BOOT_A, CFG_V6, FakeSerial, tagged_burst, tagged_v2_burst
from oglo import cli
from oglo.hil import (
    FAIL,
    PASS,
    RELEASE_SOAK_SECONDS,
    WARN,
    HilConfig,
    HilReport,
    StreamMonitor,
    Target,
    _release_soak_gate,
    capture_tag_stream,
    run_hil,
    run_line_matrix,
    sha256_file,
    validate_config,
    validate_tag2_spec,
)


LEFT = "OGLO-L-00028"
RIGHT = "OGLO-R-00028"


def _config(tmp_path: Path, **changes) -> HilConfig:
    values = {
        "left_serial": LEFT,
        "right_serial": RIGHT,
        "output_root": tmp_path,
        "tag_seconds": 0.01,
        "reconnect_cycles": 1,
        "reconnect_seconds": 0.01,
        "stall_seconds": 0.01,
        "recovery_seconds": 0.01,
        "short_seconds": 0.01,
        "window_seconds": 0.01,
    }
    values.update(changes)
    return HilConfig(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"left_serial": "OGLO-R-00028"}, "left serial"),
        ({"right_serial": "OGLO-R-28"}, "right serial"),
        ({"expected_firmware": "latest"}, "numeric"),
        ({"min_free_gib": 99.9}, "100 GiB"),
        ({"soak_seconds": True}, "finite number"),
        ({"soak_seconds": float("nan")}, "finite number"),
    ],
)
def test_hil_target_and_resource_guardrails_fail_before_discovery(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        validate_config(_config(tmp_path, **changes))


def test_long_soak_needs_exact_pair_confirmation(tmp_path):
    with pytest.raises(ValueError, match="confirm-soak"):
        validate_config(_config(tmp_path, soak_seconds=72 * 3600))

    validate_config(
        _config(
            tmp_path,
            soak_seconds=72 * 3600,
            confirm_soak=f"{LEFT},{RIGHT}",
        )
    )


def test_short_diagnostic_soak_cannot_satisfy_the_explicit_72h_gate(tmp_path):
    config = _config(tmp_path, soak_seconds=RELEASE_SOAK_SECONDS - 1)
    verdict, detail, measurements = _release_soak_gate(config, {"ok": True})
    report = HilReport(run_dir=tmp_path, config=config)
    report.add("diagnostic dual-hand soak", PASS)
    report.add("72h dual-hand soak", verdict, detail, measurements)

    assert verdict == WARN
    assert report.result == WARN
    assert measurements["minimum_release_seconds"] == 259200
    assert "diagnostic soak" in detail


def test_72h_gate_needs_duration_confirmation_and_a_passing_soak(tmp_path):
    confirmed = _config(
        tmp_path,
        soak_seconds=RELEASE_SOAK_SECONDS,
        confirm_soak=f"{LEFT},{RIGHT}",
    )
    assert _release_soak_gate(confirmed, {"ok": True})[0] == PASS
    assert _release_soak_gate(confirmed, {"ok": False})[0] == FAIL


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_ack_prefix", "#STREAM TAG2 boot="),
        ("boot_id.bytes", 8),
        ("boot_id.scope", "one stream"),
    ],
)
def test_canonical_spec_binds_full_ack_and_boot_identity_contract(tmp_path, field, value):
    source = Path(__file__).resolve().parents[1] / "spec" / "TAG_V2.json"
    spec = json.loads(source.read_text())
    if field == "start_ack_prefix":
        spec["negotiation"][field] = value
    else:
        _, child = field.split(".")
        spec["boot_id"][child] = value
    changed = tmp_path / "TAG_V2.json"
    changed.write_text(json.dumps(spec))

    with pytest.raises(ValueError, match="spec/parser mismatch"):
        validate_tag2_spec(changed)


def test_dry_run_opens_no_hardware_and_writes_verifiable_evidence(tmp_path):
    class ForbiddenBackend:
        def __getattribute__(self, name):
            raise AssertionError(f"dry-run touched backend.{name}")

    report = run_hil(_config(tmp_path, dry_run=True), backend=ForbiddenBackend())
    assert report.result == "dry-run"
    assert report.as_dict()["flash_performed"] is False

    raw = json.loads((report.run_dir / "hil-report.json").read_text())
    assert raw["result"] == "dry-run"
    assert raw["config"]["left_serial"] == LEFT
    manifest = (report.run_dir / "manifest.sha256").read_text().splitlines()
    assert manifest
    for row in manifest:
        digest, relative = row.split("  ", 1)
        assert sha256_file(report.run_dir / relative) == digest


def test_dry_run_does_not_hide_a_broken_canonical_contract(tmp_path):
    bad_spec = tmp_path / "TAG_V2.json"
    bad_spec.write_text("{}\n", encoding="utf-8")
    report = run_hil(
        _config(tmp_path / "results", dry_run=True, tag2_spec=bad_spec)
    )
    assert report.result == FAIL
    assert "unbound wire contract" in report.error


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(17), SystemExit),
    ],
)
def test_interrupts_seal_failed_hil_evidence_before_reraising(tmp_path, raised, expected):
    class InterruptedBackend:
        def discover(self, config):
            raise raised

    with pytest.raises(expected) as caught:
        run_hil(_config(tmp_path), backend=InterruptedBackend())

    error = caught.value
    assert error is raised
    assert error.hil_report_finalized is True
    report_path = Path(error.hil_report_path)
    manifest_path = report_path.with_name("manifest.sha256")
    raw = json.loads(report_path.read_text())
    assert raw["result"] == FAIL
    assert raw["finished_at"] is not None
    assert raw["error"].startswith(type(raised).__name__)
    assert any(item["name"] == "HIL execution interrupted" for item in raw["checks"])
    assert manifest_path.exists()
    assert report_path.stat().st_mode & 0o222 == 0
    assert manifest_path.stat().st_mode & 0o222 == 0


def test_hil_cli_dry_run_requires_exact_serials_and_reports_directory(tmp_path, capsys):
    result = cli.main(
        [
            "hil",
            "--left",
            LEFT,
            "--right",
            RIGHT,
            "--output",
            str(tmp_path),
            "--dry-run",
        ]
    )
    assert result == 0
    output = capsys.readouterr().out
    assert "HIL result: dry-run" in output and "Evidence:" in output


def test_incremental_monitors_cover_tag1_tag2_crc_loss_and_u64():
    v1 = StreamMonitor(1, started_ns=0)
    payload = tagged_burst(8)
    for offset in range(0, len(payload), 7):
        v1.feed(payload[offset:offset + 7])
    v1_summary = v1.cumulative(now_ns=1_000_000_000)
    assert v1_summary["counts"] == {"tactile": 8, "imu": 16, "mag": 4}
    assert v1_summary["malformed_crc_or_structure"] == 0
    assert v1_summary["crc_checked"] is False

    original = tagged_v2_burst(8, start_time_us=0x1_0000_0000 + 123)
    corrupted = bytearray(original)
    corrupted[20] ^= 0x01  # first frame payload; its CRC must reject that whole frame
    v2 = StreamMonitor(2, started_ns=0)
    for offset in range(0, len(corrupted), 11):
        v2.feed(bytes(corrupted[offset:offset + 11]))
    v2_summary = v2.cumulative(now_ns=1_000_000_000)
    assert v2_summary["counts"]["tactile"] == 7
    assert v2_summary["counts"]["imu"] == 16
    assert v2_summary["counts"]["mag"] == 4
    assert v2_summary["malformed_crc_or_structure"] == 1
    assert v2_summary["crc_checked"] is True
    assert v2.last_device_us["tactile"] > 0xFFFFFFFF


class _LineSerial:
    def __init__(self) -> None:
        self.timeout = 0.0
        self.dtr = False
        self.rts = False
        self.closed = False
        self.out = bytearray()
        self.uptime = 1000

    def write(self, data: bytes) -> int:
        for command in data.splitlines():
            if command == b"GET STATUS" and self.dtr:
                self.uptime += 10
                status = {
                    "uptime_ms": self.uptime,
                    "seq": 1,
                    "imu_ok": True,
                    "imu": {"ok": True, "mag_ok": True},
                    "sensor_ok": True,
                    "error_flags": 0,
                    "deadline_misses": 0,
                    "tag_dropped": 0,
                    "tag_short_writes": 0,
                }
                self.out += b"#STATUS " + json.dumps(status).encode() + b"\n"
            if command == b"GET CONFIG" and self.dtr:
                self.out += b"#CONFIG " + json.dumps(
                    {"serial": LEFT, "side": "left", "boot_id": BOOT_A}
                ).encode() + b"\n"
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        out = bytes(self.out[:size])
        del self.out[:size]
        return out

    def reset_input_buffer(self) -> None:
        self.out.clear()

    def close(self) -> None:
        self.closed = True


def test_dtr_rts_matrix_proves_dtr_gate_and_rts_independence_without_reenumeration():
    serial = _LineSerial()
    target = Target(
        side="left",
        logical_serial=LEFT,
        port="/dev/fake-left",
        usb_serial="USBLEFT",
        vid=0x2886,
        pid=1,
        product="OGLO",
        manufacturer="OpenGraphLabs",
    )
    result = run_line_matrix(
        target,
        serial_factory=lambda *_: serial,
        candidate_provider=lambda: [
            SimpleNamespace(device=target.port, serial_number=target.usb_serial)
        ],
        settle_seconds=0.0,
        response_seconds=0.001,
        sleep=lambda _: None,
    )
    assert result["ok"]
    assert result["sequence"] == ["00", "10", "00", "11", "00", "01", "00"]
    assert [item["response_bytes"] > 0 for item in result["observations"]] == [
        False, True, False, True, False, False, False
    ]
    assert result["uptimes_ms"] == sorted(result["uptimes_ms"])
    assert result["postcheck_10"]["config"]["boot_id"] == BOOT_A
    assert result["actual_transition_states"] == [
        "00", "10", "00", "11", "00", "01", "00", "10", "00"
    ]
    assert serial.closed


def test_real_capture_helper_negotiates_exact_tag2_ack_and_checks_every_crc(tmp_path):
    target = Target(
        side="left",
        logical_serial=LEFT,
        port="/dev/fake-left",
        usb_serial="USBLEFT",
        vid=0x2886,
        pid=1,
        product="OGLO",
        manufacturer="OpenGraphLabs",
    )
    cfg = {
        **CFG_V6,
        "serial": LEFT,
        "fw_rev": "0.9.13",
        "tag_ver_max": 2,
        "boot_id": BOOT_A,
    }

    def factory(*_):
        serial = FakeSerial(
            cfg,
            stream=tagged_v2_burst(4),
            chunk=17,
            hz=1000.0,
        )
        serial.timeout = 0.0
        serial.dtr = True
        serial.rts = False
        return serial

    raw_path = tmp_path / "left-tag2.bin"
    result = capture_tag_stream(
        target,
        version=2,
        seconds=0.04,
        serial_factory=factory,
        raw_path=raw_path,
    )
    assert result["ok"], result["failures"]
    assert result["ack_boot_id"] == result["config_boot_id"] == BOOT_A
    assert result["crc_checked"] is True
    assert all(result["counts"][name] > 0 for name in ("tactile", "imu", "mag"))
    assert result["malformed_crc_or_structure"] == 0
    assert result["raw_sha256"] == sha256_file(raw_path)


def test_stalled_reader_records_both_boundaries_and_waits_past_stale_backlog(tmp_path):
    target = Target(
        "left", LEFT, "/dev/fake-left", "USBLEFT", 0x2886, 1, "OGLO", "OGL"
    )
    cfg = {
        **CFG_V6,
        "serial": LEFT,
        "fw_rev": "0.9.13",
        "tag_ver_max": 2,
        "boot_id": BOOT_A,
    }

    def factory(*_):
        serial = FakeSerial(
            cfg, stream=tagged_v2_burst(4), chunk=17, hz=1000.0
        )
        serial.timeout = 0.0
        return serial

    result = capture_tag_stream(
        target,
        version=2,
        seconds=0.04,
        stall_before_read=0.08,
        serial_factory=factory,
        raw_path=tmp_path / "recovery.bin",
    )

    recovery = result["stalled_reader_recovery"]
    assert result["ok"], result["failures"]
    assert recovery["pre_stall_boundary"]["seq"]["tactile"] is not None
    assert recovery["pre_stall_boundary"]["device_time_us"]["tactile"] is not None
    assert recovery["pre_stall_boundary"]["boot_id"] == BOOT_A
    assert recovery["pre_stall_boundary"]["status"]["uptime_ms"] > 0
    assert recovery["host_input_reset_after_stall"] is True
    assert recovery["post_stall_first_fresh_tactile"] is not None
    assert recovery["first_fresh_frame_latency_after_input_reset_s"] is not None
    assert recovery["boot_identity_unchanged"] is True
    assert recovery["tactile_seq_transition"] in ("forward", "wrap")
    assert recovery["tactile_device_time_advance_us"] >= recovery[
        "expected_device_time_advance_min_us"
    ]
    assert recovery["stale_device_backlog_detected"] is True
    assert recovery["stale_tactile_frames_before_fresh"] > 0


def test_stalled_reader_never_calls_old_backlog_a_fresh_recovery(tmp_path):
    target = Target(
        "left", LEFT, "/dev/fake-left", "USBLEFT", 0x2886, 1, "OGLO", "OGL"
    )
    cfg = {
        **CFG_V6,
        "serial": LEFT,
        "fw_rev": "0.9.13",
        "tag_ver_max": 2,
        "boot_id": BOOT_A,
    }

    class StaleOnlySerial(FakeSerial):
        def _refill(self):
            return tagged_v2_burst(1, start_seq=1, start_time_us=4_000)

    def factory(*_):
        serial = StaleOnlySerial(cfg, stream=tagged_v2_burst(1), chunk=8192)
        serial.timeout = 0.0
        return serial

    result = capture_tag_stream(
        target,
        version=2,
        seconds=0.02,
        stall_before_read=0.08,
        serial_factory=factory,
    )

    recovery = result["stalled_reader_recovery"]
    assert result["ok"] is False
    assert recovery["post_stall_first_fresh_tactile"] is None
    assert recovery["stale_device_backlog_detected"] is True
    assert "no fresh valid TAG2 frame" in "; ".join(result["failures"])


def _snapshot(side: str, firmware: str) -> dict:
    serial = LEFT if side == "left" else RIGHT
    config = {
        **CFG_V6,
        "serial": serial,
        "side": side,
        "fw_rev": firmware,
        "tag_ver_max": 2,
        "boot_id": BOOT_A,
    }
    status = {
        "uptime_ms": 1000,
        "seq": 1,
        "imu_ok": True,
        "mag_ok": True,
        "sensor_ok": True,
        "error_flags": 0,
        "deadline_misses": 0,
        "tag_dropped": 0,
        "tag_short_writes": 0,
        "raw": {},
    }
    zero = {"valid": True, "count": 80, "baseline": [1] * 80, "noise": [1] * 80}
    return {
        "config": config,
        "status": status,
        "zero": zero,
        "calibration_sha256": "a" * 64,
        "fwinfo": {"running_image_sha256": "b" * 64},
        "fwinfo_error": None,
        "running_image_sha256": "b" * 64,
    }


def test_preflight_fails_closed_before_modem_lines_when_candidate_is_not_expected(tmp_path):
    targets = {
        side: Target(side, LEFT if side == "left" else RIGHT, f"/dev/{side}", side, 0x2886, 1, "OGLO", "OGL")
        for side in ("left", "right")
    }

    class Backend:
        line_calls = 0

        def discover(self, config):
            return targets, []

        def snapshot(self, target):
            return _snapshot(target.side, "0.9.12")

        def line_matrix(self, target):
            self.line_calls += 1
            return {"ok": True}

    backend = Backend()
    report = run_hil(_config(tmp_path), backend=backend)
    assert report.result == FAIL
    assert "preflight failed" in report.error
    assert backend.line_calls == 0
    assert (report.run_dir / "manifest.sha256").exists()


def test_full_fake_backend_binds_every_saved_tag2_capture_and_preserves_snapshots(tmp_path):
    targets = {
        side: Target(
            side,
            LEFT if side == "left" else RIGHT,
            f"/dev/{side}",
            side,
            0x2886,
            1,
            "OGLO",
            "OGL",
        )
        for side in ("left", "right")
    }

    class Backend:
        def discover(self, config):
            return targets, [target.as_dict() for target in targets.values()]

        def snapshot(self, target):
            return _snapshot(target.side, "0.9.13")

        def line_matrix(self, target):
            return {"ok": True, "sequence": ["00", "10", "00", "11", "00", "01", "00"]}

        @staticmethod
        def _factory(target, version):
            cfg = {
                **CFG_V6,
                "serial": target.logical_serial,
                "side": target.side,
                "fw_rev": "0.9.13",
                "tag_ver_max": 2,
                "boot_id": BOOT_A,
            }
            stream = tagged_v2_burst(4) if version == 2 else tagged_burst(4)

            def factory(*_):
                serial = FakeSerial(cfg, stream=stream, chunk=31, hz=1000.0)
                serial.timeout = 0.0
                serial.dtr = True
                serial.rts = False
                return serial

            return factory

        def capture(self, target, *, version, seconds, raw_path, stall_before_read=0.0):
            return capture_tag_stream(
                target,
                version=version,
                seconds=max(0.02, seconds),
                serial_factory=self._factory(target, version),
                raw_path=raw_path,
                stall_before_read=stall_before_read,
            )

        def reconnect(self, target, *, cycles, seconds):
            return {"ok": True, "failures": [], "cycles": [{"cycle": 1}]}

        def dual_capture(self, targets, *, seconds, output_dir, label):
            devices = {
                side: self.capture(
                    target,
                    version=2,
                    seconds=seconds,
                    raw_path=output_dir / f"{label}-{side}-tag2.bin",
                )
                for side, target in targets.items()
            }
            return {"ok": all(item["ok"] for item in devices.values()), "devices": devices}

    report = run_hil(_config(tmp_path), backend=Backend())
    failures = [item for item in report.checks if item.verdict == FAIL]
    assert not failures, failures
    assert report.result == "warn"  # the explicitly omitted 72-hour gate remains visible
    for side in ("left", "right"):
        capture = json.loads(
            (report.run_dir / "captures" / f"{side}-tag2.json").read_text()
        )
        assert capture["saved_raw_reparsed_against_contract"] is True
        assert capture["tag2_contract_spec_sha256"] == (
            "e002287e4239dc85b547326f7da7871d62648c314f7394fed8d0b70adbcd9b0f"
        )
        assert Path(report.artifacts[f"before_{side}"].split(" sha256=", 1)[0]).stat().st_mode & 0o222 == 0
