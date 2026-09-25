#!/usr/bin/env bash
# Pull the private Hugging Face dataset repo $OGLO_HF_REPO into $OGLO_DATA (default
# <project>/hf-data, see _env.sh), the same folder collect.sh records into and hf_upload.sh sends,
# then rebuild episodes.jsonl and README.md there from the manifests (`dataset.py download`).
#
#   scripts/hf_download.sh                          # everything on the Hub
#   scripts/hf_download.sh --include "pickupacup/*" # one task; other `hf download` options pass through
#
# Files already here with the same content are skipped; a local file that differs from the Hub is
# overwritten, so upload before downloading on a machine that records. The Hub's episodes.jsonl and
# README.md are not fetched: both are derived from the manifests, and the rebuilt ones list exactly
# the episodes this folder holds. Needs hf 1.0 or newer. hf keeps its bookkeeping in
# $OGLO_DATA/.cache/, which dataset.py ignores.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec "$PY" "$ROOT/examples/camera_glove/dataset.py" download --out "$OGLO_DATA" "$@"
