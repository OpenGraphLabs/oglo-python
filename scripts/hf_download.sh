#!/usr/bin/env bash
# Pull the private Hugging Face dataset repo $OGLO_HF_REPO into $OGLO_DATA (default
# <project>/hf-data, see _env.sh), the same folder collect.sh records into and hf_upload.sh sends.
#
#   scripts/hf_download.sh                          # everything on the Hub
#   scripts/hf_download.sh --include "pickupacup/*" # one task; other `hf download` options pass through
#
# Files already here with the same content are skipped; a local file that differs from the Hub is
# overwritten, so upload before downloading on a machine that records. episodes.jsonl and README.md
# are not fetched: dataset.py derives both from the manifests, and refuses to replace a card that a
# different dataset.py version wrote, so run `dataset.py index --out "$OGLO_DATA"` (or hf_upload.sh)
# afterwards. hf keeps its bookkeeping in $OGLO_DATA/.cache/, which dataset.py ignores.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
[ -n "${OGLO_HF_REPO:-}" ] || { echo "set OGLO_HF_REPO in $WORKSTATION" >&2; exit 1; }
HF="${HF_CLI:-$(command -v hf || echo "$HOME/.local/bin/hf")}"
mkdir -p "$OGLO_DATA"
exec "$HF" download "$OGLO_HF_REPO" --repo-type dataset --local-dir "$OGLO_DATA" \
    --exclude episodes.jsonl --exclude README.md "$@"
