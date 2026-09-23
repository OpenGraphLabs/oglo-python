"""Offline, compatibility-based firmware preparation for macOS/Linux."""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from ._firmware_journal import atomic_json, read_journal
from ._firmware_package import FirmwareError, FirmwarePolicy, load_bundle, resolve_policy
from ._ownership import DeviceLease, state_directory
from ._firmware_worker import find_device, identity
from ._usb import list_candidates

PHASE_LIMITS = {'quiet': 70, 'inspect': 30, 'open': 30, 'write': 180,
                'close': 5, 'reboot': 60, 'verify': 30, 'verified': 10}


def select_devices(policy, serials=None, *, port=None, count=None):
    candidates = list_candidates()
    if policy.compatible:
        if serials is not None and (not serials or any(not isinstance(s, str) or
                not re.fullmatch(r'OGLO-[LR]-[A-Za-z0-9-]+', s) for s in serials) or
                len({s.casefold() for s in serials}) != len(serials)):
            raise FirmwareError('select unique full logical glove serials')
        selected = []
        for p in candidates:
            if p.vid != 0x2886 or p.pid != 0x0056 or (port and os.path.realpath(p.device) != os.path.realpath(port)):
                continue
            if not p.serial_number or not re.fullmatch(r'[0-9a-fA-F]{12}', p.serial_number):
                raise FirmwareError('connected OGLO lacks a stable USB chip identity')
            chip = p.serial_number.upper()
            selected.append({'serial': 'USB-' + chip, 'usb_serial': chip})
        if not selected or (serials is None and count is not None and len(selected) != count):
            raise FirmwareError(f'expected {count or "at least one"} glove(s); found {len(selected)}; select serials when several are attached')
        for d in selected:
            find_device(d)
        return selected
    visible = [d for d in policy.devices if any(p.vid == 0x2886 and p.pid == 0x0056 and p.serial_number and
               p.serial_number.casefold() == d['usb_serial'].casefold() and
               (port is None or os.path.realpath(p.device) == os.path.realpath(port)) for p in candidates)]
    if serials is not None:
        names = tuple(s.casefold() for s in serials)
        if len(names) != len(set(names)):
            raise FirmwareError('duplicate selected logical serial')
        selected = [d for d in visible if d['serial'].casefold() in names]
        if len(selected) != len(names):
            raise FirmwareError(f'policy-approved requested gloves are not all attached: {serials}')
    else:
        selected = visible
    if not selected or (count is not None and len(selected) != count):
        raise FirmwareError(f'expected {count or "at least one"} selected policy device(s); found {len(selected)}')
    if count == 2 and {d['side'] for d in selected} != {'left', 'right'}:
        raise FirmwareError('selected pair must contain one left and one right hand')
    for d in selected:
        find_device(d)  # reject duplicate physical USB identities before commands
    return selected


def _stop_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                # The inherited flock remains held by the stuck process; releasing
                # our fd does not permit a second SDK to race a live writer.
                raise FirmwareError('USB worker could not terminate; ownership remains held; host intervention required') from exc


def supervise(argv, *, lease_fds, directory, timeout, on_event=None, env=None):
    """Wall deadlines cover blocked OS open/write/read/close, not only replies."""
    events = queue.Queue()
    with (directory / 'worker-stderr.log').open('wb') as err, (directory / 'events.jsonl').open('w') as log:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                   pass_fds=tuple(lease_fds), start_new_session=True, env=env)
        def read_events():
            try:
                for line in process.stdout:
                    # Worker emits bounded records (snapshots are below 100 KiB).
                    if len(line) > 1024 * 1024:
                        events.put({'error': 'oversized worker event'}); return
                    events.put(json.loads(line))
            except Exception as exc:
                events.put({'error': f'worker output error: {exc}'})
            finally:
                events.put(None)
        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        started = phase_start = time.monotonic()
        phase, phase_key, result, eof = 'inspect', None, None, False
        try:
            while True:
                elapsed = time.monotonic()
                if elapsed - started > timeout or elapsed - phase_start > PHASE_LIMITS[phase]:
                    raise FirmwareError(f'firmware {phase} deadline exceeded; preparation did not complete')
                try:
                    event = events.get(timeout=.1)
                except queue.Empty:
                    event = {}
                if event is None:
                    eof = True
                elif event:
                    log.write(json.dumps(event) + '\n'); log.flush()
                    if 'phase' in event:
                        key = event['phase'], event.get('serial')
                        if event['phase'] not in PHASE_LIMITS:
                            raise FirmwareError('unknown worker phase')
                        if key != phase_key:
                            phase_key, phase, phase_start = key, event['phase'], time.monotonic()
                    if 'error' in event:
                        raise FirmwareError(event['error'])
                    if 'result' in event:
                        result = event['result']
                    if on_event:
                        on_event(event)
                if eof and process.poll() is not None:
                    if process.returncode != 0 or result is None:
                        raise FirmwareError('firmware worker exited without verified results')
                    return result
        finally:
            _stop_process(process)
            reader.join(timeout=1)
            process.stdout.close()


