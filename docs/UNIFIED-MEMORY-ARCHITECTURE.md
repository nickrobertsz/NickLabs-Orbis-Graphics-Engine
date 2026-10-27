# NickLabs Orbis — complete unified-memory architecture (directive D4)

This is a mandatory directive, tracked on its own. The Graphics Engine 2.0 memory work
(`GRAPHICS-ENGINE-MEMORY.md`) is part of it.

## Accounting rule

* The PS4 has one 8 GiB GDDR5 pool. Firmware and OrbisOS reserve part of it; Linux sees about
  7 GiB.
* The VRAM carve-out, the GTT budget, the dmem regions and the Vulkan heaps are *limits over*
  that one pool, not separate memory. Orbis never adds them together.
* `orbis-mem status` reports each kind of memory separately:

| Field | Meaning |
|---|---|
| `pool.linux_ram` | what Linux manages |
| `pool.vram_carveout` | fixed at boot by the loader |
| `pool.physical_estimate` | the two above added together; firmware reservations are invisible to Linux |
| `gpu.gtt_used` | GPU buffers in system pages, counted once (the device counter) |
| `gpu.cpu_visible_vram` | how much of the carve-out the CPU can map |
| `client_totals` | private per-client memory summed exactly; shared buffers only as a range (largest single client to sum over all clients), never summed as if separate |
| `reclaim` | GPU pages held in shmem backups (still RAM until the core VM swaps them) |
| `compressed` | swap and zram exactly as the kernel reports them (`mm_stat`). Compression is claimed only from these numbers |

Sources of the evidence below: the pinned kernel tree (feeRnt 6.15.4 crashnt-4.7 @108ef97 + Orbis
0001), the real Orbis 0.2.0 kernel config (`out/kernel/aeolia-belize-6.15/config`), Mesa 26.2.5,
and LLVM's AMDGPU target table.

## Status vocabulary

| Term | Meaning |
|---|---|
| Implemented | source exists |
| Integrated | wired into Orbis: units, packages, CLI, policy |
| Source-checked | the patch applies in order to the pinned tree, or a static check passed |
| Host-tested | tests pass on the build host with fixtures |
| Awaiting Nobara compilation | kernel code, compiled only on the ASUS |
| Awaiting PS4 qualification | needs the CUH-1215A |
| Hardware-limited | evidence given in the table |
| Alternative implemented | the goal is reached by another mechanism |
| Pending | not done yet |

## 1. Mechanisms U1–U28

