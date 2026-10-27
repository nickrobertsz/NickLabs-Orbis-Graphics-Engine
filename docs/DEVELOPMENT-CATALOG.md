# NickLabs Orbis — development catalog

Updated 2026-10-27, for source release 2.0.0-r3.1. The catalog tracks five directives:

* **D1** Graphics Engine 2.0
* **D2** Gaming, RTC and Nobara/CachyOS performance
* **D3** Rust
* **D4** Complete unified-memory architecture (mandatory)
* **D5** r3 audit corrections (GE-A01…A10) and the Universal PS4 Hardware Porting Kit

The complete-architecture directive (2026-10-26) splits the work in two:
* this environment authors and integrates the source and does only cheap checks;
* the **Nobara ASUS** compiles the kernel, Mesa, the packages and the images (`./orbis-ge all`).

P0/P1/P2 set the order of work only. Every requirement stays listed until it is done or resolved
with evidence.

## Status vocabulary

| Status | Meaning |
|---|---|
| Implemented | source exists |
| Integrated | connected to its Orbis components (units, packages, CLI, policies, build) |
| Source-checked | patch applies in order to the pinned tree, syntax/static check, or compiled here (userspace only) |
| Host-tested | tests with fixtures pass on the build host |
| Awaiting Nobara compilation | kernel, Mesa, packages or images; never compiled here |
| Awaiting PS4 qualification | needs the CUH-1215A. **Nothing is PS4-qualified yet** |
| Hardware-limited | a physical limit, shown with evidence |
| Alternative implemented | the goal is met another way |
| Pending | not done |

Tests: 148 Python host tests and 42 Rust tests, all passing.
- The Rust binaries were compiled here for btver2 and pass the Jaguar ISA gate.
- `orbis-gfx-probe` (C, `--membench` reworked in r3) was compiled here with `-Wall -Wextra`
  (no warnings) against the Vulkan headers, and passes the ISA gate. It was **not run on a GPU**:
  this host has no Vulkan driver.
- The kernel series (19 patches) applies in order on the pinned base. No kernel code was compiled
  here.

---

## D1 Graphics Engine 2.0

| Item | Status |
|---|---|
| ge-0001…0009: fan/thermal, SCLK, GDDR5, HDMI audio, bridge, cpufreq, telemetry + GTT budget, SMU | Implemented, Integrated, Source-checked, Awaiting Nobara compilation |
| Loader: VRAM MTRR split, `vram.txt`/`gtt.txt`, identity, time handoff | Implemented, Integrated, Host-tested (C helpers); payloads Awaiting Nobara compilation |
| Mesa 26.2.5-201 + lib32, UVD caps fail-closed | Implemented, Integrated, Source-checked, Awaiting Nobara compilation |
| UVD hardware decode | Alternative implemented (software decode); research kernel separate, known to fault, never packed |
| orbis_engine + orbis_gfx (telemetry, memory, controls, hwd, Black Box, debug, video, AI, qualify, stage-0) | Implemented, Integrated, Host-tested |
| One owner per hardware control; manual overrides respected; restore journal | Implemented, Integrated, Host-tested |
| Kernel/Mesa interface compatibility (libdrm ≥ 2.4.133 check, RADV GFX7 caps) | Integrated, Source-checked |

## D2 Gaming, RTC, performance