def _prepare_locked(policy, devices, *, on_event=None, serials=None, count=None):
    if sys.platform not in ('darwin', 'linux'):
        raise FirmwareError('managed application update is currently supported on macOS/Linux only')
    load_bundle(policy.bundle)  # reject packages before acquiring/opening any device
    leases = {}
    directory = state_directory() / 'attempts' / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    try:
        for device in sorted(devices, key=lambda d: d['usb_serial']):
            leases[device['serial']] = DeviceLease(identity(device)).acquire()
        request = {'policy': True if policy.compatible else str(policy.path), 'policy_sha256': policy.sha256,
                   'serials': serials if policy.compatible else [d['serial'] for d in devices],
                   'devices': devices, 'count': count, 'attempt': directory.name,
                   'lease_fds': [lease.fd for lease in leases.values()]}
        atomic_json(directory / 'request.json', request)
        env = os.environ.copy()
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
        results = supervise([sys.executable, '-m', 'oglo._firmware_worker', str(directory / 'request.json')],
                            lease_fds=request['lease_fds'], directory=directory,
                            timeout=90 + len(devices) * 360, on_event=on_event, env=env)
        if policy.compatible:
            # The worker resolves logical identities; keep the same physical lease
            # continuously through discovery, update and capture handoff.
            chips = {d['usb_serial'] for d in devices}
            if (not results or len({r['serial'] for r in results}) != len(results) or
                    len({r['usb_serial'] for r in results}) != len(results) or
                    any(r['usb_serial'] not in chips or r['state'] != 'verified' for r in results) or
                    (serials is None and {r['usb_serial'] for r in results} != chips) or
                    (count is not None and len(results) != count) or
                    (serials is not None and {r['serial'].casefold() for r in results} != {s.casefold() for s in serials})):
                raise FirmwareError('worker did not verify the selected physical devices')
            selected_leases = {r['serial']: leases['USB-' + r['usb_serial']] for r in results}
            for lease in leases.values():
                if lease not in selected_leases.values():
                    lease.close()
            leases = selected_leases
        elif {r['serial'] for r in results} != {d['serial'] for d in devices}:
            raise FirmwareError('worker did not verify all selected devices')
        return results, leases
    except BaseException as exc:
        for lease in leases.values():
            lease.close()
        if isinstance(exc, FirmwareError):
            raise FirmwareError(f'{exc}; diagnostic files: {directory}') from exc
        raise


def prepare(policy=True, *, serials=None, on_event=None, _usb_serials=None):
    """Prepare attached approved devices sequentially. Never selects unseen units."""
    policy = resolve_policy(policy)
    if policy is None:
        raise FirmwareError('an explicit firmware policy is required')
    devices = select_devices(policy, serials)
    if _usb_serials is not None:
        devices = [d for d in devices if d['usb_serial'] in _usb_serials]
        if {d['usb_serial'] for d in devices} != set(_usb_serials):
            raise FirmwareError('selected USB attachment changed before preparation')
    results, leases = _prepare_locked(policy, devices, on_event=on_event, serials=serials)
    for lease in leases.values():
        lease.close()
    return results


def connect_prepared(policy, *, serials=None, port=None, count=1, timeout=10):
    from . import _connect_usb_port, __version__
    from ._firmware_protocol import compare_preserved, snapshot
    devices = select_devices(policy, serials, port=port, count=count)
    def progress(event):
        if 'phase' in event and event['phase'] in {'quiet', 'inspect', 'write', 'reboot', 'verify', 'verified'}:
            print(f"OGLO: {event.get('serial', '')} — {event['phase']}", file=sys.stderr)
    results, leases = _prepare_locked(policy, devices, on_event=progress, serials=serials, count=count)
    if policy.compatible:
        devices = [{'serial': r['serial'], 'usb_serial': r['usb_serial'],
                    'side': r['after']['config']['side']} for r in results]
    gloves = []
    try:
        for device in devices:
            result = next(r for r in results if r['serial'] == device['serial'])
            glove = _connect_usb_port(find_device(device), timeout=timeout, _lease=leases[device['serial']])
            gloves.append(glove)
            fresh = snapshot(glove._t._s, device)
            compare_preserved(result['after'], fresh)
            if fresh['fwinfo'] != result['after']['fwinfo']:
                raise FirmwareError('running image changed during handoff to capture')
            metadata = {'running_image_sha256': fresh['fwinfo']['running_image_sha256'],
                        'usb_serial': device['usb_serial'], 'policy_id': policy.policy_id,
                        'policy_sha256': policy.sha256, 'sdk_version': __version__,
                        'verified_at': result['updated_at'], 'attempt': result['attempt']}
            glove._info = replace(glove.info, firmware_verification=metadata)
        return tuple(sorted(gloves, key=lambda g: g.info.side))
    except BaseException:
        for glove in gloves:
            glove.close()
        for lease in leases.values():
            lease.close()
        raise


