#!/usr/bin/env python3
"""Build the offline SDK bundle from already signed release artifacts; never signs."""
import argparse
import shutil
import tempfile
from pathlib import Path

from oglo._firmware_package import load_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--signature', type=Path, required=True, help='DER signature, not a private key')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; will not overwrite a distributed bundle')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temp:
        staging = Path(temp) / 'bundle'; staging.mkdir()
        for source, name in ((args.application, 'application.bin'), (args.manifest, 'manifest.txt'), (args.signature, 'signature.der')):
            shutil.copyfile(source, staging / name)
        bundle = load_bundle(staging)
        staging.rename(args.output)
    print(f'Verified signed bundle: {args.output} ({len(bundle.image)} application bytes)')


if __name__ == '__main__':
    main()
