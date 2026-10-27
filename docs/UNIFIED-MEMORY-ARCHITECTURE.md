# NickLabs Orbis — complete unified-memory architecture (directive 4)

This directive (2026-10-26) is tracked on its own. The Graphics Engine 2.0 memory work
(`GRAPHICS-ENGINE-MEMORY.md`) counts toward it but does not satisfy it alone.

**Accounting rule.**
* The PS4 has one 8 GiB GDDR5 pool. Firmware reservations take part of it; Linux sees about 7 GiB.
* The VRAM carve-out, the GTT budget and the Vulkan heaps are *views* of that one pool. Orbis
  never adds them together as if they were extra memory.
* GTT pages are ordinary Linux pages and are counted once, on the GPU side (`orbis-mem status`).
* Vulkan heaps carry the note "same physical GDDR5" (`orbis-games caps`).

Evidence below comes from the pinned kernel tree (feeRnt 6.15.4 crashnt-4.7 @108ef97 + Orbis
0001), the real Orbis 0.2.0 kernel config (`out/kernel/aeolia-belize-6.15/config`), Mesa 26.2.5,
and LLVM's AMDGPU target documentation. Paths are relative to those trees.

## 1. Classification

The categories follow the directive:
1. hardware-impossible;
2. unsupported by upstream drivers;
3. needs a custom kernel adaptation;
4. needs a loader change;
5. achievable with other techniques;
6. not yet investigated.

