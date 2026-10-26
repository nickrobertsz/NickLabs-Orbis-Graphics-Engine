#!/usr/bin/env bash
# NickLabs Orbis Graphics Engine 2.0.0 / Graphics Pack 1.0.0 — undo integrate-root.sh on an Orbis root.
#
#   rollback-root.sh --root DIR [--boot BOOTDIR] [--dry-run]
#
# Restores, from DIR/var/lib/orbis-graphics/integration.json:
#   * the Orbis 0.2.0 Mesa packages from the in-image [orbis] repository (/var/lib/orbis/repo),
#   * removal of every pack package (orbis-graphics-pack, orbis-graphics, orbis-graphics-engine,
#     linux-orbis-ps4-ge, opencl-mesa-orbis-ps4, the experimental line, and dependencies that only
#     the pack pulled in),
#   * pacman.conf, the boot bzImage and boot/rootfs initramfs (from *.orbis-gfx-prev), the GE loader
#     folder on the boot partition, the orbis-qualify extension.
set -euo pipefail
root=""; boot=""; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) root="$(realpath "$2")"; shift 2 ;;
    --boot) boot="$(realpath "$2")"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$root" ] || { echo "need --root" >&2; exit 2; }
run() { echo "+ $*"; [ "$dry" = 1 ] || "$@"; }
in_root() { if [ "$root" = / ]; then "$@"; else arch-chroot "$root" "$@"; fi; }
state="$root/var/lib/orbis-graphics"
[ -f "$state/integration.json" ] || { echo "no integration receipt in $state; nothing to roll back" >&2; exit 3; }
grep -q '"state": "integrated"' "$state/integration.json" || { echo "receipt says the pack is not integrated" >&2; exit 3; }

# Order matters: the pack's own packages require the newer Mesa (mesa-orbis-ps4>=26.2.5-101/-201),
# so they are removed BEFORE Mesa is downgraded; otherwise pacman refuses the downgrade
# (Graphics Pack 1.0.0's rollback had this order reversed: ledger GE-B07).
echo "== remove the pack packages"
rm_list=()
for n in orbis-graphics-pack orbis-graphics-engine orbis-graphics opencl-mesa-orbis-ps4 linux-orbis-ps4-ge; do
  in_root pacman -Q "$n" >/dev/null 2>&1 && rm_list+=("$n")
done
[ ${#rm_list[@]} -gt 0 ] && run in_root pacman -Rns --noconfirm "${rm_list[@]}"

echo "== reinstall the Orbis 0.2.0 Mesa packages"
base=()
for n in mesa-orbis-ps4 lib32-mesa-orbis-ps4; do
  f="$(ls "$root"/var/lib/orbis/repo/"$n"-26.2.5-1-*.pkg.tar.zst 2>/dev/null | head -1)"
  [ -n "$f" ] && base+=("/var/lib/orbis/repo/$(basename "$f")")
done
[ ${#base[@]} -gt 0 ] || { echo "Orbis 0.2.0 Mesa packages not found in /var/lib/orbis/repo" >&2; exit 4; }
if in_root pacman -Q mesa-orbis-ps4-experimental >/dev/null 2>&1; then
  run sh -c "echo 'Orbis Graphics rollback: back to the 0.2.0 Mesa' > '$root/etc/orbis/allow-graphics-change'"
fi
run in_root pacman -U --noconfirm --ask=4 "${base[@]}"
in_root pacman -Q mesa-orbis-ps4-experimental >/dev/null 2>&1 && run in_root pacman -Rns --noconfirm mesa-orbis-ps4-experimental

echo "== files"
[ -L "$root/usr/local/bin/orbis-qualify" ] && run rm -f "$root/usr/local/bin/orbis-qualify"
restore() { local f="$1"; [ -f "$f.orbis-gfx-prev" ] && run mv -f "$f.orbis-gfx-prev" "$f" || true; }
restore "$root/usr/lib/orbis/initramfs/initramfs.cpio.gz"
if [ -n "$boot" ]; then
  restore "$boot/initramfs.cpio.gz"
  restore "$boot/bzImage"
  [ -d "$boot/orbis-ge-loader" ] && run rm -rf "$boot/orbis-ge-loader"
  if [ -f "$state/boot-files-before.txt" ]; then   # *.orbis-prev made by the 0.2.0 kernel hook during integration
    for f in "$boot"/bzImage.orbis-prev "$boot"/initramfs.cpio.gz.orbis-prev; do
      [ -f "$f" ] && ! grep -qx "$(basename "$f")" "$state/boot-files-before.txt" && run rm -f "$f"
    done
  fi
fi
[ -f "$state/pacman.conf.pre-integration" ] && run cp -a "$state/pacman.conf.pre-integration" "$root/etc/pacman.conf"
run rm -rf "$state/repo"
if [ "$dry" = 0 ]; then
  in_root pacman -Q > "$state/post-rollback-packages.txt"
  python3 - "$state" <<'PY'
import json, sys, time, pathlib
st = pathlib.Path(sys.argv[1])
rec = json.loads((st / "integration.json").read_text())
def q(f):
    return dict(l.split() for l in (st / f).read_text().splitlines())
pre, now = q("pre-integration-packages.txt"), q("post-rollback-packages.txt")
rec["state"] = "rolled-back"
rec["rolled_back_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
rec["differences_from_pre_integration"] = {n: {"pre": pre.get(n), "now": now.get(n)} for n in set(pre) | set(now)
                                           if pre.get(n) != now.get(n)}
(st / "integration.json").write_text(json.dumps(rec, indent=1) + "\n")
print("differences from the pre-integration package set:", rec["differences_from_pre_integration"] or "none")
PY
fi
echo "== done"
