# Sourced by collect.sh / doctor.sh; not meant to be run on its own.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${OGLO_PYTHON:-$HOME/miniforge3/envs/oglo/bin/python}"
[ -x "$PY" ] || { echo "python not found: $PY  (set OGLO_PYTHON)" >&2; exit 1; }

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
