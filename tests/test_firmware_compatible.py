"""Compatibility update boundaries without a pre-enrolled device inventory."""
import copy
import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from oglo import _firmware_package as pkg, _firmware_worker as worker, _firmware_protocol as protocol
from oglo._firmware_journal import atomic_json, journal_path, read_journal
from oglo._usb import PortCandidate
from oglo import firmware as fw
from test_firmware import DEVICE, RIGHT, SnapshotPort, make_snapshot, setup_worker, pending_entry


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv('OGLO_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.delenv('OGLO_FIRMWARE_POLICY', raising=False)


def candidate(d):
    return {'serial': 'USB-' + d['usb_serial'], 'usb_serial': d['usb_serial']}


def discovered_worker(monkeypatch, target=False):
    trace, snapshots = setup_worker(monkeypatch, initial_target=target)
    by_chip = {d['usb_serial']: d for d in (DEVICE, RIGHT)}
    monkeypatch.setattr(worker, 'find_device', lambda d: by_chip[d['usb_serial']]['serial'])
    monkeypatch.setattr(worker, 'read_identity', lambda port, chip: dict(by_chip[chip]))
    return trace, snapshots


def test_bundled_real_production_signature_and_image():
    pytest.importorskip('cryptography')
    policy = pkg.bundled_policy()
    assert not policy.devices and policy.path is None
    assert len(pkg.load_bundle(policy.bundle).image) == 1051104


def test_enable_is_environment_scoped_persistent_and_opens_no_usb(monkeypatch):
    pytest.importorskip('cryptography')
    monkeypatch.setattr(fw, 'list_candidates', lambda: pytest.fail('enable opened hardware'))
    monkeypatch.setattr(pkg.sys, 'platform', 'linux')
    assert pkg.resolve_policy() is None
    path = pkg.configure_auto_update(True)
    assert pkg.resolve_policy().compatible
    assert pkg.resolve_policy(False) is None
    assert pkg.resolve_policy(True).compatible
    saved = json.loads(path.read_text())
    assert 'devices' not in saved
    monkeypatch.setattr(pkg.sys, 'prefix', sys.prefix + '-another-venv')
    assert pkg.resolve_policy() is None


def test_disable_and_corrupt_settings(monkeypatch):
    monkeypatch.setattr(pkg, 'load_bundle', lambda _: None)
    monkeypatch.setattr(pkg.sys, 'platform', 'linux')
    pkg.configure_auto_update(True)
    pkg.configure_auto_update(False)
    assert pkg.resolve_policy() is None
    pkg.settings_path().write_text('{broken')
    with pytest.raises(pkg.FirmwareError):
        pkg.resolve_policy()
    assert pkg.resolve_policy(False) is None


def test_legacy_override_is_not_silently_replaced(monkeypatch):
    monkeypatch.setenv('OGLO_FIRMWARE_POLICY', 'old.json')
    with pytest.raises(pkg.FirmwareError, match='unset'):
        pkg.configure_auto_update(True)
    with pytest.raises(pkg.FirmwareError, match='unset'):
        pkg.configure_auto_update(False)


def test_unknown_usb_serials_are_discovered_without_allowlist(monkeypatch):
    devices = [dict(DEVICE, usb_serial='ABCDEF123456'), dict(RIGHT, usb_serial='123456ABCDEF')]
    ports = [PortCandidate('/dev/' + d['serial'], d['usb_serial'], 0x2886, 0x0056, '', '') for d in devices]
    ports.append(PortCandidate('/dev/not-oglo', 'D' * 12, 0x1234, 0x0056, '', ''))
    monkeypatch.setattr(fw, 'list_candidates', lambda: ports)
    monkeypatch.setattr(worker, 'list_candidates', lambda: ports)
    assert fw.select_devices(pkg.bundled_policy(), count=2) == [candidate(d) for d in devices]
    with pytest.raises(pkg.FirmwareError, match='expected 1'):
        fw.select_devices(pkg.bundled_policy(), count=1)
    ports.append(ports[0])
    with pytest.raises(pkg.FirmwareError, match='expected one'):
        fw.select_devices(pkg.bundled_policy())


def test_identity_comes_from_device_and_is_bound_to_usb(monkeypatch):
    monkeypatch.setattr(protocol.time, 'sleep', lambda _: None)
    state = make_snapshot()
    state['config']['device_id'] = 'oglo-' + DEVICE['usb_serial'].lower()
    assert protocol.read_identity(SnapshotPort(state), DEVICE['usb_serial']) == DEVICE
    with pytest.raises(pkg.FirmwareError, match='identity disagree'):
        protocol.read_identity(SnapshotPort(state), 'A' * 12)


def test_generic_pair_preflights_both_then_updates_and_repeat_skips(monkeypatch):
    trace, _ = discovered_worker(monkeypatch)
    policy = pkg.bundled_policy()
    result = worker.run_compatible(policy, [candidate(DEVICE), candidate(RIGHT)], 'generic', count=2)
    first = trace.index(('write', DEVICE['serial']))
    assert ('close', RIGHT['serial']) in trace[:first]
    assert all(r['wrote_image'] for r in result)
    trace.clear()
    result = worker.run_compatible(policy, [candidate(DEVICE), candidate(RIGHT)], 'again', count=2)
    assert not any(t[0] == 'write' for t in trace)
    assert not any(r['wrote_image'] for r in result)
    report = fw.inventory(policy)
    assert report['scope'] == 'observed_devices_only'
    assert len(report['devices']) == 2


def test_selected_serial_does_not_update_other_attached_glove(monkeypatch):
    trace, _ = discovered_worker(monkeypatch)
    result = worker.run_compatible(pkg.bundled_policy(), [candidate(DEVICE), candidate(RIGHT)], 'right-only', serials=[RIGHT['serial']], count=1)
    assert [r['serial'] for r in result] == [RIGHT['serial']]
    assert ('write', DEVICE['serial']) not in trace
    assert read_journal(worker.identity(DEVICE)) is None


@pytest.mark.parametrize('change', ['missing', 'same_side', 'duplicate'])
def test_generic_invalid_pair_never_writes(monkeypatch, change):
    trace, _ = discovered_worker(monkeypatch)
    devices = [candidate(DEVICE), candidate(RIGHT)]
    if change == 'missing':
        devices.pop()
    else:
        def identify(port, chip):
            if chip == DEVICE['usb_serial']:
                return DEVICE
            return dict(RIGHT, serial=DEVICE['serial'] if change == 'duplicate' else 'OGLO-L-OTHER', side='left')
        monkeypatch.setattr(worker, 'read_identity', identify)
    with pytest.raises(pkg.FirmwareError):
        worker.run_compatible(pkg.bundled_policy(), devices, 'bad-pair', count=2)
    assert not any(t[0] == 'write' for t in trace)


def test_generic_pending_waits_before_discovery_once(monkeypatch):
    trace, _ = discovered_worker(monkeypatch, target=True)
    policy = pkg.bundled_policy()
    entry = pending_entry(policy)
    atomic_json(journal_path(entry['identity']), entry)
    result = worker.run_compatible(policy, [candidate(DEVICE)], 'recover', count=1)
    assert trace[1] == ('sleep', 65)
    assert trace.count(('sleep', 65)) == 1
    assert result[0]['before'] == entry['before']
    assert not result[0]['wrote_image']


@pytest.mark.parametrize('field,value', [('hw_rev', 'OTHER'), ('running_image_sha256', '0' * 64), ('update_protocol', 2)])
def test_incompatible_second_hand_blocks_first_hand_transfer(monkeypatch, field, value):
    trace, snapshots = discovered_worker(monkeypatch)
    snapshots[RIGHT['serial']]['fwinfo'][field] = value
    monkeypatch.setattr(protocol.time, 'sleep', lambda _: None)
    def actual_snapshot(port, expected):
        return protocol.snapshot(SnapshotPort(snapshots[port.serial]), expected)
    monkeypatch.setattr(worker, 'snapshot', actual_snapshot)
    with pytest.raises(pkg.FirmwareError):
        worker.run_compatible(pkg.bundled_policy(), [candidate(DEVICE), candidate(RIGHT)], 'incompatible', count=2)
    assert not any(t[0] == 'write' for t in trace)


def test_empty_observation_history_is_not_fleet_success():
    report = fw.inventory()
    assert report['scope'] == 'observed_devices_only'
    assert report['devices'] == [] and not report['all_verified_in_saved_history']


def test_generic_inventory_merge_preserves_latest_failure(monkeypatch):
    discovered_worker(monkeypatch)
    policy = pkg.bundled_policy()
    worker.run_compatible(policy, [candidate(DEVICE)], 'left')
    report = fw.inventory()
    failed = copy.deepcopy(report)
    failed['devices'][0].update(state='pending', observed_at='2099-01-01T00:00:00+00:00', verified_at=None)
    assert not fw.merge_inventory(policy, [report, failed])['all_verified_in_saved_history']
    assert read_journal(worker.identity(DEVICE))['state'] == 'verified'


def test_persistent_setting_drives_existing_connect_without_code_change(monkeypatch):
    import oglo
    monkeypatch.setattr(pkg, 'load_bundle', lambda _: None)
    monkeypatch.setattr(pkg.sys, 'platform', 'linux')
    pkg.configure_auto_update(True)
    called = []
    def connect(policy, **kwargs):
        assert policy.compatible and not policy.devices
        called.append(kwargs)
        return ('left', 'right') if kwargs['count'] == 2 else ('one',)
    monkeypatch.setattr(fw, 'connect_prepared', connect)
    assert oglo.connect() == 'one'
    assert oglo.connect_pair() == ('left', 'right')
    assert len(called) == 2


def test_release_installer_hash_failure_never_installs_or_enables(tmp_path, monkeypatch):
    import importlib.util
    path = Path(__file__).parents[1] / 'tools/install_template.py'
    spec = importlib.util.spec_from_file_location('installer', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.VERSION = '0.1.0test'; module.WHEEL = 'oglo-test.whl'; module.SHA256 = '0' * 64
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'corrupted'))
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('must not install'))
    with pytest.raises(RuntimeError, match='checksum'):
        module.main([])


