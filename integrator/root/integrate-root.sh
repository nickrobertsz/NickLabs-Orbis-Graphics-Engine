#!/usr/bin/env bash
# NickLabs Orbis Graphics Engine 2.0.0 (and Graphics Pack 1.0.0) — install a pack into an Orbis 0.2.0
# root filesystem. With a 2.0 pack this also stages the Graphics Engine kernel as the boot bzImage,
# copies the GE loader payloads to the boot partition and enables the Black Box / memory policy /
# hardware API services (orbis-graphics-engine.install).
#
#   integrate-root.sh --root DIR --pack PACKDIR [--boot BOOTDIR] [--experimental] [--dry-run]
#
# DIR is a mounted Orbis root (the image's ORBIS-ROOT inside the Nobara container, an Orbis staging
# rootfs before the image exists, or / on a running console). BOOTDIR is the ORBIS-BOOT FAT
# partition (bzImage, initramfs.cpio.gz, ...) when it should be updated too.
# Every change is recorded in DIR/var/lib/orbis-graphics/integration.json and undone by
# rollback-root.sh. Nothing outside DIR and BOOTDIR is touched.
set -euo pipefail
root=""; pack=""; boot=""; experimental=0; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --root) root="$(realpath "$2")"; shift 2 ;;
    --pack) pack="$(realpath "$2")"; shift 2 ;;
    --boot) boot="$(realpath "$2")"; shift 2 ;;
    --experimental) experimental=1; shift ;;
    --dry-run) dry=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$root" ] && [ -n "$pack" ] || { echo "need --root and --pack" >&2; exit 2; }
run() { echo "+ $*"; [ "$dry" = 1 ] || "$@"; }
in_root() { if [ "$root" = / ]; then "$@"; else arch-chroot "$root" "$@"; fi; }
state="$root/var/lib/orbis-graphics"
receipt="$state/integration.json"
log() { echo "== $*"; }

log "checks"
[ -f "$pack/pack-manifest.json" ] || { echo "not a Graphics Pack directory: $pack" >&2; exit 3; }
[ -d "$root/usr/share/orbis" ] || { echo "$root is not an Orbis root (no /usr/share/orbis)" >&2; exit 3; }
plat="$(ls -d "$root"/var/lib/pacman/local/orbis-platform-* 2>/dev/null | head -1 | xargs -r basename)"
case "$plat" in
  orbis-platform-0.2.0-*) echo "base: $plat" ;;
  "") echo "orbis-platform is not installed in $root" >&2; exit 3 ;;
  *) echo "WARNING: base is $plat; this pack was qualified against orbis-platform 0.2.0" >&2 ;;
esac
if [ -f "$receipt" ] && grep -q '"state": "integrated"' "$receipt"; then
  echo "the Graphics Pack is already integrated here (see $receipt); run rollback-root.sh first" >&2; exit 4
fi
( cd "$pack" && sha256sum -c --quiet SHA256SUMS ) || { echo "pack checksum failure" >&2; exit 5; }
free_mb=$(df -Pm "$root" | awk 'NR==2 {print $4}')
need_mb=$(( $(du -sm "$pack/repo" | cut -f1) * 4 + 300 ))
echo "free in root: ${free_mb} MiB; estimated need: ${need_mb} MiB (packages unpacked + kernel modules)"
[ "$free_mb" -ge "$need_mb" ] || { echo "not enough space in $root (${free_mb} MiB free, ~${need_mb} MiB needed): enlarge ORBIS-ROOT first" >&2; exit 6; }
signed=0; [ -f "$pack/keys/orbis-repo-key.asc" ] && [ -f "$pack/repo/orbis-graphics.db.tar.zst.sig" ] && signed=1

log "record the starting state"
run mkdir -p "$state"
if [ "$dry" = 0 ]; then
  in_root pacman -Q > "$state/pre-integration-packages.txt"
  cp -a "$root/etc/pacman.conf" "$state/pacman.conf.pre-integration"
fi

log "local [orbis-graphics] repository inside the root"
run rm -rf "$state/repo"
run cp -a "$pack/repo" "$state/repo"
siglevel="Optional TrustAll"
if [ "$signed" = 1 ]; then
  siglevel="Required"
  fpr="$(cat "$pack/keys/orbis-repo-key.fingerprint")"
  run install -Dm644 "$pack/keys/orbis-repo-key.asc" "$root/usr/share/orbis-graphics/orbis-graphics-repo-key.asc"
  [ "$dry" = 1 ] || in_root sh -c "pacman-key --list-keys >/dev/null 2>&1 || { pacman-key --init && pacman-key --populate archlinux; }"
  run in_root pacman-key --add /usr/share/orbis-graphics/orbis-graphics-repo-key.asc
  run in_root pacman-key --lsign-key "$fpr"
