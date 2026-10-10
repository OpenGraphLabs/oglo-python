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
# Only reviewed migrations are supported. The target is the bundled image; the
# sources are the exact released images a glove may be running before it.
VERSION = '0.9.25'
FILE_SHA = '3e9d2f1e3c8d085c19ffd345f2f34572f7c5ce65164d2c3b9ef52148ca408d63'
RUNNING_SHA = 'd024e7296cec141ce1f02fbcdbd787895bb2da6fa2ad4808d80c174b85e6bbb4'
# 0.9.17 became the field release on 2026-10-03, so a glove reaching the SDK may
# already be on it. Accepting only 0.9.16 would refuse exactly the up-to-date
# fleet. Each entry is an exact released running image, never a version string
# on its own.
FROM_IMAGES = {
    '0.9.16': 'b1c53157df9fc259a64ebe8a2c0454d916d2c2ccac163f083335496234345897',
    '0.9.17': 'eddf0ca99dcd929e202464d2a9c311923e895bee95fd7aa0c5dd7ec013a01615',
    # 0.9.18 was the field release from 2026-10-05 to 2026-10-07, and gloves
    # updated by dev3/dev4 are sitting on exactly this image. Leaving it out
    # would refuse the fleet this SDK had just finished updating.
    '0.9.18': 'f83f4e5b8e706d7b53868b5537c5afb9549c9cf95c23a03be86182de2b31bdb1',
}
# Retained for the policy description: the oldest accepted source image.
FROM_SHA = FROM_IMAGES['0.9.16']


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


BUNDLE_DIR = Path(__file__).parent / 'firmware_bundle'


def accepted_sources(target=None) -> dict:
    """Running images a glove may legitimately be on before an update.

    Four sources, and none of them is a server's word:

    * `FROM_IMAGES`, the reviewed floor compiled into this SDK.
    * the release inside the wheel, because a glove this SDK already updated is
      sitting on exactly that image.
    * every release this install has itself fetched and verified, so a glove is
      still recognised several firmware versions later.
    * the target, which makes re-running an update a no-op instead of a refusal.

    A version string alone is never enough: each entry is an exact running
    image, which is what the glove reports and what cannot be faked by
    relabelling a build.
    """
    sources = dict(FROM_IMAGES)
    sources.setdefault(VERSION, RUNNING_SHA)
    try:
        from ._firmware_channel import known_releases
        sources.update(known_releases())
    except Exception:
        # A missing or unreadable cache must never widen or block the floor.
        pass
    if target is not None:
        version, running = target
        sources[version] = running
    return sources


def current_policy():
    """The newest verified release available: fetched if there is one, else the wheel.

    This is what removes "new firmware needs a new SDK, which needs every
    customer to reinstall". `fetch_current` returns None for every failure, so
    the wheel's copy remains the floor and an offline machine behaves exactly
    as it did before this existed.
    """
    directory, bundle = BUNDLE_DIR, None
    try:
        from ._firmware_channel import fetch_current
        fetched = fetch_current()
        if fetched is not None:
            bundle = load_bundle(fetched)
            directory = fetched
    except FirmwareError:
        directory, bundle = BUNDLE_DIR, None
    if bundle is None:
        version, file_sha, running = VERSION, FILE_SHA, RUNNING_SHA
    else:
        version, file_sha, running = bundle.version, bundle.file_sha256, bundle.running_sha256
    # This describes compatible products/images, never customers or glove IDs.
    rules = {'schema': 1, 'part': PART, 'hardware': HARDWARE, 'key_id': KEY_ID,
             'from_sha256': FROM_SHA,
             'from_sha256_accepted': dict(sorted(accepted_sources((version, running)).items())),
             'target_version': version,
             'target_sha256': running, 'file_sha256': file_sha}
    return FirmwarePolicy(None, f'oglo-compatible-{version}-v2', digest(rules),
                          directory, ())


def bundled_policy():
    """Retained name; resolves to the current release, fetched or bundled."""
    return current_policy()


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
    version: str
    file_sha256: str
    running_sha256: str

    @property
    def begin(self):
        sig = base64.b64encode(self.signature).decode('ascii')
        return (f'FW BEGIN 1 {PART} {HARDWARE} {self.version} {len(self.image)} '
                f'{self.file_sha256} {KEY_ID} {sig}\n').encode('ascii')


# The manifest is the release identity, and the detached KMS signature over its
# exact bytes is what makes it one. Everything below is derived from a manifest
# that verified against the public key pinned in this file, never from a file
# name, a version string or whatever served the bytes. That is what lets a
# release arrive at runtime without the transport having to be trusted: the only
# thing that can produce a loadable bundle is the signing key.
VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


def version_tuple(version: str) -> tuple:
    if not VERSION_RE.match(version):
        raise FirmwareError(f'malformed firmware version: {version!r}')
    return tuple(int(part) for part in version.split('.'))


def parse_manifest(manifest: bytes, image_len: int) -> tuple:
    """Return (version, file_sha256) from the canonical manifest, or raise."""
    expected_keys = ('part', 'hw_rev', 'version', 'size', 'sha256', 'key_id')
    text = manifest.decode('ascii')
    lines = text.split('\n')
    if len(lines) != 8 or lines[0] != 'OGLO-FW-MANIFEST-V1' or lines[7] != '':
        raise ValueError('noncanonical manifest shape')
    fields = {}
    for line, key in zip(lines[1:7], expected_keys):
        name, sep, value = line.partition('=')
        if not sep or name != key:
            raise ValueError(f'manifest expected {key} at this line')
        fields[name] = value
    # Identity this SDK will not accept a substitution for, at any version.
    if fields['part'] != PART or fields['hw_rev'] != HARDWARE or fields['key_id'] != KEY_ID:
        raise ValueError('manifest describes another product or signing key')
    if not VERSION_RE.match(fields['version']) or not SHA256_RE.match(fields['sha256']):
        raise ValueError('manifest version or hash is malformed')
    if fields['size'] != str(image_len):
        raise ValueError('manifest size does not match the application')
    return fields['version'], fields['sha256']


def load_bundle(directory: Path, *, expect=None) -> Bundle:
    """Load and fully verify a signed bundle directory.

    `expect` is a (version, file_sha256, running_sha256) triple the bundle must
    equal. The bundle shipped inside the wheel passes the pinned constants, so
    it stays byte-identical to what was reviewed. A bundle fetched at runtime
    passes None, and its identity comes from its own signed manifest.
    """
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
        if not 64 <= len(signature) <= 80:
            raise ValueError('implausible signature length')
        version, file_sha = parse_manifest(manifest, len(image))
        if hashlib.sha256(image).hexdigest() != file_sha:
            raise ValueError('application file hash does not match its manifest')
        # The running digest is the last 32 bytes of the image and must be the
        # hash of everything before it, so a tampered image cannot keep it.
        if len(image) < 64 or hashlib.sha256(image[:-32]).digest() != image[-32:]:
            raise ValueError('ESP application runtime digest is inconsistent')
        running_sha = image[-32:].hex()
        if expect is not None and (version, file_sha, running_sha) != tuple(expect):
            raise ValueError('bundle is not the release this SDK pins')
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = serialization.load_pem_public_key(PUBLIC_KEY)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError('unexpected signing key')
        key.verify(signature, manifest, ec.ECDSA(hashes.SHA256()))
        return Bundle(image, manifest, signature, version, file_sha, running_sha)
    except ImportError as exc:
        raise FirmwareError('managed firmware requires installing oglo[firmware]') from exc
    except Exception as exc:
        raise FirmwareError(f'invalid signed firmware bundle: {type(exc).__name__}: {exc}') from exc
