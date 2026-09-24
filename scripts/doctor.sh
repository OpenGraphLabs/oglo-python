#!/usr/bin/env bash
# `oglo doctor` with the workstation's interpreter (scripts/workstation.env). Extra arguments pass through.
# Re-runs itself under `sg dialout` when the group was added but the session predates it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ensure_dialout "$ROOT/scripts/doctor.sh" "$@"
exec "$(dirname "$PY")/oglo" doctor "$@"
