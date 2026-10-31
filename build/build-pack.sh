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
orbis_src=""; experimental=0; tests=1; engine="${ENGINE:-}"; bore=0; target="cuh-1215a"
while [ $# -gt 0 ]; do
  case "$1" in
    --orbis-src) orbis_src="$(realpath "$2")"; shift 2 ;;
    --bore) bore=1; shift ;;            # package out/kernel-ge-bore (only when asked: GE-A06)
    --target) target="$2"; shift 2 ;;   # recorded in the pack's selection manifest
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
[ -s "$orbis_src/out/repo/orbis.db.tar.zst" ] || {
  echo "BLOCKED: $orbis_src/out/repo has no built Core repository; pass the completed private Core staging checkout" >&2
  exit 6
}
[ -f "$orbis_src/out/rootfs/packages.txt" ] || echo "NOTE: no out/rootfs/packages.txt; the pack will carry the full dependency closure (bigger, still correct)"
free_gb=$(df -Pk "$R" | awk 'NR==2 {print int($4/1048576)}')
[ "$free_gb" -ge 40 ] || echo "WARNING: ${free_gb} GB free here; the pack build needs about 40 GB" >&2
commit="$(git rev-parse HEAD 2>/dev/null || echo worktree)"
source_before="$(python3 build/source_snapshot.py --fingerprint)"
echo "Source snapshot: $source_before (local patches included in source archive)"

if [ "$tests" = 1 ]; then
  step "1/5 unit tests (host, ~10 s)"
  python3 -m unittest discover -s tests/unit -t tests/unit
fi

# GE-A05: the 0.2.0 checkout is only read. Its state is recorded now and compared at the end;
# anything that has to write (make, gpg) works on a private copy under out/.work-orbis-src.
python3 build/orbis_src_guard.py snapshot "$orbis_src" out/.orbis-src-guard.json
priv="$R/out/.work-orbis-src"
cleanup_priv() { rm -rf "$priv"; }
trap cleanup_priv EXIT

step "2/5 build containers"
core_container='orbis-build:0.2.0-fullbase'
if ! $SUDO "$engine" image exists "$core_container" 2>/dev/null; then
  core_container='orbis-build:0.2.0'
fi
if ! $SUDO "$engine" image exists "$core_container" 2>/dev/null; then
  echo "orbis-build:0.2.0 missing; building it from a private copy of $orbis_src (same Containerfile and snapshot as 0.2.0)"
  rm -rf "$priv/src" && mkdir -p "$priv/src"
  # everything except build outputs and git metadata: make may write stamps, never into the original
  tar -C "$orbis_src" --exclude=./out --exclude=./.git \
    --exclude=./graphics/mesa-orbis-ps4/src --exclude=./graphics/mesa-orbis-ps4/pkg \
    --exclude=./graphics/lib32-mesa-orbis-ps4/src --exclude=./graphics/lib32-mesa-orbis-ps4/pkg \
    --exclude=./graphics/xf86-video-amdgpu-orbis-ps4/src --exclude=./graphics/xf86-video-amdgpu-orbis-ps4/pkg \
    -cf - . | tar -C "$priv/src" -xf -
  make -C "$priv/src" container ENGINE="$SUDO $engine"
fi
$SUDO "$engine" build --pull=never --build-arg "BASE=$core_container" -t "$CTR" -f build/Containerfile.gfx build

step "3/5 packages, dependency closure, mini-root (in the container; Mesa takes 1-3 h)"
# Every assemble starts with a clean *staging repository* so outdated package
# filenames cannot slip into a new release. Preserve old logs/artifacts under
# a recoverable name; compiled package caches remain in out/pkgcache.
if [[ -d out/pack ]]; then
  mv -- out/pack "out/pack.previous.$(date +%Y%m%d-%H%M%S).$$"
fi
mkdir -p out/pack
# Persistent package cache receipts are separate from this temporary staging.
[ -f out/loader-ge/build.json ] && cp -a out/loader-ge out/pack/loader
mounts=(-v "$R":/src:z -v "$orbis_src/platform":/orbis/platform:ro,z)
args=(--staging /src/out/pack)
if [ -f "$orbis_src/out/repo/orbis.db.tar.zst" ]; then mounts+=(-v "$orbis_src/out/repo":/orbis/repo:ro,z); args+=(--orbis-repo /orbis/repo); fi
if [ -f "$orbis_src/out/rootfs/packages.txt" ]; then mounts+=(-v "$orbis_src/out/rootfs/packages.txt":/orbis/packages.txt:ro,z); args+=(--base-packages /orbis/packages.txt); fi
if [ -f "$orbis_src/out/keys/orbis-repo-key.fingerprint" ]; then
  # gpg writes into its home (trustdb, random_seed, locks): sign from a private copy of the keys,
  # readable by this user only, removed when the build ends
  rm -rf "$priv/keys" && mkdir -p "$priv" && (umask 077 && cp -a "$orbis_src/out/keys" "$priv/keys")
  mounts+=(-v "$priv/keys":/orbis/keys:z); args+=(--keys /orbis/keys)
fi
[ "$experimental" = 1 ] && args+=(--experimental)
[ "$bore" = 1 ] && args+=(--bore)
args+=(--target "$target")
$SUDO "$engine" run --rm --privileged "${mounts[@]}" -e ORBIS_DISPOSABLE_BUILD=1 \
  -e ORBIS_CONTAINER_ID="$($SUDO "$engine" image inspect --format '{{.Id}}' "$CTR")" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -w /src \
  $CTR build/in-container-build.sh "${args[@]}"
$SUDO chown -R "$(id -u):$(id -g)" out/pack

step "4/5 assemble the pack"
source_after="$(python3 build/source_snapshot.py --fingerprint)"
[[ "$source_before" == "$source_after" ]] || {
  echo 'Source tree changed during package compilation; refusing mismatched-source pack' >&2
  exit 7
}
mapfile -t ex < out/pack/experimental-state.txt
mkdir -p out/release
bp=(); [ -f "$orbis_src/out/rootfs/packages.txt" ] && bp=(--base-packages "$orbis_src/out/rootfs/packages.txt")
python3 build/assemble_pack.py --staging out/pack --out out/release --git-commit "$commit" \
  --experimental-state "${ex[0]}" --experimental-reason "${ex[1]:-}" "${bp[@]}"

step "5/5 self-check"
python3 build/orbis_src_guard.py verify "$orbis_src" out/.orbis-src-guard.json
./orbis-graphics verify --pack out/release/$PACK
( cd out/release && sha256sum -c $PACK.sha256 )
echo
echo "Pack: $R/out/release/$PACK"
echo "Experimental Mesa: ${ex[0]} (${ex[1]:-})"
echo "Next: ./orbis-graphics integrate --base-image <orbis-0.2.0 image> --pack out/release/$PACK --output <new image>"
echo "      ./orbis-graphics stage0 --base-stage0-image <orbis-0.2.0 stage-0 image> --pack out/release/$PACK --output <new stage-0 image>"