| Item | Status |
|---|---|
| ge-0010 loader wall clock, ge-0011 Aeolia RTC (C) | Implemented, Integrated, Source-checked, Awaiting Nobara compilation |
| orbis-time policy, units, LAN NTP, RTC counter probe | Implemented, Integrated, Host-tested; counter discovery Awaiting PS4 qualification |
| Native calendar from the RTC counter | Pending on the console probe. The register is undocumented, not proven absent. Alternative implemented: loader time + last-known-good + LAN NTP |
| ge-0012 CachyOS subset, BORE kernel package, ge-0013 Jaguar option | Implemented, Integrated, Source-checked, Awaiting Nobara compilation |
| orbis-perf: modes, sched_ext supervision + EEVDF fallback, IRQ, cpusets, I/O, THP/MGLRU/compaction, KSM opt-in, SCLK/thermal | Implemented, Integrated, Host-tested |
| **CPU/GPU workload coordination** (GPU-bound / CPU-bound / light / balanced → background CPUs, SCLK) | Implemented, Integrated, Host-tested (new) |
| Game detection, per-game profiles, restore on exit | Implemented, Integrated, Host-tested |
| Vulkan capability detection → DXVK/VKD3D/WineD3D by features | Implemented, Integrated, Host-tested |
| Runner catalog, lock, offline bundle, `./orbis-ge runners` | Implemented, Integrated, Host-tested; real downloads on Nobara |
| orbis-run: env, scopes, logs, classifier (HTTP 404 vs Noob404), frame-time telemetry, **Gamescope option** | Implemented, Integrated, Host-tested |
| **Shader-cache budget and pruning** (per game before launch, weekly by age and total) | Implemented, Integrated, Host-tested (new) |
| orbis-gaming packages (Steam, Wine, Lutris, Gamescope, MangoHud, lib32, scx) | Integrated; Awaiting Nobara compilation (closure) |
| Controllers: HID_STEAM / NINTENDO / xpad / uinput, SDL mapping per game | Integrated (config + orbis-run); Awaiting PS4 qualification |
| Jaguar ISA gate | Implemented, Integrated, Host-tested (btver2 passes, v3 fails) |
| **External gaming-network access** | **Pending: Nick's decision.** The LAN-only policy is unchanged; manual options are documented. An automatic firewall exception was refused by this environment's safety checks and is not in the tree |

## D3 Rust

| Item | Status |
|---|---|
| orbis-core, orbis-time, orbis-perf, orbis-games/orbis-run (std only, btver2, LTO) | Implemented, Integrated, Source-checked (compiled), Host-tested |
| Rust-for-Linux in the GE kernel (rustc 1.87.0 + rust-src + bindgen) | Integrated; Awaiting Nobara compilation |
| A first kernel component in Rust | Pending until a 6.15 abstraction fits. The C RTC driver and the C amdgpu shrinker are kept in C on purpose (they live inside C drivers) |

## D4 Unified memory

Full table with evidence: `UNIFIED-MEMORY-ARCHITECTURE.md`.

| Item | Status |
|---|---|
| U1–U5, U10–U14, U16, U17 upstream mechanisms | Integrated |
| U3 runtime GTT budget | Implemented, Integrated; kernel Awaiting Nobara compilation |
| **U25 amdgpu TTM shrinker** (ge-0014 + ge-0016 eviction fix + CVE-2026-52949 backports) | Implemented, Integrated (orbis-mem reclaim, orbis-memd), Source-checked; off by default; Awaiting Nobara compilation and PS4 qualification |
| **U26 active GTT trim** (ge-0014, reworked in ge-0016: requested vs effective budget, honest -EBUSY, trim account) | Implemented, Integrated, Source-checked (kernel), Host-tested (orbis-mem); Awaiting Nobara compilation |
| **U9 per-game GTT ceiling** (ge-0015 dmem region `gtt`, ge-0016 status, `memory.gtt_max_mb`, pre-launch admission) | Implemented, Integrated, Host-tested (admission ordering, refusal, server trust checks); kernel Awaiting Nobara compilation |
| U8 per-game VRAM protection/ceiling | Implemented, Integrated, Host-tested |
| U6 zero-copy CPU/GPU sharing: interface | Integrated (kernel + Mesa) |
| U6 end-to-end verification and contract | Implemented (`orbis-mem bench`, probe `--membench`), compiled here; Awaiting PS4 qualification |
| U18 accounting without double counting; owner diagnostics; reclaim/zram/dmem fields | Implemented, Integrated, Host-tested |
| U27 bandwidth characterisation | Implemented (tool); Awaiting PS4 qualification |
| U22 GTT scanout | Implemented as an isolated research kernel; Awaiting Nobara compilation + PS4 experiment |
| U15 KSM | Implemented (opt-in); Pending measurement |
| U19 fault replay, U20 SVM, U21 device-private migration | Hardware-limited (GFX7: no XNACK / retry faults); Alternative implemented (explicit mapping, placement, reclaim) |
| U23 runtime carve-out resize | Hardware-limited at runtime; Alternative implemented (U24 + U3 + U25) |
| U24 boot memory-layout profiles | Implemented, Integrated, Host-tested |
| U28 static VRAM swap tier | Rejected with reasoning |

