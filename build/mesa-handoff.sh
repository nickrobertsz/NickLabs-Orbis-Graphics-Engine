#!/usr/bin/env bash
# Shared Core/GE Mesa handoff. Source inside a disposable Arch build container.
# The caller must explicitly opt in via ORBIS_DISPOSABLE_BUILD=1.
orbis_mesa_handoff() {
  local archive="${1:?Mesa package path required}" expected="${2:?Expected package/version required}"
  if [[ "${ORBIS_DISPOSABLE_BUILD:-}" != 1 || ${EUID:-$(id -u)} != 0 ]] ||
     { [[ ! -e /run/.containerenv ]] && [[ ! -e /.dockerenv ]]; }; then
    printf 'ERROR: Mesa handoff requires an explicitly opted-in disposable root container\n' >&2
    return 10
  fi
  [[ -s "$archive" ]] || { printf 'ERROR: missing Mesa archive: %s\n' "$archive" >&2; return 9; }
  local built installed actual p
  built="$(pacman -Qp -- "$archive")" || return 9
  [[ "$built" == "$expected" ]] || {
    printf 'ERROR: Mesa archive metadata mismatch: got %s, expected %s\n' "$built" "$expected" >&2
    return 9
  }
  # pacman -Qq <virtual provider> may succeed, but pacman -R requires an
  # actual installed package. Query actual package names without arguments.
  installed="$(pacman -Qq)" || return 9
  local -a conflicts=()
  while IFS= read -r p; do
    case "$p" in
      mesa|vulkan-radeon|vulkan-mesa-device-select|vulkan-mesa-layers|vulkan-swrast|\
      mesa-ps4|libva-mesa-driver|mesa-orbis-ps4-experimental)
        conflicts+=("$p") ;;
    esac
  done <<< "$installed"
  if (( ${#conflicts[@]} )); then
    printf 'Removing actual installed Mesa conflicts from disposable builder: %s\n' "${conflicts[*]}"
    pacman -Rdd --noconfirm -- "${conflicts[@]}" || return 9
  fi
  pacman -U --noconfirm -- "$archive" || return 9
  actual="$(pacman -Q mesa-orbis-ps4)" || return 9
  [[ "$actual" == "$expected" ]] || {
    printf 'ERROR: installed Mesa is %s, expected %s\n' "$actual" "$expected" >&2
    return 9
  }
  # A local database consistency check is helpful, but avoid ignoring a bad
  # custom Mesa install. The surrounding release gates validate full closure.
  printf 'Verified container-local Mesa: %s\n' "$actual"
}
