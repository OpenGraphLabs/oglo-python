#!/usr/bin/env python3
"""Generate the standalone Phase-0 diagnostics contract and conformance bytes."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import struct
import unicodedata
import zlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "spec" / "diagnostics_v1"
VECTORS = OUT / "vectors"
PREFIX = b"OGLO-DIAGNOSTICS-CONTRACT-V1\0"
HEADER = struct.Struct("<2sBHIQQ")
CRC = struct.Struct("<I")
MAGIC = bytes.fromhex("a55b")
NONCE = bytes.fromhex("00112233445566778899aabbccddeeff")
BOOT = bytes.fromhex("102132435465768798a9bacbdcedfe0f")
STREAM = 0x0102030405060708
OTHER_STREAM = 0x1112131415161718
APP, CONFIG, CAL, CONTEXT = (bytes.fromhex(x * 32) for x in ("11", "22", "33", "44"))
MODEL = 0x4F474C4F  # "OGLO" LE model identifier
CONTEXT_MAX_UTF8 = 1024
CONTEXT_COMMAND_MAX = 1379  # b"SET CONTEXT " (12) + ceil(1024/3)*4 minus padding + LF
DEVICE_CONTEXT_FORMAT = "<16s32s32s32sIIHHHHBBB"
STREAM_EVIDENCE_FORMAT = "<16s16sQQIIII32s32s17IHBBBB"
COMMAND_RE = re.compile(
    rb"(?:STREAM TAG2 (?:ON|OFF) nonce=[0-9a-f]{32}|SET CONTEXT [A-Za-z0-9_-]+)\n"
)


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if not isinstance(key, str) or unicodedata.normalize("NFC", key) != key:
            raise ValueError("JSON keys must be NFC strings")
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def strict_json(raw: str) -> Any:
    if "\r" in raw:
        raise ValueError("canonical JSON uses LF only")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {x}")))


def canonical(value: Any) -> bytes:
    def norm(v: Any) -> Any:
        if v is None:
            raise ValueError("null is not allowed in canonical contract/context JSON")
        if isinstance(v, float):
            raise ValueError("floats are not allowed in canonical contract/context JSON")
        if isinstance(v, str):
            if unicodedata.normalize("NFC", v) != v:
                raise ValueError("strings must already be NFC")
            return v
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, dict):
            result = {}
            for k, x in v.items():
                if not isinstance(k, str) or unicodedata.normalize("NFC", k) != k or k in result:
                    raise ValueError("keys must be unique NFC strings")
                result[k] = norm(x)
            return result
        return v
    return json.dumps(norm(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def config_calibration_golden() -> tuple[bytes, bytes, bytes, bytes]:
    """Closed canonical preimages; never a native/C struct image or padding."""
    config_view={"schema":"oglo.config_view.v1","hardware_model_id":MODEL,"firmware_schema":6,"firmware_semver":{"major":0,"minor":9,"patch":12},"tag_ver_max":2,"tactile":{"taxel_count":80,"order":"finger,row,col","packing":"packed12","rate_hz":250},"imu":{"axes":["ax","ay","az","gx","gy","gz"],"raw":"i16le","rate_hz":500},"mag":{"axes":["mx","my","mz"],"raw":"i16le","rate_hz":125}}
    cal_view={"schema":"oglo.calibration_view.v1","tactile_baseline_u16":[0]*80,"tactile_threshold_u16":[0]*80,"imu_bias_i16":[0]*6,"mag_bias_i16":[0]*3}
    cb=canonical(config_view); kb=canonical(cal_view)
    return cb,kb,hashlib.sha256(b"OGLO-CONFIG-V1\0"+cb).digest(),hashlib.sha256(b"OGLO-CALIBRATION-V1\0"+kb).digest()


def validate_context(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > CONTEXT_MAX_UTF8:
        raise ValueError("context exceeds UTF-8 byte limit")
    value = strict_json(raw)
    if not isinstance(value, dict) or canonical(value).decode() != raw:
        raise ValueError("context must be compact sorted canonical JSON")
    required = {"schema", "context_epoch", "config_sha256", "calibration_sha256"}
    optional = {"capture_session_id", "recording_id", "fault_injection_id"}
    if set(value) - required - optional or not required <= set(value):
        raise ValueError("context has missing or unknown keys")
    if value["schema"] != "oglo.device_context.v1":
        raise ValueError("context schema must be oglo.device_context.v1")
    if type(value["context_epoch"]) is not int or not 0 <= value["context_epoch"] <= 0xffffffff:
        raise ValueError("context_epoch must be u32")
    for key in ("config_sha256", "calibration_sha256"):
        x = value[key]
        if not isinstance(x, str) or len(x) != 64 or any(c not in "0123456789abcdef" for c in x):
            raise ValueError(f"{key} must be lowercase SHA-256 hex")
    if any(not isinstance(value[k], str) or not value[k] for k in optional & set(value)):
        raise ValueError("context optional identifiers must be nonempty strings when present")
    return value


def decode_context_token(token: str) -> dict[str, Any]:
    """Decode the exact unpadded base64url SET CONTEXT argument."""
    if not isinstance(token, str) or not token or "=" in token or len("SET CONTEXT ".encode() + token.encode() + b"\n") > CONTEXT_COMMAND_MAX:
        raise ValueError("context token must be unpadded base64url")
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token):
        raise ValueError("context token has a non-base64url character")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("context token is not UTF-8 base64url") from exc
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != token:
        raise ValueError("context token is not canonical base64url")
    return validate_context(text)


def validate_command(command: bytes) -> tuple[str, str]:
    """Validate the exact LF-terminated, case-sensitive TAG2 command grammar."""
    if not isinstance(command, bytes) or not COMMAND_RE.fullmatch(command):
        raise ValueError("command must be exact ASCII with one LF and no extra tokens")
    text = command[:-1].decode("ascii")
    if text.startswith("STREAM TAG2 ON nonce="):
        return "start", text.rsplit("=", 1)[1]
    if text.startswith("STREAM TAG2 OFF nonce="):
        return "stop", text.rsplit("=", 1)[1]
    token = text.removeprefix("SET CONTEXT ")
    decode_context_token(token)
    return "context", token


def command_transition(
    lifecycle: str,
    command: bytes,
    *,
    active_nonce: str | None = None,
    retired_nonces: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Normative command admission model used by cross-language vectors."""
    kind, value = validate_command(command)
    result = {
        "lifecycle": lifecycle,
        "accepted": False,
        "response": "command_rejected",
        "reopen": False,
    }
    if active_nonce is not None:
        result["active_nonce"] = active_nonce
    if kind == "context":
        if lifecycle == "IDLE":
            result.update(accepted=True, response="context_ack")
        return result
    if kind == "start":
        if lifecycle in {"STARTING", "ACTIVE"} and value == active_nonce:
            result.update(accepted=True, response="cached_start_ack")
        elif lifecycle == "IDLE" and value not in retired_nonces:
            result.update(lifecycle="STARTING", active_nonce=value, accepted=True, response="new_start")
        elif value in retired_nonces:
            result["response"] = "nonce_retired"
        else:
            result["response"] = "nonce_conflict"
        return result
    if lifecycle in {"ACTIVE", "STOPPING"} and value == active_nonce:
        result.update(lifecycle="STOPPING", accepted=True, response="stop_or_cached_stop_ack")
    elif value in retired_nonces:
        result.update(accepted=True, response="cached_stop_ack")
    else:
        result["response"] = "nonce_conflict"
    return result


