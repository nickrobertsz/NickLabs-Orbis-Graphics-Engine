#!/usr/bin/env bash
# NickLabs Orbis Graphics Engine 2.0 kernel: the Orbis 0.2.0 kernel (feeRnt 6.15.4 crashnt-4.7 +
# Orbis 0001) plus the Graphics Engine series in kernel/patches/series (clock restore, fan/thermal,
# HDMI audio, bridge robustness, cpufreq, telemetry + GTT budget, SMU mailbox) and the
# kernel/config/orbis-ge.fragment config (Black Box options, cpufreq, PSI).
#
# Runs the unmodified Orbis 0.2.0 kernel/build-kernel.sh on a private copy of its kernel/ folder
# inside the orbis-build:0.2.0 container (clang/LLVM), so the Orbis 0.2.0 checkout is only read.
#
#   kernel/build-ge-kernel.sh --orbis-src ~/orbis-release/nicklabs-ps4-orbis-linux [--jobs N]
#                             [--research-uvd | --research-sgdisplay | --bore]
#
# Output: out/kernel-ge/ (bzImage, config, modules-<rel>.tar, build-manifest.json, SHA256SUMS),
# release 6.15.4-orbis-ps4-ge-g<commit>. With --research-uvd the community UVD bring-up diff
# (kernel/patches/research/uvd-feernt-engine2-6.15.diff, known to fault) is added and the result
# goes to out/kernel-ge-uvd/ with release ...-orbis-ps4-ge-uvd: a separate research kernel that is
# never put into the pack automatically. With --bore the CachyOS BORE scheduler patch
# (kernel/patches/variants/bore/) is added and the result goes to out/kernel-ge-bore/ with
# release ...-orbis-ps4-ge-bore: an alternative to the default EEVDF kernel, packaged as
# linux-orbis-ps4-ge-bore (kernel/package-bore; install it to switch, `orbis boot rollback-kernel` to go back).
# The performance fragment kernel/config/orbis-perf.fragment needs pahole (BTF for sched_ext);
# it is installed into the build container from the same Arch snapshot when missing.
# With --research-sgdisplay the GTT-scanout experiment (kernel/patches/research/sg-scanout-
# liverpool-6.15.diff, unified-memory item R-M1) is added: out/kernel-ge-sg/, release
# ...-orbis-ps4-ge-sg, boot with amdgpu.orbis_sg_scanout=2; never packaged.
# Rust-for-Linux (CONFIG_RUST=y, Rust directive): the kernel needs a rustc it supports, so the
# build uses rustup's pinned toolchain RUST_KERNEL_TOOLCHAIN (default 1.87.0, contemporary with
# Linux 6.15) with rust-src, plus bindgen. --no-rust builds without it (recorded as BLOCKED).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$here/.." && pwd)"
orbis_src=""; jobs=""; uvd=0; sg=0; bore=0; rust=1; inside=0
while [ $# -gt 0 ]; do
  case "$1" in
    --orbis-src) orbis_src="$(realpath "$2")"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --research-uvd) uvd=1; shift ;;
    --research-sgdisplay) sg=1; shift ;;
    --bore) bore=1; shift ;;
    --no-rust) rust=0; shift ;;
    --in-container) inside=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
series="$here/patches/series"
grep -qv '^[[:space:]]*\(#\|$\)' "$series" || { echo "kernel/patches/series is empty" >&2; exit 3; }
[ $((uvd + sg + bore)) -le 1 ] || { echo "--research-uvd, --research-sgdisplay and --bore are separate kernels" >&2; exit 2; }
name=kernel-ge; [ "$uvd" = 1 ] && name=kernel-ge-uvd; [ "$bore" = 1 ] && name=kernel-ge-bore; [ "$sg" = 1 ] && name=kernel-ge-sg
if [ "$inside" = 0 ]; then
  [ -f "$orbis_src/kernel/build-kernel.sh" ] || { echo "--orbis-src must be the Orbis 0.2.0 checkout" >&2; exit 2; }
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  engine="${ENGINE:-podman}"
  mkdir -p "$R/out"
  extra=""; [ "$uvd" = 1 ] && extra="--research-uvd"; [ "$bore" = 1 ] && extra="--bore"; [ "$sg" = 1 ] && extra="--research-sgdisplay"
  [ "$rust" = 0 ] && extra="$extra --no-rust"
  exec $SUDO "$engine" run --rm -v "$R":/gfx:z -v "$orbis_src":/orbis:ro,z -w /gfx orbis-build:0.2.0 \
    bash -c "kernel/build-ge-kernel.sh --in-container --orbis-src /orbis ${jobs:+--jobs $jobs} $extra && chown -R $(id -u):$(id -g) /gfx/out/$name /gfx/out/$name-work"
