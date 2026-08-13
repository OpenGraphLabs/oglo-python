from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    path = ROOT / ".github" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_policy = _load_script("release_policy")
workflow_policy = _load_script("check_workflows")


def _project_file(tmp_path: Path, version: str = "0.1.0rc3") -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "[build-system]\nrequires = ['hatchling']\n\n"
        f"[project]\nname = 'oglo'\nversion = '{version}'\n",
        encoding="utf-8",
    )
    return path


def _dist_files(tmp_path: Path, version: str = "0.1.0rc3") -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    metadata = f"Metadata-Version: 2.4\nName: oglo\nVersion: {version}\n\n".encode()

    wheel = dist / f"oglo-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"oglo-{version}.dist-info/METADATA", metadata)

    sdist = dist / f"oglo-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"oglo-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    return dist


def test_release_manifest_round_trip_and_outputs(tmp_path: Path) -> None:
    project = _project_file(tmp_path)
    dist = _dist_files(tmp_path)
    sha = "a" * 40
    manifest = release_policy.build_manifest(
        dist_dir=dist,
        project_file=project,
        repository="OpenGraphLabs/oglo-python",
        source_sha=sha,
    )

    assert manifest["tag"] == "v0.1.0rc3"
    assert manifest["source_sha"] == sha
    result = release_policy.verify_manifest(
        dist_dir=dist,
        project_file=project,
        repository="OpenGraphLabs/oglo-python",
        source_sha=sha,
        tag="v0.1.0rc3",
    )
    assert result["prerelease"] == "true"
    assert result["wheel_name"] == "oglo-0.1.0rc3-py3-none-any.whl"
    assert result["sdist_name"] == "oglo-0.1.0rc3.tar.gz"


def test_release_manifest_rejects_changed_distribution_bytes(tmp_path: Path) -> None:
    project = _project_file(tmp_path)
    dist = _dist_files(tmp_path)
    sha = "b" * 40
    release_policy.build_manifest(
        dist_dir=dist,
        project_file=project,
        repository="OpenGraphLabs/oglo-python",
        source_sha=sha,
    )
    wheel = dist / "oglo-0.1.0rc3-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")

    with pytest.raises(release_policy.PolicyError, match="bytes do not match"):
        release_policy.verify_manifest(
            dist_dir=dist,
            project_file=project,
            repository="OpenGraphLabs/oglo-python",
            source_sha=sha,
            tag="v0.1.0rc3",
        )


@pytest.mark.parametrize(
    ("tag", "sha"),
    [
        ("v0.1.0rc2", "a" * 40),
        ("0.1.0rc3", "a" * 40),
        ("v0.1.0rc3", "A" * 40),
        ("v0.1.0rc3", "abc"),
    ],
)
def test_source_policy_rejects_tag_or_sha_mismatch(tag: str, sha: str) -> None:
    with pytest.raises(release_policy.PolicyError):
        release_policy.validate_source(
            version="0.1.0rc3",
            tag=tag,
            repository="OpenGraphLabs/oglo-python",
            source_sha=sha,
        )


def test_manifest_rejects_forged_source_identity(tmp_path: Path) -> None:
    project = _project_file(tmp_path)
    dist = _dist_files(tmp_path)
    sha = "c" * 40
    release_policy.build_manifest(
        dist_dir=dist,
        project_file=project,
        repository="OpenGraphLabs/oglo-python",
        source_sha=sha,
    )
    manifest_path = dist / release_policy.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_sha"] = "d" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(release_policy.PolicyError, match="source SHA mismatch"):
        release_policy.verify_manifest(
            dist_dir=dist,
            project_file=project,
            repository="OpenGraphLabs/oglo-python",
            source_sha=sha,
            tag="v0.1.0rc3",
        )


def test_repository_workflows_satisfy_release_policy() -> None:
    workflow_policy.check_repository(ROOT)


def test_release_workflow_reconciles_partial_drafts_without_overwrite() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "--draft" in text
    assert "--draft=false" in text
    assert '--target "$SOURCE_SHA"' in text
    assert "download_and_verify_asset" in text
    assert 'test "$is_draft" = true' in text
    assert "gh release upload" in text
    assert "--clobber" not in text


def test_workflow_policy_rejects_unpinned_action(tmp_path: Path) -> None:
    workflow = tmp_path / "unsafe.yml"
    text = "steps:\n  - uses: actions/checkout@v7\n"
    with pytest.raises(workflow_policy.WorkflowPolicyError, match="full SHA"):
        workflow_policy.check_action_pins(workflow, text)


def test_workflow_policy_rejects_unpinned_reusable_workflow(tmp_path: Path) -> None:
    workflow = tmp_path / "unsafe-reusable.yml"
    text = "jobs:\n  inherited:\n    uses: owner/repository/.github/workflows/ci.yml@main\n"
    with pytest.raises(workflow_policy.WorkflowPolicyError, match="full SHA"):
        workflow_policy.check_action_pins(workflow, text)


def test_workflow_policy_rejects_pr_path_filter(tmp_path: Path) -> None:
    workflow = tmp_path / "ci.yml"
    text = """on:
  pull_request:
    paths:
      - src/**
name: Required CI gate
if: ${{ always() }}
name: Build and install distributions
name: Attest main-branch distributions
python-dist-${{ github.sha }}-${{ github.run_attempt }}
actions/attest-build-provenance@
id-token: write
attestations: write
"""
    with pytest.raises(workflow_policy.WorkflowPolicyError, match="path filters"):
        workflow_policy.check_ci(workflow, text)