def _declared_len_possible(kind: int, length: int) -> bool:
    fixed = {1: 120, 128: 133, 129: 17, 130: 39, 131: 131, 132: 131, 133: 202}
    if kind in fixed:
        return length == fixed[kind]
    if kind == 2:
        return length in {1 + 14 * count for count in range(1, 9)}
    if kind == 3:
        return length in {1 + 8 * count for count in range(1, 9)}
    return False


def _payload_shape_valid(kind: int, payload: bytes) -> bool:
    if not _declared_len_possible(kind, len(payload)):
        return False
    if kind not in (2, 3):
        return True
    stride = 14 if kind == 2 else 8
    count = payload[0]
    if not 1 <= count <= 8 or len(payload) != 1 + count * stride:
        return False
    offsets = [struct.unpack_from("<H", payload, 1 + index * stride)[0] for index in range(count)]
    return offsets[0] == 0 and all(a < b for a, b in zip(offsets, offsets[1:]))


def parse_tag2(buf: bytes):
    """Small stdlib conformance parser: packets, exact tail, diagnostics."""
    packets=[]; bad=0; i=0
    while True:
        j=buf.find(MAGIC,i)
        if j < 0:
            # Only retain a trailing first magic byte; arbitrary junk must not grow
            # the carry buffer or become an imagined future frame prefix.
            return packets, (b"\xa5" if buf.endswith(b"\xa5") else b""), bad
        if len(buf)-j < 25: return packets,buf[j:],bad
        magic,typ,plen,seq,clock,stream=HEADER.unpack_from(buf,j)
        end=j+25+plen+4
        if plen>512 or not _declared_len_possible(typ, plen) or end>len(buf):
            if end>len(buf) and _declared_len_possible(typ, plen): return packets,buf[j:],bad
            bad+=1;i=j+1;continue
        raw=buf[j:end]
        if struct.unpack_from("<I",raw,end-j-4)[0]!=zlib.crc32(raw[:-4]): bad+=1;i=j+1;continue
        if not _payload_shape_valid(typ, raw[25:-4]): bad+=1;i=j+1;continue
        packets.append({"type":typ,"seq":seq,"device_time_us":clock,"stream_id":stream,"payload":raw[25:-4],"raw":raw});i=end


def admit(packets, stream_id, expected_seq=0, cached_controls=()):
    cached = set(cached_controls)
    state={"expected_seq":expected_seq,"accepted":[],"gaps":0,"stale":0,"duplicates":0,"control_replays":0,"fail_closed":False,"no_tag1_fallback":True}
    for p in packets:
        if p["stream_id"]!=stream_id: state["stale"]+=1;continue
        control_key = (p["type"], p["seq"], p["payload"])
        if control_key in cached:
            state["control_replays"] += 1
            continue
        delta=(p["seq"]-state["expected_seq"])&0xffffffff
        if delta==0: state["accepted"].append(p["seq"]);state["expected_seq"]=(state["expected_seq"]+1)&0xffffffff
        elif delta < 0x80000000: state["gaps"]+=delta;state["accepted"].append(p["seq"]);state["expected_seq"]=(p["seq"]+1)&0xffffffff
        else: state["duplicates"]+=1
    return state


def admit_start_ack(packets, *, expected_nonce: bytes, expected_contract_sha256: bytes,
                    expected_context_sha256: bytes, lifecycle: str = "STARTING"):
    """Derive TAG2 negotiation admission solely from parsed packet bytes."""
    result={"accepted":False,"fail_closed":True,"no_tag1_fallback":True,"reason":"no_valid_start_ack","stream_id":"","tail_packets":[]}
    if lifecycle != "STARTING": result["reason"]="wrong_lifecycle"; return result
    if not packets: return result
    first=packets[0]
    if first["type"] != 128 or first["seq"] != 0: result["reason"]="wrong_first_frame"; return result
    if first["stream_id"] == 0: result["reason"]="zero_stream_id"; return result
    try: version, nonce, boot, _app, _epoch, context, contract = struct.unpack("<B16s16s32sI32s32s",first["payload"])
    except struct.error: result["reason"]="bad_start_ack_payload"; return result
    if version != 2: result["reason"]="wrong_protocol_version"; return result
    if nonce != expected_nonce: result["reason"]="wrong_nonce"; return result
    if boot == b"\0"*16: result["reason"]="zero_boot_id"; return result
    if contract != expected_contract_sha256: result["reason"]="wrong_contract_sha256"; return result
    if context != expected_context_sha256: result["reason"]="wrong_context_sha256"; return result
    result.update(accepted=True,fail_closed=False,reason="accepted",stream_id=first["stream_id"],tail_packets=packets[1:])
    return result


def sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def entry(path: Path, data: bytes, provenance: str, status: str) -> dict[str, Any]:
    return {"name": path.name, "owner": "oglo-python", "path": path.relative_to(ROOT).as_posix(), "provenance": provenance, "status": status, "size": len(data), "sha256": sha(data)}


def frame(kind: int, payload: bytes, seq: int, clock: int, stream: int) -> bytes:
    head = HEADER.pack(MAGIC, kind, len(payload), seq, clock, stream)
    return head + payload + CRC.pack(zlib.crc32(head + payload))


