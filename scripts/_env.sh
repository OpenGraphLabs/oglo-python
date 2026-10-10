# Sourced by collect.sh / doctor.sh; not meant to be run on its own.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Workstation choices (interpreter, camera, codec, Hub repo, data folder) live in scripts/workstation.env,
# which git ignores; scripts/workstation.env.example documents the variables. Anything
# already set in the environment wins over the file.
WORKSTATION="${OGLO_WORKSTATION:-$ROOT/scripts/workstation.env}"
if [ -f "$WORKSTATION" ]; then
    _shell_python="${OGLO_PYTHON:-}"; _shell_camera="${OGLO_CAMERA:-}"
    _shell_codec="${OGLO_CODEC:-}"; _shell_repo="${OGLO_HF_REPO:-}"; _shell_data="${OGLO_DATA:-}"
    source "$WORKSTATION"
    if [ -n "$_shell_python" ]; then OGLO_PYTHON="$_shell_python"; fi
    if [ -n "$_shell_camera" ]; then OGLO_CAMERA="$_shell_camera"; fi
    if [ -n "$_shell_codec" ]; then OGLO_CODEC="$_shell_codec"; fi
    if [ -n "$_shell_repo" ]; then OGLO_HF_REPO="$_shell_repo"; fi
    if [ -n "$_shell_data" ]; then OGLO_DATA="$_shell_data"; fi
fi
PY="${OGLO_PYTHON:-$(command -v python3 || true)}"
[ -n "$PY" ] && [ -x "$PY" ] || { echo "python not found: '${PY}'  (set OGLO_PYTHON in $WORKSTATION)" >&2; exit 1; }
# The dataset lives beside the checkouts, not inside one: <project>/hf-data, where <project> is
# the folder that holds the main checkout, so every worktree records into, and uploads and
# downloads from, the same tree. dataset.py works it out (`dataset.py data-dir`), for itself too.
if [ -z "${OGLO_DATA:-}" ]; then
    OGLO_DATA="$("$PY" "$ROOT/examples/camera_glove/dataset.py" data-dir)"
fi
export OGLO_PYTHON OGLO_CAMERA OGLO_CODEC OGLO_HF_REPO OGLO_DATA
# Import oglo from this checkout's src/ even when OGLO_PYTHON's environment has the package
# installed (editable) from another checkout or worktree, so one environment serves them all.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# POSIX single-quote each argument so `sg -c` (which runs /bin/sh) sees it unchanged.
sh_quote() {
    local out="" a
    for a in "$@"; do out+=" '${a//\'/\'\\\'\'}'"; done
    printf '%s' "$out"
}

# The glove is a dialout-owned serial port. `usermod -aG dialout` only reaches new
# login sessions; until the user logs out and back in, re-run the caller under
# `sg dialout`, which needs no password once /etc/group lists the user.
ensure_dialout() {
    id -nG | tr ' ' '\n' | grep -qx dialout && return 0
    if getent group dialout | cut -d: -f4 | tr ',' '\n' | grep -qx "$(id -un)"; then
        echo "note: dialout is not active in this login session yet; re-running under 'sg dialout'" >&2
        echo "      (log out and back in once to make it permanent)" >&2
        exec sg dialout -c "exec$(sh_quote "$@")"
    fi
    echo "warning: you are not in group 'dialout'; the glove serial port will be 'Permission denied'." >&2
    echo "         fix once:  sudo usermod -aG dialout \$USER   then log out and back in." >&2
}
