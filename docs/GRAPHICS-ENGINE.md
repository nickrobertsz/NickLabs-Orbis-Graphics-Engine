# NickLabs Orbis Graphics Engine 2.0.0 — engineering report

Release date of this source: 2026-10-26. Target: Sony PS4 CUH-1215A (Belize, Liverpool GPU),
on top of NickLabs PS4 Orbis Linux 0.2.0. Second target (CUH-2215B, Baikal) is not qualified.

This is a separate release. It does not modify the Orbis Linux 0.2.0 sources or images, nor the
Graphics Pack 1.0.0 release. It produces its own pack (`ORBIS-GRAPHICS-ENGINE-v2.0.0.tar.zst`),
a new bootable image made from a copy of the 0.2.0 image, and a new stage-0 image.

**Proof level.** Every kernel patch applies in order to the pinned 0.2.0 kernel tree. None was
compiled here: compiling third-party kernel code was refused in this build environment, and that
refusal was respected. All userspace code runs under 92 host unit tests on fixture trees (the
loader patch's C helpers are compiled and run from the patch text). Nothing has run on a PS4.
The first boot on the CUH-1215A is the qualification, using the commands in
[GRAPHICS-ENGINE-NOBARA.md](GRAPHICS-ENGINE-NOBARA.md).

## 1. What the release consists of

| Layer | Component | Where | State |
|---|---|---|---|
| Kernel | `linux-orbis-ps4-ge` = 0.2.0 kernel + 9 patches + config fragment | `kernel/` | applies; build on Nobara |
| Loader | ps4-linux-loader d0f4d4c + memory-contract patch | `loader/` | C helpers tested; payloads built on Nobara |
| Mesa | 26.2.5 + linux-psx 0001/0002/0003/0005, pkgrel 201 | `mesa/stable`, `mesa/lib32` | 0002 dry-run applied to the pinned source |
| Tools | `orbis-graphics` 2.0.0 (qualify, health, profiles, stage-0) | `diag/orbis_gfx` | tested |
| Engine | `orbis-graphics-engine` 2.0.0 (memory policy, hardware API, Black Box, video, AI) | `diag/orbis_engine` | tested |
| Integration | pack builder, image integrator, rollback, stage-0 composer, `./orbis-ge` | `build/`, `integrator/` | tested with fixtures; image steps run on Nobara |

### The kernel series (`kernel/patches/series`)

| Patch | Origin | What it does | Default |
|---|---|---|---|
| ge-0001 fan/thermal | rmux (linux-psx 7.2) | hwmon `ps4_fan`: APU temperature from the EMC, fan threshold read/write over ICC (read-modify-write of the 52-byte config) | keeps the console's own threshold |
| ge-0002 SCLK restore | rmux liverpool_clk (sony-jaguar-devs GpuWork) | restores the GPU engine clock divider that ps4-kexec's GFX soft reset leaves at the fused startup value; sysfs `ps4_sclk`, debugfs `amdgpu_liverpool_clk` | restore ON (stock clock, not an overclock); `amdgpu.ps4_sclk_restore=0` disables |
| ge-0003 VRAM type/clocks | rmux | reports GDDR5 and the real default clocks to userspace | default SCLK reported only after a successful restore |
| ge-0004 HDMI audio | linux-psx 0175 + 0054 | programs the audio DTO in modeset (fixes audio at 0.615× speed) and enables DIG1 on the ATI HDMI codec | on |
| ge-0005 bridge A/B | aday1 | MN864729 un-mute knobs | all off (0.2.0 behaviour) |
| ge-0006 bridge robustness | rmux | DPCD-gated detect, polling, enable retries with bridge reset, DPCD wait, retrain, 720p fallback mode, fixed-link CR/EQ bypass, debugfs `ps4_bridge_status` | all off (0.2.0 behaviour) |
| ge-0007 cpufreq | rmux | `ps4-cpufreq` (Jaguar hardware P-states via MSRs; only P-states the firmware enabled) | built in, PS4 boots only |
| ge-0008 telemetry + GTT budget | NickLabs Orbis (new) | `orbis_engine_busy` (GRBM/SRBM sampling) and a runtime-writable `orbis_gtt_budget` | read-only telemetry; budget = kernel default until something writes it |
| ge-0009 SMU mailbox | rmux | SMC mailbox / memory P-state messages | sysfs absent unless `amdgpu.ps4_smu_mailbox=1` |

Config fragment `kernel/config/orbis-ge.fragment`: release suffix `-orbis-ps4-ge`, PS4 cpufreq and
governors, PSI, SysRq, soft-lockup/hung-task/workqueue watchdogs, netconsole (dynamic, extended),
crash dump / vmcore / kexec_file, devcoredump, printk time. `build-ge-kernel.sh` checks that these
options survive `olddefconfig`.

Research only (never packed): `kernel/patches/research/uvd-feernt-engine2-6.15.diff` — feeRnt's
6.15 UVD bring-up, whose own log reports VM faults. `./orbis-ge kernel --research-uvd` builds it
as a separate `-orbis-ps4-ge-uvd` kernel for experiments (see the video report).

## 2. Unified CPU/GPU memory

Full design: [GRAPHICS-ENGINE-MEMORY.md](GRAPHICS-ENGINE-MEMORY.md). In short:

* **Boot contract (loader).** The VRAM carve-out is fixed before Linux starts: E820 reservation,
  the GPU framebuffer window (`MC_VM_FB_LOCATION`, 16 MiB granularity), `CONFIG_MEMSIZE` and a
  uncached MTRR (amdgpu maps VRAM write-combining through PAT). The stock loader computes one MTRR mask, which is only valid for
  power-of-two sizes: **3072 MiB yields an invalid MTRR** (ledger GE-B01). The GE loader patch
  splits the carve-out into up to five power-of-two MTRR blocks, validates `vram.txt` (16 MiB
  multiples, 64–4096 MiB), reads `gtt.txt` (chrisripp semantics: `ttm.pages_limit` +
  `amdgpu.gttsize`) and appends a boot identity (`orbis.loader=ge2 orbis.vram_mb=N`) so Linux can
  check the contract.
* **Runtime.** Changing the carve-out while Linux runs is not possible (investigated: E820,
  the MC aperture and amdgpu's VRAM manager are all fixed at init). Everything outside the
  carve-out is ordinary RAM; GPU buffers there are GTT buffers. ge-0008 makes the GTT budget
  writable at runtime, and TTM's global limit is already a runtime parameter. `orbis-memd`
  moves the budget with the PC/GAMING/AI mode and with memory pressure (PSI). When memory is
  calm it never goes below the budget the kernel chose at boot.
* **Ownership.** `orbis-mem status` reports the carve-out, GTT use and budget, Linux RAM and every
  process's GPU memory (DRM fdinfo), without counting a page twice.
* **Not possible on GFX7.** Recoverable GPU page faults (HMM / SVM / CUDA-style on-demand
  migration) need hardware Liverpool does not have. Migration stays explicit (TTM placement and
  eviction).

## 3. GPU clocks, power and thermals

* Liverpool has no powerplay block in this kernel, so the standard DPM files
  (`power_dpm_*`, `pp_dpm_sclk`) do not exist; qualify reports `pm.clocks` UNSUPPORTED with that
  reason. The GE kernel adds what the community found workable instead:
  * `ps4_sclk`: two hardware-defined states only, `max` (DID 1, full SPLL output: the clock GameOS
    runs at) and `startup` (the fused divider kexec leaves). The SPLL and voltages are never
    touched. qualify `pm.sclk` is PASS at `max` and DEGRADED at `startup`.
  * `ps4_fan` hwmon: APU temperature and fan threshold (45–85 °C accepted by Orbis tools; the EMC's
    own thermal shutdown stays active regardless).
  * `ps4-cpufreq` for the CPU.
  * The SMU mailbox (memory P-states) is an expert, opt-in interface; Orbis tools only read it.
* Profiles (`orbis-gpu profile`): `performance` sets SCLK max and a 70 °C fan threshold;
  `compute` max / 72 °C; `quiet` startup clock; `balanced` and `diagnostic` change nothing. Each
  change is logged and `orbis-gpu profile reset` / `orbis-hw reset` restore the previous values.
* Clock gating: the CIK clock-gating flags for Liverpool are left as the 0.2.0 kernel sets them;
  no gating change is made without hardware evidence (ledger GE-B10).

## 4. Video decode

Full report: [GRAPHICS-ENGINE-VIDEO.md](GRAPHICS-ENGINE-VIDEO.md). The default GE kernel, like
0.2.0, has **no UVD ring**. Every codec is therefore labelled *software (CPU)*, and
`orbis-video test` returns UNSUPPORTED. With a UVD kernel, H.264 counts as hardware only when the
kernel exposes the ring **and** VA-API lists the profile **and** a decode through VA-API surfaces
matches a software decode frame for frame. Mesa pkgrel 201 includes linux-psx 0002, which only
advertises AVC on Liverpool when the kernel reports a UVD queue and AVC caps (it fails closed).
VCE encode: absent in every Orbis kernel. HEVC, VP9 and AV1 are not in the hardware.
`orbis-video configure` writes an mpv config that matches what was proven.

## 5. Display

ge-0004 fixes the HDMI audio sample-rate defect (DTO) and DIG1. ge-0005 and ge-0006 bring the
community bridge fixes as A/B knobs that all default to 0.2.0 behaviour, so the release cannot
regress a display that works today. The fallback path stays the 0.2.0 one (1080p60 only, Orbis
0001), plus an opt-in 720p mode (`amdgpu.ps4_bridge_extra_modes=1`). Recommended knob sets for a
black screen are in the Nobara guide. Matching set: kernel 6.15.4-orbis-ps4-ge, the libdrm from the
Arch snapshot 2026/10/20 (≥ 2.4.133, enforced), Mesa 26.2.5-201, and the 0.2.0 initramfs firmware
(unchanged; the pack carries none).

## 6. AI compute

Report: [GRAPHICS-ENGINE-AI.md](GRAPHICS-ENGINE-AI.md). `orbis-ai-gpu campaign` runs eleven
llama.cpp Vulkan variants through the Orbis 0.2.0 inference harness. Each variant uses only knobs
that exist in llama.cpp b11845, each targets the long-prefill fault class Carlos Georges reported,
and each run compares against a CPU reference (KL divergence, intermediate-tensor diff with the first
diverging tensor named, server greedy output identity). `recommend` chooses a variant only when it
is numerically correct. `apply` writes the variant into the AI service and the GPU gate receipt.
The CPU stays the backend until the gate passes on a hardware GPU. RustiCL is qualified by
`orbis-gpu qualify` (integer oracle). There is no ROCm path (GFX7 has no amdkfd).

## 7. NickLabs Orbis Black Box

Report: [GRAPHICS-ENGINE-BLACKBOX.md](GRAPHICS-ENGINE-BLACKBOX.md). A recorder starts with
`sysinit` and writes every record with fsync, so nothing depends on a graceful shutdown. It keeps
a per-boot record, a flight log (telemetry every 5 s), a kernel event log with GPU-state captures
on faults and resets, devcoredump harvesting, and a verdict on the previous boot. Optional extras:
LAN streaming (kernel netconsole + JSON events) to the Nobara with `orbis-blackbox-receiver`, and an
experimental kdump capture kernel. Front end: `orbis-debug collect | export | last-crash | gpu |
memory | video | boots | stream | kdump`, each with text and `--json` output.

## 8. Hardware management backend

* CLI: `orbis-hw status | telemetry | capabilities | set <knob> <value> | reset`,
  `orbis-mem status | plan | set-boot | set-budget`, plus the 1.0.0 `orbis-gpu profile`.
* API: `orbis-hwd` (HTTP+JSON on 127.0.0.1:8421; writes need the token in `/etc/orbis/hwd-token`;
  a LAN bind is possible but always requires the token and refuses non-private clients). Endpoints
  are listed in `diag/orbis_engine/hwd.py`. This is the backend for a future settings UI.
* Modes: the 1.0.0 profile units hook into `orbis-pc/gaming/ai.target`; `orbis-memd` follows
  `/var/lib/orbis/desired-mode`.
* Every interface the running kernel lacks is reported as unavailable, together with the patch
  that adds it. No value is ever estimated.

## 9. Native PS4 reverse engineering

Documentation only: [GRAPHICS-ENGINE-RE-WORKFLOW.md](GRAPHICS-ENGINE-RE-WORKFLOW.md) (Ghidra on
the ASUS laptop, own-console dumps, clean-room rules). GameOS does not run under Linux and nothing
here tries to run it.

## 10. Image integration

`./orbis-ge all` (Nobara) builds the kernel, the loader payloads and the pack, then writes:

* a new full image, from a copy of the 0.2.0 image. The GE kernel becomes `bzImage` and the 0.2.0
  kernel is kept as `bzImage.orbis-gfx-prev`; the initramfs gets the stage-0 overlay; the GE
  payloads go to `orbis-ge-loader/`; the services are enabled; receipts and checksums are written;
* a new stage-0 image, booting the GE kernel and carrying the extended GPU/engine evidence.

The original images are never opened for writing. `./orbis-graphics rollback` produces a restored
image, and on the console `rollback-root.sh` does the same.

## 11. Result states

Everything uses `PASS / FAIL / DEGRADED / BLOCKED / UNSUPPORTED / NOT_RUN`. UNSUPPORTED and BLOCKED
always carry the reason. The current state of every item is in
[GRAPHICS-ENGINE-RELEASE-STATUS.md](GRAPHICS-ENGINE-RELEASE-STATUS.md); hardware capabilities are in
[GRAPHICS-ENGINE-HARDWARE-CAPABILITIES.md](GRAPHICS-ENGINE-HARDWARE-CAPABILITIES.md); defects are
in [GRAPHICS-ENGINE-BUG-LEDGER.md](GRAPHICS-ENGINE-BUG-LEDGER.md).

## 12. Licensing and provenance

Kernel patches keep their GPL-2.0 (ps4_fan, cpufreq, SMU, bridge) or MIT (liverpool_clk) origins
and authors; the new ge-0008 is GPL-2.0 like amdgpu. The loader patch is GPL-compatible with the
loader. Mesa patches are MIT. The NickLabs tools are MIT. Every source is pinned by commit or
SHA-256 in `manifest/sources.json` and `mesa/stable/patches/provenance.json`. The pack contains
**no PS4 firmware**: images keep the firmware of the Orbis initramfs they were built from (private,
from Nick's own console). GPL source correspondence: the pack includes the complete source
archive of this tree. The kernel is built from the pinned feeRnt commit plus the patches listed
here, and the build manifest records exactly that.
