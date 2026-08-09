"""Keep current user documentation aligned with the package and firmware contract."""

from __future__ import annotations

import re
from pathlib import Path

import oglo
from oglo._config import MIN_FIRMWARE


ROOT = Path(__file__).resolve().parent.parent
CURRENT_DOCS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *(ROOT / "docs").glob("*.md"),
)


def test_current_docs_match_the_release_and_firmware_floor():
    text = "\n".join(path.read_text() for path in CURRENT_DOCS)
    assert oglo.__version__ == "0.1.0rc3"
    assert MIN_FIRMWARE == (0, 9, 10)
    assert "0.1.0rc2" not in text
    assert "0.9.9" not in text
    assert "pair_id" not in text
    assert "allow_unpaired" not in text
    assert "allow-unpaired" not in text
    assert "0.9.11" in text
    assert "oglo-0.1.0rc3-py3-none-any.whl" in text
    assert "@v0.1.0rc3" in text


def test_current_markdown_relative_links_resolve():
    pattern = re.compile(r"\[[^]]*\]\(([^)]+)\)")
    missing = []
    for document in CURRENT_DOCS:
        for target in pattern.findall(document.read_text()):
            target = target.strip().split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not missing, "broken relative Markdown links:\n" + "\n".join(missing)
