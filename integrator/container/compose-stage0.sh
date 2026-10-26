#!/usr/bin/env bash
# NickLabs Orbis Graphics Pack — build the graphics stage-0 diagnostic image (as root; in the Orbis
# build container on Nobara, or on any Linux host with dosfstools/e2fsprogs/zstd).
#
#   compose-stage0.sh --work DIR --size 4G --output OUT.img
#
# DIR (prepared by `orbis-graphics stage0`) holds: bzImage, initramfs.cpio.gz (base initramfs +
# graphics overlay, already concatenated), bootargs.txt, vram.txt, miniroot.tar.zst.
# The mini-root is extracted to ROOTFS/orbis-gfxdiag (root-owned, as it must be) and the vendored
# Orbis 0.2.0 image builder writes the image (FAT ORBIS-BOOT + ext4 ORBIS-ROOT with the mini-root).
set -euo pipefail
work=""; size="4G"; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --work) work="$(realpath "$2")"; shift 2 ;;
    --size) size="$2"; shift 2 ;;
    --output) out="$(realpath -m "$2")"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root (inside the container: automatic)" >&2; exit 2; }
here="$(cd "$(dirname "$0")" && pwd)"
rootfs="$work/rootfs"
rm -rf --one-file-system "${rootfs:?}"
mkdir -p "$rootfs/orbis-gfxdiag"
tar --zstd -xpf "$work/miniroot.tar.zst" -C "$rootfs/orbis-gfxdiag" --numeric-owner
[ -e "$rootfs/orbis-gfxdiag/MINIROOT" ] || { echo "mini-root archive has no MINIROOT marker" >&2; exit 3; }
for d in dev proc sys sys/kernel/debug tmp; do mkdir -p "$rootfs/orbis-gfxdiag/$d"; done
python3 "$here/../vendor/orbis020/make_usb_image.py" image --stage0 \
  --kernel "$work/bzImage" --initramfs "$work/initramfs.cpio.gz" \
  --rootfs-dir "$rootfs" --size "$size" --boot-size 512M \
  --bootargs "$(head -n1 "$work/bootargs.txt")" --vram "$(head -n1 "$work/vram.txt")" \
  -o "$out" > "$out.plan.json"
echo "== wrote $out"
