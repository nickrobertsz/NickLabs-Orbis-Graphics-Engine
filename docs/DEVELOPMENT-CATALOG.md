# NickLabs Orbis — development catalog

Updated 2026-10-26 (evening, PDT). This catalog tracks four directives, each separately:

* **D1** Graphics Engine 2.0.
* **D2** Gaming, RTC and Nobara/CachyOS performance.
* **D3** Rust.
* **D4** Complete unified-memory architecture (added 2026-10-26).

Every mandatory requirement stays listed until it is implemented or conclusively resolved.

**P0 / P1 / P2 are execution order only.** They do not narrow the scope: the complete
architecture is the target.

## Status vocabulary

Each item has an engineering state and, separately, its evidence.

| Engineering state | Meaning |
|---|---|
| IMPLEMENTED | source code exists |
| INTEGRATED | connected to the Orbis components it belongs to (units, packages, CLI, policy) |
| VERIFIED | the behaviour has supporting evidence (the evidence label says which kind) |
| PENDING | required, not yet implemented |
| HARDWARE-LIMITED | a specific physical capability is shown to be missing (with the evidence) |
| ALTERNATIVE REQUIRED | the goal stays; another mechanism is needed |

| Evidence | Meaning |
|---|---|
| BUILT | compiled here |
| HOST-VERIFIED | tests pass on the x86 build host with fixtures |
| PS4-VERIFIED | worked on the CUH-1215A. **Nothing is at this level yet** |
| BLOCKED | cannot be done *in this environment* (reason given); never used for "impossible" |
| UNVERIFIED | written and checked as far as possible; for kernel patches: "applies" (proven to apply in order to the pinned tree, not compiled) |

Kernel code: compiling it was refused in this environment, and the refusal was respected. A
patch that applies is **not** a built kernel. The compiled kernel, modules, initramfs, loader
payloads, Mesa and packages come from the Nobara pipeline (`./orbis-ge all`); until that has
run they stay BLOCKED here.

Tests now: 92 Python host tests and 30 Rust tests (core 9, games 10, perf 8, time 3). All pass.

---

## D1 Graphics Engine 2.0

| Item | State | Evidence |
|---|---|---|
| ge-0001 fan/thermal, ge-0002 SCLK restore, ge-0003 GDDR5/clocks, ge-0004 HDMI audio, ge-0005/0006 bridge, ge-0007 cpufreq, ge-0008 telemetry + GTT budget, ge-0009 SMU mailbox | IMPLEMENTED + INTEGRATED (series, package) | UNVERIFIED (applies) |
| UVD research diff | IMPLEMENTED (research kernel only, never packed) | UNVERIFIED; known to fault (GE-B08) |
| Loader: exact VRAM MTRR split, `vram.txt`/`gtt.txt`, boot identity | IMPLEMENTED + INTEGRATED | HOST-VERIFIED (C helpers compiled from the patch) |
| Mesa 26.2.5-201 + lib32, 0002 UVD caps fail-closed | IMPLEMENTED + INTEGRATED | UNVERIFIED (patch dry-run on pinned source) |
| orbis_engine (telemetry, memory policy, controls, hwd API, Black Box, orbis-debug, video, AI campaign, kdump) | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| orbis_gfx 2.0 qualify + stage-0 overlay | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| Pack assembly, integrator, rollback fix (GE-B07), `./orbis-ge` | IMPLEMENTED + INTEGRATED | HOST-VERIFIED (integrator); `orbis-ge` syntax-checked |
| Black Box records the time source; ordered after orbis-time; `orbis-debug collect` includes native reports | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| Hardware UVD decode by default | ALTERNATIVE REQUIRED (software decode until a 6.15 UVD bring-up works) | — |

## D2 Gaming, RTC, performance

