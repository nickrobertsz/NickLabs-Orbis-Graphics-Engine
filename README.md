# NickLabs Orbis Graphics Engine 2.0.0

A separate release for **NickLabs Orbis Linux 0.2.0**. The reference target is the PS4
**CUH-1215A** (Belize southbridge, Liverpool GPU). Since 2.0.0-r3 a target registry covers every
Fat, Slim and Pro family; see `docs/HARDWARE-SUPPORT-MATRIX.md`. It contains:

* a GE kernel;
* a loader memory contract;
* Mesa 26.2.5;
* unified-memory policy;
* hardware management and the Black Box recorder;
* native Rust services for performance, time and gaming;
* a Windows-gaming stack;
* a hardware Porting Kit (target registry, `orbis-hw identify`, PortForge).

Orbis 0.2.0 and the Graphics Pack 1.0.0 are not changed. The release installs into a *copy* of a
0.2.0 image, and everything it changes can be rolled back.

**Status (2.0.0-r3): source complete for the current scope, not yet built on Nobara, nothing run
on a PS4. No target is verified; Baikal boards are unsupported (no 6.15 Baikal kernel).**
* The kernel series (2 stable backports, including CVE-2026-52949, plus 16 GE patches) is proven
  to apply but not compiled. Kernel compilation was refused in the authoring environment.
* Python and Rust userspace is tested on a host with fixtures: 130 + 40 tests.
* The Rust binaries and orbis-gfx-probe were built here for btver2 and passed the Jaguar ISA gate.

See `docs/DEVELOPMENT-CATALOG.md` for every item's state and evidence.

## What is in it

| Area | Where | What |
|---|---|---|
| Kernel | `kernel/` | 0.2.0 kernel + 2 stable TTM backports (CVE-2026-52949) + 16 patches: ge-0016 reclaim corrections, fan/thermal, SCLK restore, GDDR5, HDMI audio, bridge, cpufreq, telemetry + runtime GTT budget, SMU, loader wall clock, Aeolia RTC, CachyOS subset, Jaguar build, GPU-buffer shrinker + active GTT trim (ge-0014), GTT dmem cgroup region (ge-0015). Plus perf/GE config fragments, Rust-for-Linux, optional BORE kernel, and a research-only UVD kernel |
| Loader | `loader/` | exact VRAM MTRR split, `vram.txt`/`gtt.txt`, boot identity, time handoff |
| Mesa | `mesa/` | 26.2.5-201 (+lib32), PS4 patches |
| Engine (Python) | `diag/` | memory policy (orbis-memd), hardware API, Black Box + orbis-debug, video, AI tuning, qualify, stage-0 |
| Native (Rust) | `rust/`, `packages/orbis-native` | `orbis-perf` (one performance controller, sched_ext, game sessions, doctor), `orbis-time` (RTC/loader/last-good/LAN NTP), `orbis-games` + `orbis-run` (Vulkan capabilities → DXVK/VKD3D, runners, launcher, diagnosis) |
| Gaming | `packages/orbis-gaming`, `gaming/` | Lutris, Gamescope, MangoHud, scx-scheds; pinned GE-Proton/DXVK/VKD3D catalog + lock |
| Integration | `build/`, `integrator/`, `./orbis-ge` | pack build, image integration, stage-0, rollback, provenance, artifact selection, receipts per target |
| Porting Kit | `targets/`, `diag/orbis_targets/` | board-support registry, safe probe, deterministic matching, qualification records, PortForge |

## Commands (on Nobara)

```sh
./orbis-ge all --orbis-src ~/orbis-release/nicklabs-ps4-orbis-linux --target cuh-1215a --base-image <0.2.0 image> [--base-stage0-image <0.2.0 stage-0 image>]
./orbis-ge target list    # supported, awaiting-hardware and unsupported targets
./orbis-ge runners        # optional: offline Proton/DXVK bundle
```

Step by step, including first boot and rollback: `docs/GRAPHICS-ENGINE-NOBARA.md`.

## Documents

- `docs/DEVELOPMENT-CATALOG.md`: implemented vs pending, state + evidence per directive
- `docs/GRAPHICS-ENGINE.md`: engineering report
- `docs/GRAPHICS-ENGINE-NOBARA.md`: exact Nobara and console commands
- `docs/UNIFIED-MEMORY-ARCHITECTURE.md`, `docs/GRAPHICS-ENGINE-MEMORY.md`: shared GDDR5
- `docs/PERFORMANCE.md`, `docs/GAMING.md`, `docs/TIME.md`, `docs/RUST.md`
- `docs/GRAPHICS-ENGINE-BUG-LEDGER.md`: findings GE-B01…B32
- `docs/HARDWARE-SUPPORT-MATRIX.md`, `docs/TARGET-PORTING-GUIDE.md`, `docs/PORTFORGE-ARCHITECTURE.md`,
  `docs/REPRODUCIBLE-BUILD-RECIPES.md`: Porting Kit and reproducible builds
- Graphics Pack 1.0.0 documents (`GRAPHICS-*.md`) are kept for reference

## Licence and firmware

* MIT covers the Orbis-authored code (`LICENSE`).
* The kernel patches are GPL-2.0 (with source correspondence kept via the pinned trees).
* Mesa and the Arch packages keep their own licences.
* **No PS4 firmware is redistributed.**

NickLabs Orbis is an independent community project, not affiliated with Sony or AMD.