def tactile() -> bytes:
    values = range(500, 580); out = bytearray()
    for a, b in zip(values[::2], values[1::2]): out += bytes((a >> 4, ((a & 15) << 4) | (b >> 8), b & 255))
    return bytes(out)


def imu_batch() -> bytes:
    return bytes([4]) + b"".join(struct.pack("<H6h", dt, 4096 + n, n, -4096, 164, 0, -164) for n, dt in enumerate((0, 2000, 4000, 6000)))


def mag_batch() -> bytes:
    return bytes([4]) + b"".join(struct.pack("<H3h", dt, 6842 + n, n, -3421) for n, dt in enumerate((0, 8000, 16000, 24000)))


def contract() -> dict[str, Any]:
    config_bytes, calibration_bytes, config_hash, calibration_hash = config_calibration_golden()
    f = lambda name, offset, wire_type, length, semantic, **rules: {"name": name, "offset": offset, "wire_type": wire_type, "endianness": "little" if wire_type not in ("bytes", "ascii") else "n/a", "length": length, "semantic": semantic, **rules}
    header_fields = [f("magic",0,"bytes",2,"TAG2 magic A5 5B",constant="a55b"),f("type",2,"u8",1,"frame type",invalid="unknown type rejected"),f("payload_len",3,"u16",2,"payload bytes only",invalid="0..512 and exact type layout"),f("seq",5,"u32",4,"global admitted TX sequence",rule="one space per stream_id"),f("device_time_us",9,"u64",8,"data first sample/control event time"),f("stream_id",17,"u64",8,"nonzero random boot-local stream",invalid="zero rejected")]
    raw_imu = [f("dt_us",0,"u16",2,"offset from header time",rule="relative to record base; first=0, later strictly increasing"), *[f(n,2+i*2,"i16",2,"raw IMU axis; relative to record base") for i,n in enumerate(("ax","ay","az","gx","gy","gz"))]]
    raw_mag = [f("dt_us",0,"u16",2,"offset from header time",rule="relative to record base; first=0, later strictly increasing"), *[f(n,2+i*2,"i16",2,"raw magnetometer axis; relative to record base") for i,n in enumerate(("mx","my","mz"))]]
    types = {
        "1": {"name": "tactile", "payload_len": 120, "payload_format": "packed12_80_taxels", "fields": [f("taxels",0,"packed_u12_be_pairs",120,"80 counts in finger,row,col order",formula="a=b0<<4|b1>>4; b=(b1&0x0f)<<8|b2; 0..4095") ]},
        "2": {"name": "imu", "payload_format": "count:u8 + count*(dt_us:u16 + raw:<6h)", "count_range": [1, 8], "payload_len_formula": "1+14*count", "first_dt_us": 0, "later_dt_us": "strictly_increasing", "default_batch_target": 4, "fields": [f("count",0,"u8",1,"sample count",valid="1..8"), {"name":"records","offset":1,"wire_type":"array","endianness":"little","length":"14*count","stride":14,"element_offset_base":"relative_to_each_record","semantic":"IMU samples ax,ay,az,gx,gy,gz","element_fields":raw_imu}]},
        "3": {"name": "mag", "payload_format": "count:u8 + count*(dt_us:u16 + raw:<3h)", "count_range": [1, 8], "payload_len_formula": "1+8*count", "first_dt_us": 0, "later_dt_us": "strictly_increasing", "default_batch_target": 4, "fields": [f("count",0,"u8",1,"sample count",valid="1..8"), {"name":"records","offset":1,"wire_type":"array","endianness":"little","length":"8*count","stride":8,"element_offset_base":"relative_to_each_record","semantic":"magnetometer samples mx,my,mz","element_fields":raw_mag}]},
        "128": {"name": "START_ACK", "payload_format": "<B16s16s32sI32s32s", "payload_len": 133, "fields": [f("protocol_version",0,"u8",1,"must be 2",constant=2),f("nonce",1,"bytes",16,"command nonce"),f("mcu_boot_id",17,"bytes",16,"random boot ID",invalid="zero"),f("application_sha256",33,"bytes",32,"firmware image digest"),f("config_epoch",65,"u32",4,"firmware config epoch"),f("context_sha256",69,"bytes",32,"host canonical context digest"),f("contract_sha256",101,"bytes",32,"contract input hash")]}, "129": {"name": "STOP_ACK", "payload_format": "<16sB", "payload_len": 17, "fields": [f("nonce",0,"bytes",16,"matching stop nonce"),f("stop_reason",16,"u8",1,"terminal reason enum",enum={"1":"requested"})]},
        "130": {"name": "STREAM_FAILURE", "payload_format": "<HBI32s", "payload_len": 39, "fields": [f("code",0,"u16",2,"failure enum",enum={"1":"nonce_conflict","2":"command_rejected","3":"context_changed","4":"tx_failure","5":"internal_error"}),f("fatal",2,"u8",1,"0 nonfatal 1 fatal",valid="0|1"),f("detail",3,"u32",4,"implementation detail"),f("context_sha256",7,"bytes",32,"context binding")]},
        "131": {"name": "BOOT_SUMMARY", "payload_format": "<16sHIIBBBBB32s32s32sI", "payload_len": 131, "fields": [f("mcu_boot_id",0,"bytes",16,"random nonzero per boot"),f("reset_reason",16,"u16",2,"stable reset enum",enum={"0":"unknown","1":"power_on","2":"external","3":"software","4":"panic","5":"interrupt_watchdog","6":"task_watchdog","7":"other_watchdog","8":"deep_sleep","9":"brownout","10":"sdio"}),f("reset_reason_raw",18,"u32",4,"esp_reset_reason raw"),f("hardware_model_id",22,"u32",4,"hardware model"),f("tag_ver_max",26,"u8",1,"TAG capability"),f("firmware_schema",27,"u8",1,"firmware schema"),f("firmware_semver_major",28,"u8",1,"semver major"),f("firmware_semver_minor",29,"u8",1,"semver minor"),f("firmware_semver_patch",30,"u8",1,"semver patch"),f("application_sha256",31,"bytes",32,"image digest"),f("config_sha256",63,"bytes",32,"config digest"),f("calibration_sha256",95,"bytes",32,"calibration digest"),f("config_epoch",127,"u32",4,"firmware-owned; starts 1 after boot NVS load; increments once after each successful config or calibration mutation")]},
        "132": {"name": "DEVICE_CONTEXT", "payload_format": DEVICE_CONTEXT_FORMAT, "payload_len": 131, "fields": [f("mcu_boot_id",0,"bytes",16,"boot binding"),f("context_sha256",16,"bytes",32,"host canonical context digest"),f("config_sha256",48,"bytes",32,"firmware config digest"),f("calibration_sha256",80,"bytes",32,"firmware calibration digest"),f("config_epoch",112,"u32",4,"firmware owned"),f("context_epoch",116,"u32",4,"host published only"),f("tactile_hz",120,"u16",2,"sample interpretation rate"),f("imu_hz",122,"u16",2,"sample interpretation rate"),f("mag_hz",124,"u16",2,"sample interpretation rate"),f("taxel_count",126,"u16",2,"must be 80",constant=80),f("tactile_packing_id",128,"u8",1,"packed12 finger,row,col",enum={"1":"packed12_finger_row_col"}),f("imu_axes_id",129,"u8",1,"axis order",enum={"1":"ax_ay_az_gx_gy_gz"}),f("mag_axes_id",130,"u8",1,"axis order",enum={"1":"mx_my_mz"})]},
        "133": {"name": "STREAM_EVIDENCE", "payload_format": STREAM_EVIDENCE_FORMAT, "payload_len": 202, "counter_rule": "all counters saturate at u32 max; counter_saturated=1 if any saturation occurred", "sequence_totals_rule": "first/last admitted exclude STREAM_EVIDENCE and STOP_ACK; evidence and STOP_ACK are excluded from transmitted totals", "fields": [f("nonce",0,"bytes",16,"stream nonce"),f("mcu_boot_id",16,"bytes",16,"boot binding"),f("start_time_us",32,"u64",8,"admitted start time"),f("end_time_us",40,"u64",8,"terminal event time"),f("start_config_epoch",48,"u32",4,"start firmware epoch"),f("end_config_epoch",52,"u32",4,"end firmware epoch"),f("start_context_epoch",56,"u32",4,"start host epoch"),f("end_context_epoch",60,"u32",4,"end host epoch"),f("start_context_sha256",64,"bytes",32,"start context"),f("end_context_sha256",96,"bytes",32,"end context"),f("first_admitted_seq",128,"u32",4,"first global seq excluding evidence/stop"),f("last_admitted_seq",132,"u32",4,"last global seq excluding evidence/stop"),*[f(f"{modality}_{stage}_samples",136+(mi*4+si)*4,"u32",4,"saturating sample counter") for mi,modality in enumerate(("tactile","imu","mag")) for si,stage in enumerate(("produced","enqueued","transmitted","dropped"))],f("queue_drops",184,"u32",4,"saturating frame admission failures"),f("short_writes",188,"u32",4,"saturating CDC short writes"),f("deadline_misses",192,"u32",4,"saturating sample/flush deadline misses"),f("terminal_reason",196,"u16",2,"terminal enum",enum={"1":"requested","2":"fatal_context_changed","3":"fatal_topology_changed","4":"tx_failure","5":"internal_error"}),f("counter_saturated",198,"u8",1,"any counter saturated",valid="0|1"),f("evidence_counts_as_transmitted",199,"u8",1,"must be 0",constant=0),f("stop_ack_counts_as_transmitted",200,"u8",1,"must be 0",constant=0),f("reserved",201,"u8",1,"must be zero",constant=0)]},
    }
    return {"schema": "oglo.diagnostics_contract.v1", "status": "implementation-contract-not-hardware-captured", "scope": "phase_0_schema_and_vectors_only_no_runtime_behavior_changes",
      "tag1": {"magic_hex": "a55a", "header_format": "<2sBHII", "header_len": 13, "header_fields": [f("magic",0,"bytes",2,"TAG1 magic A5 5A",constant="a55a"),f("type",2,"u8",1,"frame type",valid="1 tactile|2 imu|3 mag"),f("payload_len",3,"u16",2,"payload bytes",valid="120|12|6 by type"),f("seq",5,"u32",4,"per modality sequence"),f("timestamp_us",9,"u32",4,"per modality timestamp")], "types": {"1": {"name": "tactile", "payload_len": 120, "fields": [f("taxels",0,"packed_u12_be_pairs",120,"80 finger,row,col counts",formula="a=b0<<4|b1>>4; b=(b1&0x0f)<<8|b2")]}, "2": {"name": "imu", "payload_len": 12, "fields": [f(n,i*2,"i16",2,"raw IMU axis") for i,n in enumerate(("ax","ay","az","gx","gy","gz"))]}, "3": {"name": "mag", "payload_len": 6, "fields": [f(n,i*2,"i16",2,"raw magnetometer axis") for i,n in enumerate(("mx","my","mz"))]}}, "sequence_scope": "per_frame_type", "crc": "none", "physical_vectors": ["spec/vectors/tag_tactile_133b.bin", "spec/vectors/tag_imu_25b.bin", "spec/vectors/tag_mag_19b.bin"]},
      "tag2": {"magic_hex": "a55b", "header_format": "<2sBHIQQ", "header_len": 25, "header_fields": header_fields, "trailer_format": "<I", "trailer_len": 4, "max_payload_len": 512, "crc32": {"algorithm": "CRC-32/ISO-HDLC", "check_hex": "cbf43926", "reflected_polynomial_hex": "edb88320", "init_hex": "ffffffff", "xorout_hex": "ffffffff", "coverage": "exact_header_plus_payload", "reference": "zlib.crc32"}, "types": types, "sequence_scope": "one_u32_space_per_stream_id_in_TX_queue_wire_admission_order; START_ACK_seq_0; modulo_2^32; atomic_sequence_allocation_and_queue_admission; failed_queue_admission_does_not_consume_seq", "lifecycle_order": "START_ACK first; BOOT_SUMMARY then DEVICE_CONTEXT before data; requested STOP: producer boundary blocks new producer admission, drain accepted FIFO, STREAM_EVIDENCE, STOP_ACK last; fatal context/topology: producer boundary blocks new admission, drain accepted FIFO, STREAM_FAILURE, STREAM_EVIDENCE, no successful STOP_ACK; failure/evidence seq allocated after drained FIFO", "idempotent_control_duplicates": "same-nonce START replay only in STARTING/ACTIVE; matching STOP retry replays cached STOP_ACK within the same boot; retired nonce never reopens", "stream_id": "nonzero_random_u64_not_reused_within_one_mcu_boot", "device_time_us": "actual monotonic u64; data header is first sample time and sample=header+dt; per-modality reconstructed sample times strictly increase; global wire header times may move backward and are not causal order", "batch_flush_deadlines_ms": {"imu":8,"mag":32}, "transport_budget": "required batching capability: tactile 250_Hz*(25+120+4) + IMU 500_Hz/4*(25+(1+14*4)+4) + MAG 125_Hz/4*(25+(1+8*4)+4) = 49937.5_Bps (about 49938_Bps), ignoring boundary flush partial batches/control frames; estimate only", "physical_rollout_gates": {"estimated_steady_wire_bps_max":55000,"normal_run_required_zero":["queue_drops","malformed_frames","crc_failures","wrong_stream_frames"],"throughput_jitter_status":"unqualified_until_real_hardware","queue_ram_cap":"must_freeze_before_phase5_approval"}},
      "admission": {"capability": "GET CONFIG numeric tag_ver_max; missing is TAG1-only proof only for validated legacy firmware <=0.9.12/schema6; 1 selects TAG1; >=2 selects min(host_max,device_max); malformed_or_contradictory_fails_closed", "command_encoding":"strict ASCII, exact uppercase and single spaces, one LF; CRLF/leading/trailing/extra tokens rejected", "commands": {"start": "STREAM TAG2 ON nonce=<32 lowercase hex>\n", "stop": "STREAM TAG2 OFF nonce=<same>\n", "context":"SET CONTEXT <unpadded base64url canonical JSON>\n"}, "start": "first valid frame after ON is START_ACK matching nonce, stream_id, contract_sha256; preserve fragment/coalesced tail; invalid ACK/CRC/wrong stream never downgrades", "active": "same START nonce retry returns byte-identical cached ACK/current stream; different nonce gives nonfatal nonce_conflict/no transition; only matching STOP accepted; other commands/context mutation nonfatal command_rejected; context/topology mutation uses fatal terminal order", "stop": "same STOP retry returns cached STOP_ACK within boot; retired START nonce cannot reopen; new nonce after terminal gets new stream_id", "host": "CRC-valid wrong-stream frame is stale evidence: discard and continue without changing current expected seq/time/counters; never TAG1 fallback", "receiver_sequence": "expected seq modulo 2^32; exact accepts; forward distance <2^31 records gap then accepts; backward distance is duplicate/out-of-order; cached control replay neither advances nor gaps", "parser_resync": "scan negotiated magic only; retain incomplete frame exactly and one possible magic-prefix byte when no complete magic; invalid type/length/CRC advances scan by one byte after the first magic byte; never reinterpret TAG2 as TAG1"},
      "context": {"schema": "oglo.device_context.v1", "required": ["schema", "context_epoch", "config_sha256", "calibration_sha256"], "optional_omit_when_unknown": ["capture_session_id", "recording_id", "fault_injection_id"], "max_utf8_bytes": CONTEXT_MAX_UTF8, "max_base64url_bytes": 1366, "max_set_context_command_bytes": CONTEXT_COMMAND_MAX, "rules": "no_nulls; strings_NFC; hashes_lowercase_64_hex; u32_context_epoch_host_published_only; UTF-8_sorted_keys_compact_separators", "context_sha256": {"algorithm":"SHA256","preimage":"canonical UTF-8 bytes without LF"}, "config_sha256": {"algorithm":"SHA256","construction":"domain_separator_bytes || canonical_view_utf8","domain_separator_hex":b"OGLO-CONFIG-V1\0".hex(),"golden_sha256":config_hash.hex(),"view":{"schema":"oglo.config_view.v1","additional_fields":False,"fields":{"hardware_model_id":"u32","firmware_schema":"u8","firmware_semver":"{major:u8,minor:u8,patch:u8}","tag_ver_max":"u8","tactile":{"taxel_count":"80","order":"finger,row,col","packing":"packed12","rate_hz":"u16"},"imu":{"axes":"array[ax,ay,az,gx,gy,gz]","raw":"i16le","rate_hz":"u16"},"mag":{"axes":"array[mx,my,mz]","raw":"i16le","rate_hz":"u16"}}}}, "calibration_sha256": {"algorithm":"SHA256","construction":"domain_separator_bytes || canonical_view_utf8","domain_separator_hex":b"OGLO-CALIBRATION-V1\0".hex(),"golden_sha256":calibration_hash.hex(),"view":{"schema":"oglo.calibration_view.v1","additional_fields":False,"fields":{"tactile_baseline_u16":"array[80]","tactile_threshold_u16":"array[80]","imu_bias_i16":"array[6]","mag_bias_i16":"array[3]"}}}, "epoch_ownership":{"config_epoch":"firmware: starts 1 after boot NVS load; increments once per successful config or calibration mutation outside ACTIVE","context_epoch":"host: supplied only by SET CONTEXT and independent of config_epoch"}, "set_command": "INACTIVE-only exact ASCII SET CONTEXT <base64url-no-padding(canonical-json)>\\n", "ack": "#CONTEXT ok sha256=<64 lowercase hex>\\n", "rejection": "#CONTEXT error code=command_rejected\\n", "writer": "single_TX_writer_when_implemented", "active": "ACTIVE rejects mutation through typed STREAM_FAILURE command_rejected", "evidence": "ACK hash must equal START_ACK and DEVICE_CONTEXT context_sha256; mismatch is typed fatal termination/no TAG1 fallback"},
      "canonicalization": {"json": "UTF-8,NFC,LF,object_keys_sorted,compact_separators,no_duplicate_keys,no_null,no_NaN,no_float,unknown_optional_omitted; runtime_u64_and_ns_use_decimal_strings_when_JSON", "hash_preimage": "SHA256(b'OGLO-DIAGNOSTICS-CONTRACT-V1\\0' + canonical_json(manifest.contract_inputs))", "generated_vector_hashes": "inventory only; excluded from contract_sha256 preimage to avoid START_ACK self-reference"}}


