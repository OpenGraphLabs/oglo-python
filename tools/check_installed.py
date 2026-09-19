"""Check that the installed distribution, rather than src/, matches this checkout.

Run after installing the candidate wheel or sdist. This does not open hardware.
"""

from importlib.metadata import version
from pathlib import Path
import hashlib
import json
import re

import oglo


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    source = root / "src" / "oglo"
    installed = Path(oglo.__file__).resolve().parent
    expected = re.search(
        r'(?m)^version = "([^"]+)"', (root / "pyproject.toml").read_text()
    ).group(1)
    if installed == source.resolve():
        raise SystemExit("Refusing source-tree import; install a wheel or sdist first")
    if oglo.__version__ != expected or version("oglo") != expected:
        raise SystemExit("Installed metadata/module version differs from pyproject.toml")
    files = {}
    for path in sorted(source.rglob("*.py")):
        relative = path.relative_to(source)
        actual = installed / relative
        if not actual.is_file() or actual.read_bytes() != path.read_bytes():
            raise SystemExit(f"Installed module differs: {relative}")
        files[str(relative)] = hashlib.sha256(actual.read_bytes()).hexdigest()
    print(json.dumps({"version": expected, "installed_path": str(installed),
                      "module_sha256": files, "passed": True}, indent=2))


if __name__ == "__main__":
    main()
