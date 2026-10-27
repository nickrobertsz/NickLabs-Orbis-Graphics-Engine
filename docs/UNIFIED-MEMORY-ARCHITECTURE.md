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
| U9 | Per-game **GTT** ceiling (enforced, not only monitored) | custom kernel (ge-0015) | 6.15 charges no cgroup for GTT; registering the GTT manager as dmem region `gtt` lets TTM's cgroup-aware eviction enforce `dmem.max` | Implemented: ge-0015 + ge-0016 status (`orbis_gtt_cgroup`) + `memory.gtt_max_mb`. r3: applied **before launch** by orbis-run admission (Host-tested); kernel Awaiting Nobara compilation. Charged to the allocating task, so shared buffers count once |
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
| U26 | Active GTT trim when the budget drops | custom kernel (ge-0014, reworked in ge-0016) | ge-0008 applied budgets lazily | Implemented: a lower `orbis_gtt_budget` evicts in LRU order at once (trylock cursor, skips what cannot move, capped); requested vs effective budget and the trim account are readable; an incomplete trim returns -EBUSY and orbis-mem reports applied-but-not-enforced (exit 3) |
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

## 2b. r3 corrections (audit GE-A01, A02, A03, A07–A10)

The kernel changes are Source-checked: they apply in order and pass checkpatch. They are Awaiting
Nobara compilation.

**GE-A01: CVE-2026-52949, and an eviction bug that came with it**
- The series now starts with two stable backports:
  - stable-6.15/0001, the `ttm_bo_swapout()` LRU-walk fix (upstream b2ed01e7ad);
  - stable-6.15/0002, the `ttm_bo_shrink()` LRU-walk fix (upstream 1d59f36e95f7, CVE-2026-52949).
  Without 0002, a failed backup put the buffer back in front of the walk cursor, and the shrinker
  scan could loop forever.
- Reconciling ge-0014 with these fixes exposed a second bug, fixed in ge-0016 (ledger GE-B25):
  - ge-0014 let `ttm_bo_shrink()` move buffers out of GTT through `ttm_bo_validate()`, which
    notifies the VM with `evict=false`;
  - per-VM ("always valid") buffers were therefore only marked *moved* and never revalidated, so
    the GPU could reach an unpopulated buffer.
- ge-0016 changes the shrink path:
  - it first calls `amdgpu_ttm_orbis_evict_to_system()`, which validates to SYSTEM and then calls
    `amdgpu_vm_bo_invalidate(bo, true)`, as TTM's own eviction does;
  - only after that does it call `ttm_bo_shrink(.allow_move = false)`.
- GPU reclaim stays off by default (`amdgpu.orbis_shrinker=0`).

**GE-A02: honest active trim (ge-0016)**
- `amdgpu_orbis_gtt_trim()` walks the GTT LRU with the trylock cursor, in at most two passes.
  - It skips TTM ghosts and buffers that are not eviction-valuable, which keeps KFD-fenced buffers
    in place.
  - It counts failed moves and records the first errno.
  - It stops at 8192 evictions per call and on signals.
  - It returns 0 only when usage is at or below the target.
- Pinned buffers are not on the LRU. Their size stays in the usage the trim reports.
- `orbis_gtt_budget`:
  - writes are serialized by a mutex;
  - reading it shows the effective `budget`, the `requested` value, `usage`, `state`
    (enforced, over_budget, incomplete or interrupted) and a `last_trim` line with the full
    account;
  - a write whose trim did not fit returns `-EBUSY` (or `-EINTR`), while the new limit stays
    applied to new allocations.
- `orbis-mem set-budget` reports `applied`, `enforced`, `state`, `trim` and `error`. Exit codes:
  1 = refused, 3 = applied but not enforced.

**GE-A09: GTT region status (ge-0016)**
- `orbis_gtt_cgroup` (read-only) reports one of:
  - `registered region=gtt capacity=N`;
  - `unavailable reason=param_off`;
  - `unavailable reason=no_cgroup_dmem`;
  - `unavailable reason=register_failed error=E`;
  - `unavailable reason=not_ps4`.
- It is surfaced in `orbis-mem status` (dmem.gtt_status, gtt_unavailable_reason), in
  `orbis-perf doctor` (memory.dmem_region_gtt) and in admission replies. A profile that requires
  limits refuses to start when the region is missing.

**GE-A03: limits before the game's first allocation**
- Launch through `orbis-run`, with limits in the profile:
  - orbis-run starts `systemd-run --user --scope` around a second copy of itself (stage 2);
  - stage 2 asks orbis-perf, through `/run/orbis/admit/<pid>.req` (with a random nonce), to limit
    the scope; replies come back in the root-only `/run/orbis/admit-replies/`;
  - orbis-perf (root) trusts only the limits in the request. It reads the cgroup from `/proc`,
    checks the request file's owner against the process owner, and accepts only a `.scope` of
    that user that holds nothing but stage 2;
  - it enables the dmem controller on every ancestor (systemd does not manage dmem; ledger
    GE-B28), writes `dmem.max` and reads it back;
  - stage 2 verifies `dmem.max` itself and only then `exec`s the game (Steam reaper, Proton, Wine,
    Gamescope or a native binary).
  - With `memory.require_limits: true`, any missing limit means the game is not started (exit 75).
- Games outside orbis-run get limits retroactively: only later allocations are held to them. These
  limits are applied only when the game's cgroup holds nothing but the game's process tree.
  Otherwise the action is `SKIPPED` with the reason (ledger GE-B29).