def cases() -> dict[str, Any]:
    config_bytes, calibration_bytes, config_hash, calibration_hash = config_calibration_golden()
    valid = {"calibration_sha256": "3" * 64, "config_sha256": "2" * 64, "context_epoch": 7, "schema": "oglo.device_context.v1"}
    raw = canonical(valid).decode()
    token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    max_value = {**valid, "recording_id": ""}
    padding = CONTEXT_MAX_UTF8 - len(canonical(max_value))
    max_value["recording_id"] = "x" * padding
    max_raw = canonical(max_value).decode()
    oversize_value = {**max_value, "recording_id": max_value["recording_id"] + "x"}
    oversize_raw = canonical(oversize_value).decode()
    start_payload = struct.pack("<B16s16s32sI32s32s", 2, NONCE, BOOT, APP, 7, CONTEXT, bytes.fromhex("55" * 32))
    start = frame(128, start_payload, 0, (2 << 32) + 1, STREAM)
    boot = frame(131, struct.pack("<16sHIIBBBBB32s32s32sI", BOOT, 3, 3, MODEL, 2, 6, 0, 9, 12, APP, CONFIG, CAL, 7), 1, (2 << 32) + 2, STREAM)
    tactile_frame = frame(1, tactile(), 2, (2 << 32) + 3, STREAM)

    def parser_case(name: str, data: bytes, *, expected_seq: int = 0, cached=()) -> dict[str, Any]:
        packets, remainder, malformed = parse_tag2(data)
        state = admit(packets, STREAM, expected_seq, cached)
        return {
            "name": name,
            "input_hex": data.hex(),
            "initial_expected_seq": expected_seq,
            "expected_packets": [
                {"type": packet["type"], "seq": packet["seq"], "stream_id": str(packet["stream_id"])}
                for packet in packets
            ],
            "expected_remainder_hex": remainder.hex(),
            "expected_malformed": malformed,
            "expected_admission": state,
        }

    corrupted_data = bytearray(tactile_frame)
    corrupted_data[-1] ^= 1
    corrupted_ack = bytearray(start)
    corrupted_ack[-1] ^= 1
    wrong_stream = frame(1, tactile(), 0, (2 << 32) + 4, OTHER_STREAM)
    unknown = frame(99, b"", 0, (2 << 32) + 5, STREAM)
    bad_length = frame(1, b"\0", 0, (2 << 32) + 6, STREAM)
    sequence_cases = [
        parser_case("normal", frame(1, tactile(), 0, 10, STREAM) + frame(1, tactile(), 1, 11, STREAM)),
        parser_case("forward_gap", frame(1, tactile(), 0, 10, STREAM) + frame(1, tactile(), 2, 12, STREAM)),
        parser_case("duplicate", frame(1, tactile(), 0, 10, STREAM) + frame(1, tactile(), 0, 10, STREAM)),
        parser_case("out_of_order", frame(1, tactile(), 2, 10, STREAM), expected_seq=3),
        parser_case("wrap", frame(1, tactile(), 0xFFFFFFFE, 10, STREAM) + frame(1, tactile(), 0xFFFFFFFF, 11, STREAM) + frame(1, tactile(), 0, 12, STREAM), expected_seq=0xFFFFFFFE),
        parser_case("cached_start_ack", start, expected_seq=1, cached=((128, 0, start_payload),)),
    ]
    parser_cases = [
        parser_case("valid_data", tactile_frame, expected_seq=2),
        parser_case("crc_data_reject_continue", bytes(corrupted_data), expected_seq=2),
        parser_case("crc_start_ack_malformed", bytes(corrupted_ack)),
        parser_case("wrong_stream_stale", wrong_stream),
        parser_case("unknown_type", unknown),
        parser_case("bad_type_length", bad_length),
        parser_case("coalesced_start_boot", start + boot),
        parser_case("coalesced_start_partial_boot", start + boot[:40]),
        *[parser_case(f"split_start_{length}", start[:length]) for length in (1, 24, 25, 100)],
        *sequence_cases,
    ]
    nonce_hex = NONCE.hex()
    other_nonce = bytes.fromhex("ffeeddccbbaa99887766554433221100").hex()
    valid_commands = [
        f"STREAM TAG2 ON nonce={nonce_hex}\n".encode(),
        f"STREAM TAG2 OFF nonce={nonce_hex}\n".encode(),
        f"SET CONTEXT {token}\n".encode(),
    ]
    transition_inputs = [
        ("new_start", "IDLE", valid_commands[0], None, ()),
        ("same_start_active", "ACTIVE", valid_commands[0], nonce_hex, ()),
        ("different_start_active", "ACTIVE", f"STREAM TAG2 ON nonce={other_nonce}\n".encode(), nonce_hex, ()),
        ("matching_stop", "ACTIVE", valid_commands[1], nonce_hex, ()),
        ("retired_start", "IDLE", valid_commands[0], None, (nonce_hex,)),
        ("cached_stop", "IDLE", valid_commands[1], None, (nonce_hex,)),
        ("context_idle", "IDLE", valid_commands[2], None, ()),
        ("context_active", "ACTIVE", valid_commands[2], nonce_hex, ()),
    ]
    invalid_commands = [
        valid_commands[0][:-1],
        valid_commands[0][:-1] + b"\r\n",
        valid_commands[0].replace(b"STREAM", b"stream"),
        valid_commands[0].replace(b"TAG2 ON", b"TAG2  ON"),
        valid_commands[0].replace(nonce_hex.encode(), nonce_hex.upper().encode()),
        valid_commands[0][:-1] + b" extra\n",
    ]
    def start_case(name, wire, lifecycle="STARTING"):
        packets, tail, malformed = parse_tag2(wire)
        return {"name":name,"input_hex":wire.hex(),"lifecycle":lifecycle,"malformed":malformed,"remainder_hex":tail.hex(),"derived":admit_start_ack(packets,expected_nonce=NONCE,expected_contract_sha256=bytes.fromhex("55"*32),expected_context_sha256=CONTEXT,lifecycle=lifecycle)}
    def ack_variant(*, version=2, nonce=NONCE, boot_id=BOOT, contract=bytes.fromhex("55"*32), context=CONTEXT, stream=STREAM, kind=128):
        payload=struct.pack("<B16s16s32sI32s32s",version,nonce,boot_id,APP,7,context,contract)
        return frame(kind,payload,0,(2<<32)+1,stream)
    negotiation_cases=[
        start_case("start_positive",start), start_case("start_wrong_nonce",ack_variant(nonce=b"\x99"*16)), start_case("start_wrong_version",ack_variant(version=1)), start_case("start_wrong_contract",ack_variant(contract=b"\x77"*32)), start_case("start_wrong_context",ack_variant(context=b"\x66"*32)), start_case("start_zero_boot",ack_variant(boot_id=b"\0"*16)), start_case("start_zero_stream",ack_variant(stream=0)), start_case("start_wrong_first_type",ack_variant(kind=131)), start_case("start_wrong_lifecycle",start,"ACTIVE"), start_case("start_crc",bytes(corrupted_ack)),
    ]
    return {
        "status": "synthetic_conformance_cases_not_hardware_captures",
        "contexts": {
            "valid_canonical_json": raw,
            "valid_set_context_base64url": token,
            "context_sha256": sha(raw.encode()),
            "max_valid_canonical_json": max_raw,
            "max_valid_utf8_bytes": len(max_raw.encode()),
            "max_valid_token_bytes": len(base64.urlsafe_b64encode(max_raw.encode()).rstrip(b"=")),
            "max_valid_command_bytes": len(b"SET CONTEXT " + base64.urlsafe_b64encode(max_raw.encode()).rstrip(b"=") + b"\n"),
            "oversize_canonical_json": oversize_raw,
            "hash_admission": {"matching": "accept", "mismatch": "fatal_stream_failure_no_tag1_fallback"},
            "config_hash_golden": {"domain_separator_hex": b"OGLO-CONFIG-V1\0".hex(), "canonical_view_utf8": config_bytes.decode("utf-8"), "preimage_hex": (b"OGLO-CONFIG-V1\0" + config_bytes).hex(), "sha256": config_hash.hex()},
            "calibration_hash_golden": {"domain_separator_hex": b"OGLO-CALIBRATION-V1\0".hex(), "canonical_view_utf8": calibration_bytes.decode("utf-8"), "preimage_hex": (b"OGLO-CALIBRATION-V1\0" + calibration_bytes).hex(), "sha256": calibration_hash.hex()},
            "invalid": {"null": '{"calibration_sha256":null}', "non_nfc": '{"schema":"oglo.device_context.v1","recording_id":"e\\u0301"}', "uppercase_hash": '{"calibration_sha256":"A"}', "bad_hash": '{"calibration_sha256":"xyz"}', "bad_epoch": '{"context_epoch":-1}', "padded_base64url": token + "=", "non_url_safe": token[:-1] + "+", "bad_utf8": "_w"},
        },
        "commands": {
            "valid_hex": [command.hex() for command in valid_commands],
            "invalid_hex": [command.hex() for command in invalid_commands],
            "transitions": [
                {
                    "name": name,
                    "lifecycle": lifecycle,
                    "command_hex": command.hex(),
                    "active_nonce": active_nonce or "",
                    "retired_nonces": list(retired),
                    "expected": command_transition(lifecycle, command, active_nonce=active_nonce, retired_nonces=retired),
                }
                for name, lifecycle, command, active_nonce, retired in transition_inputs
            ],
            "fatal_order": ["producer_boundary", "drain_accepted_frames", "STREAM_FAILURE", "STREAM_EVIDENCE"],
            "fatal_stop_ack": "forbidden",
        },
        "parser_cases": parser_cases,
        "negotiation_cases": negotiation_cases,
        "timing": {
            "imu_default_offsets_us": [0, 2000, 4000, 6000],
            "mag_default_offsets_us": [0, 8000, 16000, 24000],
            "imu_flush_deadline_ms": 8,
            "mag_flush_deadline_ms": 32,
            "global_wire_header_monotonic_required": False,
            "per_modality_reconstructed_sample_monotonic_required": True,
        },
    }