def test_release_installer_stops_when_pip_fails(monkeypatch):
    import importlib.util, hashlib
    path = Path(__file__).parents[1] / 'tools/install_template.py'
    spec = importlib.util.spec_from_file_location('installer', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.VERSION = '0.1.0test'; module.WHEEL = 'oglo-test.whl'; module.SHA256 = hashlib.sha256(b'wheel').hexdigest()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'wheel'))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        module.main(['--auto-firmware'])
    assert len(calls) == 1 and 'pip' in calls[0]


def test_generic_parent_keeps_physical_lease_through_logical_handoff(monkeypatch):
    from oglo._ownership import DeviceLease
    from oglo._usb import PortBusyError
    monkeypatch.setattr(fw.sys, 'platform', 'linux')
    monkeypatch.setattr(fw, 'load_bundle', lambda _: None)
    def supervise(argv, **kwargs):
        request = json.loads(Path(argv[-1]).read_text())
        assert request['policy'] is True and request['serials'] == [RIGHT['serial']]
        assert len(request['lease_fds']) == 2
        for d in (DEVICE, RIGHT):
            with pytest.raises(PortBusyError):
                DeviceLease(worker.identity(d)).acquire()
        return [{'serial': RIGHT['serial'], 'usb_serial': RIGHT['usb_serial'], 'state': 'verified'}]
    monkeypatch.setattr(fw, 'supervise', supervise)
    results, leases = fw._prepare_locked(pkg.bundled_policy(), [candidate(DEVICE), candidate(RIGHT)], serials=[RIGHT['serial']], count=1)
    try:
        with DeviceLease(worker.identity(DEVICE)):
            pass
        with pytest.raises(PortBusyError):
            DeviceLease(worker.identity(RIGHT)).acquire()
        assert set(leases) == {RIGHT['serial']}
    finally:
        for lease in leases.values(): lease.close()


