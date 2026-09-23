"""Atomic recovery state, written before BEGIN can reach the USB endpoint."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ._firmware_package import FirmwareError, read_json
from ._ownership import state_directory


def journal_path(identity: str) -> Path:
    root = state_directory() / 'firmware'
    root.mkdir(mode=0o700, exist_ok=True)
    return root / (hashlib.sha256(identity.encode()).hexdigest() + '.json')


def atomic_json(path: Path, value) -> None:
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == 'posix':
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_journal(identity):
    path = journal_path(identity)
    if not path.exists():
        return None
    data = read_json(path)
    if (data.get('schema') != 1 or data.get('identity') != identity or
            data.get('state') not in {'pending', 'verified', 'needs_attention'} or
            not isinstance(data.get('before'), dict) or
            not isinstance(data.get('target_sha256'), str)):
        raise FirmwareError(f'invalid recovery journal {path}; preserve it for diagnosis')
    return data


def require_capture_ready(identity):
    previous = read_journal(identity)
    if previous and previous['state'] != 'verified':
        raise FirmwareError('unfinished firmware preparation; rerun with the same firmware policy before capture')
