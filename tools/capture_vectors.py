#!/usr/bin/env python3
"""Capture golden vectors from a real board. Run once; commit the result.

This command touches hardware. The normal test suite only reads its checked-in
output, so `pytest` still passes on a laptop with nothing plugged in and in CI.

    python3 tools/capture_vectors.py                 # auto-detect
    python3 tools/capture_vectors.py --port /dev/...

Each vector is a `.bin` of real device bytes plus a `.expected.json` produced by the
small reference decoder in this file. That decoder intentionally does not import the
SDK wire decoder: using the code under test to manufacture its own expected result
would make the golden test circular.

Regenerating silently defeats the purpose. If firmware changes the bytes, the diff on
these files IS the review. Commit both, and read the diff.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import serial  # noqa: E402

VECTORS = Path(__file__).resolve().parent.parent / "spec" / "vectors"

# Reference-format constants are deliberately repeated here instead of imported from
# oglo._wire. A wrong edit to the production decoder must not automatically rewrite
# the expected answer to agree with itself.
TAG_MAGIC = b"\xa5\x5a"
TAG_HDR_LEN = 13
TAG_TACTILE, TAG_IMU, TAG_MAG = 1, 2, 3
TAXELS = 80
TAXEL_PACKED_LEN = 120
TAG_IMU_LEN = 12
TAG_MAG_LEN = 6
ACCEL_LSB_PER_G = 4096.0
GYRO_LSB_PER_DPS = 16.4
MAG_LSB_PER_GAUSS = 6842.0

def find_ports() -> list[str]:
    """Use SDK descriptor discovery. Never open unrelated ports to identify them."""
    from oglo._usb import list_candidates

    return [c.device for c in list_candidates()]


def open_port(port: str):
    """Use the SDK's opener so the DTR handling stays in one place.

    Firmware 0.9.9 uses TinyUSB, which gates CDC transmit on DTR.
    """
    from oglo._usb import open_serial

    return open_serial(port)


def send(s: serial.Serial, cmd: str) -> None:
    s.write((cmd + "\n").encode())
    s.flush()


def read_config(s: serial.Serial, timeout: float = 6.0) -> dict:
    # A previous process may have crashed with any one of the old stream modes on.
    # Shut all of them down before asking for text.
    send(s, "STREAM BIN OFF")
    send(s, "STREAM TAXEL OFF")
    send(s, "STREAM TAG OFF")
    time.sleep(0.4)
    s.reset_input_buffer()
    end = time.monotonic() + timeout
    next_request = 0.0
    buffered = bytearray()
    while time.monotonic() < end:
        now = time.monotonic()
        if now >= next_request:
            send(s, "GET CONFIG")
            next_request = now + 0.5
        chunk = s.read(9000)
        if chunk:
            buffered += chunk
            # Serial reads are arbitrary chunks. Only consume newline-terminated
            # lines and carry the partial tail into the next read.
            lines = bytes(buffered).split(b"\n")
            buffered = bytearray(lines.pop())
            for raw_line in lines:
                line = raw_line.decode("utf8", "replace").strip()
                if line.startswith("#CONFIG "):
                    try:
                        value = json.loads(line[len("#CONFIG "):])
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        return value
            if len(buffered) > 65536:
                buffered = buffered[-65536:]
        else:
            time.sleep(0.005)
    raise SystemExit(f"no #CONFIG from the board. Is it running OGLO firmware?")


def validate_capture_config(cfg: dict) -> bool:
    """Validate the capture contract without importing the production parser."""
    try:
        version_parts = []
        for part in str(cfg["fw_rev"]).split(".")[:3]:
            digits = ""
            for char in part:
                if not char.isdigit():
                    break
                digits += char
            if not digits:
                raise ValueError
            version_parts.append(int(digits))
        version = tuple(version_parts)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid fw_rev={cfg.get('fw_rev')!r}") from exc
    version += (0,) * (3 - len(version))
    if version < (0, 9, 9):
        raise ValueError(f"firmware {cfg.get('fw_rev')} is older than 0.9.9")
    if cfg.get("schema_ver") != 6:
        raise ValueError(f"schema_ver={cfg.get('schema_ver')!r}; expected 6")
    if cfg.get("values_per_sample") != TAXELS:
        raise ValueError(
            f"values_per_sample={cfg.get('values_per_sample')!r}; expected {TAXELS}"
        )
    if cfg.get("sample_shape") != [5, 4, 4]:
        raise ValueError(f"sample_shape={cfg.get('sample_shape')!r}; expected [5, 4, 4]")
    if cfg.get("imu_len") != 25:
        raise ValueError(f"imu_len={cfg.get('imu_len')!r}; expected 25")
    if not cfg.get("serial") or not cfg.get("hw_rev"):
        raise ValueError("config must include serial and hw_rev")
    return bool(cfg.get("has_mag", False))


def grab(s: serial.Serial, start: str, stop: str, seconds: float) -> bytes:
    s.reset_input_buffer()
    send(s, start)
    buf = bytearray()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        buf += s.read(4096)
    send(s, stop)
    time.sleep(0.3)
    s.reset_input_buffer()
    return bytes(buf)


def jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 9)  # keep the file diffable across platforms
    return obj


def decode_reference(packet: bytes) -> Dict[str, Any]:
    """Decode one tagged packet without calling the SDK decoder under test."""
    if len(packet) < TAG_HDR_LEN:
        raise ValueError(f"packet is {len(packet)} B, shorter than the tagged header")
    magic, ptype, plen, seq, t_us = struct.unpack_from("<2sBHII", packet)
    if magic != TAG_MAGIC:
        raise ValueError("packet does not start with tagged-stream magic")
    expected_len = {
        TAG_TACTILE: TAXEL_PACKED_LEN,
        TAG_IMU: TAG_IMU_LEN,
        TAG_MAG: TAG_MAG_LEN,
    }.get(ptype)
    if expected_len is None:
        raise ValueError(f"unknown tagged packet type {ptype}")
    if plen != expected_len or len(packet) != TAG_HDR_LEN + plen:
        raise ValueError(
            f"type {ptype} declares {plen} B and file is {len(packet)} B; "
            f"expected exactly {expected_len} payload bytes"
        )

    payload = packet[TAG_HDR_LEN:]
    if ptype == TAG_TACTILE:
        counts = []
        for offset in range(0, TAXEL_PACKED_LEN, 3):
            b0, b1, b2 = payload[offset:offset + 3]
            counts.extend(((b0 << 4) | (b1 >> 4), ((b1 & 0x0F) << 8) | b2))
        return {"seq": seq, "t_us": t_us, "counts": counts}
    if ptype == TAG_IMU:
        raw = struct.unpack("<6h", payload)
        return {
            "seq": seq,
            "t_us": t_us,
            "accel": [v / ACCEL_LSB_PER_G for v in raw[:3]],
            "gyro": [v / GYRO_LSB_PER_DPS for v in raw[3:]],
            "raw": list(raw),
        }
    raw = struct.unpack("<3h", payload)
    return {
        "seq": seq,
        "t_us": t_us,
        "field": [v / MAG_LSB_PER_GAUSS for v in raw],
        "raw": list(raw),
    }


def collect_vectors(raw: bytes, *, has_mag: bool) -> Dict[str, Tuple[bytes, Dict[str, Any]]]:
    """Return a complete capture set, or fail before touching checked-in files."""
    specs = [
        (TAG_TACTILE, TAXEL_PACKED_LEN, "tag_tactile"),
        (TAG_IMU, TAG_IMU_LEN, "tag_imu"),
    ]
    if has_mag:
        specs.append((TAG_MAG, TAG_MAG_LEN, "tag_mag"))

    out: Dict[str, Tuple[bytes, Dict[str, Any]]] = {}
    missing = []
    for ptype, payload_len, prefix in specs:
        one = _first_whole_packet(raw, ptype, payload_len)
        if one is None:
            missing.append(prefix.removeprefix("tag_"))
            continue
        out[f"{prefix}_{len(one)}b"] = (one, decode_reference(one))
    if missing:
        raise RuntimeError(
            "tagged capture is incomplete; no complete " + ", ".join(missing)
            + " packet was seen. Existing vectors were not changed."
        )
    return out


def write_vector_set(
    vectors: Dict[str, Tuple[bytes, Dict[str, Any]]],
    meta: dict,
    *,
    directory: Path = VECTORS,
) -> None:
    """Replace one complete tagged-vector set and remove obsolete length variants."""
    directory.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for name, (raw, decoded) in vectors.items():
            outputs = {
                directory / f"{name}.bin": raw,
                directory / f"{name}.expected.json": (
                    json.dumps({"meta": meta, "decoded": jsonable(decoded)}, indent=1) + "\n"
                ).encode(),
            }
            for destination, content in outputs.items():
                temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
                with temporary.open("xb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                staged.append((temporary, destination))

        for temporary, destination in staged:
            os.replace(temporary, destination)

        keep = {
            filename
            for name in vectors
            for filename in (f"{name}.bin", f"{name}.expected.json")
        }
        for path in directory.glob("tag_*"):
            if path.is_file() and path.name not in keep and (
                path.suffix == ".bin" or path.name.endswith(".expected.json")
            ):
                path.unlink()
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    for name, (raw, _) in vectors.items():
        print(f"  {name}.bin  {len(raw)} B  -> {name}.expected.json")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", help="serial port; omit to auto-detect")
    ap.add_argument("--seconds", type=float, default=2.0)
    args = ap.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        raise SystemExit("--seconds must be a finite positive number")

    port = args.port
    if not port:
        found = find_ports()
        if not found:
            raise SystemExit("no serial ports found; pass --port")
        if len(found) > 1:
            raise SystemExit(
                "more than one glove is attached; pass --port explicitly: " + ", ".join(found)
            )
        port = found[0]
        print(f"using {port}  (candidates: {found})")

    s = open_port(port)
    try:
        cfg = read_config(s)
        try:
            has_mag = validate_capture_config(cfg)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"board does not satisfy the SDK contract: {exc}") from exc
        meta = {
            k: cfg.get(k)
            for k in ("serial", "hw_rev", "fw_rev", "rate_hz", "imu_len", "has_mag")
        }
        print(f"board: {meta}")

        # One vector per required modality. Nothing is written until the complete
        # set has been captured and independently decoded.
        raw = grab(s, "STREAM TAG ON", "STREAM TAG OFF", args.seconds)
        try:
            vectors = collect_vectors(raw, has_mag=has_mag)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        write_vector_set(vectors, meta)
    finally:
        s.close()
    print(f"\nwrote to {VECTORS}")
    print("Commit both files. Do NOT regenerate silently -- the diff is the review.")
    return 0


def _first_whole_packet(raw: bytes, ptype: int, payload_len: int) -> bytes | None:
    i = 0
    while True:
        j = raw.find(TAG_MAGIC, i)
        if j < 0 or len(raw) - j < TAG_HDR_LEN:
            return None
        plen = int.from_bytes(raw[j + 3:j + 5], "little")
        end = j + TAG_HDR_LEN + plen
        if raw[j + 2] == ptype and plen == payload_len and end <= len(raw):
            return raw[j:end]
        i = j + 2


if __name__ == "__main__":
    raise SystemExit(main())