fi
if ! grep -q '^\[orbis-graphics\]' "$root/etc/pacman.conf"; then
  echo "+ add [orbis-graphics] before [orbis] in $root/etc/pacman.conf (SigLevel $siglevel)"
  if [ "$dry" = 0 ]; then
    python3 - "$root/etc/pacman.conf" "$siglevel" <<'PY'
import sys
p, sig = sys.argv[1], sys.argv[2]
lines = open(p).read().splitlines(True)
block = ["# NickLabs Orbis Graphics (local, offline). Listed before [orbis] so its newer\n",
         "# mesa-orbis-ps4 builds are chosen. Removed by rollback-root.sh.\n",
         "[orbis-graphics]\n", f"SigLevel = {sig}\n", "Server = file:///var/lib/orbis-graphics/repo\n", "\n"]
for i, l in enumerate(lines):
    if l.strip() == "[orbis]":
        j = i
        while j > 0 and lines[j - 1].startswith("#"):
            j -= 1
        lines[j:j] = block
        break
else:
    lines += ["\n"] + block
open(p, "w").write("".join(lines))
PY
  fi
fi

log "save the boot files this run may replace (restored by rollback-root.sh)"
save_prev() { [ -f "$1" ] && [ ! -f "$1.orbis-gfx-prev" ] && run cp -a "$1" "$1.orbis-gfx-prev"; return 0; }
save_prev "$root/usr/lib/orbis/initramfs/initramfs.cpio.gz"
if [ -n "$boot" ]; then
  save_prev "$boot/initramfs.cpio.gz"; save_prev "$boot/bzImage"
  [ "$dry" = 1 ] || ls -1 "$boot" > "$state/boot-files-before.txt"
fi

log "install the pack (offline: only the local [orbis-graphics] and [orbis] repositories)"
# The pack carries its whole dependency closure, so no public mirror is contacted (LAN-only consoles).
if [ "$dry" = 0 ]; then
  awk '/^\[/{ if (seen) exit; seen = 1 } {print}' "$root/etc/pacman.conf" > "$state/pacman-offline.conf"
  printf '\n[orbis-graphics]\nSigLevel = %s\nServer = file:///var/lib/orbis-graphics/repo\n' "$siglevel" >> "$state/pacman-offline.conf"
  osig="$(awk '/^\[orbis\]/{f=1;next} /^\[/{f=0} f && /^SigLevel/{print; exit}' "$root/etc/pacman.conf")"
  printf '\n[orbis]\n%s\nServer = file:///var/lib/orbis/repo\n' "${osig:-SigLevel = Optional TrustAll}" >> "$state/pacman-offline.conf"
fi
pconf=/var/lib/orbis-graphics/pacman-offline.conf
run in_root pacman --config "$pconf" -Sy --noconfirm --needed orbis-graphics-pack
if [ "$experimental" = 1 ]; then
  log "experimental Mesa line (deliberate graphics change, logged by the Orbis package guard)"
  run sh -c "echo 'Orbis Graphics Pack: experimental Mesa line requested at integration' > '$root/etc/orbis/allow-graphics-change'"
  run in_root pacman --config "$pconf" -S --noconfirm --ask=4 mesa-orbis-ps4-experimental
fi

if [ "$dry" = 0 ]; then   # pacman copied the pack's packages into its cache; the repo copy above is enough
  for f in "$pack"/repo/*.pkg.tar.zst; do rm -f "$root/var/cache/pacman/pkg/$(basename "$f")" "$root/var/cache/pacman/pkg/$(basename "$f").sig"; done
fi

log "orbis-qualify extension (/usr/local/bin precedes /usr/bin)"
run install -d "$root/usr/local/bin"
run ln -sfn /usr/lib/orbis-graphics/orbis-qualify-with-graphics "$root/usr/local/bin/orbis-qualify"

log "GPU stage-0 diagnosis in the boot initramfs (orbis.gfxdiag=1; inert otherwise)"
overlay="$pack/stage0/orbis-gfx-overlay.cpio.gz"
add_overlay() {  # $1 = initramfs path; its pre-integration copy was saved as .orbis-gfx-prev above
  local f="$1"
  [ -f "$f" ] || [ -f "$f.orbis-gfx-prev" ] || { echo "   (no $f)"; return 0; }
  if [ "$dry" = 0 ]; then cat "$f.orbis-gfx-prev" "$overlay" > "$f.new" && mv "$f.new" "$f"; fi
  echo "   $f: original + overlay ($(sha256sum "$overlay" | cut -c1-16))"
}
add_overlay "$root/usr/lib/orbis/initramfs/initramfs.cpio.gz"
[ -n "$boot" ] && add_overlay "$boot/initramfs.cpio.gz"

gekern=""
if [ "$dry" = 0 ]; then
  gekern="$(ls -d "$root"/usr/lib/modules/*-orbis-ps4-ge*/vmlinuz 2>/dev/null | grep -v -- '-ge-uvd' | sort | tail -1 || true)"
