"""Isolated USB worker. No user module imports, even for top-level connect_pair()."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from ._firmware_journal import atomic_json, journal_path, read_journal
from ._firmware_package import FirmwareError, RUNNING_SHA, load_bundle, resolve_policy, read_json
from ._firmware_protocol import basic_health, compare_preserved, snapshot, transfer
from ._ownership import usb_identity
from ._usb import _open_serial_locked, list_candidates

QUIET_SECONDS = 65.0


def emit(**event):
    print(json.dumps(event, allow_nan=False), flush=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(device):
    return usb_identity(0x2886, 0x0056, device['usb_serial'])


def find_device(device):
    matches = [p for p in list_candidates() if p.vid == 0x2886 and p.pid == 0x0056 and
               p.serial_number and p.serial_number.casefold() == device['usb_serial'].casefold()]
    if len(matches) != 1:
        raise FirmwareError(f"{device['serial']}: expected one USB {device['usb_serial']}; found {len(matches)}")
    return matches[0].device


def close_port(port):
    # Clear firmware's sticky host-ping authorization even when HUPCL is off.
    # This is a USB control request, not text in an unfinished binary session.
    try:
        if hasattr(port, 'dtr'):
            port.dtr = False
    finally:
        port.close()


def run(policy, devices, attempt):
    bundle = load_bundle(policy.bundle)
    saved = {}
    pending = False
    for device in devices:
        key = identity(device)
        entry = read_journal(key)
        if entry:
            if entry['state'] == 'needs_attention':
                raise FirmwareError(f"{device['serial']}: preservation failure needs investigation; original backup retained")
            if entry['target_sha256'] != RUNNING_SHA:
                raise FirmwareError('recovery journal has a different firmware target')
            if entry['state'] == 'pending' and entry.get('policy_sha256') != policy.sha256:
                raise FirmwareError('pending attempt requires the original policy')
            pending |= entry['state'] == 'pending'
        saved[key] = entry
    if pending:
        emit(phase='quiet', serial='pending devices', seconds=QUIET_SECONDS)
        # Starts afresh after acquiring all locks, even across host reboot/clock
        # jumps. No port is opened and no OUT byte is sent during this interval.
        time.sleep(QUIET_SECONDS)
    snapshots = {}
    # All selected identities and preservation baselines must pass before ANY BEGIN.
    for device in devices:
        key = identity(device)
        emit(phase='inspect', serial=device['serial'])
        port = _open_serial_locked(find_device(device))
        try:
            current = snapshot(port, device)
            if saved[key] and saved[key]['state'] == 'pending':
                try:
                    compare_preserved(saved[key]['before'], current)
                except FirmwareError:
                    entry = {**saved[key], 'state': 'needs_attention', 'observed': current, 'updated_at': now()}
                    atomic_json(journal_path(key), entry)
                    raise
            snapshots[key] = current
        finally:
            emit(phase='close', serial=device['serial'])
            close_port(port)
    results = []
    for device in devices:
        key = identity(device)
        current = snapshots[key]
        old = saved[key]
        recovering = old is not None and old['state'] == 'pending'
        before = old['before'] if recovering else current
        needs_write = current['fwinfo']['running_image_sha256'] != RUNNING_SHA
        entry = {'schema': 1, 'identity': key, 'serial': device['serial'],
                 'usb_serial': device['usb_serial'], 'state': 'pending',
                 'attempt': attempt, 'policy_id': policy.policy_id, 'policy_sha256': policy.sha256,
                 'target_sha256': RUNNING_SHA, 'before': before, 'updated_at': now(),
                 'preservation_compared': recovering or needs_write}
        # Save before opening for write: lost READY, killed process and blocked
        # close must all leave a recoverable pending marker.
        atomic_json(journal_path(key), entry)
        if needs_write:
            emit(phase='open', serial=device['serial'])
            port = _open_serial_locked(find_device(device))
            try:
                # Close/reopen can cross a cable swap or external reset. Recheck
                # identity/config and source hash immediately before erasing OTA.
                fresh = snapshot(port, device)
                compare_preserved(before, fresh)
                if fresh['fwinfo'] != current['fwinfo']:
                    raise FirmwareError('running image changed between preflight and transfer')
                emit(phase='write', serial=device['serial'])
                confirmed = transfer(port, bundle, lambda **e: emit(serial=device['serial'], **e))
            finally:
                emit(phase='close', serial=device['serial'])
                # No ABORT/STOP/PING here. Incomplete transfer may still be binary.
                close_port(port)
            if not confirmed:
                emit(phase='quiet', serial=device['serial'], seconds=QUIET_SECONDS)
                time.sleep(QUIET_SECONDS)
            emit(phase='reboot', serial=device['serial'])
            time.sleep(1.0)
            end = time.monotonic() + 50
            last = None
            while time.monotonic() < end:
                port = None
                try:
                    port = _open_serial_locked(find_device(device))
                    current = snapshot(port, device)
                    if current['fwinfo']['running_image_sha256'] != RUNNING_SHA:
                        # If COMMIT was never received, this may still be the old
                        # image. Do not write or keep sending recovery commands.
                        raise FirmwareError('automatic reboot did not produce the approved image; rerun preparation after recovery')
                    break
                except FirmwareError:
                    if port is not None:
                        raise
                    last = sys.exc_info()[1]
                except OSError as exc:
                    last = exc
                finally:
                    if port is not None:
                        close_port(port)
                time.sleep(.5)
            else:
                raise FirmwareError(f'{device["serial"]}: did not reappear after commit: {last}')
        emit(phase='verify', serial=device['serial'])
        port = _open_serial_locked(find_device(device))
        try:
            current = snapshot(port, device)
            if current['fwinfo']['running_image_sha256'] != RUNNING_SHA:
                raise FirmwareError('target image is not running')
            try:
                compare_preserved(before, current)
            except FirmwareError:
                entry.update(state='needs_attention', observed=current, updated_at=now())
                atomic_json(journal_path(key), entry)
                raise
            health = basic_health(port)
            # Health check starts/stops streams; ensure it left persistent values intact.
            final = snapshot(port, device)
            try:
                compare_preserved(before, final)
            except FirmwareError:
                entry.update(state='needs_attention', observed=final, updated_at=now())
                atomic_json(journal_path(key), entry)
                raise
            if final['fwinfo'] != current['fwinfo']:
                raise FirmwareError('image identity changed during readiness verification')
        finally:
            emit(phase='close', serial=device['serial'])
            close_port(port)
        entry.update(state='verified', after=final, health=health, updated_at=now(), wrote_image=needs_write)
        atomic_json(journal_path(key), entry)
        results.append(entry)
        emit(phase='verified', serial=device['serial'], wrote_image=needs_write)
    return results


def main(path):
    request = read_json(path)
    # A directly invoked worker without the parent's inherited leases must fail.
    for fd in request['lease_fds']:
        os.fstat(fd)
    policy = resolve_policy(request['policy'])
    if policy.sha256 != request['policy_sha256']:
        raise FirmwareError('policy changed before worker start')
    expected = [d for d in policy.devices if d['serial'] in request['serials']]
    if len(expected) != len(request['serials']) or len(expected) != len(request['lease_fds']):
        raise FirmwareError('worker target/lease mismatch')
    result = run(policy, expected, request['attempt'])
    emit(result=result)


if __name__ == '__main__':
    from pathlib import Path
    try:
        main(Path(sys.argv[1]))
    except BaseException as exc:
        emit(error=f'{type(exc).__name__}: {exc}')
        sys.exit(1)