| Item | State | Evidence |
|---|---|---|
| ge-0010 wall clock from loader time | IMPLEMENTED + INTEGRATED | UNVERIFIED (applies); parser HOST-VERIFIED |
| ge-0011 Aeolia RTC `/dev/rtc0` (C, kept in C) | IMPLEMENTED + INTEGRATED | UNVERIFIED (applies) |
| Native RTC calendar without loader time | ALTERNATIVE REQUIRED until `orbis-time rtc-probe` finds the counter register on a console (the register is undocumented, not proven absent) | — |
| Loader time handoff | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| orbis-time policy + units (boot, 15-min save timer, shutdown save, LAN NTP) | IMPLEMENTED + INTEGRATED | BUILT + HOST-VERIFIED |
| ge-0012 CachyOS subset, BORE variant (`linux-orbis-ps4-ge-bore` package) | IMPLEMENTED + INTEGRATED | UNVERIFIED (applies) |
| ge-0013 Jaguar `-march=btver2` option | IMPLEMENTED | UNVERIFIED. Note: the 0.2.0 `build-kernel.sh` already passes `KCFLAGS=-march=btver2`, so this mainly documents and enforces it |
| perf fragment (sched_ext/BTF, NTSYNC, MGLRU, PSI, dmem, cpusets, RTC, I/O, net) | IMPLEMENTED + INTEGRATED | UNVERIFIED; options asserted by the build script |
| orbis-perf controller (mode policy, adaptive SCLK, thermal guard, IRQ, cpusets, THP/MGLRU/compaction, I/O, restore journal) | IMPLEMENTED + INTEGRATED (unit, package, conflicts=falcond) | BUILT + HOST-VERIFIED (fixture e2e) |
| sched_ext management (`orbis-scx@`, failure count → EEVDF, bench → auto) | IMPLEMENTED + INTEGRATED | BUILT + HOST-VERIFIED (logic) |
| Game detection (Steam reaper AppId, *.exe, orbis-run markers with owner check), per-game profiles, restore on exit | IMPLEMENTED + INTEGRATED | BUILT + HOST-VERIFIED |
| Hardware-control ownership (one owner per knob; 1.0 profile units retired; manual overrides respected; doctor flags rivals) | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| Vulkan capability detection → DXVK 3.x/2.x/1.10/WineD3D, VKD3D-Proton check (features, not version numbers) | IMPLEMENTED + INTEGRATED | BUILT + HOST-VERIFIED |
| Runner catalog (GE-Proton11-11/10-34/7-55, DXVK 3.1.1/2.7.1/1.10.3, vkd3d-proton 3.0.1), mirrors, SHA-512 + SHA-256 lock, offline bundle, `./orbis-ge runners` | IMPLEMENTED + INTEGRATED | HOST-VERIFIED (local fetch/lock/tamper/install test); real downloads on Nobara |
| orbis-run (profiles, env, shader caches, sync primitives, scope, logs, classifier incl. HTTP 404 vs Noob404) | IMPLEMENTED + INTEGRATED | BUILT + HOST-VERIFIED |
| Frame-time telemetry (MangoHud log → last.json) | IMPLEMENTED + INTEGRATED | HOST-VERIFIED (parser); MangoHud options UNVERIFIED on console |
| Gaming packages (`orbis-gaming`: Lutris, Gamescope, MangoHud, scx-scheds, lib32 deps) | IMPLEMENTED + INTEGRATED (pack meta) | UNVERIFIED (names resolve at closure time on Nobara) |
| Jaguar ISA gate (0.2.0 `isa_audit` + AVX1 correction GE-B17) | IMPLEMENTED + INTEGRATED (makepkg `check()`) | HOST-VERIFIED (btver2 passes; x86-64-v3 build fails) |
| `orbis-perf doctor` next to the unchanged `orbis doctor` | IMPLEMENTED | BUILT + HOST-VERIFIED |
| AI-mode memory safety (0.2.0 MemoryHigh 92 % + systemd-oomd drop-in) | IMPLEMENTED + INTEGRATED | UNVERIFIED |
| **Gaming internet access under the LAN-only policy** | **PENDING: needs Nick's decision.** An automatic tool that inserts a runtime firewall exception for the gaming session was denied by this environment's safety checks as weakening security, so it is not in the tree. Documented manual options stay in `GAMING.md` §6 / `orbis-games network` | — |
| CPU/GPU workload coordination beyond the session policy (e.g. GPU-bound vs CPU-bound detection from `orbis_engine_busy` + PSI) | PENDING (P1) | — |
| Shader-cache housekeeping (size cap per game is set; pruning old caches) | PENDING (P2) | — |

## D3 Rust

| Item | State | Evidence |
|---|---|---|
| Workspace (std only), release LTO, btver2 builds | IMPLEMENTED + INTEGRATED (`packages/orbis-native`) | BUILT here; package build on Nobara |
| orbis-core, orbis-time, orbis-perf, orbis-games/orbis-run | IMPLEMENTED + INTEGRATED | BUILT + HOST-VERIFIED (30 tests) |
| Rust-for-Linux (`CONFIG_RUST=y`, rustc 1.87.0 + rust-src + bindgen) | IMPLEMENTED + INTEGRATED (build script) | UNVERIFIED (kernel build BLOCKED here) |
| Rust sched_ext schedulers (scx-scheds, managed by orbis-perf) | INTEGRATED | UNVERIFIED on PS4 |
| First Orbis kernel driver in Rust | PENDING (no 6.15 abstraction for the PS4 subsystems yet; C RTC kept on purpose) | — |

## D4 Complete unified-memory architecture

