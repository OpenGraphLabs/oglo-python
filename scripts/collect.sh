#!/usr/bin/env bash
# Launch examples/camera_glove/collect.py with the interpreter and defaults of this workstation.
#
#   scripts/collect.sh --pair --task "pick up a cup"
#   scripts/collect.sh --serial OGLO-R-00114 --task "smoke test"
# In the window: g record, h save, x discard, z calibrate, q quit (the foot switch types these).
#
# Defaults added in front of your arguments (yours win): --camera $OGLO_CAMERA (part of the
# camera's V4L2 name, because /dev/video numbers change across reboots), --codec $OGLO_CODEC
# for the OpenCV backend, --out <repo>/captures. Both variables come from scripts/workstation.env
# (see workstation.env.example); without them the camera is index 0 and the codec collect.py's default.
# The default captures/ gets a `*` .gitignore so recordings never show up in git status.
# If dialout was added with usermod but you have not re-logged in, this re-runs itself
# under `sg dialout` so the glove port opens anyway.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ensure_dialout "$ROOT/scripts/collect.sh" "$@"

extra=(--camera "${OGLO_CAMERA:-0}")
[ -n "${OGLO_CODEC:-}" ] && extra+=(--codec "$OGLO_CODEC")
if ! printf '%s\n' "$@" | grep -qE '^--out(=|$)'; then
    mkdir -p "$ROOT/captures"
    [ -e "$ROOT/captures/.gitignore" ] || echo '*' > "$ROOT/captures/.gitignore"
    extra+=(--out "$ROOT/captures")
fi

exec "$PY" "$ROOT/examples/camera_glove/collect.py" "${extra[@]}" "$@"
