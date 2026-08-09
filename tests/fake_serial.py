"""A serial port that never existed.

`UsbTransport` takes a serial-like object rather than opening one, so the entire
handshake and read path runs against this. It also models the two behaviours that
actually break real code: reads return *whatever happened to be ready*, not neat
frames, and a board answers commands rather than replaying a fixed script.
"""

from __future__ import annotations

import json
import struct
import time
from typing import Callable, Dict, List, Optional

from oglo import _wire as w

CFG_V6 = {
    "device": "oglo", "schema_ver": 6, "serial": "OGLO-L-TEST01", "side": "left",
    "hw_rev": "RDR02_FLEX5_REV_D_TIA", "fw_rev": "0.9.10", "rate_hz": 250,
    "samples_per_packet": 3, "imu_len": 25, "has_mag": True, "values_per_sample": 80,
    "sample_shape": [5, 4, 4],
    "channels": ["pinky", "ring", "middle", "index", "thumb"],
    "device_id": "oglo-test-device-01", "batch": "",
    "factory_passed": True, "stream_clean": True, "stream_thr": 80,
    "zero_valid": True, "cal_lock": False,
}

COUNTS = [550 + (i % 17) for i in range(w.TAXELS)]


def tag(ptype: int, seq: int, t_us: int, payload: bytes) -> bytes:
    return w.TAG_MAGIC + bytes([ptype]) + struct.pack("<HII", len(payload), seq, t_us) + payload


