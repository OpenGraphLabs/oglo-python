"""Generate the generic one-command release installer from the exact wheel."""
from pathlib import Path
import argparse
import hashlib
import re


def build(wheel, output):
    wheel = Path(wheel)
    match = re.fullmatch(r'oglo-([0-9A-Za-z.]+)-py3-none-any.whl', wheel.name)
    if not match:
        raise ValueError('expected the OGLO universal wheel')
    version = match[1]
    source = Path(__file__).with_name('install_template.py').read_text()
    for key, value in {'VERSION': version, 'WHEEL': wheel.name,
                       'SHA256': hashlib.sha256(wheel.read_bytes()).hexdigest()}.items():
        source = source.replace('@' + key + '@', value)
    Path(output).write_text(source)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.wheel, args.output)
