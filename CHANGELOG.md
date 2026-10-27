# Changelog

## 2.0.0-r2 (source revision, 2026-10-27): only new or changed components

Kernel (all Source-checked, Awaiting Nobara compilation):
- **ge-0014** amdgpu PS4 GPU-buffer shrinker (U25): idle GTT/system buffers are backed up to
  shmem (swappable to zram) or purged if discardable, restored on use; active GTT trim when the
  budget drops (U26); sysfs `orbis_gtt_reclaim`. Opt-in: `amdgpu.orbis_shrinker=1`.
- **ge-0015** GTT registered as dmem cgroup region `gtt`: enforced per-app GTT limits (U9).
- Research kernel `--research-sgdisplay` (GTT scanout experiment R-M1), never packed.

Userspace:
- orbis-mem: accounting without double counting shared buffers, CPU-visible VRAM, reclaim,
  swap/zram (actual `mm_stat`), dmem regions, top GTT owners; `reclaim` and `bench` commands.
  orbis-memd goes below current GTT usage only when the shrinker is enabled.
- orbis-gfx-probe `--membench`: bandwidth per memory kind, copies, end-to-end zero-copy check
  (`VK_EXT_external_memory_host`) (U6/U27).
- orbis-perf: CPU/GPU workload coordination, per-game GTT ceilings (`memory.gtt_max_mb`) next to
  the VRAM ceilings, per-region restore journal, opt-in KSM.
- orbis-games/orbis-run: shader-cache budget and pruning (+ weekly user timer), Gamescope option.

Build system:
- `./orbis-ge`: `check` step, per-step input fingerprints, per-package build cache, logs per step,
  failures not masked, optional stage-0 handled correctly, build receipt v2.
- Nobara instructions no longer mask Rust test failures.

Docs: catalog with the new status vocabulary; unified-memory classification, ge-0014 design and
verification procedure, U6 sharing contract; ledger GE-B21..B24.

Unchanged: everything else from 2.0.0; Orbis 0.2.0 and Graphics Pack 1.0.0; the LAN-only network
policy.

## 2.0.0 (NickLabs Orbis Graphics Engine, source release, 2026-10-26)

Separate release on top of Orbis 0.2.0; Graphics Pack 1.0.0 is left as published.

- Kernel series of 13 patches:
  - ge-0001…0009: Graphics Engine (fan/thermal, SCLK restore, GDDR5 info, HDMI audio DTO,
    bridge knobs, cpufreq, engine telemetry + runtime GTT budget, SMU mailbox);
  - ge-0010: wall clock from the loader;
  - ge-0011: Aeolia RTC `/dev/rtc0`;
  - ge-0012: CachyOS subset;
  - ge-0013: Jaguar build option.
- Kernel extras:
  - perf config fragment (sched_ext/BTF, NTSYNC, MGLRU, PSI, dmem, cpusets, RTC);
  - Rust-for-Linux;
  - optional BORE kernel `linux-orbis-ps4-ge-bore`.
- Loader: exact VRAM MTRR split, `vram.txt`/`gtt.txt`, boot identity, time handoff.
- Mesa 26.2.5-201 (+lib32) with the UVD caps patch (fail-closed).
- Engine (Python): orbis-memd, orbis-hwd, Black Box + orbis-debug (now with the time source and
  native reports), video, AI tuning, GE qualify, stage-0.
- Native (Rust, std only, btver2 + LTO, ISA-gated) in `orbis-native`:
  - `orbis-perf`: one controller, sched_ext management, game sessions, dmem protection and
    ceilings, compaction per mode, doctor;
  - `orbis-time`;
  - `orbis-games` + `orbis-run`: Vulkan capabilities → DXVK 3.x/2.x/1.10/WineD3D, VKD3D-Proton,
    a pinned runner catalog with an offline bundle and lock, profiles, frame-time telemetry and
    failure classification.
- `orbis-gaming` meta package; `./orbis-ge` one-command Nobara build (+ `runners`).
- Unified-memory architecture classified with evidence (docs/UNIFIED-MEMORY-ARCHITECTURE.md).
- Fixes found during integration:
  - GE-B17: the 0.2.0 ISA audit flags AVX1 permutes;
  - GE-B19: the 1.0 profile units fight orbis-perf;
  - GE-B20: lock-file lookup.

Nothing has been built on Nobara or run on a PS4 yet; see docs/DEVELOPMENT-CATALOG.md.

## 1.0.0 (source release, 2026-10-26)

First release of the NickLabs Orbis Graphics Pack for Orbis 0.2.0 on the CUH-1215A.

- Mesa 26.2.5 stable line (`mesa-orbis-ps4` 26.2.5-101, `opencl-mesa-orbis-ps4`, `lib32-mesa-orbis-ps4`)
  with PS4 patches 0001/0003/0005; RustiCL, VA-API and the Vulkan overlay layer enabled.
- Experimental Mesa line (main @ f7dd61f), recorded as BLOCKED when the patches do not apply.
- `orbis-gpu` (qualify, health, profile, capabilities) with result states PASS / FAIL / DEGRADED /
  UNSUPPORTED / NOT_RUN / BLOCKED and JSON schema `orbis-gfx-report/1`; `orbis-gpu-qualify` and
  `orbis-gpu-health` aliases; `orbis-qualify` extended with a graphics receipt.
- `orbis-gfx-probe` 1.0.0: GL pattern shader, 1080p frame timing, draw calls; Vulkan raster
  (gradient, depth) and compute oracles; pipeline cache; OpenCL integer oracle.
- Stage-0 GPU diagnosis: initramfs overlay (`orbis.gfxdiag=1`) and a GPU mini-root on the USB stick.
- Reversible performance profiles with state and a JSONL change log; no overclocking.
- Nobara integrator `orbis-graphics`: verify, inspect, integrate (image or staging rootfs), stage0,
  rollback, dry runs and receipts.
- Pack build wiring for Nobara (`build/build-pack.sh`), deterministic pack archive.
- Kernel research and optional `linux-orbis-ps4-gfx` wiring; opt-in bridge A/B patch 0101.
- Bug catalog (28 entries) and a Nobara-side GitHub updater.

Not yet built into packages or tested on a PS4; see docs/GRAPHICS-RELEASE-STATUS.md.
