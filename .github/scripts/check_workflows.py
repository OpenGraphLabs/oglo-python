#!/usr/bin/env python3
"""Fail closed when GitHub workflows drift from the release security policy."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)(?:\s+#.*)?$")


class WorkflowPolicyError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkflowPolicyError(message)


def check_action_pins(path: Path, text: str) -> None:
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = USES_RE.match(line)
        if not match:
            continue
        target = match.group(1)
        _require("@" in target, f"{path}:{line_number}: action has no ref")
        action, ref = target.rsplit("@", 1)
        _require(not action.startswith("./"), f"{path}:{line_number}: local actions are not allowed")
        _require(bool(FULL_SHA_RE.fullmatch(ref)), f"{path}:{line_number}: action is not pinned to a full SHA")


def check_ci(path: Path, text: str) -> None:
    _require(re.search(r"(?m)^\s{2}pull_request:\s*$", text) is not None, "CI must run for every pull request")
    _require("pull_request_target:" not in text, "CI must not use pull_request_target")
    _require(re.search(r"(?m)^\s+paths(?:-ignore)?:", text) is None, "CI triggers must not use path filters")
    _require("name: Required CI gate" in text, "CI must expose one stable required gate")
    _require("if: ${{ always() }}" in text, "the required gate must always be created")
    _require("name: Build and install distributions" in text, "the existing package check must remain during migration")
    _require("name: Attest main-branch distributions" in text, "main distributions must be attested")
    _require("python-dist-${{ github.sha }}-${{ github.run_attempt }}" in text, "artifacts must bind SHA and run attempt")
    _require("actions/attest-build-provenance@" in text, "CI must create GitHub provenance")
    _require("id-token: write" in text and "attestations: write" in text, "provenance job permissions are incomplete")


def check_release(path: Path, text: str) -> None:
    _require(re.search(r"(?ms)^on:\s*\n\s{2}push:\s*\n\s{4}tags:", text) is not None, "release must be tag-push-only")
    _require("workflow_dispatch:" not in text, "release must not bypass tag creation with workflow_dispatch")
    _require(re.search(r"(?m)^permissions: \{\}\s*$", text) is not None, "release default permissions must be empty")
    _require("name: sdk-github-release" in text, "publish must use the protected release environment")
    _require("artifact-ids:" in text and "run-id:" in text, "release must select an exact CI artifact")
    _require("head_sha" in text and "event=push" in text, "release must select exact main push CI")
    _require("--signer-workflow" in text, "release must pin the provenance signer workflow")
    _require("--signer-digest" in text, "release must pin the signer workflow SHA")
    _require("--source-digest" in text and "--source-ref refs/heads/main" in text, "release must pin source provenance")
    _require("gh release create" in text and "--verify-tag" in text, "release creation must reuse the verified tag")
    _require(text.count("contents: write") == 1, "only the publish job may write repository contents")
    _require("id-token: write" not in text, "release does not need OIDC write permission")

    lower = text.lower()
    banned = (
        "pypa/gh-action-pypi-publish",
        "twine upload",
        "upload.pypi.org",
        "pypi_api_token",
        "pypi-api-token",
        "__token__",
    )
    for term in banned:
        _require(term not in lower, f"PyPI publication is forbidden in release workflow: {term}")


def check_repository(root: Path) -> None:
    workflows = root / ".github" / "workflows"
    paths = sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml")))
    _require(bool(paths), "no workflows found")
    for path in paths:
        check_action_pins(path, path.read_text(encoding="utf-8"))
    check_ci(workflows / "ci.yml", (workflows / "ci.yml").read_text(encoding="utf-8"))
    check_release(workflows / "release.yml", (workflows / "release.yml").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        check_repository(args.root.resolve())
    except WorkflowPolicyError as exc:
        raise SystemExit(f"workflow policy rejected repository: {exc}") from exc
    print("workflow policy: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
