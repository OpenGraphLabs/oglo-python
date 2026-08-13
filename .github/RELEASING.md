# GitHub release procedure

The public SDK is released only from the canonical
[`OpenGraphLabs/oglo-python`](https://github.com/OpenGraphLabs/oglo-python)
repository. GitHub Releases are the only publication target. This repository does
not publish to PyPI, and no workflow or release operator should upload a package
to PyPI.

## One-time repository settings

Apply these controls before creating the first tag handled by
`.github/workflows/release.yml`:

1. Keep the existing `main` required checks while this workflow change is being
   reviewed. After it lands and a pull request has produced the new check, replace
   the seven matrix-specific required contexts with the single stable
   `Required CI gate` context. Keep strict up-to-date branches, admin enforcement,
   linear history, and required conversation resolution enabled.
2. Create an environment named `sdk-github-release`. Limit it to tags matching
   `v*`. Add an owner/release-manager approval rule when a second approver is
   available. The publish job is the only job with `contents: write` and waits on
   this environment.
3. Add a tag ruleset for `refs/tags/v*`: restrict tag creation to release managers
   and block updates and deletion. A released tag is immutable and is never moved
   to another commit.
4. In Actions settings, require full-length action SHA pins. If organization policy
   permits it, allow GitHub-authored actions only. The checked-in policy test also
   rejects movable action tags.

No PyPI token, API key, cloud signing key, or long-lived GitHub token is required.
The workflow uses the repository-scoped `GITHUB_TOKEN` with per-job permissions.

## Release a version

1. Merge the version and changelog change through a pull request. The literal
   `[project].version` in `pyproject.toml` is the release version.
2. Wait for the `main` push run of `CI` at that exact merge SHA to succeed. That run
   builds one wheel and one sdist, records their hashes and source SHA in an
   internal manifest, uploads one immutable artifact, and creates GitHub build
   provenance. The artifact is retained for 30 days.
3. Create one annotated tag whose name is exactly `v` plus the package version.
   Tag the already-tested `main` SHA, not a local rebuild or a different checkout:

   ```bash
   git fetch origin main --tags
   git switch main
   git pull --ff-only origin main
   version="0.1.0rc4" # example; must equal pyproject.toml
   sha="$(git rev-parse origin/main)"
   git tag -a "v${version}" "$sha" -m "OGLO Python SDK ${version}"
   git push origin "refs/tags/v${version}"
   ```

4. The tag starts `GitHub Release`. Its read-only verification job fails unless:

   - the tag is annotated, resolves to a commit contained in `main`, and exactly
     matches `[project].version`;
   - a successful `push` run of `.github/workflows/ci.yml` exists for that exact
     commit and its stable gate and provenance jobs both passed;
   - the exact, unexpired artifact ID from that CI run contains one wheel, one
     sdist, and the matching source/hash manifest;
   - both public packages have GitHub provenance signed by the CI workflow at that
     exact `main` SHA.

5. Approve the `sdk-github-release` environment if it has a reviewer rule. The
   write-scoped job downloads the same artifact ID again, reconciles its hashes,
   re-resolves the tag through the GitHub API, re-verifies provenance, and creates
   a GitHub Release containing the wheel, sdist, and `SHA256SUMS`. It never rebuilds
   and never publishes to PyPI.

If the CI artifact expired, use **Re-run all jobs** on the original successful
`main` push run before creating the tag. The artifact name includes both source SHA
and run attempt, so reruns cannot silently reuse an older attempt's bytes.

## Independent verification

Download a release and verify both the published checksums and GitHub provenance:

```bash
tag="v0.1.0rc4" # example
mkdir -p "/tmp/oglo-${tag}"
gh release download "$tag" \
  --repo OpenGraphLabs/oglo-python \
  --dir "/tmp/oglo-${tag}"
(cd "/tmp/oglo-${tag}" && shasum -a 256 --check SHA256SUMS)
for artifact in "/tmp/oglo-${tag}"/*.whl "/tmp/oglo-${tag}"/*.tar.gz; do
  gh attestation verify "$artifact" \
    --repo OpenGraphLabs/oglo-python \
    --signer-workflow OpenGraphLabs/oglo-python/.github/workflows/ci.yml \
    --source-ref refs/heads/main \
    --deny-self-hosted-runners
done
```

If a published package is bad, do not replace its assets or move its tag. Preserve
the audit trail, fix the issue on `main`, increment the version, and release a new
tag.