| # | Mechanism | Category | Evidence | State |
|---|---|---|---|---|
| U1 | TTM / GEM buffer objects, VRAM + GTT domains | upstream | amdgpu on CIK | Integrated (0.2.0) |
| U2 | GTT/GART mapping of system pages, size chosen at boot | upstream | `amdgpu_ttm.c`: `gtt_size = ttm_tt_pages_limit()` or `amdgpu.gttsize` | Integrated; the GE loader writes both from `gtt.txt` (Host-tested) |
| U3 | Runtime GTT budget | custom kernel | upstream `ttm_resource_manager.size` is fixed at init; ge-0008 makes it writable | Implemented + Integrated (orbis-memd); kernel Source-checked, Awaiting Nobara compilation |
| U4 | Budget visible to Vulkan/GL | upstream | `amdgpu_kms.c`: `gtt.total_heap_size = gtt_man->size` → newly started processes see the current budget | Integrated |
| U5 | CPU visibility of the whole carve-out | upstream | `gmc_v7_0.c`: x86-64 APU path sets the aperture to all of VRAM | Integrated; reported as `gpu.cpu_visible_vram` (Host-tested) |
| U6 | GPU access to process memory without copies (userptr + HMM mirror, `VK_EXT_external_memory_host`) | upstream | `CONFIG_HMM_MIRROR=y`, `CONFIG_DRM_AMDGPU_USERPTR=y`; RADV exposes the extension when `has_userptr` | Interface Integrated. End-to-end check `orbis-mem bench` / `orbis-gfx-probe --membench` implemented (compiled here against Vulkan headers; Awaiting PS4 qualification). The contract is in §3 |
| U7 | Vulkan heap presentation | upstream, configurable | RADV APU path: 2/3 device-local + 1/3 host, or one unified heap (driconf) | Integrated per game (`memory.vulkan_heaps`). The default stays RADV's split until U27 numbers exist |
| U8 | Per-game VRAM protection and ceiling | upstream (dmem) | amdgpu registers region `vram` | Implemented + Integrated (`dmem.low` 75 %, `memory.dmem_max_mb`); Host-tested. `dmem.low` is best-effort protection |
| U9 | Per-game **GTT** ceiling (enforced, not only monitored) | custom kernel (ge-0015) | 6.15 charges no cgroup for GTT; registering the GTT manager as dmem region `gtt` lets TTM's cgroup-aware eviction enforce `dmem.max` | Implemented: ge-0015 + `memory.gtt_max_mb` in orbis-perf (Host-tested); kernel Awaiting Nobara compilation. Charged to the allocating task, so shared buffers count once |
| U10 | PSI memory pressure | upstream | GE kernel `CONFIG_PSI` | Integrated (orbis-memd, orbis-perf) |
| U11 | MGLRU + working-set protection | upstream + CachyOS | `CONFIG_LRU_GEN`, le9 (ge-0012) | Integrated |
| U12 | Compaction | upstream | `CONFIG_COMPACTION=y` | Integrated: proactiveness PC 20 / GAMING 0 / AI 40 (Host-tested) |
| U13 | zram / zswap | upstream | 0.2.0 zram-generator (zstd) | Integrated; zswap built but off, so pages are not compressed twice (zram is the swap device) |
| U14 | THP | upstream | `CONFIG_TRANSPARENT_HUGEPAGE=y` | Integrated per mode (AI: always) |
| U15 | KSM | upstream | `CONFIG_KSM=y` | Implemented as opt-in policy (`"ksm": true` in perf.json). Off by default: scanning costs Jaguar CPU and games rarely share pages. Pending measurement |
| U16 | DMA-BUF sharing | upstream | one amdgpu device: an import resolves to the same GEM object | Integrated; the shrinker never touches exported/imported buffers |
| U17 | DMA reservation / sync | upstream | dma_resv + syncobj | Integrated; the shrinker takes resv by trylock and waits for fences only outside direct reclaim |
| U18 | Per-process GPU memory accounting | Orbis | fdinfo `drm-total-*` / `drm-shared-*` | Implemented + Integrated: private exact, shared as a range, top owners (Host-tested) |
| U19 | Transparent GPU page-fault replay (HMM/SVM migrate-on-fault) | **hardware** | XNACK starts at GFX8 (gfx801); GFX7 has none. The retry-fault path `amdgpu_vm_handle_fault` exists only in `gmc_v9_0.c`/`gmc_v10_0.c` | **Hardware-limited.** Alternative implemented: explicit mapping (U6), placement (U3, U8, U9), reclaim (U25) |
| U20 | amdkfd SVM | hardware + upstream | needs U19; `# CONFIG_HSA_AMD is not set`; KFD knows Kaveri/Hawaii, not Liverpool | Hardware-limited (SVM). Not a dependency of Orbis: Vulkan compute + RustiCL are the compute path |
| U21 | Device-private memory | hardware (transparent use) | migration is fault-driven (U19) | Hardware-limited; explicit TTM moves instead |
| U22 | Scanout from GTT (smaller carve-out) | **upstream gap, hardware unproven** | GTT scanout needs DC + `gpu_vm_support`, which is off for pre-Carrizo; Liverpool uses DCE8 non-DC | Implemented as an isolated research kernel (`--research-sgdisplay`, `amdgpu.orbis_sg_scanout=2`), never packed; Awaiting Nobara compilation + PS4 experiment R-M1. Main display path unchanged |
| U23 | Runtime carve-out resize | hardware + loader | MC FB aperture programmed before Linux; DCE scans out of it; VRAM manager sized once | Hardware-limited at runtime. Alternative implemented: U24 + U3 + U25 |
| U24 | Boot-time memory-layout profiles | loader (done) | GE loader: any 16 MiB multiple, ≤5 MTRR blocks | Implemented + Integrated (Host-tested) |
| U25 | **Reclaimable idle GPU buffers (amdgpu TTM shrinker)** | custom kernel (ge-0014) | 6.15 TTM shrink helpers exist; only xe used them | Implemented (§2), Integrated (orbis-mem/orbis-memd); Source-checked; Awaiting Nobara compilation + PS4 qualification |
| U26 | Active GTT trim when the budget drops | custom kernel (ge-0014) | ge-0008 applied budgets lazily | Implemented: a lower `orbis_gtt_budget` evicts in LRU order at once; orbis-memd may go below usage only when U25 is enabled |
| U27 | Bandwidth per memory path (VRAM / GTT write-combined / GTT snooped, copies) | to measure | same DRAM through non-coherent and coherent paths | Tool implemented (`orbis-mem bench`, probe `--membench`); Awaiting PS4 qualification |
| U28 | Static free-carve-out swap tier | evaluated | strictly worse than a smaller carve-out | Rejected; its dynamic form is U25/U26 |

