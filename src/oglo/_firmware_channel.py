"""Fetch the current signed firmware at runtime, so a new release needs no new SDK.

Before this existed the firmware lived only inside the wheel, which tied the
firmware version to the SDK version: every firmware release needed an SDK
release, and every SDK release needed every customer to reinstall. The plan that
shipped the bundled path said the server route would be "defined as a separate
interface" later. This is that interface.

Nothing here is trusted. The channel supplies bytes; `load_bundle` decides
whether they are a release, by verifying the detached KMS signature over the
manifest against the public key pinned in `_firmware_package`. A hostile or
broken channel can therefore serve nothing worse than an older genuine release
or no release at all, and both of those fall back to the copy inside the wheel.
That is why plain HTTPS to a public URL is enough: the signature is the trust
anchor, not the transport.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from ._firmware_package import (FirmwareError, KEY_ID, PART, HARDWARE,
                                VERSION as BUNDLED_VERSION, load_bundle,
                                read_json, version_tuple)

# A release tag whose assets are replaced in place, so this URL never changes.
# A new firmware updates the assets here and every installed SDK picks it up on
# its next connect. Overridable for tests and for an air-gapped mirror.
CHANNEL_URL = os.environ.get(
    'OGLO_FIRMWARE_CHANNEL_URL',
    'https://github.com/OpenGraphLabs/oglo-python/releases/download/firmware-current',
)
CHANNEL_FILES = ('current.json', 'application.bin', 'manifest.txt', 'signature.der')
# The application is bounded by the OTA slot; the rest are tiny by construction.
LIMITS = {'current.json': 4096, 'application.bin': 0x330000,
          'manifest.txt': 1024, 'signature.der': 80}
# Short on purpose: a blackholed network must not delay the start of a
# capture. Falling back to the bundled release costs nothing.
TIMEOUT_SECONDS = float(os.environ.get('OGLO_FIRMWARE_CHANNEL_TIMEOUT', '5'))
# Cache directories are named by version, and nothing else is read as one.
VERSION_DIR_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')


def channel_root() -> Path:
    from ._ownership import state_directory
    root = state_directory() / 'firmware-channel'
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def known_releases() -> dict:
    """Releases this install has verified, as {version: running_sha256}.

    Every entry was written only after `load_bundle` accepted the bytes, so this
    is a record of what this machine has proven, not of what it was told. It is
    what lets a glove already carrying a previous release still be recognised as
    a legitimate starting point several firmware versions later.
    """
    path = channel_root() / 'known_releases.json'
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except FirmwareError:
        return {}
    if data.get('schema') != 1 or not isinstance(data.get('releases'), dict):
        return {}
    out = {}
    for version, running in data['releases'].items():
        if isinstance(version, str) and isinstance(running, str) and len(running) == 64:
            out[version] = running
    return out


def remember_release(version: str, running_sha256: str) -> None:
    from ._firmware_journal import atomic_json
    releases = known_releases()
    if releases.get(version) == running_sha256:
        return
    releases[version] = running_sha256
    atomic_json(channel_root() / 'known_releases.json',
                {'schema': 1, 'releases': dict(sorted(releases.items()))})


def _get(name: str) -> bytes:
    request = urllib.request.Request(
        f'{CHANNEL_URL}/{name}',
        headers={'User-Agent': 'oglo-sdk-firmware-channel/1'},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        limit = LIMITS[name]
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{name} exceeds its size limit')
    return data


def cached_release(version: str) -> Path | None:
    """A previously fetched and verified release directory, re-verified now."""
    directory = channel_root() / version
    if not all((directory / name).exists() for name in CHANNEL_FILES[1:]):
        return None
    try:
        bundle = load_bundle(directory)
    except FirmwareError:
        return None
    return directory if bundle.version == version else None


def newest_cached() -> Path | None:
    """The newest already-verified release on disk that beats the wheel.

    Reached when the pointer itself cannot be fetched. A lab that fetched a
    newer release yesterday must not quietly fall back to the wheel today just
    because the network is down, which would move a glove backwards on the next
    connect. Each candidate is re-verified, so a cache edited on disk is
    discarded rather than trusted.
    """
    try:
        candidates = sorted(
            (d for d in channel_root().iterdir()
             if d.is_dir() and VERSION_DIR_RE.match(d.name)
             and version_tuple(d.name) > version_tuple(BUNDLED_VERSION)),
            key=lambda d: version_tuple(d.name),
            reverse=True,
        )
    except (OSError, FirmwareError):
        return None
    for directory in candidates:
        verified = cached_release(directory.name)
        if verified is not None:
            return verified
    return None


# Resolved at most once per process. `connect()` must not pay a network round
# trip every call, and a capture loop that opens gloves repeatedly must not turn
# into a stream of requests. A fresh process checks again, which is what makes a
# new release arrive without anyone doing anything.
_RESOLVED: list = []


def reset_cache() -> None:
    """Forget this process's answer. For tests and for a forced re-check."""
    _RESOLVED.clear()


def fetch_current(*, offline_ok: bool = True) -> Path | None:
    if _RESOLVED:
        return _RESOLVED[0]
    result = _fetch_current_uncached(offline_ok=offline_ok)
    _RESOLVED.append(result)
    return result


def _fetch_current_uncached(*, offline_ok: bool = True) -> Path | None:
    """Return a verified directory for the current release, or None.

    None means "use what is in the wheel". Every failure mode lands there: no
    network, a moved URL, a malformed pointer, bytes that do not verify, or a
    channel offering something older than the SDK already carries. Returning
    None is always safe, because the bundled release is itself signed.
    """
    try:
        pointer = json.loads(_get('current.json').decode('utf-8'))
        if pointer.get('schema') != 1:
            raise ValueError('unsupported channel schema')
        version = pointer.get('version')
        if not isinstance(version, str) or not version:
            raise ValueError('channel names no version')
        # The pointer is a hint for deciding whether to download at all. It is
        # never the authority: the manifest signature is, and it is checked
        # below against this same version.
        if version_tuple(version) <= version_tuple(BUNDLED_VERSION):
            # Nothing newer than the wheel. Never move a glove backwards.
            return None
        cached = cached_release(version)
        if cached is not None:
            return cached
        staging = Path(tempfile.mkdtemp(prefix='oglo-fw-', dir=str(channel_root())))
        try:
            for name in CHANNEL_FILES[1:]:
                (staging / name).write_bytes(_get(name))
            bundle = load_bundle(staging)
            if bundle.version != version:
                raise ValueError('channel pointer and signed manifest disagree')
            if version_tuple(bundle.version) <= version_tuple(BUNDLED_VERSION):
                raise ValueError('signed release is not newer than the bundled one')
            destination = channel_root() / bundle.version
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            staging.replace(destination)
            remember_release(bundle.version, bundle.running_sha256)
            return destination
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except (urllib.error.URLError, OSError, ValueError, FirmwareError,
            json.JSONDecodeError, TimeoutError):
        if offline_ok:
            return newest_cached()
        raise


def pointer_for(directory: Path) -> bytes:
    """The current.json a publisher should upload beside a bundle directory."""
    bundle = load_bundle(directory)
    return (json.dumps({'schema': 1, 'part': PART, 'hw_rev': HARDWARE,
                        'key_id': KEY_ID, 'version': bundle.version,
                        'size': len(bundle.image),
                        'file_sha256': bundle.file_sha256,
                        'running_sha256': bundle.running_sha256},
                       indent=2, sort_keys=True) + '\n').encode('utf-8')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
