#!/usr/bin/env bash
# NickLabs Orbis Graphics Pack — build the stage-0 GPU diagnostic mini-root (inside the Arch build
# container on Nobara, as root).
#
#   diag/miniroot/build-miniroot.sh --snapshot 2026/10/20 --repo out/pack/repo --out out/pack/miniroot
#
# A minimal Arch root with exactly the pack's GPU userspace (same mesa-orbis-ps4 / opencl / libdrm /
# Vulkan loader / orbis-graphics packages as the full system) plus Python for `orbis-gpu qualify`.
# No systemd, no network stack, no firmware, no kernel. The initramfs (orbis-gfx-stage0) chroots into
# it read-only with /dev, /proc, /sys bound and a tmpfs /tmp.
# Output: orbis-gfxdiag-miniroot.tar.zst (deterministic tar: sorted, numeric owners, mtime 0) +
#         orbis-gfxdiag-miniroot.manifest.json (package list, size, sha256)
set -euo pipefail
snapshot=""; repo=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --snapshot) snapshot="$2"; shift 2 ;;
    --repo) repo="$(realpath "$2")"; shift 2 ;;
    --out) out="$(realpath -m "$2")"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$snapshot" ] && [ -n "$repo" ] && [ -n "$out" ] || { echo "need --snapshot --repo --out" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run as root inside the build container" >&2; exit 2; }
conf="$(mktemp)"
cat > "$conf" <<CONF
[options]
Architecture = x86_64
SigLevel = Required DatabaseOptional
LocalFileSigLevel = Optional
NoExtract = usr/share/man/* usr/share/doc/* usr/share/info/* usr/share/locale/* usr/share/i18n/*
NoExtract = usr/include/* usr/share/gtk-doc/* usr/lib/python3*/test/*

[orbis-graphics]
SigLevel = Optional TrustAll
Server = file://$repo

[core]
Server = https://archive.archlinux.org/repos/$snapshot/\$repo/os/\$arch
[extra]
Server = https://archive.archlinux.org/repos/$snapshot/\$repo/os/\$arch
CONF
root="$(mktemp -d)"
# orbis-graphics itself depends on orbis-platform (the full Orbis system); the mini-root only needs
# its library, CLI and probe, so those files are extracted from the built package below.
pkgs=(glibc gcc-libs bash coreutils util-linux grep gawk python
      mesa-orbis-ps4 opencl-mesa-orbis-ps4 vulkan-icd-loader ocl-icd libdrm)
pacstrap -C "$conf" -M -G -c "$root" "${pkgs[@]}"
og="$(ls "$repo"/orbis-graphics-1.0.0-*.pkg.tar.zst | head -1)"
tar --zstd -xf "$og" -C "$root" usr/lib/orbis-graphics usr/bin/orbis-gpu usr/share/orbis-graphics
rm -rf "$root"/var/cache/pacman/pkg/* "$root"/usr/share/{man,doc,info,locale,i18n,gtk-doc} "$root"/usr/include
for d in dev proc sys sys/kernel/debug tmp out; do mkdir -p "$root/$d"; done
chroot "$root" pacman -Q > "$root/orbis-gfxdiag-packages.txt" 2>/dev/null || pacman --root "$root" -Q > "$root/orbis-gfxdiag-packages.txt"
mesa="$(grep '^mesa-orbis-ps4 ' "$root/orbis-gfxdiag-packages.txt" | cut -d' ' -f2)"
printf 'orbis-gfxdiag 1.0.0 mesa-orbis-ps4 %s snapshot %s\n' "$mesa" "$snapshot" > "$root/MINIROOT"
mkdir -p "$out"
tar --sort=name --numeric-owner --mtime=@0 --xattrs -C "$root" -cf - . | zstd -T0 -19 -q -o "$out/orbis-gfxdiag-miniroot.tar.zst"
python3 - "$root" "$out" <<'PY'
import hashlib, json, os, sys
root, out = sys.argv[1], sys.argv[2]
size = sum(os.lstat(os.path.join(d, f)).st_size for d, _, fs in os.walk(root) for f in fs)
tar = os.path.join(out, "orbis-gfxdiag-miniroot.tar.zst")
pk = dict(l.split() for l in open(os.path.join(root, "orbis-gfxdiag-packages.txt")) if len(l.split()) == 2)
json.dump({"schema": "orbis-gfxdiag-miniroot/1", "marker": open(os.path.join(root, "MINIROOT")).read().strip(),
           "installed_bytes": size, "archive_sha256": hashlib.sha256(open(tar, "rb").read()).hexdigest(),
           "packages": pk}, open(os.path.join(out, "orbis-gfxdiag-miniroot.manifest.json"), "w"), indent=1)
print(f"mini-root: {size >> 20} MiB installed, {len(pk)} packages")
PY
rm -rf --one-file-system "$root"