fi
if [ -n "$gekern" ]; then
  log "Graphics Engine kernel: $(basename "$(dirname "$gekern")")"
  if [ -n "$boot" ]; then
    # Orbis 0.2.0's kernel-sync hook may already have staged it (when it found the boot partition);
    # make sure the boot partition carries exactly this kernel either way.
    if ! cmp -s "$gekern" "$boot/bzImage"; then run cp "$gekern" "$boot/bzImage"; fi
    echo "   $boot/bzImage = $(sha256sum "$boot/bzImage" | cut -c1-16) (previous kernel kept as bzImage.orbis-gfx-prev)"
  else
    echo "   no --boot given: the kernel is installed in the root; stage it with: orbis boot install-kernel $gekern"
  fi
fi
if [ -d "$pack/loader" ] && ls "$pack"/loader/orbis-ge-*.bin >/dev/null 2>&1 && [ -n "$boot" ]; then
  log "Graphics Engine loader payloads -> $boot/orbis-ge-loader/ (the console still boots with whatever payload you send)"
  run mkdir -p "$boot/orbis-ge-loader"
  run cp "$pack"/loader/orbis-ge-*.bin "$pack"/loader/SHA256SUMS "$pack"/loader/build.json "$boot/orbis-ge-loader/"
fi

log "receipt"
if [ "$dry" = 0 ]; then
  in_root pacman -Q > "$state/post-integration-packages.txt"
  python3 - "$state" "$pack" "$boot" "$experimental" "$siglevel" <<'PY'
import hashlib, json, sys, time, pathlib
state, pack, boot, exp, sig = sys.argv[1:6]
st = pathlib.Path(state)
def q(f):
    out = {}
    for l in (st / f).read_text().splitlines():
        n, v = l.split()
        out[n] = v
    return out
pre, post = q("pre-integration-packages.txt"), q("post-integration-packages.txt")
man = json.loads((pathlib.Path(pack) / "pack-manifest.json").read_text())
def h(p):
    p = pathlib.Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
root = st.parents[2]
files = {}
for p in ["usr/lib/orbis/initramfs/initramfs.cpio.gz"] + (["BOOT:initramfs.cpio.gz", "BOOT:bzImage"] if boot else []):
    real = pathlib.Path(boot) / p.split(":", 1)[1] if p.startswith("BOOT:") else root / p
    files[p] = {"sha256": h(real), "original_sha256": h(str(real) + ".orbis-gfx-prev")}
rec = {"schema": "orbis-graphics-integration/1", "state": "integrated", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
       "pack": {"name": man.get("name"), "version": man.get("version"), "build_id": man.get("build_id")},
       "experimental_mesa": exp == "1", "repo_siglevel": sig,
       "installed_or_upgraded": {n: {"from": pre.get(n), "to": v} for n, v in post.items() if pre.get(n) != v},
       "removed": {n: v for n, v in pre.items() if n not in post},
       "files": files, "ge_loader_dir": "BOOT:orbis-ge-loader" if boot and (pathlib.Path(boot) / "orbis-ge-loader").is_dir() else None,
       "symlinks": {"/usr/local/bin/orbis-qualify": "/usr/lib/orbis-graphics/orbis-qualify-with-graphics"},
       "pacman_conf_backup": "var/lib/orbis-graphics/pacman.conf.pre-integration",
       "rollback": "/usr/lib/orbis-graphics/rollback-root.sh --root / --boot /boot/ps4"}
(st / "integration.json").write_text(json.dumps(rec, indent=1) + "\n")
print(json.dumps({k: rec[k] for k in ("pack", "installed_or_upgraded", "removed")}, indent=1))
PY
fi
log "done"
