# Kernel / libdrm / Mesa compatibility (Graphics Pack 1.0.0)

## The contract

| Layer | Version in the pack | From | Checked by |
|---|---|---|---|
| Kernel | **unchanged**: Orbis 0.2.0, 6.15.4 feeRnt crashnt-4.7 @ 108ef97 + Orbis 0001 (1080p60 only), bzImage SHA-256 `cbc11420…c157` | the base image | integrator refuses any other kernel |
| amdgpu config | `DRM_AMDGPU=y` (built in), `DRM_AMDGPU_CIK=y`, `DRM_AMD_DC=y`, `DEBUG_FS` (ALLOW_ALL), `HWMON`, `SENSORS_K10TEMP`; **no** `HSA_AMD` | 0.2.0 kernel config | qualify reads the live state |
| GPU firmware | CE, PFP, ME, MEC, MEC2, RLC, SDMA0, SDMA1, injected by ps4-linux-loader from the running console; nothing from the pack | loader | `gpu.firmware` |
| libdrm | ≥ 2.4.133 (Mesa 26.2.5 requires `libdrm_amdgpu >= 2.4.133`) | Arch snapshot 2026/10/20 | build stops if older; package dependency |
| LLVM | shared `libLLVM` with the AMDGPU target | Arch snapshot | Mesa build |
| Mesa stable | 26.2.5, tarball SHA-256 `794b3d…a0a5`, patches 0001/0003/0005 | `mesa/stable` | PKGBUILD hashes, `patch -F0` |
| Mesa experimental | main @ `f7dd61f78f83c9ffb3dc9c980cd9b303a5ce58b1` + the same patches | `mesa/experimental` | dry-run patch; BLOCKED when it does not apply |
| Vulkan loader, ocl-icd, clinfo, vulkan-tools, mesa-utils, libva-utils, drm_info | Arch snapshot | dependency closure | |
| Xorg driver | `xf86-video-amdgpu-orbis-ps4` from Orbis 0.2.0, unchanged | base image | |

The pack's Mesa is the same release and patch set as Orbis 0.2.0's (26.2.5-1). Differences:

| Option | 0.2.0 (26.2.5-1) | Pack (26.2.5-101) |
|---|---|---|
| RustiCL (`gallium-rusticl`) | false | **true**, in its own package `opencl-mesa-orbis-ps4` (opt-in at runtime with `RUSTICL_ENABLE=radeonsi`) |
| VA-API (`gallium-va`, `video-codecs`) | disabled / none | enabled / all (no effect while the kernel has no UVD; ready if a kernel brings it) |
| Vulkan layers | device-select | device-select, overlay (`VK_LAYER_MESA_overlay`: on-screen frame statistics, off unless enabled per app) |
| Rust crates | none | pinned by Mesa's own `subprojects/*-rs.wrap` hashes, fetched once, then `--wrap-mode=nodownload` |

`pkgrel=101` makes the pack's Mesa a normal upgrade of 0.2.0's for pacman and passes the 0.2.0
package guard. The experimental package *replaces* `mesa-orbis-ps4` (different name), which the guard
blocks unless the integrator sets `orbis pkg allow-graphics-change` for that one transaction (it does,
only with `--experimental`, and the receipt records it).

## Limits of the 0.2.0 kernel (read from the source at 108ef97)

| Feature | Status | Detail |
|---|---|---|
| Power management / clocks | **not available** | `cik.c` adds no powerplay (pp_smu) IP block for CHIP_LIVERPOOL, so there is no `power_dpm_force_performance_level`, `pp_dpm_sclk` or overdrive. Profiles skip GPU clock knobs with a recorded reason. |
| UVD (decode) / VCE (encode) | **not available** | the UVD 4.2 and VCE 2.0 blocks are commented out for Liverpool; the loader also does not supply `liverpool_uvd.bin` / `liverpool_vce.bin` |
| amdkfd / ROCm | **not available** | no `CONFIG_HSA_AMD`; ROCm does not support GCN2 in any case |
| Display | 1080p60 only | Orbis 0001 makes `ps4_bridge` advertise only 1920x1080@60 |
| ICC delays in the MN864729 enable sequence | already removed | the cause aday1 found for the HDMI black screen (ledger GFX-001) |

## Should 1.0.0 replace the kernel? No.

Every check the pack needs reads interfaces the 0.2.0 kernel already has, and no community kernel
change has been reviewed and tested on a CUH-1215A. Replacing the kernel would add risk without a
measured benefit. The pack therefore ships **no replacement kernel**, and the manifest says
`"kernel": {"replacement": false}`.

What would justify a `linux-orbis-ps4-gfx` kernel later, and where to look:

| Goal | Candidate (kernel/candidates.json) | Risk |
|---|---|---|
| HDMI black screen A/B test | `kernel/patches/available/0101` (port of aday1 0003, defaults = 0.2.0 behaviour) | low: knobs are off by default |
| GPU clock control / DPM | `sjd-6.18.21-stable`, `sjd-6.18.21-gpuwork` (SCLK forcing, low-power states) | high: 6.18 base, Belize untested, thermal risk |
| Hardware video decode | `feernt-uvd-engine`, `feernt-uvd-engine2` (6.15.4 base) | medium: same base; also needs private UVD firmware in the initramfs |
| DP/HPD robustness | `sjd-6.18.21-dprework`, `feernt-6.15.4-crashniels` | medium |
| Newer crashnt fixes | `feernt-crashnt-4.8.3` (6.15.4 base) | low to medium: same line, review the diff |
| Reference only | `sjd-7.0-clean`, `feernt-6.18.21-rmuxnet-clean` | wholesale jumps are a non-goal |

Procedure: `kernel/research-deltas.sh` (only GPU/display/bridge paths, with commit lists and
format-patch files), review, copy chosen patches to `kernel/patches/`, list them in
`kernel/patches/series`, `kernel/build-gfx-kernel.sh --orbis-src …`, rebuild the pack. The `-gfx`
kernel has its own release name (`6.15.4-orbis-ps4-gfx-…`) and modules directory; the 0.2.0 pacman
hook stages it and keeps the previous kernel as `bzImage.orbis-prev` (`orbis boot rollback-kernel`).
Note that a later reinstall or upgrade of `linux-orbis-ps4` stages the stock kernel again.

## Mesa line choice

| | Stable (default) | Experimental |
|---|---|---|
| Source | 26.2.5 release tarball | main at a pinned commit |
| Patches | 0001, 0003, 0005 | the same, dry-run checked |
| When | everyone | only to test a fix that exists only on main |
| Install | `integrate` | `integrate --experimental` (refused unless the pack says BUILT) |
| Back | `rollback`, or reflash | `rollback` (reinstalls 0.2.0 Mesa) |

Excluded patches and why: `mesa/stable/patches/provenance.json` (0002 UVD capabilities pairs with a
UVD kernel; 0004 is superseded by 0005).
