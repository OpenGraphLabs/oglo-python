#!/usr/bin/env bash
# Index $OGLO_DATA (default <project>/hf-data, see _env.sh) and push it to the private Hugging Face dataset repo $OGLO_HF_REPO
# (scripts/workstation.env; --repo overrides).
#
#   scripts/hf_upload.sh              # rewrite episodes.jsonl + README.md, upload what is new
#   scripts/hf_upload.sh --dry-run    # only show what would be sent
#   scripts/hf_upload.sh --repo user/other-name --message "batch 3"
#
# Uses the `hf` CLI, 1.0 or newer (on PATH or ~/.local/bin/hf); the logged-in token must have
# write access to the repo's organization, not just the personal namespace. Set HF_CLI to use
# another one. The repo must be private (an existing public one is refused; hf's --private only
# applies to a repo it creates). Only the indexed episodes, gloves/ and the two root files are
# sent, the index last; _discarded/ and _failed/ never leave this machine. The upload refuses while
# any folder under a task is not a publishable episode, while an episode's folder on the Hub holds
# another recording, and while a checkout's captures/ (the default folder before hf-data) still
# holds episodes.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec "$PY" "$ROOT/examples/camera_glove/dataset.py" upload --out "$OGLO_DATA" "$@"
