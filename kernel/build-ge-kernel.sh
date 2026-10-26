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
#                             [--research-uvd]
#
# Output: out/kernel-ge/ (bzImage, config, modules-<rel>.tar, build-manifest.json, SHA256SUMS),
# release 6.15.4-orbis-ps4-ge-g<commit>. With --research-uvd the community UVD bring-up diff
# (kernel/patches/research/uvd-feernt-engine2-6.15.diff, known to fault) is added and the result
# goes to out/kernel-ge-uvd/ with release ...-orbis-ps4-ge-uvd: a separate research kernel that is
# never put into the pack automatically.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
R="$(cd "$here/.." && pwd)"
orbis_src=""; jobs=""; uvd=0; inside=0
while [ $# -gt 0 ]; do
  case "$1" in
    --orbis-src) orbis_src="$(realpath "$2")"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    --research-uvd) uvd=1; shift ;;
    --in-container) inside=1; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
series="$here/patches/series"
grep -qv '^[[:space:]]*\(#\|$\)' "$series" || { echo "kernel/patches/series is empty" >&2; exit 3; }
name=kernel-ge; [ "$uvd" = 1 ] && name=kernel-ge-uvd
if [ "$inside" = 0 ]; then
  [ -f "$orbis_src/kernel/build-kernel.sh" ] || { echo "--orbis-src must be the Orbis 0.2.0 checkout" >&2; exit 2; }
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  engine="${ENGINE:-podman}"
  mkdir -p "$R/out"
  extra=""; [ "$uvd" = 1 ] && extra="--research-uvd"
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
  cp "$here/patches/$p" "$pdir/ge-$(basename "$p")"
  echo "ge-$(basename "$p")" >> "$pdir/series"
done
frag="$work/kernel/config/orbis-ge.fragment"
cp "$here/config/orbis-ge.fragment" "$frag"
if [ "$uvd" = 1 ]; then
  cp "$here/patches/research/uvd-feernt-engine2-6.15.diff" "$pdir/ge-research-uvd.patch"
  echo "ge-research-uvd.patch" >> "$pdir/series"
  sed -i 's/^CONFIG_LOCALVERSION=.*/CONFIG_LOCALVERSION="-orbis-ps4-ge-uvd"/' "$frag"
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
python3 - "$R/out/$name/build-manifest.json" "$uvd" "$R/out/$name/config" <<'PY'
import json, sys
p, uvd, cfg = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
d = json.load(open(p))
want = "orbis-ps4-ge-uvd" if uvd else "orbis-ps4-ge"
assert ("-" + want + "-") in d["kernel_release"] or d["kernel_release"].endswith("-" + want), d["kernel_release"]
conf = open(cfg).read()
missing = [k for k in ("CONFIG_PS4_CPUFREQ=y", "CONFIG_PSI=y", "CONFIG_NETCONSOLE_DYNAMIC=y", "CONFIG_DETECT_HUNG_TASK=y",
                       "CONFIG_MAGIC_SYSRQ=y", "CONFIG_PROC_VMCORE=y") if k not in conf.splitlines()]
assert not missing, f"config fragment did not survive olddefconfig: {missing}"
d["orbis_graphics_engine"] = {"version": "2.0.0", "research_uvd": uvd, "status": "compiled; NOT boot-tested"}
json.dump(d, open(p, "w"), indent=2)
print("kernel release:", d["kernel_release"])
PY