def test_cli_watch_does_not_retry_failed_glove_when_another_is_attached(monkeypatch):
    from oglo.cli import main
    trace = []
    visible = [[candidate(DEVICE)], [candidate(DEVICE), candidate(RIGHT)]]
    calls = 0
    def select(*a, **kw):
        nonlocal calls
        if calls == len(visible):
            raise KeyboardInterrupt
        result = visible[calls]; calls += 1
        return result
    def prepare(*a, **kw):
        trace.extend(kw['_usb_serials'])
        raise pkg.FirmwareError('simulated failure')
    monkeypatch.setattr(fw, 'select_devices', select)
    monkeypatch.setattr(fw, 'prepare', prepare)
    monkeypatch.setattr(fw, 'inventory', lambda *a: {})
    monkeypatch.setattr(worker.time, 'sleep', lambda _: None)
    assert main(['firmware', 'prepare', '--watch']) == 130
    assert trace == [DEVICE['usb_serial'], RIGHT['usb_serial']]


def test_cli_enable_refuses_unsupported_host_before_touching_usb(monkeypatch):
    from oglo.cli import main
    monkeypatch.setattr(pkg.sys, 'platform', 'win32')
    monkeypatch.setattr(pkg, 'load_bundle', lambda _: pytest.fail('no validation needed on unsupported host'))
    assert main(['firmware', 'enable']) == 1
    assert not pkg.auto_update_enabled()


def test_installer_uses_invoking_python_and_enables_only_after_install(monkeypatch):
    import importlib.util, hashlib
    path = Path(__file__).parents[1] / 'tools/install_template.py'
    spec = importlib.util.spec_from_file_location('installer', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.VERSION = '0.1.0test'; module.WHEEL = 'oglo-test.whl'; module.SHA256 = hashlib.sha256(b'wheel').hexdigest()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'wheel'))
    calls = []
    def run(args, **kwargs):
        assert kwargs['check'] is True and args[0] == sys.executable
        if 'pip' in args:
            wheel = Path(args[-1].removesuffix('[firmware]'))
            assert wheel.read_bytes() == b'wheel'
        calls.append(args)
    monkeypatch.setattr(module.subprocess, 'run', run)
    module.main(['--auto-firmware'])
    assert len(calls) == 4
    assert '--force-reinstall' in calls[1] and '--no-deps' in calls[1]
    assert calls[-1][1:] == ['-m', 'oglo', 'firmware', 'enable']


def test_prepare_all_rejects_partial_worker_success(monkeypatch):
    monkeypatch.setattr(fw.sys, 'platform', 'linux')
    monkeypatch.setattr(fw, 'load_bundle', lambda _: None)
    monkeypatch.setattr(fw, 'supervise', lambda *a, **kw: [dict(DEVICE, state='verified')])
    with pytest.raises(pkg.FirmwareError, match='selected physical'):
        fw._prepare_locked(pkg.bundled_policy(), [candidate(DEVICE), candidate(RIGHT)])