## D5 r3 audit corrections

| Finding | Resolution | Status |
|---|---|---|
| GE-A01 CVE-2026-52949 | stable-6.15/0001-0002 backports first in the series. ge-0016: shrink evicts as an eviction (VM revalidation), then `ttm_bo_shrink(.allow_move=false)`. Reclaim off by default | Source-checked (apply + checkpatch); Awaiting Nobara compilation |
| GE-A02 active trim | ge-0016: trylock LRU cursor, skip/count failures, cap, signals; requested vs effective budget; -EBUSY when not enforced; orbis-mem `enforced`/exit 3 | Source-checked (kernel); Host-tested (userspace) |
| GE-A03 pre-launch admission | orbis-run stage 2 inside a fresh scope; orbis-perf admission server (owner, scope and exclusivity checks, dmem enabled down the path, read-back); exec only after verification; exit 75 for required limits; retroactive path only on exclusive cgroups | Implemented, Integrated, Host-tested (ordering test: the game starts only after the limit is in force) |
| GE-A04/A10 probe | memory requirements, alignment, type bits, external-buffer capability, coherent preference, flush/invalidate, cleanup on every path | Implemented; compiled here; Awaiting PS4 qualification |
| GE-A05 0.2.0 immutability | private copy for `make container`, private key workspace, before/after guard (exit 9) | Implemented, Host-tested |
| GE-A06 stale artifacts | provenance per output, explicit selection (BORE only with --bore, research never), images checked against provenance, receipt v3, release manifest | Implemented, Host-tested |
| GE-A07 accounting | (drm-pdev, client id) with PID sets; device-counter check; RSS kept apart | Implemented, Host-tested |
| GE-A08 results | one result vocabulary and exit codes shared by the probe, JSON and `orbis-mem bench`; NOT_RUN distinct | Implemented, Host-tested |
| GE-A09 GTT region | `orbis_gtt_cgroup` status with reason; surfaced in orbis-mem, doctor and admission | Source-checked (kernel); Host-tested (userspace) |

## D5 r3.1 audit corrections

| Finding | Resolution | Status |
|---|---|---|
| P0 false-success image verification | provenance+checksum reuse; failing step outputs; fail-closed `release-status` gate for kernel, loader, pack, image, requested stage-0; FAILED receipts; no Done | **Confirmed defect corrected**; Host-tested (real `orbis-ge` on a tree copy, 7 scenarios) |
| P1 zero-copy synchronization | host-visible required; alias verified or INCOMPATIBLE; fill after import | **Confirmed defect corrected**; compiled here; **requires console qualification** |
| P1 eviction correctness | reviewed: reservation, fences, VM invalidation, eviction lists, revalidation, interruptions, cursor, concurrency, lifetime | **Reviewed, no defect found**; remains **awaiting kernel compilation** and qualification |
| P1 dynamic GTT budget | ge-0017: separate atomic budget, constant capacity, reporting, device-state guards | **Confirmed defect corrected**; **awaiting kernel compilation** |
| P1 teardown order | ge-0017 | **Confirmed defect corrected** (lifetime hygiene); awaiting kernel compilation |
| P1 admission protocol | fd-based reads, start-time binding, private replies, safe temporaries, scope ownership/membership, failure states | **Confirmed defects corrected**; Host-tested; **requires console qualification** (systemd user scopes) |
| P2 base-image target identity | `build/target_identity.py`, exit 8, receipt evidence, recorded override | Corrected; Host-tested |
| P2 release-manifest lifecycle | run id, `current_run` vs `history` | Corrected; Host-tested |
| P2 fingerprint precision | `build/fingerprint.py`, exact package cache keys, `orbis-graphics-docs` | Corrected (+ stale-cache defect GE-B40); Host-tested |