Full classification with evidence: `UNIFIED-MEMORY-ARCHITECTURE.md` (U1-U28). Summary:

| Item | State | Evidence |
|---|---|---|
| U1-U2, U5, U10-U17 upstream mechanisms (TTM/GEM, GART, full CPU visibility of VRAM, PSI, MGLRU, compaction, zram, THP, KSM, DMA-BUF, dma_resv) | INTEGRATED | source/config evidence; PS4 pending |
| U3 runtime GTT budget + orbis-memd | IMPLEMENTED + INTEGRATED | kernel UNVERIFIED (applies), policy HOST-VERIFIED |
| U6 zero-copy GPU access to process memory (userptr + HMM mirror; `VK_EXT_external_memory_host`) | INTEGRATED (0.2.0 kernel config + Mesa); reported by `orbis-games caps` | HOST-VERIFIED (report) |
| U7 APU heap presentation per game (`memory.vulkan_heaps`) | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| U8 per-game VRAM protection (`dmem.low`) and ceiling (`dmem.max`) | IMPLEMENTED + INTEGRATED | HOST-VERIFIED (fixture) |
| U9 per-application GTT budget | ALTERNATIVE REQUIRED (no memcg/dmem charge for TTM pages in 6.15); fdinfo accounting today | — |
| U12 compaction per mode | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| U18 ownership accounting without double counting | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| U19 transparent GPU fault replay (HMM/SVM migrate-on-fault) | **HARDWARE-LIMITED**: GFX7 has no XNACK; the kernel has no GFX7 retry-fault path | LLVM target table; `gmc_v7_0.c` vs `gmc_v9_0.c` |
| U20 amdkfd SVM | HARDWARE-LIMITED (SVM). The KFD port is possible but has no gfx7 userspace | `kfd_device.c`, 0.2.0 config |
| U22 scanout from GTT (much smaller carve-out) | ALTERNATIVE REQUIRED / research R-M1 (blocked upstream for pre-Carrizo, hardware not proven incapable) | `amdgpu_dm.c:1855`, `amdgpu_display.c:573` |
| U23 runtime carve-out resize | HARDWARE-LIMITED at runtime (boundary kept); alternatives U24 + U3 + U25 | — |
| U24 boot-time memory-layout profiles | IMPLEMENTED + INTEGRATED | HOST-VERIFIED |
| **U25 amdgpu TTM shrinker (idle GPU buffers reclaimable to zram)** | **PENDING P0 for D4**: design done (ge-0014). Needs a compile/test loop: on Nobara | — |
| U26 active GTT trim on budget shrink | PENDING (with U25) | — |
| U27 bandwidth per memory path (VRAM / GTT-WC / GTT-snooped) | PENDING measurement R-M2 (console) | — |
| U28 static VRAM swap tier | evaluated and rejected (worse than a smaller carve-out); the dynamic form is U25/U26 | reasoning in doc |

## Pending work, in execution order

| # | Prio | Directive | Item |
|---|---|---|---|
| 1 | P0 | all | Nobara pipeline run: kernel (+bore), loader, pack (Mesa, orbis-native, orbis-gaming), images, receipt. This turns BLOCKED into BUILT |
| 2 | P0 | D4 | ge-0014 amdgpu TTM shrinker + active trim + `orbis_gtt_reclaim` accounting; orbis-mem reporting (U25/U26) |
| 3 | P0 | D2 | Gaming internet access: Nick's decision on the mechanism (see D2 table) |
| 4 | P1 | D4 | R-M1 GTT scanout experiment (research kernel), R-M2 bandwidth benchmark, U5 field in `orbis-mem status`, U9 top-GTT-owner action in orbis-memd |
| 5 | P1 | D2 | CPU/GPU workload coordination (GPU-bound/CPU-bound → SCLK/scheduler), first PS4 numbers for scheduler bench and frame times |
| 6 | P1 | D2 | RTC counter discovery on the console (`orbis-time rtc-probe`) → native calendar |
| 7 | P2 | D2 | shader-cache pruning; KSM measurement (U15) |
| 8 | P2 | D3 | first Rust kernel component once an abstraction fits |
| 9 | — | all | PS4 qualification of every item above (PS4-VERIFIED) |

## Checkpoints (none overwritten)

| Tag / folder | What |
|---|---|
| Orbis v0.2.0, Graphics Pack v1.0.0 | historical releases, untouched |
| `orbis-pre-gaming-rtc-nobara-20261026` | before D2 |
| `orbis-pre-rust-20261026` | before D3 |
| `orbis-ckpt-catalog-20261026` | first catalog + regression pass |
| `orbis-ckpt-native-memory-20261026` | this state: native packaging, docs, D4 classification (created with this catalog) |
