#!/usr/bin/env bash
# NickLabs Orbis Graphics Engine — package build, dependency closure and mini-root. Runs as root INSIDE
# the graphics build container (build/Containerfile.gfx); build/build-pack.sh starts it.
#
#   build/in-container-build.sh --staging /src/out/pack [--experimental] [--orbis-repo /orbis/out/repo]
#                               [--base-packages /orbis/out/rootfs/packages.txt] [--keys /orbis/out/keys]
set -euo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)"
staging=""; experimental=0; orbis_repo=""; base_pk=""; keys=""
while [ $# -gt 0 ]; do
  case "$1" in
    --staging) staging="$2"; shift 2 ;;
    --experimental) experimental=1; shift ;;
    --orbis-repo) orbis_repo="$2"; shift 2 ;;
    --base-packages) base_pk="$2"; shift 2 ;;
    --keys) keys="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$staging" ] || { echo "need --staging" >&2; exit 2; }
snapshot="$(python3 -c "import json; print(json.load(open('$R/manifest/sources.json'))['arch_snapshot']['date'])")"
repo="$staging/repo"; mkdir -p "$repo" "$staging/logs"
step() { echo; echo "=== $*"; }
# makepkg runs as the unprivileged 'builder' on a private copy of the tree, so the host checkout is
# never chowned; packages land in /build/pkg and are copied into the staging repo.
rm -rf /build && mkdir -p /build/pkg /build/src
tar -C "$R" --exclude=./out --exclude=./.git -cf - . | tar -C /build/src -xf -
mkdir -p /build/src/out && [ ! -d "$R/out/kernel-ge" ] || cp -a "$R/out/kernel-ge" /build/src/out/
chown -R builder /build
B=/build/src

