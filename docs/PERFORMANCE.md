# NickLabs Orbis — performance architecture (Nobara / CachyOS techniques on a PS4)

Status labels: BUILT, HOST-VERIFIED, PS4-VERIFIED, BLOCKED, UNVERIFIED (see
`DEVELOPMENT-CATALOG.md`). **No item on this page is PS4-VERIFIED yet.** Nothing here has been
benchmarked on a CUH-1215A, and this page claims no speed-ups. It describes what is wired and
how to measure it.

## 1. One controller, no competing daemons

PC, GAMING and AI are modes of one OS, switched with Orbis 0.2.0's `orbis mode pc|gaming|ai`
(unchanged). Each tunable has exactly one owner:

| Knob | Owner | Notes |
|---|---|---|
| systemd targets, services, VRAM request, zram-generator, `vm.swappiness`, `vm.vfs_cache_pressure`, power preset | Orbis 0.2.0 `orbisd` (mode engine) | untouched; `orbis-perf policy` lists these under `left_to_orbis_0_2_0` |
| GTT budget (unified memory) | `orbis-memd` (Graphics Engine 2.0, Python) | follows mode and memory pressure |
| CPU scheduler (EEVDF / BORE kernel / sched_ext), THP, MGLRU, I/O scheduler, IRQ affinity, background cpuset, GPU engine clock + fan threshold per mode, adaptive GPU clock, thermal guard, dmem VRAM protection, game sessions | **`orbis-perf`** (Rust, `orbis-perf.service`) | replaces the 1.0 `orbis-gpu-profile@` units, whose wants are removed on install and restored on removal |
| Manual settings made with `orbis-hw`/`orbis-gpu` | the user | `orbis-perf` reads `controls-state.json` / `profile-state.json` and leaves those knobs alone |

Competing managers: `falcond` is a package conflict. `orbis-perf doctor` reports `scx_loader`,
`tuned`, `auto-cpufreq` and `system76-power` as FAIL when they run. When `gamemoded` is running
(0.2.0 ships gamemode), `orbis-perf` leaves the CPU governor to it during that session.

Everything `orbis-perf` changes is written to a restore journal first
(`/var/lib/orbis/perf/restore-{mode,session}.json`). `orbis-perf restore` (also run when the
service stops) puts the old values back.

## 2. Mapping the Nobara / CachyOS techniques

