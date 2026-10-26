#!/usr/bin/env bash
# NickLabs Orbis Graphics — build the portable GPU stack used by the stage-0 diagnostic mini-root.
#
#   diag/miniroot/build-portable-stack.sh --src DIR --prefix /opt/orbis-gfx --out BUILD_DIR [--jobs N]
#
# Builds, from pinned sources (manifest/sources.json):
#   libdrm (amdgpu only) and Mesa (radeonsi + RADV, EGL surfaceless/GBM, GLES2/GL,
#   no X11/Wayland/GLX) with the Orbis PS4 patch series, compiled for Jaguar (-march=btver2).
# The result is self-contained under --prefix; diag/miniroot/assemble-miniroot.py adds the
# runtime libraries it links against and the GPU probe.
#
# This is NOT the distribution Mesa: the Arch packages (mesa/stable/PKGBUILD) are built
# from the same source + patches by build/build-pack.sh on Nobara.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$here/../.." && pwd)"
src=""; prefix="/opt/orbis-gfx"; out=""; jobs="$(nproc)"; variant="stable"
while [ $# -gt 0 ]; do
  case "$1" in
    --src) src="$(realpath "$2")"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --out) out="$(realpath -m "$2")"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --variant) variant="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$src" ] && [ -n "$out" ] || { echo "need --src and --out" >&2; exit 2; }
mesa_dir="$src/mesa-$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['mesa'][sys.argv[2]]['dir_suffix'])" "$R/manifest/sources.json" "$variant")"
meson=(python3 "$src/meson/meson.py")
export PYTHONPATH="$src/mako${PYTHONPATH:+:$PYTHONPATH}"
stage="$out/stage"
mkdir -p "$out" "$stage"
export PKG_CONFIG_PATH="$stage$prefix/lib/pkgconfig:${ORBIS_EXTRA_PKG_CONFIG_PATH:-}"
export PATH="${ORBIS_EXTRA_PATH:+$ORBIS_EXTRA_PATH:}$PATH"
export CFLAGS="-O2 -march=btver2 -mtune=btver2 -pipe -fno-plt"
export CXXFLAGS="$CFLAGS"

echo "== verify pinned sources"
python3 "$R/build/check_sources.py" --src "$src" --variant "$variant"

echo "== libdrm"
rm -rf "$out/libdrm"
"${meson[@]}" setup "$out/libdrm" "$src/libdrm" --prefix "$prefix" --libdir lib --buildtype release \
  -Damdgpu=enabled -Dradeon=disabled -Dintel=disabled -Dnouveau=disabled -Dvmwgfx=disabled \
  -Dfreedreno=disabled -Dvc4=disabled -Detnaviv=disabled -Dexynos=disabled -Domap=disabled \
  -Dtegra=disabled -Dcairo-tests=disabled -Dman-pages=disabled -Dvalgrind=disabled \
  -Dtests=false -Dinstall-test-programs=false -Dudev=false
ninja -C "$out/libdrm" -j "$jobs"
DESTDIR="$stage" ninja -C "$out/libdrm" install

echo "== mesa ($variant): apply Orbis patches to a private copy"
work="$out/mesa-src"
rm -rf "$work" && mkdir -p "$work"
git -C "$mesa_dir" archive --format=tar HEAD | tar -C "$work" -xf -
while read -r p; do
  [ -z "$p" ] && continue
  echo "applying $p"
  patch -d "$work" -Np1 -F0 -i "$R/mesa/$variant/patches/$p"
done < "$R/mesa/$variant/patches/series"

echo "== mesa: configure"
rm -rf "$out/mesa"
"${meson[@]}" setup "$out/mesa" "$work" --prefix "$prefix" --libdir lib --buildtype release \
  -Db_ndebug=true \
  -Dplatforms= \
  -Dgallium-drivers=radeonsi \
  -Dvulkan-drivers=amd \
  -Dvulkan-layers= \
  -Dglx=disabled -Degl=enabled -Dgbm=enabled -Dgles1=disabled -Dgles2=enabled -Dopengl=true \
  -Dglvnd=disabled \
  -Dllvm=enabled -Dshared-llvm=enabled \
  -Dvideo-codecs= -Dgallium-va=disabled -Dgallium-rusticl=false \
  -Dvalgrind=disabled -Dlibunwind=disabled -Dlmsensors=disabled \
  -Dbuild-tests=false -Dintel-rt=disabled -Dmicrosoft-clc=disabled \
  ${ORBIS_MESA_EXTRA_OPTS:-}
echo "== mesa: build"
ninja -C "$out/mesa" -j "$jobs"
DESTDIR="$stage" ninja -C "$out/mesa" install
python3 - "$out" "$prefix" "$variant" <<'PY'
import json, subprocess, sys, pathlib
out, prefix, variant = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
info = {"variant": variant, "prefix": prefix,
        "mesa_version": (out / "mesa-src" / "VERSION").read_text().strip(),
        "cflags": "-O2 -march=btver2 -mtune=btver2"}
(out / "stage" / prefix.lstrip("/") / "orbis-gfx-build.json").write_text(json.dumps(info, indent=1) + "\n")
print(json.dumps(info))
PY
echo "== installed into $stage$prefix"
