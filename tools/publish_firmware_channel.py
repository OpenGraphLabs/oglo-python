#!/usr/bin/env python3
"""Publish a signed firmware bundle to the public runtime channel.

Every installed SDK reads the `firmware-current` release's assets, so this is
what makes a new firmware reach customers without anyone reinstalling anything.
Run it once per firmware release, after the signed bundle is committed in
`oglo-hardware`.

    python3 tools/publish_firmware_channel.py \\
        --bundle ../oglo-hardware/firmware/OGLO-MT-RDR-02/oglo_rdr02_tia/golden_build

It verifies the bundle before uploading and verifies the live channel after, so
a half-finished publish cannot be mistaken for a finished one. The tag is fixed
on purpose: the URL baked into every SDK must never change.

Nothing here signs anything. The signature comes from the KMS workflow in
`oglo-hardware`; this only moves already-signed bytes to a public place. That is
why a compromised publish cannot push firmware: without the signing key, the
bytes do not load.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from oglo import _firmware_channel as channel  # noqa: E402
from oglo import _firmware_package as pkg  # noqa: E402

TAG = 'firmware-current'
REPO = 'OpenGraphLabs/oglo-python'


def run(*args: str) -> str:
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f'{" ".join(args)} failed:\n{done.stderr.strip()}')
    return done.stdout.strip()


def staged_bundle(source: Path, staging: Path) -> tuple:
    """Copy the three signed files under their channel names and verify them."""
    names = {'application.bin': ('oglo_rdr02_tia.ino.bin', 'application.bin'),
             'manifest.txt': (None, 'manifest.txt'),
             'signature.der': (None, 'signature.der')}
    for target, (golden_name, plain) in names.items():
        candidates = [source / plain]
        if golden_name:
            candidates.append(source / golden_name)
        # A golden_build/ directory names the signed pair by version.
        candidates += sorted(source.glob('*.manifest')) if target == 'manifest.txt' else []
        candidates += sorted(source.glob('*.sig')) if target == 'signature.der' else []
        for candidate in candidates:
            if candidate.is_file():
                shutil.copyfile(candidate, staging / target)
                break
        else:
            raise SystemExit(f'{source} has no file for {target}')
    bundle = pkg.load_bundle(staging)
    pointer = channel.pointer_for(staging)
    (staging / 'current.json').write_bytes(pointer)
    return bundle, json.loads(pointer)


def live_pointer() -> dict | None:
    url = f'https://github.com/{REPO}/releases/download/{TAG}/current.json'
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.loads(response.read(4096).decode('utf-8'))
    except Exception:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path,
                        help='directory holding the signed application, manifest and signature')
    parser.add_argument('--dry-run', action='store_true',
                        help='verify and print what would be published, upload nothing')
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix='oglo-channel-') as temp:
        staging = Path(temp)
        bundle, pointer = staged_bundle(args.bundle.resolve(), staging)
        print(f'bundle    {bundle.version}  {len(bundle.image)} B')
        print(f'file      {bundle.file_sha256}')
        print(f'running   {bundle.running_sha256}')

        before = live_pointer()
        print(f'channel   {before.get("version") if before else "(not published yet)"}')
        if before and before.get('version') == bundle.version:
            print('Channel already serves this release; nothing to do.')
            return 0
        if args.dry_run:
            print('\ndry run: nothing uploaded.')
            return 0

        files = [str(staging / name) for name in channel.CHANNEL_FILES]
        existing = subprocess.run(['gh', 'release', 'view', TAG, '--repo', REPO],
                                  capture_output=True, text=True)
        if existing.returncode != 0:
            run('gh', 'release', 'create', TAG, '--repo', REPO, '--prerelease',
                '--title', 'Current OGLO firmware (runtime channel)',
                '--notes',
                'Assets read at runtime by every installed OGLO SDK. Replaced in '
                'place for each firmware release so the URL never changes. The '
                'manifest signature, not this page, is what makes the bytes a '
                'release: an SDK verifies it against the key compiled into it and '
                'otherwise falls back to the copy inside its own wheel.',
                *files)
        else:
            run('gh', 'release', 'upload', TAG, '--repo', REPO, '--clobber', *files)

        # A brand-new asset takes a moment to become downloadable, so retry
        # before declaring failure. Still fails closed: a publish that cannot be
        # read back is not a publish, because customers read it, not this page.
        after = None
        for attempt in range(6):
            after = live_pointer()
            if after and after.get('version') == bundle.version:
                break
            time.sleep(5)
        if not after or after.get('version') != bundle.version:
            raise SystemExit(
                f'published, but the channel still reports '
                f'{after.get("version") if after else "nothing"}; do not treat this as done')
        print(f'\nchannel now serves {after["version"]}')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
