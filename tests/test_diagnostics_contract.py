"""Phase-0 artifacts are deterministic, byte-exact, and runtime independent."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "diagnostics_v1"
PREFIX = b"OGLO-DIAGNOSTICS-CONTRACT-V1\0"
module_spec = importlib.util.spec_from_file_location("contract_gen", ROOT / "tools" / "generate_diagnostics_contract.py")
gen = importlib.util.module_from_spec(module_spec)
assert module_spec.loader is not None
module_spec.loader.exec_module(gen)


def test_generator_check_and_stale_detection():
    result = subprocess.run([sys.executable, "tools/generate_diagnostics_contract.py", "--check"], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    stale = SPEC / "vectors" / ".stale"
    stale.write_bytes(b"stale")
    try:
        result = subprocess.run([sys.executable, "tools/generate_diagnostics_contract.py", "--check"], cwd=ROOT, text=True, capture_output=True)
        assert result.returncode == 1 and ".stale" in result.stdout
    finally:
        stale.unlink()


def test_tag2_exact_layout_crc_batching_and_global_sequence_order():
    tag2 = json.loads((SPEC / "contract.json").read_text())["tag2"]
    assert tag2["header_format"] == "<2sBHIQQ" and tag2["header_len"] == 25
    assert tag2["types"]["1"]["payload_len"] == 120
    assert tag2["types"]["2"]["payload_len_formula"] == "1+14*count"
    assert tag2["types"]["3"]["payload_len_formula"] == "1+8*count"
    assert tag2["crc32"]["check_hex"] == "cbf43926"
    names = ["start_ack", "boot_summary", "device_context", "tactile", "imu", "mag", "stream_failure", "stream_evidence", "stop_ack"]
    seqs = []
    for name in names:
        raw = (SPEC / "vectors" / f"{name}.bin").read_bytes()
        magic, kind, plen, seq, clock, stream = struct.unpack("<2sBHIQQ", raw[:25])
        assert magic == b"\xa5\x5b" and len(raw) == 25 + plen + 4
        assert struct.unpack("<I", raw[-4:])[0] == zlib.crc32(raw[:-4])
        assert clock > 2**32 and stream != 0
        expected = json.loads((SPEC / "vectors" / f"{name}.expected.json").read_text())
        assert (kind, seq, str(clock), str(stream), plen) == (expected["type"], expected["seq"], expected["device_time_us"], expected["stream_id"], expected["payload_len"])
        seqs.append(seq)
        if kind in (2, 3):
            count = raw[25]; stride = 14 if kind == 2 else 8
            assert 1 <= count <= 8 and plen == 1 + stride * count
            offsets = [struct.unpack_from("<H", raw, 26 + i * stride)[0] for i in range(count)]
            assert offsets[0] == 0 and offsets == sorted(offsets) and len(set(offsets)) == count
    assert seqs == list(range(len(names)))
    assert names[0] == "start_ack" and names[-1] == "stop_ack"
    assert tag2["lifecycle_order"].startswith("START_ACK first")
    # Exact default-rate estimate: 250*(25+120+4) + 500/4*(25+(1+14*4)+4)
    # + 125/4*(25+(1+8*4)+4). It deliberately excludes partial batches/control.
    assert 250 * 149 + (500 / 4) * 86 + (125 / 4) * 62 == 49937.5
    assert "49937.5_Bps" in tag2["transport_budget"]


def test_manifest_hash_view_and_every_artifact_digest():
    manifest = json.loads((SPEC / "manifest.json").read_text())
    assert manifest["contract_sha256"] == hashlib.sha256(PREFIX + gen.canonical(manifest["contract_inputs"])).hexdigest()
    assert manifest["vector_set_sha256"] == hashlib.sha256(gen.canonical(manifest["generated_vector_entries"])).hexdigest()
    for item in manifest["contract_inputs"]["contract_entries"] + [manifest["contract_inputs"]["cases_entry"]] + manifest["generated_vector_entries"]:
        raw = (ROOT / item["path"]).read_bytes()
        assert item["size"] == len(raw) and item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(manifest["generated_vector_entries"]) == 18
    start = (SPEC / "vectors" / "start_ack.bin").read_bytes()
    assert start[25 + 1 + 16 + 16 + 32 + 4 + 32:25 + 133] == bytes.fromhex(manifest["contract_sha256"])
    for expected_path in (SPEC / "vectors").glob("*.expected.json"):
        expected = json.loads(expected_path.read_text())
        assert set(expected["expected"]) >= {"header"}
        assert expected["expected"].get("payload") or expected["expected"].get("payload_hex")


def test_canonical_context_profile_and_negative_examples():
    cases = json.loads((SPEC / "cases.json").read_text())["contexts"]
    valid = cases["valid_canonical_json"]
    assert gen.validate_context(valid)["schema"] == "oglo.device_context.v1"
    token = cases["valid_set_context_base64url"]
    assert "=" not in token and base64.urlsafe_b64decode(token + "==").decode() == valid
    for name, raw in cases["invalid"].items():
        try:
            if name in ("padded_base64url", "non_url_safe", "bad_utf8"):
                gen.decode_context_token(raw)
            else:
                gen.validate_context(raw)
        except ValueError: pass
        else: raise AssertionError(raw)
    for bad in ('{"x":null}', '{"x":1.0}', '{"e\\u0301":1}', '{"x":1,"x":2}'):
        try: gen.canonical(gen.strict_json(bad))
        except ValueError: pass
        else: raise AssertionError(bad)


def test_control_vectors_decode_every_named_field_and_epoch_owner():
    contract = json.loads((SPEC / "contract.json").read_text())
    expected = {
        name: json.loads((SPEC / "vectors" / f"{name}.expected.json").read_text())["expected"]["payload"]
        for name in ("start_ack", "stop_ack", "stream_failure", "boot_summary", "device_context", "stream_evidence")
    }
    boot = expected["boot_summary"]
    assert (boot["firmware_semver_major"], boot["firmware_semver_minor"], boot["firmware_semver_patch"]) == (0, 9, 12)
    assert boot["reset_reason"] == 2 and boot["config_epoch"] == 7
    assert len((SPEC / "vectors" / "boot_summary.bin").read_bytes()) == 25 + 131 + 4
    context = expected["device_context"]
    assert (context["config_epoch"], context["context_epoch"]) == (7, 7)
    assert (context["taxel_count"], context["tactile_packing_id"], context["imu_axes_id"], context["mag_axes_id"]) == (80, 1, 1, 1)
    assert len((SPEC / "vectors" / "device_context.bin").read_bytes()) == 25 + 131 + 4
    evidence = expected["stream_evidence"]
    for modality, total in (("tactile", 1), ("imu", 4), ("mag", 4)):
        assert evidence[f"{modality}_produced_samples"] == total
        assert evidence[f"{modality}_enqueued_samples"] == total
        assert evidence[f"{modality}_transmitted_samples"] == total
        assert evidence[f"{modality}_dropped_samples"] == 0
    assert evidence["queue_drops"] == evidence["short_writes"] == evidence["deadline_misses"] == 0
    assert evidence["evidence_counts_as_transmitted"] == evidence["stop_ack_counts_as_transmitted"] == 0
    assert evidence["reserved"] == 0
    assert contract["context"]["epoch_ownership"]["config_epoch"].startswith("firmware")
    assert contract["context"]["epoch_ownership"]["context_epoch"].startswith("host")
    for kind in (128, 129, 130, 131, 132, 133):
        fields = contract["tag2"]["types"][str(kind)]["fields"]
        assert all({"name", "offset", "wire_type", "length", "semantic"} <= set(field) for field in fields)


def test_executable_parser_admission_vectors_and_tail_preservation():
    cases = json.loads((SPEC / "cases.json").read_text())
    by_name = {case["name"]: case for case in cases["parser_cases"]}
    for case in cases["parser_cases"]:
        packets, remainder, malformed = gen.parse_tag2(bytes.fromhex(case["input_hex"]))
        cached = ()
        if case["name"] == "cached_start_ack":
            packet = packets[0]
            cached = ((packet["type"], packet["seq"], packet["payload"]),)
        state = gen.admit(packets, gen.STREAM, case["initial_expected_seq"], cached)
        assert remainder.hex() == case["expected_remainder_hex"]
        assert malformed == case["expected_malformed"]
        assert state == case["expected_admission"]
    assert by_name["crc_data_reject_continue"]["expected_malformed"] == 1
    assert by_name["crc_start_ack_malformed"]["expected_malformed"] == 1
    assert by_name["wrong_stream_stale"]["expected_admission"]["stale"] == 1
    assert by_name["wrong_stream_stale"]["expected_admission"]["expected_seq"] == 0
    assert by_name["forward_gap"]["expected_admission"]["gaps"] == 1
    assert by_name["duplicate"]["expected_admission"]["duplicates"] == 1
    assert by_name["wrap"]["expected_admission"]["expected_seq"] == 1
    assert by_name["cached_start_ack"]["expected_admission"]["control_replays"] == 1
    partial = by_name["coalesced_start_partial_boot"]
    assert len(partial["expected_packets"]) == 1 and partial["expected_remainder_hex"]
    assert all(case["expected_admission"]["no_tag1_fallback"] for case in cases["parser_cases"])


def test_exact_command_grammar_nonce_lifecycle_and_fatal_order():
    cases = json.loads((SPEC / "cases.json").read_text())["commands"]
    assert [gen.validate_command(bytes.fromhex(raw))[0] for raw in cases["valid_hex"]] == ["start", "stop", "context"]
    for raw in cases["invalid_hex"]:
        try:
            gen.validate_command(bytes.fromhex(raw))
        except ValueError:
            pass
        else:
            raise AssertionError(raw)
    transitions = {item["name"]: item for item in cases["transitions"]}
    for item in transitions.values():
        active = item["active_nonce"] or None
        actual = gen.command_transition(item["lifecycle"], bytes.fromhex(item["command_hex"]), active_nonce=active, retired_nonces=tuple(item["retired_nonces"]))
        assert actual == item["expected"]
    assert transitions["same_start_active"]["expected"]["response"] == "cached_start_ack"
    assert transitions["retired_start"]["expected"]["response"] == "nonce_retired"
    assert transitions["retired_start"]["expected"]["reopen"] is False
    assert transitions["different_start_active"]["expected"]["accepted"] is False
    assert cases["fatal_order"] == ["producer_boundary", "drain_accepted_frames", "STREAM_FAILURE", "STREAM_EVIDENCE"]
    assert cases["fatal_stop_ack"] == "forbidden"


def test_context_byte_limits_hash_preimages_and_physical_gates():
    cases = json.loads((SPEC / "cases.json").read_text())["contexts"]
    contract = json.loads((SPEC / "contract.json").read_text())
    maximum = cases["max_valid_canonical_json"]
    assert len(maximum.encode()) == gen.CONTEXT_MAX_UTF8 == cases["max_valid_utf8_bytes"]
    assert cases["max_valid_token_bytes"] == 1366
    assert cases["max_valid_command_bytes"] == gen.CONTEXT_COMMAND_MAX == 1379
    gen.validate_context(maximum)
    try:
        gen.validate_context(cases["oversize_canonical_json"])
    except ValueError:
        pass
    else:
        raise AssertionError("1025-byte context accepted")
    assert hashlib.sha256(cases["valid_canonical_json"].encode()).hexdigest() == cases["context_sha256"]
    assert contract["context"]["config_sha256"]["view"]["additional_fields"] is False
    assert contract["context"]["calibration_sha256"]["view"]["fields"]["tactile_baseline_u16"] == "array[80]"
    for name in ("config", "calibration"):
        golden = cases[f"{name}_hash_golden"]
        domain = bytes.fromhex(golden["domain_separator_hex"])
        view = golden["canonical_view_utf8"].encode("utf-8")
        assert domain.endswith(b"\0")
        assert (domain + view).hex() == golden["preimage_hex"]
        assert hashlib.sha256(domain + view).hexdigest() == golden["sha256"]
        assert contract["context"][f"{name}_sha256"]["golden_sha256"] == golden["sha256"]
    gates = contract["tag2"]["physical_rollout_gates"]
    assert gates["estimated_steady_wire_bps_max"] == 55000
    assert gates["throughput_jitter_status"] == "unqualified_until_real_hardware"
    assert gates["queue_ram_cap"] == "must_freeze_before_phase5_approval"


def test_default_batch_timing_and_per_modality_clock_rule():
    cases = json.loads((SPEC / "cases.json").read_text())["timing"]
    contract = json.loads((SPEC / "contract.json").read_text())["tag2"]
    assert cases["imu_default_offsets_us"] == [0, 2000, 4000, 6000]
    assert cases["mag_default_offsets_us"] == [0, 8000, 16000, 24000]
    assert (cases["imu_flush_deadline_ms"], cases["mag_flush_deadline_ms"]) == (8, 32)
    assert cases["global_wire_header_monotonic_required"] is False
    assert cases["per_modality_reconstructed_sample_monotonic_required"] is True
    assert contract["batch_flush_deadlines_ms"] == {"imu": 8, "mag": 32}


def test_start_ack_negotiation_is_derived_from_parsed_bytes():
    cases = json.loads((SPEC / "cases.json").read_text())["negotiation_cases"]
    outcomes = {}
    for case in cases:
        packets, remainder, malformed = gen.parse_tag2(bytes.fromhex(case["input_hex"]))
        actual = gen.admit_start_ack(
            packets,
            expected_nonce=gen.NONCE,
            expected_contract_sha256=bytes.fromhex("55" * 32),
            expected_context_sha256=gen.CONTEXT,
            lifecycle=case["lifecycle"],
        )
        assert actual == case["derived"]
        outcomes[case["name"]] = actual
    assert outcomes["start_positive"]["accepted"] is True
    for name, reason in (("start_wrong_nonce", "wrong_nonce"), ("start_wrong_version", "wrong_protocol_version"), ("start_wrong_contract", "wrong_contract_sha256"), ("start_wrong_context", "wrong_context_sha256"), ("start_zero_boot", "zero_boot_id"), ("start_zero_stream", "zero_stream_id"), ("start_wrong_first_type", "no_valid_start_ack"), ("start_wrong_lifecycle", "wrong_lifecycle"), ("start_crc", "no_valid_start_ack")):
        value = outcomes[name]
        assert not value["accepted"] and value["fail_closed"] and value["no_tag1_fallback"]
        assert value["reason"] == reason
