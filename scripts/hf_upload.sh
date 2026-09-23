#!/usr/bin/env bash
# Index captures/ and push it to the private Hugging Face dataset repo (ntumars-opengraph/oglo-tactile-ego).
#
#   scripts/hf_upload.sh              # rewrite episodes.jsonl + README.md, upload what is new
#   scripts/hf_upload.sh --dry-run    # only show what would be sent
#   scripts/hf_upload.sh --repo user/other-name --message "batch 3"
#
# Uses the `hf` CLI (~/.local/bin/hf); the logged-in token must have write access to the
# ntumars-opengraph org, not just the personal namespace. Set HF_CLI to use another one.
# _discarded/ and _failed/ folders never leave this machine.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec "$PY" "$ROOT/examples/camera_glove/dataset.py" upload --out "$ROOT/captures" "$@"
