#!/usr/bin/env bash
# NickLabs Orbis Graphics Engine 2.0.0 — builds every package (GE kernel package, Mesa 26.2.5-201,
# tools, engine), the dependency closure and the stage-0 GPU mini-root in a container, then assembles
#   out/release/ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst (+ .sha256)
# Run kernel/build-ge-kernel.sh and loader/build-loader.sh first (nobara/orbis-ge-all.sh does all of it).
#
#   build/build-pack.sh --orbis-src ~/orbis-release/nicklabs-ps4-orbis-linux [--experimental] [--no-tests]
#
# --orbis-src   the Orbis 0.2.0 checkout you built 0.2.0 from. Read for: the orbis-build:0.2.0 container
#               (built from it if missing), out/repo (0.2.0 packages, never modified),
#               out/rootfs/packages.txt (so the pack skips what the image already has) and out/keys
#               (the same local signing key; optional).
# --experimental also try the experimental Mesa (main snapshot). A failure is recorded as BLOCKED in the
#               pack manifest; it never fails the stable pack.
#
# Nothing here touches the host OS outside this checkout (sudo is only used for podman) and nothing
# touches a PS4.
set -euo pipefail
R="$(cd "$(dirname "$0")/.." && pwd)"
cd "$R"
orbis_src=""; experimental=0; tests=1; engine="${ENGINE:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --orbis-src) orbis_src="$(realpath "$2")"; shift 2 ;;
    --experimental) experimental=1; shift ;;
    --no-tests) tests=0; shift ;;
    --engine) engine="$2"; shift 2 ;;
    -h|--help) sed -n 2,17p "$0"; exit 0 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
need() { command -v "$1" >/dev/null || { echo "missing $1 ($2)" >&2; exit 2; }; }
step() { echo; echo "######## $*"; }
need git "sudo dnf install git"; need python3 "sudo dnf install python3"; need zstd "sudo dnf install zstd"
[ -n "$engine" ] || { command -v podman >/dev/null && engine=podman; } || { command -v docker >/dev/null && engine=docker; } || true
[ -n "$engine" ] || { echo "need podman (sudo dnf install podman)" >&2; exit 2; }
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
[ -n "$orbis_src" ] && [ -f "$orbis_src/platform/orbis/__init__.py" ] || {
  echo "--orbis-src must point at the Orbis 0.2.0 checkout (the folder with platform/ packaging/ out/)" >&2; exit 2; }
ver="$(python3 -c "import sys; sys.path.insert(0,'$orbis_src/platform'); import orbis; print(orbis.__version__)")"
[ "$ver" = 0.2.0 ] || { echo "--orbis-src is Orbis $ver; the Graphics Engine 2.0.0 targets 0.2.0" >&2; exit 2; }
rel_ver="$(python3 -c "import json; print(json.load(open('manifest/release.json'))['version'])")"
rel_pfx="$(python3 -c "import json; print(json.load(open('manifest/release.json'))['archive_prefix'])")"
PACK="$rel_pfx-v$rel_ver.tar.zst"
CTR="orbis-gfx-build:$rel_ver"
[ -f out/kernel-ge/build-manifest.json ] || { echo "out/kernel-ge missing: run kernel/build-ge-kernel.sh --orbis-src $orbis_src first" >&2; exit 2; }
[ -f out/loader-ge/build.json ] || echo "NOTE: out/loader-ge missing (loader/build-loader.sh); the pack will carry no GE loader payloads"
[ -f "$orbis_src/out/repo/orbis.db.tar.zst" ] || echo "NOTE: $orbis_src/out/repo has no repository; Orbis 0.2.0 packages are assumed installed"
[ -f "$orbis_src/out/rootfs/packages.txt" ] || echo "NOTE: no out/rootfs/packages.txt; the pack will carry the full dependency closure (bigger, still correct)"
free_gb=$(df -Pk "$R" | awk 'NR==2 {print int($4/1048576)}')
[ "$free_gb" -ge 40 ] || echo "WARNING: ${free_gb} GB free here; the pack build needs about 40 GB" >&2
commit="$(git rev-parse HEAD 2>/dev/null || echo worktree)"
git diff --quiet 2>/dev/null || echo "NOTE: uncommitted changes; the source archive in the pack is built from HEAD ($commit)"

if [ "$tests" = 1 ]; then
  step "1/5 unit tests (host, ~10 s)"
  python3 -m unittest discover -s tests/unit -t tests/unit
fi

step "2/5 build containers"
if ! $SUDO "$engine" image exists orbis-build:0.2.0 2>/dev/null && ! $SUDO "$engine" image inspect orbis-build:0.2.0 >/dev/null 2>&1; then
  echo "orbis-build:0.2.0 missing; building it from $orbis_src (same Containerfile and snapshot as 0.2.0)"
  make -C "$orbis_src" container ENGINE="$SUDO $engine"
fi
$SUDO "$engine" build -t $CTR -f build/Containerfile.gfx build

step "3/5 packages, dependency closure, mini-root (in the container; Mesa takes 1-3 h)"
rm -rf out/pack && mkdir -p out/pack
[ -f out/loader-ge/build.json ] && cp -a out/loader-ge out/pack/loader
mounts=(-v "$R":/src:z -v "$orbis_src/platform":/orbis/platform:ro,z)
args=(--staging /src/out/pack)
if [ -f "$orbis_src/out/repo/orbis.db.tar.zst" ]; then mounts+=(-v "$orbis_src/out/repo":/orbis/repo:ro,z); args+=(--orbis-repo /orbis/repo); fi
if [ -f "$orbis_src/out/rootfs/packages.txt" ]; then mounts+=(-v "$orbis_src/out/rootfs/packages.txt":/orbis/packages.txt:ro,z); args+=(--base-packages /orbis/packages.txt); fi
if [ -f "$orbis_src/out/keys/orbis-repo-key.fingerprint" ]; then mounts+=(-v "$orbis_src/out/keys":/orbis/keys:z); args+=(--keys /orbis/keys); fi
[ "$experimental" = 1 ] && args+=(--experimental)
$SUDO "$engine" run --rm --privileged "${mounts[@]}" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -w /src \
  $CTR build/in-container-build.sh "${args[@]}"
$SUDO chown -R "$(id -u):$(id -g)" out/pack

step "4/5 assemble the pack"
mapfile -t ex < out/pack/experimental-state.txt
mkdir -p out/release
bp=(); [ -f "$orbis_src/out/rootfs/packages.txt" ] && bp=(--base-packages "$orbis_src/out/rootfs/packages.txt")
python3 build/assemble_pack.py --staging out/pack --out out/release --git-commit "$commit" \
  --experimental-state "${ex[0]}" --experimental-reason "${ex[1]:-}" "${bp[@]}"

step "5/5 self-check"
./orbis-graphics verify --pack out/release/$PACK
( cd out/release && sha256sum -c $PACK.sha256 )
echo
echo "Pack: $R/out/release/$PACK"
echo "Experimental Mesa: ${ex[0]} (${ex[1]:-})"
echo "Next: ./orbis-graphics integrate --base-image <orbis-0.2.0 image> --pack out/release/$PACK --output <new image>"
echo "      ./orbis-graphics stage0 --base-stage0-image <orbis-0.2.0 stage-0 image> --pack out/release/$PACK --output <new stage-0 image>"