mk() {  # mk <recipe dir relative to the tree> <log name> [deps|nodeps]
  local mode=--syncdeps; [ "${3:-deps}" = nodeps ] && mode=--nodeps
  echo "    log: $staging/logs/$2.log"
  if ( cd "$B/$1" && sudo -u builder makepkg $mode --noconfirm --cleanbuild --force PKGDEST=/build/pkg ) \
       > "$staging/logs/$2.log" 2>&1; then
    cp /build/pkg/*.pkg.tar.zst "$repo/" && rm -f /build/pkg/*.pkg.tar.zst
  else
    tail -n 25 "$staging/logs/$2.log" >&2; return 1
  fi
}

step "libdrm requirement (Mesa 26.2.5 needs libdrm_amdgpu >= 2.4.133)"
v="$(pacman -Si libdrm | awk '/^Version/ {print $3; exit}')"
python3 - "$v" <<'PY'
import sys
v = sys.argv[1].split("-")[0]
parts = tuple(int(x) for x in v.split(".")[:3])
print("libdrm in the Arch snapshot:", v)
sys.exit(0 if parts >= (2, 4, 133) else "libdrm too old for Mesa 26.2.5")
PY

step "Mesa stable 26.2.5 (mesa-orbis-ps4 + opencl-mesa-orbis-ps4), ~1-2 h"
mk mesa/stable mesa-stable
step "32-bit Mesa (build deps preinstalled by Containerfile.gfx; mesa-orbis-ps4 is a runtime dep only)"
mk mesa/lib32 mesa-lib32 nodeps
step "orbis-graphics tools + meta package"
mk packages/orbis-graphics orbis-graphics nodeps
mk packages/orbis-graphics-engine orbis-graphics-engine nodeps
mk packages/orbis-graphics-pack orbis-graphics-pack nodeps

exp_state=NOT_BUILT; exp_reason="not requested (build-pack.sh --experimental)"
if [ "$experimental" = 1 ]; then
  step "EXPERIMENTAL Mesa main snapshot"
  if mk mesa/experimental mesa-experimental; then
    exp_state=BUILT; exp_reason="built from the pinned main commit"
  else
    exp_state=BLOCKED
    exp_reason="$(grep -o 'ORBIS-EXPERIMENTAL-BLOCKED:.*' "$staging/logs/mesa-experimental.log" | head -1)"
    [ -n "$exp_reason" ] || exp_reason="build failed; see logs/mesa-experimental.log"
    rm -f "$repo"/mesa-orbis-ps4-experimental-*.pkg.tar.zst
  fi
fi
printf '%s\n%s\n' "$exp_state" "$exp_reason" > "$staging/experimental-state.txt"

if grep -qv '^[[:space:]]*\(#\|$\)' "$R/kernel/patches/series" 2>/dev/null; then
  step "graphics kernel package (kernel/patches/series lists reviewed patches)"
  [ -f "$R/out/kernel-ge/build-manifest.json" ] || {
    echo "kernel/patches/series is not empty but out/kernel-ge is missing: run kernel/build-ge-kernel.sh first" >&2; exit 5; }
  mk kernel/package linux-orbis-ps4-ge nodeps
else
  step "kernel: no reviewed graphics patches in kernel/patches/series; the Orbis 0.2.0 kernel is kept"
fi

step "dependency closure from the Arch snapshot $snapshot"
tmpdb="$(mktemp -d)"; conf="$(mktemp)"
cat > "$conf" <<CONF
[options]
Architecture = x86_64
SigLevel = Required DatabaseOptional
LocalFileSigLevel = Optional
[orbis-graphics-build]
SigLevel = Optional TrustAll
Server = file://$repo
[core]
Server = https://archive.archlinux.org/repos/$snapshot/\$repo/os/\$arch
[extra]
Server = https://archive.archlinux.org/repos/$snapshot/\$repo/os/\$arch
[multilib]
Server = https://archive.archlinux.org/repos/$snapshot/\$repo/os/\$arch
CONF
repo-add -q -n "$repo/orbis-graphics-build.db.tar.zst" "$repo"/*.pkg.tar.zst
if [ -n "$orbis_repo" ] && [ -f "$orbis_repo/orbis.db.tar.zst" ]; then
  # the Orbis 0.2.0 repository: its packages are already on every Orbis image, so they are resolved
  # from here and left out of the pack
  sed -i "/^\[core\]/i [orbis]\nSigLevel = Optional TrustAll\nServer = file://$orbis_repo\n" "$conf"
fi
pacman --config "$conf" --dbpath "$tmpdb" -Sy >/dev/null
assume=()
[ -f "$orbis_repo/orbis.db.tar.zst" ] 2>/dev/null || assume=(--assume-installed orbis-platform=0.2.0
  --assume-installed xf86-video-amdgpu-orbis-ps4=25.0.0 --assume-installed linux-orbis-ps4=6.15.4)
pacman --config "$conf" --dbpath "$tmpdb" -Sp --print-format '%n %v %r' "${assume[@]}" orbis-graphics-pack > "$staging/closure.txt"
awk '$3 != "orbis-graphics-build" && $3 != "orbis" {print $1}' "$staging/closure.txt" | sort -u > "$staging/closure-arch.txt"
if [ -n "$base_pk" ] && [ -f "$base_pk" ]; then
  awk '{print $1}' "$base_pk" | sort -u > "$staging/base-names.txt"
  comm -23 "$staging/closure-arch.txt" "$staging/base-names.txt" > "$staging/download.txt"
  echo "closure: $(wc -l < "$staging/closure-arch.txt") Arch packages, $(wc -l < "$staging/download.txt") not already in the Orbis 0.2.0 image"
else
  cp "$staging/closure-arch.txt" "$staging/download.txt"
  echo "closure: $(wc -l < "$staging/download.txt") Arch packages (no base package list given: the full closure is included)"
fi
if [ -s "$staging/download.txt" ]; then
  xargs -a "$staging/download.txt" pacman --config "$conf" --dbpath "$tmpdb" -Sw --noconfirm -dd --cachedir "$repo" >/dev/null
fi
rm -f "$repo"/orbis-graphics-build.db* "$repo"/orbis-graphics-build.files*

step "sign + repository database"
sign=()
if [ -n "$keys" ] && [ -f "$keys/orbis-repo-key.fingerprint" ]; then
  export GNUPGHOME="$keys/gnupg"
  fpr="$(cat "$keys/orbis-repo-key.fingerprint")"
  for f in "$repo"/*.pkg.tar.zst; do
    [ -f "$f.sig" ] || gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$fpr" --detach-sign --no-armor "$f"
  done
  sign=(-s -k "$fpr")
  mkdir -p "$staging/keys"; cp "$keys/orbis-repo-key.asc" "$keys/orbis-repo-key.fingerprint" "$staging/keys/"
fi
rm -f "$repo"/orbis-graphics.db* "$repo"/orbis-graphics.files*
repo-add -q -n -R "${sign[@]}" "$repo/orbis-graphics.db.tar.zst" "$repo"/*.pkg.tar.zst

step "stage-0 GPU diagnostic mini-root"
"$R/diag/miniroot/build-miniroot.sh" --snapshot "$snapshot" --repo "$repo" --out "$staging/miniroot"
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" "$staging" 2>/dev/null || true
echo; echo "=== in-container build complete"
