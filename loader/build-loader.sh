#!/usr/bin/env bash
# NickLabs Orbis Graphics Engine 2.0: build the patched ps4-linux-loader payloads (run on Nobara).
#
#   loader/build-loader.sh [--out out/loader-ge] [--engine podman]
#
# Clones ps4-linux-loader at the commit Orbis 0.2.0 pins (manifest/sources.json "loader"),
# applies loader/0001-*.patch, builds in the orbis-build:0.2.0 container (gcc, make; the payload
# is freestanding and runs on the PS4 under GoldHEN) and writes
#   out/loader-ge/orbis-ge-<N>mb.elf / .bin   N = 256 384 512 768 1024 1536 2048 (default size)
#   out/loader-ge/SHA256SUMS, build.json
# Any validated size can still be chosen at boot with vram.txt. Status: built only on Nobara.
set -euo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)"
out="$R/out/loader-ge"; engine="${ENGINE:-podman}"; inside=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$(realpath -m "$2")"; shift 2 ;;
    --engine) engine="$2"; shift 2 ;;
    --in-container) inside=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
commit="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['loader']['commit'])" "$R/manifest/sources.json")"
patch="$(ls "$R"/loader/0001-*.patch)"
if [ "$inside" = 0 ]; then
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  mkdir -p "$R/out"
  exec $SUDO "$engine" run --rm -v "$R":/gfx:z -w /gfx orbis-loader-build:0.2.0 \
    bash -c "loader/build-loader.sh --in-container --out /gfx/out/loader-ge && chown -R $(id -u):$(id -g) /gfx/out/loader-ge /gfx/out/src"
fi
src="$R/out/src/ps4-linux-loader"
[ -d "$src/.git" ] || git clone -q https://github.com/ps4-linux/ps4-linux-loader "$src"
git -C "$src" fetch -q origin || true
git -C "$src" reset -q --hard "$commit"
git -C "$src" clean -qfdx
[ "$(git -C "$src" rev-parse HEAD)" = "$commit" ] || { echo "loader source is not at $commit" >&2; exit 3; }
git -C "$src" apply "$patch"
make -C "$src/linux" orbis
mkdir -p "$out"
for f in "$src"/linux/payloads/orbis-ge-*mb.bin "$src"/linux/payloads/orbis-ge-*mb.elf; do cp "$f" "$out/"; done
( cd "$out" && sha256sum orbis-ge-* > SHA256SUMS )
python3 - "$out" "$commit" "$(sha256sum "$patch" | cut -d' ' -f1)" <<'PY'
import json, os, sys, time
out, commit, psha = sys.argv[1:4]
json.dump({"schema": "orbis-ge-loader/1", "loader_commit": commit, "patch_sha256": psha,
           "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "payloads": sorted(f for f in os.listdir(out) if f.startswith("orbis-ge-")),
           "status": "built; not run on a PS4 by NickLabs Orbis"}, open(os.path.join(out, "build.json"), "w"), indent=1)
PY
echo "payloads in $out (send one with: python3 <orbis-0.2.0>/packaging/scripts/send_payload.py <PS4-IP> $out/orbis-ge-1024mb.elf)"
