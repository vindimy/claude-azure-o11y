# shellcheck shell=bash
# Shared by deploy.sh, destroy.sh and build-image.sh. Works on macOS's bash 3.2 (no `source <(...)`,
# no associative arrays, no GNU-only regex).

# load_params FILE: export KEY=VALUE lines; `#` starts a comment; blank lines are ignored.
load_params() {
  local line key
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%%#*}
    line="${line%"${line##*[![:space:]]}"}"
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    key="${key#"${key%%[![:space:]]*}"}"
    export "$key=${line#*=}"
  done <"$1"
}

# flag_to_var --some-flag -> SOME_FLAG
flag_to_var() { echo "${1#--}" | tr '[:lower:]-' '[:upper:]_'; }
