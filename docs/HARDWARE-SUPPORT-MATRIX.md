# Hardware support matrix (Graphics Engine 2.0.0-r3)

Generated from the target registry (`targets/`, resolved in `targets/generated/registry.json`;
`./orbis-ge target list` prints the same table). Each capability has one of three states:

| State | Meaning |
|---|---|
| **verified** | shown on that hardware, citing a qualification record |
| **awaiting-hardware** | implemented here, not yet shown on that hardware |
| **unsupported** | not possible today, with a reason |

**No target is verified yet.** Nothing from Orbis 0.2.0 or the Graphics Engine has booted on a
PS4. The NickLabs CUH-1215A is the reference target: it is where qualification starts, but it
gets no special treatment in matching and is never assumed.

## Targets

| Target | Family | Models | Boards | Southbridge | GPU (PCI) | HDMI bridge | Kernel flavor | Status |
|---|---|---|---|---|---|---|---|---|
| cuh-10xx | Fat | CUH-10xx | SAA-001, CVN-K12 | Aeolia | Liverpool 0x9920 | MN86471A | aeolia-belize-6.15 | awaiting-hardware |
| cuh-11xx | Fat | CUH-11xx | SAB-001 | Aeolia | Liverpool 0x9920 | MN86471A | aeolia-belize-6.15 | awaiting-hardware |
| cuh-12xx | Fat | CUH-12xx | SAC-001 | Belize | Liverpool 0x9922 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| **cuh-1215a** (reference) | Fat | CUH-1215A | SAC-001 | Belize | Liverpool 0x9922 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-20xx-belize | Slim | CUH-20xx | SAD-001, SAD-003 (-11) | Belize | Liverpool 16 nm 0x9923 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-20xx-baikal | Slim | CUH-20xx | SAD-002 | Baikal | 0x9923 | MN864729 | — | **unsupported** |
| cuh-21xx-belize2 | Slim | CUH-21xx | SAE-001, SAE-003 | Belize 2 | 0x9923 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-21xx-baikal | Slim | CUH-21xx | SAE-002, SAE-004 | Baikal | 0x9923 | MN864729 | — | **unsupported** |
| cuh-22xx-belize2 | Slim | CUH-22xx | SAF-003, SAF-005 | Belize 2 | 0x9923 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-22xx-baikal | Slim | CUH-22xx | SAF-004, SAF-006 | Baikal | 0x9923 | MN864729 | — | **unsupported** |
| cuh-70xx | Pro | CUH-70xx | NVA-001 | Belize | Gladius 0x9924 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-71xx-belize2 | Pro | CUH-71xx | NVB-003 | Belize 2 | Gladius 0x9924 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-71xx-baikal | Pro | CUH-71xx | NVB-004 | Baikal | Gladius 0x9924 | MN864729 | — | **unsupported** |
| cuh-72xx-belize2 | Pro | CUH-72xx | NVG-001, NVG-003 | Belize 2 | Gladius 0x9924 | MN864729 | aeolia-belize-6.15 | awaiting-hardware |
| cuh-72xx-baikal | Pro | CUH-72xx | NVG-002, NVG-004 | Baikal | Gladius 0x9924 | MN864729 | — | **unsupported** |

A chassis number does not decide the southbridge. CUH-20xx, 21xx, 22xx, 71xx and 72xx were each
built on two different boards. The NickLabs secondary console, a CUH-2215B, can be a Belize 2 or a
Baikal board; Nick reports the Baikal path. `orbis-hw identify` settles it from PCI IDs, from
`hw.sce_subsys_subid` or from the board label.

## Capabilities (buildable targets)

Every buildable target has the same capability list. Each capability is **awaiting-hardware**,
except video decode (unsupported: UVD is research-only). Each line shows what implements the
capability and the evidence that would make it verified.

| Capability | Implemented by | Evidence needed |
|---|---|---|
| boot | 0.2.0 loader + GE loader patch, kernel flavor | boot log (`orbis-debug collect --hardware`) |
| southbridge-io | apcie, ahci, xhci, sky2, sdhci in the base tree | every Sony PCI function bound (`orbis-hw qualify`) |
| icc-power-fan | ps4-apcie-icc, ps4-fan + ge-0001 | fan threshold readable/writable, power button |
| rtc | ge-0011 RTC, ge-0010 loader wall clock | `orbis-time status` |
| cpufreq | ge-0007 Jaguar P-states | scaling driver present, P-state change |
| display-hdmi | ps4_bridge (MN86471A / MN864729) + ge-0005/0006 | picture at boot and after a mode set |
| hdmi-audio | ge-0004 | speaker-test over HDMI |
| gpu-3d | amdgpu CIK + Mesa 26.2.5 | `orbis-gpu qualify` PASS |
| gpu-clocks | ge-0002, ge-0009 (opt-in) | `ps4_sclk` state=max |
| ge-telemetry | ge-0008 | `orbis_engine_busy` under load |
| ge-unified-memory | stable-6.15/0001-0002, ge-0014/0015/0016, orbis-memd, orbis-run admission | `orbis-mem bench` PASS, budget enforced, admission ADMITTED |
| wifi-bt | mwifiex (Marvell) or mt76x8 (MT7668), by board | association on the NickLabs LAN |
| video-decode | — | unsupported: UVD is a research kernel only |