| # | Mechanism | Category | Evidence | Orbis state |
|---|---|---|---|---|
| U1 | TTM / GEM buffer objects, VRAM + GTT domains | upstream, in use | amdgpu on CIK | IMPLEMENTED + INTEGRATED (0.2.0) |
| U2 | GTT/GART mapping of system pages, size chosen at boot | upstream | `amdgpu_ttm.c:1967-1982`: `gtt_size = ttm_tt_pages_limit()` or `amdgpu.gttsize` | INTEGRATED; GE loader writes `amdgpu.gttsize`/`ttm.pages_limit` from `gtt.txt` (HOST-VERIFIED) |
| U3 | **Runtime GTT budget** | 3 (custom) | `ttm_resource_manager.size` is fixed at init upstream; ge-0008 makes it writable (`orbis_gtt_budget`). Allocations above the budget get `-ENOSPC`, and TTM then evicts other GTT buffers | IMPLEMENTED (ge-0008, UNVERIFIED: applies) + INTEGRATED (`orbis-memd`, HOST-VERIFIED) |
| U4 | Budget visible to Vulkan/GL | upstream consequence | `amdgpu_kms.c:771`: `gtt.total_heap_size = gtt_man->size`, so Mesa sizes its heaps from the *current* budget for each newly started process | INTEGRATED by construction; per-process heap refresh needs a restart of the app (PENDING doc note only) |
| U5 | CPU-visible GPU buffers (whole carve-out) | upstream | `gmc_v7_0.c:388-396`: on x86-64 APUs the aperture is set to all of VRAM (`MC_VM_FB_OFFSET`), so `visible_vram_size = real_vram_size`. No small-BAR limit | INTEGRATED (0.2.0); `orbis-mem status` reports it (PENDING: add field) |
| U6 | **GPU-visible CPU memory, zero-copy (userptr + HMM mirror)** | upstream | 0.2.0 config: `CONFIG_HMM_MIRROR=y`, `CONFIG_DRM_AMDGPU_USERPTR=y`. amdgpu userptr mirrors a process range with `hmm_range_fault` + MMU interval notifiers. Mesa 26.2.5 RADV exposes `VK_EXT_external_memory_host` when `has_userptr` (`radv_physical_device.c:875`); radeonsi exposes `GL_AMD_pinned_memory` | INTEGRATED (kernel + Mesa); reported by `orbis-games caps` → `memory.external_memory_host` (BUILT, HOST-VERIFIED parser); PS4-VERIFIED pending |
| U7 | Vulkan heap presentation on the APU | upstream, configurable | `radv_physical_device.c:472-487`: for APUs RADV reports (carve-out + GTT) as 2/3 device-local + 1/3 host; driconf `radv_enable_unified_heap_on_apu` shows one heap | INTEGRATED: per-game profile `memory.vulkan_heaps = auto|unified|split` (orbis-run, HOST-VERIFIED) |
| U8 | Per-application VRAM protection / ceiling | upstream (6.15 dmem cgroup) | amdgpu registers its VRAM region with `drmm_cgroup_register_region` | `dmem.low` (75 % of VRAM) and the new `dmem.max` per game profile (`memory.dmem_max_mb`): above the ceiling the game's buffers go to GTT, the same GDDR5 reached through GART, instead of evicting other clients. IMPLEMENTED + INTEGRATED (orbis-perf, HOST-VERIFIED fixture) |
| U9 | Per-application **GTT** budget (cgroup) | 2 (upstream lacks it in 6.15) | dmem only covers VRAM. In 6.15 TTM pages are not charged to memcg | ALTERNATIVE REQUIRED. Current alternative: per-process GTT accounting from DRM fdinfo (`orbis-mem status`) + global budget. PENDING: an orbis-memd action that names the top GTT owner under pressure |
| U10 | Memory pressure monitoring | upstream | PSI (GE kernel `CONFIG_PSI`) | INTEGRATED (`orbis-memd`, `orbis-perf status`) |
| U11 | MGLRU, working-set protection | upstream + CachyOS | `CONFIG_LRU_GEN`, le9 (ge-0012) | INTEGRATED (orbis-perf, UNVERIFIED kernel) |
| U12 | Compaction | upstream | `CONFIG_COMPACTION=y` (0.2.0 config) | INTEGRATED: `vm.compaction_proactiveness` per mode (PC 20, GAMING 0, AI 40) in orbis-perf (HOST-VERIFIED) |
| U13 | zram / zswap | upstream | 0.2.0 zram-generator (zstd) | INTEGRATED (0.2.0); zswap built, off (no double compression) |
| U14 | THP | upstream | `CONFIG_TRANSPARENT_HUGEPAGE=y` | INTEGRATED per mode (AI: always) |
| U15 | KSM | upstream | `CONFIG_KSM=y` | available; not enabled by policy (CPU cost on Jaguar; PENDING measurement) |
| U16 | DMA-BUF sharing (compositor ↔ client) | upstream | same amdgpu device on both sides: the import resolves to the same GEM object, with no copy and no extra pin. `CONFIG_DMABUF_MOVE_NOTIFY` is off; it only matters for cross-device importers, and the PS4 has none | INTEGRATED (upstream behaviour) |
| U17 | DMA reservation / sync | upstream | dma_resv + syncobj (Vulkan timeline semaphores) | INTEGRATED |
| U18 | Memory ownership tracking | Orbis | DRM fdinfo per client + RSS, GTT counted once | IMPLEMENTED + INTEGRATED (`orbis-mem`, HOST-VERIFIED) |
| U19 | **Transparent GPU page-fault replay (HMM/SVM, migrate-on-fault)** | **1 (hardware)** | LLVM AMDGPU target table: XNACK (replayable GPU memory faults) is a feature from GFX8 (gfx801) onwards. gfx700-gfx705 (GFX7 / GCN 2, the generation of Liverpool) have no XNACK. In the kernel, the retry-fault path `amdgpu_vm_handle_fault` exists only in `gmc_v9_0.c`/`gmc_v10_0.c`; `gmc_v7_0.c` only logs or stops on a fault (`amdgpu_vm_fault_stop`, lines 713/977/1309) | HARDWARE-LIMITED. Software cannot replay a faulted wavefront on GFX7. Alternative: explicit mapping (U6 userptr, U3/U8 placement) |
| U20 | AMD SVM (amdkfd `svm_range`) | 1 + 2 | needs U19, and needs amdkfd. 0.2.0 has `# CONFIG_HSA_AMD is not set`; `kfd_device.c` knows Kaveri/Hawaii (GFX7) but not Liverpool | HARDWARE-LIMITED (SVM). KFD on Liverpool would be a custom port (3), but ROCm userspace no longer supports gfx7, so nothing would use it. Vulkan compute + RustiCL are the compute path |
| U21 | Device-private memory (ZONE_DEVICE migration) | 1 for transparent use | migration to device memory is driven by GPU faults (U19) | HARDWARE-LIMITED for transparent use; explicit migration = TTM moves (U3/U8) |
| U22 | Scanout from GTT (shrinks the carve-out toward scanout + firmware only) | **2 (upstream), hardware not proven** | `amdgpu_display.c:573-577`: GTT scanout needs DC and `gpu_vm_support`. `amdgpu_dm.c:1855`: `asic_type < CHIP_CARRIZO → gpu_vm_support = false`. `amdgpu_device.c:4021-4033`: Liverpool uses the non-DC DCE8 path by default. Nothing shows DCE8 *cannot* read through the MC system aperture | ALTERNATIVE REQUIRED / research R-M1: an experiment flag on the research kernel that pins a scanout BO in a GTT range mapped by the system aperture. Until then the smallest safe carve-out is chosen per mode (U24) |
| U23 | Runtime carve-out resize | 1 + 4 | the MC FB aperture is programmed before Linux by the loader. Moving it while DCE scans out of it, with amdgpu's VRAM manager sized once at init, is not supported by GFX7 or the driver (`GRAPHICS-ENGINE-MEMORY.md`) | HARDWARE-LIMITED at runtime (fixed boundary kept). Alternatives: U24, U3, U25 |
| U24 | Boot-time memory-layout profiles | 4 (done) | GE loader: any 16 MiB multiple, ≤5 MTRR blocks, `vram.txt` + `gtt.txt` | IMPLEMENTED + INTEGRATED (`orbis-mem set-boot --mode`, HOST-VERIFIED) |
| U25 | **Reclaimable GPU buffers under memory pressure (amdgpu TTM shrinker)** | **3 (custom)** | 6.15 TTM has the shrink helpers (`ttm_bo_shrink`, `ttm_tt_setup_backup`, `ttm_backup` shmem), but only `xe` registers a shrinker (`xe/xe_shrinker.c`). amdgpu's only path is the pages-limit swapout at populate time (`ttm_tt.c:385-394`), not driven by memory pressure | PENDING (design §2). This is the main new mechanism: idle GTT buffers become swappable to zram instead of pinning RAM until OOM |
| U26 | Active GTT trim when the budget shrinks | 3 | ge-0008 applies new budgets lazily (on the next allocation) | PENDING: `orbis_gtt_budget` write → `ttm_lru_walk_for_evict` down to the budget. Trimming alone only moves buffers GTT→SYSTEM (still RAM), so it only reclaims memory together with U25 |
| U27 | GDDR5 bandwidth per path (VRAM vs GTT write-combined vs GTT snooped) | 6 | the PS4 GPU reaches the same DRAM through the GPU memory path (non-coherent, full bandwidth) or the coherent path. In amdgpu terms: VRAM and USWC GTT versus cached (snooped) GTT | PENDING measurement (R-M2): Vulkan compute copy benchmark per memory type on the console; it decides whether `vulkan_heaps=unified` costs bandwidth |
| U28 | Free carve-out as a swap tier | 5 (evaluated) | a *static* VRAM-backed block device is strictly worse than a smaller carve-out (the same bytes as cached RAM instead of uncached BAR reads). Only a dynamic, TTM-evictable version would help | REJECTED in static form; dynamic form folded into U25/U26 |

