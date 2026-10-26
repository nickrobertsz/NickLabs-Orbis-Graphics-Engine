#!/usr/bin/env bash
# NickLabs Orbis Graphics Pack — runs INSIDE the privileged Orbis build container on Nobara.
#
#   image-op.sh integrate --image /work/OUT.img --pack /pack [--experimental]
#   image-op.sh rollback  --image /work/OUT.img
#
# Loop-mounts the image's two partitions by byte offset (no partition scanning), mounts
# ORBIS-ROOT at /target and ORBIS-BOOT at /target/boot/ps4, runs integrate-root.sh /
# rollback-root.sh, unmounts, and checks both filesystems (e2fsck -fn, fsck.fat -n).
# The image given here is always the integrator's COPY; the original is never opened for writing.
set -euo pipefail
op="$1"; shift
image=""; pack=""; extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    --image) image="$2"; shift 2 ;;
    --pack) pack="$2"; shift 2 ;;
    --experimental) extra+=(--experimental); shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
here="$(cd "$(dirname "$0")" && pwd)"
[ -f "$image" ] || { echo "no image $image" >&2; exit 2; }
read -r boot_off boot_len root_off root_len < <(python3 - "$image" <<'PY'
import struct, sys
b = open(sys.argv[1], "rb").read(512)
assert b[510:512] == b"\x55\xaa", "no MBR"
parts = [struct.unpack_from("<B3sB3sII", b, 446 + 16 * i) for i in range(4)]
fat = [p for p in parts if p[2] in (0x0b, 0x0c)][0]
lin = [p for p in parts if p[2] == 0x83][0]
print(fat[4] * 512, fat[5] * 512, lin[4] * 512, lin[5] * 512)
PY
)
target=/target
mkdir -p "$target"
cleanup() {
  set +e
  umount "$target/boot/ps4" 2>/dev/null
  for m in dev/pts dev proc sys run; do umount "$target/$m" 2>/dev/null; done
  umount "$target" 2>/dev/null
  [ -n "${lroot:-}" ] && losetup -d "$lroot" 2>/dev/null
  [ -n "${lboot:-}" ] && losetup -d "$lboot" 2>/dev/null
}
trap cleanup EXIT
lroot="$(losetup -f --show -o "$root_off" --sizelimit "$root_len" "$image")"
lboot="$(losetup -f --show -o "$boot_off" --sizelimit "$boot_len" "$image")"
echo "ORBIS-ROOT -> $lroot, ORBIS-BOOT -> $lboot"
[ "$(blkid -s LABEL -o value "$lroot")" = ORBIS-ROOT ] || { echo "partition 2 is not ORBIS-ROOT" >&2; exit 3; }
[ "$(blkid -s LABEL -o value "$lboot")" = ORBIS-BOOT ] || { echo "partition 1 is not ORBIS-BOOT" >&2; exit 3; }
mount "$lroot" "$target"
mkdir -p "$target/boot/ps4"
mount -t vfat "$lboot" "$target/boot/ps4"
case "$op" in
  integrate) "$here/../root/integrate-root.sh" --root "$target" --pack "$pack" --boot "$target/boot/ps4" "${extra[@]}" ;;
  rollback)  "$here/../root/rollback-root.sh" --root "$target" --boot "$target/boot/ps4" ;;
  *) echo "unknown op $op" >&2; exit 2 ;;
esac
cp "$target/var/lib/orbis-graphics/integration.json" "$(dirname "$image")/$(basename "$image").in-image-receipt.json" 2>/dev/null || true
sync
cleanup
trap - EXIT
echo "== filesystem checks"
e2fsck -fn "$image?offset=$root_off" || { echo "e2fsck reports problems" >&2; exit 4; }
dd if="$image" of=/tmp/bootpart.img bs=1M iflag=skip_bytes,count_bytes skip="$boot_off" count="$boot_len" status=none
fsck.fat -n /tmp/bootpart.img || { echo "fsck.fat reports problems" >&2; exit 4; }
rm -f /tmp/bootpart.img
echo "== $op complete"