**GE-A07: accounting**
- One entry per DRM client, keyed by (drm-pdev, drm-client-id), with the PIDs and names that hold
  it.
- `accounting_check` compares the client sums with the device counters. It reports unattributed
  memory (kernel buffers) as a range and flags violations.
- Process RSS (physical RAM) is kept apart from GPU accounting.

**GE-A04, A08, A10: probe `--membench`**
- Host-pointer import is validated before allocation:
  - `vkGetPhysicalDeviceExternalBufferProperties` must report the import as supported;
  - pointer and size must be aligned to `minImportedHostPointerAlignment`;
  - the memory type must be allowed both by the host pointer's type bits and by the buffer's
    `VkMemoryRequirements`, with host-coherent types preferred;
  - the buffer's required size must fit in the imported range.
- Non-coherent types are mapped, and flushed or invalidated around CPU access.
- Every failure path frees what it created. After a device loss or timeout, the buffers are leaked
  on purpose.
- Results: PASS, FAIL, UNSUPPORTED_EXTENSION, INCOMPATIBLE_MEMORY_TYPE, ALLOCATION_FAILED,
  SYNC_FAILED, DEVICE_LOST, TIMEOUT, NOT_RUN.
- The exit codes are identical in the probe and in `orbis-mem bench`: 0 / 1 / 4 / 4 / 5 / 5 / 3 /
  3 / 2. A probe whose exit code contradicts its own JSON is reported as FAIL.

## 2c. r3.1 corrections (independent audit of r3)

Kernel (ge-0017; Source-checked, Awaiting Nobara compilation):
- **Runtime budget as its own value.** r2/r3 wrote `ttm_resource_manager.size` at runtime.
  TTM documents that field as constant, and amdgpu also reads it in `amdgpu_bo_validate_size()`,
  the info ioctl and sysfs. The budget is now `amdgpu_gtt_mgr.orbis_budget` (atomic64):
  - written only under `orbis_budget_lock`;
  - read without locks by `amdgpu_gtt_mgr_new()` through `amdgpu_gtt_mgr_limit()`.
- **Capacity.** On PS4, `manager.size` is the constant capacity: max(boot GTT size, TTM page
  limit at init), the same capacity the `gtt` dmem region is registered with. The budget starts
  at the boot GTT size, so default behaviour is unchanged. Writes are bounded by the smaller of the
  capacity and the current TTM page limit; `orbis-mem` clamps to the capacity before writing.
- **Reporting.** The info ioctl (`gtt.total_heap_size`) and `mem_info_gtt_total` report the
  effective budget, as before, so Mesa still sees dynamic GTT sizing.
- **Device state.** Budget and reclaim stores run inside `drm_dev_enter()` and refuse during a GPU
  reset or suspend.
- **Teardown.** The budget mutex is now destroyed last, on every path. It used to be destroyed
  before `ttm_resource_manager_evict_all()` and skipped when eviction failed. The sysfs files go
  first (`cik_common_hw_fini`; removal waits for running stores), then the shrinker (start of
  `amdgpu_ttm_fini`), then the manager.

Eviction path, reviewed with **no defect found**. `amdgpu_ttm_orbis_evict_to_system()` and its two
callers were checked as follows:
- the reservation is held, from the trylock LRU cursor, which also holds a reference;
- the fences are waited (`ttm_bo_wait_ctx` in the TT→SYSTEM move before the GART unbind;
  `no_wait_gpu` turns a busy buffer into a skip);
- the VM is invalidated (move_notify, then `amdgpu_vm_bo_invalidate(bo, true)`, so always-valid
  buffers go to `vm->evicted` under the shared VM reservation);
- page-table BOs are excluded through `eviction_valuable`;
- revalidation and repopulation happen on the next CS or CPU fault;
- interrupts and allocation failures leave the old placement;
- concurrent trim and reclaim cannot touch the same buffer: trylock, plus the budget mutex
  between trims.

One upstream-equivalent risk remains: the shrinker validates to SYSTEM in reclaim context, as
`ttm_bo_shrink(.allow_move=true)` does upstream. That is why reclaim stays off by default.

Zero-copy probe (compiled here, Awaiting PS4 qualification):
- **Non-host-visible imports** are never touched (VK_EXT_external_memory_host forbids access) and
  report INCOMPATIBLE_MEMORY_TYPE.
- **Coherent imports** are used directly.
- **Non-coherent imports** are flushed or invalidated through a mapping that is either the
  original pointer or a verified x86-64 alias of the same pages. Anything else is INCOMPATIBLE.
- The initial fill now happens after the import and is flushed, so stale dirty lines cannot
  overwrite GPU results (a false FAIL).
- Results and exit codes are unchanged.

Admission (Host-tested):
- requests are opened without following links and decided on the open file;
- the PID is bound to the process start time, before and after the limits are written;
- only an `orbis-game` scope of the user's own systemd manager, owned by that user, is limited;
- replies are 0600 and owned by the requester, in a 0711 root directory;
- `NOT_IN_SCOPE`, `REQUEST_FAILED` and `SCOPE_FAILED` are reported instead of silent states.

### Per-target assessment

See `HARDWARE-SUPPORT-MATRIX.md` for the full table.
- Fat and Slim (Liverpool, 18 CUs) and Pro (Gladius, 36 CUs) share the 8 GiB pool and every
  mechanism above. The Pro's default carve-out is to be set from its own qualification run.
- Baikal boards get none of the kernel mechanisms until a 6.15 Baikal flavor exists.

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
