"""Exercise the generated installer in a clean environment, without opening USB.

Use the local release wheel as the download source; checksum, pip installation,
signature verification and persistent enablement use the actual installer code.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import venv


def main(installer, wheel):
    installer, wheel = Path(installer).resolve(), Path(wheel).resolve()
    with tempfile.TemporaryDirectory(prefix='oglo-installer-check-') as temp:
        root = Path(temp)
        environment = root / 'venv'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env.pop('OGLO_FIRMWARE_POLICY', None)
        env['OGLO_STATE_DIR'] = str(root / 'state')
        script = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location('release_installer', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.URL = sys.argv[2]
module.main(['--auto-firmware'])
'''
        subprocess.run([str(python), '-c', script, str(installer), wheel.as_uri()],
                       cwd=root, env=env, check=True)
        check = '''import oglo
from oglo._firmware_package import resolve_policy, load_bundle
p = resolve_policy()
assert p is not None and p.compatible and not p.devices
assert load_bundle(p.bundle).image
print('INSTALLER CHECK PASSED:', oglo.__version__)
'''
        subprocess.run([str(python), '-c', check], cwd=root, env=env, check=True)


if __name__ == '__main__':
    main(*sys.argv[1:])
