#!/usr/bin/env python3
"""Build and verify the immutable SDK release artifact contract."""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import re
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable


PACKAGE_NAME = "oglo"
MANIFEST_NAME = "release-manifest.json"
MANIFEST_SCHEMA = 1
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,2}"
    r"(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"
)


class PolicyError(ValueError):
    """A release input failed closed against the repository policy."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def project_version(project_file: Path) -> str:
    """Read the sole literal ``project.version`` without a TOML dependency."""

    in_project = False
    versions: list[str] = []
    for raw_line in project_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project:
            continue
        match = re.fullmatch(r"version\s*=\s*(['\"])([^'\"]+)\1\s*(?:#.*)?", line)
        if match:
            versions.append(match.group(2))

    _require(len(versions) == 1, "pyproject.toml must contain one literal [project].version")
    version = versions[0]
    _require(bool(VERSION_RE.fullmatch(version)), f"unsupported release version: {version!r}")
    return version


def expected_tag(version: str) -> str:
    _require(bool(VERSION_RE.fullmatch(version)), f"unsupported release version: {version!r}")
    return f"v{version}"


def is_prerelease(version: str) -> bool:
    expected_tag(version)
    return bool(re.search(r"(?:a|b|rc)[0-9]+|\.dev[0-9]+", version))


def validate_source(*, version: str, tag: str, repository: str, source_sha: str) -> None:
    _require(tag == expected_tag(version), f"tag {tag!r} does not match version {version!r}")
    _require(bool(REPOSITORY_RE.fullmatch(repository)), "repository must be owner/name")
    _require(bool(SHA_RE.fullmatch(source_sha)), "source SHA must be 40 lowercase hex characters")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_name(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and "" not in path.parts


def _metadata_fields(data: bytes, source: str) -> tuple[str, str]:
    try:
        message = email.parser.BytesParser().parsebytes(data)
    except Exception as exc:  # pragma: no cover - defensive parser boundary
        raise PolicyError(f"cannot parse package metadata from {source}: {exc}") from exc
    name = message.get("Name")
    version = message.get("Version")
    _require(bool(name), f"missing Name metadata in {source}")
    _require(bool(version), f"missing Version metadata in {source}")
    return str(name), str(version)


def _verify_wheel(path: Path, version: str) -> None:
    normalized_version = re.sub(r"[^A-Za-z0-9.]+", "_", version)
    expected_name = f"{PACKAGE_NAME}-{normalized_version}-py3-none-any.whl"
    _require(path.name == expected_name, f"unexpected wheel filename: {path.name!r}")

    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            _require(_safe_archive_name(item.filename), f"unsafe wheel member: {item.filename!r}")
            mode = (item.external_attr >> 16) & 0o170000
            _require(mode != stat.S_IFLNK, f"wheel contains symlink: {item.filename!r}")
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        _require(len(metadata_names) == 1, "wheel must contain exactly one METADATA file")
        name, embedded_version = _metadata_fields(
            archive.read(metadata_names[0]), f"{path.name}:{metadata_names[0]}"
        )
    _require(name == PACKAGE_NAME, f"wheel package name is {name!r}, expected {PACKAGE_NAME!r}")
    _require(embedded_version == version, "wheel metadata version does not match pyproject.toml")


def _verify_sdist(path: Path, version: str) -> None:
    expected_name = f"{PACKAGE_NAME}-{version}.tar.gz"
    _require(path.name == expected_name, f"unexpected sdist filename: {path.name!r}")

    with tarfile.open(path, "r:gz") as archive:
        metadata_members = []
        for member in archive.getmembers():
            _require(_safe_archive_name(member.name), f"unsafe sdist member: {member.name!r}")
            _require(
                not (member.issym() or member.islnk() or member.isdev() or member.isfifo()),
                f"sdist contains unsafe special member: {member.name!r}",
            )
            if member.name.endswith("/PKG-INFO"):
                metadata_members.append(member)
        _require(len(metadata_members) == 1, "sdist must contain exactly one PKG-INFO file")
        extracted = archive.extractfile(metadata_members[0])
        _require(extracted is not None, "cannot read sdist PKG-INFO")
        name, embedded_version = _metadata_fields(
            extracted.read(), f"{path.name}:{metadata_members[0].name}"
        )
    _require(name == PACKAGE_NAME, f"sdist package name is {name!r}, expected {PACKAGE_NAME!r}")
    _require(embedded_version == version, "sdist metadata version does not match pyproject.toml")


def _distribution_paths(dist_dir: Path) -> tuple[Path, Path]:
    _require(dist_dir.is_dir(), f"distribution directory does not exist: {dist_dir}")
    entries = sorted(dist_dir.iterdir(), key=lambda path: path.name)
    _require(all(path.is_file() and not path.is_symlink() for path in entries), "dist may contain only regular files")
    payloads = [path for path in entries if path.name != MANIFEST_NAME]
    wheels = [path for path in payloads if path.name.endswith(".whl")]
    sdists = [path for path in payloads if path.name.endswith(".tar.gz")]
    _require(len(payloads) == 2, "dist must contain exactly one wheel and one sdist")
    _require(len(wheels) == 1, "dist must contain exactly one wheel")
    _require(len(sdists) == 1, "dist must contain exactly one sdist")
    return wheels[0], sdists[0]


def inspect_distributions(dist_dir: Path, version: str) -> dict[str, dict[str, Any]]:
    wheel, sdist = _distribution_paths(dist_dir)
    _verify_wheel(wheel, version)
    _verify_sdist(sdist, version)
    return {
        "wheel": {"name": wheel.name, "sha256": _sha256(wheel), "size": wheel.stat().st_size},
        "sdist": {"name": sdist.name, "sha256": _sha256(sdist), "size": sdist.stat().st_size},
    }


def build_manifest(
    *, dist_dir: Path, project_file: Path, repository: str, source_sha: str
) -> dict[str, Any]:
    version = project_version(project_file)
    validate_source(
        version=version,
        tag=expected_tag(version),
        repository=repository,
        source_sha=source_sha,
    )
    manifest_path = dist_dir / MANIFEST_NAME
    _require(not manifest_path.exists(), f"refusing to overwrite {manifest_path}")
    files = inspect_distributions(dist_dir, version)
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "package": PACKAGE_NAME,
        "version": version,
        "tag": expected_tag(version),
        "repository": repository,
        "source_sha": source_sha,
        "files": [
            {"kind": kind, **files[kind]}
            for kind in ("wheel", "sdist")
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_manifest(
    *,
    dist_dir: Path,
    project_file: Path,
    repository: str,
    source_sha: str,
    tag: str,
) -> dict[str, Any]:
    version = project_version(project_file)
    validate_source(version=version, tag=tag, repository=repository, source_sha=source_sha)
    manifest_path = dist_dir / MANIFEST_NAME
    _require(manifest_path.is_file() and not manifest_path.is_symlink(), "release manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"invalid release manifest: {exc}") from exc

    expected_keys = {
        "schema_version",
        "package",
        "version",
        "tag",
        "repository",
        "source_sha",
        "files",
    }
    _require(isinstance(manifest, dict), "release manifest must be an object")
    _require(set(manifest) == expected_keys, "release manifest fields do not match the schema")
    _require(manifest["schema_version"] == MANIFEST_SCHEMA, "unsupported release manifest schema")
    _require(manifest["package"] == PACKAGE_NAME, "release manifest package mismatch")
    _require(manifest["version"] == version, "release manifest version mismatch")
    _require(manifest["tag"] == tag, "release manifest tag mismatch")
    _require(manifest["repository"] == repository, "release manifest repository mismatch")
    _require(manifest["source_sha"] == source_sha, "release manifest source SHA mismatch")

    actual_files = inspect_distributions(dist_dir, version)
    manifest_files = manifest["files"]
    _require(isinstance(manifest_files, list) and len(manifest_files) == 2, "manifest must list two files")
    by_kind: dict[str, dict[str, Any]] = {}
    for entry in manifest_files:
        _require(isinstance(entry, dict), "manifest file entry must be an object")
        _require(set(entry) == {"kind", "name", "sha256", "size"}, "invalid manifest file fields")
        kind = entry["kind"]
        _require(kind in {"wheel", "sdist"} and kind not in by_kind, "invalid manifest file kind")
        by_kind[kind] = entry
    _require(set(by_kind) == {"wheel", "sdist"}, "manifest must list wheel and sdist")
    for kind, actual in actual_files.items():
        _require(by_kind[kind] == {"kind": kind, **actual}, f"{kind} bytes do not match the CI manifest")

    return {
        "version": version,
        "tag": tag,
        "source_sha": source_sha,
        "prerelease": "true" if is_prerelease(version) else "false",
        "wheel_name": actual_files["wheel"]["name"],
        "wheel_sha256": actual_files["wheel"]["sha256"],
        "sdist_name": actual_files["sdist"]["name"],
        "sdist_sha256": actual_files["sdist"]["sha256"],
    }


def _write_github_output(path: Path, values: Iterable[tuple[str, str]]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values:
            _require("\n" not in key and "\n" not in value, "GitHub output values must be single-line")
            output.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-manifest")
    build.add_argument("--dist", type=Path, required=True)
    build.add_argument("--project-file", type=Path, required=True)
    build.add_argument("--repository", required=True)
    build.add_argument("--source-sha", required=True)

    source = subparsers.add_parser("validate-source")
    source.add_argument("--project-file", type=Path, required=True)
    source.add_argument("--repository", required=True)
    source.add_argument("--source-sha", required=True)
    source.add_argument("--tag", required=True)
    source.add_argument("--github-output", type=Path, required=True)

    verify = subparsers.add_parser("verify-dist")
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--project-file", type=Path, required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--github-output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build-manifest":
            manifest = build_manifest(
                dist_dir=args.dist,
                project_file=args.project_file,
                repository=args.repository,
                source_sha=args.source_sha,
            )
            print(json.dumps(manifest, sort_keys=True))
        elif args.command == "validate-source":
            version = project_version(args.project_file)
            validate_source(
                version=version,
                tag=args.tag,
                repository=args.repository,
                source_sha=args.source_sha,
            )
            _write_github_output(
                args.github_output,
                (
                    ("version", version),
                    ("tag", args.tag),
                    ("source_sha", args.source_sha),
                    ("prerelease", "true" if is_prerelease(version) else "false"),
                ),
            )
        elif args.command == "verify-dist":
            result = verify_manifest(
                dist_dir=args.dist,
                project_file=args.project_file,
                repository=args.repository,
                source_sha=args.source_sha,
                tag=args.tag,
            )
            _write_github_output(args.github_output, result.items())
            print(json.dumps(result, sort_keys=True))
        else:  # pragma: no cover - argparse enforces the command set
            raise AssertionError(args.command)
    except PolicyError as exc:
        raise SystemExit(f"release policy rejected input: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