On Baikal targets every kernel-dependent capability is **unsupported**. The reason is the kernel
flavor (next section). The shared userspace (Mesa, orbis-native, orbis-graphics-engine, gaming)
does not depend on the southbridge.

## Why Baikal is unsupported

The evidence is listed below; the full analysis is in
`research/portforge/cuh-2215b-nicklabs/`.

- The Graphics Engine needs the 6.15 kernel interfaces: the TTM shrink helpers, the dmem cgroup and
  the stable TTM fixes.
- The only 6.15 PS4 flavor is `aeolia-belize-6.15` (feeRnt 6.15.4 crashnt-4.7 @108ef97, the Orbis
  0.2.0 base).
- The community kernels for Baikal are as follows. NickLabs has verified none of these reports.
  - `5.4.247-baikal-dfaus` in sony-jaguar-devs/ps4-linux: reported working on CUH-2215B,
    CUH-2216A (B1), CUH-7116B, CUH-7202B and CUH-7216B.
  - `x_experimental__5.15.15-fix-baikal`: reported non-functional.
  - A binary "Baikal 7.0.8" kernel from X-F1REBALL-X: reported working on CUH-2116A, with no
    pinned source.
- Difference between the 6.15.4 tree and the 5.4.247 Baikal branch (commit 7f796666a956),
  measured here:
  - The 6.15.4 tree already has the Baikal PCI IDs (apcie, ahci, sky2, xhci) and Baikal SATA PHY
    init in `ahci.c`.
  - It lacks the Baikal PCIe glue: `ps4-bpcie.c`, `ps4-bpcie-icc.c`, `ps4-bpcie-uart.c`,
    `baikal.h` and `aeolia-baikal.h` (about 1,650 lines).
  - It also lacks the `bpcie_*` declarations in `arch/x86/include/asm/ps4.h` and the
    `bpcie_status()` hook in `arch/x86/platform/ps4/ps4.c`.
- PortForge built an **unreviewed** candidate with the new files. It applies cleanly to the 6.15.4
  GE tree. Results:
  - checkpatch reports 124 errors, all in 5.4-era code;
  - the candidate has never been compiled;
  - API drift from 5.4 to 6.15 (IRQ domains, MSI) is expected;
  - merging the existing-file hunks by hand is listed in `merge-notes.md`.

## Unified memory per target

| | Fat / Slim (Liverpool) | Pro (Gladius) |
|---|---|---|
| Pool | 8 GiB GDDR5, about 176 GB/s | 8 GiB GDDR5, about 218 GB/s; the extra 1 GiB DDR3 belongs to OrbisOS, not Linux |
| Compute units | 18 | 36 |
| Carve-out | loader `vram.txt` (GE loader: multiple of 16 MiB, 64-4096) | same; the default is to be set from a Pro qualification run (twice the CUs, same pool) |
| GTT budget, reclaim, per-app limits | ge-0008/0014/0015/0016, admission | same |
| Zero copy | VK_EXT_external_memory_host via userptr + HMM; `orbis-mem bench` | same |
| SVM / fault replay | hardware-limited: GFX7 has no XNACK | same |

Every item above is awaiting-hardware. Baikal targets have no GE memory features until a Baikal
flavor exists.

## Sources

- [psdevwiki: PS4 Southbridge](https://www.psdevwiki.com/ps4/Southbridge): part numbers, boards
  and chassis.
- [tomfitzhenry/ps4-which-southbridge](https://github.com/tomfitzhenry/ps4-which-southbridge):
  `hw.sce_subsys_subid` values (Aeolia 0x1xxxx, Belize 0x2xxxx, Baikal 0x3xxxx, Belize 2
  0x40100).
- [feeRnt/ps4-linux-12xx](https://github.com/feeRnt/ps4-linux-12xx) and
  [sony-jaguar-devs/ps4-linux](https://github.com/sony-jaguar-devs/ps4-linux): branches, and
  model/southbridge/kernel reports.
- [codedwrench/ps4-linux](https://github.com/codedwrench/ps4-linux): upstream of those forks.
- [X-F1REBALL-X/PS4-Linux](https://github.com/X-F1REBALL-X/PS4-Linux): Belize 7.1.7 and Baikal
  7.0.8 binary kernels.
- [ConsoleMods: PS4 Buying Guide](https://consolemods.org/wiki/PS4:Buying_Guide): model family
  notes.
- Kernel tree: `include/linux/pci_ids.h` (Sony southbridge IDs), `ps4_bridge.c` (GPU 0x9920,
  0x9922, 0x9923, 0x9924 by family), `amdgpu_drv.c` (0x9924 = CHIP_GLADIUS).
- [kernel.org stable](https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git): TTM
  backports.
