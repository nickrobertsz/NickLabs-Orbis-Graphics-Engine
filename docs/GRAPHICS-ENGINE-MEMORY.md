# Unified CPU/GPU memory on the PS4 (Graphics Engine 2.0)

## The hardware

The CUH-1215A has one 8 GiB GDDR5 pool shared by the Jaguar CPU and the Liverpool GPU. Linux sees
roughly 7 GiB after firmware reservations. The GPU reaches memory through two windows:

* **VRAM (carve-out).** A physically contiguous range that the loader reserves before Linux
  starts (the ps4-linux-loader places it at physical 4 GiB) and is mapped through the MC framebuffer aperture.
  amdgpu manages it as VRAM.
* **GTT.** Any Linux page mapped into the GPU's address space through GART / GPUVM. On Liverpool,
  which amdgpu flags as an APU (`AMD_IS_APU`), Mesa (radeonsi, RADV) treats memory as unified and
  places many buffers in GTT already.

## The boot-time contract (loader)

| Item | Programmed by | Constraint |
|---|---|---|
| E820 reservation of the carve-out | loader | contiguous, before the kernel starts |
| `MC_VM_FB_LOCATION` | loader | 16 MiB granularity |
| `CONFIG_MEMSIZE` | loader | MiB |
| uncached (UC) MTRR over the carve-out | loader | each MTRR covers a power-of-two size aligned to that size; amdgpu's own CPU mappings of VRAM are write-combining through PAT (PAT WC overrides MTRR UC) |

**Stock loader defect (GE-B01).** The stock loader computes a single MTRR mask with
`~(size - 1)`, which is only correct for power-of-two sizes. 3072 MiB (allowed by some 0.2.0
guidance) produces an invalid mask. The **GE loader** splits the carve-out into at most five
aligned power-of-two blocks (MTRRs 3–7; it reads the variable-MTRR count first). It accepts any
16 MiB multiple between 64 and 4096 MiB whose binary form has five or fewer set bits; for example
384, 768, 1536 and 3072 are all valid.

The GE loader also reads `gtt.txt` from the boot partition, a size in MiB from 256 to 7680, as
introduced by chrisripp/ps4-linux-payloads. It appends `ttm.pages_limit=<MiB*256>` and
`amdgpu.gttsize=<MiB>` to the command line. Last, it adds a boot identity:
`orbis.loader=ge2 orbis.vram_mb=N [orbis.gtt_mb=N]`. Qualify uses that identity to
cross-check the carve-out amdgpu actually sees (`memory.loader_contract`).

## Runtime resizing: investigated, not possible

Moving the carve-out while Linux runs would mean all of the following:
1. Removing (or adding) physical pages from Linux: memory hot-remove of a reserved E820 range
   that was never online, or offlining online memory in use.
2. Reprogramming the MC aperture while the display engine scans out of it.
3. Rebuilding amdgpu's VRAM manager (sized once at init) with live buffers.

None of these is supported by amdgpu on GFX7, and getting them wrong corrupts memory, so the
carve-out stays a boot-time choice (`orbis-mem set-boot`, then reboot). Flexibility while running
comes from the GTT side.

## Runtime GTT budget (kernel ge-0008 + orbis-memd)

* `/sys/class/drm/card0/device/orbis_gtt_budget`
  * Read it to get `budget usage ttm_pages_limit min vram_carveout`.
  * Write a byte count to set the budget. Accepted values run from 256 MiB to the TTM limit.
  * The value is enforced by `amdgpu_gtt_mgr` for new allocations; existing buffers are not
    moved.
* `/sys/module/ttm/parameters/pages_limit` caps every GTT user. It is writable at runtime, and
  `orbis-mem` raises it first when a larger budget needs it.
* `orbis-memd` (systemd `orbis-memd.service`) runs one decision every 5 s:

| Situation | Decision |
|---|---|
| memory PSI `some avg10` ≥ 10 % | shrink by 10 % (never below 512 MiB or current use) |
| PSI ≤ 1 % for 60 s, or right after a mode switch | move halfway toward the mode target |
| otherwise | hold |

Mode targets (fraction of Linux RAM for GPU buffers, RAM always left to Linux). These are policy
starting points, recorded with every decision in `/var/log/orbis/memory-policy.jsonl`:

| Mode | GTT target | Linux headroom | Recommended carve-out |
|---|---|---|---|
| PC | 40 % | 1.5 GiB | 512 MiB |
| GAMING | 60 % | 1 GiB | 1024 MiB |
| AI | 80 % | 0.75 GiB | 1024 MiB |

When memory is calm the budget never goes below the value amdgpu chose at this boot, so no mode is
less generous to the GPU than the stock kernel. Only memory pressure shrinks it.

## Ownership tracking

`orbis-mem status` (and `GET /v1/memory`) reports:

* the pool: the carve-out and Linux RAM;
* the GPU side: VRAM used, GTT used, the budget and the TTM limit;
* the Linux side: RAM available, RAM in use excluding GTT, and memory PSI;
* per process: VRAM, GTT and CPU-domain GPU memory from DRM fdinfo, plus RSS. One entry per DRM
  client, even when a process holds several file descriptors.

GTT pages are counted once, on the GPU side.

## Comparison with other unified-memory systems

| System | How CPU/GPU memory is shared | Why it does or does not transfer to Liverpool |
|---|---|---|
| Apple Silicon + Asahi (AGX) | no carve-out; all GPU memory is ordinary pages mapped by the GPU's own MMU (GEM shmem) | Liverpool still needs a carve-out for scanout and firmware-visible structures, but the principle (small VRAM, everything else mapped system memory) is what the GTT policy implements |
| NVIDIA UVM / HMM (Pascal and later) | one virtual address space; GPU page faults are recoverable, pages migrate on demand | requires recoverable GPU faults; GFX7 VM faults are fatal to the context → **not possible** |
| AMD SVM (amdkfd, GFX9+ with XNACK) | shared virtual memory with retry faults | needs amdkfd and retry faults; the PS4 kernel has no HSA/KFD and GFX7 has no retry → **not possible** |
| Integrated AMD APUs on PCs (radeonsi/RADV) | small BIOS carve-out + large GTT | the same model as here; the GE adds a runtime budget and pressure policy, which stock amdgpu does not have |

**Software alternatives implemented:** a small or right-sized carve-out chosen per mode (boot
plan), a GTT budget that tracks mode and pressure, explicit eviction by TTM when the budget
shrinks, and per-process accounting so a runaway GPU client can be identified.

## Commands

```
orbis-mem status                    # who owns what right now
orbis-mem plan ai                   # carve-out + GTT plan for AI mode
sudo orbis-mem set-boot --mode ai   # writes vram.txt (+ gtt.txt with the GE loader); reboot
sudo orbis-mem set-budget 4096      # runtime GTT budget (MiB), GE kernel
journalctl -u orbis-memd            # policy decisions
```
