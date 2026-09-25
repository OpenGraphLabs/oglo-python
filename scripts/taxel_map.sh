#!/usr/bin/env bash
# Live taxel map (examples/05_taxel_map.py) with the workstation's interpreter. Arguments pass through.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ensure_dialout "$ROOT/scripts/taxel_map.sh" "$@"
exec "$PY" "$ROOT/examples/05_taxel_map.py" "$@"
