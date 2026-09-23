"""Application OTA v1. Called only inside the supervised, exclusively owned worker."""
from __future__ import annotations

import json
import re
import struct
import time
import zlib
from dataclasses import asdict

from . import _wire
from ._config import parse_config
from ._firmware_package import (FILE_SHA, FROM_SHA, HARDWARE, KEY_ID, RUNNING_SHA,
                                VERSION, FirmwareError)
from ._usb import UsbTransport

PRESERVED_CONFIG = ('serial', 'side', 'device_id', 'pair_id', 'batch', 'hw_rev',
                    'rate_hz', 'samples_per_packet', 'imu_len', 'has_mag',
                    'values_per_sample', 'sample_shape', 'channels', 'factory_passed',
                    'stream_clean', 'stream_thr', 'zero_valid', 'cal_lock')
ZERO_FIELDS = {'valid', 'count', 'frames', 'thr', 'clean', 'locked', 'baseline', 'noise'}


class LineChannel:
    def __init__(self, port):
        self.port = port
        self.buffer = b''

    def write(self, data):
        if self.port.write(data) != len(data):
            raise FirmwareError('short USB write; no further OUT commands are safe')

    def wait(self, match, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                line = line.rstrip(b'\r').decode('ascii', 'replace')
                result = match(line)
                if result is not None:
                    return result
                if line.startswith('#FW ERR') or line.startswith('#ERR'):
                    raise FirmwareError(line)
            self.buffer += self.port.read(4096)
            if len(self.buffer) > 65536:
                raise FirmwareError('oversized or unterminated firmware response')
        raise TimeoutError('firmware response deadline exceeded')

    def query(self, command, prefix):
        self.write((command + '\n').encode('ascii'))
        value = self.wait(lambda line: json.loads(line[len(prefix):]) if line.startswith(prefix) else None)
        if not isinstance(value, dict):
            raise FirmwareError(f'invalid {command} reply')
        return value


def snapshot(port, expected):
    io = LineChannel(port)
    # Leading newline clears an interrupted ASCII command, only after any required
    # no-OUT recovery period has completed. Never call this during binary receive.
    io.write(b'\nSTREAM BIN OFF\nSTREAM TAXEL OFF\nSTREAM TAG OFF\n')
    time.sleep(0.4)
    port.reset_input_buffer()
    cfg = io.query('GET CONFIG', '#CONFIG ')
    info, _ = parse_config(cfg)
    if info.serial != expected['serial'] or info.side != expected['side']:
        raise FirmwareError(f"USB {expected['usb_serial']} reports {info.serial}/{info.side}; expected {expected['serial']}/{expected['side']}")
    if any(k not in cfg for k in PRESERVED_CONFIG):
        raise FirmwareError('CONFIG lacks identity or preservation fields')
    fw = io.query('GET FWINFO', '#FWINFO ')
    if (fw.get('hw_rev') != HARDWARE or info.hw_rev != HARDWARE or
            fw.get('fw_rev') != info.fw_rev or fw.get('schema_ver') != 6 or
            fw.get('update_protocol') != 1 or fw.get('signing_key_id') != KEY_ID or
            fw.get('rollback_supported') is not True or fw.get('max_chunk') != 1024):
        raise FirmwareError('unsupported application update contract')
    running = fw.get('running_image_sha256')
    if (info.fw_rev, running) not in {('0.9.16', FROM_SHA), (VERSION, RUNNING_SHA)}:
        raise FirmwareError(f'firmware {info.fw_rev}/{running} is outside the approved migration; no reinstall or downgrade')
    zero = io.query('GET ZERO', '#TZERO ')
    if set(zero) != ZERO_FIELDS or zero.get('count') != 80:
        raise FirmwareError('invalid calibration snapshot')
    for key in ('valid', 'clean', 'locked'):
        if type(zero[key]) is not bool:
            raise FirmwareError('invalid calibration flags')
    for key in ('baseline', 'noise'):
        if (not isinstance(zero[key], list) or len(zero[key]) != 80 or
                any(type(v) is not int or not 0 <= v <= 65535 for v in zero[key])):
            raise FirmwareError('invalid calibration arrays')
    for key in ('frames', 'thr'):
        if type(zero[key]) is not int or not 0 <= zero[key] <= 65535:
            raise FirmwareError('invalid calibration metadata')
    if (cfg['zero_valid'] != zero['valid'] or cfg['cal_lock'] != zero['locked'] or
            cfg['stream_thr'] != zero['thr'] or cfg['stream_clean'] != (zero['valid'] and zero['clean'])):
        raise FirmwareError('CONFIG and ZERO disagree')
    return {'config': cfg, 'fwinfo': fw, 'zero': zero}


def compare_preserved(before, after):
    if not isinstance(before, dict) or set(before) != {'config', 'fwinfo', 'zero'}:
        raise FirmwareError('original preservation snapshot is invalid')
    different = [k for k in PRESERVED_CONFIG if k not in before['config'] or before['config'][k] != after['config'].get(k)]
    if before['zero'] != after['zero']:
        different.append('GET ZERO')
    if different:
        raise FirmwareError('settings/calibration changed: ' + ', '.join(different))


def transfer(port, bundle, progress=lambda **event: None):
    """No retries, ABORT, ping or text after ambiguous binary writes.

    A missing ACK fails this attempt. The durable pending journal forces a fresh
    no-OUT period on the next invocation. RECEIVED can prove a lost final ACK.
    """
    io = LineChannel(port)
    io.write(bundle.begin)
    ready = io.wait(lambda line: re.fullmatch(r'#FW READY session=([0-9a-f]{8}) next=0 max_chunk=1024', line), 20)
    session = int(ready[1], 16)
    sid = ready[1]
    for offset in range(0, len(bundle.image), 1024):
        payload = bundle.image[offset:offset + 1024]
        body = struct.pack('<BIIH', 1, session, offset, len(payload)) + payload
        io.write(b'OGFW' + body + struct.pack('<I', zlib.crc32(body) & 0xffffffff))
        next_offset = offset + len(payload)
        def ack(line):
            if line == f'#FW ACK session={sid} next={next_offset}':
                return 'ack'
            if next_offset == len(bundle.image) and line == f'#FW RECEIVED session={sid} bytes={next_offset}':
                return 'received'
            if line.startswith('#FW NACK'):
                raise FirmwareError(line)
            return None
        result = io.wait(ack)
        progress(acknowledged_bytes=next_offset, total_bytes=len(bundle.image))
    if result != 'received':
        io.wait(lambda line: True if line == f'#FW RECEIVED session={sid} bytes={len(bundle.image)}' else None)
    io.write(f'FW COMMIT {sid}\n'.encode('ascii'))
    # A lost COMMIT response is ambiguous, not a reason to resend. Rebind and prove
    # the running image. An explicit error is different and fails immediately.
    try:
        io.wait(lambda line: True if line == f'#FW COMMIT OK session={sid} sha256={FILE_SHA}' else None, 10)
    except (TimeoutError, OSError):
        return False
    return True


def basic_health(port, seconds=3.0):
    """Packet delivery check only, never a sensor-freshness or soak qualification."""
    # This short, isolated probe has no background writer. The worker owns the
    # descriptor and DTR lifecycle; no LINK PING can race later OTA/text I/O.
    transport = UsbTransport(port, owns_port=False)
    info, _ = transport.read_config(timeout=5)
    before = transport.read_status()
    if not before.healthy or not before.mag_ok or not info.has_mag:
        raise FirmwareError('device health/status is not ready')
    start = time.monotonic()
    seen = {k: {'count': 0, 'first_s': None, 'last_s': None, 'max_gap_s': 0.0} for k in ('tactile', 'imu', 'mag')}
    try:
        transport.start()
        while time.monotonic() - start < seconds:
            for p in transport.poll():
                name = ('tactile' if isinstance(p, _wire.TactilePacket) else
                        'imu' if isinstance(p, _wire.ImuPacket) else
                        'mag' if isinstance(p, _wire.MagPacket) else None)
                if name:
                    row = seen[name]; t = time.monotonic() - start
                    row['count'] += 1
                    if row['first_s'] is None:
                        row['first_s'] = t
                    if row['last_s'] is not None:
                        row['max_gap_s'] = max(row['max_gap_s'], t - row['last_s'])
                    row['last_s'] = t
    finally:
        transport.stop()
    transport.drain(settle=.2)
    after = transport.read_status()
    counters = asdict(transport.dropped)
    report = {'window_s': seconds, 'streams': seen, 'host_counters': counters,
              'status_before': before.raw, 'status_after': after.raw,
              'qualification': 'basic USB packet delivery only; sensor fresh-value rates and long-term stability not qualified'}
    def fail(message):
        error = FirmwareError(message)
        error.observation = report
        raise error
    if any(counters.values()) or not after.healthy or not after.mag_ok:
        fail(f'post-update stream loss/status failure: {counters}')
    for name, rate in (('tactile', info.rate_hz), ('imu', 500), ('mag', 125)):
        row = seen[name]
        if (row['count'] < rate * seconds * 0.8 or row['first_s'] is None or row['first_s'] > .5 or
                seconds - row['last_s'] > .5 or row['max_gap_s'] > .5):
            fail(f'post-update {name} packet delivery failed: {row}')
    for key in ('tag_dropped', 'tag_short_writes', 'deadline_misses'):
        a, b = before.raw.get(key), after.raw.get(key)
        # In the pinned 0.9.16/17 TX task, short_writes counts transient retries
        # while retaining the partial frame. Actual rejected frames increment
        # tag_dropped. Match the existing recorder's version-specific semantics.
        if type(a) is not int or type(b) is not int or b < a or (key != 'tag_short_writes' and b != a):
            fail(f'device counter changed during readiness check: {key}: {a} -> {b}')
    if after.uptime_ms < before.uptime_ms:
        fail('device reset during readiness check')
    return report