fi

work="$R/out/$name-work"
rm -rf "$work/kernel" && mkdir -p "$work"
cp -a "$orbis_src/kernel" "$work/kernel"
ln -sfn "$orbis_src/platform" "$work/platform"
t=aeolia-belize-6.15
pdir="$work/kernel/patches/$t"
[ -z "$(tail -c1 "$pdir/series")" ] || echo >> "$pdir/series"
grep -v '^[[:space:]]*\(#\|$\)' "$series" | while read -r p; do
  [ -f "$here/patches/$p" ] || { echo "kernel/patches/$p listed in series but missing" >&2; exit 2; }
  q="ge-$(printf '%s' "$p" | tr '/' '-')"     # directory in the name: stable-6.15/0001 and ge-6.15/0001 never collide
  [ ! -e "$pdir/$q" ] || { echo "duplicate patch name $q in kernel/patches/series" >&2; exit 2; }
  cp "$here/patches/$p" "$pdir/$q"
  echo "$q" >> "$pdir/series"
done
command -v pahole >/dev/null || pacman -Sy --noconfirm --needed pahole   # BTF for sched_ext
if [ "$rust" = 1 ]; then
  rtc="${RUST_KERNEL_TOOLCHAIN:-1.87.0}"
  # rustup verifies its downloads against the signed channel manifest. The Arch "rust" package
  # conflicts with rustup; removing it only affects this throw-away (--rm) container.
  if ! command -v rustup >/dev/null; then
    pacman -Q rust >/dev/null 2>&1 && pacman -Rdd --noconfirm rust
    pacman -Sy --noconfirm --needed rustup
  fi
  command -v bindgen >/dev/null || pacman -Sy --noconfirm --needed rust-bindgen
  rustup toolchain install "$rtc" --profile minimal -c rust-src
  export RUSTUP_TOOLCHAIN="$rtc"
  echo "Rust for Linux: rustc $(rustc --version) / $(bindgen --version)"
fi
frag="$work/kernel/config/orbis-ge.fragment"
cp "$here/config/orbis-ge.fragment" "$frag"
cat "$here/config/orbis-perf.fragment" >> "$frag"
if [ "$rust" = 1 ]; then printf 'CONFIG_RUST=y\n' >> "$frag"; else printf '# CONFIG_RUST is not set\n' >> "$frag"; fi
if [ "$bore" = 1 ]; then
  for b in "$here"/patches/variants/bore/*.patch; do
    cp "$b" "$pdir/ge-variant-$(basename "$b")"
    echo "ge-variant-$(basename "$b")" >> "$pdir/series"
  done
  printf 'CONFIG_SCHED_BORE=y\nCONFIG_MIN_BASE_SLICE_NS=2000000\n' >> "$frag"
  sed -i 's/^CONFIG_LOCALVERSION=.*/CONFIG_LOCALVERSION="-orbis-ps4-ge-bore"/' "$frag"
fi
if [ "$uvd" = 1 ]; then
  cp "$here/patches/research/uvd-feernt-engine2-6.15.diff" "$pdir/ge-research-uvd.patch"
  echo "ge-research-uvd.patch" >> "$pdir/series"
  sed -i 's/^CONFIG_LOCALVERSION=.*/CONFIG_LOCALVERSION="-orbis-ps4-ge-uvd"/' "$frag"
