#!/usr/bin/env bash
# Launch examples/camera_glove/collect.py from the `oglo` conda env without activating it.
#
#   scripts/collect.sh --pair --task "pick up a cup"
#   scripts/collect.sh --serial OGLO-R-00114 --task "smoke test"
# In the window: g record, h save, x discard, z calibrate, q quit (the foot switch types these).
#
# Defaults added in front of your arguments (yours win): --camera SC233 (the OVISION stereo camera,
# found by V4L2 name because /dev/video numbers change across reboots), --out <repo>/captures.
# The default captures/ gets a `*` .gitignore so recordings never show up in git status.
# Use another interpreter with OGLO_PYTHON=/path/to/python.
# If dialout was added with usermod but you have not re-logged in, this re-runs itself
# under `sg dialout` so the glove port opens anyway.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ensure_dialout "$ROOT/scripts/collect.sh" "$@"

extra=()
if ! printf '%s\n' "$@" | grep -qE '^--out(=|$)'; then
    mkdir -p "$ROOT/captures"
    [ -e "$ROOT/captures/.gitignore" ] || echo '*' > "$ROOT/captures/.gitignore"
    extra+=(--out "$ROOT/captures")
fi

exec "$PY" "$ROOT/examples/camera_glove/collect.py" --camera SC233 "${extra[@]}" "$@"