## 2. Design: amdgpu TTM shrinker for the PS4 (U25 + U26)

Kernel patch ge-0014 (planned, opt-in `amdgpu.orbis_shrinker=1`, PS4 only). It follows the
upstream `xe` pattern with the same TTM helpers:

1. **Backup-capable GTT.** In `amdgpu_ttm_tt_create`, for user GTT BOs on Liverpool, set
   `TTM_TT_FLAG_EXTERNAL_MAPPABLE` semantics where allowed and call `ttm_tt_setup_backup()`.
   Skip userptr BOs (already ordinary process memory) and pinned or scanout BOs.
2. **Restore on populate.** In `amdgpu_ttm_tt_populate`, if `ttm_tt_is_backed_up(tt)`,
   restore from the shmem backup before binding to GART. This mirrors `xe_ttm_tt_populate`.
3. **Shrinker.** Register `shrinker_alloc("amdgpu-orbis")`:
   * `count_objects` = idle, unpinned, populated GTT pages;
   * `scan_objects` = `ttm_lru_walk_for_evict` over the GTT manager, calling `ttm_bo_shrink()`
     on `ttm_bo_shrink_suitable()` BOs, with `writeback` only from kswapd
     (`ttm_bo_shrink_avoid_wait()`).
   * Purgeable (`AMDGPU_GEM_CREATE_DISCARDABLE`) BOs are purged instead of backed up.
4. **Active trim (U26).** Writing a lower `orbis_gtt_budget` walks the GTT LRU and evicts until
   `usage ≤ budget`. orbis-memd then lets the shrinker reclaim the pages.
5. **Accounting.** sysfs `orbis_gtt_reclaim`: `backed_up_pages`, `shrunk_total`, `restored_total`.
   `orbis-mem status` reports them; backed-up pages are counted in swap/zram, not GTT.

Verification needs a compile and console test loop. The authoring environment refused kernel
compilation, so this patch is written and tested in the Nobara build loop
(`./orbis-ge kernel`, then the memory-pressure test R-M3: run a Vulkan app holding 2 GiB GTT,
raise pressure with `stress-ng --vm`, and check that `backed_up_pages` grows without
VK_ERROR_DEVICE_LOST).

## 3. What runs today (in this source tree)

```
orbis-mem status                 # pool, carve-out, GTT budget/usage, per-process VRAM/GTT, PSI (no double counting)
sudo orbis-mem set-boot --mode gaming   # memory-layout profile for the next boot (vram.txt + gtt.txt)
orbis-games caps --json | jq .memory    # Vulkan heaps (with the overlap note), external_memory_host, memory_budget
# per-game profile (~/.config/orbis/games.d/<game>.json):
#   "memory": {"dmem_low_mb": null, "dmem_max_mb": 768, "vulkan_heaps": "unified"}
orbis-perf policy ai             # compaction_proactiveness, THP, MGLRU per mode
```