| Technique | Orbis implementation | Status |
|---|---|---|
| EEVDF baseline with CachyOS defaults | kernel patch ge-0012 (CachyOS subset: EEVDF base-slice/migration defaults, `Kconfig.hz` options) | UNVERIFIED (applies) |
| BORE | separate kernel `linux-orbis-ps4-ge-bore` (`build-ge-kernel.sh --bore`, `patches/variants/bore`). BORE changes the base slice even when `sched_bore=0`, so it is never in the default kernel | UNVERIFIED (applies) |
| sched_ext (scx_lavd, scx_bpfland, scx_rusty, scx_flash) | GE kernel `CONFIG_SCHED_CLASS_EXT` + BTF (pahole); `scx-scheds` (Rust BPF schedulers) from the Arch snapshot; run as `orbis-scx@<name>.service` via `orbis-perf scx-exec`; arguments in `/etc/orbis/perf/scx/<name>.args` (default: `scx_lavd --performance`) | wiring BUILT; scheduler runs PS4 UNVERIFIED |
| Failure fallback | If a BPF scheduler exits or the kernel ejects it, the kernel returns to EEVDF on its own. `orbis-perf` counts failures in `/run/orbis/perf/scx-failures.json` and stops retrying a scheduler after two | HOST-VERIFIED (logic) |
| Measured scheduler choice | `orbis-perf sched bench [--seconds 8]`: wake-up latency (p50/p99/max) under an all-core load, plus throughput, per scheduler. "Gaming/interactive" picks the lowest p99 within 85 % of the best throughput. "AI/shader/batch" picks the highest throughput within 2× of the lowest p99. `orbis-perf sched set auto` uses the result | HOST-VERIFIED (rules); PS4 numbers pending |
| falcond-like game manager | `orbis-perf` daemon: detects Steam (reaper AppId), Wine `*.exe` and `orbis-run` sessions. Per-game profile: governor, SCLK, nice/ioprio for the game, `SCHED_IDLE` for background, dmem.low. Restores on exit | HOST-VERIFIED (fixture e2e) |
| NTSYNC / fsync / esync | `CONFIG_NTSYNC=y` (already in 0.2.0) → `/dev/ntsync` for Wine 10+/Proton 10+. `orbis-run` raises `RLIMIT_NOFILE` for esync | config UNVERIFIED |
| Preemption | `PREEMPT_DYNAMIC`, HZ 1000 (0.2.0) kept | — |
| IRQ affinity | GAMING: GPU interrupt on CPU 0, with background work confined to CPUs 0-1 (`orbis-background.slice` AllowedCPUs). The game keeps CPUs 2-7, i.e. the second Jaguar module and half of the first | wiring HOST-VERIFIED |
| cgroups / cpusets | GE kernel `CONFIG_CPUSETS`, `orbis-background.slice` (drop-in for `orbis-web`; the Vulkan probe runs there), user `orbis-game.slice` (CPU/IO weight 400) | UNVERIFIED |
| I/O schedulers | ADIOS (from CachyOS, in ge-0012), mq-deadline, BFQ; preference per mode (GAMING: adios → mq-deadline → bfq) | UNVERIFIED |
| MGLRU / le9 working-set protection | `CONFIG_LRU_GEN(_ENABLED)`, `min_ttl_ms=1000`, le9 ratios from CachyOS | UNVERIFIED |
| zram / zswap | 0.2.0 zram-generator (zstd, RAM/2 ≤ 4 GiB) kept; zswap built but off by default (no double compression) | — |
| THP | madvise (PC/GAMING; GAMING defrag `defer`), always (AI: llama.cpp benefits from large pages) | UNVERIFIED |
| dmem cgroup (dmemcg-booster idea) | `CONFIG_CGROUP_DMEM`: during a game session `dmem.low` = 75 % of VRAM on the game's cgroup and its ancestors, so the game's VRAM is the last evicted | HOST-VERIFIED (fixture) |
| Adaptive GPU clocks | PC: every 5 ticks, busy ≥ 15 % → max SCLK; ≤ 5 % for 3 samples → startup clock. GAMING/AI: max. Thermal guard: drop to startup clock at 85 °C, resume at 78 °C. GE kernel `ps4_sclk` (ge-0002) | HOST-VERIFIED (logic) |
| Compiler tuning | kernel `-march=btver2` (ge-0013, `CONFIG_X86_PS4_JAGUAR_TUNE`); Rust tools `-C target-cpu=btver2` + fat LTO. CachyOS's x86-64-v3 repositories are **never** used, because Jaguar lacks AVX2/FMA/BMI2 | BUILT (Rust) |
| AI-mode memory safety | 0.2.0 `MemoryHigh=92%` on `orbis-ai.slice` kept; drop-in adds `ManagedOOMMemoryPressure=kill` at 60 %, used when `systemd-oomd` runs (needs PSI, enabled in the GE kernel; enable with `systemctl enable --now systemd-oomd`) | UNVERIFIED |

## 3. Commands

```
orbis-perf status [--json]        # mode, scheduler, clocks, memory, PSI, thermals, Vulkan, games, time source
orbis-perf doctor [--json]        # dependency checks (exit 1 on FAIL); Orbis 0.2.0's `orbis doctor` is unchanged
orbis-perf policy gaming          # what a mode does, and what is left to Orbis 0.2.0
sudo orbis-perf sched list
sudo orbis-perf sched bench --seconds 8          # short, bounded; restores the previous scheduler
sudo orbis-perf sched set scx_lavd | eevdf | auto
orbis-perf games                  # detected sessions; profiles: /usr/share|/etc|~/.config orbis/games.d/*.json
sudo orbis-perf restore
```

Overrides: `/etc/orbis/perf/perf.json` with `{"modes": {"gaming": {"fan_threshold_c": 68,
"background_cpus": "0"}}}` (any field that `orbis-perf policy` shows).

## 4. How to measure on the console (to reach PS4-VERIFIED)

1. `sudo orbis-perf doctor`: every check should be PASS on the GE kernel. Record UNSUPPORTED items.
2. `sudo orbis-perf sched bench --seconds 8 --json > bench.json`, once per kernel (default and `-bore`).
3. In a game: `MANGOHUD=1 orbis-run %command%` and `orbis-perf status --json` during play. Compare
   frame-time p99 between `eevdf` and the bench pick. Short runs only.
4. Put the numbers in the release status before calling anything faster.