## D5 Porting Kit

| Item | Status |
|---|---|
| Target registry: 15 targets (Fat/Slim/Pro; Aeolia, Belize, Belize 2, Baikal); schema; generated view; three-state capabilities | Implemented, Host-tested. **No target verified.** Baikal unsupported (no 6.15 flavor) |
| `orbis-hw probe` (read-only, redacting) / `identify` (confirmed, probable, conflicting, unknown) / `compatibility` / `qualify` (record + proposed registry patch, never applied) | Implemented, Integrated (orbis-graphics-engine package), Host-tested |
| `orbis-debug collect --hardware`, recovery assets per target | Implemented |
| PortForge (new, candidate, diff-trees, assist, check, review, report, promote) with provenance and review gates | Implemented, Host-tested (scenarios: 1215A, Belize Slim, Baikal Slim, unknown, conflicting, missing patch, stale artifact) |
| `./orbis-ge --target`, multi-target runs, per-target images and receipts, shared kernel/Mesa/userspace | Implemented; Awaiting Nobara run |
| CUH-2215B Baikal gap analysis (simulated inventory, real community diff) | Done: unreviewed candidate + merge notes in `research/portforge/cuh-2215b-nicklabs/` |

## Build and release system

| Item | Status |
|---|---|
| `./orbis-ge all`: check → kernel → loader → pack → image → stage-0 (optional) → verify | Implemented, Source-checked; runs on Nobara |
| Dependency-aware fingerprints per step and per package (`out/.fp`, `out/pkgcache`) | Implemented (cache logic Host-tested with a fake builder) |
| Logs per step, failures keep their exit code, no masked tests | Implemented |
| Build receipt v3 per target + `out/RELEASE-MANIFEST.json`; provenance per output; explicit artifact selection; 0.2.0 checkout guard | Implemented, Host-tested (selection, guard); Awaiting Nobara run |
| Rollback: original images untouched, rollback-root, kernel rollback, `orbis-perf restore` | Integrated |

## Next actions

| Owner | Action |
|---|---|
| Nobara | `./orbis-ge all …`, then send `out/logs/*.log` for any failure (kernel compile errors in `out/logs/kernel.log`) |
| PS4 | `orbis-hw identify`, `orbis-gpu qualify`, `orbis-mem bench`, `orbis-hw qualify --target cuh-1215a`, `orbis-perf doctor`, `orbis-time rtc-probe` (with `ps4_rtc.hw_probe=1`), the ge-0014 test in `UNIFIED-MEMORY-ARCHITECTURE.md` §2, then `orbis-debug collect --hardware` |
| CUH-2215B | `orbis-hw probe --json` (or `lspci -nn` under the 5.4.247 Baikal kernel) to replace the simulated inventory; review PortForge candidate c001 |
| Nick | decision on outbound gaming network access |
| Jaleco | fix whatever the logs show; then measured defaults (scheduler, heap policy, KSM) from the PS4 numbers |

## Checkpoints (none overwritten)

Orbis v0.2.0, Graphics Pack v1.0.0, `orbis-pre-gaming-rtc-nobara-20261026`,
`orbis-pre-rust-20261026`, `orbis-ckpt-catalog-20261026`, `orbis-ckpt-native-memory-20261026`,
`orbis-pre-architecture-20261026` (state as delivered in 2.0.0),
`orbis-ckpt-architecture-r2-20261027` (2.0.0-r2), `orbis-pre-r3-20261027` (r2 before this work),
and `orbis-ckpt-r3-porting-kit-20261027` (2.0.0-r3), `orbis-pre-r3.1-audit-fixes-20261027` (r3 before
the r3.1 corrections) and `orbis-ckpt-ge-r3.1-audit-corrected-20261027` (this release).