## 2. ge-0014: amdgpu GPU-buffer shrinker and active trim (U25/U26)

**Files.** `amdgpu_ttm.c` (hooks), `liverpool_orbis_shrinker.c/.h` (shrinker, trim, counters)
and `liverpool_orbis.c` (sysfs).

**Enabling.** The shrinker is off by default. `sudo orbis-mem reclaim enable` adds
`amdgpu.orbis_shrinker=1` to `bootargs.txt`, then reboot. It works only on PS4 hardware with
the default TTM pool.

How it works:

1. **Backup-capable buffers.**
   * User buffers (`ttm_bo_type_device`) get `TTM_TT_FLAG_EXTERNAL | EXTERNAL_MAPPABLE` plus a
     shmem backup (`ttm_tt_setup_backup`).
   * The flags are set after `ttm_sg_tt_init()`, so the page array stays.
   * Bind/populate/unpopulate tell these buffers apart from imported dma-bufs.
   * Userptr buffers drop the capability (their pages are process memory, reclaimed by the
     core VM).
   * Kernel buffers (rings, page tables, firmware) and sg imports are never eligible.
2. **Shrink.**
   * A registered memory shrinker walks the SYSTEM and TT LRUs with trylocks
     (`ttm_bo_lru_for_each_reserved_guarded`).
   * It skips exported/imported dma-bufs, TTM ghost objects, pinned buffers, and buffers
     `amdgpu_ttm_bo_eviction_valuable` rejects (KFD/encrypted/preempt).
   * It calls `ttm_bo_shrink()`: the regular TT→SYSTEM move (VM invalidation through
     move_notify, GART unbind), then the pages are copied into shmem and freed. Discardable
     buffers (`AMDGPU_GEM_CREATE_DISCARDABLE`) are purged instead.
   * Direct reclaim never waits for the GPU (`no_wait_gpu`). kswapd may wait and requests
     writeback to swap.
   * Device page mappings are cleared before pages leave and restored if nothing was taken.
3. **Restore.** The next CPU fault or GPU validation calls populate, which restores from shmem
   (`ttm_tt_restore`). A purged buffer starts fresh.
4. **Connection to reclaim and zram.** Backed-up pages are ordinary shmem pages. They leave RAM
   only when the core VM swaps them, to zram on Orbis. `orbis_gtt_reclaim` reports
   `backed_up_pages`; real compression appears only in zram `mm_stat`. Moving a buffer out of
   GTT is never reported as freed or compressed memory.
5. **Active trim.** Writing an `orbis_gtt_budget` below current usage evicts GTT buffers (LRU,
   at most 8192 per write) until usage fits. Evicted buffers sit in SYSTEM, where the shrinker
   can back them up.
6. **Counters.** `/sys/class/drm/card0/device/orbis_gtt_reclaim` holds: enabled, shrinkable,
   purgeable, backed_up, shrunk_total, purged_total, restored_total, scans, trim_evictions,
   backup_bytes_avail. Writing `scan <pages> [writeback]` runs one pass for testing.
   `orbis-mem status`/`reclaim` and orbis-memd decisions include them.

### Known risks to check in the first compile and test loop

* Lockdep in reclaim context: build once with `CONFIG_PROVE_LOCKING`.
* `page->mapping` handling on partial backups.
* Interaction with VM always-valid buffers.

