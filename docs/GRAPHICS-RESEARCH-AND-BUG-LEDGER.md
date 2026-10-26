# Graphics research and bug ledger (Graphics Pack 1.0.0)

The machine-readable ledger is `research/catalog.json` (schema `orbis-gfx-catalog/1`); this page is
a readable copy as of 2026-10-26. GFX-001…018 come from the research handoff; ORB-001…010 are
findings from building this pack. **None of these reports were observed on Nick's console.**

## Evidence levels

| Level | Meaning |
|---|---|
| SAME-MODEL | reported on a CUH-1215A |
| SAME-FAMILY | reported on CUH-12xx / Liverpool |
| COMMUNITY | author report or guide; hardware may differ |
| BA | Baikal/Pro hardware; a lead, not transferable |
| DOC | official/project documentation or source |
| ORBIS-SOURCE | checked by Orbis in the pinned source |
| UNRATED | collected by catalog_update.py, not reviewed |

## Ledger

| ID | Sev. | Evidence | Issue | Status | What the pack does | Sources |
|---|---|---|---|---|---|---|
| GFX-001 | P0 | SAME-FAMILY | HDMI alive at 1080p60 but all pixels black after a userspace KMS modeset (MN864729 VMUTE not cleared after disable/enable) | mitigated-in-base | 0.2.0 kernel (crashnt-4.7) already removes the in-queue ICC delays that aday1 identified; aday1 reports the prebuilt crashnt-4.7 kernel fixes it on a CUH-12xx. Pack: display.connector/display.link checks + stage-0 connector/EDID capture; opt-in kernel patch kernel/patches/available/0101 adds runtime A/B knobs (defaults = 0.2.0 behaviour). | [1](https://github.com/aday1/ps4-hdmi-blackscreen) |
| GFX-002 | P0 | COMMUNITY | Display manager / X11 / Wayland login black or grey screen; unsupported EDID; hotplug failure | monitor | stage-0 and qualify record connectors, modes, EDID header validity and link state; Orbis 0001 advertises 1080p60 only (safe fallback). | [1](https://github.com/feeRnt/ps4-linux-12xx/releases), [2](https://github.com/DionKill/ps4-linux-tutorial/blob/main/docs/issues.md) |
| GFX-003 | P0 | SAME-FAMILY | Vulkan geometry/texture corruption while 2D/UI looks fine (RADV on Liverpool 1002:9922) | open | vk.raster pixel oracles (gradient, depth test) + vk.compute numeric oracle; Mesa patch 0005 (RADV meta fill on MUBUF) included; no universal fix known. | [1](https://github.com/vinegarhq/sober/issues/2490) |
| GFX-004 | P0 | DOC | GPU shows up as software renderer / DRM missing / Liverpool not mapped in libdrm/Mesa/Xorg | addressed-in-pack | Mesa patch 0001 adds Liverpool/Gladius families; gl.renderer FAILs on llvmpipe/softpipe; vk.device requires a non-CPU RADV device (driverID 3). | [1](https://github.com/fail0verflow/ps4-radeon-patches) |
| GFX-005 | P0 | COMMUNITY | Missing/wrong GPU firmware; GFX/SDMA rings fail or time out | addressed-in-pack | gpu.firmware lists per-block versions from debugfs (CE PFP ME MEC MEC2 RLC SDMA0 SDMA1); gpu.rings; opt-in gpu.ib_test; pack contains no firmware. | Orbis source review |
| GFX-006 | P0 | BA | Kernel/libdrm/Mesa ABI mismatch after upgrades (5.4 Baikal kernel + newer libdrm) | not-applicable-but-guarded | 6.15.4 is not capped at old Mesa; contract documented in GRAPHICS-KERNEL-MESA-COMPATIBILITY.md; build refuses libdrm < 2.4.133. | [1](https://github.com/feeRnt/ps4-linux-12xx/issues/8) |
| GFX-007 | P1 | BA | Mesa upgrade regresses: 26.2.5 on Baikal had OpenGL but stalled Vulkan/UVD and was rolled back | monitor | Two Mesa lines (stable 26.2.5 + pinned main snapshot) and rollback-root.sh back to the 0.2.0 Mesa. | [1](https://github.com/meerzulee/linux-psx) |
| GFX-008 | P1 | COMMUNITY | Some apps improve with RADV_DEBUG=nocompute or amdgpu.abmlevel=0; WineD3D slow | policy | Never global: profiles never set RADV_DEBUG; documented as per-application troubleshooting only (conflicts with GPU compute). | [1](https://github.com/DionKill/ps4-linux-tutorial/blob/main/docs/issues.md) |
| GFX-009 | P1 | COMMUNITY | DPCD/DP link readiness, HPD polling and clock recovery cause intermittent black screens | research | kernel/candidates.json includes sjd-6.18.21-dprework and feeRnt 6.15.4 branches; research-deltas.sh extracts GPU/bridge diffs for review; nothing applied. | [1](https://github.com/feeRnt/ps4-linux-12xx/releases) |
| GFX-010 | P1 | COMMUNITY | Sustained performance depends on SCLK, low-power states, ASPM, IRQs, thermals | blocked-by-kernel | On the 0.2.0 kernel Liverpool has no powerplay (pp_smu) so there is no DPM/clock control: pm.clocks = UNSUPPORTED with the reason. perf.sustained (opt-in) records frame time drift and CPU temperatures. | [1](https://github.com/sony-jaguar-devs/ps4-linux) |
| GFX-011 | P1 | DOC | GPU carve-out competes with Linux RAM in unified GDDR5 | addressed-in-pack | Profiles only RECOMMEND vram_mb; changing it is the existing explicit 0.2.0 step (orbis boot set-vram, reboot). | [1](https://github.com/ps4-linux/ps4-linux-loader) |
| GFX-012 | P1 | BA | UVD H.264 decode inconsistent; stalls after upgrades | blocked-by-kernel | 0.2.0 kernel has the UVD 4.2 / VCE 2.0 IP blocks commented out for Liverpool and the loader does not supply UVD/VCE firmware: video.decode = UNSUPPORTED. feeRnt x_exp__6.15.4-uvd-engine(2) are research candidates. | [1](https://github.com/meerzulee/linux-psx) |
| GFX-013 | P1 | DOC | Vulkan/compute may enumerate but miscompute or stall | addressed-in-pack | vk.compute integer oracle checked on CPU, cl.rusticl integer kernel checked on CPU, bounded by timeouts; no ROCm dependency. | [1](https://docs.mesa3d.org/drivers/radv.html), [2](https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.2/reference/system-requirements.html) |
| GFX-014 | P2 | COMMUNITY | App/peripheral bugs look like GPU bugs (OSD, controller, HDMI audio) | addressed-in-pack | qualify keeps GL, Vulkan, compute, video and display checks separate with their own result codes. | [1](https://github.com/danyboy666/ps4-linux-12xx/releases) |
| GFX-015 | P2 | COMMUNITY | Newer kernels: experimental 1080p120, HDMI audio, clock controls | research | Opt-in only; not in 1.0.0. Candidates listed in kernel/candidates.json. | [1](https://github.com/sony-jaguar-devs/ps4-linux) |
| GFX-016 | P2 | COMMUNITY | GPU performance affected by CPU-side disk/IRQ/network load | addressed-in-pack | gl.frame_timing reports median and p95; DEGRADED when p95 > 2x median; perf.sustained is opt-in. | [1](https://github.com/feeRnt/ps4-linux-12xx/discussions/7) |
| GFX-017 | P2 | SAME-MODEL | No HDMI signal can be a physical transmitter/port defect | documented | GRAPHICS-HARDWARE-QUALIFICATION.md: check the PS4 menu shows a picture before Linux; stage-0 logs connector status independent of the display. | [1](https://www.ifixit.com/Answers/View/376181/PS4%2BCUH-1215A%2B-%2BNo%2BHDMI%2Bsignal) |
| GFX-018 | P2 | BA | 7.x Baikal GPU improvements are not a drop-in for Belize 6.15.4 | policy | Compatibility matrix is per target; 7.0-Clean is a research candidate only. | [1](https://github.com/meerzulee/linux-psx) |
| ORB-001 | P0 | DOC | Mesa 26.2.5 requires libdrm_amdgpu >= 2.4.133 (meson.build) | guarded | build/in-container-build.sh checks the snapshot's libdrm before building; mesa-orbis-ps4 depends on libdrm>=2.4.133. | [1](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-26.2.5/meson.build) |
| ORB-002 | P1 | ORBIS-SOURCE | Orbis 0.2.0 kernel (feeRnt 108ef97): CHIP_LIVERPOOL gets no pp_smu block in cik.c | blocked-by-kernel | No power_dpm_force_performance_level / pp_dpm_sclk; profile gpu_perf_level writes are skipped with a recorded reason; pm.clocks UNSUPPORTED. | [1](https://github.com/feeRnt/ps4-linux-12xx/tree/108ef9764583076cbff879f581fd598817eb880a/drivers/gpu/drm/amd/amdgpu/cik.c) |
| ORB-003 | P1 | ORBIS-SOURCE | Orbis 0.2.0 kernel: UVD 4.2 and VCE 2.0 IP blocks commented out for Liverpool | blocked-by-kernel | video.decode UNSUPPORTED (no uvd ring). | [1](https://github.com/feeRnt/ps4-linux-12xx/tree/108ef9764583076cbff879f581fd598817eb880a/drivers/gpu/drm/amd/amdgpu/cik.c) |
| ORB-004 | P1 | ORBIS-SOURCE | Loader extracts 8 GPU microcode files; liverpool_uvd.bin / liverpool_vce.bin are not extracted | documented | Hardware video would also need private firmware in the initramfs; the pack never ships firmware. | [1](https://github.com/ps4-linux/ps4-linux-loader) |
| ORB-005 | P1 | ORBIS-SOURCE | aday1 bridge patch: 2 of 8 hunks target ICC delays that crashnt-4.7 already removed | port-prepared | kernel/patches/available/0101 (applies -F0; not compile- or boot-tested; defaults = 0.2.0 behaviour). | [1](https://github.com/aday1/ps4-hdmi-blackscreen/blob/5c1c2fc1416d1aa99da408315c37f45d2ed1d7e9/patches/0003-ps4_bridge-unstrand-the-unmute.patch) |
| ORB-006 | P2 | ORBIS-SOURCE | rmuxnet/ps4-linux-12xx not reachable anonymously on 2026-10-26 | monitor | feeRnt carries a 6.18.21-rmuxnet-clean branch (candidate feernt-6.18.21-rmuxnet-clean). | [1](https://github.com/rmuxnet/ps4-linux-12xx) |
| ORB-007 | P2 | ORBIS-SOURCE | linux-psx main moved from a1910182 (Orbis Mesa patch pin) to 21380639 | review-needed | Re-diff userspace/mesa/patches against the pinned copies before the next Mesa bump. | [1](https://github.com/meerzulee/linux-psx) |
| ORB-008 | P2 | ORBIS-SOURCE | Community Mesa PKGBUILD (DionKill mesa-ps4) uses sha256sums SKIP | policy | Orbis pins every source by commit or SHA-256. | [1](https://github.com/DionKill/ps4-video-archlinux) |
| ORB-009 | P2 | ORBIS-SOURCE | Historic recipe (Ps3itaTeam) recommended SigLevel = Never | policy | Orbis repos are signed with the local key; the pack's offline pacman config uses the Orbis key when present. | [1](https://github.com/Ps3itaTeam/ps4linux-video-drivers) |
| ORB-010 | P2 | ORBIS-SOURCE | 0.2.0 kernel builds without CONFIG_HSA_AMD (no amdkfd): no ROCm/HIP path | policy | GPU compute = Vulkan (RADV) and OpenCL (RustiCL) only, both checked with numeric oracles; CPU fallback stays the default AI backend until they pass. | Orbis source review |

## Sources reviewed for this pack

| Source | Pinned at | Used for |
|---|---|---|
| feeRnt/ps4-linux-12xx | `108ef97` (tag v6.15.4_crashnt-4.7__no-built-in-fw) | the 0.2.0 kernel; read `cik.c`, `ps4_bridge.c`, `.config` for the limits above |
| feeRnt branches/tags | see `kernel/candidates.json` (ls-remote 2026-10-26) | kernel research candidates |
| sony-jaguar-devs/ps4-linux | see `kernel/candidates.json` | 6.18/7.0 GPU, clock, DP work (research only) |
| meerzulee/linux-psx | `a1910182` for the Mesa patches; main is now `21380639` (ORB-007) | Mesa patches 0001/0003/0005 |
| aday1/ps4-hdmi-blackscreen | `5c1c2fc` | GFX-001 analysis, patch 0101 port |
| fail0verflow/ps4-radeon-patches | `391335346f7b` (reference snapshot) | Liverpool patch lineage (historic, Mesa 13) |
| DionKill/ps4-video-archlinux | `57e0f8ecc7a1` (reference snapshot) | packaging comparison (uses SKIP hashes, ORB-008) |
| DionKill/ps4-linux-tutorial | `82b524952f3d` (reference snapshot) | community issue list |
| Ps3itaTeam/ps4linux-video-drivers | linked | historic packaging (SigLevel=Never, ORB-009) |
| Mesa docs (RADV, RustiCL, envvars, 26.2.0 notes) | docs.mesa3d.org | runtime options, RustiCL opt-in |
| vinegarhq/sober #2490, feeRnt #8, feeRnt discussion #7 | watched in `research/watch.json` | GFX-003, GFX-006, GFX-016 |

## Keeping it current

`research/catalog_update.py` runs on Nobara (never on the PS4). It polls the repositories and
issues in `research/watch.json` (issues, pull requests, releases, commits on listed branches),
keeps keyword matches, rates the evidence from the text, guesses severity and whether a reproducer
is given, groups mirrored issues, and adds new items to `inbox` as `new-unreviewed` with a Markdown
report in `research/reports/`. It never edits reviewed entries (it only refreshes their sources'
state and timestamps) and never downloads or applies code. Moving an inbox item into `entries`,
with an evidence level and a pack action, is a human decision.

```sh
export GITHUB_TOKEN=...            # optional, read-only; raises the limit from 60 to 5000 requests/hour
research/catalog_update.py --dry-run
research/catalog_update.py
```

Discussions (GitHub GraphQL only) and Discord/private reports are not collected; check them by hand.
