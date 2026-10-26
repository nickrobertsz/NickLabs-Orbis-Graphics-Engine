#!/usr/bin/env bash
# NickLabs Orbis Graphics Pack — optional graphics kernel: the Orbis 0.2.0 kernel (feeRnt 6.15.4
# crashnt-4.7 + Orbis 0001) plus the REVIEWED patches listed in kernel/patches/series.
# Runs the unmodified Orbis 0.2.0 kernel/build-kernel.sh on a private copy of its kernel/ folder
# inside the orbis-build:0.2.0 container (clang/LLVM toolchain), so the Orbis 0.2.0 checkout is
# only read. Output: out/kernel-gfx/ (bzImage, config, modules, build-manifest.json, SHA256SUMS).
#
#   kernel/build-gfx-kernel.sh --orbis-src ~/orbis-release/nicklabs-ps4-orbis-linux [--jobs N]
#
# Release name: 6.15.4-orbis-ps4-gfx-g<commit>; modules live next to the 0.2.0 kernel's, and the
# 0.2.0 pacman hook keeps the previous bzImage as bzImage.orbis-prev (`orbis boot rollback-kernel`).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$here/.." && pwd)"
orbis_src=""; jobs=""
while [ $# -gt 0 ]; do
  case "$1" in
    --orbis-src) orbis_src="$(realpath "$2")"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --in-container) inside=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
series="$here/patches/series"
if ! grep -qv '^[[:space:]]*\(#\|$\)' "$series" 2>/dev/null; then
  echo "kernel/patches/series lists no patches: there is nothing to add to the Orbis 0.2.0 kernel."
  echo "Run kernel/research-deltas.sh, review, add patches, then run this again."
  exit 3
fi
if [ "${inside:-0}" = 0 ]; then
  [ -f "$orbis_src/kernel/build-kernel.sh" ] || { echo "--orbis-src must be the Orbis 0.2.0 checkout" >&2; exit 2; }
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  engine="${ENGINE:-podman}"
  mkdir -p "$R/out"
  exec $SUDO "$engine" run --rm -v "$R":/gfx:z -v "$orbis_src":/orbis:ro,z -w /gfx orbis-build:0.2.0 \
    bash -c "kernel/build-gfx-kernel.sh --in-container --orbis-src /orbis ${jobs:+--jobs $jobs} && chown -R $(id -u):$(id -g) /gfx/out/kernel-gfx /gfx/out/kernel-gfx-work"
fi

work="$R/out/kernel-gfx-work"
rm -rf "$work/kernel" && mkdir -p "$work"
cp -a "$orbis_src/kernel" "$work/kernel"
ln -sfn "$orbis_src/platform" "$work/platform"
t=aeolia-belize-6.15
pdir="$work/kernel/patches/$t"
[ -z "$(tail -c1 "$pdir/series")" ] || echo >> "$pdir/series"
grep -v '^[[:space:]]*\(#\|$\)' "$series" | while read -r p; do
  [ -f "$here/patches/$p" ] || { echo "kernel/patches/$p listed in series but missing" >&2; exit 2; }
  cp "$here/patches/$p" "$pdir/gfx-$(basename "$p")"
  echo "gfx-$(basename "$p")" >> "$pdir/series"
done
cp "$here/config/orbis-gfx.fragment" "$work/kernel/config/"
python3 - "$work/kernel/targets/$t.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["config_fragments"].append("config/orbis-gfx.fragment")
d["status"] = "Orbis Graphics Pack kernel (0.2.0 kernel + reviewed GPU patches) - compile-tested only"
json.dump(d, open(p, "w"), indent=2)
PY
echo "patch series:"; sed 's/^/  /' "$pdir/series"
"$work/kernel/build-kernel.sh" "$t" --out "$R/out/kernel-gfx" --src "$work/src" ${jobs:+--jobs "$jobs"}
python3 - "$R/out/kernel-gfx/build-manifest.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
assert d["kernel_release"].split("-")[1:4][:3] == ["orbis", "ps4", "gfx"], d["kernel_release"]
d["orbis_graphics"] = {"pack": "1.0.0", "status": "compiled; NOT boot-tested"}
json.dump(d, open(p, "w"), indent=2)
print("kernel release:", d["kernel_release"])
PY
