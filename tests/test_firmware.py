"""Firmware update failure boundaries. No real USB or firmware writes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_serial import CFG_V6, FakeSerial
from oglo import _firmware_package as pkg, _firmware_protocol as protocol, _firmware_worker as worker
from oglo._firmware_journal import atomic_json, journal_path, read_journal, require_capture_ready
from oglo._ownership import DeviceLease
from oglo._usb import PortCandidate, PortBusyError
from oglo.firmware import PHASE_LIMITS, inventory, select_devices, supervise

DEVICE = {'serial': 'OGLO-L-00067', 'usb_serial': '7CE8B1A0D948', 'side': 'left'}
RIGHT = {'serial': 'OGLO-R-00065', 'usb_serial': '68EE8F507F38', 'side': 'right'}


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('OGLO_FIRMWARE_POLICY', raising=False)


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / 'policy.json'
    atomic_json(path, {'schema': 1, 'id': 'lab-0917', 'enabled': True, 'bundle': 'bundle', 'devices': [DEVICE, RIGHT]})
    return pkg.FirmwarePolicy.load(path)


def make_snapshot(device=DEVICE, *, target=False):
    cfg = {**CFG_V6, 'serial': device['serial'], 'side': device['side'], 'pair_id': 'pair-01',
           'batch': 'batch-01', 'fw_rev': pkg.VERSION if target else '0.9.16'}
    fw = {'fw_rev': cfg['fw_rev'], 'hw_rev': pkg.HARDWARE, 'schema_ver': 6, 'update_protocol': 1,
          'running_image_sha256': pkg.RUNNING_SHA if target else pkg.FROM_SHA, 'max_chunk': 1024,
          'signing_key_id': pkg.KEY_ID, 'rollback_supported': True}
    return {'config': cfg, 'fwinfo': fw, 'zero': FakeSerial(cfg).zero_recipe}


class UpdatePort:
    """Binary receive swallows all bytes, just like the reviewed firmware loop."""
    def __init__(self, *, failure=None, image=None):
        self.failure = failure
        self.image = image or b'x' * 2050
        self.out = b''
        self.writes = []
        self.receiving = False
        self.data = b''
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        if data.startswith(b'FW BEGIN'):
            self.receiving = True
            if self.failure != 'ready':
                self.out += b'#FW READY session=123456ab next=0 max_chunk=1024\r\n'
        elif self.receiving:
            assert data[:4] == b'OGFW', 'text was injected into binary receive'
            body, crc = data[4:-4], data[-4:]
            assert zlib.crc32(body) == struct.unpack('<I', crc)[0]
            version, session, offset, length = struct.unpack('<BIIH', body[:11])
            assert version == 1 and session == 0x123456ab and offset == len(self.data)
            assert length == len(body[11:])
            self.data += body[11:]
            end = len(self.data)
            final = end == len(self.image)
            if self.failure == 'nack':
                self.out += f'#FW NACK session=123456ab next={offset} reason=crc\n'.encode()
            elif self.failure != 'ack' and not (final and self.failure in ('last_ack', 'last_all')):
                self.out += f'#FW ACK session=123456ab next={end}\n'.encode()
            if final:
                self.receiving = False
                if self.failure != 'last_all':
                    self.out += f'#FW RECEIVED session=123456ab bytes={end}\n'.encode()
        elif data.startswith(b'FW COMMIT'):
            if self.failure != 'commit':
                self.out += f'#FW COMMIT OK session=123456ab sha256={pkg.FILE_SHA}\n'.encode()
        else:
            raise AssertionError(f'unexpected command: {data!r}')
        return len(data)

    def read(self, n):
        # Ragged reads exercise line fragmentation and ACK/RECEIVED coalescing.
        out, self.out = self.out[:37], self.out[37:]
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def quick_wait(monkeypatch):
    original = protocol.LineChannel.wait
    monkeypatch.setattr(protocol.LineChannel, 'wait', lambda self, match, seconds=5: original(self, match, .02))


@pytest.mark.parametrize('failure', [None, 'last_ack', 'commit'])
def test_transfer_accepts_only_proven_completion(failure, quick_wait):
    port = UpdatePort(failure=failure)
    bundle = pkg.Bundle(port.image, b'', b'x' * 70)
    progress = []
    assert protocol.transfer(port, bundle, lambda **e: progress.append(e)) is (failure != 'commit')
    assert port.data == port.image
    assert sum(d.startswith(b'FW COMMIT') for d in port.writes) == 1
    assert [e['acknowledged_bytes'] for e in progress] == [1024, 2048, 2050]


@pytest.mark.parametrize('failure', ['ready', 'ack', 'last_all', 'nack'])
def test_uncertain_binary_transfer_sends_no_abort_stop_or_retry(failure, quick_wait):
    port = UpdatePort(failure=failure)
    with pytest.raises((TimeoutError, pkg.FirmwareError)):
        protocol.transfer(port, pkg.Bundle(port.image, b'', b'x' * 70))
    assert not any(d.startswith((b'FW ABORT', b'FW COMMIT', b'STREAM', b'LINK')) for d in port.writes)
    frames = [d for d in port.writes if d.startswith(b'OGFW')]
    assert len(frames) == len(set(frames))


def test_short_write_never_follows_with_another_command():
    class Short(UpdatePort):
        def write(self, data):
            super().write(data)
            return len(data) - 1
    port = Short()
    with pytest.raises(pkg.FirmwareError, match='short'):
        protocol.transfer(port, pkg.Bundle(port.image, b'', b'x' * 70))
    assert len(port.writes) == 1


@pytest.mark.parametrize('change', ['duplicate_serial', 'duplicate_usb', 'wrong_side', 'disabled', 'unknown_key'])
def test_policy_rejects_ambiguous_or_implicit_approval(policy, change):
    data = json.loads(policy.path.read_text())
    if change == 'duplicate_serial':
        data['devices'].append(DEVICE)
    elif change == 'duplicate_usb':
        data['devices'][1]['usb_serial'] = DEVICE['usb_serial']
    elif change == 'wrong_side':
        data['devices'][0]['side'] = 'right'
    elif change == 'disabled':
        data['enabled'] = False
    else:
        data['latest'] = True
    atomic_json(policy.path, data)
    with pytest.raises(pkg.FirmwareError):
        pkg.FirmwarePolicy.load(policy.path)


def test_policy_is_disabled_by_default_and_change_is_detected(policy, monkeypatch):
    assert pkg.resolve_policy() is None
    monkeypatch.setenv('OGLO_FIRMWARE_POLICY', str(policy.path))
    assert pkg.resolve_policy().sha256 == policy.sha256
    data = json.loads(policy.path.read_text()); data['id'] = 'different'
    atomic_json(policy.path, data)
    with pytest.raises(pkg.FirmwareError, match='changed'):
        pkg.resolve_policy(policy)


def test_selection_only_approved_full_usb_identities(policy, monkeypatch):
    import oglo.firmware as fw
    candidates = [PortCandidate('/dev/p' + str(i), d['usb_serial'], 0x2886, 0x0056, '', '') for i, d in enumerate((DEVICE, RIGHT))]
    candidates.append(PortCandidate('/dev/unselected', 'AAAAAAAAAAAA', 0x2886, 0x0056, '', ''))
    monkeypatch.setattr(fw, 'list_candidates', lambda: candidates)
    monkeypatch.setattr(worker, 'list_candidates', lambda: candidates)
    assert select_devices(policy, count=2) == [DEVICE, RIGHT]
    assert select_devices(policy, [RIGHT['serial']], count=1) == [RIGHT]
    with pytest.raises(pkg.FirmwareError):
        select_devices(policy, ['00067'], count=1)
    candidates.append(candidates[0])
    with pytest.raises(pkg.FirmwareError, match='expected one'):
        select_devices(policy, [DEVICE['serial']], count=1)


@pytest.mark.parametrize('field', list(protocol.PRESERVED_CONFIG) + ['zero'])
def test_preservation_detects_each_contract_field(field):
    a = make_snapshot(); b = copy.deepcopy(a)
    if field == 'zero':
        b['zero']['baseline'][0] += 1
    else:
        b['config'][field] = 'changed'
    with pytest.raises(pkg.FirmwareError, match='changed'):
        protocol.compare_preserved(a, b)


def pending_entry(policy, device=DEVICE):
    return {'schema': 1, 'identity': worker.identity(device), 'state': 'pending',
            'before': make_snapshot(device), 'target_sha256': pkg.RUNNING_SHA,
            'policy_sha256': policy.sha256}


def test_pending_and_corrupt_history_fail_closed(policy):
    key = worker.identity(DEVICE)
    entry = pending_entry(policy)
    atomic_json(journal_path(key), entry)
    with pytest.raises(pkg.FirmwareError, match='unfinished'):
        require_capture_ready(key)
    journal_path(key).write_text('{broken')
    with pytest.raises(pkg.FirmwareError, match='cannot read'):
        require_capture_ready(key)


def test_inventory_does_not_call_one_verified_device_a_fleet(policy):
    key = worker.identity(DEVICE)
    entry = {**pending_entry(policy), 'state': 'verified', 'after': make_snapshot(target=True)}
    atomic_json(journal_path(key), entry)
    result = inventory(policy)
    assert result['devices'][0]['state'] == 'verified'
    assert result['devices'][1]['state'] == 'not_verified_for_policy'
    assert result['all_verified_in_saved_history'] is False


@pytest.mark.skipif(os.name != 'posix', reason='isolated POSIX updater')
@pytest.mark.parametrize('phase', ['open', 'write', 'close'])
def test_watchdog_kills_blocked_io_and_releases_lease(tmp_path, monkeypatch, phase):
    monkeypatch.setitem(PHASE_LIMITS, phase, .15)
    lease = DeviceLease('hang-' + phase).acquire()
    directory = tmp_path / 'attempt'; directory.mkdir()
    code = 'import json,time; print(json.dumps({"phase": ' + repr(phase) + '}), flush=True); time.sleep(30)'
    start = time.monotonic()
    try:
        with pytest.raises(pkg.FirmwareError, match='deadline'):
            supervise([sys.executable, '-c', code], lease_fds=[lease.fd], directory=directory, timeout=2)
        assert time.monotonic() - start < 3
        with pytest.raises(PortBusyError):
            DeviceLease('hang-' + phase).acquire()
    finally:
        lease.close()
    with DeviceLease('hang-' + phase):
        pass


def setup_worker(monkeypatch, *, initial_target=False):
    trace = []
    snapshots = {d['serial']: make_snapshot(d, target=initial_target) for d in (DEVICE, RIGHT)}
    monkeypatch.setattr(worker, 'load_bundle', lambda _: object())
    monkeypatch.setattr(worker, 'emit', lambda **e: trace.append(('event', e)))
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: trace.append(('sleep', seconds)))
    monkeypatch.setattr(worker, 'find_device', lambda d: d['serial'])
    def open_port(path):
        trace.append(('open', path))
        return SimpleNamespace(serial=path, close=lambda: trace.append(('close', path)))
    monkeypatch.setattr(worker, '_open_serial_locked', open_port)
    monkeypatch.setattr(worker, 'snapshot', lambda p, d: copy.deepcopy(snapshots[p.serial]))
    monkeypatch.setattr(worker, 'basic_health', lambda p: {'qualification': 'fake'})
    def transfer(p, bundle, progress):
        assert read_journal(worker.identity(next(d for d in (DEVICE, RIGHT) if d['serial'] == p.serial)))['state'] == 'pending'
        trace.append(('write', p.serial))
        snapshots[p.serial]['config']['fw_rev'] = pkg.VERSION
        snapshots[p.serial]['fwinfo']['fw_rev'] = pkg.VERSION
        snapshots[p.serial]['fwinfo']['running_image_sha256'] = pkg.RUNNING_SHA
        return True
    monkeypatch.setattr(worker, 'transfer', transfer)
    return trace, snapshots


def test_both_preflight_before_first_write_and_repeat_skips(policy, monkeypatch):
    trace, snapshots = setup_worker(monkeypatch)
    result = worker.run(policy, [DEVICE, RIGHT], 'first')
    first_write = next(i for i, t in enumerate(trace) if t[0] == 'write')
    assert ('close', RIGHT['serial']) in trace[:first_write]
    assert all(r['state'] == 'verified' and r['wrote_image'] for r in result)
    trace.clear()
    # Legitimate settings changed after a completed preparation are a new baseline.
    snapshots[DEVICE['serial']]['config']['batch'] = 'authorized-new-batch'
    result = worker.run(policy, [DEVICE, RIGHT], 'second')
    assert not any(t[0] == 'write' for t in trace)
    assert all(not r['wrote_image'] for r in result)


def test_recovery_waits_with_no_open_and_keeps_original_backup(policy, monkeypatch):
    trace, snapshots = setup_worker(monkeypatch, initial_target=True)
    entry = pending_entry(policy)
    atomic_json(journal_path(entry['identity']), entry)
    result = worker.run(policy, [DEVICE], 'recovery')
    assert trace[1] == ('sleep', 65)
    assert all(t[0] != 'open' for t in trace[:2])
    assert not any(t[0] == 'write' for t in trace)
    assert result[0]['before'] == entry['before']


def test_recovery_mismatch_blocks_all_writes_and_persists_attention(policy, monkeypatch):
    trace, snapshots = setup_worker(monkeypatch, initial_target=True)
    entry = pending_entry(policy)
    atomic_json(journal_path(entry['identity']), entry)
    snapshots[DEVICE['serial']]['zero']['baseline'][0] += 1
    with pytest.raises(pkg.FirmwareError, match='changed'):
        worker.run(policy, [DEVICE, RIGHT], 'bad')
    assert not any(t[0] == 'write' for t in trace)
    saved = read_journal(entry['identity'])
    assert saved['state'] == 'needs_attention' and saved['before'] == entry['before']
    with pytest.raises(pkg.FirmwareError, match='investigation'):
        worker.run(policy, [DEVICE], 'retry')


def test_first_hand_success_second_failure_is_not_pair_success(policy, monkeypatch):
    trace, _ = setup_worker(monkeypatch)
    original = worker.transfer
    def fail_right(port, bundle, progress):
        if port.serial == RIGHT['serial']:
            raise TimeoutError('lost READY')
        return original(port, bundle, progress)
    monkeypatch.setattr(worker, 'transfer', fail_right)
    with pytest.raises(TimeoutError):
        worker.run(policy, [DEVICE, RIGHT], 'partial')
    assert read_journal(worker.identity(DEVICE))['state'] == 'verified'
    assert read_journal(worker.identity(RIGHT))['state'] == 'pending'


@pytest.fixture
def signed_bundle(tmp_path, monkeypatch):
    # Local fixture key is injected only into this test; SDK trusts the production
    # key constant. The real signed 0.9.17 image is independently checked offline.
    ec = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ec')
    from cryptography.hazmat.primitives import hashes, serialization
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    image = b'fixture-application' * 10
    image += hashlib.sha256(image).digest()
    sha = hashlib.sha256(image).hexdigest()
    monkeypatch.setattr(pkg, 'PUBLIC_KEY', public)
    monkeypatch.setattr(pkg, 'FILE_SHA', sha)
    monkeypatch.setattr(pkg, 'RUNNING_SHA', image[-32:].hex())
    manifest = (f'OGLO-FW-MANIFEST-V1\npart={pkg.PART}\nhw_rev={pkg.HARDWARE}\nversion={pkg.VERSION}\n'
                f'size={len(image)}\nsha256={sha}\nkey_id={pkg.KEY_ID}\n').encode()
    folder = tmp_path / 'signed'; folder.mkdir()
    (folder / 'application.bin').write_bytes(image)
    (folder / 'manifest.txt').write_bytes(manifest)
    (folder / 'signature.der').write_bytes(key.sign(manifest, ec.ECDSA(hashes.SHA256())))
    return folder


def test_signed_bundle_verifies(signed_bundle):
    assert pkg.load_bundle(signed_bundle).image == (signed_bundle / 'application.bin').read_bytes()


@pytest.mark.parametrize('name', ['application.bin', 'manifest.txt', 'signature.der'])
def test_modified_bundle_is_rejected_before_usb(signed_bundle, name):
    path = signed_bundle / name
    data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
    with pytest.raises(pkg.FirmwareError, match='invalid signed'):
        pkg.load_bundle(signed_bundle)


def test_wrong_signing_key_is_rejected(signed_bundle, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    other = ec.generate_private_key(ec.SECP256R1()).public_key()
    monkeypatch.setattr(pkg, 'PUBLIC_KEY', other.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(pkg.FirmwareError, match='InvalidSignature'):
        pkg.load_bundle(signed_bundle)


class SnapshotPort(FakeSerial):
    def __init__(self, state):
        super().__init__(state['config'])
        self.state = state
        self.zero_recipe = state['zero']

    def _handle(self, cmd):
        if cmd == 'GET FWINFO':
            self._out.extend(('#FWINFO ' + json.dumps(self.state['fwinfo']) + '\n').encode())
        else:
            super()._handle(cmd)


@pytest.mark.parametrize('target', [False, True])
def test_snapshot_reads_exact_supported_images(monkeypatch, target):
    state = make_snapshot(target=target)
    monkeypatch.setattr(protocol.time, 'sleep', lambda _: None)
    assert protocol.snapshot(SnapshotPort(state), DEVICE) == state


@pytest.mark.parametrize('bad', ['hash', 'version', 'identity', 'rollback', 'calibration', 'clean_contract'])
def test_snapshot_fails_closed_on_unknown_or_inconsistent_device(monkeypatch, bad):
    state = make_snapshot()
    if bad == 'hash': state['fwinfo']['running_image_sha256'] = 'a' * 64
    elif bad == 'version': state['fwinfo']['fw_rev'] = '0.9.18'
    elif bad == 'identity': state['config']['serial'] = 'OGLO-L-OTHER'
    elif bad == 'rollback': state['fwinfo']['rollback_supported'] = False
    elif bad == 'calibration': state['zero']['baseline'].pop()
    else: state['zero']['clean'] = False
    monkeypatch.setattr(protocol.time, 'sleep', lambda _: None)
    with pytest.raises(pkg.FirmwareError):
        protocol.snapshot(SnapshotPort(state), DEVICE)


def test_missing_one_pair_member_prevents_preparation(policy, monkeypatch):
    import oglo
    import oglo.firmware as fw
    candidate = PortCandidate('/dev/left', DEVICE['usb_serial'], 0x2886, 0x0056, '', '')
    monkeypatch.setattr(fw, 'list_candidates', lambda: [candidate])
    monkeypatch.setattr(fw, '_prepare_locked', lambda *a, **k: pytest.fail('must select both hands before any preparation'))
    with pytest.raises(pkg.FirmwareError, match='expected 2'):
        oglo.connect_pair(firmware_policy=policy)


def test_prepared_connection_handoff_keeps_identity_and_policy(policy, monkeypatch):
    import oglo
    import oglo.firmware as fw
    from oglo._config import parse_config
    from dataclasses import replace
    after = make_snapshot(target=True)
    info, _ = parse_config(after['config'])
    glove = SimpleNamespace(_t=SimpleNamespace(_s=None), _info=info, info=info, close=lambda: None)
    result = {'serial': DEVICE['serial'], 'after': after, 'updated_at': '2026-09-23T00:00:00+00:00', 'attempt': 'test'}
    lease = DeviceLease(worker.identity(DEVICE)).acquire()
    monkeypatch.setattr(fw, 'select_devices', lambda *a, **kw: [DEVICE])
    monkeypatch.setattr(fw, '_prepare_locked', lambda *a, **kw: ([result], {DEVICE['serial']: lease}))
    monkeypatch.setattr(fw, 'find_device', lambda _: '/dev/rebound')
    def connect(path, *, timeout, _lease):
        assert path == '/dev/rebound' and _lease is lease
        return glove
    monkeypatch.setattr(oglo, '_connect_usb_port', connect)
    monkeypatch.setattr(protocol, 'snapshot', lambda *a: after)
    try:
        assert oglo.connect(DEVICE['serial'], firmware_policy=policy) is glove
        assert glove._info.firmware_verification['running_image_sha256'] == pkg.RUNNING_SHA
        assert glove._info.firmware_verification['policy_sha256'] == policy.sha256
    finally:
        lease.close()


def test_record_replay_preserves_verified_runtime_metadata(tmp_path):
    from dataclasses import replace
    from oglo import record, replay
    from test_record_replay import glove
    g = glove(n=20, hz=250)
    metadata = {'running_image_sha256': pkg.RUNNING_SHA, 'usb_serial': DEVICE['usb_serial'],
                'policy_id': 'lab', 'policy_sha256': 'b' * 64, 'sdk_version': 'test',
                'verified_at': '2026-09-23T00:00:00+00:00', 'attempt': 'unit-test'}
    g._info = replace(g.info, firmware_verification=metadata)
    try:
        g._refresh_info(strict=True)
        assert g.info.firmware_verification == metadata
        episode = record(tmp_path, seconds=.15, glove=g)
        assert replay(episode).info.firmware_verification == metadata
        meta_path = episode / 'meta.json'
        data = json.loads(meta_path.read_text()); data['firmware_verification']['running_image_sha256'] = 'bad'
        atomic_json(meta_path, data)
        with pytest.raises(Exception, match='verification hash'):
            replay(episode)
    finally:
        g.close()


def test_health_covers_all_modalities_with_measured_window():
    from fake_serial import tagged_burst
    state = make_snapshot(target=True)
    port = FakeSerial(state['config'], hz=250, stream=tagged_burst(4))
    try:
        result = protocol.basic_health(port, seconds=.2)
        assert set(result['streams']) == {'tactile', 'imu', 'mag'}
        assert all(v['count'] > 0 for v in result['streams'].values())
    finally:
        port.close()


def test_partial_commit_waits_before_reinspection(policy, monkeypatch):
    trace, snapshots = setup_worker(monkeypatch)
    original = worker.transfer
    def ambiguous(*args):
        original(*args)
        return False
    monkeypatch.setattr(worker, 'transfer', ambiguous)
    worker.run(policy, [DEVICE], 'ambiguous')
    write_index = trace.index(('write', DEVICE['serial']))
    quiet_index = trace.index(('sleep', 65))
    assert quiet_index > write_index
    assert not any(t[0] == 'open' for t in trace[write_index + 1:quiet_index])