def records(contract_hash: bytes):
    config_bytes, calibration_bytes, config_hash, calibration_hash = config_calibration_golden()
    start = struct.pack("<B16s16s32sI32s32s", 2, NONCE, BOOT, APP, 7, CONTEXT, contract_hash)
    evidence = struct.pack(
        STREAM_EVIDENCE_FORMAT,
        NONCE, BOOT, (2<<32)+100000, (2<<32)+160000,
        7, 7, 7, 7, CONTEXT, CONTEXT,
        0, 6,
        1, 1, 1, 0,
        4, 4, 4, 0,
        4, 4, 4, 0,
        0, 0, 0,
        1, 0, 0, 0, 0,
    )
    source = [("start_ack",128,start,{}), ("boot_summary",131,struct.pack("<16sHIIBBBBB32s32s32sI",BOOT,2,0xaabbccdd,MODEL,2,6,0,9,12,APP,config_hash,calibration_hash,7),{}), ("device_context",132,struct.pack(DEVICE_CONTEXT_FORMAT,BOOT,CONTEXT,config_hash,calibration_hash,7,7,250,500,125,80,1,1,1),{}), ("tactile",1,tactile(),{"counts_range":[500,579]}), ("imu",2,imu_batch(),{"count":4,"offsets_us":[0,2000,4000,6000]}), ("mag",3,mag_batch(),{"count":4,"offsets_us":[0,8000,16000,24000]}), ("stream_failure",130,struct.pack("<HBI32s",1,0,9,CONTEXT),{"code":"nonce_conflict","fatal":False}), ("stream_evidence",133,evidence,{"counts":"sample_totals","last_seq":"last_frame_seq"}), ("stop_ack",129,struct.pack("<16sB",NONCE,1),{})]
    def decoded(kind, payload, seq, clock):
        base = {"header":{"magic_hex":"a55b","type":kind,"payload_len":len(payload),"seq":seq,"device_time_us":str(clock),"stream_id":str(STREAM)}}
        if kind == 1:
            vals=[]
            for j in range(0,120,3): vals += [(payload[j]<<4)|(payload[j+1]>>4),((payload[j+1]&15)<<8)|payload[j+2]]
            base["payload"]={"taxels":vals,"order":"finger,row,col"}
        elif kind in (2,3):
            stride=14 if kind==2 else 8; names=("ax","ay","az","gx","gy","gz") if kind==2 else ("mx","my","mz")
            records=[]
            for j in range(payload[0]):
                values=struct.unpack_from("<H"+("6h" if kind==2 else "3h"),payload,1+j*stride); records.append({"dt_us":values[0],**dict(zip(names,values[1:]))})
            base["payload"]={"count":payload[0],"records":records}
        elif kind == 128:
            v=struct.unpack("<B16s16s32sI32s32s",payload); base["payload"]={"protocol_version":v[0],"nonce_hex":v[1].hex(),"mcu_boot_id_hex":v[2].hex(),"application_sha256":v[3].hex(),"config_epoch":v[4],"context_sha256":v[5].hex(),"contract_sha256":v[6].hex()}
        elif kind == 129:
            v=struct.unpack("<16sB",payload); base["payload"]={"nonce_hex":v[0].hex(),"stop_reason":v[1]}
        elif kind == 130:
            v=struct.unpack("<HBI32s",payload); base["payload"]={"code":v[0],"fatal":v[1],"detail":v[2],"context_sha256":v[3].hex()}
        elif kind == 131:
            v=struct.unpack("<16sHIIBBBBB32s32s32sI",payload); base["payload"]={"mcu_boot_id_hex":v[0].hex(),"reset_reason":v[1],"reset_reason_raw":v[2],"hardware_model_id":v[3],"tag_ver_max":v[4],"firmware_schema":v[5],"firmware_semver_major":v[6],"firmware_semver_minor":v[7],"firmware_semver_patch":v[8],"application_sha256":v[9].hex(),"config_sha256":v[10].hex(),"calibration_sha256":v[11].hex(),"config_epoch":v[12]}
        elif kind == 132:
            v=struct.unpack(DEVICE_CONTEXT_FORMAT,payload); base["payload"]={"mcu_boot_id_hex":v[0].hex(),"context_sha256":v[1].hex(),"config_sha256":v[2].hex(),"calibration_sha256":v[3].hex(),"config_epoch":v[4],"context_epoch":v[5],"tactile_hz":v[6],"imu_hz":v[7],"mag_hz":v[8],"taxel_count":v[9],"tactile_packing_id":v[10],"imu_axes_id":v[11],"mag_axes_id":v[12]}
        elif kind == 133:
            values=struct.unpack(STREAM_EVIDENCE_FORMAT,payload)
            counter_names=["first_admitted_seq","last_admitted_seq",*[f"{modality}_{stage}_samples" for modality in ("tactile","imu","mag") for stage in ("produced","enqueued","transmitted","dropped")],"queue_drops","short_writes","deadline_misses"]
            base["payload"]={"nonce_hex":values[0].hex(),"mcu_boot_id_hex":values[1].hex(),"start_time_us":str(values[2]),"end_time_us":str(values[3]),"start_config_epoch":values[4],"end_config_epoch":values[5],"start_context_epoch":values[6],"end_context_epoch":values[7],"start_context_sha256":values[8].hex(),"end_context_sha256":values[9].hex(),**dict(zip(counter_names,values[10:27])),"terminal_reason":values[27],"counter_saturated":values[28],"evidence_counts_as_transmitted":values[29],"stop_ack_counts_as_transmitted":values[30],"reserved":values[31]}
        else:
            base["payload_hex"]=payload.hex()
        return base
    return [{"name":n,"frame":frame(k,p,i,(2<<32)+100000+i*10000,STREAM),"type":k,"seq":i,"device_time_us":str((2<<32)+100000+i*10000),"stream_id":str(STREAM),"payload_len":len(p),"expected":decoded(k,p,i,(2<<32)+100000+i*10000)} for i,(n,k,p,x) in enumerate(source)]