def tagged_burst(n_tactile: int = 4, *, start_seq: int = 0) -> bytes:
    """Tactile at 250 Hz with IMU at 500 and mag at 125, the shipping ratio."""
    out = bytearray()
    for k in range(n_tactile):
        seq = (start_seq + k) & 0xFFFFFFFF
        t = (4000 * seq) & 0xFFFFFFFF
        out += tag(w.TAG_TACTILE, seq, t, w.pack12(COUNTS))
        for j in range(2):  # IMU is 2x tactile
            out += tag(
                w.TAG_IMU,
                (seq * 2 + j) & 0xFFFFFFFF,
                (t + 2000 * j) & 0xFFFFFFFF,
                struct.pack("<6h", 777, -531, -3982, -5, -8, 1),
            )
        if seq % 2 == 0:  # mag is a quarter of IMU
            out += tag(
                w.TAG_MAG, (seq // 2) & 0xFFFFFFFF, t,
                struct.pack("<3h", 3142, 678, -1107),
            )
    return bytes(out)

class FakeSerial:
    """Answers commands the way a board does; hands back bytes in ragged chunks."""

    def __init__(
        self,
        config: Optional[Dict] = None,
        *,
        stream: bytes = b"",
        chunk: int = 64,
        config_after: int = 1,
        echo_unknown: bool = True,
        locked: bool = False,
        sweep_completes: bool = True,
        hz: Optional[float] = None,
    ) -> None:
        self.config = config
        self._out = bytearray()
        self._stream = stream
        self._streaming = False
        self.chunk = chunk
        #: Answer GET CONFIG only from the Nth ask, to exercise the retry.
        self._config_after = config_after
        self._config_asks = 0
        self._echo_unknown = echo_unknown
        self.locked = locked
        self.sweep_completes = sweep_completes
        #: Paced refill. Without it the fake replays one burst as fast as the reader
        #: asks, which reports impossible rates and fabricates sequence loss by
        #: repeating the same seq numbers forever.
        self.hz = hz
        # A real producer continues its modality sequences after bytes that were
        # already queued at STREAM ON. Starting every synthetic refill at zero made
        # the fake manufacture duplicate/backward packets and hid bugs whenever the
        # recorder did not treat those anomalies as data-integrity failures.
        initial, _ = w.iter_tagged(stream)
        tactile_seqs = [p.seq for p in initial if isinstance(p, w.TactilePacket)]
        self._next_tactile_seq = ((tactile_seqs[-1] + 1) & 0xFFFFFFFF) if tactile_seqs else 0
        self._next_refill: Optional[float] = None
        self.commands: List[str] = []
        self.closed = False
        self._burst_tactile = 4
        self._burst_secs = (self._burst_tactile / hz) if hz else 0.0
        self.zero_recipe = {
            "valid": True,
            "count": w.TAXELS,
            "frames": 0,
            "thr": int((config or {}).get("stream_thr", 0) or 0),
            "clean": bool((config or {}).get("stream_clean", False)),
            "locked": bool(locked),
            "baseline": [550 + (i % 17) for i in range(w.TAXELS)],
            "noise": [2 + (i % 3) for i in range(w.TAXELS)],
        }
        self.status = {
            "uptime_ms": 1234,
            "seq": 10,
            "imu_ok": True,
            "imu": {"ok": True, "mag_ok": bool((config or {}).get("has_mag", False))},
            "sensor_ok": True,
            "error_flags": 0,
            "deadline_misses": 0,
            "tag_dropped": int((config or {}).get("tag_dropped", 0) or 0),
            "tag_short_writes": 0,
        }

    # -- SerialLike -------------------------------------------------------------

    def write(self, data: bytes) -> int:
        for line in data.decode("utf8", "replace").splitlines():
            cmd = line.strip()
            if cmd:
                self.commands.append(cmd)
                self._handle(cmd)
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        if self._streaming and len(self._out) < size:
            self._out += self._refill()  # a real board keeps producing
        n = min(size, self.chunk, len(self._out))
        if n <= 0:
            return b""
        out = bytes(self._out[:n])
        del self._out[:n]
        return out

    def _refill(self) -> bytes:
        """More stream, with sequence numbers that continue and (optionally) a rate.

        Repeating the same bytes verbatim would restart the sequence every burst,
        which a correct reader must report as enormous loss -- an artefact of the
        fake, not of anything under test.
        """
        if not self._stream:
            return b""  # nothing to produce; the test is driving _out by hand
        if self._burst_secs > 0:
            now = time.monotonic()
            if self._next_refill is None or now < self._next_refill:
                return b""
            # A real board keeps sampling while the host process is descheduled and
            # its USB buffer delivers those accumulated packets on the next read.
            # Advancing from ``now`` silently discarded every missed fake interval,
            # which made rate tests fail only on busy CI runners.
            bursts = 1 + int((now - self._next_refill) / self._burst_secs)
            self._next_refill += bursts * self._burst_secs
        else:
            bursts = 1
        n = self._burst_tactile * bursts
        out = tagged_burst(n, start_seq=self._next_tactile_seq)
        self._next_tactile_seq = (self._next_tactile_seq + n) & 0xFFFFFFFF
        return out

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.closed = True

    # -- board behaviour --------------------------------------------------------

    def _handle(self, cmd: str) -> None:
        up = cmd.upper()
        if up == "GET CONFIG":
            self._config_asks += 1
            if self.config is not None and self._config_asks >= self._config_after:
                self._out += b"#CONFIG " + json.dumps(self.config).encode() + b"\r\n"
            return
        if up == "GET STATUS":
            self._out += b"#STATUS " + json.dumps(self.status).encode() + b"\r\n"
            return
        if up == "STREAM TAG ON":
            self._streaming = True
            self._out += self._stream
            if self._burst_secs > 0:
                # ``self._stream`` is the first produced burst; the next one is due
                # one burst period later. Reset this on every new stream session.
                self._next_refill = time.monotonic() + self._burst_secs
            return
        if up == "STREAM TAG OFF":
            self._streaming = False
            return
        # Reply with the firmware's ACTUAL strings, not a generic #OK. A fake that
        # answers differently from the board tests the fake.
        if up.startswith("SET THR "):
            threshold = min(4095, max(0, int(up.split()[-1])))
            self.config = dict(self.config or {}, stream_thr=threshold)
            self.zero_recipe["thr"] = threshold
            self._out += b"#THR " + up.split()[-1].encode() + b"\r\n"
            return
        if up == "SET STREAM CLEAN":
            self.config = dict(self.config or {}, stream_clean=True)
            self.zero_recipe["clean"] = True
            self._out += b"#STREAM clean (per-taxel baseline + thr applied on wire)\r\n"
            return
        if up == "SET STREAM RAW":
            self.config = dict(self.config or {}, stream_clean=False)
            self.zero_recipe["clean"] = False
            self._out += b"#STREAM raw (unprocessed counts on wire)\r\n"
            return
        if up.startswith("SET RATE "):
            hz = up.split()[-1]
            self.config = dict(self.config or {}, rate_hz=int(hz))
            self._out += (
                "#SCAN settle_us=5 avg=1 gap_us=20 discharge_us=10 adischarge=off "
                f"throwaway=0 rate_hz={hz} ble_batch=3 synth_hz=0\r\n"
            ).encode()
            return
        if up.startswith("SET IMURATE "):
            period = min(100, max(1, int(up.split()[-1])))
            self._out += f"#IMURATE period_ms={period}\r\n".encode()
            return
        if up == "SWEEP" or up == "DIAG SWEEP":
            # The trap: bare SWEEP is the settle diagnostic, NOT the calibration.
            self._out += b"#SWEEP settle_us,ghost_max,std_max,mean_all,scan_us\r\n#SWEEP done\r\n"
            return
        if up.startswith("SWEEP "):
            if self.locked:
                self._out += b"#ERR locked (FACTORY UNLOCK first)\r\n"
                return
            self._out += b"#SWEEP started (open/close repeatedly; touch nothing)\r\n"
            if self.sweep_completes:
                self.config = dict(
                    self.config or {},
                    zero_valid=True,
                    stream_clean=bool(self.zero_recipe.get("clean", False)),
                )
                self._out += b"#TZERO " + json.dumps(self.zero_recipe).encode() + b"\r\n"
            return
        if up == "GET ZERO":
            self._out += b"#TZERO " + json.dumps(self.zero_recipe).encode() + b"\r\n"
            return
        if self._echo_unknown and up.startswith(("SET ", "ZERO", "DIAG")):
            self._out += b"#OK\r\n"
            return
        if self._echo_unknown:
            self._out += b"#ERR unknown command\r\n"
