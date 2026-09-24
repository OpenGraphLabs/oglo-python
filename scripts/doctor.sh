#!/usr/bin/env bash
# `oglo doctor` with the workstation's interpreter (scripts/workstation.env). Extra arguments pass through.
# Re-runs itself under `sg dialout` when the group was added but the session predates it.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ensure_dialout "$ROOT/scripts/doctor.sh" "$@"
# Through the interpreter rather than a sibling `oglo` script: OGLO_PYTHON may name an
# interpreter whose bin/ does not carry the console script (a bare venv python, uv run).
exec "$PY" -c 'import sys; from oglo.cli import main; sys.exit(main(["doctor", *sys.argv[1:]]))' "$@"