### Nobara compile and PS4 verification

```sh
./orbis-ge kernel --orbis-src $ORBIS                       # compile; errors land in out/logs/kernel.log
grep -n "error:\|warning: " out/logs/kernel.log | head -50   # send this (and the full log) back if it fails
# optional debug build for the first test: append CONFIG_PROVE_LOCKING=y and CONFIG_DEBUG_VM=y to
# kernel/config/orbis-ge.fragment on a scratch branch, rebuild, boot once
# on the console after booting with amdgpu.orbis_shrinker=1:
cat /sys/class/drm/card0/device/orbis_gtt_reclaim            # enabled=1 ...
orbis-mem bench --mb 128                                     # U6/U27 numbers + zero-copy PASS/FAIL
vkcube & sleep 20                                            # or a game: hold GPU buffers
sudo orbis-mem reclaim scan 65536                            # manual pass: backed_up_pages should grow
stress-ng --vm 2 --vm-bytes 75% --timeout 60s               # real pressure: shrunk_total grows, no VK_ERROR_DEVICE_LOST
orbis-mem status --json | jq '.reclaim, .compressed'          # compare backed_up_bytes with zram orig_data_bytes
sudo dmesg | grep -i "orbis\|ttm\|amdgpu" | tail -50          # send back with orbis-debug collect
```

## 3. U6 — CPU/GPU sharing contract (GFX7, no fault replay)

| Question | Answer on Liverpool |
|---|---|
| What can be shared without copies | (a) GTT buffers mapped for both CPU (WC or cached) and GPU (GART): every host-visible Vulkan memory type. (b) VRAM, which the CPU maps entirely (U5), WC. (c) Ordinary process memory imported with `VK_EXT_external_memory_host` (amdgpu userptr, HMM mirror) or `GL_AMD_pinned_memory` |
| What the GPU can access | Only pages that are bound in GART/GPUVM before the work is submitted. There is no on-demand migration (U19): a GPU access to an unmapped address faults and stops the context |
| Ownership transitions | Explicit. CPU writes → submit (the queue submission makes host writes visible on coherent types; non-coherent types need `vkFlushMappedMemoryRanges`). GPU writes → fence or semaphore wait → host barrier (`VK_ACCESS_HOST_READ_BIT`) → CPU reads |
| Cache coherency | WC types (VRAM, GTT USWC): CPU writes are coherent after submit; CPU reads are uncached and slow, so use them for upload only. Cached (snooped) GTT types: coherent both ways and the right choice for GPU→CPU readback; GPU accesses go through the coherent path (bandwidth measured by U27). Imported host memory is cached and snooped |
| Synchronisation | dma_resv implicit sync for shared buffers (compositor); timeline semaphores / fences inside Vulkan; `orbis-run` changes none of it |
| Lifetime | The importer keeps the host allocation alive until `vkFreeMemory`. Freeing or unmapping it earlier is undefined behaviour. amdgpu's MMU notifier invalidates the GPU mapping when the CPU mapping changes |
| Pinned memory | userptr pages are pinned only while GPU work is in flight (`hmm_range_fault` + notifier), not permanently. Scanout and kernel buffers are pinned and never shrunk |
| Fallback | Without the extension, or when an import fails (alignment: `minImportedHostPointerAlignment`): use a host-visible GTT buffer (still no copy for the GPU) or a staging copy. The probe reports which path worked |

"Advertised" and "verified" are separate fields: `orbis-games caps` shows
`memory.external_memory_host` (advertised), and `orbis-mem bench` shows `zero_copy` = PASS/FAIL
(verified on this console).

## 4. Commands

```
orbis-mem status                 # accounting as above (no double counting)
orbis-mem reclaim [status|scan N|enable|disable]
orbis-mem bench [--mb 128]       # U27 bandwidth + U6 zero-copy check (stored for orbis-debug collect)
sudo orbis-mem set-boot --mode gaming
orbis-games caps --json | jq .memory
# per-game profile: "memory": {"dmem_low_mb": null, "dmem_max_mb": 768, "gtt_max_mb": 3072, "vulkan_heaps": "auto"}
orbis-perf policy ai             # compaction, THP, MGLRU, KSM
```
