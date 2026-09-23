"""Signed, bundled compatibility rules; optional legacy device policies."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ._usb import UsbError

PART = 'OGLO-PCBA-D'
HARDWARE = 'RDR02_FLEX5_REV_D_TIA'
KEY_ID = 'oglo-fw-prod-p256-1'
PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaQPeZy9FP2htR+bFEJLP/hE5KohX
OIw/oWCZrADOjiV59AUtZuD3yXTimjGaQZGIPieWOcu2qNl7qGco9JVnUQ==
-----END PUBLIC KEY-----
'''
# This first implementation intentionally supports only this reviewed migration.
VERSION = '0.9.17'
FILE_SHA = 'bcfdb9944e27bc38289d44edb6b1a6d6c3838244fa805723c8dd2ee3a8c4a3ad'
RUNNING_SHA = 'eddf0ca99dcd929e202464d2a9c311923e895bee95fd7aa0c5dd7ec013a01615'
FROM_SHA = 'b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897'


class FirmwareError(UsbError):
    """Preparation failed. Do not start capture with a partially prepared pair."""


def read_json(path: Path, limit: int = 1024 * 1024):
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError(f'duplicate JSON key: {k}')
            obj[k] = v
        return obj
    try:
        with path.open('rb') as f:
            raw = f.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('JSON is too large')
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if not isinstance(value, dict):
            raise ValueError('expected JSON object')
        return value
    except (ValueError, OSError) as exc:
        raise FirmwareError(f'cannot read {path}: {exc}') from exc


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class FirmwarePolicy:
    path: Path | None
    policy_id: str
    sha256: str
    bundle: Path
    devices: tuple

    @property
    def compatible(self):
        return self.path is None

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().resolve()
        data = read_json(path)
        if set(data) != {'schema', 'id', 'enabled', 'bundle', 'devices'} or type(data['schema']) is not int or data['schema'] != 1 or data['enabled'] is not True:
            raise FirmwareError('policy requires schema=1, enabled=true, id, bundle and devices only')
        if not isinstance(data['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', data['id']):
            raise FirmwareError('invalid policy id')
        if not isinstance(data['bundle'], str) or not data['bundle']:
            raise FirmwareError('policy needs an offline bundle directory')
        devices = data['devices']
        if not isinstance(devices, list) or not 1 <= len(devices) <= 1000:
            raise FirmwareError('policy needs an explicit device inventory')
        serials, chips = set(), set()
        normalized = []
        for d in devices:
            if not isinstance(d, dict) or set(d) != {'serial', 'usb_serial', 'side'}:
                raise FirmwareError('each device needs serial, usb_serial and side')
            if not isinstance(d['serial'], str) or not re.fullmatch(r'OGLO-[LR]-[A-Za-z0-9-]+', d['serial']):
                raise FirmwareError('invalid logical serial')
            if not isinstance(d['usb_serial'], str) or not re.fullmatch(r'[0-9a-fA-F]{12}', d['usb_serial']):
                raise FirmwareError('USB serial must be the full 12-digit chip identity')
            if d['side'] != ('left' if d['serial'].startswith('OGLO-L-') else 'right'):
                raise FirmwareError('logical serial and expected side disagree')
            if d['serial'].casefold() in serials or d['usb_serial'].casefold() in chips:
                raise FirmwareError('duplicate logical or USB identity in policy')
            serials.add(d['serial'].casefold()); chips.add(d['usb_serial'].casefold())
            normalized.append({**d, 'usb_serial': d['usb_serial'].upper()})
        return cls(path, data['id'], digest(data), (path.parent / data['bundle']).resolve(), tuple(normalized))


def bundled_policy():
    # This describes compatible products/images, never customers or glove IDs.
    rules = {'schema': 1, 'part': PART, 'hardware': HARDWARE, 'key_id': KEY_ID,
             'from_sha256': FROM_SHA, 'target_sha256': RUNNING_SHA, 'file_sha256': FILE_SHA}
    return FirmwarePolicy(None, 'oglo-compatible-0917-v1', digest(rules),
                          Path(__file__).parent / 'firmware_bundle', ())


def settings_path():
    from ._ownership import state_directory
    # Enable only for this Python environment, not every application of this user.
    environment = str(Path(sys.prefix).resolve())
    root = state_directory() / 'environments'
    root.mkdir(mode=0o700, exist_ok=True)
    return root / (hashlib.sha256(environment.encode()).hexdigest() + '.json')


def auto_update_enabled():
    path = settings_path()
    if not path.exists():
        return False
    data = read_json(path)
    if (set(data) != {'schema', 'enabled', 'environment'} or type(data['schema']) is not int or data['schema'] != 1 or
            type(data['enabled']) is not bool or data['environment'] != str(Path(sys.prefix).resolve())):
        raise FirmwareError('invalid automatic firmware setting; run oglo firmware enable or disable')
    return data['enabled']


def configure_auto_update(enabled):
    from ._firmware_journal import atomic_json
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    if os.environ.get('OGLO_FIRMWARE_POLICY'):
        raise FirmwareError('unset OGLO_FIRMWARE_POLICY before changing compatibility-based updates')
    if enabled:
        if sys.platform not in ('darwin', 'linux'):
            raise FirmwareError('automatic firmware updates require macOS/Linux')
        load_bundle(bundled_policy().bundle)
    path = settings_path()
    atomic_json(path, {'schema': 1, 'enabled': enabled, 'environment': str(Path(sys.prefix).resolve())})
    return path


def resolve_policy(value=None):
    if value is False:
        return None
    if value is True:
        return bundled_policy()
    if isinstance(value, FirmwarePolicy):
        fresh = bundled_policy() if value.compatible else FirmwarePolicy.load(value.path)
        if fresh.sha256 != value.sha256:
            raise FirmwareError('policy changed after loading')
        return fresh
    path = value or os.environ.get('OGLO_FIRMWARE_POLICY')
    return FirmwarePolicy.load(path) if path else (bundled_policy() if auto_update_enabled() else None)


@dataclass(frozen=True)
class Bundle:
    image: bytes
    manifest: bytes
    signature: bytes

    @property
    def begin(self):
        sig = base64.b64encode(self.signature).decode('ascii')
        return f'FW BEGIN 1 {PART} {HARDWARE} {VERSION} {len(self.image)} {FILE_SHA} {KEY_ID} {sig}\n'.encode('ascii')


def load_bundle(directory: Path) -> Bundle:
    try:
        def bounded(name, size):
            with (directory / name).open('rb') as f:
                data = f.read(size + 1)
            if len(data) > size:
                raise ValueError(f'{name} exceeds size limit')
            return data
        image = bounded('application.bin', 0x330000)
        manifest = bounded('manifest.txt', 1024)
        signature = bounded('signature.der', 80)
        expected = (f'OGLO-FW-MANIFEST-V1\npart={PART}\nhw_rev={HARDWARE}\nversion={VERSION}\n'
                    f'size={len(image)}\nsha256={FILE_SHA}\nkey_id={KEY_ID}\n').encode('ascii')
        if manifest != expected or not 64 <= len(signature) <= 80:
            raise ValueError('noncanonical manifest or signature')
        if hashlib.sha256(image).hexdigest() != FILE_SHA:
            raise ValueError('application file hash does not match approved release')
        if len(image) < 64 or image[-32:].hex() != RUNNING_SHA or hashlib.sha256(image[:-32]).digest() != image[-32:]:
            raise ValueError('ESP application runtime digest does not match approved release')
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = serialization.load_pem_public_key(PUBLIC_KEY)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError('unexpected signing key')
        key.verify(signature, manifest, ec.ECDSA(hashes.SHA256()))
        return Bundle(image, manifest, signature)
    except ImportError as exc:
        raise FirmwareError('managed firmware requires installing oglo[firmware]') from exc
    except Exception as exc:
        raise FirmwareError(f'invalid signed firmware bundle: {type(exc).__name__}: {exc}') from exc