def files() -> dict[Path, bytes]:
    c = contract(); cs = cases(); result = {OUT/"contract.json": canonical(c)+b"\n", OUT/"cases.json": canonical(cs)+b"\n"}
    physical = [ROOT/"spec/vectors/tag_tactile_133b.bin", ROOT/"spec/vectors/tag_imu_25b.bin", ROOT/"spec/vectors/tag_mag_19b.bin"]
    contract_entries = [entry(OUT/"contract.json",result[OUT/"contract.json"],"synthetic","implementation-contract-not-hardware-captured")] + [entry(p,p.read_bytes(),"physical_capture","existing_TAG1_capture") for p in physical]
    contract_entries.sort(key=lambda x:x["name"])
    cases_entry = entry(OUT/"cases.json",result[OUT/"cases.json"],"synthetic","implementation-contract-not-hardware-captured")
    inputs = {"schema":"oglo.diagnostics_manifest.v1","status":"implementation-contract-not-hardware-captured","canonicalization":c["canonicalization"],"contract_entries":contract_entries,"cases_entry":cases_entry}
    h = sha(PREFIX+canonical(inputs));
    for r in records(bytes.fromhex(h)):
        result[VECTORS/(r["name"]+".bin")] = r["frame"]
        result[VECTORS/(r["name"]+".expected.json")] = canonical({k:v for k,v in r.items() if k != "frame"})+b"\n"
    generated = [entry(p,d,"synthetic","implementation-contract-not-hardware-captured") for p,d in result.items() if p.parent == VECTORS]
    generated.sort(key=lambda x:x["name"])
    result[OUT/"manifest.json"] = canonical({"contract_inputs":inputs,"contract_sha256":h,"vector_set_sha256":sha(canonical(generated)),"generated_vector_entries":generated,"conformance_pins":"Python/C++/TypeScript pin both contract_sha256 and vector_set_sha256"})+b"\n"
    return result


def main() -> int:
    check = argparse.ArgumentParser(); check.add_argument("--check",action="store_true"); args=check.parse_args(); wanted=files()
    stale = sorted(p for p in VECTORS.glob("*") if p.is_file() and p not in wanted)
    bad = [p for p,d in wanted.items() if not p.exists() or p.read_bytes()!=d] + stale
    if args.check:
        if bad: print("out of date: "+", ".join(p.relative_to(ROOT).as_posix() for p in bad)); return 1
        print(f"verified {len(wanted)} diagnostics contract artifacts"); return 0
    for p,d in wanted.items(): p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(d)
    for p in stale: p.unlink()
    print(f"generated {len(wanted)} diagnostics contract artifacts"); return 0

if __name__ == "__main__": raise SystemExit(main())