def observed_devices(policy):
    if not policy.compatible:
        return policy.devices
    devices = []
    root = state_directory() / 'firmware'
    from ._firmware_package import read_json
    for path in sorted(root.glob('*.json')):
        entry = read_json(path)
        # Never silently ignore a corrupt journal that might represent a failure.
        chip = entry.get('usb_serial', '')
        name = entry.get('serial', '')
        if not re.fullmatch(r'[0-9a-fA-F]{12}', chip) or not re.fullmatch(r'OGLO-[LR]-[A-Za-z0-9-]+', name):
            raise FirmwareError(f'invalid saved device identity: {path}')
        device = {'serial': name, 'usb_serial': chip.upper(),
                  'side': 'left' if name.startswith('OGLO-L-') else 'right'}
        verified = read_journal(identity(device))
        if verified != entry:
            raise FirmwareError('saved identity does not match journal path')
        if entry.get('policy_sha256') == policy.sha256:
            devices.append(device)
    if len({d['serial'] for d in devices}) != len(devices):
        raise FirmwareError('saved logical identity belongs to multiple USB devices')
    return devices


def inventory(policy=True):
    """Saved observations, not a claim that unplugged devices are currently healthy."""
    policy = resolve_policy(policy)
    if policy is None:
        raise FirmwareError('an explicit firmware policy is required')
    rows = []
    for device in observed_devices(policy):
        entry = read_journal(identity(device))
        same_policy = entry and entry.get('policy_sha256') == policy.sha256
        rows.append({**device, 'state': entry['state'] if same_policy else 'not_verified_for_policy',
                     'observed_at': entry.get('updated_at') if same_policy else None,
                     'verified_at': entry.get('updated_at') if same_policy and entry['state'] == 'verified' else None,
                     'running_image_sha256': entry.get('after', {}).get('fwinfo', {}).get('running_image_sha256') if same_policy else None})
    return {'schema': 1, 'policy_id': policy.policy_id, 'policy_sha256': policy.sha256, 'devices': rows,
            'scope': 'observed_devices_only' if policy.compatible else 'explicit_inventory',
            'all_verified_in_saved_history': bool(rows) and all(r['state'] == 'verified' for r in rows)}


def merge_inventory(policy, reports):
    """Reconcile exported history without importing it into trusted recovery state."""
    from datetime import datetime
    from ._firmware_package import RUNNING_SHA
    policy = resolve_policy(policy)
    if policy is None:
        raise FirmwareError('an explicit firmware policy is required')
    merged = {d['serial']: {**d, 'state': 'not_verified_for_policy', 'observed_at': None,
                          'verified_at': None, 'running_image_sha256': None} for d in observed_devices(policy)}
    times = {}
    for report in reports:
        if (not isinstance(report, dict) or report.get('schema') != 1 or
                report.get('policy_sha256') != policy.sha256 or report.get('policy_id') != policy.policy_id or
                not isinstance(report.get('devices'), list)):
            raise FirmwareError('inventory export belongs to a different or invalid policy')
        seen = set()
        for row in report['devices']:
            if not isinstance(row, dict):
                raise FirmwareError('invalid inventory row')
            name = row.get('serial')
            if policy.compatible:
                if (not isinstance(name, str) or not re.fullmatch(r'OGLO-[LR]-[A-Za-z0-9-]+', name) or
                        not isinstance(row.get('usb_serial'), str) or not re.fullmatch(r'[0-9A-F]{12}', row['usb_serial']) or
                        row.get('side') != ('left' if name.startswith('OGLO-L-') else 'right') or
                        any(v['usb_serial'] == row['usb_serial'] and k != name for k, v in merged.items())):
                    raise FirmwareError('invalid or conflicting inventory identity')
                if name not in merged:
                    merged[name] = {**row, 'state': 'not_verified_for_policy', 'observed_at': None,
                                    'verified_at': None, 'running_image_sha256': None}
            if name not in merged or name in seen:
                raise FirmwareError('unknown or duplicate inventory identity')
            seen.add(name)
            if any(row.get(k) != merged[name][k] for k in ('usb_serial', 'side')):
                raise FirmwareError('inventory physical/logical identity mismatch')
            state = row.get('state')
            if state not in ('verified', 'pending', 'needs_attention', 'not_verified_for_policy'):
                raise FirmwareError('invalid inventory state')
            if state == 'not_verified_for_policy':
                continue
            try:
                instant = datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00'))
                if instant.tzinfo is None:
                    raise ValueError('timezone is missing')
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise FirmwareError('inventory observation needs an explicit timezone') from exc
            if state == 'verified' and (row.get('running_image_sha256') != RUNNING_SHA or row.get('verified_at') != row.get('observed_at')):
                raise FirmwareError('verified export lacks the approved runtime hash and time')
            # A newer failure supersedes an older success. Tied conflicting
            # observations fail closed rather than arbitrarily choosing a host.
            if name in times and instant == times[name] and row != merged[name]:
                raise FirmwareError('conflicting inventory observations at the same time')
            if name not in times or instant > times[name]:
                times[name] = instant
                merged[name] = dict(row)
    rows = list(merged.values())
    return {'schema': 1, 'policy_id': policy.policy_id, 'policy_sha256': policy.sha256,
            'scope': 'observed_devices_only' if policy.compatible else 'explicit_inventory',
            'devices': rows, 'all_verified_in_saved_history': bool(rows) and all(r['state'] == 'verified' for r in rows)}