fi
if [ "$sg" = 1 ]; then
  cp "$here/patches/research/sg-scanout-liverpool-6.15.diff" "$pdir/ge-research-sg.patch"
  echo "ge-research-sg.patch" >> "$pdir/series"
  sed -i 's/^CONFIG_LOCALVERSION=.*/CONFIG_LOCALVERSION="-orbis-ps4-ge-sg"/' "$frag"
fi
python3 - "$work/kernel/targets/$t.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["config_fragments"].append("config/orbis-ge.fragment")
d["status"] = "NickLabs Orbis Graphics Engine 2.0 kernel - compiled; NOT boot-tested"
json.dump(d, open(p, "w"), indent=2)
PY
echo "patch series:"; sed 's/^/  /' "$pdir/series"
"$work/kernel/build-kernel.sh" "$t" --out "$R/out/$name" --src "$work/src" ${jobs:+--jobs "$jobs"}
python3 - "$R/out/$name/build-manifest.json" "$uvd" "$R/out/$name/config" "$bore" "$rust" "${RUSTUP_TOOLCHAIN:-}" "$sg" <<'PY'
import json, sys
p, uvd, cfg, bore = sys.argv[1], sys.argv[2] == "1", sys.argv[3], sys.argv[4] == "1"
rust, rust_tc, sg = sys.argv[5] == "1", sys.argv[6], sys.argv[7] == "1"
d = json.load(open(p))
want = "orbis-ps4-ge-uvd" if uvd else "orbis-ps4-ge-bore" if bore else "orbis-ps4-ge-sg" if sg else "orbis-ps4-ge"
assert ("-" + want + "-") in d["kernel_release"] or d["kernel_release"].endswith("-" + want), d["kernel_release"]
conf = open(cfg).read()
missing = [k for k in ("CONFIG_PS4_CPUFREQ=y", "CONFIG_PSI=y", "CONFIG_NETCONSOLE_DYNAMIC=y", "CONFIG_DETECT_HUNG_TASK=y",
                       "CONFIG_MAGIC_SYSRQ=y", "CONFIG_PROC_VMCORE=y",
                       # performance layer: a missing one means a dependency (pahole/BTF) was absent
                       "CONFIG_SCHED_CLASS_EXT=y", "CONFIG_DEBUG_INFO_BTF=y", "CONFIG_NTSYNC=y", "CONFIG_LRU_GEN=y",
                       "CONFIG_CGROUP_DMEM=y", "CONFIG_CPUSETS=y", "CONFIG_RTC_CLASS=y", "CONFIG_CACHY=y",
                       "CONFIG_X86_PS4_JAGUAR_TUNE=y") if k not in conf.splitlines()]
bad = [k for k in ("CONFIG_RTC_DRV_CMOS=y",) if k in conf.splitlines()]
assert not bad, f"options that must stay off on a PS4: {bad}"
if bore:
    assert "CONFIG_SCHED_BORE=y" in conf.splitlines(), "BORE variant without CONFIG_SCHED_BORE"
if rust:
    assert "CONFIG_RUST=y" in conf.splitlines(), ("CONFIG_RUST was dropped by olddefconfig: `make rustavailable` "
                                                   "failed (rustc/rust-src/bindgen/libclang); fix it or build with --no-rust")
assert not missing, f"config fragment did not survive olddefconfig: {missing}"
d["orbis_graphics_engine"] = {"version": "2.0.0", "research_uvd": uvd, "research_sg_scanout": sg, "bore": bore,
                              "scheduler": "BORE (CachyOS)" if bore else "EEVDF (CachyOS defaults) + sched_ext",
                              "rust_for_linux": {"enabled": rust, "toolchain": rust_tc or None} if rust else
                                                {"enabled": False, "state": "BLOCKED", "why": "built with --no-rust"},
                              "status": "compiled; NOT boot-tested"}
json.dump(d, open(p, "w"), indent=2)
print("kernel release:", d["kernel_release"])
PY
